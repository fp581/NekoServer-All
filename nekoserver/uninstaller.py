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

import errno
import fcntl
import grp
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import host
from neko_common import SETUP_LOCK, SHARED, answered_within

ROOT = Path(__file__).resolve().parent
SETTINGS = 'my-config.json'
TEMPLATE = 'config.json'
UNITS = '/etc/systemd/system'
SAMBA_SHARES = '/etc/samba/nekoserver.conf'
SAMBA_CONFIG = '/etc/samba/smb.conf'
SAMBA_CREATED = '/etc/samba/nekoserver-accounts'  # the Samba accounts setup created (modules/samba.py)
OWN_SMB_CONF = '[global]\n   include = /etc/samba/nekoserver.conf\n'  # the smb.conf setup writes when there was none
PACKAGED_SMB_CONF = (
    '/etc/samba/smb.conf.packaged'  # the package's example, set aside by setup; put back when NekoServer's own goes
)
SAMBA_BEFORE = (
    '/etc/samba/nekoserver-samba-before.json'  # was Samba on before NekoServer switched it on (modules/samba.py)
)
SAMBA_UNITS = ('smbd.service', 'smb.service')  # its name on Debian and Ubuntu, then on Arch and Fedora
MANAGED = '# Managed by NekoServer'
DAEMON_JSON = '/etc/docker/daemon.json'
DOCKER_GUARD = '/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf'  # modules/docker.py
FSTAB = '/etc/fstab'
# Where Linux itself is mounted: a line for one of these is never taken out of /etc/fstab.
SYSTEM_PLACES = frozenset(('/', '/boot', '/boot/efi', '/efi', '/home', '/usr', '/var', '/tmp', 'none', 'swap'))
GATEWAY_SETTINGS = '/etc/nekoserver-gateway'
TAILNET_LEDGER = 'gateway/tailnet-owned.json'  # on the SSD: the Tailscale routes setup made (gateway_tools.py)
# The server-OS step (modules/server_os.py): no sleep, log limits, and the sleep targets masked.
OS_DROPINS = (
    '/etc/systemd/sleep.conf.d/90-nekoserver.conf',
    '/etc/systemd/logind.conf.d/90-nekoserver.conf',
    '/etc/systemd/journald.conf.d/90-nekoserver.conf',
)
OS_MARKER = '# Managed by NekoServer server_os'
SLEEP_TARGETS = (
    'sleep.target',
    'suspend.target',
    'hibernate.target',
    'hybrid-sleep.target',
    'suspend-then-hibernate.target',
)
MASKS_BEFORE = '/etc/nekoserver-os/sleep-masks-before.json'
CONTROL_GROUP = 'nekoserver-control'
COMPOSE_PREFIX = 'neko-'
DEFAULT_MOUNTS = {
    'data_mount': '/srv/nekoserver',
    'media_mount': '/mnt/nekoserver-media',
    'external_backup_mount': '/mnt/nekoserver-photos',
}
# A drive's mount folder from the settings is used only when it is a plain absolute
# folder of its own; the root and the system's folders are never a drive of NekoServer's.
ROOT_HOME = '/root'
HOMES = '/home'
PROTECTED = (
    '/',
    '/bin',
    '/boot',
    '/dev',
    '/etc',
    HOMES,
    '/lib',
    '/lib64',
    '/opt',
    '/proc',
    ROOT_HOME,
    '/run',
    '/sbin',
    '/sys',
    '/usr',
    '/var',
    tempfile.gettempdir(),
)
# NekoServer's own places outside the drives (removed with the server itself).
SYSTEM_PATHS = (
    '/etc/nekoserver-dashboard',
    '/etc/nekoserver-control',
    '/etc/nekoserver-backups',
    GATEWAY_SETTINGS,
    '/etc/nekoserver-os',
    '/etc/nekoserver-heartbeat',
    '/etc/nekoserver-notifications',
    '/etc/modules-load.d/nekoserver-i2c.conf',
    '/usr/local/bin/nekoserver-dashboard',
    '/usr/share/applications/nekoserver-dashboard.desktop',
    '/usr/local/share/nekoserver-gateway',
    '/usr/local/share/nekoserver-pages',
    '/var/lib/nekoserver-host',
    '/var/lib/nekoserver-openrgb',
    '/var/lib/nekoserver-notifications',
    '/var/lib/nekoserver-dashboard',
    '/var/lib/nekoserver-control',
)
# systemd's record of when each NekoServer timer last ran goes too: left behind, a setup run
# on this PC later counts every night since as a missed backup (the timers are persistent),
# and runs the nightly snapshot at once, stopping the apps just as setup finishes.
SYSTEM_GLOBS = (
    'usr/local/lib/nekoserver-*',
    'etc/systemd/system/*.d/10-nekoserver-storage.conf',
    'var/lib/systemd/timers/stamp-nekoserver-*.timer',
    # the copy the gateway step keeps of an earlier install's unit before rewriting it
    'etc/systemd/system/nekoserver-*.before-*',
    # the longer waits for disks on a network storage box (network_storage.py)
    'etc/udev/rules.d/90-nekoserver-*.rules',
)
# Setup's own record of its steps and its log, kept on the system disk while the SSD is not
# mounted (installer.py DEFAULT_STATE, for setup run with sudo): moved to the SSD later,
# with the record left beside it as state.json.moved-to-ssd. NekoServer's, so it goes.
SETUP_STATE = '/root/.local/state/nekoserver'
# /etc/fstab as it was before NekoServer first changed it (modules/storage.py). It stays:
# it is your own file, and the only copy of the fstab you had before.
FSTAB_BEFORE = '/etc/fstab.nekoserver.bak'
PACKAGES_LEFT = (
    'Docker, Samba, Caddy, Tailscale, Rust and gcc, smartmontools, qrencode, OpenRGB, and Steam if the TV step '
    'added it, are packages of your Linux: they stay installed, with the package repositories NekoServer added for '
    "them (Docker's and Tailscale's own, on Debian and Ubuntu). Remove any you do not want with your package manager."
)

# The choices. 'server' is always on; the rest is yours. (key, default on, title, personal data)
CHOICES = (
    ('server', True, 'NekoServer itself: its services, programs and settings', False),
    ('apps', True, "The apps' containers and networks in Docker (the apps stop; their data is the next choice)", False),
    (
        'data',
        False,
        "The apps' data on the SSD: databases, settings, passwords, downloads, NekoServer's own files",
        False,
    ),
    ('photos', False, 'Your photos and videos (Immich: the library on the SSD and the archive on the HDD)', True),
    ('media', False, 'Your movies, shows and music (the library on the HDD)', True),
    ('cloud', False, 'Your Nextcloud files (on the HDD)', True),
    ('backups', False, 'The backups: on the HDD and on the USB drive', True),
    (
        'mounts',
        False,
        'The drive mounts: the lines NekoServer wrote in /etc/fstab and the mount folders (the drives themselves stay as they are)',
        False,
    ),
)
PERSONAL = {'photos': 'photos', 'media': 'movies and music', 'cloud': 'Nextcloud files', 'backups': 'backups'}
# What NekoServer itself keeps at the top of each drive, by choice. Only these are ever
# removed: anything else on a drive (a folder of your own) is listed as staying.
SSD_DATA = (
    'appdata',
    'compose',
    'config',
    'dashboard-state',
    'docker-engine',
    'gateway',
    'installer',
    'reports',
    'scripts',
    'secrets',
    'vm',
)


def top_folder(what):
    """The folder at the top of a drive that holds `what` ("photos", "music", "cloud"), from
    the table setup and the runtime share (shared.json "locations"): the first part of
    that group's first folder. Each can be on the SSD or the HDD since 0.46.0, so both
    drives are looked at for it (the review of 0.47.9: Nextcloud's files on the SSD stayed)."""
    return SHARED['locations'][what]['folders'][0].split('/')[0]


DRIVE_PLACES = {
    'photos': (('data', top_folder('photos')), ('media', top_folder('photos'))),
    'media': (('media', top_folder('music')), ('data', top_folder('music'))),
    'cloud': (('media', top_folder('cloud')), ('data', top_folder('cloud'))),
    'backups': (('media', 'backups'), ('media', 'recovery-snapshots'), ('data', 'backups')),
}

# ---- a group's own drive (0.50.0, gate run C18) ------------------------------------------
# "locations" and "install" in the settings place a group's files, or its apps' settings
# and databases, on a drive of its own under "storage", mounted at EXTRA_MOUNTS/<name>.
EXTRA = 'drive:'  # the plan's name for such a drive: drive:<name>
GROUP_DRIVE_OWN = 'nekoserver'  # <drive>/nekoserver/appdata/<app> (modules/storage.installed_binds)
INSTALL = 'install'  # in a drive's "holds": a group's apps are installed there
# The kinds of files (shared.json "locations") each choice covers.
CHOICE_HOLDS = {'photos': ('photos',), 'media': ('music', 'movies'), 'cloud': ('cloud',)}
GROUP_NAMES = {'photos': 'Photos', 'movies': 'Movies and shows', 'music': 'Music', 'cloud': 'Nextcloud'}
DRIVE_NAME = re.compile(r'[a-z][a-z0-9]{0,15}')  # neko_common.DRIVE_NAME
NOT_GROUP_DRIVES = ('data_ssd', 'media_hdd', 'ssd', 'media', 'one')  # the two, and the one drive of both


def _section(settings, name):
    value = settings.get(name)
    return value if isinstance(value, dict) else {}


def group_drives(settings):
    """{name: (its record under "storage", what is placed on it)} for each drive of its own
    a group uses: what is placed on it is a list of kinds of files (photos, movies, music,
    cloud) and INSTALL when apps keep their settings there. A drive named by nothing, a
    folder of the system disk or one a NAS shares (NFS) is none."""
    storage = _section(settings, 'storage')
    placed = {}
    named = [(drive, what) for what, drive in _section(settings, 'locations').items()]
    named += [(drive, INSTALL) for drive in _section(settings, INSTALL).values()]
    for drive, what in named:
        if isinstance(drive, str):
            placed.setdefault(drive, set()).add(what)
    found = {}
    for name, holds in placed.items():
        record = storage.get(name)
        usable = (
            isinstance(record, dict)
            and DRIVE_NAME.fullmatch(name)
            and name not in NOT_GROUP_DRIVES
            and isinstance(record.get('uuid'), str)
            and record['uuid']
            and record.get('folder') is None
            and not _section(record, 'network').get('export')
        )
        if usable:
            found[name] = (record, sorted(holds))
    return found


WHAT_WORDS = {'movies': 'your movies', 'tv': 'your shows', 'music': 'your music'}


def held_words(holds):
    """What is on a group's own drive, in words: "the settings of the apps installed there,
    Music's files"."""
    words = ["the settings and databases of the apps installed there"] if INSTALL in holds else []
    words += [GROUP_NAMES.get(what, what) + "'s files" for what in holds if what != INSTALL]
    return ', '.join(words)


