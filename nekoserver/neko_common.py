"""Shared local filesystem safety; standard library only."""

import contextlib
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

VERSION = '0.50.0'

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


# The facts setup shares with the runtime (runtime/src/shared.json, built into it): one
# copy, so the two can never disagree.
SHARED = json.loads((Path(__file__).resolve().parent / 'runtime/src/shared.json').read_text())
# Every app NekoServer can install: {id: (name, what it's for, local port)}. The one
# list the installer, checks, dashboard and runtime take names and ports from.
APPS = {app['id']: (app['name'], app['role'], app['port']) for app in SHARED['apps']}
MEDIA_APPS = tuple(app['id'] for app in SHARED['apps'] if app['media'])


def app_port(app):
    return APPS[app][2]


def app_ports(apps):
    """{app: local port} for the named apps."""
    return {app: app_port(app) for app in apps}


# Local ports already taken: every app, Immich's restore test (2284) and the private
# Tailscale addresses (8442-8448). The dashboard (8787 by default) must avoid them all.
APP_PORTS = frozenset({*(port for _, _, port in APPS.values()), 2284, *range(8442, 8449)})
DASHBOARD_DEFAULT_PORT = 8787
# The optional AI assistant (modules/assistant.py, ASSISTANT.md): Ollama, on this PC
# only (127.0.0.1), asked by the dashboard's Ask Neko page. Its port is not in
# APP_PORTS: something of the person's own on it (an Ollama they installed) keeps it,
# and the assistant is not installed.
ASSISTANT = 'assistant'
OLLAMA_PORT = 11434


def assistant_installed(config):
    """The AI assistant is wanted and its Compose file is in place (setup writes it last,
    once the rest of its files are there). False for any settings that say otherwise."""
    options = config.get(ASSISTANT)
    if not isinstance(options, dict) or options.get('wanted') is not True:
        return False
    data = config.get('data_mount')
    return isinstance(data, str) and (Path(data) / 'compose' / ASSISTANT / 'compose.yaml').is_file()


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


def nightly_timer(description, time, zone, persistent):
    """The text of a systemd timer that starts its service every night at `time` (HH:MM)
    in `zone`. `persistent`: a night missed while the PC was off is caught up at boot."""
    return (
        f'[Unit]\nDescription={description}\n'
        f'[Timer]\nOnCalendar=*-*-* {time}:00 {zone}\nPersistent={"true" if persistent else "false"}\n'
        '[Install]\nWantedBy=timers.target\n'
    )


# Each job's recovery record (journal) and the service that finishes it, shared with the
# runtime, which writes them.
RECOVERY = {job: (Path(r['journal']), r['service']) for job, r in SHARED['recovery'].items()}
MAINTENANCE_JOURNAL = RECOVERY['maintenance'][0]
# Held by a running setup (install.sh keeps it open); the runtime checks the same file.
SETUP_LOCK = Path(SHARED['setup_lock'])
# Held for the whole of a NekoServer upgrade (upgrade.py) and a drive move (migrate.py),
# also while they finish one that was cut off: the two never run at the same time,
# whether started from the dashboard or a terminal. In /run: a restart lets it go.
OPERATION_LOCK = Path('/run/nekoserver-operation.lock')


