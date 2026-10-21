"""Shared local filesystem safety; standard library only."""
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.request
from pathlib import Path

VERSION = '0.27.6'

TIMEZONE_PATTERN = re.compile(r'UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+')
ZONEINFO = Path('/usr/share/zoneinfo')


def host_timezone(config):
    """IANA zone used for container TZ and systemd timer schedules.

    Order: top-level ``timezone``, the legacy ``media.timezone`` and
    ``maintenance.timezone`` keys, then the host's /etc/localtime, then UTC.
    ``"auto"`` (the default in a new config) means "use this PC's timezone".
    """
    value = config.get('timezone')
    automatic = value == 'auto'
    if automatic:
        value = None
    for section in ('media', 'maintenance'):
        if value is None and not automatic:
            group = config.get(section, {})
            value = group.get('timezone') if isinstance(group, dict) else None
    if value is None:
        try:
            link = os.readlink('/etc/localtime')
            value = link.split('zoneinfo/', 1)[1] if 'zoneinfo/' in link else None
        except OSError:
            value = None
    if value is None:
        value = 'UTC'
    if not isinstance(value, str) or not TIMEZONE_PATTERN.fullmatch(value):
        raise RuntimeError('timezone must be an IANA zone such as Europe/London')
    if ZONEINFO.is_dir() and not (ZONEINFO / value).is_file():
        raise RuntimeError(f'timezone {value} is not installed under {ZONEINFO}')
    return value


# Every app NekoServer can install: (name, what it's for, local port). The one list
# the installer, checks and dashboard take names and ports from.
APPS = {
    'jellyfin': ('Jellyfin', 'Movies & shows', 8096),
    'immich': ('Immich', 'Photos & memories', 2283),
    'seerr': ('Seerr', 'Discover & request', 5055),
    'navidrome': ('Navidrome', 'Your music library', 4533),
    'sonarr': ('Sonarr', 'Series automation', 8989),
    'radarr': ('Radarr', 'Movie automation', 7878),
    'lidarr': ('Lidarr', 'Music automation', 8686),
    'prowlarr': ('Prowlarr', 'Media sources', 9696),
    'qbittorrent': ('qBittorrent', 'Download queue', 8080),
    'nextcloud': ('Nextcloud', 'Your personal files', 8088),
}
# The apps with their own setup checks (media services).
MEDIA_APPS = ('jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent')


def app_port(app):
    return APPS[app][2]


def app_ports(apps):
    """{app: local port} for the named apps."""
    return {app: app_port(app) for app in apps}