def own_folders(settings):
    """The person's own folders setup uses where they are (existing media, "extra" in a
    row of the placement table) and the shared folders they are on: [{'path', 'what',
    'share', 'mount'}]. Never removed, whatever is chosen; read here only to say so and
    to keep them (nothing is ever deleted because the settings name it)."""
    return [
        own_folder(entry)
        for row in _section(settings, 'placement').values()
        for entry in listed_extra(row)
        if isinstance(entry, dict) and isinstance(entry.get('path'), str) and entry['path'].startswith('/')
    ]


def listed_extra(row):
    """The "extra" folders a row of the placement table lists; none when it lists none."""
    listed = row.get('extra') if isinstance(row, dict) else None
    return listed if isinstance(listed, list) else []


def own_folder(entry):
    """One of the person's folders, as the uninstaller says it."""
    text = lambda key: entry.get(key) if isinstance(entry.get(key), str) else ''  # noqa: E731
    return {
        'path': entry['path'],
        'what': str(entry.get('holds') or 'media'),
        'share': text('share'),
        'mount': text('mount'),
    }


def ours(drive, name, known):
    """A name at the top of a drive that is NekoServer's own (or the filesystem's)."""
    return name in known or name in FILESYSTEM_OWN or (drive == 'external' and name.startswith('.neko-'))


# The weekly USB copy: the photo archive's two sections, Nextcloud's files, the Immich
# database copies, copies set aside as damaged, the encrypted app password lists
# (backup.rs USB_PASSWORDS), and its ledger files (.neko-...).
USB_OWN = ('upload', 'library', 'nextcloud', 'immich-database', 'neko-damaged', 'passwords')
FILESYSTEM_OWN = ('lost+found',)
SIZE_UNKNOWN = 'size unknown without sudo'


# What setup wrote about the drives outside this folder: the mount folders and the drives'
# UUIDs (native_runtime.storage_config: data_mount, media_mount, storage). Used when this
# folder has no my-config.json (the uninstaller run from a fresh download, say).
DRIVE_RECORDS = ('/etc/nekoserver-os/drives.json', '/etc/nekoserver-backups/storage.json')


