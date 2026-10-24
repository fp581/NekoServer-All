#!/usr/bin/env python3
"""Remove NekoServer from this PC: what you choose, after two confirmations.

    sudo bash uninstall.sh             choose what goes, see the plan, confirm twice
    sudo bash uninstall.sh --dry-run   see what each choice would remove; nothing changes

NekoServer itself (its services, programs and settings) always goes. Everything else is
a choice: the apps' containers, the apps' data on the SSD, your photos, your movies and
music, your Nextcloud files, the backups, the drive mounts. Nothing is ever formatted:
a drive you keep is left exactly as it is, and the packages NekoServer installed
(Docker, Samba, Caddy, Tailscale, Rust, OpenRGB, ...) are left for you to keep or remove.
"""
import grp
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = 'my-config.json'
TEMPLATE = 'config.json'
UNITS = '/etc/systemd/system'
SAMBA_SHARES = '/etc/samba/nekoserver.conf'
SAMBA_CONFIG = '/etc/samba/smb.conf'
OWN_SMB_CONF = '[global]\n   include = /etc/samba/nekoserver.conf\n'   # the smb.conf setup writes when there was none
MANAGED = '# Managed by NekoServer'
DAEMON_JSON = '/etc/docker/daemon.json'
FSTAB = '/etc/fstab'
GATEWAY_SETTINGS = '/etc/nekoserver-gateway'
CONTROL_GROUP = 'nekoserver-control'
COMPOSE_PREFIX = 'neko-'
DEFAULT_MOUNTS = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media', 'external_backup_mount': '/mnt/nekoserver-photos'}
# A drive's mount folder from the settings is used only when it is a plain absolute
# folder of its own; the root and the system's folders are never a drive of NekoServer's.
ROOT_HOME = '/root'
HOMES = '/home'
PROTECTED = ('/', '/bin', '/boot', '/dev', '/etc', HOMES, '/lib', '/lib64', '/opt', '/proc', ROOT_HOME, '/run', '/sbin',
             '/sys', '/usr', '/var', tempfile.gettempdir())
# NekoServer's own places outside the drives (removed with the server itself).
SYSTEM_PATHS = ('/etc/nekoserver-dashboard', '/etc/nekoserver-control', '/etc/nekoserver-backups', GATEWAY_SETTINGS,
                '/etc/nekoserver-os', '/etc/nekoserver-heartbeat', '/etc/nekoserver-notifications',
                '/etc/modules-load.d/nekoserver-i2c.conf', '/usr/local/bin/nekoserver-dashboard',
                '/usr/share/applications/nekoserver-dashboard.desktop', '/usr/local/share/nekoserver-gateway',
                '/usr/local/share/nekoserver-pages', '/var/lib/nekoserver-host', '/var/lib/nekoserver-openrgb',
                '/var/lib/nekoserver-notifications', '/var/lib/nekoserver-dashboard', '/var/lib/nekoserver-control')
SYSTEM_GLOBS = ('usr/local/lib/nekoserver-*', 'run/nekoserver-*', 'etc/systemd/system/*.d/10-nekoserver-storage.conf')
PACKAGES_LEFT = ('Docker, Samba, Caddy, Tailscale, Rust and gcc, smartmontools, qrencode, OpenRGB, and Steam if the TV step '
                 'added it, are packages of your Linux: they stay installed. Remove any you do not want with your package manager.')

# The choices. 'server' is always on; the rest is yours. (key, default on, title, personal data)
CHOICES = (('server', True, 'NekoServer itself: its services, programs and settings', False),
           ('apps', True, "The apps' containers and networks in Docker (the apps stop; their data is the next choice)", False),
           ('data', False, "The apps' data on the SSD: databases, settings, passwords, downloads, NekoServer's own files", False),
           ('photos', False, 'Your photos and videos (Immich: the library on the SSD and the archive on the HDD)', True),
           ('media', False, 'Your movies, shows and music (the library on the HDD)', True),
           ('cloud', False, 'Your Nextcloud files (on the HDD)', True),
           ('backups', False, 'The backups: on the HDD and on the USB drive', True),
           ('mounts', False, 'The drive mounts: the lines NekoServer wrote in /etc/fstab and the mount folders (the drives themselves stay as they are)', False))
PERSONAL = {'photos': 'photos', 'media': 'movies and music', 'cloud': 'Nextcloud files', 'backups': 'backups'}