# Local ports already taken: every app, Immich's restore test (2284) and the private
# Tailscale addresses (8442-8448). The dashboard (8787 by default) must avoid them all.
APP_PORTS = frozenset({*(port for _, _, port in APPS.values()), 2284, *range(8442, 8449)})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Requests to the apps never follow a redirect (it could lead anywhere)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_opener(*handlers):
    """An opener for the apps on this computer: no proxy, no redirects."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), *handlers)


# Where NekoServer's systemd units live; every one it writes starts with this.
SYSTEMD_UNITS = Path('/etc/systemd/system')
MANAGED_UNIT = '[Unit]\nDescription=NekoServer '
# Docker waits for both drives (written by the docker step, checked by recovery).
DOCKER_STORAGE_DROPIN = SYSTEMD_UNITS / 'docker.service.d/10-nekoserver-storage.conf'


def check_units(names, what):
    """Refuse to replace a systemd unit NekoServer didn't write (checked before any
    change is made)."""
    for name in names:
        path = safe_path(SYSTEMD_UNITS / name)
        if path.exists() and not path.read_text().startswith(MANAGED_UNIT):
            raise RuntimeError(f'Unmanaged {what} unit preserved')


def write_units(units):
    for name, text in units.items():
        atomic_write(SYSTEMD_UNITS / name, text, 0o644)


MAINTENANCE_JOURNAL = Path('/etc/nekoserver-backups/maintenance-resume.json')


def pending_maintenance(journal=MAINTENANCE_JOURNAL, outcome='Nothing was changed.'):
    """Why setup must not run yet, or None. An unfinished monthly update must be finished
    (or rolled back) before anything else touches the apps: another version would
    replace the program that can finish it, and even the same version would start the
    apps, letting them write data that a pending rollback then replaces."""
    journal = Path(journal)
    if not journal.exists():
        return None
    try:
        started = json.loads(journal.read_text()).get('version')
    except (OSError, ValueError, AttributeError):
        started = None
    which = f'NekoServer v{started}' if isinstance(started, str) and started else 'an earlier NekoServer'
    return (f'A monthly update started by {which} did not finish, so some apps may still be stopped. '
            'Let it finish (or roll back) before running setup:\n'
            '  sudo systemctl start nekoserver-maintenance-resume.service\n'
            'When that has finished (sudo systemctl status nekoserver-maintenance-resume.service shows it '
            f'exited successfully), run  sudo bash install.sh  again. {outcome}')


def safe_path(path):
    path = Path(path)
    if '..' in path.parts:
        raise RuntimeError('Parent traversal is not allowed')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError(f'Refusing symlink path: {item}')
    return path


def regular_files(root):
    """(path, size) of every regular file under root; symlinks and unreadable entries skipped."""
    for folder, _dirs, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                yield path, info.st_size


def reading_max_age(interval):
    """How old the dashboard's readings may be before they count as late: two collection
    intervals plus 30 seconds for a slow run (150 s at the default 60 s). The page uses
    the same rule (statusStale in dashboard/app.js); `interval` comes from the snapshot."""
    if type(interval) is not int or not 30 <= interval <= 300:
        interval = 60
    return 2 * interval + 30


# exFAT has no owners or permissions (chmod is silently ignored): the mount options
# alone decide who can read the backup copies, so it is mounted for root only. Same
# options as the backup program (backup.rs usb_mount_options).
PRIVATE_EXFAT = 'uid=0,gid=0,fmask=0177,dmask=0077'


def usb_mount_options(fstype, writable=False):
    options = ('rw' if writable else 'ro') + ',nosuid,nodev,noexec'
    return options + ',' + PRIVATE_EXFAT if fstype == 'exfat' else options


def private_mount_options(options, fstype):
    """Are these (findmnt) mount options root-only? Always for filesystems with real
    permissions; exFAT needs uid/gid 0 and masks hiding everything from others."""
    if fstype != 'exfat':
        return True
    found = {key: value for key, _, value in (o.partition('=') for o in options.split(',') if '=' in o)}

    def hidden(key):
        try:
            return int(found[key], 8) & 0o077 == 0o077
        except (KeyError, ValueError):
            return False
    return found.get('uid', '0') == '0' and found.get('gid', '0') == '0' and hidden('fmask') and hidden('dmask')


LEDGER_NAME = '.neko-ledger.json'   # checksum ledger at the root of a backup tree
QUARANTINE = 'neko-damaged'         # damaged copies kept aside at the root; never data


def valid_ledger_path(rel):
    """A ledger path is plain and relative: no leading /, no '.', '..' or empty parts."""
    return (isinstance(rel, str) and rel and '\0' not in rel and not rel.startswith('/')
            and all(part not in ('', '.', '..') for part in rel.split('/')))


def read_ledger(root):
    """{relative path: sha256} recorded for a backup tree, or {} if it has no ledger yet.
    A ledger with any unsafe path or malformed entry is not trusted at all."""
    path = Path(root) / LEDGER_NAME
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f'The checksum ledger {path} is not a regular file')
    try:
        value = json.loads(path.read_text())
        entries = value['entries'] if value.get('format') == 'neko-ledger-v1' else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        entries = None
    if not isinstance(entries, dict):
        raise RuntimeError(f'The checksum ledger {path} is unreadable')
    out = {}
    for rel, row in entries.items():
        sha = row.get('sha256') if isinstance(row, dict) else None
        if not valid_ledger_path(rel) or not (isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha)):
            raise RuntimeError(f'The checksum ledger {path} has an invalid entry; it is not trusted')
        out[rel] = sha
    return out


def record_ledger(root, entries, how):
    """Add {relative path: sha256} to a tree's checksum ledger, with the same rules as the
    backup program: one writer at a time (its lock), an existing entry is never
    replaced, and the new ledger is written to `.neko-ledger.json.<uuid>.tmp`,
    fsync'd, renamed and its folder fsync'd. Returns the paths actually added."""
    import fcntl
    import time
    import uuid
    root = safe_path(Path(root))
    with open(root / '.neko-ledger.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A backup job is using this backup right now; try again when it has finished') from None
        path = root / LEDGER_NAME
        value = json.loads(path.read_text()) if path.exists() else {'format': 'neko-ledger-v1', 'entries': {}}
        read_ledger(root)  # refuse to extend a ledger that is not trusted
        added = []
        for rel, sha in entries.items():
            if not valid_ledger_path(rel) or not re.fullmatch(r'[0-9a-f]{64}', sha):
                raise RuntimeError('Refusing an invalid checksum ledger entry')
            if rel in value['entries']:
                continue
            value['entries'][rel] = {'sha256': sha, 'size': (root / rel).stat().st_size,
                                     'verified_at': time.time(), 'how': how}
            added.append(rel)
        if added:
            temp = root / f'{LEDGER_NAME}.{uuid.uuid4()}.tmp'
            try:
                with open(temp, 'x') as out:
                    os.fchmod(out.fileno(), 0o600)
                    json.dump(value, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, path)
                fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                if temp.exists():
                    temp.unlink()
        return added


def sha256_file(path):
    """SHA-256 of a file, read in full."""
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class UnreadableEntries(RuntimeError):
    """Some entries of a folder tree could not be listed or read; `problems` holds
    (path, why) for each. Raised after the whole tree was walked, so all are named."""

    def __init__(self, problems):
        self.problems = problems
        shown = '; '.join(f'{p}: {why}' for p, why in problems[:5])
        more = f' and {len(problems) - 5} more' if len(problems) > 5 else ''
        super().__init__(f'{len(problems)} entries could not be read: {shown}{more}')


def file_entry(path, problems):
    """(path, size) of a regular file; anything else is added to `problems`."""
    try:
        info = path.lstat()
    except OSError as error:
        problems.append((path, error.strerror))
        return None
    if stat.S_ISREG(info.st_mode):
        return path, info.st_size
    problems.append((path, 'symlink' if stat.S_ISLNK(info.st_mode) else 'not a regular file'))
    return None


def plain_folder(value):
    """A folder about to be walked, checked first (folder paths come from settings
    files): absolute, with no '..' and no symlinks along the way."""
    text = str(value)
    if '\x00' in text or '..' in Path(text).parts or not Path(text).is_absolute():
        raise RuntimeError('A folder to read must be a plain absolute path: ' + text)
    return safe_path(Path(text))


def strict_files(root, skip=None, skip_dirs=()):
    """Every regular file under `root` as (path, size), strictly: an unreadable folder or
    file, a symlink or a special file is an error, never silently left out. `skip(here,
    name)` names files that are not data (temporary or partial files); `skip_dirs` are
    folder names left out at the top level. Used by restores and media imports."""
    root = plain_folder(root)
    problems, out = [], []

    def unreadable(error):
        problems.append((error.filename, error.strerror))
    for folder, dirs, files in os.walk(root, onerror=unreadable):
        here = Path(folder)
        if here == root:
            dirs[:] = [d for d in dirs if d not in skip_dirs]
        problems.extend((here / d, 'symlink') for d in dirs if (here / d).is_symlink())
        wanted = (name for name in files if not (skip and skip(here, name)))
        out.extend(entry for entry in (file_entry(here / name, problems) for name in wanted) if entry)
    if problems:
        raise UnreadableEntries(problems)
    return out


def not_backup_data(here, root, name):
    """The tree's own ledger, notes and partial files: kept next to the data, not data."""
    return (name.startswith('.neko-') or name.endswith(('.neko-latest', '.neko-part'))
            or (here == root and name.startswith(LEDGER_NAME)))