def _json_object(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def drive_record(path):
    """The drives part of one of setup's records, or None when it is missing, unreadable or
    not one (a mount folder that is not text, no drive with a UUID)."""
    value = _json_object(path)
    if not value or not all(isinstance(value.get(key), str) for key in ('data_mount', 'media_mount')):
        return None
    storage = value.get('storage')
    if not isinstance(storage, dict) or not any(
        isinstance(v, dict) and isinstance(v.get('uuid'), str) and v['uuid'] for v in storage.values()
    ):
        return None
    return {key: value[key] for key in ('data_mount', 'media_mount', 'storage')}


def newest_drive_record(root='/'):
    """(record, its path) of the one setup wrote last, or (None, None)."""
    found = []
    for name in DRIVE_RECORDS:
        path = system_file(root, name)
        record = drive_record(path)
        if record is not None:
            found.append((path.stat().st_mtime, name, record))
    if not found:
        return None, None
    _, name, record = max(found, key=lambda item: item[0])
    return record, name


def find_settings(root=ROOT, system='/'):
    """(settings, where they came from, in words). my-config.json in this folder first. Without
    it, the drives setup recorded in /etc (newest of DRIVE_RECORDS) on top of the template, so
    that a fresh download still finds the real mount folders and UUIDs."""
    mine = _json_object(Path(root) / SETTINGS)
    if mine is not None:
        return mine, str(Path(root) / SETTINGS)
    template = _json_object(Path(root) / TEMPLATE) or {}
    record, name = newest_drive_record(system)
    if record is not None:
        return {**template, **record}, (
            name
            + " (the drives setup recorded: this folder has no "
            + SETTINGS
            + "; the USB backup drive is not in that record)"
        )
    if template:
        return template, (
            'the template '
            + str(Path(root) / TEMPLATE)
            + ' (no '
            + SETTINGS
            + ' here and no record of the drives in '
            + ' or '.join(DRIVE_RECORDS)
            + '), so nothing on the drives is removed'
        )
    return {}, 'no settings found, so nothing on the drives is removed'


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
    """A command's output (stdout), '' when it fails or is missing. For looking only."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def call(argv, timeout=300):
    """(exit code, what it said) of a command that changes something; the code is None
    when it could not run or did not finish in time."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, 'did not finish within ' + str(timeout) + ' seconds'
    except OSError as exc:
        return None, str(exc)
    return done.returncode, (done.stderr or done.stdout or '').strip()


class Stopped(RuntimeError):
    """Something could not be stopped: the uninstall ends before anything is deleted."""


CONTROL_UNIT = 'nekoserver-control.service'
# Fans the control service took over and has not handed back to the motherboard yet;
# the service removes each record as it hands the fan back (modules/dashboard.py).
FAN_RECORDS = ('/run/nekoserver-control/originals.json', '/run/nekoserver-control/originals-detect.json')
CONTROL_STOP_SECONDS = 660  # it lets an app restart finish, then hands the fans back (dashboard.CONTROL_WAIT)


def unchanged_routes(owned, current):
    """The ports of `owned` (NekoServer's ledger) whose Serve entries are still as
    recorded in `current` (tailscale serve status --json)."""
    tcp, web = current.get('TCP') or {}, current.get('Web') or {}
    mine_tcp, mine_web = owned.get('TCP') or {}, owned.get('Web') or {}
    ports = []
    for port, value in sorted(mine_tcp.items()):
        # By port and handler, whatever host name they were set under (a server renamed
        # since keeps its routes under its old name): the same rule as gateway_tools.ours.
        recorded = [v for key, v in mine_web.items() if key.rpartition(':')[2] == port]
        found = [v for key, v in web.items() if key.rpartition(':')[2] == port]
        if tcp.get(port) == value and recorded and found and all(h == recorded[0] for h in found):
            ports.append(port)
    return ports


NOT_MOUNTED = 'not mounted'


def mismatch(found, record):
    """How the drive mounted at a folder (neko_common.mounted_identity) differs from the
    one its settings record names, in words."""
    if found is None:
        return NOT_MOUNTED
    uuid, fstype, tree = found
    if uuid != record.get('uuid'):
        return other_drive('UUID', uuid, record.get('uuid'))
    if fstype != record.get('fstype'):
        return other_drive('filesystem', fstype, record.get('fstype'))
    wanted = str(record.get('subvolume') or '').rstrip('/')
    return (
        'the right drive, but another part of it is mounted there ('
        + instead(tree or 'its top level', wanted or 'its top level')
        + ')'
    )


def instead(found, wanted):
    """'<found>, not <wanted>': what is there, and what the settings name."""
    return str(found) + ', not ' + str(wanted)


def other_drive(what, found, wanted):
    """Another drive than the settings name, said with what differs."""
    return 'not the one the settings name (' + what + ' ' + instead(found, wanted) + ')'


def without_locks(path):
    """[path], or for a folder with a lock file in it, everything else in it."""
    inside = sorted(path.iterdir()) if path.is_dir() and not path.is_symlink() else []
    if not any(entry.name.endswith(LOCK_SUFFIX) for entry in inside):
        return [path]
    return [entry for entry in inside if not entry.name.endswith(LOCK_SUFFIX)]


def no_containers_why(which=shutil.which, euid=os.geteuid):
    """Why no app container was listed, in words: only a guess can be given, but "run
    with sudo" is said only when that could help (Docker installed, not run as root)."""
    if not which('docker'):
        return 'Docker is not installed'
    if euid() != 0:
        return 'Docker is not running, nothing was deployed, or this account may not ask it: run with sudo'
    return 'Docker is not running, or nothing was deployed'


class Plan:
    """Everything a run would remove, by choice, found on this PC (a test's root stands
    in for /). Only what exists is listed, so the plan shown is the truth."""

    def __init__(self, settings, root='/', run=run, group_exists=None, identity=None):
        self.root = Path(root)
        self.run = run
        self.identity = identity  # reads (UUID, filesystem, subvolume) at a folder: neko_common.mounted_identity
        self.group_exists = group_exists or self._group_exists
        self.warnings = []
        folders = {}
        for key, default in DEFAULT_MOUNTS.items():
            folders[key] = mount_folder(settings.get(key, default), default)
            if folders[key] != settings.get(key, default):
                self.warnings.append(
                    'The settings name ' + repr(settings.get(key)) + ' as ' + key + ', which cannot be a drive of '
                    "NekoServer's; " + default + ' is used instead'
                )
        self.data, self.media, self.external = (
            folders['data_mount'],
            folders['media_mount'],
            folders['external_backup_mount'],
        )
        storage = settings.get('storage', {}) if isinstance(settings.get('storage'), dict) else {}
        # A role kept on the disk Linux runs from (0.50.0, "folder" in its record) carries
        # that disk's own UUID: never a drive of NekoServer's to look for in /etc/fstab, where
        # that UUID is the system's own line. Its line is found by its mount folder.
        self.uuids = {
            str(v.get('uuid'))
            for v in storage.values()
            if isinstance(v, dict)
            and v.get('uuid')
            and (not v.get('folder') or str(v.get('folder')).startswith(ONE_DRIVE + '/'))
        }
        photos = settings.get('photos', {}) if isinstance(settings.get('photos'), dict) else {}
        expected = {
            'data': storage.get('data_ssd') if isinstance(storage.get('data_ssd'), dict) else {},
            'media': storage.get('media_hdd') if isinstance(storage.get('media_hdd'), dict) else {},
            'external': {'uuid': photos.get('external_backup_uuid')},
        }
        # A group's own drive (0.50.0, gate run C18): mounted at /mnt/nekoserver-drives/<name>
        # and checked like the SSD and the HDD before anything on it goes.
        self.placed = group_drives(settings)
        expected.update({EXTRA + name: record for name, (record, _holds) in self.placed.items()})
        self.records = expected
        self.drives = tuple(expected)
        self.identified = {drive: self._identify(drive) for drive in expected}
        self.own_folders = own_folders(settings)  # the person's own folders and shares: never removed
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
        # Sockets too (the OpenRGB proxy's, 0.40.1): left on, one would start its service again.
        return sorted(p.name for p in folder.glob('nekoserver-*.*') if p.suffix in UNIT_KINDS and p.is_file())

    def system_paths(self):
        found = [self.at(p) for p in SYSTEM_PATHS if self.at(p).exists()]
        # The setup locks stay: the uninstall holds them until it ends, and a lock file
        # deleted while held would let a setup started meanwhile take a new one. They are
        # in /run, which is emptied at every start.
        for pattern in SYSTEM_GLOBS:
            found.extend(p for p in self.root.glob(pattern) if p.exists())
        found.extend(self.run_paths())
        found.extend(self.at(UNITS) / unit for unit in self.units())
        return sorted(set(found))

    def run_paths(self):
        """NekoServer's places in /run, without a single lock file. A lock is held by a setup
        (the uninstall holds those itself until it ends) or by a job: deleted while held, a
        second program would take a new lock under the same name instead of waiting, so
        the exclusion would be gone. /run is emptied at every start, so they go then."""
        found = []
        for path in sorted(self.root.glob('run/nekoserver-*')):
            if path.name.endswith(LOCK_SUFFIX) or not (path.exists() or path.is_symlink()):
                continue
            try:
                found.extend(without_locks(path))
            except OSError:  # setup's folder in /run is root's own (a dry run without sudo, gate run B8)
                self.could_not_look(path)
        return found

    def shortcuts(self):
        """Desktop entries NekoServer wrote for each person's account."""
        found = []
        homes = (
            [self.at(ROOT_HOME)] + sorted(self.at(HOMES).glob('*')) if self.at(HOMES).is_dir() else [self.at(ROOT_HOME)]
        )
        for home in homes:
            for folder in (home / '.local/share/applications', home / 'Desktop', self.xdg_desktop(home)):
                try:
                    if folder is not None and self.folder_here(folder):
                        found.extend(p for p in folder.glob('nekoserver-*.desktop') if self.ours(p))
                except OSError:  # not readable by this account (a dry run without sudo)
                    self.could_not_look(folder)
        return sorted(set(found))

    def could_not_look(self, folder):
        """Said once in the plan: a folder this account may not read (a dry run without sudo)."""
        said = (
            'Could not look in ' + str(folder) + ' without sudo, so what is in it is not listed '
            '(run with sudo for the whole plan)'
        )
        if said not in self.warnings:
            self.warnings.append(said)

    def setup_state(self):
        """Setup's records on the system disk (SETUP_STATE), when there are any."""
        folder = self.at(SETUP_STATE)
        try:
            return [folder] if self.folder_here(folder) and not folder.is_symlink() else []
        except OSError:  # not readable by this account (a dry run without sudo)
            self.could_not_look(folder)
            return []

    def kept(self):
        """What NekoServer's setup left on the system disk that stays, and why, in words."""
        try:
            there = self.at(FSTAB_BEFORE).is_file()
        except OSError:
            there = False
        if not there:
            return []
        return [
            'Kept: '
            + FSTAB_BEFORE
            + ', your /etc/fstab as it was before NekoServer first changed it. It stays, as your '
            'own copy of how this PC mounted its drives before; delete it yourself once you no longer need it'
        ]

    @staticmethod
    def folder_here(folder):
        """True for a folder, False when there is none; a folder this account may not look into raises.

        Path.is_dir() cannot be used: since Python 3.14 it answers False for "permission denied"
        too, and the plan would then quietly miss what it could not see."""
        try:
            return stat.S_ISDIR(folder.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False

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
        """(share file, smb.conf to remove or None, accounts NekoServer created). An account
        the shares used that was there before (setup reuses it) is not NekoServer's: it stays,
        with its password, for whatever else uses it."""
        shares = self.at(SAMBA_SHARES)
        text = shares.read_text(errors='replace') if shares.is_file() else ''
        if not text.startswith(MANAGED):
            return None, None, []
        main = self.at(SAMBA_CONFIG)
        own = main if main.is_file() and main.read_text(errors='replace') == OWN_SMB_CONF else None
        return shares, own, [a for a in self.created_accounts() if a in self.share_accounts(text) and a != 'root']

    @staticmethod
    def share_accounts(text):
        return {line.split('=', 1)[1].strip() for line in text.splitlines() if line.strip().startswith('valid users =')}

    def created_accounts(self):
        try:
            lines = self.at(SAMBA_CREATED).read_text().splitlines()
        except OSError:
            return []
        return sorted(line.strip() for line in lines if line.strip() and not line.startswith('#'))

    def samba_service(self, own):
        """(the Samba service, True when it is to be switched off, the words for the plan), or
        (None, False, '') when Samba is not installed or already off. Off only when NekoServer
        switched it on: its record says it was off before, or (no record, from a release
        before it was kept) smb.conf is NekoServer's own. Your own smb.conf means your Samba."""
        record = _json_object(self.at(SAMBA_BEFORE))
        if record is not None and (
            record.get('format') != 'neko-samba-before-v1'
            or record.get('unit') not in SAMBA_UNITS
            or not isinstance(record.get('enabled'), bool)
        ):
            record = None  # not one this release can read: as if there were none
        units = [record['unit']] if record else list(SAMBA_UNITS)
        unit = next((u for u in units if self.unit_on(u)), None)
        if unit is None:
            return None, False, ''
        if record:
            on_before = record['enabled'] or record.get('active') is True
            why = 'it was on before NekoServer' if on_before else 'NekoServer switched it on (it was off before)'
            return unit, not on_before, why
        if own:
            return unit, True, 'NekoServer switched it on (smb.conf is only its own include)'
        return unit, False, 'your own smb.conf uses it'

    def unit_on(self, unit):
        """Installed, and switched on at startup or running now."""
        shown = self.run(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'UnitFileState', '-p', 'ActiveState'], 30)
        state = {key: value for key, sep, value in (line.partition('=') for line in shown.splitlines()) if sep}
        return state.get('LoadState') == 'loaded' and (
            state.get('UnitFileState') in ('enabled', 'enabled-runtime')
            or state.get('ActiveState') in ('active', 'activating', 'reloading')
        )

    def reused_accounts(self):
        """Accounts the shares used that NekoServer did not create (they stay)."""
        shares = self.at(SAMBA_SHARES)
        text = shares.read_text(errors='replace') if shares.is_file() else ''
        return (
            sorted(self.share_accounts(text) - set(self.created_accounts()) - {'root', ''})
            if text.startswith(MANAGED)
            else []
        )

    def os_dropins(self):
        """NekoServer's own no-sleep, logind and journald settings (its marker on the first line)."""
        found = []
        for name in OS_DROPINS:
            try:
                if self.at(name).read_text().startswith(OS_MARKER):
                    found.append(self.at(name))
            except OSError:
                continue
        return found

    def sleep_masks(self):
        """The sleep targets to unmask: masked now, and not masked before NekoServer (its record).
        With no record (an install from before it was kept) and NekoServer's drop-ins present,
        the masks are NekoServer's."""
        try:
            record = json.loads(self.at(MASKS_BEFORE).read_text())
            before = set(record.get('masked_before', [])) if isinstance(record, dict) else set(SLEEP_TARGETS)
        except OSError:
            before = set() if self.os_dropins() else set(SLEEP_TARGETS)
        except ValueError:
            before = set(SLEEP_TARGETS)  # an unreadable record: unmask nothing
        units = self.at(UNITS)
        return [
            name
            for name in SLEEP_TARGETS
            if name not in before and (units / name).is_symlink() and os.readlink(units / name) == '/dev/null'
        ]

    def tailnet_routes(self):
        """The Tailscale Serve ports NekoServer set up (its ownership ledger on the SSD) that
        are still exactly as it set them: only these are switched off. Routes of your own,
        or one changed since, stay; with no ledger (home-network access only) nothing does."""
        try:
            ledger = json.loads(self.at(self.data.rstrip('/') + '/' + TAILNET_LEDGER).read_text())
        except (OSError, ValueError):
            return []
        owned = (
            ledger.get('owned')
            if isinstance(ledger, dict) and ledger.get('format') == 'neko-tailnet-owned-v1'
            else None
        )
        if not isinstance(owned, dict) or not shutil.which('tailscale'):
            return []
        try:
            current = json.loads(self.run(['tailscale', 'serve', 'status', '--json'], 30) or '{}')
        except ValueError:
            return []
        return unchanged_routes(owned, current if isinstance(current, dict) else {})

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

    def mount_of(self, drive):
        """Where a drive is mounted, as the settings name it: the SSD, the HDD, the USB
        drive, or a group's own drive (/mnt/nekoserver-drives/<name>)."""
        if drive.startswith(EXTRA):
            return EXTRA_MOUNTS + '/' + drive[len(EXTRA) :]
        return {'data': self.data, 'media': self.media, 'external': self.external}[drive]

    def folder_of(self, drive):
        return self.at(self.mount_of(drive))

    def _identify(self, drive):
        """The drive mounted at this folder is the one the settings name: only then is
        anything on it planned for removal (and it is checked again right before)."""
        folder = self.mount_of(drive)
        if not self.at(folder).is_dir():
            return False
        uuid = self.records[drive].get('uuid')
        if not isinstance(uuid, str) or not uuid:
            self.warnings.append('The settings name no drive for ' + folder + ', so nothing on it is removed')
            return False
        problem = self.identity_problem(drive)
        if problem:
            self.warnings.append('The drive at ' + folder + ' is ' + problem + ', so nothing on it is removed')
        return problem is None

    def identity_problem(self, drive):
        """None when the drive mounted at its folder now is the one the settings name, else
        how it differs, in words. The SSD and the HDD as setup checks them (UUID, filesystem
        and Btrfs subvolume, as neko_common.identity_matches: the same Btrfs drive mounted
        with another subvolume, or its top level, holds another tree); the USB drive by its
        UUID, all its settings record."""
        folder, record = self.folder_of(drive), self.records[drive]
        if drive != 'external':
            import neko_common

            found = (self.identity or neko_common.mounted_identity)(folder)
            wanted = (record.get('uuid'), record.get('fstype'), str(record.get('subvolume') or '').rstrip('/'))
            return None if found is not None and tuple(found) == wanted else mismatch(found, record)
        uuid = str(record.get('uuid'))
        lines = [
            line.strip()
            for line in self.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(folder)], 20).splitlines()
            if line.strip()
        ]
        found = lines[-1] if lines else ''  # stacked mounts: the last is the one in use
        if found.lower() == uuid.lower():
            return None
        return NOT_MOUNTED if not found else other_drive('UUID', found, uuid)

    def owned(self, drive, names):
        """These top-level entries of a drive, when they exist and the drive is identified."""
        if not self.identified[drive]:
            return []
        folder = self.folder_of(drive)
        return [folder / name for name in names if (folder / name).exists() or (folder / name).is_symlink()]

    def usb_paths(self):
        if not self.identified['external'] or not self.mounted(self.external):
            return []
        folder = self.folder_of('external')
        ledgers = sorted(p.name for p in folder.glob('.neko-*'))
        return self.owned('external', USB_OWN + tuple(ledgers))

    def drive_paths(self, key):
        found = self.owned('data', SSD_DATA) if key == 'data' else []
        found += [p for drive, name in DRIVE_PLACES.get(key, ()) for p in self.owned(drive, (name,))]
        for name in self.placed:
            found += self.owned(EXTRA + name, self.group_drive_names(name, key))
        return found + (self.usb_paths() if key == 'backups' else [])

    def group_drive_names(self, name, key):
        """The top-level entries of a group's own drive that a choice covers: the apps'
        settings and databases ('nekoserver', for the apps' data) and the top folder of each
        kind of files placed there (photos, the library, Nextcloud's files)."""
        holds = self.placed[name][1]
        names = [GROUP_DRIVE_OWN] if key == 'data' and INSTALL in holds else []
        return names + sorted({top_folder(what) for what in CHOICE_HOLDS.get(key, ()) if what in holds})

    def others(self):
        """What is on the drives but is none of NekoServer's: it stays, whatever is chosen."""
        known = {'data': set(SSD_DATA), 'media': set(), 'external': set(USB_OWN)}
        for places in DRIVE_PLACES.values():
            for drive, name in places:
                known[drive].add(name)
        for name in self.placed:
            known[EXTRA + name] = {n for key, *_ in CHOICES for n in self.group_drive_names(name, key)}
        found = []
        for drive in self.drives:
            found.extend(p for p in self.listed(drive) if not ours(drive, p.name, known[drive]))
        return found

    def listed(self, drive):
        """What is at the top of a drive setup identified; nothing when it cannot be read."""
        folder = self.folder_of(drive)
        if not self.identified[drive] or not folder.is_dir():
            return []
        try:
            return sorted(folder.iterdir())
        except OSError:
            return []

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
        return [
            line
            for line in lines
            if line.strip()
            and not line.lstrip().startswith('#')
            and len(line.split()) >= 2
            # Never a line of the system itself, whatever the settings name.
            and line.split()[1] not in SYSTEM_PLACES
            and (line.split()[1] in folders or any(line.split()[0] == 'UUID=' + u for u in self.uuids))
        ]

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
        paths = self.system_paths() + self.shortcuts() + self.os_dropins() + self.setup_state()
        notes.extend(self.kept())
        if self.unfinished_packages():
            notes.append(
                'A package installation setup left unfinished is finished first, as setup would, so no '
                'service stays masked or kept from starting'
            )
        self._sleep_part(notes)
        self._samba_part(paths, notes)
        self.served = self.tailnet_routes()
        if self.served:
            notes.append(
                'Tailscale: the addresses NekoServer made on your tailnet are switched off (ports '
                + ', '.join(self.served)
                + '); any of your own stay, and this PC stays logged in to Tailscale'
            )
        if self.group_exists(CONTROL_GROUP):
            notes.append('The ' + CONTROL_GROUP + ' group goes')
        if self.docker_root():
            notes.append(
                'Docker keeps its data-root on the SSD ('
                + self.docker_root()
                + ') unless the SSD data or the mounts go; '
                'while it does, its start guard stays too (' + str(self.docker_guard()) + '), so Docker never starts '
                'on the system disk while the SSD is missing'
            )
        return {'paths': paths, 'notes': notes}

    def docker_guard(self):
        return self.at(DOCKER_GUARD)

    def kept_for_docker(self, chosen):
        """What stays although its group goes: Docker's start guard, while Docker keeps its
        data-root on the SSD (neither the SSD data nor the mounts chosen). Without it a
        missing SSD would make Docker start a new, empty data-root on the system disk."""
        if self.docker_root() and not {'data', 'mounts'} & set(chosen):
            return {self.docker_guard()}
        return set()

    def unfinished_packages(self):
        """A package installation setup left unfinished (host.py's record of it), or the
        start protection it keeps until it is tidied up (NekoServer's policy-rc.d)."""
        return self.at(host.Debian.journal).exists() or _our_policy(self.at(host.POLICY))

    def _sleep_part(self, notes):
        """The sleep targets to unmask (self.unmask) and what the plan says about them."""
        self.unmask = self.sleep_masks()
        if self.os_dropins() or self.unmask:
            notes.append(
                'The PC may sleep again: NekoServer\'s no-sleep and log-size settings go'
                + (' and these are unmasked: ' + ', '.join(self.unmask) if self.unmask else '')
                + ' (sleep settings you had before NekoServer stay)'
            )

    def _samba_part(self, paths, notes):
        """The Samba files NekoServer wrote (added to `paths`) and what the plan says."""
        shares, own, accounts = self.samba()
        self.samba_off = None
        if self.at(SAMBA_BEFORE).is_file():
            paths.append(self.at(SAMBA_BEFORE))
        if not shares:
            return
        unit, off, why = self.samba_service(own)
        if unit:
            self.samba_off = unit if off else None
            notes.append('Samba (' + unit + ') ' + ('is switched off: ' if off else 'stays on: ') + why)
        paths.append(shares)
        if self.at(SAMBA_CREATED).is_file():
            paths.append(self.at(SAMBA_CREATED))
        reused = self.reused_accounts()
        notes.append(
            'Samba: the shares NekoServer defined go'
            + (', with the account ' + ', '.join(accounts) + ' it created' if accounts else '')
            + ('; the account ' + ', '.join(reused) + ' was there before NekoServer and stays' if reused else '')
            + (
                '; smb.conf (only the include NekoServer wrote) goes too'
                if own
                else '; your own smb.conf stays (take out its "include = ' + SAMBA_SHARES + '" line yourself)'
            )
            + (
                "; the package's example smb.conf, set aside by setup, comes back"
                if own and self.at(PACKAGED_SMB_CONF).is_file()
                else ''
            )
        )
        if own:
            paths.append(own)

    def _apps_group(self):
        projects, networks = self.compose_projects(), self.docker_networks()
        notes = []
        if projects:
            notes.append('Remove the containers of ' + ', '.join(projects))
        if networks:
            notes.append('Remove the Docker networks ' + ', '.join(networks))
        if not projects and not networks:
            notes.append('No app containers found (' + no_containers_why() + ')')
        return {'paths': [], 'notes': notes}

    def _mounts_group(self):
        notes = []
        lines = self.fstab_lines()
        if lines:
            notes.append('Take out of /etc/fstab: ' + ' | '.join(lines))
        for folder in (self.data, self.media, self.external):
            if self.mounted(folder):
                notes.append('Unmount ' + folder + ' and remove the empty folder')
        for name in sorted(self.placed):
            if self.at(self.mount_of(EXTRA + name)).is_dir():
                notes.append(
                    'Unmount ' + self.mount_of(EXTRA + name) + ' (a drive of its own) and remove the empty folder'
                )
        if self.own_folders:
            notes.append(
                'Your own folders used where they are are unmounted from the library; the folders, and the '
                "shares they are on, stay as they are"
            )
        if self.docker_root():
            notes.append(
                "Docker's data-root setting ("
                + self.docker_root()
                + ') is taken out of '
                + DAEMON_JSON
                + ': Docker goes back to its default place'
            )
        return {'paths': [], 'notes': notes}

    def _drive_group(self, key):
        notes = []
        if key == 'backups' and not self.mounted(self.external):
            notes.append(
                'The USB drive is not mounted at '
                + self.external
                + ' right now (NekoServer mounts it for each weekly copy): '
                'plug it in and mount it there first, or delete its folders yourself'
            )
        if key == 'data' and self.docker_root():
            notes.append(
                "Docker's data-root setting ("
                + self.docker_root()
                + ') is taken out of '
                + DAEMON_JSON
                + ' and Docker is stopped first'
            )
        return {'paths': self.drive_paths(key), 'notes': notes}

    def size(self, path):
        """How much is in a folder (du), or that it can't be known: without sudo, a folder
        this account may not read in (setup's are private) would be counted too small."""
        if os.geteuid() != 0 and self.unreadable_inside(path):
            return SIZE_UNKNOWN
        out = self.run(['du', '-sh', str(path)], 600)
        return out.split()[0] if out.split() else '?'

    def unreadable_inside(self, path):
        """Whether something in the folder (or the folder itself) can't be read or entered
        by this account: the first one found is enough."""
        found = self.run(
            ['find', str(path), '(', '!', '-readable', '-o', '-type', 'd', '!', '-executable', ')', '-print', '-quit'],
            600,
        )
        return bool(found.strip())

    def places(self):
        """Every drive and folder NekoServer uses, by name and with what is on it (gate run
        C18: a group's own drive was never named). Only the drives can lose anything, and
        only what the choices below list; the person's own folders and shares never do."""
        lines = ['== The drives and folders NekoServer uses:']
        for drive, words in (('data', 'the SSD: the apps'), ('media', 'the HDD: media files and backups')):
            lines.append('   - ' + self.mount_of(drive) + ' (' + words + ')')
        if self.records['external'].get('uuid'):
            lines.append('   - ' + self.external + ' (the USB drive: the weekly copy, when it is plugged in)')
        for name, (_record, holds) in sorted(self.placed.items()):
            lines.append('   - ' + self.mount_of(EXTRA + name) + ' (a drive of its own: ' + held_words(holds) + ')')
        for folder in self.own_folders:
            shared = ':' in folder['share'] or folder['share'].startswith('//')
            lines.append(
                '   - '
                + folder['path']
                + ' ('
                + WHAT_WORDS.get(folder['what'], folder['what'])
                + (', on the shared folder ' + folder['share'] if shared else '')
                + '): your own folder: kept, never removed'
            )
        for share, mount in sorted({(f['share'], f['mount']) for f in self.own_folders if ':' in f['share']}):
            lines.append('   - ' + share + ' at ' + mount + ' (your own shared folder: kept, with its /etc/fstab line)')
        return lines

    def describe(self, chosen):
        """The plan, in full, for the person to read before confirming."""
        lines = ['!! ' + w for w in self.warnings]
        lines.extend(self.places())
        others = self.others()
        if others:
            lines.append('== Not NekoServer\'s, so these stay on the drives whatever you choose:')
            lines.extend('   - ' + str(p) for p in others)
        for key, _, title, _ in CHOICES:
            if key not in chosen:
                continue
            group = self.groups[key]
            lines.append('== ' + title)
            lines.extend('   - ' + note for note in group['notes'])
            keep = self.kept_for_docker(chosen)
            lines.extend(
                '   - remove ' + str(p) + ('  (' + self.size(p) + ')' if p.is_dir() and key != 'server' else '')
                for p in group['paths']
                if p not in keep
            )
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


