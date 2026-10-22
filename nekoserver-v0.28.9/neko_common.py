"""Shared local filesystem safety; standard library only."""
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.request
from pathlib import Path

VERSION = '0.28.9'

# "Europe/London", and the one-word zones zoneinfo also ships ("Japan", "CET", "UTC").
TIMEZONE_PATTERN = re.compile(r'[A-Za-z_][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*')
ZONEINFO = Path('/usr/share/zoneinfo')
ETC_TIMEZONE = Path('/etc/timezone')


def local_timezone(localtime='/etc/localtime', etc_timezone=ETC_TIMEZONE, zoneinfo=ZONEINFO):
    """This PC's timezone: from the /etc/localtime link, else /etc/timezone (Debian and
    Ubuntu keep it there, and cloud images often copy the file instead of linking it),
    else None."""
    try:
        link = os.readlink(localtime)
        if 'zoneinfo/' in link:
            return link.split('zoneinfo/', 1)[1]
    except OSError:
        pass
    try:
        name = Path(etc_timezone).read_text().strip()
    except OSError:
        return None
    return name if name and TIMEZONE_PATTERN.fullmatch(name) and (Path(zoneinfo) / name).is_file() else None


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
        value = local_timezone()
    if value is None:
        # Not knowable: night-time jobs then run at UTC times (the setup log says so).
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
DASHBOARD_DEFAULT_PORT = 8787


def dashboard_port(config):
    """The dashboard's local port as configured, or None when the setting isn't a usable port."""
    port = config.get('dashboard', {}).get('port', DASHBOARD_DEFAULT_PORT)
    return port if type(port) is int and 1024 <= port <= 65535 else None


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


def systemd_arg(value):
    """One argument for a systemd unit line, quoted so that spaces, quotes, `$` and `%`
    stay part of it ("Neko Server/recover.py" is one argument, not two)."""
    text=str(value)
    if any(c in text for c in '\n\r\0'):raise RuntimeError('This path cannot be used in a system service: '+repr(text))
    return '"'+text.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


# Long file names in backups. The rules match backup.rs (fitted, version_parts,
# latest_marker, partial_name), which writes them.
NAME_MAX = 255                       # longest name ext4 and exFAT accept, in bytes
VERSION_TAG = len('.neko-20261022T104800Z-9999')   # room kept for a version's date
NAMES_FILE = '.neko-names.json'      # full names behind shortened version names


def fitted(text, room, whole):
    """`text` when it fits in `room` bytes; otherwise its start (cut between characters)
    and `~` plus 8 hex of the SHA-256 of `whole`, so long names stay distinct."""
    data = text.encode()
    if len(data) <= room:
        return text
    tag = '~' + hashlib.sha256(whole.encode()).hexdigest()[:8]
    return data[:max(0, room - len(tag))].decode('utf-8', 'ignore') + tag


def version_parts(name):
    """(stem, extension) of a file's dated versions: name.ext -> ('name', '.ext')."""
    stem, dot, ext = name.rpartition('.')
    stem, ext = (stem, dot + ext) if stem else (name, '')
    if len(stem.encode()) + VERSION_TAG + len(ext.encode()) <= NAME_MAX:
        return stem, ext
    if len(ext.encode()) + VERSION_TAG + 24 > NAME_MAX:
        stem, ext = name, ''
    return fitted(stem, NAME_MAX - VERSION_TAG - len(ext.encode()), name), ext


LATEST_SUFFIX = '.neko-latest'     # the note naming a file's current saved copy


def latest_note(name):
    """The name of the note saying which saved copy of `name` is current."""
    return '.' + fitted(name, NAME_MAX - len('.') - len(LATEST_SUFFIX), name) + LATEST_SUFFIX


def partial_name(name):
    """A short name of its own for a copy in progress, whatever the file's length."""
    return '.neko-' + hashlib.sha256(name.encode()).hexdigest()[:16] + '.neko-part'


def saved_names(folder):
    """{shortened version stem: the file's full name} kept in a backup folder."""
    try:
        value = json.loads((Path(folder) / NAMES_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    names = value.get('names') if isinstance(value, dict) and value.get('format') == 'neko-names-v1' else None
    return {k: v for k, v in names.items() if isinstance(k, str) and isinstance(v, str) and '/' not in v} if isinstance(names, dict) else {}


def saved_versions(folder):
    """{dated copy: the file it is a version of}, as the backup recorded them (since
    0.28.7) in the folder's names file."""
    try:
        value = json.loads((Path(folder) / NAMES_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    versions = value.get('versions') if isinstance(value, dict) and value.get('format') == 'neko-names-v1' else None
    return {k: v for k, v in versions.items() if isinstance(k, str) and isinstance(v, str) and '/' not in v and '/' not in k} \
        if isinstance(versions, dict) else {}


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


def record_ledger(root, entries, how, replace=()):
    """Add {relative path: sha256} to a tree's checksum ledger, with the same rules as the
    backup program: one writer at a time (its lock), an existing entry is never
    replaced, and the new ledger is written to `.neko-ledger.json.<uuid>.tmp`,
    fsync'd, renamed and its folder fsync'd. Returns the paths actually added.
    `replace`: paths whose entry you settled yourself when two ledgers disagreed (the
    recovery wizard asks first); only those are overwritten."""
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
            if rel in value['entries'] and rel not in replace:
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
    return (name.startswith('.neko-') or name.endswith((LATEST_SUFFIX, '.neko-part'))
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


# NekoServer's services run with systemd's ProtectHome, which hides these from them, so
# a drive mounted under one would be invisible to the dashboard, the gateway and the
# alert services. Refused before anything is installed.
HIDDEN_FROM_SERVICES = ('/home', '/root', '/run/user')


def mount_path(value):
    path = safe_path(value)
    if (not path.is_absolute() or str(path) in ('/', '/mnt', '/srv', '/home', '/var', '/tmp')
            or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value))):
        raise RuntimeError(f'Unsafe mount path: {value}')
    if any(path == Path(top) or Path(top) in path.parents for top in HIDDEN_FROM_SERVICES):
        raise RuntimeError(f'Drives cannot be mounted under /home, /root or /run/user ({value}): NekoServer\'s services '
                           'cannot see those folders. Mount it under /srv or /mnt instead, for example /srv/nekoserver')
    return path


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) == '.' or not re.fullmatch(r'[A-Za-z0-9_./-]+', str(value)):
        raise RuntimeError(f'Unsafe relative path: {value}')
    return path


def checked_run(argv, timeout, what=None):
    """Run a command that must succeed. A failure raises RuntimeError with the command's
    own last words, so the setup log says why (not just "exit status 1")."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        said = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(f'{what or argv[0]} failed: {said[-500:] or "no details"}')
    return result


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


def refresh_settings(target, wanted, log, label):
    """Bring a scheduled job's saved settings up to date after a setup run (an app added
    or removed, say). Only a job that is installed has them; True when they changed."""
    if not Path(target).is_file():
        return False
    try:
        current = json.loads(Path(target).read_text())
    except (OSError, ValueError):
        current = None
    if current == wanted:
        return False
    atomic_json(target, wanted)
    log(f'{label}: settings brought up to date')
    return True


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
            raise OSError(code, os.strerror(code), str(target)) from exc
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
            capture_output=True, text=True, timeout=10, check=False)
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
