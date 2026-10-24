"""The Linux family this server runs, and how to install packages and name services on it.

Shared code never calls a package manager itself: it asks this module. Each family
(Arch/Nyarch and Debian/Ubuntu) knows its package manager, the names its repositories
use for the packages NekoServer needs, what its services are called and which of its
settings files belong in host backups. See PORTING.md for how a family is added.

Run as  python3 host.py bootstrap  (install.sh does) to install the system packages
the apps need, and  python3 host.py repair  to undo repository settings an older
NekoServer left that stop the package manager working (install.sh runs it first).
"""
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType

from neko_common import atomic_write

OS_RELEASE = '/etc/os-release'
# Every system package the apps need outside their containers, by NekoServer's names.
APP_PACKAGES = ('docker', 'docker-compose', 'tailscale', 'caddy', 'samba', 'hdparm',
                'smartmontools', 'qrencode', 'xdg-utils')
# Tailscale's service has the same name on every family.
TAILSCALE_UNIT = 'tailscaled.service'


class Unsupported(RuntimeError):
    """This Linux family has no NekoServer adapter yet."""


def os_release(path=OS_RELEASE):
    """The KEY=value pairs of /etc/os-release, unquoted ({} if unreadable)."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _words(release, key):
    return release.get(key, '').lower().split()


# A package being set up together with this one: it meets any version asked of it.
ANY_VERSION = object()


def _alternative(text):
    """`name:arch (>= 1.2)` -> ('name', '>=', '1.2'); no version: ('name', None, None)."""
    name, _, version = text.partition('(')
    name = name.strip().split(':')[0]
    if not name:
        return None, None, None
    match = re.fullmatch(r'\s*(<<|<=|=|>=|>>)\s*(\S+?)\s*\)\s*', version)
    return (name, match[1], match[2]) if match else (name, None, None)


def _still_needed(group, met, pending, satisfies):
    """The unfinished package one dependency (`A (>= 1) | B`) still needs: none when one
    of its choices is already met, otherwise the first unfinished choice, as apt picks.
    `met`: {name: installed version, None for a name only provided without a version, or
    ANY_VERSION}; a versioned choice is met only by a version that satisfies it, and
    never by an unversioned "Provides" (Debian policy). `satisfies(version, op, wanted)`
    compares versions as dpkg does."""
    options = [option for option in (_alternative(a) for a in group.split('|')) if option[0]]
    for name, op, wanted in options:
        if name not in met:
            continue
        have = met[name]
        if op is None or have is ANY_VERSION or (have is not None and satisfies(have, op, wanted)):
            return None
    return next((name for name, _, _ in options if name in pending), None)


def _reason(result):
    """The last line a package manager printed, for error messages."""
    return ((result.stderr or result.stdout or '').strip().splitlines()[-1:] or ['no details'])[0]


def environment():
    """For package managers and systemctl: English messages (their output is read, and
    "Candidate:" is "Candidat :" in French), no questions on the terminal, and Ubuntu's
    needrestart left out: after an install it can restart running services on its own,
    a Docker someone already runs included."""
    return {**os.environ, 'LC_ALL': 'C', 'LANGUAGE': 'C', 'DEBIAN_FRONTEND': 'noninteractive',
            'NEEDRESTART_SUSPEND': '1', 'NEEDRESTART_MODE': 'l'}


# apt waits this long (seconds) for another package job, such as Ubuntu's automatic
# security updates, instead of failing at once because dpkg is busy.
APT_WAIT = ['-o', 'DPkg::Lock::Timeout=600']


def _fetch(url, timeout=60):
    """A repository signing key (at most 64 KB) from its maker's https address."""
    if not url.startswith('https://'):
        raise RuntimeError('Repository keys are only downloaded over https')
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(65536)