def remove(plan, chosen, run=run, out=print, rmtree=shutil.rmtree, root='/', call=call, sleep=None, reconcile=None):
    """Do it, in a safe order. Nothing may be unfinished (a monthly update, a backup's
    recovery record) when it starts; then the schedules are switched off and everything
    stops (Samba too, when NekoServer switched it on); then, with the jobs' shared lock
    held and everything checked again (nothing running, nothing unfinished, each drive
    still the one the settings name), the apps' containers, Docker's data-root setting
    and the files go; then the mounts. When something does
    not stop or is unfinished, Stopped is raised before anything is deleted, with the
    services as they were. `reconcile` finishes an unfinished package installation
    (host.py's, on this PC by default)."""
    units = plan.units()
    samba = [plan.samba_off] if getattr(plan, 'samba_off', None) else []
    shares, own, accounts = plan.samba()  # read now: the files go with the rest
    _admitted(plan, call)
    before = _unit_states(plan, units + samba, call)
    drives = {}
    try:
        _stop_units(plan, units, call, out, sleep, check=lambda: _maintenance_idle(call))
        _stop_samba(plan, call, out)
        drives = open_drives(plan, out)
        jobs = JobsLock(drives.get('data'), sleep)
        jobs.take()
        try:
            _admitted(plan, call)
            _nothing_running(units, call)
            # Only now, with the lock held and every check passed, what can't be undone
            # starts: the apps' containers (a job holding the lock may be using them), then
            # Docker's data-root setting. Neither waits for a service that takes the lock.
            _remove_docker_parts(plan, chosen, root, call, out)
        except BaseException:
            jobs.release()
            raise
    except Stopped as exc:
        close_drives(drives)
        raise Stopped(str(exc) + _restore_units(before, call, out)) from None
    except BaseException:
        close_drives(drives)
        raise
    try:
        left = _destroy(plan, chosen, out, (rmtree, call, reconcile), accounts, drives, jobs)
    finally:
        jobs.release()
        close_drives(drives)  # before the drives are unmounted: an open folder keeps one busy
    _tidy_system(plan, units, shares and not plan.samba_off, own, run, out)
    if 'mounts' in chosen:
        left += _remove_mounts(plan, system_file(root, FSTAB), call, out)
    if units:
        run(['systemctl', 'daemon-reload'], 120)
    return left


