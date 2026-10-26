"""Shared local filesystem safety; standard library only."""
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = '0.40.2'

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
# How long a snapshot's recovery (its resume service, and its ExecStopPost) may take: the
# 3-hour wait for the shared recovery lock while another job holds it (backup.rs
# coordinate), plus the recovery itself. Without a journal to process it returns at once.
RESUME_TIMEOUT = '3h30min'
# Docker waits for both drives (written by the docker step, checked by recovery).
DOCKER_STORAGE_DROPIN = SYSTEMD_UNITS / 'docker.service.d/10-nekoserver-storage.conf'


def check_units(names, what):
    """Refuse to replace a systemd unit NekoServer didn't write (checked before any
    change is made)."""
    for name in names:
        path = safe_path(SYSTEMD_UNITS / name)
        if path.exists() and not path.read_text().startswith(MANAGED_UNIT):
            raise RuntimeError(f'Unmanaged {what} unit preserved')


# systemd's record of when a persistent timer (Persistent=true) last ran. An uninstaller
# before 0.40.0 left NekoServer's there; a timer created again later with the same name
# then counted every night since as missed and ran its job as soon as it was switched on.
TIMER_STAMPS = Path('/var/lib/systemd/timers')


def forget_last_run(unit):
    """Before a NekoServer timer's unit file is first written: systemd's left-over record
    of its last run goes, so the new timer waits for its first scheduled time. A timer that
    is already there keeps its record (a night the PC was off is still caught up)."""
    unit = Path(unit)
    if unit.suffix != '.timer' or not unit.name.startswith('nekoserver-') or unit.exists():
        return
    (TIMER_STAMPS / ('stamp-' + unit.name)).unlink(missing_ok=True)


def write_units(units):
    for name, text in units.items():
        forget_last_run(SYSTEMD_UNITS / name)
        atomic_write(SYSTEMD_UNITS / name, text, 0o644)


MAINTENANCE_JOURNAL = Path('/etc/nekoserver-backups/maintenance-resume.json')


MAINTENANCE_UNITS = ('nekoserver-maintenance.service', 'nekoserver-maintenance-resume.service')