def read_settings(root=ROOT):
    """my-config.json, or the template's defaults when the folder has none."""
    for name in (SETTINGS, TEMPLATE):
        try:
            value = json.loads((Path(root) / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def mount_folder(value, default):
    """The folder a drive is mounted at, from the settings: a plain absolute path of its
    own (/srv/nekoserver, /mnt/...). Anything else (the root, a system folder, '..', not
    a string) is refused and the default used, so that nothing in a settings file can
    point the removal anywhere but at NekoServer's drives."""
    if not isinstance(value, str) or not value.startswith('/') or value != os.path.normpath(value):
        return default
    if value in PROTECTED or any(value.startswith(p + '/') for p in PROTECTED if p != '/'):
        return default
    return value


def run(argv, timeout=300):
    """A command's output (stdout), '' when it fails or is missing."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


class Plan:
    """Everything a run would remove, by choice, found on this PC (a test's root stands
    in for /). Only what exists is listed, so the plan shown is the truth."""

    def __init__(self, settings, root='/', run=run, group_exists=None):
        self.root = Path(root)
        self.run = run
        self.group_exists = group_exists or self._group_exists
        self.warnings = []
        folders = {}
        for key, default in DEFAULT_MOUNTS.items():
            folders[key] = mount_folder(settings.get(key, default), default)
            if folders[key] != settings.get(key, default):
                self.warnings.append('The settings name ' + repr(settings.get(key)) + ' as ' + key + ', which cannot be a drive of '
                                     "NekoServer's; " + default + ' is used instead')
        self.data, self.media, self.external = folders['data_mount'], folders['media_mount'], folders['external_backup_mount']
        storage = settings.get('storage', {}) if isinstance(settings.get('storage'), dict) else {}
        self.uuids = {str(v.get('uuid')) for v in storage.values() if isinstance(v, dict) and v.get('uuid')}
        self.groups = {key: self._group(key) for key, *_ in CHOICES}

    def at(self, path):
        """The place of an absolute path under this plan's root (/ on a real PC, a temporary
        folder in a test), checked to stay inside that root."""
        base = os.path.normpath(str(self.root))
        full = os.path.normpath(os.path.join(base, str(path).lstrip('/')))
        if full != base and not full.startswith(base.rstrip('/') + '/'):
            raise RuntimeError('Not a path under ' + base + ': ' + str(path))
        return Path(full)

    @staticmethod
    def _group_exists(name):
        try:
            grp.getgrnam(name)
            return True
        except KeyError:
            return False

    # ---- what each choice covers
    def units(self):
        folder = self.at(UNITS)
        return sorted(p.name for p in folder.glob('nekoserver-*.*') if p.suffix in ('.service', '.timer') and p.is_file())

    def system_paths(self):
        found = [self.at(p) for p in SYSTEM_PATHS if self.at(p).exists()]
        for pattern in SYSTEM_GLOBS:
            found.extend(p for p in self.root.glob(pattern) if p.exists())
        found.extend(self.at(UNITS) / unit for unit in self.units())
        return sorted(set(found))

    def shortcuts(self):
        """Desktop entries NekoServer wrote for each person's account."""
        found = []
        homes = [self.at(ROOT_HOME)] + sorted(self.at(HOMES).glob('*')) if self.at(HOMES).is_dir() else [self.at(ROOT_HOME)]
        for home in homes:
            for folder in (home / '.local/share/applications', home / 'Desktop', self.xdg_desktop(home)):
                try:
                    if folder is not None and folder.is_dir():
                        found.extend(p for p in folder.glob('nekoserver-*.desktop') if self.ours(p))
                except OSError:   # not readable by this account (a dry run without sudo)
                    self.warnings.append('Could not look in ' + str(folder) + ' (run with sudo for the whole plan)')
        return sorted(set(found))

    @staticmethod
    def xdg_desktop(home):
        """The desktop folder named in ~/.config/user-dirs.dirs, when it names one."""
        try:
            for line in (home / '.config/user-dirs.dirs').read_text().splitlines():
                if line.startswith('XDG_DESKTOP_DIR='):
                    value = line.split('=', 1)[1].strip().strip('"')
                    return Path(value.replace('$HOME', str(home)))
        except OSError:
            pass
        return None

    @staticmethod
    def ours(path):
        try:
            return 'NekoServer' in path.read_text(errors='replace')
        except OSError:
            return False

    def samba(self):
        """(share file, smb.conf to remove or None, accounts) NekoServer made."""
        shares = self.at(SAMBA_SHARES)
        text = shares.read_text(errors='replace') if shares.is_file() else ''
        if not text.startswith(MANAGED):
            return None, None, []
        accounts = sorted({line.split('=', 1)[1].strip() for line in text.splitlines() if line.strip().startswith('valid users =')})
        main = self.at(SAMBA_CONFIG)
        own = main if main.is_file() and main.read_text(errors='replace') == OWN_SMB_CONF else None
        return shares, own, [a for a in accounts if a and a != 'root']

    def tailnet_served(self):
        return self.at(GATEWAY_SETTINGS).exists() and shutil.which('tailscale') is not None

    def docker_root(self):
        """Docker's data-root when NekoServer pointed it at the SSD."""
        try:
            value = json.loads(self.at(DAEMON_JSON).read_text()).get('data-root')
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value.startswith(self.data.rstrip('/') + '/') else None

    def compose_projects(self):
        """The apps' Docker projects (neko-jellyfin, neko-immich, ...) with containers."""
        if not shutil.which('docker'):
            return []
        out = self.run(['docker', 'ps', '-a', '--format', '{{.Label "com.docker.compose.project"}}'], 60)
        return sorted({line.strip() for line in out.splitlines() if line.strip().startswith(COMPOSE_PREFIX)})

    def docker_networks(self):
        if not shutil.which('docker'):
            return []
        out = self.run(['docker', 'network', 'ls', '--format', '{{.Name}}'], 60)
        return sorted({line.strip() for line in out.splitlines() if line.strip().startswith(COMPOSE_PREFIX)})

    def drive_paths(self, key):
        data, media, external = self.at(self.data), self.at(self.media), self.at(self.external)
        if key == 'data':
            keep = {'photos', 'library', 'lost+found'}   # other choices, and the filesystem's own folder
            return sorted(p for p in data.iterdir() if p.name not in keep) if data.is_dir() else []
        places = {'photos': (data / 'photos', media / 'photos'), 'media': (media / 'library', data / 'library'),
                  'cloud': (media / 'cloud',), 'backups': (media / 'backups', media / 'recovery-snapshots')}
        found = [p for p in places.get(key, ()) if p.exists()]
        if key == 'backups' and external.is_dir() and self.mounted(self.external):
            found.extend(sorted(p for p in external.iterdir() if p.name != 'lost+found'))
        return found

    def mounted(self, mountpoint):
        """Whether one of NekoServer's drives (a folder mount_folder accepted) is mounted."""
        if mountpoint not in (self.data, self.media, self.external):
            return False
        return os.path.ismount(str(self.at(mountpoint))) if self.root == Path('/') else self.at(mountpoint).is_dir()

    def fstab_lines(self):
        """The fstab lines that mount NekoServer's drives (by UUID from the settings, or by
        its mount folders)."""
        try:
            lines = self.at(FSTAB).read_text().splitlines()
        except OSError:
            return []
        folders = {self.data, self.media, self.external}
        return [line for line in lines if line.strip() and not line.lstrip().startswith('#') and len(line.split()) >= 2
                and (line.split()[1] in folders or any(line.split()[0] == 'UUID=' + u for u in self.uuids))]

    # ---- the choices, in words a person can check
    def _group(self, key):
        """{'paths': [...], 'notes': [...]} for one choice: what it removes."""
        if key == 'server':
            return self._server_group()
        if key == 'apps':
            return self._apps_group()
        if key == 'mounts':
            return self._mounts_group()
        return self._drive_group(key)

    def _server_group(self):
        notes = []
        units = self.units()
        if units:
            notes.append('Stop and remove ' + str(len(units)) + ' services and timers: ' + ', '.join(units))
        paths = self.system_paths() + self.shortcuts()
        shares, own, accounts = self.samba()
        if shares:
            paths.append(shares)
            notes.append('Samba: the shares NekoServer defined go' + (', with the account ' + ', '.join(accounts) if accounts else '')
                         + ('; smb.conf (only the include NekoServer wrote) goes too' if own else
                            '; your own smb.conf stays (take out its "include = ' + SAMBA_SHARES + '" line yourself)'))
            if own:
                paths.append(own)
        if self.tailnet_served():
            notes.append('Tailscale: the pages NekoServer served on your tailnet are switched off (tailscale serve reset); '
                         'this PC stays logged in to Tailscale')
        if self.group_exists(CONTROL_GROUP):
            notes.append('The ' + CONTROL_GROUP + ' group goes')
        if self.docker_root():
            notes.append('Docker keeps its data-root on the SSD (' + self.docker_root() + ') unless the SSD data or the mounts go')
        return {'paths': paths, 'notes': notes}

    def _apps_group(self):
        projects, networks = self.compose_projects(), self.docker_networks()
        notes = []
        if projects:
            notes.append('Remove the containers of ' + ', '.join(projects))
        if networks:
            notes.append('Remove the Docker networks ' + ', '.join(networks))
        if not projects and not networks:
            notes.append('No app containers found (Docker not running, nothing deployed, or no permission to ask it: run with sudo)')
        return {'paths': [], 'notes': notes}

    def _mounts_group(self):
        notes = []
        lines = self.fstab_lines()
        if lines:
            notes.append('Take out of /etc/fstab: ' + ' | '.join(lines))
        for folder in (self.data, self.media, self.external):
            if self.mounted(folder):
                notes.append('Unmount ' + folder + ' and remove the empty folder')
        if self.docker_root():
            notes.append("Docker's data-root setting (" + self.docker_root() + ') is taken out of ' + DAEMON_JSON + ': Docker goes back to its default place')
        return {'paths': [], 'notes': notes}

    def _drive_group(self, key):
        notes = []
        if key == 'backups' and not self.mounted(self.external):
            notes.append('The USB drive is not mounted at ' + self.external + ' right now (NekoServer mounts it for each weekly copy): '
                         'plug it in and mount it there first, or delete its folders yourself')
        if key == 'data' and self.docker_root():
            notes.append("Docker's data-root setting (" + self.docker_root() + ') is taken out of ' + DAEMON_JSON + ' and Docker is stopped first')
        return {'paths': self.drive_paths(key), 'notes': notes}

    def size(self, path):
        out = self.run(['du', '-sh', str(path)], 600)
        return out.split()[0] if out.split() else '?'

    def describe(self, chosen):
        """The plan, in full, for the person to read before confirming."""
        lines = ['!! ' + w for w in self.warnings]
        for key, _, title, _ in CHOICES:
            if key not in chosen:
                continue
            group = self.groups[key]
            lines.append('== ' + title)
            lines.extend('   - ' + note for note in group['notes'])
            lines.extend('   - remove ' + str(p) + ('  (' + self.size(p) + ')' if p.is_dir() and key != 'server' else '') for p in group['paths'])
            if not group['notes'] and not group['paths']:
                lines.append('   - nothing of this is on this PC')
        return '\n'.join(lines)

    def phrase(self, chosen):
        personal = [PERSONAL[k] for k, *_ in CHOICES if k in chosen and k in PERSONAL]
        return 'remove NekoServer' + (' and delete my ' + ', '.join(personal) if personal else '')


def choose(ask=input, out=print):
    """The checklist: numbers toggle, "all" takes everything, Enter goes on."""
    chosen = {key for key, on, _, _ in CHOICES if on}
    while True:
        out('')
        out('What should go? NekoServer itself always does. Type numbers to switch the others on or off.')
        for number, (key, _, title, _) in enumerate(CHOICES, 1):
            out(' [' + ('x' if key in chosen else ' ') + '] ' + str(number) + '. ' + title)
        answer = ask('Numbers to switch, "all" for everything, or Enter to go on: ').strip().lower()
        if answer == '':
            return chosen
        if answer == 'all':
            chosen = {key for key, *_ in CHOICES}
            continue
        for token in answer.replace(',', ' ').split():
            _toggle(chosen, token, out)


def _toggle(chosen, token, out):
    if not token.isdigit() or not 1 <= int(token) <= len(CHOICES):
        out(' Not a choice: ' + token)
        return
    key = CHOICES[int(token) - 1][0]
    if key == 'server':
        out(' NekoServer itself is always removed.')
    elif key in chosen:
        chosen.discard(key)
    else:
        chosen.add(key)


def confirm(plan, chosen, ask=input, out=print):
    """Two confirmations: yes to the plan as shown, then the phrase naming what goes."""
    out('')
    out(plan.describe(chosen))
    out('')
    if ask('Remove everything listed above? Type yes to go on, anything else to stop: ').strip().lower() != 'yes':
        return False
    phrase = plan.phrase(chosen)
    out('')
    out('Last check: nothing listed can be brought back afterwards.')
    return ask('Type exactly  ' + phrase + '  to go on: ').strip() == phrase


def system_file(root, path):
    """One of the system's own files (/etc/fstab, Docker's daemon.json) under root: / on a
    real PC, a test's folder otherwise. The path is a constant of this module."""
    return Path(root) / path.lstrip('/')


def remove(plan, chosen, run=run, out=print, rmtree=shutil.rmtree, root='/'):
    """Do it, in a safe order: stop everything, then the files, then the mounts."""
    units = plan.units()
    _stop_units(units, run, out)
    if 'apps' in chosen:
        _remove_containers(plan, run, out)
    if plan.docker_root() and ('data' in chosen or 'mounts' in chosen):
        _reset_docker_root(system_file(root, DAEMON_JSON), run, out)
    if plan.tailnet_served():
        out('Switching off what NekoServer served on your tailnet')
        run(['tailscale', 'serve', 'reset'], 60)
    shares, _, accounts = plan.samba()
    _remove_samba_accounts(plan, accounts, run, out)
    _remove_files(plan, chosen, out, rmtree)
    if units:
        run(['systemctl', 'daemon-reload'], 120)
        run(['systemctl', 'reset-failed'], 60)
    if shares and plan.root == Path('/') and shutil.which('smbcontrol'):
        run(['smbcontrol', 'all', 'reload-config'], 30)
    if plan.group_exists(CONTROL_GROUP):
        run(['groupdel', CONTROL_GROUP], 30)
    if 'mounts' in chosen:
        _remove_mounts(plan, system_file(root, FSTAB), run, out)
    if units:
        run(['systemctl', 'daemon-reload'], 120)


def _stop_units(units, run, out):
    if not units:
        return
    out("Stopping NekoServer's services and timers")
    timers = [u for u in units if u.endswith('.timer')]
    for unit in timers + [u for u in units if u not in timers]:
        run(['systemctl', 'disable', '--now', unit], 120)


def _remove_containers(plan, run, out):
    for project in plan.compose_projects():
        out('Removing the containers of ' + project)
        ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project], 60).split()
        if ids:
            run(['docker', 'rm', '-f', *ids], 300)
    for network in plan.docker_networks():
        run(['docker', 'network', 'rm', network], 60)