def _destroy(plan, chosen, out, tools, accounts, drives, jobs):
    """Everything that cannot be undone, while the jobs' shared lock is held. Returns what
    was left. (tools: rmtree, call and reconcile, as remove got them.)"""
    rmtree, call, reconcile = tools
    left = []
    for port in plan.served:
        out('Switching off the Tailscale address on port ' + port)
        _tried(
            call,
            out,
            ['tailscale', 'serve', '--bg', '--https=' + port, 'off'],
            left,
            'the Tailscale address on port ' + port,
        )
    _remove_samba_accounts(plan, accounts, call, out, left)
    if plan.unmask:
        out('Unmasking ' + ', '.join(plan.unmask))
        _tried(call, out, ['systemctl', 'unmask', *plan.unmask], left, 'the masks on ' + ', '.join(plan.unmask))
    _finish_packages(plan, call, out, reconcile)
    journal_limits = any('journald' in str(p) for p in plan.os_dropins())
    # The person's own folders bound into a library: unmounted before anything around them
    # is deleted (a bind from the same drive looks like a plain folder). A path that still
    # holds one, or that may hold one that could not be checked, is kept.
    unchecked = unchecked_folders(plan, out)
    held = _unbind_own_folders(plan, chosen, call, out, left) + unchecked
    keep = jobs.kept() | kept_folders(plan, skip=set(unchecked))
    left += _remove_files(plan, chosen, out, rmtree, drives, keep, held)
    # The lock last: released first, then its file (with the folder, when the SSD data goes).
    jobs.release()
    jobs.tidy('data' in chosen and plan.folder_of('data') / 'installer' in plan.groups['data']['paths'])
    if journal_limits:  # the log limits go now; logind's at the next start
        _tried(
            call,
            out,
            ['systemctl', 'restart', 'systemd-journald.service'],
            left,
            "NekoServer's log limits (until the next restart)",
        )
    return left


def kept_folders(plan, skip=()):
    """The person's own folders as (device, inode): a library folder that goes never takes
    one with it, even where it is mounted inside that library from the same drive (a bind
    mount of the same filesystem has the same device number, so the other-drive check alone
    would not stop there). `skip`: the ones that did not answer (never looked at again)."""
    kept = set()
    for folder in getattr(plan, 'own_folders', ()):
        if folder['path'] in skip:
            continue
        try:
            found = os.stat(plan.at(folder['path']))
        except (OSError, RuntimeError):
            continue
        kept.add((found.st_dev, found.st_ino))
    return kept


def unchecked_folders(plan, out):
    """The person's own folders that could not be checked (looked at) now, with the folders
    setup mounts them at: nothing around any of them is removed, since what is theirs could
    not be told from what is NekoServer's. Said, so the person knows why something stayed."""
    held = []
    for folder in getattr(plan, 'own_folders', ()):
        why = not_checked(plan, folder['path'])
        if why:
            out(
                '  your own folder ' + folder['path'] + ' ' + why + ', so it is kept and nothing around it (or '
                'around the folders it is shown in) is removed'
            )
            held.append(folder['path'])
    return held + own_bind_targets(plan) if held else []


# How long one of the person's folders (on a shared folder, perhaps) may take to answer.
LOOK_SECONDS = 10
NO_ANSWER = f'did not answer within {LOOK_SECONDS} seconds (its shared folder is off or not connected)'


def not_checked(plan, path):
    """Why the person's folder at `path` could not be checked now, or None."""
    try:
        place = plan.at(path)
    except RuntimeError:
        return 'could not be checked'
    if not answers(place):
        return NO_ANSWER
    try:
        os.stat(place)
    except OSError:
        return 'could not be checked'
    return None


def answers(path, seconds=LOOK_SECONDS, start=subprocess.Popen, now=time.monotonic, sleep=time.sleep):
    """Whether `path` can be looked at (stat) within `seconds`, by a child process
    (neko_common.answered_within): a shared folder that is away can hold a look for ever."""
    return answered_within(['stat', '--', str(path)], seconds, start, now, sleep) is not None


def own_bind_targets(plan):
    """The folders setup's /etc/fstab block mounts the person's own folders at (inside the
    libraries of its drives)."""
    try:
        text = plan.at(FSTAB).read_text(errors='replace')
    except (OSError, RuntimeError):
        return []
    begin, end = FSTAB_BLOCKS[0]
    lines = [line.strip() for line in text.splitlines()]
    if begin not in lines or end not in lines[lines.index(begin) :]:
        return []
    first = lines.index(begin)
    inside = lines[first + 1 : lines.index(end, first)]
    return [line.split()[1] for line in inside if len(line.split()) >= 2 and not line.startswith('#')]


def holds(path, folder, plan):
    """`path` (a path the plan found) is `folder` (an absolute path) or a folder around it."""
    try:
        inner = Path(plan.at(folder))
    except RuntimeError:
        return False
    return inner == Path(path) or Path(path) in inner.parents


def _unbind_own_folders(plan, chosen, call, out, left):
    """Each bind of the person's own folders inside a path that is about to be removed,
    unmounted first, the deepest first. Returns those that would not unmount (still in
    place: the paths around them are kept)."""
    paths = _chosen_paths(plan, chosen)
    stuck = []
    for target in sorted(set(own_bind_targets(plan)), key=len, reverse=True):
        place = plan.at(target)
        if not any(holds(path, target, plan) for path in paths):
            continue
        if not answers(place):
            # Shown from a shared folder that is away: left mounted, and the paths around it kept.
            out('  your own folder shown at ' + target + ' ' + NO_ANSWER + ', so it is kept as it is')
            stuck.append(target)
            continue
        if not os.path.ismount(str(place)):
            continue
        out('Unmounting ' + target + ' (your own folder, shown there: it stays where it is)')
        code, said = call(['umount', target], 120)
        if code != 0:
            out('  could not unmount (busy' + (': ' + said if said else '') + '): sudo umount ' + target)
            left.append(target + ' (still mounted)')
            stuck.append(target)
    return stuck


def _our_policy(policy):
    try:
        return policy.is_file() and policy.read_text(errors='replace') == host.OUR_POLICY
    except OSError:
        return False


def _reconcile_here():
    host.current().reconcile()


def _finish_packages(plan, call, out, reconcile=None):
    """A package installation setup left unfinished (its record is in /var/lib/nekoserver-host,
    which goes with NekoServer) is finished first, as the next setup run would: its packages
    set up, their new services unmasked and left switched off, NekoServer's policy-rc.d
    removed. With only the record deleted, those services would stay masked for good, and
    policy-rc.d would keep every package installed later from starting its service. When
    finishing is not possible, the protection alone is lifted."""
    if not plan.unfinished_packages():
        return
    out('Finishing the package installation setup left unfinished')
    try:
        (reconcile or _reconcile_here)()
        return
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:  # host.Unsupported is a RuntimeError
        out('  could not finish it (' + str(exc) + '); lifting the protection it kept instead')
    record = plan.at(host.Debian.journal)
    # A mask the package set itself (Debian's record of those) stays.
    units = [unit for unit in _recorded_units(record) if not plan.at(host.PACKAGE_MASKS + '/' + unit).exists()]
    if units:
        out('Unmasking ' + ', '.join(units))
        _tried(call, out, ['systemctl', 'unmask', *units])
    policy = plan.at(host.POLICY)
    if _our_policy(policy):
        out('  removing ' + str(policy))
        policy.unlink(missing_ok=True)


def _recorded_units(record):
    """The services the unfinished installation masked (its record's new_units)."""
    try:
        units = json.loads(record.read_text())['before']['new_units']
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(units, list):
        return []
    return [unit for unit in units if isinstance(unit, str) and re.fullmatch(r'[\w@.:-]+\.(service|socket)', unit)]


def _tidy_system(plan, units, shares, own, run, out):
    """After the files: Samba's example smb.conf back, systemd and Samba told, the group gone."""
    packaged = plan.at(PACKAGED_SMB_CONF)
    if own and packaged.is_file():
        out("Putting the package's example smb.conf back")
        packaged.rename(plan.at(SAMBA_CONFIG))
    if units:
        run(['systemctl', 'daemon-reload'], 120)
        run(['systemctl', 'reset-failed'], 60)
    if shares and plan.root == Path('/') and shutil.which('smbcontrol'):
        run(['smbcontrol', 'all', 'reload-config'], 30)
    if plan.group_exists(CONTROL_GROUP):
        run(['groupdel', CONTROL_GROUP], 30)


def _tried(call, out, argv, left=None, what=''):
    """Run one change; when it fails, say so with the command to run by hand (and add
    `what` to the list of what was left, when given). True when it worked."""
    code, said = call(argv, 60)
    if code != 0:
        out('  could not (' + (said or EXIT_STATUS + str(code)) + '): sudo ' + ' '.join(argv))
        if left is not None and what:
            left.append(what + ' (sudo ' + ' '.join(argv) + ')')
    return code == 0


def _stop_samba(plan, call, out):
    """Samba switched off when NekoServer switched it on (the plan says which, and why),
    before anything is deleted: it must have stopped, and systemd must say so. While it
    still runs (or nobody can tell), its accounts and shares, and the files it serves,
    are not touched: Stopped."""
    unit = getattr(plan, 'samba_off', None)
    if not unit:
        return
    out('Switching off Samba (' + unit + '), which NekoServer switched on')
    code, said = call(['systemctl', 'disable', '--now', unit], 120)
    state = (call(['systemctl', 'is-active', unit], 30)[1].split() or ['unknown'])[0]
    if code != 0 or state not in STOPPED:
        why = (said or EXIT_STATUS + str(code)) if code != 0 else 'systemd says it is ' + state
        raise Stopped(
            'Samba (' + unit + ') did not switch off (' + why + '), so its accounts, its shares and the files it '
            'serves stay as they are.' + NOTHING_DELETED
        )