def maintenance_running(run=subprocess.run):
    """The monthly update, or the service that finishes a cut-off one, is working right
    now (both are oneshot services: "activating" while they run)."""
    try:
        result = run(['systemctl', 'is-active', *MAINTENANCE_UNITS], capture_output=True, text=True,
                     timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool({'activating', 'deactivating'} & set(result.stdout.split()))


def pending_maintenance(journal=MAINTENANCE_JOURNAL, outcome='Nothing was changed.', running=None):
    """Why setup must not run yet, or None. An unfinished monthly update must be finished
    (or rolled back) before anything else touches the apps: another version would
    replace the program that can finish it, and even the same version would start the
    apps, letting them write data that a pending rollback then replaces."""
    running = running or maintenance_running   # looked up now, so a test's stand-in is used
    journal = Path(journal)
    if not journal.exists():
        return None
    try:
        started = json.loads(journal.read_text()).get('version')
    except (OSError, ValueError, AttributeError):
        started = None
    which = f'NekoServer v{started}' if isinstance(started, str) and started else 'an earlier NekoServer'
    if running():
        return (f'A monthly update started by {which} is running right now (it can take an hour or more). '
                'Let it finish: sudo systemctl status nekoserver-maintenance.service shows "inactive" once it '
                f'has. Then run  sudo bash install.sh  again. {outcome}')
    return (f'A monthly update started by {which} did not finish, so some apps may still be stopped. '
            'Let it finish (or roll back) before running setup:\n'
            '  sudo systemctl start nekoserver-maintenance-resume.service\n'
            'When that has finished (sudo systemctl status nekoserver-maintenance-resume.service shows it '
            f'exited successfully), run  sudo bash install.sh  again. {outcome}\n'
            'If it keeps failing, this release can finish it instead (it changes nothing else):\n'
            '  sudo bash install.sh --finish-update')


def pick_drive(options, label, input_fn=input, out_fn=print, skippable=False, attempts=3):
    """The drive chosen by its number, then confirmed by typing its name. A typo is asked
    again. ENTER (or Ctrl+D) stops: None when the choice may be left for later
    (`skippable`), otherwise "Selection aborted"."""
    for n, v in enumerate(options, 1):
        out_fn(f"  {n}) /dev/{v['name']} {v.get('size', '')} {v['fstype']} UUID={v['uuid']} "
               f"mounted={v.get('mountpoint') or '-'}")
    question = f'Choose {label} by number (Enter to skip for now): ' if skippable else 'Choose number (or ENTER to abort): '
    for _ in range(attempts):
        try:
            choice = input_fn(question).strip()
        except EOFError:
            choice = ''
        if not choice and skippable:
            return None
        if not choice:
            raise RuntimeError('Selection aborted')
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            break
        out_fn(f'Type a number from 1 to {len(options)}, or press Enter to stop.')
    else:
        raise RuntimeError('Selection aborted')
    return confirmed(options[int(choice) - 1], label, input_fn, out_fn, attempts)


def confirmed(device, label, input_fn, out_fn, attempts=3):
    """The drive, once its name is typed back (a typo is asked again)."""
    for _ in range(attempts):
        if input_fn(f"Type {device['name']} to confirm it is {label}: ").strip() in (
                device['name'], '/dev/' + device['name'], device['uuid']):
            return device
        out_fn(f"That is not {device['name']}. Type {device['name']} exactly as shown, to be sure of the drive.")
    raise RuntimeError('Drive confirmation failed')


def safe_path(path):
    path = Path(path)
    if '..' in path.parts:
        raise RuntimeError('Parent traversal is not allowed')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError(f'Refusing symlink path: {item}')
    return path


# Where Debian and Ubuntu put games (Steam among them), which root's PATH leaves out.
GAMES_DIRS = ('/usr/games', '/usr/local/games')


def find_program(name, which=shutil.which):
    """The full path of a program on PATH or in the games folders, or None. Its folder is
    the real one (/usr/bin, not root's /usr/sbin where that is a link to it), so a
    shortcut names the program as the system does."""
    found = which(name) or next((found for found in (which(name, path=folder) for folder in GAMES_DIRS) if found), None)
    if not found:
        return None
    found = Path(found)
    return str(found.parent.resolve() / found.name)


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
    """A short name of its own for a copy in progress, whatever the file's length. The
    name's bytes as the drive has them (os.fsencode): an old file name that is not UTF-8
    (Latin-1 `Caf\\xe9.flac`, say) works too, and a UTF-8 name gives the same as before."""
    return '.neko-' + hashlib.sha256(os.fsencode(name)).hexdigest()[:16] + '.neko-part'


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


def record_ledger(root, entries, how, replace=(), retire=None):
    """Add {relative path: sha256} to a tree's checksum ledger, with the same rules as the
    backup program: one writer at a time (its lock), an existing entry is never
    replaced, and the new ledger is written to `.neko-ledger.json.<uuid>.tmp`,
    fsync'd, renamed and its folder fsync'd. Returns the paths actually added.
    `replace`: paths whose entry you settled yourself when two ledgers disagreed (the
    recovery wizard asks first); only those are overwritten. `retire`: {path: why} for
    saved copies you accepted as lost (the wizard asks first): each entry moves to
    "retired" with when and why, so the checks stop expecting it (backup's ledger.rs
    keeps that part as it is)."""
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
        added += retire_entries(value, retire or {})
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


def retire_entries(value, retire):
    """Move each of `retire`'s paths ({path: why}) from a ledger's entries to its
    "retired" part, with when and why. Returns the paths moved."""
    import time
    moved = []
    for rel, why in retire.items():
        old = value['entries'].pop(rel, None) if valid_ledger_path(rel) else None
        if old is not None:
            value.setdefault('retired', {})[rel] = {**old, 'retired_at': time.time(), 'why': why}
            moved.append(rel)
    return moved


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


# The system's own folders. A folder NekoServer walks (to restore, import, copy or size
# it) comes from a settings file or a backup's own record, so a changed record must not
# be able to point a restore or an import at them. Drives and folders anywhere else
# (/srv, /mnt, /data, /home, a desktop drive under /media or /run/media) are fine.
SYSTEM_FOLDERS = ('/bin', '/boot', '/dev', '/etc', '/lib', '/lib32', '/lib64', '/proc', '/root', '/run', '/sbin',
                  '/sys', '/usr')
DESKTOP_DRIVES = '/run/media'


def inside(path, folder):
    """True when `path` (already resolved) is `folder` or somewhere under it."""
    return os.path.commonpath([path, folder]) == folder


def plain_folder(value):
    """A folder about to be walked, checked first (folder paths come from settings
    files): absolute, with no '..', no symlinks along the way, and not one of the
    system's own folders."""
    text = str(value)
    if '\x00' in text or '..' in Path(text).parts or not Path(text).is_absolute():
        raise RuntimeError('A folder to read must be a plain absolute path: ' + text)
    real = os.path.realpath(text)
    if real == '/' or (any(inside(real, system) for system in SYSTEM_FOLDERS) and not inside(real, DESKTOP_DRIVES)):
        raise RuntimeError('NekoServer does not read the system\'s own folders: ' + text)
    safe_path(Path(text))   # no symlinks along the way, so the resolved path is the same folder
    return Path(real)


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


LEDGER_LOCK = '.neko-ledger.lock'
ATOMIC_TEMP = re.compile(r'\.neko-\d+-\d+-\d+')   # the backup program's atomic-write temporaries


def not_backup_data(here, root, name):
    """The tree's own ledger (with its lock, temporaries and an unreadable one set aside)
    at the root, and in any folder the version names, current-version notes, partial
    copies and atomic-write temporaries: kept next to the data, not data. Only these
    exact names and patterns: a user's own `.neko-notes.txt` is data (backup.rs
    metadata_name and ledger_file say the same)."""
    if here == root and (name in (LEDGER_NAME, LEDGER_LOCK) or name.startswith(LEDGER_NAME + '.')):
        return True
    return (name == NAMES_FILE or name.endswith((LATEST_SUFFIX, '.neko-part'))
            or ATOMIC_TEMP.fullmatch(name) is not None)


def backup_files(root, tree_root=None):
    """Every data file under `root`, a folder of a backup tree, strictly (see
    strict_files). Notes, partial files and, at the tree's own root (`tree_root`, where
    its ledger is), the ledger and the folder of damaged copies are not data. A restore
    reads folders below the tree's root (a user's files, a photo section): there a
    folder named neko-damaged or a file named like the ledger is someone's own (the
    backup program draws the same line)."""
    root = Path(root)
    at_root = tree_root is not None and Path(tree_root) == root
    try:
        return strict_files(root, lambda here, name: not_backup_data(here, root if at_root else None, name),
                            (QUARANTINE,) if at_root else ())
    except UnreadableEntries as exc:
        raise RuntimeError('Some backup files could not be read; nothing was restored from a partial list: '
                           + str(exc).split(': ', 1)[1]) from None


class BackupDamaged(RuntimeError):
    """A backup copy no longer matches its recorded checksum (the copy is at fault)."""


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
        # Written to the drive: dropped from memory, so the read below comes from the
        # drive itself (from memory, damage on the drive would never be seen).
        os.posix_fadvise(out.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    with open(target, 'rb') as back:
        os.posix_fadvise(back.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        landed = hashlib.file_digest(back, 'sha256').hexdigest()
    if landed != stream.hexdigest():
        raise RuntimeError(f'Restoring {source} failed: the copy written to disk does not match what was read')
    if expected is not None and landed != expected:
        raise BackupDamaged(f'{source} is damaged on the backup (it no longer matches its recorded checksum)')
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


# The settings a command uses when no --config is given (default_settings).
MY_SETTINGS = 'my-config.json'      # made by the wizard; git does not track it
SETTINGS_TEMPLATE = 'config.json'   # the template in the release folder (no drives in it)
BUNDLE_MANIFEST = 'release-manifest.json'   # beside the copy kept on the SSD (modules/recovery.MANIFEST)
NO_SETTINGS = '<no settings file found>'
NO_SETTINGS_HELP = ('config.json in this folder is the settings template (no drives in it), not your settings. '
                    'Run  sudo bash install.sh  first (it makes my-config.json), or name your settings file with '
                    '--config (for example --config my-config.json)')


def default_settings(folder):
    """The settings file a command in `folder` uses without --config: my-config.json when
    the wizard made it; config.json in the copy kept on the SSD (installer/releases/v...,
    where it is your settings, with release-manifest.json beside it); otherwise
    NO_SETTINGS, which checked_settings refuses, because there config.json is only the
    template. An argparse default: it is only checked when --config is not given."""
    folder = Path(folder)
    if (folder / MY_SETTINGS).is_file():
        return str(folder / MY_SETTINGS)
    if (folder / BUNDLE_MANIFEST).is_file() and (folder / SETTINGS_TEMPLATE).is_file():
        return str(folder / SETTINGS_TEMPLATE)
    return NO_SETTINGS


def checked_settings(value):
    """argparse type for --config: the value as given, but never the template stand-in
    (no settings found next to the command)."""
    import argparse
    if value == NO_SETTINGS:
        raise argparse.ArgumentTypeError(NO_SETTINGS_HELP)
    return value


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


def fetch(url, what, limit, timeout=30, opener=None, headers=None):
    """A download setup needs (an app's release list or compose file), or a plain reason
    it failed: no internet, a page that is gone, GitHub's request limit."""
    request = urllib.request.Request(url, headers={'User-Agent': 'NekoServer-installer', **(headers or {})})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            return response.read(limit)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429) and 'api.github.com' in url:
            raise RuntimeError(f'{what}: GitHub is limiting requests from this address right now (HTTP {exc.code}); '
                               'wait an hour and run setup again') from None
        raise RuntimeError(f'{what}: {url} answered HTTP {exc.code}; try again later') from None
    except OSError as exc:
        reason = str(getattr(exc, 'reason', exc))
        # A clock set wrong makes every certificate look not valid yet, or expired.
        if re.search(r'not yet valid|certificate has expired', reason, re.IGNORECASE):
            raise RuntimeError(f'{what}: could not download {url} ({reason}). This PC\'s clock looks wrong (check '
                               'the date and time with  timedatectl): let the network set it (sudo timedatectl '
                               'set-ntp true), wait a minute, then run setup again') from None
        raise RuntimeError(f'{what}: could not download {url} ({reason}). Check that this PC '
                           'is connected to the internet, then run setup again') from None


# Images are downloaded before the apps are started: a slow connection then has hours,
# not the minutes a start may take, and a cut-off download goes on where it stopped.
PULL_SECONDS = 4 * 3600


def compose_up(base, what, log=None, timeout=900):
    """Download the images (any amount of time a slow connection needs; a download cut off
    by Ctrl+C goes on where it stopped next time) and then start the app."""
    services = compose_services(base, what)
    taken = busy_ports(base, services, what)
    if taken:
        # Docker would say only "failed to bind host port ...: address already in use".
        raise RuntimeError(f'{what} cannot start: ' + '; '.join(
            f'port {port} ({ip}) is in use by {holder}' for port, ip, holder in taken)
            + f'. {what} needs it: stop that program and keep it from starting with the PC (for a '
            'service, sudo systemctl disable --now its name), then run setup again')
    named = services_to_pull(base, what, services)
    if named:
        if log:
            log(f'{what}: downloading its images (this can take a while on a slow connection)')
        pull_images([*base, 'pull', '--quiet', *named], what, log)
    result = checked_run([*base, 'up', '-d'], timeout, f'{what} (docker compose up)')
    lost = without_network(base, what)
    if lost:
        if log:
            log(f'{what}: {", ".join(lost)} was running without its network (left so by an earlier start that '
                'failed); recreating it')
        result = checked_run([*base, 'up', '-d', '--force-recreate', '--no-deps', *lost], timeout,
                             f'{what} (docker compose up)')
    return result


def compose_services(base, what):
    """The app's services as docker compose resolves them."""
    shown = checked_run([*base, 'config', '--format', 'json'], 120, f'{what} (docker compose config)')
    try:
        return json.loads(shown.stdout or '{}').get('services') or {}
    except ValueError:
        raise RuntimeError(f'{what}: docker compose config gave no readable answer') from None


def port_taken(ip, port, protocol='tcp'):
    """Binding this address as Docker would fails: another program listens there."""
    kind = socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM
    with socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET, kind) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # a closing connection is no holder
        try:
            probe.bind((ip, port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def port_holder(port, protocol='tcp', docker=('docker',)):
    """Who listens on this port, for a person to find it: a program and its process, or
    the Docker container publishing it."""
    try:
        shown = subprocess.run(['ss', '-Hlunp' if protocol == 'udp' else '-Hltnp', f'sport = :{port}'],
                               capture_output=True, text=True, timeout=15, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        shown = ''
    found = re.search(r'users:\(\("([^"]+)",pid=(\d+)', shown)
    if not found:
        return 'another program'
    if found.group(1) == 'docker-proxy':
        try:
            names = subprocess.run([*docker, 'ps', '--filter', f'publish={port}', '--format', '{{.Names}}'],
                                   capture_output=True, text=True, timeout=30, check=False).stdout.split()
        except (OSError, subprocess.SubprocessError):
            names = []
        return 'the Docker container ' + ', '.join(names) if names else 'another Docker container'
    return f'{found.group(1)} (process {found.group(2)})'


def compose_rows(base, what):
    """The app's containers as docker compose ps lists them (a dict each)."""
    shown = checked_run([*base, 'ps', '--format', 'json'], 60, f'{what} (docker compose ps)').stdout or ''
    try:
        rows = json.loads(shown)                  # an older Compose: one JSON list
        rows = rows if isinstance(rows, list) else [rows]
    except ValueError:                            # a newer one: a JSON object per line
        rows = [json.loads(line) for line in shown.splitlines() if line.strip().startswith('{')]
    return [row for row in rows if isinstance(row, dict)]


def without_network(base, what):
    """Services whose running container has no network at all. A start that failed while
    Docker set up its networking (a port in use, say) left the container that way, and
    Docker 29 went on starting it without one: the app runs, but nothing can reach it
    (tried on AlmaLinux). No NekoServer app runs without a network."""
    docker = list(base[:base.index('compose')]) if 'compose' in base else ['docker']
    lost = []
    for row in compose_rows(base, what):
        if row.get('State') != 'running' or not (row.get('ID') or row.get('Name')):
            continue
        try:
            shown = subprocess.run([*docker, 'inspect', '--format', '{{len .NetworkSettings.Networks}}',
                                    row.get('ID') or row.get('Name')], capture_output=True, text=True,
                                   timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.returncode == 0 and shown.stdout.strip() == '0':
            lost.append(row.get('Service'))
    return sorted(name for name in lost if name)


def busy_ports(base, services, what):
    """[(port, address, holder)] for the ports these services publish that another program
    holds now. The app's own running containers hold theirs: those are left out."""
    wanted = [(name, p) for name, service in sorted(services.items())
              for p in (service.get('ports') or []) if isinstance(p, dict) and p.get('published')]
    if not wanted:
        return []
    running = {row.get('Service') for row in compose_rows(base, what) if row.get('State') == 'running'}
    docker = tuple(base[:base.index('compose')]) if 'compose' in base else ('docker',)
    taken = []
    for name, p in wanted:
        ip, port, protocol = p.get('host_ip') or '0.0.0.0', int(p['published']), p.get('protocol') or 'tcp'
        if name not in running and port_taken(ip, port, protocol):
            taken.append((port, ip, port_holder(port, protocol, docker)))
    return taken


def services_to_pull(base, what, services=None):
    """The services whose image is named (lscr.io/...:latest, say) and so can be
    downloaded. After a monthly update, maintenance pins each service to the exact image
    it tested (sha256:...): such an image cannot be downloaded by its ID ("pull access
    denied for sha256"), and must already be on this PC, which is checked instead."""
    if services is None:
        services = compose_services(base, what)
    docker = list(base[:base.index('compose')]) if 'compose' in base else ['docker']
    named = []
    for name, service in sorted(services.items()):
        image = str(service.get('image') or '')
        if image.startswith('sha256:'):
            present = subprocess.run([*docker, 'image', 'inspect', image], capture_output=True, text=True, timeout=60, check=False)
            if present.returncode:
                raise RuntimeError(f'{what}: the image the monthly update pinned for {name} ({image[:19]}...) is not on this PC '
                                   'any more. Run the monthly update again (sudo systemctl start nekoserver-maintenance.service), '
                                   'or take the pin out of maintenance-images.json next to its compose.yaml')
        else:
            named.append(name)
    return named


# A registry that is limiting requests ("toomanyrequests"), or a connection that drops
# part-way, answers like this. Each is worth waiting for and trying again.
TRANSIENT_PULL = ('toomanyrequests', 'too many requests', 'tls handshake timeout', 'unexpected eof',
                  'i/o timeout', 'connection reset', 'temporary failure', 'service unavailable', '503')
PULL_RETRY_WAITS = (30, 60, 120, 240, 480)


def pull_images(argv, what, log=None, waits=PULL_RETRY_WAITS, sleep=time.sleep):
    """docker compose pull, tried again after a wait when the registry is limiting
    requests or the connection dropped (several apps pull at setup, from the same
    registries). Other failures, and one that keeps failing, raise as before."""
    for attempt, wait in enumerate((*waits, None)):
        try:
            return checked_run(argv, PULL_SECONDS, f'{what} (docker compose pull)')
        except RuntimeError as exc:
            said = str(exc).lower()
            if wait is None or not any(mark in said for mark in TRANSIENT_PULL):
                raise
            if log:
                reason = next((line.strip() for line in reversed(str(exc).splitlines()) if line.strip()), '')
                log(f'{what}: the download was refused for now (the registry is busy or the '
                    f'connection dropped: {reason[-160:]}); trying again in {wait} seconds ({attempt + 1} of {len(waits)})')
            sleep(wait)


def findmnt(columns, target, by='--mountpoint', run=None, timeout=10):
    """findmnt's columns for the mount in effect at target: with mounts stacked on one
    folder it lists them oldest first, and the last one is the one in use. None when
    nothing is mounted there (or findmnt can't say)."""
    try:
        result = (run or subprocess.run)(['findmnt', '-n', '-o', columns, by, str(target)], capture_output=True,
                                         text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (result.stdout or '').splitlines() if line.strip()]
    return lines[-1] if result.returncode == 0 and lines else None


def checked_run(argv, timeout, what=None):
    """Run a command that must succeed. A failure raises RuntimeError with the command's
    own last words, so the setup log says why (not just "exit status 1")."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f'{what or argv[0]} failed: {last_words(result.stderr or result.stdout)}')
    return result


def last_words(output, limit=500):
    """The last lines of a command's output, up to about `limit` characters, cut only at
    a line boundary (a cut mid-line gave messages like "rror toomanyrequests")."""
    lines = [line.rstrip() for line in (output or '').strip().splitlines() if line.strip()]
    kept = []
    for line in reversed(lines):
        if kept and sum(len(k) + 1 for k in kept) + len(line) > limit:
            break
        kept.insert(0, line[-limit:])
    return '\n'.join(kept) or 'no details'


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


def rename_no_replace(source, target):
    """Move `source` (a file or a folder) to `target` on the same drive, never over
    something already there (FileExistsError): unlike os.rename, which replaces an empty
    folder or a file at `target` if one appeared since it was checked."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise RuntimeError('Linux renameat2 is needed to move folders safely')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:   # RENAME_NOREPLACE
        code = ctypes.get_errno()
        raise (FileExistsError if code == errno.EEXIST else OSError)(code, os.strerror(code), str(target))


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


def made_whole(directory, mode, uid, gid):
    """Make one folder that appears with its owner and mode already set: made under a
    temporary name beside it, then moved into place. Cut off between making and
    chowning it (Ctrl+C, a power cut), a folder made in place would stay root's for
    good, and the app or desktop user it was meant for could never write to it."""
    temp = directory.with_name('.' + directory.name + '.neko-new')
    if temp.is_dir() and not temp.is_symlink():
        try:
            temp.rmdir()          # left by a run cut off here; empty, as it was made
        except OSError:
            pass
    temp.mkdir(mode=mode)
    if uid is not None:
        os.chown(temp, uid, gid)
    temp.chmod(mode)  # Apply after ownership changes, independent of umask.
    try:
        rename_no_replace(temp, directory)
    except FileExistsError:
        temp.rmdir()              # made meanwhile by something else: that one stays
    except (OSError, RuntimeError):
        # A filesystem without no-replace renames (FAT, say): made in place as before.
        temp.rmdir()
        directory.mkdir(mode=mode)
        if uid is not None:
            os.chown(directory, uid, gid)
        directory.chmod(mode)


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
        made_whole(directory, mode, uid, gid)
    if not path.is_dir():
        raise RuntimeError('Expected directory')
    return path


def mounted_identity(path):
    """(UUID, filesystem type, tree) of the mount in effect at path, or None. The tree is
    the Btrfs subvolume ("/@data"), or the folder of a drive mounted from a folder, as
    findmnt shows it in brackets (/dev/sdb1[/@data]); '' for the whole filesystem. The
    same reading as the runtime's drive check (backup.rs guard)."""
    fields = (findmnt('UUID,FSTYPE,SOURCE', path) or '').split()
    if len(fields) < 3:
        return None
    tree = fields[2].split('[', 1)[1].rstrip(']').rstrip('/') if '[' in fields[2] else ''
    return fields[0], fields[1], tree


def identity_matches(path, expected):
    """The drive mounted at path is the one `expected` (a storage.<drive> record) names:
    its UUID and filesystem, and the Btrfs subvolume it is used with. The same filesystem
    mounted with another subvolume holds another tree."""
    found = mounted_identity(path)
    wanted = str(expected.get('subvolume') or '').rstrip('/')
    return found is not None and found == (expected.get('uuid'), expected.get('fstype'), wanted)


def devices_with_uuid(uuid, run=None):
    """The block devices that carry filesystem `uuid` (each once: lsblk lists an LVM
    volume under every disk it spans), or None when lsblk can't say. LVM snapshots of the
    drive are left out (lvm_snapshots): they are not copies."""
    try:
        result = (run or subprocess.run)(['lsblk', '-J', '-l', '-o', 'PATH,UUID,TYPE,NAME,KNAME,PKNAME'], capture_output=True,
                                         text=True, timeout=10, check=False)
        rows = json.loads(result.stdout or '')['blockdevices'] if result.returncode == 0 else None
        snapshots = lvm_snapshots(rows)
        found = [row['path'] for row in rows if row.get('uuid') == uuid and row.get('path') and row['path'] not in snapshots]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return sorted(set(found))


def lvm_layer(name, suffix):
    """Whether a device-mapper name is LVM's hidden layer `suffix` ('real' or 'cow') of a
    volume: '<vg>-<lv>-real'. LVM doubles every '-' inside the names, so the dash before
    the suffix is a separator only when the dashes there are odd in number."""
    if not name.endswith('-' + suffix):
        return False
    stem = name[:-len(suffix)]
    return (len(stem) - len(stem.rstrip('-'))) % 2 == 1


def lvm_snapshots(rows):
    """The paths of LVM snapshots (lvcreate -s) and their hidden layers, from lsblk's list
    (TYPE, NAME, KNAME, PKNAME). A volume with snapshots sits on '<origin>-real', and each
    snapshot on that '-real' and on its own '<snapshot>-cow'; lsblk lists such a volume
    once under each. A snapshot carries the volume's filesystem UUID, but it is not a copy
    left connected: Linux mounts the volume itself by that UUID (LVM gives the snapshot's
    link the lower priority), so it is left out of the copy check. The origin stays in."""
    names = {row.get('kname'): str(row.get('name') or '') for row in rows if row.get('type') == 'lvm'}
    found = set()
    for row in rows:
        name, parent = str(row.get('name') or ''), names.get(row.get('pkname'), '')
        if row.get('type') != 'lvm':
            continue
        layer = lvm_layer(name, 'real') or lvm_layer(name, 'cow')
        snapshot = (lvm_layer(parent, 'real') and parent != name + '-real') or parent == name + '-cow'
        if layer or snapshot:
            found.add(row.get('path'))
    return found


def btrfs_device_id(device, run=None):
    """A Btrfs device's own UUID (UUID_SUB), read from the device itself (blkid -p, which
    needs root), or None when it can't be read."""
    try:
        result = (run or subprocess.run)(['blkid', '-p', '-s', 'UUID_SUB', '-o', 'value', '--', device],
                                         capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return (result.stdout or '').strip() or None


def look_alikes(uuid, fstype, run=None):
    """The devices that look like copies of one another among those carrying the drive's
    UUID (a dd or Clonezilla copy left connected): fstab mounts by UUID, so Linux could
    mount either. The devices of one Btrfs pool share the UUID but each has a UUID_SUB of
    its own; a copy of one has the same one. [] when nothing looks copied, or when this
    can't be checked (lsblk or blkid can't say): a healthy drive is never refused then."""
    devices = devices_with_uuid(uuid, run) or []
    if len(devices) < 2:
        return []
    if fstype != 'btrfs':
        return devices
    ids = [btrfs_device_id(device, run) for device in devices]
    if None in ids:
        return []
    return [device for device, sub in zip(devices, ids) if ids.count(sub) > 1]


def copy_problem(record, drive, run=None):
    """Why the drive `record` (a storage.<drive> record) can't be trusted because a copy
    of it is connected, or None."""
    copies = look_alikes(record.get('uuid'), record.get('fstype'), run)
    if not copies:
        return None
    return (f'Two drives look the same (one is probably a copy); unplug the copy and try again. '
            f'{" and ".join(copies)} carry the same filesystem as the {drive} (UUID {record.get("uuid")}), '
            'so Linux could use either one.')


def verify_storage(config, keys=('data_ssd', 'media_hdd')):
    """Fail closed unless dedicated mounts match saved UUID/filesystem identities."""
    identities = []
    for key in keys:
        mount_key = 'data_mount' if key == 'data_ssd' else 'media_mount'
        path = mount_path(config[mount_key])
        expected = config['storage'][key]
        if not expected.get('uuid') or not expected.get('fstype'):
            raise RuntimeError('Storage identity is not configured')
        if not identity_matches(path, expected):
            raise RuntimeError('Storage identity mismatch; refusing private data access')
        problem = copy_problem(expected, 'SSD' if key == 'data_ssd' else 'HDD')
        if problem:
            raise RuntimeError(problem)
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