class Family:
    """One Linux family. Subclasses fill in the class attributes and the package-manager
    calls; everything else is shared."""
    name = ''
    label = ''
    manager = ''          # the package-manager program
    ids = ()              # os-release ID values of this family
    like = ()             # os-release ID_LIKE words of derivatives
    packages = MappingProxyType({})   # NekoServer's name -> this family's package name(s)
    services = MappingProxyType({})   # NekoServer's name -> this family's systemd unit
    settings = ()         # this family's own host settings, kept in host backups
    journal = None        # where an unfinished installation is recorded, for families
                          # whose packages need tidying afterwards (settle)
    root = Path('/')      # where system files are (a folder in tests)

    def __init__(self, run=subprocess.run, release=None):
        self.run = run
        self.release = os_release() if release is None else release

    @classmethod
    def matches(cls, release):
        return bool(set(_words(release, 'ID')) & set(cls.ids) or set(_words(release, 'ID_LIKE')) & set(cls.like))

    @classmethod
    def service(cls, wanted):
        """The systemd unit (with .service) this family uses for a NekoServer service name."""
        return cls.services.get(wanted, wanted + '.service')

    def path(self, absolute):
        return self.root / absolute.lstrip('/')

    def ready(self):
        """The package manager can be called."""
        return shutil.which(self.manager) is not None

    def names(self, wanted):
        """This family's package names for one of NekoServer's names."""
        found = self.packages.get(wanted, wanted)
        return list(found) if isinstance(found, tuple) else [found]

    @classmethod
    def reboot_pending(cls, root='/'):
        """An earlier update needs a restart before services are changed. (Arch's
        new-kernel check runs in install.sh, right after its system update.)"""
        return False

    def _call(self, argv, timeout, **extra):
        extra.setdefault('env', environment())
        return self.run(argv, capture_output=True, text=True, timeout=timeout, check=False, **extra)

    def available(self, wanted):
        """The configured repositories offer this package."""
        raise NotImplementedError

    def has(self, package):
        """This one package (in the family's own name) is installed."""
        raise NotImplementedError

    def installed(self, wanted):
        return self.ready() and all(self.has(name) for name in self.names(wanted))

    def _install(self, names, timeout):
        raise NotImplementedError

    def prepare(self, missing):
        """Before installing: check for conflicts and add vendor repositories. May return
        what settle() must know about the system as it was before (kept in the journal)."""

    def protect(self, before):
        """Just before installing: keep the new services from starting until settle()."""

    def wait_until_idle(self):
        """After an installation was cut off: wait until the package tools have stopped."""

    def repair(self, log=print):
        """Bring NekoServer's own package-manager settings up to date (an older release
        may have left some that stop the package manager working)."""

    @contextlib.contextmanager
    def locked(self):
        """One NekoServer package job at a time, whichever process runs it."""
        yield

    def settle(self, packages, before):
        """After installing, also after a failed or interrupted one: undo anything the
        packages did that NekoServer manages itself."""

    def packaged_example(self, absolute):
        """This settings file is known to be its package's untouched example (families
        without a record of what their packages wrote never know: None)."""

    def set_aside_samba_example(self, packages, before):
        """Samba's packaged example smb.conf, never anyone's own: it must not have existed
        before, and the package system's record of what the package wrote (ucf on Debian,
        rpm on Fedora) must say it is unchanged. With no such record it stays, and the Samba
        step asks for it to be reviewed."""
        config = self.path(SAMBA_CONFIG)
        if 'samba' in packages and not before.get('samba_config_existed', True) and self.packaged_example(SAMBA_CONFIG):
            config.replace(config.with_name('smb.conf.packaged'))

    def reconcile(self):
        """Finish an installation that stopped part-way, now or in an earlier run: the
        packages it did install are tidied up (settle) before anything else happens."""
        with self.locked():
            self._reconcile()

    def _reconcile(self):
        if self.journal is None:
            return
        record = self.path(self.journal)
        if not record.exists():
            return
        try:
            saved = json.loads(record.read_text())
            packages, before = saved['packages'], saved['before']
            if saved.get('format') != 'neko-package-install-v1' or not isinstance(before, dict) \
                    or not all(isinstance(name, str) for name in packages):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError('Unreadable record of an unfinished package installation: ' + str(record)) from None
        self.settle(packages, before)
        record.unlink()

    def install(self, wanted, timeout=900):
        """Install the packages that aren't installed yet; nothing else on the system is
        upgraded. Raises RuntimeError with the package manager's reason on failure."""
        if not self.ready():
            raise RuntimeError(self.label + ' package manager (' + self.manager + ') not found')
        with self.locked():
            self._reconcile()
            missing = [name for name in wanted if not self.installed(name)]
            if missing:
                self._install_missing(missing, timeout)
            return missing

    def _install_missing(self, missing, timeout):
        if {'docker', 'docker-compose'} & set(missing) and snap_docker(self.root):
            raise RuntimeError(SNAP_DOCKER)
        before = self.prepare(missing) or {}
        if self.journal is not None:
            atomic_write(self.path(self.journal), json.dumps(
                {'format': 'neko-package-install-v1', 'packages': missing, 'before': before}) + '\n')
        # Only the parts that are missing: a package already there is never reinstalled
        # or upgraded (that would restart a Docker someone already runs, for example).
        names = [real for name in missing for real in self.names(name) if not self.has(real)]
        try:
            self.protect(before)
            result = self._install(names, timeout)
        except subprocess.TimeoutExpired as exc:
            self._after_interruption()
            # The time actually waited (a family may allow more than asked).
            raise RuntimeError('Installing ' + ', '.join(missing) + ' did not finish within '
                               + str(int(exc.timeout or timeout) // 60) + ' minutes; run setup again') from None
        except BaseException:
            self._after_interruption()
            raise
        # Packages can be installed even when the whole command failed: tidy them first.
        self._reconcile()
        if result.returncode:
            raise RuntimeError('Installing ' + ', '.join(missing) + ' failed: ' + _reason(result))

    def existing_units(self, units):
        """The services that are already there before installing (a Docker someone runs,
        say): those are never stopped or switched off, only ones the packages newly bring."""
        states = self.unit_states(units) if units else {}
        return [unit for unit in units if states.get(unit, {}).get('LoadState') not in (None, 'not-found')]

    def unit_states(self, units):
        result = self._call(['systemctl', 'show', *units, '--property=Id,LoadState,UnitFileState,ActiveState'], 60)
        if result.returncode:
            raise RuntimeError('systemd could not report on ' + ', '.join(units) + ': ' + _reason(result))
        states = {}
        for block in result.stdout.split('\n\n'):
            fields = {key.strip(): value.strip() for key, value in (line.split('=', 1) for line in block.splitlines()
                                                                    if '=' in line)}
            if fields.get('Id'):
                states[fields['Id']] = fields
        return states

    def switch_off(self, units):
        """Disable and stop these services, then check that they really are. Units that
        aren't installed (not found) or are masked can't start and are left alone."""
        if not units:
            return
        states = self.unit_states(units)
        present = [unit for unit in units if states.get(unit, {}).get('LoadState') == 'loaded']
        if not present:
            return
        result = self._call(['systemctl', 'disable', '--now', '--quiet', *present], 120)
        states = self.unit_states(present)
        still = [unit for unit in present
                 if states.get(unit, {}).get('UnitFileState') not in ('disabled', 'masked')
                 or states.get(unit, {}).get('ActiveState') not in ('inactive', 'failed')]
        if result.returncode or still:
            raise RuntimeError('Could not switch off ' + ', '.join(still or present) + ' after installing'
                               + (': ' + _reason(result) if result.returncode else '')
                               + '. NekoServer starts these itself once their drive is ready; switch them '
                               'off (sudo systemctl disable --now ' + ' '.join(still or present)
                               + ') and run setup again.')

    def unmask(self, units):
        """Lift masks NekoServer set itself before installing."""
        if units:
            result = self._call(['systemctl', 'unmask', '--quiet', *units], 60)
            if result.returncode:
                raise RuntimeError('Could not unmask ' + ', '.join(units) + ': ' + _reason(result))

    def _after_interruption(self):
        """Tidy up once the package tools have stopped. If that isn't possible now, the
        record and the start protection stay for the next setup run."""
        with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
            self.wait_until_idle()
            self._reconcile()


class Arch(Family):
    name = 'arch'
    label = 'Arch Linux / Nyarch'
    manager = 'pacman'
    ids = ('arch', 'nyarch')
    like = ('arch',)
    services = MappingProxyType({'samba': 'smb.service', 'tailscale': TAILSCALE_UNIT})
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')
    wait = 600            # seconds to wait for another pacman to finish
    sleep = staticmethod(time.sleep)

    def available(self, wanted):
        return self.ready() and all(self._call(['pacman', '-Si', name], 60).returncode == 0
                                    for name in self.names(wanted))

    def has(self, package):
        return self._call(['pacman', '-Q', package], 30).returncode == 0

    def wait_until_idle(self):
        """pacman's lock file is there while another pacman (an updater, say) works: wait
        for it. (install.sh removes one left from before the last restart.)"""
        lock = self.path(PACMAN_LOCK)
        waited = 0
        while lock.exists():
            if waited >= self.wait:
                raise RuntimeError(f'pacman is still locked ({PACMAN_LOCK}) after {self.wait // 60} minutes: '
                                   'another installation is running. Let it finish, then run setup again. If '
                                   'nothing is installing, the lock is left from one that was cut off: remove it '
                                   f'(sudo rm {PACMAN_LOCK}), run  sudo pacman -Syu, then setup again.')
            self.sleep(5)
            waited += 5

    def _install(self, names, timeout):
        self.wait_until_idle()
        return self._call(['pacman', '-S', '--needed', '--noconfirm', *names], timeout)


class Repository:
    """A maker's apt repository: where it is, its signing key and its source line."""
    MARKER = '# Managed by NekoServer\n'

    def __init__(self, name, address, key_url, key_path, line, legacy_key=None):
        self.name, self.address, self.key_url, self.key_path, self.line = name, address, key_url, key_path, line
        self.legacy_key = legacy_key   # where 0.28.0 kept the key

    def listing(self, key, system, codename):
        return self.line.format(key=key, address=self.address, os=system, codename=codename)


DOCKER = Repository('docker', 'https://download.docker.com/linux/', 'https://download.docker.com/linux/{os}/gpg',
                    '/etc/apt/keyrings/docker.asc',
                    'deb [arch=amd64 signed-by={key}] {address}{os} {codename} stable\n')
TAILSCALE = Repository('tailscale', 'https://pkgs.tailscale.com/stable/',
                       'https://pkgs.tailscale.com/stable/{os}/{codename}.noarmor.gpg',
                       # Under /etc/apt/keyrings, so host backups keep it with the list.
                       '/etc/apt/keyrings/tailscale-archive-keyring.gpg',
                       'deb [signed-by={key}] {address}{os} {codename} main\n',
                       legacy_key='/usr/share/keyrings/tailscale-archive-keyring.gpg')
# Debian's samba package writes an example smb.conf; NekoServer writes its own.
SAMBA_CONFIG = '/etc/samba/smb.conf'
POLICY = '/usr/sbin/policy-rc.d'
# Held by NekoServer for each package job (install, tidy-up, repository repair).
PACKAGE_LOCK = '/run/nekoserver-packages.lock'
# There while a pacman works (created and deleted by pacman itself, never held open).
PACMAN_LOCK = '/var/lib/pacman/db.lck'
# Where snapd mounts installed snaps: Ubuntu's folder, and other systems'.
SNAP_FOLDERS = ('/snap', '/var/lib/snapd/snap')
# Tried on Ubuntu: the snap's Docker takes over /run/docker.sock at once, and after it is
# removed there is no socket until Docker is restarted.
SNAP_DOCKER = ('Docker is installed here as a snap. Its Docker takes over the socket every docker command uses '
               '(/run/docker.sock), so NekoServer\'s backups, updates and setup would reach it instead of the '
               'Docker that runs your apps. NekoServer uses Docker\'s official packages and does not mix them: if '
               'nothing else uses the snap\'s containers, remove it (sudo snap remove docker), restart Docker if it '
               'is installed (sudo systemctl restart docker.socket docker), then run setup again.')


def snap_docker(root='/'):
    """Docker is installed as a snap (its daemon runs as snap.docker.dockerd)."""
    return any((Path(root) / folder.lstrip('/') / 'docker').is_dir() for folder in SNAP_FOLDERS)
# Held while they work: apt's (released if apt-get is stopped) and dpkg's own (held
# until dpkg itself has finished, also when the apt-get that started it was stopped).
DPKG_LOCKS = ('/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock')
# Held while the package lists are refreshed (by apt-get update, or the system's apt-daily).
APT_LISTS_LOCK = '/var/lib/apt/lists/lock'
# deb-systemd-helper's record of units a package masked itself.
PACKAGE_MASKS = '/var/lib/systemd/deb-systemd-helper-masked'
OUR_POLICY = '#!/bin/sh\n# NekoServer: no service starts while packages install.\nexit 101\n'


def _deb822(text):
    """The stanzas of a deb822 file as {lower-case field name: value}."""
    for stanza in text.replace('\r\n', '\n').split('\n\n'):
        fields = _deb822_fields(stanza)
        if fields:
            yield fields


def _deb822_fields(stanza):
    """One stanza's fields. A value can go on over lines that start with a space or tab
    (a lone "." is an empty line); comment lines are skipped."""
    fields, name = {}, None
    for line in stanza.splitlines():
        if line.startswith('#'):
            continue
        if line[:1] in (' ', '\t') and name:
            fields[name] += '\n' + ('' if line.strip() == '.' else line.strip())
        elif ':' in line:
            name, value = line.split(':', 1)
            name = name.strip().lower()
            fields[name] = value.strip()
    return fields


def _stanzas(path, text):
    """The repository addresses of the enabled entries in a one-line .list file or a
    deb822 .sources file."""
    if path.suffix == '.sources':
        for fields in _deb822(text):
            if fields.get('enabled', 'yes').strip().lower() != 'no':
                yield from fields.get('uris', '').split()
    else:
        yield from (line for line in text.splitlines() if line.split()[:1] in (['deb'], ['deb-src']))


def _audit_packages(text):
    """The package names dpkg --audit lists (indented lines; an ":arch" suffix dropped)."""
    return {line.split()[0].split(':')[0] for line in text.splitlines() if line[:1] in (' ', '\t') and line.split()}


def _free(lock):
    """Nobody holds this lock file (a missing one is free too)."""
    if not lock.exists():
        return True
    try:
        with lock.open('a') as handle:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(handle, fcntl.LOCK_UN)
            return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _md5(path):
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


class PackageLock:
    """One NekoServer package job at a time across processes (setup, a retry, a step
    installing a missing tool), held at PACKAGE_LOCK; nested jobs share it."""
    wait = 1800           # seconds to wait for another package job to finish
    sleep = staticmethod(time.sleep)
    holding = 0           # nested package jobs in this process

    @contextlib.contextmanager
    def locked(self):
        if self.holding:
            self.holding += 1
            try:
                yield
            finally:
                self.holding -= 1
            return
        path = self.path(PACKAGE_LOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as handle:
            waited = 0
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if waited >= self.wait:
                        raise RuntimeError('Another NekoServer setup is installing packages; let it finish, '
                                           'then run setup again') from None
                    self.sleep(5)
                    waited += 5
            self.holding = 1
            try:
                yield
            finally:
                self.holding = 0
                fcntl.flock(handle, fcntl.LOCK_UN)


class Debian(PackageLock, Family):
    name = 'debian'
    label = 'Debian / Ubuntu'
    manager = 'apt-get'
    ids = ('debian', 'ubuntu')
    # Some Ubuntu-based systems (elementary OS, for one) name only Ubuntu.
    like = ('debian', 'ubuntu')
    packages = MappingProxyType({
        # Docker's official packages, never Debian's own docker.io.
        'docker': ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'),
        'docker-compose': 'docker-compose-plugin',
        'intel-media-driver': 'intel-media-va-driver',
        'libva-mesa-driver': 'mesa-va-drivers',
        'libva-utils': 'vainfo',
        'firefox': 'firefox-esr',
    })
    services = MappingProxyType({'samba': 'smbd.service', 'tailscale': TAILSCALE_UNIT})
    settings = ('/etc/apt/sources.list', '/etc/apt/sources.list.d', '/etc/apt/preferences.d',
                '/etc/apt/keyrings', '/etc/default/locale', '/etc/default/keyboard')
    journal = '/var/lib/nekoserver-host/package-install.json'
    # Packages from their makers' own repositories (Debian doesn't carry them).
    repositories = MappingProxyType({'docker': DOCKER, 'docker-compose': DOCKER, 'tailscale': TAILSCALE})
    # Services these packages enable and start on their own. NekoServer starts them
    # itself once their storage, settings and guards are in place (Docker must never
    # start on the system disk), so they are kept from starting and left switched off.
    autostarted = MappingProxyType({
        'docker': ('docker.service', 'docker.socket'), 'caddy': ('caddy.service',),
        # (samba-ad-dc.service comes masked by the package itself and is left as it is.
        # winbind comes with samba here and is never used by a standalone server; the
        # settings backup refuses to run beside it.)
        'samba': ('smbd.service', 'nmbd.service', 'winbind.service'), 'tailscale': (TAILSCALE_UNIT,)})
    # Debian's own container packages that would conflict with Docker's.
    conflicts = ('docker.io', 'docker-compose', 'docker-doc', 'podman-docker', 'containerd', 'runc')

    def __init__(self, run=subprocess.run, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch
        self.refreshed = False
        self.holding = 0      # nested package jobs in this process
        if self.ubuntu():
            self.packages = MappingProxyType({**type(self).packages, 'firefox': 'firefox'})

    def ubuntu(self):
        return 'ubuntu' in _words(self.release, 'ID') + _words(self.release, 'ID_LIKE')

    def vendor_release(self):
        """(distribution, codename) as Docker's and Tailscale's repositories name them."""
        codename = self.release.get('VERSION_CODENAME', '')
        if self.ubuntu():
            # Derivatives such as Linux Mint name the Ubuntu release they are built on.
            return 'ubuntu', (self.release.get('UBUNTU_CODENAME') or codename).lower()
        # Derivatives such as LMDE name the Debian release they are built on.
        return 'debian', (self.release.get('DEBIAN_CODENAME') or codename).lower()

    @classmethod
    def reboot_pending(cls, root='/'):
        return (Path(root) / 'run/reboot-required').exists()

    def refresh(self):
        """Refresh the package lists (never an upgrade), once. The system refreshes them
        itself now and then (apt-daily): apt-get waits for dpkg's lock, not for the lists'
        lock, so that wait is here (apt-get names the holder if it outlasts it)."""
        if self.refreshed:
            return
        waited = 0
        while waited < self.wait and not _free(self.path(APT_LISTS_LOCK)):
            self.sleep(5)
            waited += 5
        result = self._call(['apt-get', *APT_WAIT, 'update'], 1200)
        if result.returncode:
            raise RuntimeError('Refreshing the package lists failed: ' + _reason(result))
        self.refreshed = True

    def has(self, package):
        # "install ok installed", or "hold ok installed" for a package someone holds.
        result = self._call(['dpkg-query', '-W', '-f=${Status}', package], 30)
        return result.returncode == 0 and result.stdout.split()[1:] == ['ok', 'installed']

    def available(self, wanted):
        if not self.ready():
            return False
        self.refresh()
        for name in self.names(wanted):
            policy = self._call(['apt-cache', 'policy', name], 60).stdout
            if 'Candidate:' not in policy or 'Candidate: (none)' in policy:
                return False
        return True

    def sources(self, address):
        """Every apt source file that already uses this repository address (one-line
        .list files and deb822 .sources files alike, whatever they are called)."""
        folder = self.path('/etc/apt/sources.list.d')
        files = [self.path('/etc/apt/sources.list'), *sorted(folder.glob('*.list')), *sorted(folder.glob('*.sources'))]
        found = []
        for path in files:
            try:
                text = path.read_text(errors='replace')
            except OSError:
                continue
            if any(address in entry for entry in _stanzas(path, text)):
                found.append(path)
        return found

    def _ours(self, repository, listing, system, codename):
        """The list is NekoServer's: marked, or exactly what 0.28.0 wrote."""
        text = listing.read_text(errors='replace')
        legacy = repository.listing(repository.legacy_key or repository.key_path, system, codename)
        return text.startswith(Repository.MARKER) or text == legacy

    def fetch_key(self, repository, system, codename):
        """The repository's signing key, or an error that says what went wrong: no
        internet, or a release the maker doesn't package for yet (their key and package
        addresses name the release, so a new Ubuntu gets "404" until they add it)."""
        url = repository.key_url.format(os=system, codename=codename)
        name = repository.name.capitalize()
        try:
            data = self.fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(f'{name} does not offer packages for this release yet ({system} "{codename}": '
                                   f'{url} was not found). Wait for {name} to add it, or install a release it '
                                   f'supports, then run setup again') from None
            raise RuntimeError(f'Downloading the {name} repository key failed (HTTP {exc.code} from {url}); '
                               'try again later') from None
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f'Downloading the {name} repository key failed ({getattr(exc, "reason", exc)}). '
                               'Check that this PC is connected to the internet, then run setup again') from None
        if not data or len(data) >= 65536:
            raise RuntimeError(f'The {name} repository key downloaded from {url} is not a key file; try again later')
        return data

    def add_repository(self, repository):
        """Make a maker's repository usable. Someone else's definition of it, in any
        file, is left alone (a second one makes apt refuse to work); NekoServer's own
        is brought up to date and its key restored when missing."""
        system, codename = self.vendor_release()
        if not codename:
            raise RuntimeError('Cannot tell this system\'s release codename for the ' + repository.name + ' repository')
        listing = self.path('/etc/apt/sources.list.d/' + repository.name + '.list')
        found = self.sources(repository.address)
        ours = listing in found and self._ours(repository, listing, system, codename)
        if ours and len(found) > 1:
            listing.unlink()      # someone else defines it too: theirs is used
            self.refreshed = False
            return False
        if found and not ours:
            return False
        if listing.exists() and not ours:
            raise RuntimeError(str(listing) + ' is someone else\'s and does not use ' + repository.address
                               + '; rename or remove it, then run setup again.')
        desired = Repository.MARKER + repository.listing(repository.key_path, system, codename)
        key = self.path(repository.key_path)
        if ours and listing.read_text() == desired and key.is_file() and key.stat().st_size:
            return False
        data = self.fetch_key(repository, system, codename)
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        atomic_write(key, data, 0o644)
        atomic_write(listing, desired.encode(), 0o644)
        self.refreshed = False
        return True

    def wait_until_idle(self):
        """Wait until neither apt nor dpkg is working any more."""
        waited = 0
        while not all(_free(self.path(lock)) for lock in DPKG_LOCKS):
            if waited >= self.wait:
                raise RuntimeError('apt or dpkg is still working (another installation, or one that was cut '
                                   'off). New services stay switched off; run setup again once it has finished.')
            self.sleep(5)
            waited += 5

    def finish_configuring(self, names):
        """Packages an interrupted installation left unpacked but not set up are set up
        now, while their services are still kept from starting: run later by hand
        (dpkg --configure -a), their setup would start those services unprotected."""
        audit = self._call(['dpkg', '--audit'], 60)
        if audit.returncode and not audit.stdout.strip():
            raise RuntimeError('dpkg could not say which packages are unfinished: ' + _reason(audit)
                               + '. Their services stay switched off; run setup again.')
        pending = _audit_packages(audit.stdout)
        ours = [name for name in names if name in pending]
        if not ours:
            return              # nothing of this installation (someone else's is theirs)
        # Only these and the unfinished packages they need, never someone else's.
        result = self._call(['dpkg', '--configure', *self._with_dependencies(ours, pending)], 1800)
        if result.returncode:
            raise RuntimeError('dpkg could not finish setting up the packages: ' + _reason(result)
                               + '. Their services stay switched off; fix the problem, then run setup again.')

    def _with_dependencies(self, names, pending):
        """These packages plus the unfinished packages they need, directly or not. For a
        choice (`A | B`) nothing is added when one of them is already set up (or provided
        by a package that is); otherwise only the first unfinished one, as apt chose."""
        ready = self._ready(pending)
        chosen, queue = set(names), list(names)
        while queue:
            result = self._call(['dpkg-query', '-W', '-f=${Pre-Depends}, ${Depends}', queue.pop()], 30)
            for group in (result.stdout if result.returncode == 0 else '').split(','):
                needed = _still_needed(group, {**ready, **dict.fromkeys(chosen, ANY_VERSION)}, pending, self.satisfies)
                if needed:
                    chosen.add(needed)
                    queue.append(needed)
        return sorted(chosen)

    def satisfies(self, version, op, wanted):
        """`version op wanted` as dpkg compares Debian versions."""
        return self._call(['dpkg', '--compare-versions', version, op, wanted], 30).returncode == 0

    def _ready(self, pending):
        """{package: version} installed and set up (not unfinished), also when held, plus
        the names they provide (with the provided version, or None when unversioned)."""
        result = self._call(['dpkg-query', '-W', '-f=${db:Status-Abbrev}|${Package}|${Version}|${Provides}\n'], 60)
        ready = {}
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            status, package, version, provides = (line.split('|', 3) + ['', '', ''])[:4]
            # Wanted install or hold (i/h), installed (i), no error flag: "ii " or "hi ".
            if status[:2] not in ('ii', 'hi') or status[2:3].strip() or package in pending:
                continue
            ready[package] = version
            for given in provides.split(','):
                name, op, provided = _alternative(given)
                if name:
                    ready.setdefault(name, provided if op == '=' else None)
        return ready

    def repair(self, log=print):
        """Bring NekoServer's own repository definitions up to date, whether or not any
        package needs installing (on an upgrade, everything usually is installed):
        - 0.28.0 could add Docker's or Tailscale's repository next to someone else's
          definition of it, which stops apt from working at all: its copy goes;
        - 0.28.0 kept Tailscale's key outside host backups: it moves to /etc/apt/keyrings;
        - a restored definition whose key is missing gets the key back."""
        with self.locked():
            system, codename = self.vendor_release()
            for repository in dict.fromkeys(self.repositories.values()):
                listing = self.path('/etc/apt/sources.list.d/' + repository.name + '.list')
                if listing not in self.sources(repository.address) \
                        or not self._ours(repository, listing, system, codename):
                    continue
                try:
                    self.add_repository(repository)
                except (OSError, RuntimeError, http.client.HTTPException) as exc:
                    if not self._usable(listing):
                        raise RuntimeError('The ' + repository.name + ' repository key is missing and could not '
                                           'be downloaded: ' + str(exc)) from None
                    log('Keeping the ' + repository.name + ' repository as it is for now (' + str(exc) + ')')

    def _usable(self, listing):
        """The key the definition points to is there (apt can use it as it is)."""
        found = re.search(r'signed-by=([^\s\]]+)', listing.read_text(errors='replace'))
        key = self.path(found.group(1)) if found else None
        return bool(key and key.is_file() and key.stat().st_size)

    def prepare(self, missing):
        self.refuse_conflicts(missing)
        for repository in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(repository)
        self.refresh()
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        existing = self.existing_units(units)
        new = [unit for unit in units if unit not in existing]
        self.check_policy(new)
        return {'samba_config_existed': self.path(SAMBA_CONFIG).exists(), 'units_before': existing,
                'new_units': new}

    def protect(self, before):
        """The new services are masked (they can't start, not even after a restart) and
        policy-rc.d stops the packages from starting anything while they install. Both
        stay until settle() has switched the services off."""
        new = before.get('new_units', [])
        if new:
            result = self._call(['systemctl', 'mask', '--quiet', *new], 60)
            if result.returncode:
                raise RuntimeError('Could not keep ' + ', '.join(new) + ' from starting: ' + _reason(result))
        if not self.path(POLICY).exists():
            atomic_write(self.path(POLICY), OUR_POLICY.encode(), 0o755)

    def refuse_conflicts(self, missing):
        if not {'docker', 'docker-compose'} & set(missing):
            return
        found = [name for name in self.conflicts if self.has(name)]
        if found:
            raise RuntimeError('This system has its own Docker packages installed (' + ', '.join(found)
                               + '). NekoServer uses Docker\'s official packages and does not mix or '
                               'replace them. Remove them yourself if nothing else uses them, then run '
                               'setup again.')

    def check_policy(self, units):
        """A policy-rc.d that someone else put there must already refuse to start these
        services while packages install; NekoServer won't replace it."""
        policy = self.path(POLICY)
        if not policy.exists() or policy.read_text(errors='replace') == OUR_POLICY:
            return
        allowed = [unit for unit in units if not self._refuses(policy, unit)]
        if allowed:
            raise RuntimeError(str(POLICY) + ' (someone else\'s) would let ' + ', '.join(allowed) + ' start while '
                               'installing, before NekoServer has its drive and settings ready. Move it aside '
                               'for the installation (sudo mv ' + POLICY + ' ' + POLICY + '.saved), run setup '
                               'again, then move it back.')

    def _refuses(self, policy, unit):
        """The policy answers "not allowed" (101) to starting this service. One that hangs
        or can't run refuses nothing."""
        try:
            return self._call([str(policy), unit, 'start'], 30).returncode == 101
        except (OSError, subprocess.SubprocessError):
            return False

    def _install(self, names, timeout):
        return self._call(['apt-get', *APT_WAIT, 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', *names],
                          timeout + 600)

    def _reconcile(self):
        policy = self.path(POLICY)
        ours = policy.is_file() and policy.read_text(errors='replace') == OUR_POLICY
        if ours or self.path(self.journal).exists():
            # A package process from a cut-off installation may still be working:
            # nothing is tidied and no protection lifted until apt and dpkg have stopped.
            self.wait_until_idle()
        super()._reconcile()
        # Start protection goes only once nothing is left to tidy up.
        if ours:
            policy.unlink()

    def settle(self, packages, before):
        self.finish_configuring([real for name in packages for real in self.names(name)])
        self.set_aside_samba_example(packages, before)
        existing = before.get('units_before', [])
        new = [unit for name in packages for unit in self.autostarted.get(name, ()) if unit not in existing]
        self.unmask([unit for unit in new if unit in before.get('new_units', [])])
        try:
            self.switch_off(new)
        except RuntimeError:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._call(['systemctl', 'mask', '--quiet', *new], 60)   # stay protected until next time
            raise

    def unmask(self, units):
        """Lift NekoServer's own masks; one a package set itself stays."""
        ours = [unit for unit in units if not self.path(PACKAGE_MASKS + '/' + unit).exists()]
        if ours:
            result = self._call(['systemctl', 'unmask', '--quiet', *ours], 60)
            if result.returncode:
                raise RuntimeError('Could not unmask ' + ', '.join(ours) + ': ' + _reason(result))

    def packaged_example(self, absolute):
        """This settings file is still exactly what its package wrote (ucf's checksum of it).
        Without that record nothing is assumed: it may be someone's settings."""
        path = self.path(absolute)
        try:
            records = self.path('/var/lib/ucf/hashfile').read_text(errors='replace').splitlines()
        except OSError:
            return False
        recorded = [line.split()[0] for line in records if line.split()[1:] == [absolute]]
        return bool(recorded) and path.is_file() and recorded[-1] == _md5(path)


class RpmRepository:
    """A maker's dnf repository, written from known text rather than a downloaded .repo
    file; dnf checks its packages with the maker's key from the https address it names."""
    MARKER = '# Managed by NekoServer\n'

    def __init__(self, name, maker, address, text, release_url=None):
        self.name, self.maker, self.address, self.text = name, maker, address, text
        self.release_url = release_url   # answers only for a Fedora release the maker packages


DOCKER_RPM = RpmRepository(
    'docker-ce', 'Docker', 'https://download.docker.com/linux/fedora/',
    '[docker-ce-stable]\nname=Docker CE Stable - $basearch\n'
    'baseurl=https://download.docker.com/linux/fedora/$releasever/$basearch/stable\n'
    'enabled=1\ngpgcheck=1\ngpgkey=https://download.docker.com/linux/fedora/gpg\n',
    release_url='https://download.docker.com/linux/fedora/{release}/x86_64/stable/repodata/repomd.xml')
TAILSCALE_RPM = RpmRepository(
    'tailscale', 'Tailscale', 'https://pkgs.tailscale.com/stable/fedora/',
    '[tailscale-stable]\nname=Tailscale stable\nbaseurl=https://pkgs.tailscale.com/stable/fedora/$basearch\n'
    'enabled=1\ntype=rpm\nrepo_gpgcheck=1\ngpgcheck=1\ngpgkey=https://pkgs.tailscale.com/stable/fedora/repo.gpg\n')


class Fedora(PackageLock, Family):
    name = 'fedora'
    label = 'Fedora'
    manager = 'dnf'
    ids = ('fedora',)
    # RHEL and its rebuilds name fedora in ID_LIKE, but their repositories (and Docker's
    # for them) differ: not covered yet.
    like = ()
    packages = MappingProxyType({
        # Docker's official packages, never Fedora's own moby-engine.
        'docker': ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'),
        'docker-compose': 'docker-compose-plugin',
        'libva-mesa-driver': 'mesa-va-drivers',
    })
    services = MappingProxyType({'samba': 'smb.service', 'tailscale': TAILSCALE_UNIT})
    settings = ('/etc/dnf/dnf.conf', '/etc/yum.repos.d')
    journal = '/var/lib/nekoserver-host/package-install.json'
    repositories = MappingProxyType({'docker': DOCKER_RPM, 'docker-compose': DOCKER_RPM, 'tailscale': TAILSCALE_RPM})
    # Services these packages may enable on their own (presets, scriptlets): kept from
    # starting while they install, then left switched off until NekoServer's own steps
    # start them on the right drive, as on Debian.
    autostarted = MappingProxyType({
        'docker': ('docker.service', 'docker.socket'), 'caddy': ('caddy.service',),
        'samba': ('smb.service', 'nmb.service'), 'tailscale': (TAILSCALE_UNIT,)})
    # Fedora's own container packages that would conflict with Docker's.
    conflicts = ('moby-engine', 'podman-docker', 'containerd')

    def __init__(self, run=subprocess.run, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch

    def has(self, package):
        return self._call(['rpm', '-q', '--quiet', package], 30).returncode == 0

    def available(self, wanted):
        if not self.ready():
            return False
        for name in self.names(wanted):
            found = self._call(['dnf', '-q', 'repoquery', '--available', '--latest-limit', '1',
                                '--qf', '%{name}\n', name], 600)
            if found.returncode or name not in found.stdout.split():
                return False
        return True

    def _install(self, names, timeout):
        return self._call(['dnf', 'install', '-y', *names], timeout + 600)

    def repo_files(self, address):
        """Every dnf repository file that already uses this address."""
        found = []
        for path in sorted(self.path('/etc/yum.repos.d').glob('*.repo')):
            try:
                if address in path.read_text(errors='replace'):
                    found.append(path)
            except OSError:
                continue
        return found

    def release_number(self):
        """The release as the makers' repositories name it (Fedora: its version)."""
        return self.release.get('VERSION_ID', '')

    def check_release(self, repository):
        """The maker packages this release (a new one can take them a while)."""
        release = self.release_number()
        system = self.release.get('NAME') or self.label
        url = repository.release_url.format(release=release)
        try:
            self.fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(f'{repository.maker} does not offer packages for {system} {release} yet ({url} '
                                   f'was not found). Wait for {repository.maker} to add it, or use a release '
                                   'it supports, then run setup again') from None
            raise RuntimeError(f'Checking the {repository.maker} repository failed (HTTP {exc.code} from {url}); '
                               'try again later') from None
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(f'Reaching the {repository.maker} repository failed ({getattr(exc, "reason", exc)}). '
                               'Check that this PC is connected to the internet, then run setup again') from None

    def add_repository(self, repository):
        """Make a maker's repository usable. Someone else's definition of it is used as it
        is (two would make dnf warn about duplicates); NekoServer's own is kept current."""
        listing = self.path('/etc/yum.repos.d/nekoserver-' + repository.name + '.repo')
        found = self.repo_files(repository.address)
        ours = listing in found and listing.read_text(errors='replace').startswith(RpmRepository.MARKER)
        if ours and len(found) > 1:
            listing.unlink()      # someone else defines it too: theirs is used
            return False
        if found and not ours:
            return False
        desired = RpmRepository.MARKER + repository.text
        if ours and listing.read_text(errors='replace') == desired:
            return False
        if repository.release_url:
            self.check_release(repository)
        atomic_write(listing, desired.encode(), 0o644)
        return True

    def prepare(self, missing):
        if {'docker', 'docker-compose'} & set(missing):
            found = [name for name in self.conflicts if self.has(name)]
            if found:
                raise RuntimeError('This system has its own container packages installed (' + ', '.join(found)
                                   + '). NekoServer uses Docker\'s official packages and does not mix or replace '
                                   'them. Remove them yourself if nothing else uses them (sudo dnf remove '
                                   + ' '.join(found) + '), then run setup again.')
        for repository in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(repository)
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        existing = self.existing_units(units)
        return {'samba_config_existed': self.path(SAMBA_CONFIG).exists(), 'units_before': existing,
                'new_units': [unit for unit in units if unit not in existing]}

    def protect(self, before):
        """The new services are masked while their packages install (a preset or
        scriptlet can't enable or start them) and until settle() has switched them off."""
        new = before.get('new_units', [])
        if new:
            result = self._call(['systemctl', 'mask', '--quiet', *new], 60)
            if result.returncode:
                raise RuntimeError('Could not keep ' + ', '.join(new) + ' from starting: ' + _reason(result))

    def settle(self, packages, before):
        self.set_aside_samba_example(packages, before)
        existing = before.get('units_before', [])
        new = [unit for name in packages for unit in self.autostarted.get(name, ()) if unit not in existing]
        self.unmask([unit for unit in new if unit in before.get('new_units', [])])
        try:
            self.switch_off(new)
        except RuntimeError:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._call(['systemctl', 'mask', '--quiet', *new], 60)   # stay protected until next time
            raise

    def packaged_example(self, absolute):
        """This settings file is still exactly what its package wrote: rpm's record of the
        file (size, checksum, owner, time) shows no change. Without a record: not assumed."""
        if not self.path(absolute).is_file():
            return False
        owner = self._call(['rpm', '-qf', '--qf', '%{NAME}\n', absolute], 30)
        if owner.returncode or not owner.stdout.split():
            return False
        check = self._call(['rpm', '-V', owner.stdout.split()[0]], 120)
        return not any(line.split()[-1:] == [absolute] for line in check.stdout.splitlines())


class Rhel(Fedora):
    """RHEL and its rebuilds (Rocky Linux, AlmaLinux, CentOS Stream), version 10 and newer:
    Fedora's way of working with dnf, other makers' repositories for Docker and
    Tailscale, and EPEL (Fedora's extra packages for them) for Caddy and qrencode."""
    name = 'rhel'
    label = 'RHEL / Rocky Linux / AlmaLinux'
    ids = ('rhel', 'rocky', 'almalinux', 'centos')
    like = ('rhel',)
    # Docker's own instructions for RHEL remove runc (RHEL's or EPEL's) and podman-docker:
    # they clash with Docker's containerd.io and its docker command.
    conflicts = ('moby-engine', 'podman-docker', 'containerd', 'runc')
    epel = frozenset({'caddy', 'qrencode'})
    EPEL_ADDRESS = 'repo=epel-'      # in every EPEL repository definition (its metalink)

    def __init__(self, run=subprocess.run, release=None, fetch=_fetch):
        super().__init__(run, release, fetch)
        # Docker packages RHEL itself under rhel/ and its rebuilds under centos/; Tailscale
        # packages them all under rhel/<major version>.
        docker = 'rhel' if self.release.get('ID') == 'rhel' else 'centos'
        major = self.release_number()
        docker_rpm = RpmRepository(
            'docker-ce', 'Docker', f'https://download.docker.com/linux/{docker}/',
            '[docker-ce-stable]\nname=Docker CE Stable - $basearch\n'
            f'baseurl=https://download.docker.com/linux/{docker}/$releasever/$basearch/stable\n'
            f'enabled=1\ngpgcheck=1\ngpgkey=https://download.docker.com/linux/{docker}/gpg\n',
            release_url=f'https://download.docker.com/linux/{docker}/{{release}}/x86_64/stable/repodata/repomd.xml')
        tailscale_rpm = RpmRepository(
            'tailscale', 'Tailscale', f'https://pkgs.tailscale.com/stable/rhel/{major}/',
            f'[tailscale-stable]\nname=Tailscale stable\nbaseurl=https://pkgs.tailscale.com/stable/rhel/{major}/$basearch\n'
            f'enabled=1\ntype=rpm\nrepo_gpgcheck=1\ngpgcheck=1\ngpgkey=https://pkgs.tailscale.com/stable/rhel/{major}/repo.gpg\n',
            release_url='https://pkgs.tailscale.com/stable/rhel/{release}/x86_64/repodata/repomd.xml')
        self.repositories = MappingProxyType({'docker': docker_rpm, 'docker-compose': docker_rpm,
                                              'tailscale': tailscale_rpm})

    def release_number(self):
        # "10.2" is release 10 to Docker and Tailscale (dnf's $releasever is "10" too).
        return self.release.get('VERSION_ID', '').split('.')[0]

    def epel_ready(self):
        return self.has('epel-release') or bool(self.repo_files(self.EPEL_ADDRESS))

    def add_epel(self):
        """EPEL's release package: from the system's own repositories on Rocky, AlmaLinux
        and CentOS Stream, from EPEL's site on RHEL itself. dnf then checks EPEL's packages
        with EPEL's key, which that package brings."""
        if self.epel_ready():
            return False
        source = ('https://dl.fedoraproject.org/pub/epel/epel-release-latest-' + self.release_number()
                  + '.noarch.rpm') if self.release.get('ID') == 'rhel' else 'epel-release'
        result = self._call(['dnf', 'install', '-y', source], 900)
        if result.returncode:
            raise RuntimeError('Adding EPEL, where Caddy and qrencode come from, failed: ' + _reason(result))
        return True

    def available(self, wanted):
        if self.ready() and set(self.names(wanted)) & self.epel:
            self.add_epel()
        return super().available(wanted)

    def prepare(self, missing):
        if self.epel & {real for name in missing for real in self.names(name)}:
            self.add_epel()
        return super().prepare(missing)


class Suse(Fedora):
    """openSUSE Leap 16 and newer, Tumbleweed and Slowroll: rpm as on Fedora, zypper for
    the packages, and everything from openSUSE's own repositories (Docker, Compose, Caddy
    and Tailscale included), as on Arch. SLES names only suse, has other repositories and
    a subscription: not covered."""
    name = 'suse'
    label = 'openSUSE'
    manager = 'zypper'
    ids = ('opensuse-leap', 'opensuse-tumbleweed', 'opensuse-slowroll', 'opensuse')
    like = ('opensuse',)
    packages = MappingProxyType({
        'docker': ('docker', 'docker-buildx'),
        'docker-compose': 'docker-compose',
        'libva-mesa-driver': 'Mesa-libva',
    })
    settings = ('/etc/zypp/zypp.conf', '/etc/zypp/repos.d')
    repositories = MappingProxyType({})
    conflicts = ()
    # zypper waits this long (seconds) for another zypper or YaST to finish, instead of
    # failing at once ("System management is locked").
    LOCK_WAIT = '600'

    def _call(self, argv, timeout, **extra):
        if argv[0] == 'zypper':
            extra.setdefault('env', {**environment(), 'ZYPP_LOCK_TIMEOUT': self.LOCK_WAIT})
        return super()._call(argv, timeout, **extra)

    def available(self, wanted):
        if not self.ready():
            return False
        # zypper answers 104 when no package matches.
        return all(self._call(['zypper', '--non-interactive', '--quiet', 'search', '--match-exact',
                               '--type', 'package', name], 300).returncode == 0
                   for name in self.names(wanted))

    def _install(self, names, timeout):
        return self._call(['zypper', '--non-interactive', 'install', '--no-recommends', *names], timeout + 600)


FAMILIES = (Arch, Debian, Fedora, Rhel, Suse)


def detect(path=OS_RELEASE):
    """The family class for this system, or None when NekoServer has no adapter for it."""
    release = os_release(path)
    return next((family for family in FAMILIES if family.matches(release)), None)


def current(run=subprocess.run, path=OS_RELEASE):
    """This system's family, ready to use. Raises Unsupported on other systems."""
    family = detect(path)
    release = os_release(path)
    if family is None:
        named = release.get('PRETTY_NAME') or release.get('ID') or 'this system'
        raise Unsupported('Automatic setup supports ' + ' and '.join(f.label for f in FAMILIES)
                          + '; ' + named + ' is not supported yet')
    return family(run, release)


def supported(path=OS_RELEASE):
    return detect(path) is not None


def family_or_default(path=OS_RELEASE):
    """For reading only (names of services and settings files): this system's family, or
    Arch when it has none, which was NekoServer's only family before 0.28.0."""
    return detect(path) or Arch


def service(wanted, path=OS_RELEASE):
    return family_or_default(path).service(wanted)


def bootstrap(family=None, log=print):
    """Install every system package the apps need, before setup starts."""
    family = family or current()
    family.repair(log)
    log('Installing the packages the apps need: ' + ', '.join(APP_PACKAGES))
    family.install(APP_PACKAGES)


def main(argv):
    commands = {'bootstrap': bootstrap, 'repair': lambda: current().repair()}
    if len(argv) != 1 or argv[0] not in commands:
        print('usage: python3 host.py bootstrap|repair', file=sys.stderr)
        return 2
    try:
        commands[argv[0]]()
    except KeyboardInterrupt:
        print('\nStopped. Packages already installed stay installed (switched off). Run  sudo bash install.sh  again '
              'when you are ready.', file=sys.stderr)
        return 130
    except subprocess.TimeoutExpired as exc:
        print('ERROR: ' + str(exc.cmd[0]) + ' did not finish within ' + str(int(exc.timeout) // 60)
              + ' minutes; check the network and run  sudo bash install.sh  again', file=sys.stderr)
        return 1
    except (RuntimeError, OSError, http.client.HTTPException) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