def _admitted(plan, call):
    """Nothing is unfinished or running that removing NekoServer would break: checked
    before anything is stopped, and again with the jobs' lock held (a monthly update can
    start, or be cut off, while you read the plan). Stopped otherwise."""
    import neko_common

    blocked = neko_common.pending_maintenance(
        plan.at(str(neko_common.MAINTENANCE_JOURNAL)),
        outcome='Nothing was removed.',
        running=lambda: not _maintenance_idle(call, raises=False),
    )
    if blocked:
        raise Stopped(blocked)
    _maintenance_idle(call)
    # A NekoServer job that was cut off keeps its recovery record until it is finished (by
    # its resume service, at the next start at the latest): uninstalling then would delete
    # what can finish it, and leave the apps stopped or half restored. The monthly
    # update's record is checked above, with its own message.
    for job, (path, unit) in neko_common.RECOVERY.items():
        record = str(path)
        if job != 'maintenance' and plan.at(record).exists():
            raise Stopped(
                'A NekoServer job was cut off and is not finished yet: its record ' + record + ' is still there, and '
                'removing NekoServer now would delete what finishes it. Run  sudo systemctl start '
                + unit
                + '  (or restart the computer) and let it finish.'
                + NOTHING_DELETED
            )


def _maintenance_idle(call, raises=True):
    """True when no monthly update (nor the service that finishes a cut-off one) is
    working; one that is: Stopped (or False, with raises=False). It is never stopped
    part-way: that would leave the apps half updated."""
    import neko_common

    states = call(['systemctl', 'is-active', *neko_common.MAINTENANCE_UNITS], 30)[1].split()
    if not {'active', 'activating', 'deactivating', 'reloading'} & set(states):
        return True
    if raises:
        raise Stopped(
            'An app update is running right now. Let it finish ('
            + neko_common.MAINTENANCE_SHOWN
            + '  lists no running one once it has), then run  sudo bash uninstall.sh  again.'
            + NOTHING_DELETED
        )
    return False


def _nothing_running(units, call):
    """With the jobs' lock held: none of NekoServer's services runs (none started again
    since they were stopped)."""
    for unit in units:
        state = (call(['systemctl', 'is-active', unit], 30)[1].split() or ['unknown'])[0]
        if state in STILL_RUNNING:
            raise Stopped(
                unit + ' started again while the others were stopped (it is ' + state + ').' + NOTHING_DELETED
            )


# The kinds of NekoServer's systemd units the uninstaller stops and removes, and those that
# start a service by themselves.
UNIT_KINDS = ('.service', '.timer', '.socket')
STARTERS = ('.timer', '.socket')
EXIT_STATUS = 'exit status '  # what a failed command said when it said nothing
STILL_RUNNING = ('active', 'activating', 'deactivating', 'reloading', 'refreshing')
STOPPED = ('inactive', 'failed')  # what systemctl is-active says of a service that has stopped
NOTHING_DELETED = (
    ' Nothing has been deleted yet. Fix that (or restart the computer), then run  sudo bash uninstall.sh  again.'
)


def _unit_states(plan, units, call):
    """{unit: (switched on at startup, running now, starts again by itself)} before anything
    is stopped, so a stopped uninstall can put them back as they were."""
    states = {}
    for unit in units:
        enabled = call(['systemctl', 'is-enabled', unit], 30)[1].split()[:1] == ['enabled']
        active = call(['systemctl', 'is-active', unit], 30)[1].split()[:1] == ['active']
        try:
            text = (plan.at(UNITS) / unit).read_text(errors='replace')
        except OSError:
            text = ''
        # A finished oneshot job (a backup, say) is not run again; a service is started.
        oneshot = 'Type=oneshot' in text and 'RemainAfterExit=yes' not in text
        states[unit] = (enabled, active, active and not oneshot)
    return states


def _restore_units(before, call, out):
    """After a stopped uninstall: every NekoServer service and timer switched on again as
    it was. Returns the words for the message."""
    failed = []
    for unit, (enabled, _active, restart) in before.items():
        for wanted, argv in ((enabled, ['systemctl', 'enable', unit]), (restart, ['systemctl', 'start', unit])):
            if wanted and call(argv, 120)[0] != 0:
                failed.append(unit)
    if failed:
        return (
            ' These could not be switched back on: '
            + ', '.join(dict.fromkeys(failed))
            + '; running  sudo bash install.sh  switches everything on again.'
        )
    out("NekoServer's services and timers were switched back on as they were")
    return " NekoServer's services and timers were switched back on as they were."


def _disable(units, call):
    """systemctl disable --now for each; what failed, in words."""
    failed = []
    for unit in units:
        code, said = call(
            ['systemctl', 'disable', '--now', unit], CONTROL_STOP_SECONDS if unit == CONTROL_UNIT else 120
        )
        if code != 0:
            failed.append(unit + ' (' + (said or EXIT_STATUS + str(code)) + ')')
    return failed


def _stop_units(plan, units, call, out, sleep=None, check=None):
    """Timers and sockets first (nothing new starts), then `check` (a monthly update that
    started meanwhile stops the uninstall, never the update), then the services, each
    stopped with what it does when stopped (a backup restarts the apps it stopped); each
    one must really have stopped, and the fan control must have handed every fan back to
    the motherboard. No lock is held meanwhile: a stopping backup takes the jobs' lock."""
    if not units:
        return
    out("Stopping NekoServer's services and timers")
    # Timers and sockets first: either would start its service again once that is stopped.
    timers = [u for u in units if u.endswith(STARTERS)]
    failed = _disable(timers, call)
    if check:
        check()
    failed += _disable([u for u in units if u not in timers], call)
    for unit in units:
        _, said = call(['systemctl', 'is-active', unit], 30)
        if said.split()[:1] and said.split()[0] in STILL_RUNNING:
            failed.append(unit + ' is still ' + said.split()[0])
    if failed:
        raise Stopped('These did not stop: ' + '; '.join(dict.fromkeys(failed)) + '.' + NOTHING_DELETED)
    if CONTROL_UNIT in units and not _fans_handed_back(plan, sleep):
        raise Stopped(
            'The fan control stopped, but some fans were not handed back to the motherboard (their records are still in '
            '/run/nekoserver-control). Check the fans: restarting the computer hands every fan back.' + NOTHING_DELETED
        )


def _fans_handed_back(plan, sleep=None, wait=15):
    import time

    for _ in range(wait * 2):
        if not any(plan.at(p).exists() for p in FAN_RECORDS):
            return True
        (sleep or time.sleep)(0.5)
    return False


def _remove_docker_parts(plan, chosen, root, call, out):
    """The apps' containers (with the apps chosen), then Docker's data-root setting (when
    the folder it names goes). Called with the jobs' lock held, after the last checks."""
    if 'apps' in chosen:
        _remove_containers(plan, call, out)
    if plan.docker_root() and ('data' in chosen or 'mounts' in chosen):
        _reset_docker_root(system_file(root, DAEMON_JSON), call, out)


def _remove_containers(plan, call, out):
    """Each app's containers removed, and checked gone, before its data can be deleted."""
    for project in plan.compose_projects():
        out('Removing the containers of ' + project)
        label = 'label=com.docker.compose.project=' + project
        ids = plan.run(['docker', 'ps', '-aq', '--filter', label], 60).split()
        if ids:
            code, said = call(['docker', 'rm', '-f', *ids], 300)
            if code != 0 or plan.run(['docker', 'ps', '-aq', '--filter', label], 60).split():
                raise Stopped(
                    'The containers of '
                    + project
                    + ' could not be removed ('
                    + (said or 'they are still there')
                    + ').'
                    + NOTHING_DELETED
                )
    for network in plan.docker_networks():
        call(['docker', 'network', 'rm', network], 60)


def _reset_docker_root(path, call, out):
    """Docker stopped (and checked stopped) before its data-root setting changes, so it
    never writes into a folder that is about to go."""
    out('Stopping Docker and taking its data-root setting out of ' + DAEMON_JSON)
    code, said = call(['systemctl', 'stop', 'docker.service', 'docker.socket'], 300)
    _, state = call(['systemctl', 'is-active', 'docker.service'], 30)
    if code != 0 or (state.split()[:1] and state.split()[0] in STILL_RUNNING):
        raise Stopped(
            'Docker did not stop ('
            + (said or state or 'still running')
            + '), so its data stays where it is.'
            + NOTHING_DELETED
        )
    content = json.loads(path.read_text())
    content.pop('data-root', None)
    replace_system_file(path, json.dumps(content, indent=2) + '\n')


def _remove_samba_accounts(plan, accounts, call, out, left=None):
    """Only accounts NekoServer created (its record of them); a failure is said, not hidden."""
    for account in accounts:
        if plan.run(['pdbedit', '-L', '-u', account], 30).strip():
            out('Removing the Samba account ' + account + ' (NekoServer created it)')
            code, said = call(['smbpasswd', '-x', account], 30)
            if code != 0:
                out('  could not remove it (' + (said or EXIT_STATUS + str(code)) + '): sudo smbpasswd -x ' + account)
                if left is not None:
                    left.append('the Samba account ' + account + ' (sudo smbpasswd -x ' + account + ')')


def _drive_of(plan, path):
    """Which of NekoServer's drives a planned path is on ('data', 'media', 'external'), or
    None for a place of its own on the system disk. The plan lists a drive's entries
    right inside its mount folder."""
    drives = getattr(plan, 'drives', DRIVES)
    return next((drive for drive in drives if plan.identified[drive] and path.parent == plan.folder_of(drive)), None)


def _chosen_paths(plan, chosen):
    """Every path of the chosen groups, in the order of the choices, but Docker's start
    guard while it is still needed (kept_for_docker)."""
    keep = plan.kept_for_docker(chosen)
    return [p for key, *_ in CHOICES if key in chosen for p in plan.groups[key]['paths'] if p not in keep]


def _remove_files(plan, chosen, out, rmtree, drives=None, keep_inodes=(), held=()):
    """The paths of every chosen group: each one the plan itself found, under NekoServer's
    own places or its drives (mount_folder), never anywhere the settings alone point.
    What is on a drive is deleted only through that drive's folder as opened and checked
    right before (open_drives); a drive that is not the one the settings name any more
    loses nothing. A path around one of `held` (the person's folders that could not be
    checked or unmounted) is kept. Returns what could not be removed (said at the end)."""
    drives = drives or {}
    left = []
    for path in _chosen_paths(plan, chosen):
        inside = [folder for folder in held if holds(path, folder, plan)]
        if inside:
            out('  kept ' + str(path) + ': your own folder ' + inside[0] + ' is in it, so it was not removed')
            left.append(str(path))
            continue
        drive = _drive_of(plan, path)
        if drive is not None and drive not in drives:
            left.append(Skipped(path))  # open_drives said why, once for the drive
            continue
        out('  removing ' + str(path))
        gone = _removed(path, rmtree, out) if drive is None else _removed_on(drives[drive], path, out, keep_inodes)
        if not gone:
            left.append(str(path))
    return left


class Skipped(str):
    """A planned path on a drive that was not the one the settings name when the files
    went: nothing there was touched, and nothing there may be deleted by hand either (it
    is the other drive's)."""


SHOWN_FAILURES = 5  # of the files inside one folder that could not be deleted, this many are named


def _say_failed(failed, path, out):
    for line in failed[:SHOWN_FAILURES]:
        out('  could not delete ' + line)
    if len(failed) > SHOWN_FAILURES:
        out('  ... and ' + str(len(failed) - SHOWN_FAILURES) + ' more in ' + str(path))