def backup_files(root):
    """Every data file of a backup tree, strictly (see strict_files). The tree's own
    ledger, notes, partial files and its folder of damaged copies are not data."""
    root = Path(root)
    try:
        return strict_files(root, lambda here, name: not_backup_data(here, root, name), (QUARANTINE,))
    except UnreadableEntries as exc:
        raise RuntimeError('Some backup files could not be read; nothing was restored from a partial list: '
                           + str(exc).split(': ', 1)[1]) from None


def copy_checked(source, target, expected=None):
    """Copy one file into a new `target`: stream it while hashing, fsync, read it back
    from disk and hash again. Raises if the two disagree or, when `expected` (its
    recorded checksum) is given, if the backup copy no longer matches it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = hashlib.sha256()
    with open(source, 'rb') as src, open(target, 'xb') as out:
        os.fchmod(out.fileno(), 0o600)
        while chunk := src.read(1 << 20):
            stream.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    with open(target, 'rb') as back:
        landed = hashlib.file_digest(back, 'sha256').hexdigest()
    if landed != stream.hexdigest():
        raise RuntimeError(f'Restoring {source} failed: the copy written to disk does not match what was read')
    if expected is not None and landed != expected:
        raise RuntimeError(f'{source} is damaged on the backup (it no longer matches its recorded '
                           'checksum); the restore was stopped. Try the other backup (HDD or USB).')
    return landed


def settings_path(value):
    """A settings file given on the command line (--config), checked before it is read:
    a .json file name, made absolute, with no '..' and no symlinks along the way."""
    text = str(value)
    if '\x00' in text or '..' in Path(text).parts:
        raise RuntimeError('Settings file path must not contain ".."')
    path = Path(text).absolute()
    if path.suffix != '.json':
        raise RuntimeError('Settings file must be a .json file: ' + str(path))
    return safe_path(path)


def mount_path(value):
    path = safe_path(value)
    if (not path.is_absolute() or str(path) in ('/', '/mnt', '/srv', '/home', '/var', '/tmp')
            or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value))):
        raise RuntimeError(f'Unsafe mount path: {value}')
    return path


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) == '.' or not re.fullmatch(r'[A-Za-z0-9_./-]+', str(value)):
        raise RuntimeError(f'Unsafe relative path: {value}')
    return path


def atomic_write(path, payload, mode=0o600):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload.encode('utf-8') if isinstance(payload, str) else payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, payload):
    atomic_write(path, json.dumps(payload, indent=2) + '\n')


def publish_no_replace(temp, target):
    """Atomic publication; hard link on Linux FS, no-replace rename on exFAT.

    If the kernel/filesystem lacks either primitive, fail without overwriting.
    Never substitute ordinary rename(), which replaces existing files.
    """
    temp, target = safe_path(temp), safe_path(target)
    try:
        os.link(temp, target)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise RuntimeError('Filesystem needs Linux renameat2 for safe backup publication') from None
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temp), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
    else:
        temp.unlink()
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def ensure_directory(path, mode=0o755, uid=None, gid=None):
    """Create parents with explicit access, preserving existing ownership/modes.

    Only directories created by this call are changed. Never recursively chmod
    a populated tree to make a service appear healthy.
    """
    path = safe_path(path)
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise RuntimeError('Directory parent is not a directory')
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        if uid is not None:
            os.chown(directory, uid, gid)
        directory.chmod(mode)  # Apply after ownership changes, independent of umask.
    if not path.is_dir():
        raise RuntimeError('Expected directory')
    return path


def verify_storage(config, keys=('data_ssd', 'media_hdd')):
    """Fail closed unless dedicated mounts match saved UUID/filesystem identities."""
    import subprocess
    identities = []
    for key in keys:
        mount_key = 'data_mount' if key == 'data_ssd' else 'media_mount'
        path = mount_path(config[mount_key])
        expected = config['storage'][key]
        if not expected.get('uuid') or not expected.get('fstype'):
            raise RuntimeError('Storage identity is not configured')
        result = subprocess.run(
            ['findmnt', '-n', '-o', 'UUID,FSTYPE', '--mountpoint', str(path)],
            capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.split() != [expected['uuid'], expected['fstype']]:
            raise RuntimeError('Storage identity mismatch; refusing private data access')
        identities.append(expected['uuid'])
    if len(set(identities)) != len(identities):
        raise RuntimeError('Storage filesystems must be distinct')


def compose_image_override(manifest):
    """Extra Compose files next to a manifest, in order: the hardware video addition
    (graphics device, see modules/hardware_video.py), then maintenance's private
    immutable image pins. Both must be private and root-owned."""
    out = []
    for name in ('hardware.yaml', 'maintenance-images.json'):
        path = safe_path(Path(manifest).parent / name)
        if not path.exists():
            continue
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
            raise RuntimeError('Compose additions next to ' + str(manifest) + ' must be private and root-owned')
        out += ['-f', str(path)]
    return out


def arch_host(path='/etc/os-release'):
    """Arch Linux or an Arch-based system such as Nyarch (from /etc/os-release)."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return False
    values = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'").lower()
    return values.get('ID') in ('arch', 'nyarch') or 'arch' in values.get('ID_LIKE', '').split()


GIB = 1024 ** 3


def ensure_room(folder, need, what='This restore'):
    """Refuse a restore that would leave less than 5 GB (or 5% of the drive) free where
    it goes, so a big restore never fills a drive or stops half-way. Shared by every
    restore: photos, Nextcloud files, nightly snapshots and monthly snapshots."""
    import shutil
    usage = shutil.disk_usage(folder)
    reserve = max(5 * GIB, usage.total // 20)
    if need + reserve > usage.free:
        raise RuntimeError(f'not enough free space: {what.lower()} needs {need / GIB:.1f} GB and '
                           f'{usage.free / GIB:.1f} GB is free on that drive (5 GB or 5% is kept free). '
                           'Free some space or choose a folder on another drive.')