def _reset_docker_root(path, run, out):
    out('Stopping Docker and taking its data-root setting out of ' + DAEMON_JSON)
    run(['systemctl', 'stop', 'docker.service', 'docker.socket'], 300)
    content = json.loads(path.read_text())
    content.pop('data-root', None)
    path.write_text(json.dumps(content, indent=2) + '\n')


def _remove_samba_accounts(plan, accounts, run, out):
    if plan.root != Path('/'):
        return   # a test's root: no real Samba accounts
    for account in accounts:
        if run(['pdbedit', '-L', '-u', account], 30).strip():
            out('Removing the Samba account ' + account)
            run(['smbpasswd', '-x', account], 30)


def _remove_files(plan, chosen, out, rmtree):
    """The paths of every chosen group: each one the plan itself found, under NekoServer's
    own places or its drives (mount_folder), never anywhere the settings alone point."""
    for key, *_ in CHOICES:
        if key not in chosen:
            continue
        for path in plan.groups[key]['paths']:
            out('  removing ' + str(path))
            if path.is_dir() and not path.is_symlink():
                rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


def _remove_mounts(plan, path, run, out):
    lines = plan.fstab_lines()
    if lines:
        kept = [line for line in path.read_text().splitlines() if line not in lines]
        path.write_text('\n'.join(kept) + '\n')
        out('Took ' + str(len(lines)) + ' line(s) out of ' + FSTAB)
    for folder in (plan.external, plan.media, plan.data):
        if not plan.mounted(folder):
            continue
        out('Unmounting ' + folder)
        run(['umount', folder], 120)
        try:
            plan.at(folder).rmdir()
        except OSError:
            out('  (' + folder + ' is not empty, so the folder stays)')


def main(argv):
    dry = '--dry-run' in argv
    if not dry and os.geteuid() != 0:
        print('Run with sudo:  sudo bash uninstall.sh   (or  --dry-run  to only look)', file=sys.stderr)
        return 1
    plan = Plan(read_settings())
    if dry:
        print('Dry run: what each choice would remove. Nothing is changed.')
        if os.geteuid() != 0:
            print('(Not running as root: what this account may not look at is left out. For the whole plan:  sudo bash uninstall.sh --dry-run)')
        print(plan.describe({key for key, *_ in CHOICES}))
        print('')
        print(PACKAGES_LEFT)
        return 0
    print('This removes NekoServer from this PC. Nothing is formatted; what you keep stays as it is.')
    try:
        chosen = choose(input, print)
        if not confirm(plan, chosen, input, print):
            print('Stopped. Nothing was changed.')
            return 2
        remove(plan, chosen, out=print)
    except (KeyboardInterrupt, EOFError):
        print('\nStopped.')
        return 130
    print('')
    print('Done. ' + PACKAGES_LEFT)
    if 'mounts' not in chosen:
        print('The drives stay mounted as before; take their lines out of /etc/fstab when you no longer want that.')
    print('This folder (' + str(ROOT) + ') is yours to delete.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