def _removed(path, rmtree, out=print):
    """Remove one path the plan found; True when it is gone. What could not be deleted
    (a file in use, a read-only drive, permission denied) is said, never skipped quietly."""
    failed = []

    def failure(_function, where, error):
        failed.append(str(where) + ': ' + str(error[1] if isinstance(error, tuple) else error))

    try:
        if path.is_dir() and not path.is_symlink():
            rmtree(path, **{('onexc' if sys.version_info >= (3, 12) else 'onerror'): failure})
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        failed.append(str(path) + ': ' + str(exc))
    _say_failed(failed, path, out)
    return not (path.exists() or path.is_symlink())


# ---- deleting on a drive: only through its folder as opened and checked, never by path
DRIVES = ('data', 'media', 'external')
FOLDER_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
OTHER_DRIVE = 'another drive is mounted there, so it was not touched'


class Drive:
    """One of NekoServer's drives, its mount folder open (fd) on the filesystem checked to be
    the one the settings name (dev: that filesystem's device number)."""

    def __init__(self, fd, dev, folder):
        self.fd, self.dev, self.folder = fd, dev, folder

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def open_drive(plan, drive):
    """(Drive, None), or (None, why not): the mount folder opened once (never through a
    symlink), the drive mounted there checked again (UUID, filesystem, subvolume), and the
    folder checked to be still on the filesystem that was opened. Everything deleted on
    the drive goes through this handle, so a drive mounted there later is never touched."""
    folder = plan.folder_of(drive)
    try:
        fd = os.open(folder, FOLDER_FLAGS)
    except OSError as exc:
        return None, 'its folder could not be opened (' + (exc.strerror or str(exc)) + ')'
    problem = plan.identity_problem(drive)
    try:
        moved = os.stat(folder, follow_symlinks=False).st_dev != os.fstat(fd).st_dev
    except OSError:
        moved = True
    if problem or moved:
        os.close(fd)
        return None, _now_there(problem) if problem else 'it changed while it was being checked'
    return Drive(fd, os.fstat(fd).st_dev, folder), None


def _now_there(problem):
    """What is at a drive's folder now, from identity_problem's words."""
    return 'no drive is mounted there now' if problem == NOT_MOUNTED else 'the drive there now is ' + problem


def open_drives(plan, out):
    """{drive: Drive} for each drive the plan identified and that is still the one the
    settings name now, right before anything on it is deleted. One that is not (another
    drive mounted there since the plan was made, or none): said, and nothing on it goes."""
    drives = {}
    for drive in getattr(plan, 'drives', DRIVES):
        if not plan.identified[drive]:
            continue
        handle, why = open_drive(plan, drive)
        if handle is None:
            out('Nothing on ' + str(plan.folder_of(drive)) + ' is removed: ' + why + '.')
        else:
            drives[drive] = handle
    return drives


def close_drives(drives):
    for handle in drives.values():
        handle.close()


def _removed_on(drive, path, out, keep_inodes=()):
    """Delete one entry right inside the drive's folder, through the drive's handle; True
    when it is gone. Never follows a symlink, and stops wherever another filesystem is
    mounted (that one is not touched)."""
    failed, held = [], []
    _delete_at(drive.fd, path.name, drive.dev, (failed, str(path), set(keep_inodes), held))
    _say_failed(failed, path, out)
    for where in held:
        if not where.endswith('/' + '/'.join(JobsLock.PATH)):
            out('  kept ' + where + ': your own folder, never removed')
    if held:  # only the jobs' lock or the person's folders are left in it (JobsLock.tidy)
        return not failed
    try:
        os.stat(path.name, dir_fd=drive.fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _delete_at(parent, name, dev, how):
    """Delete name (in the folder open as `parent`), all of it when it is a folder. how:
    (the list of failures, its path for messages, (device, inode) pairs to keep, the list
    of what was kept)."""
    failed, where, keep, held = how
    try:
        found = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        failed.append(where + ': ' + (exc.strerror or str(exc)))
        return
    if (found.st_dev, found.st_ino) in keep:  # the jobs' lock, or a folder of the person's
        held.append(where)
        return
    subvolume = found.st_dev != dev
    if subvolume and not _nested_subvolume(parent, name, found):
        failed.append(where + ': ' + OTHER_DRIVE)
        return
    before = len(failed)
    _remove_found(parent, name, found, how)
    if subvolume and len(failed) > before:  # one line for it, with the first reason
        first = failed[before]
        del failed[before:]
        failed.append(where + ': ' + SUBVOLUME_LEFT + where + '  (' + first + ')')


def _remove_found(parent, name, found, how):
    """Remove `name` (its stat result `found`) in the folder open as `parent`: a folder
    emptied first, then removed."""
    failed, where, _keep, held = how
    try:
        if stat.S_ISDIR(found.st_mode):
            # A Btrfs subvolume in it is part of the same drive: emptied and removed with
            # it (rmdir removes an empty subvolume), its files checked against its own device.
            _empty_at(parent, name, found.st_dev, how)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)
    except OSError as exc:
        if not (exc.errno == errno.ENOTEMPTY and (failed or held)):  # what stayed in it is said once, for itself
            failed.append(where + ': ' + (exc.strerror or str(exc)))


# A subvolume that could not be emptied (a read-only snapshot, say) is said with what removes it.
SUBVOLUME_LEFT = 'it is a Btrfs subvolume of this drive (a snapshot, perhaps read-only); remove it with  sudo btrfs subvolume delete '
BTRFS_SUBVOLUME_ROOT = 256  # the inode number of the top folder of every Btrfs subvolume


def _mount_id(fd):
    """The mount an open folder is on (its mnt_id in /proc/self/fdinfo), or None."""
    try:
        with open('/proc/self/fdinfo/' + str(fd), encoding='ascii') as info:
            for line in info:
                key, _, value = line.partition(':')
                if key == 'mnt_id':
                    return value.strip()
    except (OSError, ValueError):
        return None
    return None


def _nested_subvolume(parent, name, found):
    """Whether name (in the folder open as `parent`), on another device number than its
    folder, is a Btrfs subvolume inside the same drive rather than another drive mounted
    there: the top folder of a subvolume, on the very same mount as its folder. Anything
    that can't be told apart counts as another drive (it is left alone)."""
    if found.st_ino != BTRFS_SUBVOLUME_ROOT or not stat.S_ISDIR(found.st_mode):
        return False
    try:
        fd = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    except OSError:
        return False
    try:
        mount = _mount_id(fd)
        return os.fstat(fd).st_dev == found.st_dev and mount is not None and mount == _mount_id(parent)
    finally:
        os.close(fd)


def _empty_at(parent, name, dev, how):
    """Everything in the folder name (in `parent`), opened without following a symlink and
    checked to be on the same filesystem."""
    failed, where, keep, held = how
    fd = os.open(name, FOLDER_FLAGS, dir_fd=parent)
    try:
        if os.fstat(fd).st_dev != dev:
            raise OSError(errno.EXDEV, OTHER_DRIVE)
        for entry in os.listdir(fd):
            _delete_at(fd, entry, dev, (failed, where + '/' + entry, keep, held))
    finally:
        os.close(fd)


class JobsLock:
    """The lock NekoServer's jobs share (installer/recovery.lock on the SSD: backups, the
    monthly update and setup take it before they change the apps). Taken only after every
    job was stopped (a stopping backup takes it itself, to restart the apps), never while
    waiting for one; held while anything is deleted. Opened through the SSD's checked
    handle, so it is the SSD's own lock. Without the SSD no job can run, and none is taken."""

    PATH = ('installer', 'recovery.lock')
    TRIES, PAUSE = 6, 5  # a job that is just ending gets half a minute

    def __init__(self, drive, sleep=None):
        self.drive, self.sleep, self.fd, self.folder = drive, sleep or time.sleep, None, None

    def take(self):
        if self.drive is None:
            return
        name = self.PATH[0]
        try:
            os.mkdir(name, 0o700, dir_fd=self.drive.fd)
        except FileExistsError:
            pass
        self.folder = os.open(name, FOLDER_FLAGS, dir_fd=self.drive.fd)
        self.fd = os.open(
            self.PATH[1], os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.folder
        )
        for attempt in range(self.TRIES):
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if attempt + 1 < self.TRIES:
                    self.sleep(self.PAUSE)
        self.release()
        raise Stopped(
            'A NekoServer job is still working (it holds '
            + str(self.drive.folder / '/'.join(self.PATH))
            + '). Let it finish, then run  sudo bash uninstall.sh  again.'
            + NOTHING_DELETED
        )

    def kept(self):
        """The lock file, as (device, inode): it stays while it is held."""
        if self.fd is None:
            return set()
        found = os.fstat(self.fd)
        return {(found.st_dev, found.st_ino)}

    def release(self):
        """Closing the lock file releases the lock (again: nothing to do)."""
        for name in ('fd', 'folder'):
            if getattr(self, name) is not None:
                os.close(getattr(self, name))
                setattr(self, name, None)

    def tidy(self, folder_goes):
        """After release: the lock file goes when its folder goes too (the SSD data chosen),
        then the folder, now empty. Through the SSD's checked handle, as everything else."""
        if self.drive is None or self.drive.fd is None or not folder_goes:
            return
        name, lock = self.PATH
        try:
            folder = os.open(name, FOLDER_FLAGS, dir_fd=self.drive.fd)
        except OSError:
            return
        try:
            if os.fstat(folder).st_dev == self.drive.dev:
                os.unlink(lock, dir_fd=folder)
        except OSError:
            pass
        finally:
            os.close(folder)
        try:
            os.rmdir(name, dir_fd=self.drive.fd)
        except OSError:
            pass


def replace_system_file(path, text):
    """A system file (/etc/fstab, Docker's daemon.json) replaced in one step, never left
    half written (a cut-off write of /etc/fstab can stop the PC from starting). It keeps
    its permissions, and the file as it was is kept beside it."""
    from neko_common import atomic_write

    path = Path(path)
    mode = path.stat().st_mode & 0o7777
    backup = path.with_name(path.name + '.before-nekoserver-uninstall-' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, backup)
    atomic_write(path, text, mode)
    return backup


# The blocks setup writes in /etc/fstab for folders mounted inside its drives (0.50.0),
# each from its first line to its last: a person's own media used where it is
# (existing_media.py) and the apps installed on another drive (modules/storage.py).
FSTAB_BLOCKS = (
    (
        '# NekoServer: existing media used where it is (written by setup from my-config.json)',
        '# NekoServer: end of existing media',
    ),
    (
        '# NekoServer: apps installed on another drive (written by setup from my-config.json)',
        '# NekoServer: end of apps installed on another drive',
    ),
)
# Where the drives beyond the two are mounted, each in a folder of its name (neko_common.EXTRA_MOUNTS).
EXTRA_MOUNTS = '/mnt/nekoserver-drives'
# The one drive that holds both roles as folders (modules/storage.ONE_DRIVE).
ONE_DRIVE = EXTRA_MOUNTS + '/one'