def hold_operation(lock=None, wait=False):
    """The operation lock, held until the returned file is closed (or the program ends);
    None when another upgrade or drive move holds it (`wait`: wait for it instead)."""
    import fcntl

    held = open(lock or OPERATION_LOCK, 'a')
    try:
        fcntl.flock(held, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        held.close()
        return None
    return held


def read_journal(journal, expected_format):
    """A job's record (journal) as a dict, or None when it is missing, unreadable, not a
    JSON object or not of `expected_format` (an upgrade's and a drive move's differ)."""
    try:
        saved = json.loads(Path(journal).read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and saved.get('format') == expected_format else None


def held_lock_reason(locks):
    """The reason paired with the first of these (lock file, reason) that another program
    holds right now, or None. A None lock is skipped, and so is a lock file that cannot
    be opened (its folder is missing, say)."""
    import fcntl

    for path, reason in locks:
        if path is None:
            continue
        try:
            with open(path, 'a') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return reason
        except OSError:
            continue
    return None


# Every job that changes the apps' versions with the update journal (MAINTENANCE_JOURNAL):
# the monthly check, its retry and the service that finishes a cut-off one, the weekly
# app updates and "Update now" on the dashboard (one instance per app; systemctl matches
# the pattern against the services loaded now).
MAINTENANCE_UNITS = (
    'nekoserver-maintenance.service',
    'nekoserver-maintenance-resume.service',
    'nekoserver-maintenance-retry.service',
    'nekoserver-updates.service',
    'nekoserver-update-now@*.service',
)
# How to see whether one of them is still working.
MAINTENANCE_SHOWN = "sudo systemctl list-units 'nekoserver-maintenance*' 'nekoserver-update*'"


def maintenance_running(run=subprocess.run):
    """An app update (the monthly one, the weekly one, "Update now", or the service that
    finishes a cut-off one) is working right now (all are oneshot services: "activating"
    while they run)."""
    try:
        result = run(
            ['systemctl', 'is-active', *MAINTENANCE_UNITS], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool({'activating', 'deactivating'} & set(result.stdout.split()))


def pending_maintenance(journal=MAINTENANCE_JOURNAL, outcome='Nothing was changed.', running=None):
    """Why setup must not run yet, or None. An unfinished monthly update must be finished
    (or rolled back) before anything else touches the apps: another version would
    replace the program that can finish it, and even the same version would start the
    apps, letting them write data that a pending rollback then replaces."""
    running = running or maintenance_running  # looked up now, so a test's stand-in is used
    journal = Path(journal)
    if not journal.exists():
        return None
    try:
        started = json.loads(journal.read_text()).get('version')
    except (OSError, ValueError, AttributeError):
        started = None
    which = f'NekoServer v{started}' if isinstance(started, str) and started else 'an earlier NekoServer'
    if running():
        return (
            f'An app update started by {which} is running right now (it can take an hour or more). '
            f'Let it finish: {MAINTENANCE_SHOWN}  lists no running one once it has. '
            f'Then run  sudo bash install.sh  again. {outcome}'
        )
    return (
        f'A monthly update started by {which} did not finish, so some apps may still be stopped. '
        'Let it finish (or roll back) before running setup:\n'
        '  sudo systemctl start nekoserver-maintenance-resume.service\n'
        'When that has finished (sudo systemctl status nekoserver-maintenance-resume.service shows it '
        f'exited successfully), run  sudo bash install.sh  again. {outcome}\n'
        'If it keeps failing, this release can finish it instead (it changes nothing else):\n'
        '  sudo bash install.sh --finish-update'
    )


def typed(input_fn, question):
    """The answer, stripped; Ctrl+D is an empty answer."""
    try:
        return input_fn(question).strip()
    except EOFError:
        return ''


def nas_disk_wwn(saved, seen, label, choose):
    """The WWN to keep for a drive on a NAS disk (iSCSI), as the runtime checks it
    (backup.rs other_nas_disk): the NAS disk setup chose, never a restored or copied LUN
    with the same filesystem UUID. `saved` is the WWN kept from before (or None), `seen` the
    one Linux shows now. A disk that shows none can't be told from a copy, so it is refused
    here as the runtime refuses it, instead of passing setup and failing every start (the
    review of 0.47.0). `choose` says how to pick the drive again."""
    if not seen:
        raise RuntimeError(
            f'The {label} is on a NAS disk that Linux shows no WWN for, so a copy of that disk could not be '
            'told apart from it. Turn on the LUN\'s identifier (WWN/NAA) on the NAS, then run setup again.'
        )
    if saved and seen != saved:
        raise RuntimeError(
            f'The {label} has its UUID, but it is on another NAS disk than before ({seen}, not {saved}): a '
            f'restored or copied LUN, most likely. {choose} Nothing was changed.'
        )
    return seen


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


def reading_max_age(snapshot):
    """How old the dashboard's readings may be before they count as late: what the readings
    say themselves (`late_after`, decided once by the collector, collect.rs), the same
    answer the page and the phone alerts use. Readings from before they said so: the most
    it could be (a 5-minute interval)."""
    value = (snapshot or {}).get('late_after')
    return value if type(value) is int and 180 <= value <= 630 else 630


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
    text = str(value)
    if any(c in text for c in '\n\r\0'):
        raise RuntimeError('This path cannot be used in a system service: ' + repr(text))
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


# Long file names in backups. The rules match backup.rs (fitted, version_parts,
# latest_marker, partial_name), which writes them.
NAME_MAX = 255  # longest name ext4 and exFAT accept, in bytes
VERSION_TAG = len('.neko-20261022T104800Z-9999')  # room kept for a version's date
NAMES_FILE = '.neko-names.json'  # full names behind shortened version names


def fitted(text, room, whole):
    """`text` when it fits in `room` bytes; otherwise its start (cut between characters)
    and `~` plus 8 hex of the SHA-256 of `whole`, so long names stay distinct."""
    data = text.encode()
    if len(data) <= room:
        return text
    tag = '~' + hashlib.sha256(whole.encode()).hexdigest()[:8]
    return data[: max(0, room - len(tag))].decode('utf-8', 'ignore') + tag


def version_parts(name):
    """(stem, extension) of a file's dated versions: name.ext -> ('name', '.ext')."""
    stem, dot, ext = name.rpartition('.')
    stem, ext = (stem, dot + ext) if stem else (name, '')
    if len(stem.encode()) + VERSION_TAG + len(ext.encode()) <= NAME_MAX:
        return stem, ext
    if len(ext.encode()) + VERSION_TAG + 24 > NAME_MAX:
        stem, ext = name, ''
    return fitted(stem, NAME_MAX - VERSION_TAG - len(ext.encode()), name), ext


LATEST_SUFFIX = '.neko-latest'  # the note naming a file's current saved copy


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
    return (
        {k: v for k, v in names.items() if isinstance(k, str) and isinstance(v, str) and '/' not in v}
        if isinstance(names, dict)
        else {}
    )


def saved_versions(folder):
    """{dated copy: the file it is a version of}, as the backup recorded them (since
    0.28.7) in the folder's names file."""
    try:
        value = json.loads((Path(folder) / NAMES_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    versions = value.get('versions') if isinstance(value, dict) and value.get('format') == 'neko-names-v1' else None
    return (
        {
            k: v
            for k, v in versions.items()
            if isinstance(k, str) and isinstance(v, str) and '/' not in v and '/' not in k
        }
        if isinstance(versions, dict)
        else {}
    )


LEDGER_NAME = '.neko-ledger.json'  # checksum ledger at the root of a backup tree
QUARANTINE = 'neko-damaged'  # damaged copies kept aside at the root; never data


def valid_ledger_path(rel):
    """A ledger path is plain and relative: no leading /, no '.', '..' or empty parts."""
    return (
        isinstance(rel, str)
        and rel
        and '\0' not in rel
        and not rel.startswith('/')
        and all(part not in ('', '.', '..') for part in rel.split('/'))
    )


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
            value['entries'][rel] = {
                'sha256': sha,
                'size': (root / rel).stat().st_size,
                'verified_at': time.time(),
                'how': how,
            }
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
SYSTEM_FOLDERS = (
    '/bin',
    '/boot',
    '/dev',
    '/etc',
    '/lib',
    '/lib32',
    '/lib64',
    '/proc',
    '/root',
    '/run',
    '/sbin',
    '/sys',
    '/usr',
)
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
    safe_path(Path(text))  # no symlinks along the way, so the resolved path is the same folder
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
ATOMIC_TEMP = re.compile(r'\.neko-\d+-\d+-\d+')  # the backup program's atomic-write temporaries


def not_backup_data(here, root, name):
    """The tree's own ledger (with its lock, temporaries and an unreadable one set aside)
    at the root, and in any folder the version names, current-version notes, partial
    copies and atomic-write temporaries: kept next to the data, not data. Only these
    exact names and patterns: a user's own `.neko-notes.txt` is data (backup.rs
    metadata_name and ledger_file say the same)."""
    if here == root and (name in (LEDGER_NAME, LEDGER_LOCK) or name.startswith(LEDGER_NAME + '.')):
        return True
    return name == NAMES_FILE or name.endswith((LATEST_SUFFIX, '.neko-part')) or ATOMIC_TEMP.fullmatch(name) is not None


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
        return strict_files(
            root,
            lambda here, name: not_backup_data(here, root if at_root else None, name),
            (QUARANTINE,) if at_root else (),
        )
    except UnreadableEntries as exc:
        raise RuntimeError(
            'Some backup files could not be read; nothing was restored from a partial list: '
            + str(exc).split(': ', 1)[1]
        ) from None


class BackupDamaged(RuntimeError):
    """A backup copy no longer matches its recorded checksum (the copy is at fault)."""


def copy_checked(source, target, expected=None):
    """Copy one file into a new `target`: stream it while hashing, fsync, read it back
    from disk and hash again. Raises if the two disagree or, when `expected` (its
    recorded checksum) is given, if the backup copy no longer matches it."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with open(source, 'rb') as src:
            return copy_checked_into(src, folder, target.name, expected, source)
    finally:
        os.close(folder)


def copy_checked_into(src, folder, name, expected=None, what=None):
    """copy_checked's copy, written as a new file `name` in the open folder `folder` (a
    folder handle: the file lands in that folder, wherever its path now leads), never
    through a link and never over a file already there. `src`: an open binary file;
    `what`: where it is read from, for the messages. Returns the checksum written."""
    what = what or name
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder)
    stream = hashlib.sha256()
    with os.fdopen(fd, 'w+b') as out:
        os.fchmod(out.fileno(), 0o600)
        while chunk := src.read(1 << 20):
            stream.update(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
        # Written to the drive: dropped from memory, so the read below comes from the
        # drive itself (from memory, damage on the drive would never be seen).
        os.posix_fadvise(out.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        out.seek(0)
        landed = hashlib.file_digest(out, 'sha256').hexdigest()
    if landed != stream.hexdigest():
        raise RuntimeError(f'Restoring {what} failed: the copy written to disk does not match what was read')
    if expected is not None and landed != expected:
        raise BackupDamaged(f'{what} is damaged on the backup (it no longer matches its recorded checksum)')
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
MY_SETTINGS = 'my-config.json'  # made by the wizard; git does not track it
SETTINGS_TEMPLATE = 'config.json'  # the template in the release folder (no drives in it)
BUNDLE_MANIFEST = 'release-manifest.json'  # beside the copy kept on the SSD (modules/recovery.MANIFEST)
NO_SETTINGS = '<no settings file found>'
NO_SETTINGS_HELP = (
    'config.json in this folder is the settings template (no drives in it), not your settings. '
    'Run  sudo bash install.sh  first (it makes my-config.json), or name your settings file with '
    '--config (for example --config my-config.json)'
)


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


# The system's top-level folders a drive is mounted inside of, never on.
TOP_FOLDERS = ('mnt', 'srv', 'home', 'var', 'tmp')


# Where each app keeps your files, chosen at setup ("locations": "ssd" or "media"; the
# apps' settings and databases always stay on the SSD, movies and shows on the media drive).
# Downloads live with the music: Lidarr moves finished downloads into the library, which
# works only on one drive. A setting missing is the layout before 0.46.0.
# Where each app's files live by default (shared with the runtime).
LOCATIONS = {what: (place['default'], tuple(place['folders'])) for what, place in SHARED['locations'].items()}
DRIVE_KEYS = {'ssd': 'data_mount', 'media': 'media_mount'}


# A drive beyond the app SSD and the media drive (0.50.0, free placement): listed under
# "storage" by a short name of its own ("d3"), and mounted at /mnt/nekoserver-drives/d3.
# A row's data may be put on one ("locations": {"photos": "d3"}).
EXTRA_MOUNTS = Path('/mnt/nekoserver-drives')
DRIVE_NAME = re.compile(r'[a-z][a-z0-9]{0,15}')
OLD_STORAGE_KEYS = ('data_ssd', 'media_hdd')


def extra_drives(config):
    """{name: its record} of the drives under "storage" beyond the SSD and the media drive."""
    storage = config.get('storage')
    if not isinstance(storage, dict):
        return {}
    return {
        name: record
        for name, record in storage.items()
        if name not in OLD_STORAGE_KEYS and name not in DRIVE_KEYS and isinstance(record, dict)
    }


def drive_folder(config, drive):
    """Where a drive is mounted: /srv/nekoserver for "ssd", /mnt/nekoserver-media for
    "media", /mnt/nekoserver-drives/<name> for another one under "storage"."""
    if drive in DRIVE_KEYS:
        return mount_path(config[DRIVE_KEYS[drive]])
    if drive not in extra_drives(config) or not DRIVE_NAME.fullmatch(str(drive)):
        raise RuntimeError(f'"{drive}" is not a drive under "storage" in my-config.json')
    return EXTRA_MOUNTS / drive


def location(config, what):
    """The drive that holds `what` (photos, music, cloud, movies): 'ssd', 'media', or the
    name of another drive under "storage"."""
    default, _ = LOCATIONS[what]
    chosen = (config.get('locations') or {}).get(what, default)
    if chosen not in DRIVE_KEYS and chosen not in extra_drives(config):
        raise RuntimeError(f'locations.{what} must be "ssd", "media" or a drive under "storage"')
    return chosen


def files_root(config, what):
    """The drive's folder (/srv/nekoserver, /mnt/nekoserver-media or another drive's) that
    holds `what`."""
    return drive_folder(config, location(config, what))


def movies_library(config):
    """The folder the films, the shows and their downloads are in (library/movies,
    library/tv, library/downloads): on the media drive unless placed elsewhere."""
    return files_root(config, 'movies') / 'library'


# Where a group's apps keep their settings and databases when not on the apps' drive
# (0.50.0): "install": {"photos": "d3"} in the settings, naming a drive under "storage".
INSTALL_SECTION = 'install'


def drives_in_use(config):
    """The extra drives something is placed on (an app's files, or its settings and
    database): the ones setup mounts and every job checks."""
    placed = [config.get('locations'), config.get(INSTALL_SECTION)]
    named = {drive for section in placed if isinstance(section, dict) for drive in section.values()}
    return sorted(drive for drive in named if isinstance(drive, str) and drive in extra_drives(config))


def photo_library(config):
    """Immich's photos and videos (its upload location)."""
    return files_root(config, 'photos') / LOCATIONS['photos'][1][0]


def music_library(config):
    return files_root(config, 'music') / LOCATIONS['music'][1][0]


def music_downloads(config):
    return files_root(config, 'music') / LOCATIONS['music'][1][1]


def cloud_files(config):
    """Nextcloud's files (its data folder)."""
    return files_root(config, 'cloud') / LOCATIONS['cloud'][1][0]


def mount_path(value):
    path = safe_path(value)
    if (
        not path.is_absolute()
        or len(path.parts) == 1
        or (len(path.parts) == 2 and path.parts[1] in TOP_FOLDERS)
        or '..' in path.parts
        or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value))
    ):
        raise RuntimeError(f'Unsafe mount path: {value}')
    if any(path == Path(top) or Path(top) in path.parents for top in HIDDEN_FROM_SERVICES):
        raise RuntimeError(
            f'Drives cannot be mounted under /home, /root or /run/user ({value}): NekoServer\'s services '
            'cannot see those folders. Mount it under /srv or /mnt instead, for example /srv/nekoserver'
        )
    return path


def relative_path(value):
    path = Path(value)
    if (
        path.is_absolute()
        or '..' in path.parts
        or str(path) == '.'
        or not re.fullmatch(r'[A-Za-z0-9_./-]+', str(value))
    ):
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
            raise RuntimeError(
                f'{what}: GitHub is limiting requests from this address right now (HTTP {exc.code}); '
                'wait an hour and run setup again'
            ) from None
        raise RuntimeError(f'{what}: {url} answered HTTP {exc.code}; try again later') from None
    except OSError as exc:
        reason = str(getattr(exc, 'reason', exc))
        # A clock set wrong makes every certificate look not valid yet, or expired.
        if re.search(r'not yet valid|certificate has expired', reason, re.IGNORECASE):
            raise RuntimeError(
                f'{what}: could not download {url} ({reason}). This PC\'s clock looks wrong (check '
                'the date and time with  timedatectl): let the network set it (sudo timedatectl '
                'set-ntp true), wait a minute, then run setup again'
            ) from None
        raise RuntimeError(
            f'{what}: could not download {url} ({reason}). Check that this PC '
            'is connected to the internet, then run setup again'
        ) from None


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
        raise RuntimeError(
            f'{what} cannot start: '
            + '; '.join(f'port {port} ({ip}) is in use by {holder}' for port, ip, holder in taken)
            + f'. {what} needs it: stop that program and keep it from starting with the PC (for a '
            'service, sudo systemctl disable --now its name), then run setup again'
        )
    named = services_to_pull(base, what, services)
    if named:
        if log:
            log(f'{what}: downloading its images (this can take a while on a slow connection)')
        pull_images([*base, 'pull', '--quiet', *named], what, log)
    result = checked_run([*base, 'up', '-d'], timeout, f'{what} (docker compose up)')
    lost = without_network(base, what)
    if lost:
        if log:
            log(
                f'{what}: {", ".join(lost)} was running without its network (left so by an earlier start that '
                'failed); recreating it'
            )
        result = checked_run(
            [*base, 'up', '-d', '--force-recreate', '--no-deps', *lost], timeout, f'{what} (docker compose up)'
        )
    broken = damaged_images(base, what)
    if broken:
        if log:
            log(
                f'{what}: {", ".join(name for name, _ in broken)} cannot start because its downloaded image is '
                'damaged (a download cut off by a power loss, most likely): downloading it again'
            )
        names = [name for name, _ in broken]
        download_again(base, what, services, names)
        result = checked_run(
            [*base, 'up', '-d', '--force-recreate', '--no-deps', *names], timeout, f'{what} (docker compose up)'
        )
        still = damaged_images(base, what)
        if still:
            raise RuntimeError(
                f'{what}: {", ".join(f"{name} ({why})" for name, why in still)} still cannot start after its image '
                'was downloaded again. Check the drive the images are on (sudo dmesg | tail), then run setup again'
            )
    return result


# What a container says when the program its image names cannot run: the image's files
# are not what they should be. Seen after a power loss during setup's download: Docker
# listed the image as complete, but the files of its last layer were lost with the
# unwritten disk cache, and the container restarted for ever with this line.
BROKEN_PROGRAM = re.compile(r'exec \S+: (?:exec format error|no such file or directory)', re.IGNORECASE)


def damaged_images(base, what, sleep=time.sleep):
    """(service, what its container said) for each container of the app that cannot start
    because its image is damaged (BROKEN_PROGRAM in its output). A container whose program
    cannot run exits at once and Docker restarts it: looked for right after the start, and
    once more after a moment when a container is not running yet."""
    docker = docker_command(base)
    for last in (False, True):
        rows = compose_rows(base, what)
        if all(row.get('State') == 'running' for row in rows):
            return []
        found = broken_containers(docker, rows)
        if found or last:
            return found
        sleep(2)  # a container still being created: let Docker start it
    return []


def docker_command(base):
    """The docker command the compose command `base` is built on."""
    return list(base[: base.index('compose')]) if 'compose' in base else ['docker']


def broken_containers(docker, rows):
    """(service, the line) for each container, among the compose rows, that is not running
    and says its program cannot run."""
    found = []
    for row in rows:
        name, container = row.get('Service'), row.get('ID') or row.get('Name')
        if not name or not container or row.get('State') == 'running':
            continue
        match = BROKEN_PROGRAM.search(container_said(docker, container))
        if match:
            found.append((name, match.group(0)))
    return sorted(found)


def container_said(docker, container):
    """The last lines of a container's output ("" when Docker cannot be asked)."""
    try:
        said = subprocess.run(
            [*docker, 'logs', '--tail', '20', container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return (said.stdout or '') + (said.stderr or '')


def download_again(base, what, services, names):
    """Remove the containers and the images of `names` (the app's services) and download
    the images anew: Docker keeps a damaged image as complete, so a plain pull would
    change nothing."""
    docker = docker_command(base)
    checked_run([*base, 'rm', '-sf', *names], 300, f'{what} (docker compose rm)')
    images = sorted({str(services.get(name, {}).get('image') or '') for name in names} - {''})
    if images:
        checked_run([*docker, 'image', 'rm', '-f', *images], 300, f'{what} (docker image rm)')
    pull_images([*base, 'pull', '--quiet', *names], what)


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
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # a closing connection is no holder
        try:
            probe.bind((ip, port))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def port_holder(port, protocol='tcp', docker=('docker',)):
    """Who listens on this port, for a person to find it: a program and its process, or
    the Docker container publishing it."""
    try:
        shown = subprocess.run(
            ['ss', '-Hlunp' if protocol == 'udp' else '-Hltnp', f'sport = :{port}'],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        shown = ''
    found = re.search(r'users:\(\("([^"]+)",pid=(\d+)', shown)
    if not found:
        return 'another program'
    if found.group(1) == 'docker-proxy':
        try:
            names = subprocess.run(
                [*docker, 'ps', '--filter', f'publish={port}', '--format', '{{.Names}}'],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            names = []
        return 'the Docker container ' + ', '.join(names) if names else 'another Docker container'
    return f'{found.group(1)} (process {found.group(2)})'


def compose_rows(base, what):
    """The app's containers as docker compose ps lists them (a dict each)."""
    shown = checked_run([*base, 'ps', '--format', 'json'], 60, f'{what} (docker compose ps)').stdout or ''
    try:
        rows = json.loads(shown)  # an older Compose: one JSON list
        rows = rows if isinstance(rows, list) else [rows]
    except ValueError:  # a newer one: a JSON object per line
        rows = [json.loads(line) for line in shown.splitlines() if line.strip().startswith('{')]
    return [row for row in rows if isinstance(row, dict)]


def without_network(base, what):
    """Services whose running container has no network at all. A start that failed while
    Docker set up its networking (a port in use, say) left the container that way, and
    Docker 29 went on starting it without one: the app runs, but nothing can reach it
    (tried on AlmaLinux). No NekoServer app runs without a network."""
    docker = list(base[: base.index('compose')]) if 'compose' in base else ['docker']
    lost = []
    for row in compose_rows(base, what):
        if row.get('State') != 'running' or not (row.get('ID') or row.get('Name')):
            continue
        try:
            shown = subprocess.run(
                [*docker, 'inspect', '--format', '{{len .NetworkSettings.Networks}}', row.get('ID') or row.get('Name')],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if shown.returncode == 0 and shown.stdout.strip() == '0':
            lost.append(row.get('Service'))
    return sorted(name for name in lost if name)


def busy_ports(base, services, what):
    """[(port, address, holder)] for the ports these services publish that another program
    holds now. The app's own running containers hold theirs: those are left out."""
    wanted = [
        (name, p)
        for name, service in sorted(services.items())
        for p in (service.get('ports') or [])
        if isinstance(p, dict) and p.get('published')
    ]
    if not wanted:
        return []
    running = {row.get('Service') for row in compose_rows(base, what) if row.get('State') == 'running'}
    docker = tuple(base[: base.index('compose')]) if 'compose' in base else ('docker',)
    taken = []
    for name, p in wanted:
        ip, port, protocol = p.get('host_ip') or '0.0.0.0', int(p['published']), p.get('protocol') or 'tcp'
        if name not in running and port_taken(ip, port, protocol):
            taken.append((port, ip, port_holder(port, protocol, docker)))
    return taken


def services_to_pull(base, what, services=None, present=None):
    """The services whose image must be downloaded: only one not on this PC yet (an app
    set up for the first time). Setup never downloads a newer image for an app that is
    already there (a named one, lscr.io/...:latest, say): that would update the app
    outside the monthly update, which first takes the snapshot it can roll back to and
    tests the new version (MONTHLY-MAINTENANCE.md). After a monthly update, maintenance
    pins each service to the exact image it tested (sha256:...): such an image cannot be
    downloaded by its ID ("pull access denied for sha256"), and must already be on this
    PC, which is checked instead. `present(image)`: whether Docker has it (tests)."""
    if services is None:
        services = compose_services(base, what)
    docker = list(base[: base.index('compose')]) if 'compose' in base else ['docker']
    if present is None:

        def present(image):
            return (
                subprocess.run(
                    [*docker, 'image', 'inspect', image], capture_output=True, text=True, timeout=60, check=False
                ).returncode
                == 0
            )

    named = []
    for name, service in sorted(services.items()):
        image = str(service.get('image') or '')
        if image.startswith('sha256:'):
            if not present(image):
                raise RuntimeError(
                    f'{what}: the image the monthly update pinned for {name} ({image[:19]}...) is not on this PC '
                    'any more. Run the monthly update again (sudo systemctl start nekoserver-maintenance.service), '
                    'or take the pin out of maintenance-images.json next to its compose.yaml'
                )
        elif not image or not present(image):
            named.append(name)
    return named


# A registry that is limiting requests ("toomanyrequests"), or a connection that drops
# part-way, answers like this. Each is worth waiting for and trying again.
TRANSIENT_PULL = (
    'toomanyrequests',
    'too many requests',
    'tls handshake timeout',
    'unexpected eof',
    'i/o timeout',
    'connection reset',
    'temporary failure',
    'service unavailable',
    '503',
)
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
                log(
                    f'{what}: the download was refused for now (the registry is busy or the '
                    f'connection dropped: {reason[-160:]}); trying again in {wait} seconds ({attempt + 1} of {len(waits)})'
                )
            sleep(wait)


def findmnt(columns, target, by='--mountpoint', run=None, timeout=10):
    """findmnt's columns for the mount in effect at target: with mounts stacked on one
    folder it lists them oldest first, and the last one is the one in use. None when
    nothing is mounted there (or findmnt can't say)."""
    try:
        result = (run or subprocess.run)(
            ['findmnt', '-n', '-o', columns, by, str(target)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
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


# ---- The media apps, a part of their own (since 0.50.0) ----------------------------------
# The media apps (setup's first page; guided_setup.choose_media_apps without it): Jellyfin with Seerr, the
# apps that find and download (Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent), Immich,
# Navidrome, video on the graphics chip, the hourly app check and the TV mode. The answer
# is "wanted" under "media_apps"; nothing there means yes, as every server set up before
# 0.50.0 has them. Every step that looks after these apps asks the two functions below,
# never media.services or photos.deploy_immich alone.
MEDIA_SECTION = 'media_apps'
# The media apps come in three groups, each chosen on its own (the user, 2026-11-11):
# any combination, none and photos alone included. Saved as "photos", "movies" and
# "music" under "media_apps"; one not chosen yet is on. "wanted": false (the one yes/no
# 0.50.0 first had, and a settings file written by hand) switches off every group that
# has no answer of its own. Which apps a group holds is in shared.json ("groups"): an
# app is set up when every group it names is on.
MEDIA_GROUPS = ('photos', 'movies', 'music')
GROUP_NAMES = {
    'photos': 'Photos (Immich)',
    'movies': 'Movies and shows (Jellyfin)',
    'music': 'Music (Navidrome)',
}
# Finding and downloading is part of its group (the user, on seeing the first page:
# "isn't the seer sonar radar part of jellyfin?"): Movies and shows is Jellyfin with
# Seerr, Sonarr and Radarr; Music is Navidrome with Lidarr; Prowlarr and qBittorrent,
# which those need, come with either. Each of the two groups has one small switch, "Also
# find and download new ones", saved as "movies_downloads" and "music_downloads" beside
# the groups and on unless switched off: off leaves Jellyfin, or Navidrome, alone.
# "downloads" is not saved: it says whether any finding and downloading is set up.
FINDING = {'movies': 'movies_downloads', 'music': 'music_downloads'}
DOWNLOADS = 'downloads'
APP_GROUPS = {app['id']: tuple(app.get('groups', ())) for app in SHARED['apps']}


def server_exists(config):
    """This PC has a NekoServer server already: one of its schedules is installed (on the
    system disk, so it is seen with the app drive unplugged too), or Docker's folder is
    on the app drive. Not so on a PC setup runs on for the first time."""
    try:
        if any(SYSTEMD_UNITS.glob('nekoserver-*')):
            return True
    except OSError:
        pass
    # The app drive's folder comes from the settings: looked at only once it is proved to
    # be a mount folder, and Docker's folder to lie inside it with every link followed.
    try:
        data = os.path.realpath(mount_path(config.get('data_mount') or '/srv/nekoserver'))
    except (RuntimeError, ValueError, TypeError):
        return False
    engine = os.path.realpath(os.path.join(data, 'docker-engine'))
    return engine.startswith(data + os.sep) and os.path.isdir(engine)


def media_group_chosen(config, group):
    """This group has an answer: its own, or the one yes/no for all of them."""
    section = config.get(MEDIA_SECTION)
    return isinstance(section, dict) and (
        isinstance(section.get(group), bool) or isinstance(section.get('wanted'), bool)
    )


def media_group(config, group):
    """The group is part of this server: its own answer, else on unless "wanted" is false."""
    section = config.get(MEDIA_SECTION)
    if group == DOWNLOADS:
        return any(media_group(config, of) and media_group(config, switch) for of, switch in FINDING.items())
    if group in FINDING.values():
        # On unless switched off (its group decides the rest: an app needs both).
        return not (isinstance(section, dict) and section.get(group) is False)
    if not isinstance(section, dict):
        return True
    if isinstance(section.get(group), bool):
        return section[group]
    return section.get('wanted') is not False


def media_apps_wanted(config):
    """Any of the media groups is part of this server."""
    return any(media_group(config, group) for group in MEDIA_GROUPS)


def listed(parts):
    """'a', 'a and b', 'a, b and c'."""
    return ', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else ''.join(parts)


def drive_holds(config, key):
    """What goes on the drive of this role ('data_ssd', 'media_hdd', 'usb'), naming only
    what this server has (gate run C: "your apps, photos and music" with Photos off)."""
    cloud = bool(config.get('nextcloud', {}).get('enabled', False))
    if key == 'data_ssd':
        return listed(
            ['your apps']
            + ['photos'] * media_group(config, 'photos')
            + ['music'] * media_group(config, 'music')
        )
    if key == 'media_hdd':
        kept = ['movies', 'shows'] * media_group(config, 'movies') + ['Nextcloud files'] * cloud
        return 'your ' + listed([*kept, 'the backups']) if kept else 'the backups'
    copied = ['photos'] * immich_wanted(config) + ['Nextcloud files'] * cloud
    return 'the weekly copy of your ' + (listed(copied) or 'files')


def app_wanted(config, app):
    """Every group the app belongs to is on (an app in no group is not a media app)."""
    return all(media_group(config, group) for group in APP_GROUPS.get(app, ()))


def media_services(config):
    """The media apps set up: those in media.services whose groups are on (none with
    every group off). One left out is stopped by the media step, and its data kept."""
    return [app for app in config.get('media', {}).get('services', []) if app_wanted(config, app)]


# Which Navidrome login the person is given. Navidrome has two: the administrator, and a
# "listener" that is not one, which the music check signs in with (a login with the
# least rights is the safer one to keep a token of). Up to 0.49.0 the person was given
# the listener for their phone, so in Navidrome alone they were no administrator.
# - A server whose Navidrome logins are made by 0.50.0 or later: the administrator, as
#   in every other app ("your_login": "administrator" under "music", written by the
#   App passwords step). The listener is then NekoServer's own checking account.
# - A server set up before: the listener stays the one the guides give for phones, as
#   they are signed in with it and Navidrome keeps playlists and favourites per
#   account; the guides name both logins. Nothing is switched silently.
ADMINISTRATOR = 'administrator'


def navidrome_login(config):
    """The saved login (account_store's name) the person signs in to Navidrome with."""
    music = config.get('music')
    yours = music.get('your_login') if isinstance(music, dict) else None
    return 'navidrome' if yours == ADMINISTRATOR else 'navidrome_listener'


def media_apps_installed(config):
    """The media apps this server already has, Immich among them: those whose Compose
    file is on the SSD. None on a new server (or before the SSD is mounted)."""
    data = os.path.realpath(mount_path(config['data_mount']))
    found = []
    for app in (*MEDIA_APPS, 'immich'):
        # The SSD's path comes from the settings file: a file is looked at only once it
        # is proved to lie on the SSD with every link followed.
        manifest = os.path.realpath(os.path.join(data, 'compose', app, 'compose.yaml'))
        if manifest.startswith(data + os.sep) and os.path.isfile(manifest):
            found.append(app)
    return found


# The steps that are only about the media apps: with them left out each one sets nothing
# up (and stops or retires what it set up before), and nothing checks them afterwards.
MEDIA_STEPS = (
    'media',
    'photos',
    'photo_preservation',
    'hardware_video',
    'onboarding',
    'music',
    'music_backup',
    'media_monitor',
    'tv',
    'app_backup',
)
# ... and the ones that are about one group alone.
GROUP_STEPS = {
    'photos': ('photos', 'photo_preservation'),
    'movies': ('tv',),
    'music': ('music', 'music_backup'),
}


def apps_to_connect(config):
    """There are apps for the connecting step (onboarding) and the hourly check of them
    (media_monitor) to work on: Jellyfin and its libraries, or the download apps."""
    return media_group(config, 'movies') or media_group(config, DOWNLOADS)


def media_steps_left_out(config):
    """The steps that set nothing up on this server because their group is off: a
    group's own steps, and with every group off all the media steps."""
    if not media_apps_wanted(config):
        return set(MEDIA_STEPS)
    left = {step for group in MEDIA_GROUPS if not media_group(config, group) for step in GROUP_STEPS[group]}
    return left if apps_to_connect(config) else left | {'onboarding', 'media_monitor'}


def immich_wanted(config):
    """Immich is set up: photos.deploy_immich, and the Photos group not left out."""
    return media_group(config, 'photos') and config.get('photos', {}).get('deploy_immich') is True


# The optional questions setup asks (the media apps, the backup times, and when it
# connects the media apps), by the key their last answer is kept under, and the words
# the next run shows them with.
OPTIONAL_QUESTIONS = {
    MEDIA_SECTION: 'Media apps',
    'backup_times': 'Backup times',
    'indexers': 'Indexers (Prowlarr)',
    'playback': 'Playback test',
}


def optional_answers_file(data_mount):
    """Where setup keeps the last answer to each optional question. No secrets: how many
    sources were added, which indexers were chosen, whether the playback test was done."""
    return Path(data_mount) / 'config' / 'onboarding-answers.json'


def optional_answers(data_mount):
    """The last answers by question, or {} when none were kept yet (or the file can't be read)."""
    file = optional_answers_file(data_mount)
    # The SSD's path comes from the settings file: the answers are read only from a file
    # that lies inside it once every link is followed.
    if not os.path.realpath(file).startswith(os.path.realpath(data_mount) + os.sep):
        return {}
    try:
        kept = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(kept, dict):
        return {}
    return {key: value for key, value in kept.items() if key in OPTIONAL_QUESTIONS and isinstance(value, str)}


def remember_answer(data_mount, question, text):
    """Keep the answer just given to an optional question, so that the next setup run
    shows it and asks again only when the person wants to change it."""
    kept = optional_answers(data_mount)
    kept[question] = str(text)[:200]
    path = optional_answers_file(data_mount)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, kept)


# ---- When the backups run (since 0.48.0 one place, asked by setup) --------------------
# The three times a person chooses: "backup_times" in the settings file, asked at the
# drives step (guided_setup.choose_backup_times). Every timer is written from them (job_times).
BACKUP_TIMES = 'backup_times'
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
DAILY_DEFAULT = '04:15'  # the media apps' snapshot; the other nightly jobs keep their distance from it
WEEKLY_DAY_DEFAULT, WEEKLY_DEFAULT = 'Sun', '03:00'  # the USB copy of the photos and Nextcloud files
MONTHLY_DEFAULT = '02:30'  # the monthly check on the 1st, and the weekly app updates
# How far from the daily time each nightly job starts (minutes), so they never start in
# the same minute: the music copy first, then the app snapshot, then this PC's own
# settings. The Immich database copy of the old daily photo schedule went two hours before.
NIGHTLY_OFFSETS = {'music': -30, 'app': 0, 'host': 60, 'photo_database': -120}
# In the weekly photo schedule the Immich database copy goes just before the USB copy,
# which takes it along (Sunday 02:15 before 03:00, as it always was).
DATABASE_BEFORE_USB = 45
# ASCII: in Python \d is any Unicode digit, and a time written in other digits
# (Arabic-Indic, say) would reach the timers' OnCalendar, which systemd cannot read.
TIME_OF_DAY = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d', re.ASCII)


def valid_time(text):
    """True for "HH:MM" on a 24-hour clock."""
    return isinstance(text, str) and TIME_OF_DAY.fullmatch(text) is not None


def minutes_later(time, minutes):
    """ "HH:MM" shifted by `minutes` (negative for earlier), going round midnight."""
    hours, mins = (int(part) for part in time.split(':'))
    total = (hours * 60 + mins + minutes) % (24 * 60)
    return f'{total // 60:02d}:{total % 60:02d}'


def day_before_if_wrapped(day, time, minutes):
    """The weekday a job lands on when it runs `minutes` from `time` on `day`: the day
    before when the shift goes back past midnight (Sun 00:20 less 45 minutes is Sat 23:35),
    the day after when it goes forward past it."""
    hours, mins = (int(part) for part in time.split(':'))
    days = (hours * 60 + mins + minutes) // (24 * 60)
    return WEEKDAYS[(WEEKDAYS.index(day) + days) % 7]


def backup_times(config):
    """The times setup asked for: {'daily', 'weekly_day', 'weekly', 'monthly'}, checked, or
    None when the settings file has none yet (made before 0.48.0): each job then keeps
    the time its own setting named (job_times)."""
    chosen = config.get(BACKUP_TIMES)
    if chosen is None:
        return None
    if not isinstance(chosen, dict):
        raise RuntimeError(
            '"backup_times" in the settings must be an object with daily, weekly_day, weekly and monthly'
        )
    times = {
        'daily': chosen.get('daily', DAILY_DEFAULT),
        'weekly_day': chosen.get('weekly_day', WEEKLY_DAY_DEFAULT),
        'weekly': chosen.get('weekly', WEEKLY_DEFAULT),
        'monthly': chosen.get('monthly', MONTHLY_DEFAULT),
    }
    for key in ('daily', 'weekly', 'monthly'):
        if not valid_time(times[key]):
            raise RuntimeError(f'"{key}" under "backup_times" must be a time of day as HH:MM, not {times[key]!r}')
    if times['weekly_day'] not in WEEKDAYS:
        raise RuntimeError('"weekly_day" under "backup_times" must be one of ' + ', '.join(WEEKDAYS))
    return times


def _own_time(config, section, key, default):
    """A job's time from its own setting (settings files from before 0.48.0)."""
    value = config.get(section, {}).get(key, default) if isinstance(config.get(section, {}), dict) else default
    if not valid_time(value):
        raise RuntimeError(f'{section}.{key} must be HH:MM')
    return value


def job_times(config):
    """When each scheduled job runs, as its timer is written: app (the media apps'
    snapshot), music, host (this PC's own settings), photo_database (the daily Immich
    database copy of the old photo schedule), photo_database_weekly and
    photo_database_weekly_day (the same copy just before the USB copy: the day before it
    when the 45 minutes cross midnight), usb_day and usb (the USB copy), monthly (the
    monthly check and the weekly app updates). From "backup_times" when setup asked for
    them, else from the settings each job had before 0.48.0, so a server set up earlier
    keeps its times."""
    chosen = backup_times(config)
    if chosen is None:
        photos = config.get('photos', {})
        day = photos.get('backup_weekday', WEEKLY_DAY_DEFAULT) if isinstance(photos, dict) else WEEKLY_DAY_DEFAULT
        if day not in WEEKDAYS:
            raise RuntimeError('photos.backup_weekday must be one of ' + ', '.join(WEEKDAYS))
        database = _own_time(config, 'photos', 'database_backup_time', '02:15')
        return {
            'app': _own_time(config, 'app_backup', 'time', DAILY_DEFAULT),
            'music': _own_time(config, 'music', 'backup_time', '03:45'),
            'host': _own_time(config, 'host_backup', 'time', '05:15'),
            'photo_database': database,
            'photo_database_weekly': database,
            'photo_database_weekly_day': day,
            'usb_day': day,
            'usb': _own_time(config, 'photos', 'backup_time', WEEKLY_DEFAULT),
            'monthly': _own_time(config, 'maintenance', 'time', MONTHLY_DEFAULT),
        }
    return {
        'app': minutes_later(chosen['daily'], NIGHTLY_OFFSETS['app']),
        'music': minutes_later(chosen['daily'], NIGHTLY_OFFSETS['music']),
        'host': minutes_later(chosen['daily'], NIGHTLY_OFFSETS['host']),
        'photo_database': minutes_later(chosen['daily'], NIGHTLY_OFFSETS['photo_database']),
        'photo_database_weekly': minutes_later(chosen['weekly'], -DATABASE_BEFORE_USB),
        'photo_database_weekly_day': day_before_if_wrapped(
            chosen['weekly_day'], chosen['weekly'], -DATABASE_BEFORE_USB
        ),
        'usb_day': chosen['weekly_day'],
        'usb': chosen['weekly'],
        'monthly': chosen['monthly'],
    }


def backup_times_said(config):
    """The three times in a few words, as setup shows them and keeps them as its answer."""
    times = job_times(config)
    return f"daily {times['app']}, weekly {times['usb_day']} {times['usb']}, monthly on the 1st at {times['monthly']}"


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
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:  # RENAME_NOREPLACE
        code = ctypes.get_errno()
        raise (FileExistsError if code == errno.EEXIST else OSError)(code, os.strerror(code), str(target))


def made_whole(directory, mode, uid, gid):
    """Make one folder that appears with its owner and mode already set: made under a
    temporary name beside it, then moved into place. Cut off between making and
    chowning it (Ctrl+C, a power cut), a folder made in place would stay root's for
    good, and the app or desktop user it was meant for could never write to it."""
    temp = directory.with_name('.' + directory.name + '.neko-new')
    if temp.is_dir() and not temp.is_symlink():
        try:
            temp.rmdir()  # left by a run cut off here; empty, as it was made
        except OSError:
            pass
    temp.mkdir(mode=mode)
    if uid is not None:
        os.chown(temp, uid, gid)
    temp.chmod(mode)  # Apply after ownership changes, independent of umask.
    try:
        rename_no_replace(temp, directory)
    except FileExistsError:
        temp.rmdir()  # made meanwhile by something else: that one stays
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
    import nfs_exports

    found = findmnt('UUID,FSTYPE,SOURCE', path) or ''
    shared = nfs_exports.mounted(found)
    if shared:
        # A folder a NAS shares over NFS has no UUID: the identity made from its address.
        return (*shared, '')
    fields = found.split()
    if len(fields) < 3:
        return None
    tree = fields[2].split('[', 1)[1].rstrip(']').rstrip('/') if '[' in fields[2] else ''
    return fields[0], fields[1], tree


def identity_matches(path, expected):
    """The drive mounted at path carries the identity `expected` (a storage.<drive> record)
    names: its UUID and filesystem, and the Btrfs subvolume it is used with. A quick look
    for the recovery menu, which asks often while it works; the drive check itself (copies,
    the NAS disk's WWN) is the runtime's (verify_storage)."""
    found = mounted_identity(path)
    wanted = str(expected.get('subvolume') or '').rstrip('/')
    return found is not None and found == (expected.get('uuid'), expected.get('fstype'), wanted)


def verify_storage(config, keys=('data_ssd', 'media_hdd'), ask=None):
    """The drives are the ones setup chose (UUID, filesystem, Btrfs subvolume, the NAS disk's
    WWN), no copy of one is connected, and the SSD and the HDD are two filesystems: checked
    by the runtime (`nekoserver-runtime check-drives`, backup.rs), the same check every job
    and Docker's start guard make. The rules live once, there; setup kept a copy of its own,
    which already disagreed with it (the review of 0.47.0)."""
    import tempfile

    import native_runtime

    if tuple(keys) == OLD_STORAGE_KEYS:
        # ... and every other drive something is placed on (0.50.0).
        keys = (*keys, *drives_in_use(config))
    settings = {k: config.get(k) for k in ('data_mount', 'media_mount', 'external_backup_mount', 'storage')}
    photos = config.get('photos') or {}
    settings['photos'] = {
        k: photos.get(k) for k in ('external_backup_uuid', 'external_backup_fstype', 'external_backup_network')
    }
    with tempfile.TemporaryDirectory(prefix='neko-drives-') as work:
        path = Path(work) / 'drives.json'
        path.write_text(json.dumps(settings))
        args = ['check-drives', '--config', str(path), '--drives', ','.join(keys)]
        code, said = (ask or native_runtime.ask_runtime)(Path(__file__).resolve().parent, config, args)
    if code != 0:
        raise RuntimeError(native_runtime.reason_of(said, f'the drive check stopped (exit {code})'))


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


GIB = 1024**3


def ensure_room(folder, need, what='This restore'):
    """Refuse a restore that would leave less than 5 GB (or 5% of the drive) free where
    it goes, so a big restore never fills a drive or stops half-way. Shared by every
    restore: photos, Nextcloud files, nightly snapshots and monthly snapshots."""
    import shutil

    usage = shutil.disk_usage(folder)
    reserve = max(5 * GIB, usage.total // 20)
    if need + reserve > usage.free:
        raise RuntimeError(
            f'not enough free space: {what.lower()} needs {need / GIB:.1f} GB and '
            f'{usage.free / GIB:.1f} GB is free on that drive (5 GB or 5% is kept free). '
            'Free some space or choose a folder on another drive.'
        )


def file_count(count):
    """ "1 file", "2 files"."""
    return f'{count} file' + ('' if count == 1 else 's')


FOLDER_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class DestinationGone(RuntimeError):
    """The restore folder is not the one the restore began in any more: its drive was
    unplugged, or another drive (or folder) is at its path now."""


class Destination:
    """A restore's new folder, opened once (as the repair's Tree opens a drive). Every
    folder and file of the restore is made from that open folder, never by its path
    again, and refused when it would be on another filesystem; before each file the path
    is checked to still lead to the folder that was opened. So when the drive is
    unplugged mid-restore, or another one is mounted there, the restore stops instead of
    going on in the empty folder on the system disk or on the other drive."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = os.open(self.path, FOLDER_FLAGS)
        held = os.fstat(self.fd)
        self.held = (held.st_dev, held.st_ino)

    def same(self):
        """The restore folder's path still leads to the folder that was opened."""
        try:
            now = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return False
        return (now.st_dev, now.st_ino) == self.held

    def check(self, done):
        """Before a write: DestinationGone, saying how far the restore got, once it moved."""
        if not self.same():
            raise DestinationGone(
                f'The restore stopped after {file_count(done)}: {self.path} is not the folder it began in any more '
                '(its drive was disconnected, or another drive is mounted there now). Nothing more was written. '
                'The files restored so far stay on that drive; connect it again and restore into a new folder.'
            )

    def folder(self, parts):
        """The open folder `parts` below the restore folder, made when missing, reached from
        the restore folder's open folder without following a link. The caller closes it."""
        fd = os.dup(self.fd)
        try:
            for part in parts:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
                found = os.open(part, FOLDER_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = found
                if os.fstat(fd).st_dev != self.held[0]:
                    raise DestinationGone(f'"{part}" in {self.path} is on another drive (one mounted there)')
        except BaseException:
            os.close(fd)
            raise
        return fd

    def entry(self, parts):
        """An open handle on a file or folder already restored below the restore folder,
        reached from its open folder without following a link and never made anew, so
        owners, modes and times go to what was restored. The caller closes it."""
        fd = os.dup(self.fd)
        try:
            for depth, part in enumerate(parts, 1):
                flags = (
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK if depth == len(parts) else FOLDER_FLAGS
                )
                found = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = found
                if os.fstat(fd).st_dev != self.held[0]:
                    raise DestinationGone(f'"{part}" in {self.path} is on another drive (one mounted there)')
        except BaseException:
            os.close(fd)
            raise
        return fd

    def write_json(self, name, payload):
        """`name` in the restore folder itself, written through its open folder and swapped
        in whole (as atomic_json does by path)."""
        part = f'.{name}.part'
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part, dir_fd=self.fd)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'w') as out:
            out.write(json.dumps(payload, indent=2) + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)  # the folder too, so the new name survives a power cut (as atomic_write)

    def close(self):
        os.close(self.fd)
