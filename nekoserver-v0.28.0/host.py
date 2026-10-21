"""The Linux family this server runs, and how to install packages and name services on it.

Shared code never calls a package manager itself: it asks this module. Each family
(Arch/Nyarch and Debian/Ubuntu) knows its package manager, the names its repositories
use for the packages NekoServer needs, what its services are called and which of its
settings files belong in host backups. See PORTING.md for how a family is added.

Run as  python3 host.py bootstrap  (install.sh does) to install the system packages
the apps need.
"""
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import MappingProxyType

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


def _apt_environment():
    return {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'}


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
        return self.run(argv, capture_output=True, text=True, timeout=timeout, check=False, **extra)

    def available(self, wanted):
        """The configured repositories offer this package."""
        raise NotImplementedError

    def installed(self, wanted):
        raise NotImplementedError

    def _install(self, names, timeout):
        raise NotImplementedError

    def prepare(self, missing):
        """Before installing: check for conflicts and add vendor repositories."""

    def settle(self, missing):
        """After installing: undo anything the packages did that NekoServer manages itself."""

    def install(self, wanted, timeout=900):
        """Install the packages that aren't installed yet; nothing else on the system is
        upgraded. Raises RuntimeError with the package manager's reason on failure."""
        if not self.ready():
            raise RuntimeError(self.label + ' package manager (' + self.manager + ') not found')
        missing = [name for name in wanted if not self.installed(name)]
        if not missing:
            return []
        self.prepare(missing)
        result = self._install([real for name in missing for real in self.names(name)], timeout)
        if result.returncode:
            raise RuntimeError('Installing ' + ', '.join(missing) + ' failed: ' + _reason(result))
        self.settle(missing)
        return missing


class Arch(Family):
    name = 'arch'
    label = 'Arch Linux / Nyarch'
    manager = 'pacman'
    ids = ('arch', 'nyarch')
    like = 'arch'
    services = MappingProxyType({'samba': 'smb.service', 'tailscale': 'tailscaled.service'})
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')

    def _query(self, flag, wanted, timeout):
        return self.ready() and all(self._call(['pacman', flag, name], timeout).returncode == 0
                                    for name in self.names(wanted))

    def available(self, wanted):
        return self._query('-Si', wanted, 60)

    def installed(self, wanted):
        return self._query('-Q', wanted, 30)

    def _install(self, names, timeout):
        return self._call(['pacman', '-S', '--needed', '--noconfirm', *names], timeout)


DOCKER_REPOSITORY = ('docker', 'https://download.docker.com/linux/{os}/gpg', '/etc/apt/keyrings/docker.asc',
                     'deb [arch=amd64 signed-by={key}] https://download.docker.com/linux/{os} {codename} stable\n')
# Debian's samba package writes an example smb.conf; NekoServer writes its own.
SAMBA_CONFIG = '/etc/samba/smb.conf'
TAILSCALE_REPOSITORY = ('tailscale', 'https://pkgs.tailscale.com/stable/{os}/{codename}.noarmor.gpg',
                        '/usr/share/keyrings/tailscale-archive-keyring.gpg',
                        'deb [signed-by={key}] https://pkgs.tailscale.com/stable/{os} {codename} main\n')


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
    # Packages from their makers' own repositories (Debian doesn't carry them).
    repositories = MappingProxyType({'docker': DOCKER_REPOSITORY, 'docker-compose': DOCKER_REPOSITORY,
                                     'tailscale': TAILSCALE_REPOSITORY})
    # Services these packages enable and start on their own. NekoServer starts them
    # itself once their storage, settings and guards are in place (Docker must never
    # start on the system disk), so they are left switched off after installing.
    autostarted = MappingProxyType({
        'docker': ('docker.service', 'docker.socket'), 'caddy': ('caddy.service',),
        'samba': ('smbd.service', 'nmbd.service', 'samba-ad-dc.service'), 'tailscale': ('tailscaled.service',)})
    # Debian's own container packages that would conflict with Docker's.
    conflicts = ('docker.io', 'docker-compose', 'docker-doc', 'podman-docker', 'containerd', 'runc')
    root = Path('/')      # where system files are written (a folder in tests)

    def __init__(self, run=subprocess.run, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch
        self.refreshed = False
        self.samba_config_existed = True
        if self.ubuntu():
            self.packages = MappingProxyType({**type(self).packages, 'firefox': 'firefox'})

    def path(self, absolute):
        return self.root / absolute.lstrip('/')

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
        result = self._call(['apt-get', 'update'], 600, env=_apt_environment())
        if result.returncode:
            raise RuntimeError('Refreshing the package lists failed: ' + _reason(result))
        self.refreshed = True

    def _dpkg_installed(self, name):
        result = self._call(['dpkg-query', '-W', '-f=${Status}', name], 30)
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

    def installed(self, wanted):
        return all(self._dpkg_installed(name) for name in self.names(wanted))

    def add_repository(self, entry):
        """Add a maker's repository unless a list for it already exists (someone else's
        setup is left alone)."""
        name, key_url, key_path, line = entry
        listing = self.path('/etc/apt/sources.list.d/' + name + '.list')
        if listing.exists():
            return False
        system, codename = self.vendor_release()
        if not codename:
            raise RuntimeError('Cannot tell this system\'s release codename for the ' + name + ' repository')
        key = self.fetch(key_url.format(os=system, codename=codename))
        if not key or len(key) >= 65536:
            raise RuntimeError('The ' + name + ' repository key could not be downloaded')
        key_file = self.path(key_path)
        key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        key_file.write_bytes(key)
        key_file.chmod(0o644)
        listing.write_text(line.format(key=key_path, os=system, codename=codename))
        listing.chmod(0o644)
        self.refreshed = False
        return True

    def prepare(self, missing):
        self.samba_config_existed = 'samba' not in missing or self.path(SAMBA_CONFIG).exists()
        if {'docker', 'docker-compose'} & set(missing):
            found = [name for name in self.conflicts if self._dpkg_installed(name)]
            if found:
                raise RuntimeError('This system has its own Docker packages installed (' + ', '.join(found)
                                   + '). NekoServer uses Docker\'s official packages and does not mix or '
                                   'replace them. Remove them yourself if nothing else uses them, then run '
                                   'setup again.')
        for entry in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(entry)
        self.refresh()

    def _install(self, names, timeout):
        # policy-rc.d tells the packages not to start their services while installing.
        policy = self.path('/usr/sbin/policy-rc.d')
        ours = not policy.exists()
        if ours:
            policy.write_text('#!/bin/sh\n# NekoServer: no service starts while packages install.\nexit 101\n')
            policy.chmod(0o755)
        try:
            return self._call(['apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', *names],
                              timeout, env=_apt_environment())
        finally:
            if ours:
                policy.unlink(missing_ok=True)

    def settle(self, missing):
        config = self.path(SAMBA_CONFIG)
        if not self.samba_config_existed and config.is_file():
            # The package's example, not anyone's settings: kept beside it, out of the way.
            config.replace(config.with_name('smb.conf.packaged'))
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        if units:
            self._call(['systemctl', 'disable', '--now', '--quiet', *units], 120)


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