def fstab_blocks(text):
    """(the lines of setup's own blocks in /etc/fstab, their first and last lines
    included; the folders those lines mount). A block without its last line is not
    taken: nothing after it is setup's for certain."""
    lines = text.splitlines()
    taken, folders = [], []
    for begin, end in FSTAB_BLOCKS:
        marks = [line.strip() for line in lines]
        if begin not in marks or end not in marks[marks.index(begin) :]:
            continue
        first = marks.index(begin)
        last = marks.index(end, first)
        taken += lines[first : last + 1]
        folders += [line.split()[1] for line in lines[first + 1 : last] if len(line.split()) >= 2]
    return taken, folders


def _unmount_inside(plan, folders, call, out, left):
    """What setup mounted inside its drives, the deepest first: a drive with something
    mounted in it cannot be unmounted. The closed folder that was underneath an app's
    mount is opened again and removed when it is empty."""
    drives = (plan.data, plan.media, EXTRA_MOUNTS)  # a group's own drive holds such mounts too
    for folder in sorted(set(folders), key=len, reverse=True):
        if not any(folder.startswith(drive.rstrip('/') + '/') for drive in drives):
            continue  # not a folder of NekoServer's drives: left alone
        place = plan.at(folder)
        if os.path.ismount(str(place)):
            out('Unmounting ' + folder)
            code, said = call(['umount', folder], 120)
            if code != 0:
                out('  could not unmount (busy' + (': ' + said if said else '') + '): sudo umount ' + folder)
                left.append(folder + ' (still mounted)')
                continue
        if place.is_dir() and not os.path.ismount(str(place)):
            call(['chattr', '-i', str(place)], 30)  # closed by setup while its drive was away
            try:
                place.rmdir()
            except OSError:
                pass  # it holds files, or it goes with the drive's own folder: both are said there


def _unmount_extra(plan, call, out, left):
    """The drives beyond the two (an app's files or settings on a drive of their own):
    each unmounted and its empty mount folder removed, then the folder that held them."""
    top = plan.at(EXTRA_MOUNTS)
    if not top.is_dir():
        return
    for place in sorted(top.iterdir()):
        folder = EXTRA_MOUNTS + '/' + place.name
        if os.path.ismount(str(place)):
            out('Unmounting ' + folder)
            code, said = call(['umount', folder], 120)
            if code != 0:
                out('  could not unmount (busy' + (': ' + said if said else '') + '): sudo umount ' + folder)
                left.append(folder + ' (still mounted)')
                continue
        call(['chattr', '-i', str(plan.at(folder))], 30)  # closed by setup before the drive was mounted
        _remove_mount_folder(plan, folder, out, left)
    try:
        top.rmdir()
    except OSError:
        pass  # a drive is still mounted there, or a folder stayed: said above


def _remove_mounts(plan, path, call, out):
    """The drives' lines out of /etc/fstab, then each drive unmounted and its empty folder
    removed. Returns what stayed (a drive still in use, say)."""
    left = []
    block, inside = fstab_blocks(path.read_text()) if path.is_file() else ([], [])
    lines = plan.fstab_lines() + block
    if lines:
        kept = [line for line in path.read_text().splitlines() if line not in lines]
        backup = replace_system_file(path, '\n'.join(kept) + '\n')
        out(
            'Took '
            + str(len(lines))
            + ' line(s) out of '
            + FSTAB
            + ' (the file as it was is kept as '
            + backup.name
            + ')'
        )
    _unmount_inside(plan, inside, call, out, left)
    for folder in (plan.external, plan.media, plan.data):
        if not plan.mounted(folder):
            continue
        out('Unmounting ' + folder)
        code, said = call(['umount', folder], 120)
        if code != 0:
            out('  could not unmount (busy' + (': ' + said if said else '') + '): sudo umount ' + folder)
            left.append(folder + ' (still mounted)')
            continue
        _remove_mount_folder(plan, folder, out, left)
    # Last: the drives under EXTRA_MOUNTS, one of which may hold the two roles as folders.
    _unmount_extra(plan, call, out, left)
    return left


def _remove_mount_folder(plan, folder, out, left):
    """The mount folder of an unmounted drive, when it is empty. Files in it (written there
    while the drive was not mounted) stay, and so does a folder that cannot go: both said."""
    try:
        plan.at(folder).rmdir()
    except OSError as exc:
        if exc.errno == errno.ENOTEMPTY:
            out('  (' + folder + ' is not empty, so the folder stays: look inside before you delete it)')
            left.append(folder + ' (the mount folder: there are files in it)')
        else:
            out('  could not remove the folder ' + folder + ' (' + (exc.strerror or str(exc)) + ')')
            left.append(folder + ' (the mount folder: ' + (exc.strerror or str(exc)) + ')')


def not_removed(left):
    """The lines naming what the uninstall could not remove ([] when nothing is left)."""
    skipped = [path for path in left if isinstance(path, Skipped)]
    failed = [path for path in left if not isinstance(path, Skipped)]
    lines = []
    if failed:
        lines += [
            'These could not be removed or switched off (do it yourself, or run  sudo bash uninstall.sh  again):'
        ] + ['   - ' + path for path in failed]
    if skipped:
        lines += [SKIPPED_HEADING] + ['   - ' + path for path in skipped]
    return lines


SKIPPED_HEADING = (
    "These were skipped, because their drive's folder no longer holds the drive the settings name (another "
    'drive, or none, is mounted there now). Do not delete them by hand: what is there now is not '
    "NekoServer's drive. Mount the right drive there, then run  sudo bash uninstall.sh  again:"
)


LOCK_SUFFIX = '.lock'
# Held by a running setup (install.sh, then installer.py): one is enough to wait for.
SETUP_LOCKS = (str(SETUP_LOCK), '/run/nekoserver-install.lock')


LOCK_UNUSABLE = ', so it can\'t keep a setup from starting meanwhile.' + NOTHING_DELETED


def open_setup_lock(lock):
    """One setup lock file, opened (made when it is not there: no setup ran since the last
    start, and one started now must find it and wait). Made private to root (0600), never
    through a link. One already there is install.sh's (made readable by all, as bash
    makes files): it must be root's own plain file that only root can change; anything
    else under its name is refused."""
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as exc:  # a link under its name, say
        raise Stopped(f'{lock} could not be opened as a lock file ({exc.strerror})' + LOCK_UNUSABLE) from None
    found = os.fstat(fd)
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.geteuid() or found.st_mode & 0o022:
        os.close(fd)
        raise Stopped(f'{lock} is not a lock file of root\'s that only root can change' + LOCK_UNUSABLE)
    return fd


def hold_setup_locks(locks=SETUP_LOCKS):
    """The setup locks, held for the whole uninstall so that no setup starts meanwhile, or
    None when a setup is running now. A lock file that is not there is made and held too:
    otherwise a setup started meanwhile would make it and go ahead."""
    held = []
    try:
        for lock in locks:
            fd = open_setup_lock(lock)
            held.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        for fd in held:
            os.close(fd)
        return None
    except BaseException:
        for fd in held:
            os.close(fd)
        raise
    return held


def busy(locks=SETUP_LOCKS, journal=None):
    """Why the uninstall must wait, or None (and the setup locks it holds). A monthly
    update that is running or did not finish must finish first: stopping it part-way and
    then deleting its record would leave the apps half updated, with nothing that can
    finish or roll it back."""
    import neko_common

    blocked = neko_common.pending_maintenance(
        **({'journal': journal} if journal else {}), outcome='Nothing was removed.'
    )
    if blocked is None and neko_common.maintenance_running():
        blocked = (
            'A monthly update is running right now. Let it finish (sudo systemctl status nekoserver-maintenance.service '
            'shows "inactive" once it has), then run  sudo bash uninstall.sh  again. Nothing was removed.'
        )
    if blocked:
        return blocked, []
    try:
        held = hold_setup_locks(locks)
    except Stopped as exc:
        return str(exc), []
    if held is None:
        return (
            'NekoServer setup is running right now. Let it finish (or stop it), then run  sudo bash uninstall.sh  again. '
            'Nothing was removed.'
        ), []
    return None, held


def main(argv):
    dry = '--dry-run' in argv
    if not dry and os.geteuid() != 0:
        print('Run with sudo:  sudo bash uninstall.sh   (or  --dry-run  to only look)', file=sys.stderr)
        return 1
    blocked, _held = (None, []) if dry else busy()  # the locks stay held until the uninstall ends
    if blocked:
        print(blocked, file=sys.stderr)
        return 1
    settings, source = find_settings()
    if dry:
        return dry_run(settings, source)
    plan = Plan(settings)
    print('Settings: ' + source)
    print('This removes NekoServer from this PC. Nothing is formatted; what you keep stays as it is.')
    # With a desktop, the setup window asks the same questions and either place answers.
    with window_session(settings):
        return removal(plan)


NEEDS_SUDO = (
    'This account may not look at {where}, which NekoServer keeps private. '
    'Run it with sudo:  sudo bash uninstall.sh --dry-run   (still nothing is changed)'
)


def dry_run(settings, source, plan_of=None, out=None):
    """What each choice would remove; nothing is changed, and no password is needed. What
    this account may not read is left out and said so; should something still be out of
    reach, the person is told to run it with sudo, never shown a traceback (gate run B8)."""
    out = out or print
    out('Settings: ' + source)
    try:
        plan = (plan_of or Plan)(settings)
        said = plan.describe({key for key, *_ in CHOICES})
    except PermissionError as exc:
        out(NEEDS_SUDO.format(where=exc.filename or 'one of its folders'))
        return 1
    out('Dry run: what each choice would remove. Nothing is changed.')
    if os.geteuid() != 0:
        out(
            '(Not running as root: what this account may not look at is left out. '
            'For the whole plan:  sudo bash uninstall.sh --dry-run)'
        )
    out(said)
    out('')
    out(PACKAGES_LEFT)
    return 0


def window_session(settings):
    """The setup window for this menu too (setup_gui.session, since 0.50.0). Whatever
    keeps it from loading (a download with files missing, say) leaves the terminal
    alone, as before: removing NekoServer must work on a broken one."""
    import contextlib

    try:
        import setup_gui

        return setup_gui.session('Removing NekoServer', settings if isinstance(settings, dict) else {})
    except Exception:  # the window is a convenience here, never a reason to stop
        return contextlib.nullcontext()


def removal(plan):
    """The questions, the removal itself and what is left to say (main)."""
    try:
        chosen = choose(input, print)
        if not confirm(plan, chosen, input, print):
            print('Stopped. Nothing was changed.')
            return 2
        left = remove(plan, chosen, out=print)
    except Stopped as exc:
        print('\nStopped: ' + str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nStopped.')
        return 130
    print('')
    print(*not_removed(left), sep='\n', end='\n' if left else '')
    print(('Finished, but not everything went (see the list above). ' if left else 'Done. ') + PACKAGES_LEFT)
    for line in plan.kept():
        print(line + '.')
    if 'mounts' not in chosen:
        print('The drives stay mounted as before; take their lines out of /etc/fstab when you no longer want that.')
    print('This folder (' + str(ROOT) + ') is yours to delete.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
