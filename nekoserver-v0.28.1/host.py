"""The Linux family this server runs, and how to install packages and name services on it.

Shared code never calls a package manager itself: it asks this module. Each family
(Arch/Nyarch and Debian/Ubuntu) knows its package manager, the names its repositories
use for the packages NekoServer needs, what its services are called and which of its
settings files belong in host backups. See PORTING.md for how a family is added.

Run as  python3 host.py bootstrap  (install.sh does) to install the system packages
the apps need.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import MappingProxyType

from neko_common import atomic_write

OS_RELEASE = '/etc/os-release'
# Every system package the apps need outside their containers, by NekoServer's names.
APP_PACKAGES = ('docker', 'docker-compose', 'tailscale', 'caddy', 'samba', 'hdparm',
                'smartmontools', 'qrencode', 'xdg-utils')


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


def _reason(result):
    """The last line a package manager printed, for error messages."""
    return ((result.stderr or result.stdout or '').strip().splitlines()[-1:] or ['no details'])[0]


def environment():
    """For package managers and systemctl: English messages (their output is read, and
    "Candidate:" is "Candidat :" in French) and no questions on the terminal."""
    return {**os.environ, 'LC_ALL': 'C', 'LANGUAGE': 'C', 'DEBIAN_FRONTEND': 'noninteractive'}


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
    like = ''             # os-release ID_LIKE word of derivatives
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
        return bool(set(_words(release, 'ID')) & set(cls.ids)) or cls.like in _words(release, 'ID_LIKE')

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
        """Before installing: check for conflicts and add vendor repositories. Returns
        what settle() must know about the system as it was before (kept in the journal)."""
        return {}

    def settle(self, packages, before):
        """After installing, also after a failed or interrupted one: undo anything the
        packages did that NekoServer manages itself."""

    def reconcile(self):
        """Finish an installation that stopped part-way, now or in an earlier run: the
        packages it did install are tidied up (settle) before anything else happens."""
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
        self.reconcile()
        missing = [name for name in wanted if not self.installed(name)]
        if not missing:
            return []
        before = self.prepare(missing)
        if self.journal is not None:
            atomic_write(self.path(self.journal), json.dumps(
                {'format': 'neko-package-install-v1', 'packages': missing, 'before': before}) + '\n')
        # Only the parts that are missing: a package already there is never reinstalled
        # or upgraded (that would restart a Docker someone already runs, for example).
        result = self._install([real for name in missing for real in self.names(name) if not self.has(real)], timeout)
        # Packages can be installed even when the whole command failed: tidy them first.
        self.reconcile()
        if result.returncode:
            raise RuntimeError('Installing ' + ', '.join(missing) + ' failed: ' + _reason(result))
        return missing


class Arch(Family):
    name = 'arch'
    label = 'Arch Linux / Nyarch'
    manager = 'pacman'
    ids = ('arch', 'nyarch')
    like = 'arch'
    services = MappingProxyType({'samba': 'smb.service', 'tailscale': 'tailscaled.service'})
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')

    def available(self, wanted):
        return self.ready() and all(self._call(['pacman', '-Si', name], 60).returncode == 0
                                    for name in self.names(wanted))

    def has(self, package):
        return self._call(['pacman', '-Q', package], 30).returncode == 0

    def _install(self, names, timeout):
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
OUR_POLICY = '#!/bin/sh\n# NekoServer: no service starts while packages install.\nexit 101\n'


def _stanzas(path, text):
    """The enabled source entries of a one-line .list file or a deb822 .sources file."""
    if path.suffix == '.sources':
        for stanza in text.split('\n\n'):
            fields = dict(line.split(':', 1) for line in stanza.splitlines()
                          if ':' in line and not line.startswith(('#', ' ')))
            if fields.get('Enabled', 'yes').strip().lower() != 'no':
                yield fields.get('URIs', '')
    else:
        yield from (line for line in text.splitlines() if line.split()[:1] in (['deb'], ['deb-src']))


def _md5(path):
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


class Debian(Family):
    name = 'debian'
    label = 'Debian / Ubuntu'
    manager = 'apt-get'
    ids = ('debian', 'ubuntu')
    like = 'debian'
    packages = MappingProxyType({
        # Docker's official packages, never Debian's own docker.io.
        'docker': ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'),
        'docker-compose': 'docker-compose-plugin',
        'intel-media-driver': 'intel-media-va-driver',
        'libva-mesa-driver': 'mesa-va-drivers',
        'libva-utils': 'vainfo',
        'firefox': 'firefox-esr',
    })
    services = MappingProxyType({'samba': 'smbd.service', 'tailscale': 'tailscaled.service'})
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
        'samba': ('smbd.service', 'nmbd.service', 'samba-ad-dc.service'), 'tailscale': ('tailscaled.service',)})
    # Debian's own container packages that would conflict with Docker's.
    conflicts = ('docker.io', 'docker-compose', 'docker-doc', 'podman-docker', 'containerd', 'runc')

    def __init__(self, run=subprocess.run, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch
        self.refreshed = False
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
        return 'debian', codename.lower()

    @classmethod
    def reboot_pending(cls, root='/'):
        return (Path(root) / 'run/reboot-required').exists()

    def refresh(self):
        """Refresh the package lists (never an upgrade), once."""
        if self.refreshed:
            return
        result = self._call(['apt-get', 'update'], 600)
        if result.returncode:
            raise RuntimeError('Refreshing the package lists failed: ' + _reason(result))
        self.refreshed = True

    def has(self, package):
        result = self._call(['dpkg-query', '-W', '-f=${Status}', package], 30)
        return result.returncode == 0 and result.stdout.strip().endswith('install ok installed')

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
        data = self.fetch(repository.key_url.format(os=system, codename=codename))
        if not data or len(data) >= 65536:
            raise RuntimeError('The ' + repository.name + ' repository key could not be downloaded')
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        atomic_write(key, data, 0o644)
        atomic_write(listing, desired.encode(), 0o644)
        self.refreshed = False
        return True

    def prepare(self, missing):
        self.refuse_conflicts(missing)
        for repository in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(repository)
        self.refresh()
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        existing = self.existing_units(units)
        self.check_policy([unit for unit in units if unit not in existing])
        return {'samba_config_existed': self.path(SAMBA_CONFIG).exists(), 'units_before': existing}

    def refuse_conflicts(self, missing):
        if not {'docker', 'docker-compose'} & set(missing):
            return
        found = [name for name in self.conflicts if self.has(name)]
        if found:
            raise RuntimeError('This system has its own Docker packages installed (' + ', '.join(found)
                               + '). NekoServer uses Docker\'s official packages and does not mix or '
                               'replace them. Remove them yourself if nothing else uses them, then run '
                               'setup again.')

    def existing_units(self, units):
        """The services that are already there before installing (a Docker someone runs,
        say): those are never stopped or switched off, only ones the packages newly bring."""
        states = self.unit_states(units) if units else {}
        return [unit for unit in units if states.get(unit, {}).get('LoadState') not in (None, 'not-found')]

    def check_policy(self, units):
        """A policy-rc.d that someone else put there must already refuse to start these
        services while packages install; NekoServer won't replace it."""
        policy = self.path(POLICY)
        if not policy.exists() or policy.read_text(errors='replace') == OUR_POLICY:
            return
        allowed = [unit for unit in units if self._call([str(policy), unit, 'start'], 30).returncode != 101]
        if allowed:
            raise RuntimeError(str(POLICY) + ' (someone else\'s) would let ' + ', '.join(allowed) + ' start while '
                               'installing, before NekoServer has its drive and settings ready. Move it aside '
                               'for the installation (sudo mv ' + POLICY + ' ' + POLICY + '.saved), run setup '
                               'again, then move it back.')

    def _install(self, names, timeout):
        policy = self.path(POLICY)
        ours = not policy.exists()
        if ours:
            # Tells the packages not to start their services while installing.
            atomic_write(policy, OUR_POLICY.encode(), 0o755)
        try:
            return self._call(['apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', *names], timeout)
        finally:
            if ours:
                policy.unlink(missing_ok=True)

    def reconcile(self):
        policy = self.path(POLICY)
        if policy.is_file() and policy.read_text(errors='replace') == OUR_POLICY:
            policy.unlink()       # left behind by an installation that was cut off
        super().reconcile()

    def settle(self, packages, before):
        self.set_aside_samba_example(packages, before)
        existing = before.get('units_before', [])
        self.switch_off([unit for name in packages for unit in self.autostarted.get(name, ()) if unit not in existing])

    def set_aside_samba_example(self, packages, before):
        """Samba's packaged example smb.conf, never anyone's own: it must not have existed
        before, and must still be what the package wrote (ucf's record of it)."""
        config = self.path(SAMBA_CONFIG)
        if 'samba' not in packages or before.get('samba_config_existed', True) or not config.is_file():
            return
        try:
            records = self.path('/var/lib/ucf/hashfile').read_text(errors='replace').splitlines()
        except OSError:
            records = []
        recorded = [line.split()[0] for line in records if line.split()[1:] == [SAMBA_CONFIG]]
        if recorded and recorded[-1] != _md5(config):
            return                # changed since it was installed: someone's settings
        config.replace(config.with_name('smb.conf.packaged'))

    def unit_states(self, units):
        result = self._call(['systemctl', 'show', *units, '--property=Id,LoadState,UnitFileState,ActiveState'], 60)
        if result.returncode:
            raise RuntimeError('systemd could not report on ' + ', '.join(units) + ': ' + _reason(result))
        states = {}
        for block in result.stdout.split('\n\n'):
            fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
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


FAMILIES = (Arch, Debian)


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
    log('Installing the packages the apps need: ' + ', '.join(APP_PACKAGES))
    family.install(APP_PACKAGES)


def main(argv):
    if argv != ['bootstrap']:
        print('usage: python3 host.py bootstrap', file=sys.stderr)
        return 2
    try:
        bootstrap()
    except (RuntimeError, OSError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
