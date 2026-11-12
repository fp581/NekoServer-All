"""Guided recovery: `sudo bash install.sh --recover`.

A menu over the existing restore tools, in three sections (0.50.0):

1. "What would you like to restore?": which backup, which date or version, which files
   and where they go. Asking only: nothing is written.
2. "Review this restore": the exact backup, the exact folder, what is (not) overwritten
   and which apps stop. One approval; only after it is anything written. Everything is
   restored into a NEW folder; live data is never overwritten.
3. "Check or repair a drive": a check and a repair are two actions with two approvals,
   and neither is ever run on a drive that is still mounted or on a network share.

Each restore ends with what to do next.
"""

import builtins
import contextlib
import datetime as dt
import errno
import fcntl
import functools
import gzip
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import stat
import subprocess
import sys
import zlib

import guided_setup
import nfs_exports
from neko_common import (
    OPERATION_LOCK,
    SHARED,
    hold_operation,
    cloud_files,
    media_group,
    location,
    photo_library,
    FOLDER_FLAGS,
    Destination,
    DestinationGone,  # noqa: F401 (the restores raise it; callers catch it from here)
    file_count,
    LEDGER_NAME,
    QUARANTINE,
    BackupDamaged,
    atomic_json,
    backup_files,
    copy_checked,
    copy_checked_into,
    ensure_room,
    findmnt,
    identity_matches,
    mounted_identity,
    read_ledger,
    record_ledger,
    settings_path,
    sha256_file,
    valid_ledger_path,
    usb_mount_options,
    private_mount_options,
    latest_note,
    saved_names,
    saved_versions,
)

ROOT = Path(__file__).resolve().parent
# The weekly USB backup holds this lock while it mounts, copies and unmounts the drive.
USB_LOCK = Path('/run/nekoserver-photo-backup.lock')
RESTORE = 'The restore'  # how the room check names a restore in its message
SNAPSHOT = re.compile(r'^snapshot-(\d+)-\d+$')
MONTHLY = re.compile(r'^(\d+)-\d+$')

# The recovery menu's two parts (its first question).
SECTIONS = [
    ('restore', 'Restore something from a backup'),
    ('drive', 'Check or repair a drive'),
]

# Section 1: what can be restored.
MENU = [
    ('photos', 'Photos and videos (Immich)'),
    ('cloud', 'Nextcloud files'),
    ('passwords', 'The list of app passwords (and the apps\' own password files)'),
    ('apps', 'App settings, from a nightly backup'),
    ('host', 'Steam and Samba settings, from a nightly backup'),
    ('monthly', 'The settings and databases of every app, from a monthly snapshot (for rebuilding a server)'),
]

# Section 3: a check and a repair are different actions, and so is fixing backup copies.
DRIVE_MENU = [
    ('check', 'Check a drive that stopped taking new files. A check only looks: it changes nothing on the drive'),
    ('repair', 'Repair a drive. A repair changes the drive, and damaged files can be lost'),
    ('copies', 'Fix damaged backup copies (files on the backup drives that the checksum checks found damaged)'),
]


def menu(config):
    """Section 1's list as this server has it: only what it has (gate run C10: "Nextcloud
    files" on a server without Nextcloud). With the media apps left out ("Install the
    media apps too?" answered no), the two items that only hold their backups (Immich's
    photo archive, the nightly app snapshots) are offered only while such a backup is
    there to restore: a server that had the apps keeps everything it had, its backups
    too. Nextcloud switched off is not offered (as modules/nextcloud.py runs it: on
    unless "enabled" is false)."""
    media = Path(config['media_mount'])
    # Whether the apps an item restores are part of this server (the groups of 0.50.0).
    on = {
        'photos': media_group(config, 'photos'),
        'apps': media_group(config, 'movies') or media_group(config, 'music'),
        'cloud': nextcloud_on(config),
    }
    held = {
        'photos': media / config.get('photos', {}).get('archive_dir', ARCHIVE_DIR),
        'apps': media / 'backups/media-applications',
    }

    def offered(key):
        if on.get(key, True):
            return True
        return key in held and has_entries(held[key])

    return [(key, words) for key, words in MENU if offered(key)]


def nextcloud_on(config):
    """Nextcloud is part of this server: on unless its "enabled" is false."""
    section = config.get('nextcloud')
    return not (isinstance(section, dict) and section.get('enabled') is False)


def has_entries(folder):
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


MONTHLY_DIR = 'backups/monthly'  # monthly snapshots, on the HDD
PASSWORD_LISTS = 'backups/password-lists'  # the lists setup makes, on the HDD (backup.rs seal_passwords)
USB_PASSWORDS = 'passwords'  # the weekly copy's folder on the USB drive for the newest password lists
MANIFEST = 'manifest.json'  # what every monthly snapshot folder must contain
SEALED = 'secrets.tar.cms'  # a monthly snapshot's encrypted password list
ARCHIVE_DIR = 'photos/archive'  # the photo archive's default folder on the HDD


def say(output, *lines):
    for line in lines:
        output(line)


def ask(question, prompt=input, output=print):
    """yes -> True, no -> False; Ctrl+D or Ctrl+C stops the recovery menu."""
    answer = guided_setup.yes_no(question, prompt, output)
    if answer is None:
        raise EOFError
    return answer


def asked(key, prompt=input, output=print, **values):
    """`ask` for a question of questions.py: its own words in the window and the terminal."""
    import questions

    answer = questions.yes_no(key, prompt, output, **values)
    if answer is None:
        raise EOFError
    return answer


def choose(title, options, prompt=input, output=print, leave='Back'):
    """Numbered choice; returns the chosen value or None for 0 (`leave`: "Back", or
    "Quit" in the top menu)."""
    output('\n' + title)
    for number, (_, label) in enumerate(options, 1):
        output(f'  {number}. {label}')
    output(f'  0. {leave}')
    # Enter alone takes the "(recommended)" one, as everywhere in setup (gate run B1).
    best = next((n for n, (_, label) in enumerate(options, 1) if '(recommended)' in label), None)
    asked = f'Choose a number (Enter for {best}): ' if best else 'Choose a number: '
    while True:
        answer = prompt(asked).strip()
        if answer == '' and best:
            return options[best - 1][0]
        if answer == '0':
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        output('Please type one of the numbers shown.')


# Dates are shown in this computer's own time, with its zone, as the dashboard shows
# them (the backups record UTC).
SHOWN = '%Y-%m-%d %H:%M %Z'


def when(micros):
    try:
        return dt.datetime.fromtimestamp(int(micros) / 1e6, dt.timezone.utc).astimezone().strftime(SHOWN)
    except (ValueError, OverflowError, OSError):
        return 'unknown date'


def snapshots(folder):
    """(path, label) for nightly snapshot-<time>-<pid> folders, newest first."""
    folder = Path(folder)
    found = []
    for path in folder.iterdir() if folder.is_dir() else ():
        match = SNAPSHOT.match(path.name)
        if match and path.is_dir() and not path.is_symlink() and (path / MANIFEST).is_file():
            found.append((int(match[1]), path))
    return [(path, when(stamp)) for stamp, path in sorted(found, reverse=True)]


def steam_left_out(run):
    """A nightly host snapshot made while Steam was open keeps no Steam files."""
    try:
        return bool(json.loads((Path(run) / MANIFEST).read_text()).get('left_out'))
    except (OSError, ValueError, AttributeError):
        return False


def run_time(stamp):
    """A monthly snapshot's (or password list's) time in microseconds. Setup named its
    password lists in seconds before 0.46.1: read as microseconds, so the newest list is
    the newest whatever made it (as the runtime's backup.rs snapshot_stamp)."""
    stamp = int(stamp)
    return stamp * 1_000_000 if stamp < 10**12 else stamp


def monthly_snapshots(folder, need=None):
    """(path, label) for complete monthly snapshots, newest first; `need` = required file."""
    folder = Path(folder)
    found = []
    for path in folder.iterdir() if folder.is_dir() else ():
        match = MONTHLY.match(path.name)
        if (
            match
            and path.is_dir()
            and not path.is_symlink()
            and (path / MANIFEST).is_file()
            and (need is None or (path / need).is_file())
        ):
            found.append((run_time(match[1]), path))
    return [(path, when(stamp)) for stamp, path in sorted(found, reverse=True)]


def cloud_users(usb):
    folder = Path(usb) / 'nextcloud'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if (p / 'files').is_dir() and not p.is_symlink())


def size_words(size):
    """A number of bytes as people say it: "512 MB", "3.4 GB"."""
    for unit, step in (('TB', 1 << 40), ('GB', 1 << 30)):
        if size >= step:
            return f'{size / step:.1f} {unit}'
    return f'{round(size / (1 << 20))} MB' if size >= 1 << 20 else 'less than 1 MB'


def standing_folder(path):
    """The nearest folder above `path` that exists: the drive a new folder will be on."""
    path = Path(path)
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def free_space(path):
    """The free space where a new folder `path` will be, in words; '' when it can't be told."""
    import shutil

    try:
        return size_words(shutil.disk_usage(standing_folder(path)).free)
    except OSError:
        return ''


def folder_problem(path, avoid=(), refuse=(), made_later=None):
    """Why a restore destination cannot be used, or None if it can. `avoid`: the backup
    trees: a restore inside one would become part of that backup (and of the next restore
    from it), and one around a backup would hold it. `refuse`: where a drive should be
    mounted but is not: a folder there would be on the system disk. `made_later`: the
    parent folder a restore makes itself once it is approved (the HDD's restore folder)."""
    for mount in refuse:
        mount = Path(mount)
        if path == mount or mount in path.parents:
            return f'The HDD is not mounted at {mount}, so that folder would be on the system disk. Pick a folder on another drive.'
    for tree in avoid:
        tree = Path(tree)
        if path == tree or tree in path.parents or path in tree.parents:
            return (
                f'That folder is inside (or around) a backup ({tree}); a restore there would mix with it. Pick another.'
            )
    try:
        path.lstat()
        return 'That folder already exists; restoring never overwrites anything. Pick another.'
    except FileNotFoundError:
        pass
    except OSError as exc:
        # A place this PC may not look into (a NAS folder that refuses root, say): said, not a crash.
        return f'That folder cannot be opened from this PC ({exc.strerror or exc}). Pick a folder on one of its own drives.'
    if not path.parent.is_dir() and path.parent != made_later:
        return 'The folder it would be in does not exist. Pick another.'
    return None


def typed_folder(prompt, output):
    """A typed absolute path without '..', or None when the user goes back."""
    while True:
        typed = prompt(
            'Type the full name of a NEW folder, for example /home/you/restored-photos '
            '(Enter alone goes back, with nothing restored): '
        ).strip()
        if not typed:
            return None
        candidate = Path(typed)
        if candidate.is_absolute() and '..' not in candidate.parts:
            return candidate
        output('Please type a full path starting with /, without "..".')


def backup_trees(config, usb=None):
    """Every backup a restore reads from: the photo archive and the snapshots on the HDD,
    and the USB drive."""
    media = Path(config['media_mount'])
    trees = [media / config.get('photos', {}).get('archive_dir', ARCHIVE_DIR), media / 'backups']
    return trees + ([Path(usb)] if usb else [])


FOLDER_CHOICES = [('use', 'Yes, use this folder'), ('other', 'No, I will type another folder')]


def new_folder(suggested, prompt=input, output=print, avoid=(), refuse=(), made_later=None):
    """Section 1's last field: where the restore goes. Always a folder that does not exist
    yet, inside an existing folder (or `made_later`) and outside every backup tree in
    `avoid` (and every missing drive's folder in `refuse`): nothing there can be
    overwritten. With no suggestion (None), you type one. Nothing is made here."""
    path = Path(suggested) if suggested is not None else None
    while True:
        if path is None:
            path = typed_folder(prompt, output)
            if path is None:
                return None
        problem = folder_problem(path, avoid, refuse, made_later)
        if problem is not None:
            output(problem)
            path = None
            continue
        pick = folder_offered(path, prompt, output)
        if pick != 'other':
            return path if pick == 'use' else None
        path = None


def folder_offered(path, prompt, output):
    """The folder new_folder found, shown with the free space there: 'use', 'other' or None."""
    room = free_space(path)
    say(
        output,
        '\nWhere should the restored files go?',
        'They go into a NEW folder, so nothing you have now is overwritten:',
        f'  {path}' + (f'  (free space on that drive: {room})' if room else ''),
    )
    return choose('Restore into this folder?', FOLDER_CHOICES, prompt, output)


# Inside a restore folder: the damaged copies a restore found, as they are on the backup
# (a photo or PDF with a few bad bytes often still opens in part). Never checked.
DAMAGED_AS_IS = 'damaged-as-is'
NOT_RESTORED = 'NOT-RESTORED.txt'


def retired_copies(root):
    """{path in the backup: why} for the saved copies accepted as lost in the repair menu
    (the ledger's "retired" part, written by neko_common.record_ledger)."""
    try:
        value = json.loads((Path(root) / LEDGER_NAME).read_text())
        retired = value.get('retired') if value.get('format') == 'neko-ledger-v1' else None
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(retired, dict):
        return {}
    return {
        key: row['why']
        for key, row in retired.items()
        if valid_ledger_path(key) and isinstance(row, dict) and isinstance(row.get('why'), str)
    }


class Checked:
    """Restores check every file against the backup tree's checksum ledger. A file the
    ledger has no checksum for yet is restored but counted as unverified. A file damaged
    on this backup, or missing from it, is not restored and is listed (the other backup
    may have it): one bad file never keeps all the good ones from coming back. A damaged
    copy is still kept, as it is, in the restore's damaged-as-is folder."""

    def __init__(self, ledger_root, other=None):
        self.root = Path(ledger_root) if ledger_root else None
        # Where else these files are backed up ("the HDD", "the USB drive"), if anywhere.
        self.other = other
        self.ledger_problem = None
        try:
            self.sums = read_ledger(self.root) if self.root else {}
        except RuntimeError as exc:
            # A checksum list the drive lost mid-write: the files are restored unchecked.
            self.sums, self.ledger_problem = {}, str(exc)
        self.lost = retired_copies(self.root) if self.root else {}
        # The restore folder, opened once (begin) and written only through that open
        # folder: its (device, inode) is checked again before every write (Destination).
        self.destination = None
        self.target = None
        self.verified = 0
        self.unverified = 0
        self.damaged = []
        self.missing = []
        self.uncertain = []
        self.accepted = []
        # Where what a restore finds is published for the repair option and the dashboard:
        # report(kind, {saved copy: recorded checksum}) -> True once written (note_findings).
        self.report = None
        # Whether what was found was handed to the repair option.
        self.reported = False

    def publish(self, kind, copies):
        """Hand damaged or missing copies ({saved copy: checksum or None}) to the repair option."""
        if copies and self.report is not None and self.report(kind, copies):
            self.reported = True

    def begin(self, destination):
        """The new restore folder is made and opened; files are copied into it next."""
        destination.mkdir(mode=0o700)
        self.destination = destination
        self.target = Destination(destination)

    def restored(self):
        return self.verified + self.unverified + len(self.damaged)

    def keep_damaged(self, folder, name, relative):
        """Move a damaged copy just written as `name` in the open `folder` (at `relative` in
        the restore) into the damaged-as-is folder."""
        kept = self.target.folder((DAMAGED_AS_IS, *relative.parent.parts))
        try:
            os.rename(name, relative.name, src_dir_fd=folder, dst_dir_fd=kept)
        finally:
            os.close(kept)

    def copy(self, source, target, expected=None):
        """Copy one file, checked against `expected` (a list of the files you had names
        the content it means) or else the checksum list's entry for the file."""
        key = None
        if self.root:
            try:
                key = Path(source).relative_to(self.root).as_posix()
            except ValueError:
                key = None
        expected = expected or self.sums.get(key)
        try:
            self.write(source, target, expected)
        except BackupDamaged:
            self.damaged.append(key or str(source))
            return
        if expected:
            self.verified += 1
        else:
            self.unverified += 1

    def write(self, source, target, expected):
        """Copy `source` to `target` through the restore folder's open folder (copy_checked
        without a begun restore). Raises DestinationGone, with nothing written, once the
        restore folder is no longer the one opened."""
        if self.target is None:
            copy_checked(source, target, expected)
            return
        relative = Path(target).relative_to(self.destination)
        self.target.check(self.restored())
        folder = self.target.folder(relative.parent.parts)
        try:
            with open(source, 'rb') as src:
                copy_checked_into(src, folder, relative.name, expected, source)
        except BackupDamaged:
            self.keep_damaged(folder, relative.name, relative)
            raise
        finally:
            os.close(folder)

    def accepted_as_lost(self, key, copy):
        """Why a listed file (`key`, kept as `copy` beside it) was accepted as lost in the
        repair menu, or None."""
        return self.lost.get(key) or self.lost.get(key.rsplit('/', 1)[0] + '/' + copy)

    def require_present(self, sources):
        """The files the ledger lists under the folders being restored that are not on
        this backup any more (listed, and asked about, before anything is copied)."""
        if not self.root:
            return []
        missing = []
        for _, folder in sources:
            try:
                prefix = Path(folder).relative_to(self.root).as_posix() + '/'
            except ValueError:
                continue
            here = (
                {path.relative_to(self.root).as_posix() for path, _ in backup_files(folder)}
                if Path(folder).is_dir()
                else set()
            )
            missing += sorted(k for k in self.sums if k.startswith(prefix) and k not in here)
        return missing

    def where_else(self):
        """What to do about files this backup can't give back."""
        if self.reported:
            fix = f'They are now listed for {FIX_COPIES}, which fixes them from a copy that still matches.'
        else:
            fix = f'{FIX_COPIES} fixes damaged or missing ones from a copy that still matches.'
        return (f'Get them from {self.other}, if it has them. ' if self.other else '') + fix

    def summary(self, destination=None):
        text = f'Checksums matched for {file_count(self.verified)}'
        if self.unverified:
            text += (
                f'; {file_count(self.unverified)} had no recorded checksum yet (the backup that copied it did '
                'not finish, or it was backed up before checksums were kept) and could not be checked'
            )
        text += '.'
        if self.ledger_problem:
            text += f' This backup\'s checksum list could not be used ({self.ledger_problem}), so no file was checked.'
        if self.uncertain:
            text += f' Left out ({file_count(len(self.uncertain))}): {UNCERTAIN_WHY}'
        if self.accepted:
            text += (
                f' Not restored ({file_count(len(self.accepted))}): you accepted them as lost when fixing damaged '
                'backup copies.'
            )
        left = self.damaged + self.missing
        if left:
            text += (
                f' NOT restored ({file_count(len(left))}), because this backup has them damaged ({len(self.damaged)}) '
                f'or no longer has them ({len(self.missing)}). {self.where_else()}'
            )
            if self.damaged and destination is not None:
                text += (
                    f' The damaged copies are kept as they are in {Path(destination) / DAMAGED_AS_IS} (they may '
                    'still open in part; nothing checked them).'
                )
        if (left or self.accepted) and destination is not None and self.write_list():
            text += f' Their names are in {Path(destination) / NOT_RESTORED}.'
        return text

    def write_list(self):
        """NOT-RESTORED.txt, written into the restore folder through its open folder; False
        when there is none or it is not the folder the restore began in any more."""
        if self.target is None or not self.target.same():
            return False
        fd = os.open(
            NOT_RESTORED,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.target.fd,
        )
        with os.fdopen(fd, 'w') as stream:
            stream.write(self.not_restored())
        return True

    def not_restored(self):
        return (
            ''.join(f'damaged: {k}\n' for k in self.damaged)
            + ''.join(f'missing: {k}\n' for k in self.missing)
            + ''.join(f'accepted as lost: {k} ({why})\n' for k, why in self.accepted)
        )


def every_file(sources):
    """[(folder name in the restore, backup folder, saved copy)]: every saved copy, every version."""
    return [
        (name, Path(source), path)
        for name, source in sources
        if Path(source).is_dir()
        for path, _ in backup_files(source)
    ]


def copy_new(sources, destination, checked=None, files=None):
    """Copy every file (every version) into a new destination. A missing or unreadable
    file, or one that fails its checksum, stops the restore. Returns file count. `files`:
    every_file(sources), when section 1 has looked them up already."""
    checked = checked or Checked(None)
    files = every_file(sources) if files is None else files
    ensure_room(destination.parent, sum(p.stat().st_size for _, _, p in files), RESTORE)
    checked.begin(destination)
    for done, (name, source, path) in enumerate(files):
        try:
            checked.copy(path, destination / name / path.relative_to(source))
        except OSError as exc:
            if exc.errno != errno.ENOSPC:
                raise
            raise RuntimeError(out_of_room(destination, done, len(files))) from None
    return len(files)


def out_of_room(destination, done, total):
    """The drive filled up while the restore was copying (something else wrote to it, or
    the room checked at the start was taken meanwhile): what is there stays, and the
    way to go on is said."""
    return (
        f'the drive filled up after {done} of {total} files were copied to {destination}. Those files are '
        f'there as they are. Free space on that drive (or remove {destination}) and run the restore again, '
        'or restore into a folder on another drive.'
    )


# The weekly backup never overwrites: the first copy keeps the file's own name, and
# each later edit is kept beside it as name.neko-<UTC time the backup saw it>[-n].ext.
# So the dated copies are the NEWER versions.
# A name that ends in a dot ("notes.") keeps it: its versions are notes.neko-<date>. (an
# empty extension, not none).
VERSIONED = re.compile(r'^(?P<stem>.+)\.neko-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<n>\d+))?(?:\.(?P<ext>[^.]*))?$')


def own_name(match):
    """The name a dated version belongs to: its stem and its extension (an empty one
    kept: notes. stays notes.)."""
    return match['stem'] + ('' if match['ext'] is None else '.' + match['ext'])


class Versions:
    """What a backup folder establishes about its dated copies. A dated name is a version
    of another file only when the backup said so: since 0.28.7 it records which file each
    version belongs to, and the note naming a file's current copy (since 0.24.3) names
    one of its versions. Anything else, a name that merely looks dated
    (report.neko-20250101T120000Z.txt saved in its own right) or an older version from
    before the record began, is restored as a file of its own: two files are never
    merged into one on a guess, so nothing is lost (at worst an older version comes back
    as an extra file)."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.names = saved_names(folder)
        self.recorded = saved_versions(folder)

    def original(self, name, match):
        if name in self.recorded:
            return self.recorded[name]
        guess = self.names.get(match['stem']) or own_name(match)
        return guess if noted_latest(self.folder, guess) == name else None


def live_name(key, root=None):
    """Where the live original of a backup copy is: a dated version
    (photo.neko-20261020T010000Z.jpg) is an earlier state of photo.jpg. With `root` (the
    backup tree) its folder says so; without it, the name alone is taken."""
    path = Path(key)
    record = Versions(Path(root) / path.parent) if root is not None else None
    return str(path.with_name(version_of(path.name, record)[0]))


def version_of(name, record=None):
    """(the file's own name, sort key); the first copy (no date) sorts oldest. `record`:
    what the file's backup folder says about its versions (Versions)."""
    match = VERSIONED.match(name)
    if not match:
        return name, ('', 0)
    if record is None:
        original = own_name(match)
    else:
        original = record.original(name, match)
        if original is None:
            return name, ('', 0)
    return original, (match['stamp'], int(match['n'] or 0))


def readable(stamp):
    try:
        return (
            dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).astimezone().strftime(SHOWN)
        )
    except ValueError:
        return 'first backup'


def noted_latest(folder, original):
    """The saved copy the weekly backup last saw as current (from its note), or None.
    Backups made before 0.24.3 have no notes; then the newest date wins."""
    try:
        name = (folder / latest_note(original)).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return None
    return name if name and '/' not in name else None


def version_groups(sources):
    """{path in the restore: [(sort key, saved copy, the file's own name)]}: every saved
    copy of each file, its first copy and its dated versions together."""
    groups, names = {}, {}
    for name, source in sources:
        if not Path(source).is_dir():
            continue
        for path, _ in backup_files(source):
            if path.parent not in names:
                names[path.parent] = Versions(path.parent)
            original, key = version_of(path.name, names[path.parent])
            groups.setdefault(Path(name) / path.parent.relative_to(source) / original, []).append((key, path, original))
    return groups


def latest_versions(sources):
    """[(source file, path in the restore, date of that version, how many versions)]:
    the current version of every file, restored under the file's own name."""
    groups = version_groups(sources)
    chosen = []
    for target, found in groups.items():
        found.sort()
        key, path, original = found[-1]
        noted = noted_latest(path.parent, original) if len(found) > 1 else None
        for candidate in found:
            if candidate[1].name == noted:
                key, path, _ = candidate
        chosen.append((path, target, readable(key[0]) if key[0] else 'first backup', len(found)))
    return chosen


def copy_latest(sources, destination, output=print, checked=None, chosen=None):
    """Copy only the newest version of each file, under its own name. Shows the dates
    of files that had several versions, so you can see which one you get. `chosen`:
    latest_versions(sources), when section 1 has looked them up already."""
    checked = checked or Checked(None)
    chosen = latest_versions(sources) if chosen is None else chosen
    ensure_room(destination.parent, sum(p.stat().st_size for p, _, _, _ in chosen), RESTORE)
    checked.begin(destination)
    for path, target, _, _ in chosen:
        checked.copy(path, destination / target)
    several = [(t, when, n) for _, t, when, n in chosen if n > 1]
    if several:
        output(f'{len(several)} files had more than one version; you get the newest of each:')
        for target, when, count in sorted(several)[:10]:
            output(f'  {target}: version from {when} ({count} versions on the backup)')
        if len(several) > 10:
            output(f'  ... and {len(several) - 10} more')
    return len(chosen)


# Since 0.39.6 each weekly copy that finishes writes which files you had then (their
# path in the backup and the saved copy holding each one's content then), as
# .neko-inventory/<UTC time>.json.gz at the backup's root (runtime inventory.rs). Since
# 0.39.7 (format v2) each entry also has that content's SHA-256: a saved copy can be
# moved later (into neko-replaced, when a file became a folder) and a new file can take
# its old name, so the checksum, not the name, says which content the list meant.
INVENTORY_DIR = '.neko-inventory'
INVENTORY_NAME = re.compile(r'^(\d{8}T\d{6}Z)(?:-(\d{1,9}))?\.json\.gz$')
INVENTORY_LIMIT = 1 << 30  # unpacked; a list this big is not trusted


def inventories(root):
    """[(path, date)] of a backup's lists of the files you had, newest first."""
    folder = Path(root) / INVENTORY_DIR
    found = []
    if folder.is_dir() and not folder.is_symlink():
        for path in folder.iterdir():
            match = INVENTORY_NAME.match(path.name)
            if match and path.is_file() and not path.is_symlink():
                found.append(((match[1], int(match[2] or 0)), path))
    return [(path, readable(key[0])) for key, path in sorted(found, reverse=True)]


LAST_RUN = 'last-run.json'  # which list stands for the newest run (runtime inventory.rs LAST_RUN)
STAMP = re.compile(r'^\d{8}T\d{6}Z$')


def list_gap(root, listed):
    """The date of the newest backup run when the newest list does not cover it (that
    run was cut off part-way, or its list could not be written: its note still says
    "pending"), else None. A backup from before
    0.40.1 has no note: its newest list is taken as its newest run, as before."""
    note = Path(root) / INVENTORY_DIR / LAST_RUN
    try:
        value = json.loads(note.read_text()) if note.is_file() and not note.is_symlink() else None
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict) or value.get('format') != 'neko-last-run-v1':
        return None
    at = value.get('at')
    if not isinstance(at, str) or not STAMP.match(at):
        return None
    newest = listed[0][0].name if listed else None
    complete = value.get('state', 'complete') == 'complete' and value.get('list') == newest
    return None if newest is not None and complete else readable(at)


INVENTORY_FORMATS = ('neko-inventory-v1', 'neko-inventory-v2')
SHA256 = re.compile(r'^[0-9a-f]{64}$')


def inventory_entry(value):
    """(saved copy's name, its SHA-256 or None for a v1 list) of one list entry, or None
    when the entry is not one NekoServer writes."""
    if isinstance(value, str):
        copy, sha = value, None
    elif isinstance(value, dict) and isinstance(value.get('sha256'), str) and SHA256.match(value['sha256']):
        copy, sha = value.get('copy'), value['sha256']
    else:
        return None
    if not isinstance(copy, str) or not valid_ledger_path(copy) or '/' in copy:
        return None
    return copy, sha


def read_inventory(path):
    """{path in the backup: (name of the saved copy beside it, its SHA-256 or None)}, or
    None when the list is missing, damaged or of an unknown kind (then nothing is
    restored from it). A v2 list's entries all have a checksum; a v1 list's have none."""
    try:
        with gzip.open(path, 'rb') as stream:
            raw = stream.read(INVENTORY_LIMIT + 1)
        value = json.loads(raw) if len(raw) <= INVENTORY_LIMIT else None
    except (OSError, EOFError, ValueError, zlib.error):
        return None
    kind = value.get('format') if isinstance(value, dict) else None
    files = value.get('files') if kind in INVENTORY_FORMATS else None
    if not isinstance(files, dict):
        return None
    out = {}
    for key, raw in files.items():
        entry = inventory_entry(raw)
        if not valid_ledger_path(key) or entry is None or (entry[1] is None) != (kind == 'neko-inventory-v1'):
            return None
        out[key] = entry
    return out


def plain_file(root, key):
    """A regular file at `key` under `root`, reached without following any link."""
    path = Path(root)
    for part in key.split('/'):
        path = path / part
        if path.is_symlink():
            return False
    return path.is_file()


REPLACED = 'neko-replaced'  # <user>/neko-replaced/<when>/<path in files> (runtime backup.rs)


def replaced_since(key, sums):
    """A Nextcloud file (<user>/files/<path>) whose place was taken over by a folder, or
    one of whose folders was once a file: the backup moved what stood in the way into
    <user>/neko-replaced/<when>/, so a name in an old list may now hold other content."""
    user, _, rest = key.partition('/')
    if not rest.startswith('files/'):
        return False
    inside = rest[len('files/') :]
    aside = re.compile(re.escape(user) + '/' + REPLACED + r'/[^/]+/(.+)$')
    for saved in sums:
        moved = aside.match(saved)
        if moved and (moved[1] == inside or moved[1].startswith(inside + '/') or inside.startswith(moved[1] + '/')):
            return True
    return False


class Located:
    """Where the content a list names is now. With its checksum (lists since 0.39.7): the
    saved copy beside the file when it still holds that content, else any saved copy the
    checksum list records with that checksum (a copy moved into neko-replaced, say). Never
    a copy whose content differs, even under the same name."""

    def __init__(self, root, sums):
        self.root, self.sums = Path(root), sums
        self.by_sum = {}
        for key, sha in sums.items():
            self.by_sum.setdefault(sha, []).append(key)

    def holds(self, saved, sha):
        if not plain_file(self.root, saved):
            return False
        recorded = self.sums.get(saved)
        if recorded is not None:
            return recorded == sha
        try:
            return sha256_file(self.root / saved) == sha
        except OSError:
            return False

    def find(self, key, copy, sha):
        """The saved copy (a path in the backup) holding `sha`, or None."""
        saved = key.rsplit('/', 1)[0] + '/' + copy
        if self.holds(saved, sha):
            return saved
        # The same name first (the copy moved aside keeps it), then any other.
        others = sorted(self.by_sum.get(sha, []), key=lambda k: (k.rsplit('/', 1)[-1] != copy, k))
        return next((k for k in others if k != saved and plain_file(self.root, k)), None)


def listed_place(located, key, copy, sha):
    """Where one listed file's content is: a saved copy's path in the backup, None when no
    saved copy holds it, or UNCERTAIN (a list without checksums, and a file that changed
    between file and folder since)."""
    if sha is not None:
        return located.find(key, copy, sha)
    if replaced_since(key, located.sums):
        return UNCERTAIN
    saved = key.rsplit('/', 1)[0] + '/' + copy
    return saved if plain_file(located.root, saved) else None


UNCERTAIN = object()
UNCERTAIN_WHY = (
    'they became a folder (or a folder became a file) after that backup, and its list is from before '
    '0.39.7, which keeps no checksums, so which saved copy is theirs can\'t be told. "Everything ever '
    'kept" brings back every saved copy of them.'
)


def listed_versions(sources, root, files, sums=None):
    """([(saved copy, path in the restore, checksum or None)], [paths missing from the
    backup], [paths a list without checksums can't place]) for the files an inventory
    lists under the restored folders."""
    located = Located(root, sums or {})
    chosen, missing, uncertain = [], [], []
    for name, folder in sources:
        try:
            prefix = Path(folder).relative_to(root).as_posix() + '/'
        except ValueError:
            continue
        for key, (copy, sha) in sorted(files.items()):
            if not key.startswith(prefix):
                continue
            saved = listed_place(located, key, copy, sha)
            if saved is UNCERTAIN:
                uncertain.append(key)
            elif saved is None:
                missing.append(key)
            else:
                chosen.append((Path(root) / saved, Path(name) / key[len(prefix) :], sha))
    return chosen, missing, uncertain


def copy_listed(chosen, destination, checked):
    """Copy each listed file's saved copy under the file's own name, checked against the
    checksum the list recorded (when it has one)."""
    ensure_room(destination.parent, sum(p.stat().st_size for p, _, _ in chosen), RESTORE)
    checked.begin(destination)
    for path, target, sha in chosen:
        checked.copy(path, destination / target, sha)
    return len(chosen)


NO_INVENTORY = (
    'This backup has no list yet of which files you had at each backup (it is written by each '
    'weekly copy since 0.39.6), so files you deleted or renamed come back too.'
)


def stale_list_options(listed, newest_run, output):
    """The choices when the newest backup has no list of its own: everything ever kept
    first (it has that backup's files), the older list named by its date."""
    output(
        f'\nThe newest backup ({newest_run}) did not finish: it was cut off part-way (a power cut, a '
        'restart), or its list of the files you had then could not be written. So it has no such list. '
        f'The newest complete list is from {listed[0][1]} and leaves out every file backed up after it.'
    )
    return [
        (
            'everything',
            (
                'Everything ever kept, also files you deleted or renamed (recommended: it has '
                'the newest backup\'s files)'
            ),
        ),
        ('last', f'As of {listed[0][1]} (an older list: files backed up after it are left out)'),
    ]


def restore_scope(listed, prompt, output, newest_run=None):
    """('as_of', inventory path, date) or ('everything', None, None); None to go back.
    `newest_run`: the date of a newer backup run that has no list (see list_gap)."""
    if not listed:
        output(NO_INVENTORY)
        return 'everything', None, None
    earlier = newest_with_files(listed) if not newest_run else None
    if newest_run:
        options = stale_list_options(listed, newest_run, output)
    elif earlier:
        output(
            f'The last backup ({listed[0][1]}) lists no files: the folder was empty then, as after a new '
            f'drive. The last backup that lists files is the one of {earlier[1]}.'
        )
        options = [
            ('earlier', f'As of the backup of {earlier[1]}: the last one that lists files (recommended)'),
            ('last', f'As of the last backup ({listed[0][1]}): nothing, as it lists no files'),
        ]
    else:
        options = [('last', f'As of the last backup ({listed[0][1]}): only the files you had then (recommended)')]
    if len(listed) > 1:
        options.append(('date', 'As of an earlier backup: choose the date'))
    if not newest_run:
        options.append(('everything', 'Everything ever kept, also files you deleted or renamed'))
    pick = choose('Which files?', options, prompt, output)
    if pick == 'date':
        path = choose('As of which backup?', listed[1:], prompt, output)
        return None if path is None else ('as_of', path, dict(listed)[path])
    if pick == 'last':
        return 'as_of', listed[0][0], listed[0][1]
    if pick == 'earlier' and earlier:
        return 'as_of', earlier[0], earlier[1]
    return None if pick is None else ('everything', None, None)


def newest_with_files(listed):
    """The newest list that names any file, when the newest of all names none (the files
    folder was empty at that backup: a new HDD, say, before anything was put back);
    (path, date), else None."""
    if read_inventory(listed[0][0]):
        return None
    return next(((path, date) for path, date in listed[1:] if read_inventory(path)), None)


COPYING = 'Copying... this can take a while for a big backup.'
# Where a restored password list goes unless another folder is named: root's own home,
# which only root can open (the restore runs as root).
PASSWORDS_HOME = Path('/root')
# What the recovery menu calls the repair of backup copies (DRIVE_MENU), where a message names it.
FIX_COPIES = '"Fix damaged backup copies" (in the recovery menu, under "Check or repair a drive")'


def recorded_copy(key, copy, sha, sums):
    """(the saved copy, its recorded checksum) of a listed file (`key`, kept as `copy`
    beside it, with the list's checksum `sha` or None) that is missing from the backup;
    (None, None) when no saved copy is known to have held it (its name holds other
    content now, and no other saved copy has that checksum)."""
    saved = key.rsplit('/', 1)[0] + '/' + copy
    if sha is None:
        return saved, sums.get(saved)
    if sums.get(saved) not in (None, sha):
        saved = next((k for k in sorted(sums) if sums[k] == sha), None)
    return (saved, sha) if saved else (None, None)


def missing_copies(missing, files_listed, sums):
    """{saved copy: recorded checksum} for the listed files missing from the backup."""
    pairs = (recorded_copy(key, *files_listed[key], sums) for key in missing)
    return {copy: sha for copy, sha in pairs if copy is not None}


ACCEPTED = ', accepted as lost in the recovery menu'  # how accept_lost ends its reason
KEPT_ASIDE = re.compile(r'kept in ' + re.escape(QUARANTINE) + r'/([0-9TZ]+)')


def lost_note(root, key, why):
    """What became of a saved copy accepted as lost: where its damaged copy still is."""
    aside = KEPT_ASIDE.search(why)
    if aside is None:
        return 'it was already missing'
    return f'its damaged copy is still on the backup, as it is: {Path(root) / QUARANTINE / aside[1] / key}'


def accepted_check(missing, checked, files_listed, output):
    """Take the files accepted as lost in the repair menu out of `missing`: they were
    given up on purpose, so they are named, not asked about as new damage."""
    accepted, rest = [], []
    for key in missing:
        copy = files_listed[key][0]
        why = checked.accepted_as_lost(key, copy)
        if why is None:
            rest.append(key)
        else:
            saved = key.rsplit('/', 1)[0] + '/' + copy
            accepted.append((key, why.removesuffix(ACCEPTED) + '; ' + lost_note(checked.root, saved, why)))
    if accepted:
        output(
            f'Not restored, as you accepted them as lost when fixing damaged backup copies: {file_count(len(accepted))} '
            f'(for example {accepted[0][0]}: {accepted[0][1]}).'
        )
    checked.accepted = accepted
    return rest


VERSION_CHOICES = [
    ('latest', 'Only the newest version of each file (recommended)'),
    ('all', 'Every version of each file, the older ones too (takes longer and needs more room)'),
]


class Files:
    """What section 1 settled about a restore of files (photos, Nextcloud files): which
    files, in which versions, and what this backup can no longer give back. Looking only:
    nothing is copied before `copy`, which section 2's approval comes before."""

    def __init__(self, sources, checked):
        self.sources, self.checked = sources, checked
        self.date = None  # the backup whose list of files is restored ("as of"), else None
        self.latest = True  # only the newest version of each file
        self.picked = []  # what is copied, as the copying function for this choice takes it
        self.lost = {}  # {saved copy: recorded checksum} missing from this backup, for the repair
        self.notes = []  # what was left out on purpose, said in the review

    def as_of(self, inventory, output):
        """Only the files a backup's list names, each as it was then. False: the list is unusable."""
        path, self.date = inventory
        files = read_inventory(path)
        if files is None:
            output(f'The list of files from {self.date} could not be read, so everything ever kept is offered instead.')
            self.date = None
            return False
        checked = self.checked
        self.picked, missing, checked.uncertain = listed_versions(self.sources, checked.root, files, checked.sums)
        if checked.uncertain:
            some = ', '.join(checked.uncertain[:5])
            self.notes.append(f'Left out ({file_count(len(checked.uncertain))}, for example {some}): {UNCERTAIN_WHY}')
        checked.missing = accepted_check(missing, checked, files, self.notes.append)
        self.lost = missing_copies(checked.missing, files, checked.sums)
        return True

    def everything(self, latest):
        """Every file this backup ever kept: the newest version of each, or every version."""
        checked = self.checked
        checked.missing = checked.require_present(self.sources)
        self.lost = {key: checked.sums[key] for key in checked.missing}
        self.latest = latest
        self.picked = latest_versions(self.sources) if latest else every_file(self.sources)

    def copies(self):
        """The saved copies that will be read."""
        if self.date is not None or self.latest:
            return [row[0] for row in self.picked]
        return [path for _, _, path in self.picked]

    def size(self):
        return sum(path.stat().st_size for path in self.copies())

    def which(self):
        """The files that come back, in words, for the review."""
        count = file_count(len(self.picked))
        if self.date is not None:
            return f'the {count} you had at the backup of {self.date}, each as it was then'
        if self.latest:
            return f'everything this backup ever kept, also files you deleted or renamed: the newest version of each ({count})'
        return f'everything this backup ever kept, with every older version of each file ({count})'

    def missing(self):
        """What this backup lost, for the review: said before the approval, never asked apart."""
        missing, other = self.checked.missing, self.checked.other
        if not missing:
            return None
        also = ''
        if other:
            also = f' {other[0].upper() + other[1:]} may still have {"it" if len(missing) == 1 else "them"}.'
        return (
            f'{file_count(len(missing))} backed up and checked before {"is" if len(missing) == 1 else "are"} not on '
            f'this backup any more (for example {", ".join(missing[:5])}), and cannot come back from it.' + also
        )

    def not_restored(self):
        """What to do next when the review was not approved."""
        if not self.checked.missing:
            return 'Nothing was restored.'
        other = self.checked.other
        return f'Nothing was restored. For the missing files: {f"use {other} instead, or " if other else ""}choose {FIX_COPIES}.'

    def copy(self, target, output):
        """The copying itself, after the approval. Returns how many files came back whole."""
        checked = self.checked
        if self.date is not None:
            output(f'Restoring the {file_count(len(self.picked))} you had at the backup of {self.date}. {COPYING}')
            count = copy_listed(self.picked, target, checked)
        else:
            output(COPYING)
            copier = copy_latest if self.latest else copy_new
            arguments = (output, checked, self.picked) if self.latest else (checked, self.picked)
            count = copier(self.sources, target, *arguments)
        return count - len(checked.damaged)


def choose_files(sources, prompt, output, ledger_root=None, other=None, report=None):
    """Section 1 for files: the files you had at a backup (when the backup lists them;
    recommended), else everything ever kept, in its newest version or in every version.
    Every file will be checked against the checksum ledger at `ledger_root`; `other`:
    where else these files are backed up, if anywhere; `report`: where damaged and
    missing copies are published for the repair option (Checked.report). Returns the
    choice (Files), or None when you went back. Nothing is written."""
    checked = Checked(ledger_root, other)
    checked.report = report
    chosen = Files(sources, checked)
    listed = inventories(ledger_root) if ledger_root else []
    scope = restore_scope(listed, prompt, output, list_gap(ledger_root, listed) if ledger_root else None)
    if scope is None:
        return None
    if scope[0] == 'as_of' and chosen.as_of(scope[1:], output):
        return chosen
    versions = choose('Which versions of each file?', VERSION_CHOICES, prompt, output)
    if versions is None:
        return None
    chosen.everything(versions == 'latest')
    return chosen


NOTHING_CHANGED = 'Nothing was restored and nothing was changed.'


def review(rows, prompt, output):
    """Section 2: everything section 1 settled, shown once, then the one approval. `rows`:
    [(what the line is about, its words)]. True only for a typed yes: Enter alone asks
    again. Nothing has been written when this is asked."""
    output('\nReview this restore')
    for label, text in rows:
        output(f'  {label + ":":<14}{text}')
    return asked('restore_approve', prompt, output)


# The review's last lines for a restore that stops no app.
APPS_KEEP_RUNNING = ('Apps', 'none is stopped. They keep running while this is restored.')


def files_approved(config, what, source, chosen, target, untouched, prompt, output):
    """Section 2 for a restore of files; True once approved. `untouched`: the live files
    that stay as they are. A restore with no room where it goes stops here, before the
    question. What this backup lost is listed for the repair option whatever the answer
    (after it: nothing is written before)."""
    size = chosen.size()
    ensure_room(standing_folder(target), size, RESTORE)
    rows = [
        ('Restore', what),
        ('From', source),
        ('Which files', chosen.which()),
        ('Size', f'{size_words(size)} (free space where it goes: {free_space(target)})'),
        ('Into', f'{target} (a new folder)'),
        *([('Missing', chosen.missing())] if chosen.missing() else []),
        *(('Note', note) for note in chosen.notes),
        ('Overwritten', f'nothing. {untouched}'),
        APPS_KEEP_RUNNING,
    ]
    approved = review(rows, prompt, output)
    chosen.checked.publish(MISSING, chosen.lost)
    if not approved:
        output(chosen.not_restored())
        return False
    make_home(config, target)
    return True


VERSIONS_EXPLAINED = (
    'Every version was copied. A file\'s own name is its FIRST backed-up version; '
    'copies named ".neko-<date>" are LATER edits, saved on the date in the name. '
    'An edit can be undone later, so the latest date is not always the current '
    'file: "newest only" restores the one the backup last saw.'
)


def restore_home(config):
    """The HDD folder that holds restores (made by `restored`, once a restore is approved)."""
    return Path(config['media_mount']) / 'restored'


def restored(config):
    """The HDD folder that holds restores, created privately on first use."""
    folder = restore_home(config)
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def make_home(config, target):
    """After the approval: the HDD's restore folder is made, when the restore goes into it."""
    if Path(target).parent == restore_home(config):
        restored(config)


def hdd_ready(config):
    """The HDD is mounted where the settings say, and it is the drive they name. When it
    has failed or is unplugged, its folder is a plain one on the system disk."""
    return identity_matches(Path(config['media_mount']), config.get('storage', {}).get('media_hdd', {}))


HDD_MISSING = (
    'The HDD is not mounted at {mount} (or another drive is there), so the usual restore folder '
    'there would be on the system disk and could fill it up. Connect the HDD (or restart) and '
    'try again, or type a folder on another drive with enough room.'
)


def restore_target(config, name, prompt, output, usb=None):
    """A NEW folder for a restore: suggested in the HDD's restore folder, but only while
    the HDD is mounted and is the right drive (never on the system disk under its empty
    folder); otherwise you type one on another drive. Nothing is made yet (make_home)."""
    avoid = backup_trees(config, usb)
    if hdd_ready(config):
        home = restore_home(config)
        return new_folder(unused(home / name), prompt, output, avoid, made_later=home)
    output(HDD_MISSING.format(mount=config['media_mount']))
    return new_folder(None, prompt, output, avoid, refuse=(Path(config['media_mount']),))


def stamp():
    return dt.datetime.now().strftime('%Y%m%d-%H%M%S')


def unused(path):
    """`path`, or path-2, path-3... when it exists already: a restore made again soon
    after (one file was missing, say) is offered a folder of its own, not a refusal."""
    path = candidate = Path(path)
    number = 1
    while os.path.lexists(candidate):  # False when it can't be looked at: new_folder then says so
        number += 1
        candidate = path.with_name(f'{path.name}-{number}')
    return candidate


def nas_disk_of(device, run=subprocess.run):
    """The WWN of the disk under `device` (lsblk, from the device down to its disk), or ''."""
    shown = run(
        ['lsblk', '-n', '-s', '-o', 'TYPE,WWN', str(device)], capture_output=True, text=True, timeout=30, check=False
    )
    for line in (shown.stdout or '').splitlines():
        kind, _, wwn = line.strip().partition(' ')
        if kind == 'disk':
            return wwn.strip()
    return ''


class Usb:
    """Mount the backup USB drive (read-only unless `writable`), if it is not mounted."""

    def __init__(self, config, writable=False):
        self.writable = writable
        self.mount = Path(config.get('external_backup_mount', '/mnt/nekoserver-photos'))
        photos = config.get('photos', {})
        self.uuid = photos.get('external_backup_uuid', '')
        self.fstype = photos.get('external_backup_fstype', 'ext4')
        # On a NAS: the NAS disk setup chose (photo_backup.same_nas_disk), as the weekly copy checks.
        self.wwn = (photos.get('external_backup_network') or {}).get('wwn')
        # A folder the NAS shares over NFS (nfs_exports.py): its address, else None.
        self.export = nfs_exports.export_of({'network': photos.get('external_backup_network')})
        self.mounted_here = False
        self.lock = None

    def __enter__(self):
        if not self.uuid:
            return None
        # Never share the drive with a running weekly backup: it unmounts when done.
        fd = os.open(USB_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError(
                'the weekly backup is using the USB drive right now. '
                'Try again when it has finished (usually within an hour)'
            ) from None
        self.lock = fd
        try:
            return self.open()
        except BaseException:
            # A refused or failed open still unmounts what it mounted and frees the drive.
            self.__exit__()
            raise

    def same_nas_disk(self, device):
        """On a NAS: `device` (the one about to be mounted, or the one mounted) is on the NAS
        disk setup chose (its WWN), never a restored or copied LUN with the same filesystem
        UUID. The device itself is looked at, not the UUID's link: with two LUNs of one
        UUID, that link names only one of them (the review of 0.47.0)."""
        if self.wwn and nas_disk_of(device) != self.wwn:
            raise RuntimeError(
                'the USB backup drive has its UUID but is on another NAS disk than setup chose (a restored or '
                'copied LUN, most likely); run  sudo bash install.sh  to choose which one to use'
            )

    def open_shared_folder(self):
        """The backup drive is a folder the NAS shares: mounted by its address when it is
        not (read-only unless `writable`), and used only when what is mounted there is
        that folder. None when the NAS does not answer."""
        if findmnt('TARGET', self.mount) is None:
            self.mount.mkdir(parents=True, exist_ok=True)
            options = nfs_exports.options(self.fstype) + (',rw' if self.writable else ',ro')
            mounted = subprocess.run(
                ['mount', '-t', 'nfs', '-o', options, self.export, str(self.mount)],
                capture_output=True,
                timeout=120,
                check=False,
            )
            if mounted.returncode != 0:
                return None
            self.mounted_here = True
        return self.mount if mounted_identity(self.mount) == (self.uuid, self.fstype, '') else None

    def open(self):
        if self.export:
            return self.open_shared_folder()
        try:
            listed = subprocess.run(
                ['findmnt', '-n', '--mountpoint', str(self.mount)], capture_output=True, check=False, timeout=30
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                'this PC did not say within 30 seconds whether the USB drive is mounted at ' + str(self.mount) + '. '
                'Check that the drive is plugged in, then try again'
            ) from None
        if listed.returncode != 0:
            link = Path('/dev/disk/by-uuid') / self.uuid
            if not link.exists():
                return None
            device = os.path.realpath(link)
            self.same_nas_disk(device)  # before it is mounted: the very device mounted next
            self.mount.mkdir(parents=True, exist_ok=True)
            options = usb_mount_options(self.fstype, self.writable)
            if (
                subprocess.run(
                    ['mount', '-o', options, device, str(self.mount)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                ).returncode
                != 0
            ):
                return None
            self.mounted_here = True
        found = (findmnt('UUID,OPTIONS,SOURCE', self.mount) or '').split()
        if len(found) != 3 or found[0].lower() != self.uuid.lower():
            return None
        # Mounted already (by the weekly copy, by hand): the device mounted there is checked
        # all the same (the review of 0.46.3: a copied LUN mounted there was taken as it was).
        self.same_nas_disk(found[2].partition('[')[0])
        if not private_mount_options(found[1], self.fstype):
            raise RuntimeError(
                'the USB drive is mounted so that other accounts on this computer could read '
                'it. Unmount it (sudo umount ' + str(self.mount) + ') and try again: it is then mounted privately'
            )
        return self.mount

    def __exit__(self, *exc):
        try:
            if self.mounted_here:
                self.mounted_here = False
                subprocess.run(['umount', str(self.mount)], capture_output=True, timeout=60, check=False)
        finally:
            if self.lock is not None:
                os.close(self.lock)
                self.lock = None


def tool(name, *args):
    """Run one of NekoServer's restore tools; True on success."""
    return subprocess.run([sys.executable, str(ROOT / name), *args], check=False).returncode == 0


def restore_photos(config, prompt, output):
    media = Path(config['media_mount'])
    archive = media / config.get('photos', {}).get('archive_dir', ARCHIVE_DIR)
    with Usb(config) as usb:
        places = {'hdd': f'The HDD: the weekly copy in {archive}', 'usb': f'The USB drive: the weekly copy in {usb}'}
        sources = [('hdd', places['hdd'])] if archive.is_dir() else []
        if usb and ((usb / 'upload').is_dir() or (usb / 'library').is_dir()):
            sources.append(('usb', places['usb']))
        if not sources:
            output('No photo backup found on the HDD or the USB drive (is the USB drive plugged in?).')
            return
        where = choose('Which backup of your photos?', sources, prompt, output)
        if where is None:
            return
        base = archive if where == 'hdd' else usb
        sources = [('upload', base / 'upload'), ('library', base / 'library')]
        # What a file that became a folder (or back) replaced, kept at the archive's top.
        if (base / REPLACED).is_dir() and not (base / REPLACED).is_symlink():
            sources.append((REPLACED_FOLDER, base / REPLACED))
        section = 'archive' if where == 'hdd' else 'usb_photos'
        chosen = choose_files(
            sources,
            prompt,
            output,
            ledger_root=base,
            other='the USB drive' if where == 'hdd' else 'the HDD',
            report=functools.partial(note_findings, config, section),
        )
        target = restore_target(config, f'photos-{stamp()}', prompt, output, usb) if chosen else None
        if target is None:
            output(NOTHING_CHANGED)
            return
        untouched = f'Your Immich library ({photo_library(config)}) stays as it is.'
        if not files_approved(config, MENU[0][1], places[where], chosen, target, untouched, prompt, output):
            return
        count, latest, checked = chosen.copy(target, output), chosen.latest, chosen.checked
        report_restore(checked, target, output)
    say(
        output,
        f'\nDone: {file_count(count)} copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        *handed_over(config, target, checked),
        'To get them back into Immich: open Immich in a browser, click "Upload" and choose',
        'files from that folder (for thousands of files, the Immich command-line uploader is',
        'faster: see https://immich.app/docs/features/command-line-interface).',
        f'Your current Immich library ({photo_library(config)}) was not touched.',
        'Rebuilding Immich after losing its drive (RECOVERY.md, "Rebuild Immich from the',
        'USB drive")? Then put both folders into the library Immich uses instead:',
        f'  sudo cp -a {shlex.quote(str(target / "upload"))} {shlex.quote(str(target / "library"))} '
        f'{shlex.quote(str(photo_library(config)))}/',
    )


def report_restore(checked, target, output):
    """Damage a restore found goes where the checks put theirs (so the repair option and
    the dashboard see it; missing copies went there before the copying began), then the
    restore's summary is said."""
    checked.publish(DAMAGED, {key: checked.sums.get(key) for key in checked.damaged})
    output(checked.summary(target))


# What the checksum checks found (written by the runtime, backup.rs save_integrity).
INTEGRITY_REPORT = 'reports/integrity.json'
INTEGRITY_FORMAT = 'neko-integrity-v1'
DAMAGED = 'damaged'
MISSING = 'missing'


def ssd_ready(config):
    """The SSD is mounted where the settings say, and it is the drive they name."""
    return identity_matches(Path(config['data_mount']), config.get('storage', {}).get('data_ssd', {}))


def holder_ready(config, what):
    """The drive that holds `what` (photos, cloud: neko_common.files_root) is mounted where
    the settings say, and is the drive they name."""
    return ssd_ready(config) if location(config, what) == 'ssd' else hdd_ready(config)


def note_findings(config, section, kind, copies):
    """Add the damaged or missing copies (`kind`) a restore found ({saved copy: its
    recorded checksum or None}) to reports/integrity.json under `section` (the backup
    tree's checks: archive, usb_photos, usb_nextcloud), as a quick check adds what it
    finds: the repair option works from that list, and the dashboard shows it. Their
    checksums are kept beside the list ("checksums"), for a copy no ledger records. The
    next full check of that tree replaces the list. True once written."""
    path = Path(config['data_mount']) / INTEGRITY_REPORT
    if not ssd_ready(config) or not path.parent.is_dir():
        return False
    try:
        value = json.loads(path.read_text()) if path.exists() else {'format': INTEGRITY_FORMAT, 'sections': {}}
    except (OSError, ValueError):
        return False  # never written over: the runtime may be the one that can read it
    sections = value.get('sections') if isinstance(value, dict) and value.get('format') == INTEGRITY_FORMAT else None
    if not isinstance(sections, dict):
        return False
    entry = sections.get(section)
    if not isinstance(entry, dict):
        entry = sections[section] = {}
    rows = [k for k in entry.get(kind, []) if isinstance(k, str)]
    added = sorted({k for k in copies if valid_ledger_path(k)} - set(rows))
    entry[kind] = rows + added
    counts = entry.setdefault('counts', {})
    counts[kind] = max(counts.get(kind, 0), len(rows)) + len(added)
    sums = entry[CHECKSUMS] if isinstance(entry.get(CHECKSUMS), dict) else {}
    sums.update(
        (k, sha) for k, sha in copies.items() if valid_ledger_path(k) and isinstance(sha, str) and SHA256.match(sha)
    )
    entry[CHECKSUMS] = sums
    entry['found_by_restore_at'] = dt.datetime.now(dt.timezone.utc).timestamp()
    atomic_json(path, value)
    return True


def owner(config):
    """Whose restored photos and files these are: the account that ran sudo, else the
    account the settings name for media (media.uid and media.gid)."""
    uid, gid = os.environ.get('SUDO_UID', ''), os.environ.get('SUDO_GID', '')
    if uid.isdigit() and gid.isdigit() and int(uid) != 0:
        return int(uid), int(gid)
    media = config.get('media', {})
    return int(media.get('uid', 1000)), int(media.get('gid', 1000))


def give_to(folder, uid, gid, dir_fd=None):
    """Make a restore folder and everything in it belong to `uid`:`gid`. Walked through
    open folders (os.fwalk), never through a link: nothing outside it changes owner.
    `dir_fd`: the restore folder already open (Destination): walked from it, not its path."""
    walk = (
        os.fwalk('.', dir_fd=dir_fd, follow_symlinks=False)
        if dir_fd is not None
        else os.fwalk(folder, follow_symlinks=False)
    )
    for _, dirs, names, fd in walk:
        os.fchown(fd, uid, gid)
        for name in dirs + names:
            os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)


def reachable(path, uid, gid):
    """Whether account `uid` (in group `gid`) can get through every folder above `path`."""
    for parent in Path(path).parents:
        info = parent.stat()
        if info.st_uid == uid:
            bit = 0o100
        elif info.st_gid == gid:
            bit = 0o010
        else:
            bit = 0o001
        if not info.st_mode & bit:
            return False
    return True


def account(uid):
    """(name, home folder) of account `uid`."""
    try:
        found = pwd.getpwuid(uid)
        return found.pw_name, found.pw_dir
    except KeyError:
        return str(uid), '/home'


def handed_over(config, target, checked=None):
    """Restored photos and files are for you to open (and upload) from your own account,
    which can't read root's private files: they are given to that account. The HDD's
    restore folder lets that account's group in (other restores in it stay private to
    root). Returns the lines that say so, or how to get at them when that did not work.
    `checked`: the restore (Checked), whose open folder is walked, and only while it is
    still the folder the restore was made in."""
    uid, gid = owner(config)
    name, home = account(uid)
    held = checked.target if checked is not None else None
    if held is not None and not held.same():
        return [
            (
                f'{target} is not the folder the restore was made in any more (its drive was disconnected or '
                'changed), so nothing in it was handed over to your account.'
            )
        ]
    try:
        give_to(target, uid, gid, held.fd if held is not None else None)
        if Path(target).parent == Path(config['media_mount']) / 'restored':
            os.chown(Path(target).parent, 0, gid)
            os.chmod(Path(target).parent, 0o750)
    except OSError as exc:
        return [
            f'They belong to root ({exc.strerror}), so your account can\'t open them yet. Run:',
            f'  sudo chown -R {name}: "{target}"',
        ]
    if not reachable(target, uid, gid):
        return [
            f'The folder now belongs to {name}, but that account can\'t get to where it is. Move it home:',
            f'  sudo mv "{target}" "{home}/"',
        ]
    return [f'The folder and its files now belong to {name}, so you can open them from your desktop.']


# Beside a user's backed-up files: what a later change of shape replaced (a file that
# became a folder, or a folder that became a file), under the date it was set aside and
# its own path (backup.rs REPLACED).
REPLACED = 'neko-replaced'
REPLACED_FOLDER = 'replaced earlier'  # its name in a restore


def cloud_sources(usb, user):
    """The user's files, and what was set aside when a file became a folder (or back)."""
    sources = [('files', usb / 'nextcloud' / user / 'files')]
    replaced = usb / 'nextcloud' / user / REPLACED
    if replaced.is_dir() and not replaced.is_symlink():
        sources.append((REPLACED_FOLDER, replaced))
    return sources


def restore_cloud(config, prompt, output):
    with Usb(config) as usb:
        users = cloud_users(usb) if usb else []
        if not users:
            output('No Nextcloud copy found on the USB drive (is it plugged in?).')
            return
        output(f'\nNextcloud files are backed up in one place: the weekly copy on the USB drive ({usb / "nextcloud"}).')
        user = choose('Whose Nextcloud files?', [(u, u) for u in users], prompt, output)
        if user is None:
            return
        sources = cloud_sources(usb, user)
        # The USB drive holds the only copy of Nextcloud files: no "other backup".
        chosen = choose_files(
            sources,
            prompt,
            output,
            ledger_root=usb / 'nextcloud',
            report=functools.partial(note_findings, config, 'usb_nextcloud'),
        )
        target = restore_target(config, f'nextcloud-{user}-{stamp()}', prompt, output, usb) if chosen else None
        if target is None:
            output(NOTHING_CHANGED)
            return
        source = f'The USB drive: the weekly copy of {user}\'s files in {usb / "nextcloud" / user}'
        untouched = f'The files in Nextcloud now ({cloud_files(config)}) stay as they are.'
        if not files_approved(config, f'{MENU[1][1]} of {user}', source, chosen, target, untouched, prompt, output):
            return
        count, latest, checked = chosen.copy(target, output), chosen.latest, chosen.checked
        report_restore(checked, target, output)
    ready = handed_over(config, target, checked)
    if encrypted_by_nextcloud(target):
        say(
            output,
            f'\nDone: {file_count(count)} copied to {target}, but they are ENCRYPTED by Nextcloud\'s',
            'server-side encryption, and the keys to read them are not in this backup.',
            'Uploading them would not make them readable. They open only on the Nextcloud',
            'server that encrypted them (with its data folder and keys). Your current Nextcloud',
            'files were not touched.',
        )
        return
    say(
        output,
        f'\nDone: {file_count(count)} copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        *(
            ()
            if not (target / REPLACED_FOLDER).is_dir()
            else (
                '"replaced earlier" holds what was in the way when a file became a folder (or a folder',
                'a file), in a folder named after the date it was set aside.',
            )
        ),
        *ready,
        'To put them back: open Nextcloud in a browser (or the desktop app) and upload the',
        'files you need. Your current Nextcloud files were not touched.',
    )


def encrypted_by_nextcloud(folder, sample=200):
    """Nextcloud's server-side encryption starts each file with an "HBEGIN:" header."""
    for path in sorted(p for p in Path(folder).rglob('*') if p.is_file() and not p.is_symlink())[:sample]:
        try:
            with path.open('rb') as stream:
                if stream.read(7) == b'HBEGIN:':
                    return True
        except OSError:
            continue
    return False


def plain_password_list(run):
    """A monthly snapshot made without a backup key keeps the password list in data.tar
    (its manifest says "unencrypted_in_data_tar")."""
    try:
        manifest = json.loads((Path(run) / MANIFEST).read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get('password_files') == 'unencrypted_in_data_tar'
        and 'secrets/accounts' in (manifest.get('paths') or {}).get('data', [])
        and (Path(run) / 'data.tar').is_file()
    )


def archive_problem(run, name):
    """Why a monthly snapshot's `<name>.tar` can't be trusted (its checksum is missing or
    doesn't match the one recorded in its manifest), or None."""
    try:
        expected = json.loads((Path(run) / MANIFEST).read_text())['sha256'][name]
    except (OSError, ValueError, KeyError, TypeError):
        expected = None
    if not isinstance(expected, str) or len(expected) != 64:
        return f'This snapshot has no checksum for its {name}.tar.'
    with (Path(run) / (name + '.tar')).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            return f'The {name}.tar in this snapshot is damaged (checksum mismatch).'
    return None


def password_snapshots(config):
    """(path, label) of the password lists on the HDD, newest first: the monthly
    snapshots' and the ones setup makes after each run (so there is one before the first
    monthly snapshot, and a newer one when passwords changed since)."""
    media = Path(config['media_mount'])
    monthly = [
        (p, d + ('' if (p / SEALED).is_file() else ' (not encrypted: made without a backup key)'))
        for p, d in monthly_snapshots(media / MONTHLY_DIR)
        if (p / SEALED).is_file() or plain_password_list(p)
    ]
    made = [(p, d + ' (made by setup)') for p, d in monthly_snapshots(media / PASSWORD_LISTS, need=SEALED)]
    return newest_first(monthly + made)


def newest_first(runs):
    """(path, label) runs from several folders, newest first by the time in their names."""
    return sorted(runs, key=lambda run: run_time(MONTHLY.match(run[0].name)[1]), reverse=True)


def usb_password_lists(usb):
    """(path, label) of the password lists the weekly copy keeps on the USB drive (the
    newest monthly snapshots' encrypted lists, with what opens them), newest first."""
    if not usb:
        return []
    return [(p, d + ' (copy on the USB drive)') for p, d in monthly_snapshots(Path(usb) / USB_PASSWORDS, need=SEALED)]


def restore_passwords(config, prompt, output):
    """Every password list there is, newest first: the HDD's (the monthly snapshots' and
    setup's) and the USB drive's copies, when it is plugged in (the review of 0.46.1: with
    any list on the HDD, a newer one on the USB drive was never offered)."""
    hdd = password_snapshots(config)
    try:
        with Usb(config) as usb:
            runs = newest_first(hdd + usb_password_lists(usb))
            if runs:
                return restore_password_list(config, runs, prompt, output)
    except RuntimeError as exc:
        # The USB drive busy (the weekly copy) or refused: the HDD's lists all the same
        # (the audit of 0.46.2).
        output(f'The USB drive is not looked at: {exc}.')
        if hdd:
            return restore_password_list(config, hdd, prompt, output)
    output('No password list on the HDD, nor on the USB drive (or it is not plugged in).')


def restore_password_list(config, runs, prompt, output):
    run = choose('Which password list? The newest is first.', [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = new_folder(
        unused(PASSWORDS_HOME / f'nekoserver-passwords-{stamp()}'), prompt, output, backup_trees(config)
    )
    if target is None:
        output(NOTHING_CHANGED)
        return
    sealed = (run / SEALED).is_file()
    rows = [
        ('Restore', MENU[2][1]),
        ('From', f'{run} (made {dict(runs)[run]})'),
        ('Into', f'{target} (a new folder that only the administrator, root, can open)'),
        (
            'Password',
            'it asks for the backup password chosen during setup (for backups made before 0.28.5: the sudo '
            'password you had then)'
            if sealed
            else 'none is asked: this list was saved without a backup key',
        ),
        ('Overwritten', 'nothing. No app\'s password is changed; the list is only unpacked for you to read.'),
        APPS_KEEP_RUNNING,
    ]
    if not review(rows, prompt, output):
        output(NOTHING_CHANGED)
        return
    if not sealed:
        # The archive is checked against the checksum recorded when the snapshot was
        # made before anything is taken out of it: a changed byte would change a password.
        problem = archive_problem(run, 'data')
        if problem:
            output(problem + ' Nothing was restored. Use another monthly snapshot.')
            return
        target.mkdir(mode=0o700)
        result = subprocess.run(
            [
                'tar',
                '--extract',
                '--file',
                str(run / 'data.tar'),
                '--directory',
                str(target),
                '--no-same-owner',
                'secrets/accounts',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            output('Could not take the password list out of that snapshot: ' + (result.stderr.strip() or 'tar failed'))
            return
        say(output, f'\nDone. Open it with:  sudo cat {target}/secrets/accounts/APP-CREDENTIALS.txt')
        return
    if tool('backup_secrets.py', '--restore', str(run), '--to', str(target)):
        say(
            output,
            f'\nDone. Open it with:  sudo cat {target}/secrets/accounts/APP-CREDENTIALS.txt',
            f'The apps\' own password files are there too: {target}/compose/<app>/.env and',
            f'{target}/appdata/nextcloud/html/config/config.php (for rebuilding from a monthly snapshot).',
        )


def restore_nightly(config, kind, prompt, output):
    folder, script, label = (
        ('backups/media-applications', 'app-recovery.py', 'app settings')
        if kind == 'apps'
        else ('backups/host-user', 'host-recovery.py', 'Steam and Samba settings')
    )
    runs = snapshots(Path(config['media_mount']) / folder)
    if not runs:
        output(f'No nightly backup of {label} found on the HDD yet.')
        return
    # A night Steam was open has no Steam files: said in the list, not found out later.
    shown = [
        (p, d + (' (no Steam files: Steam was open)' if kind == 'host' and steam_left_out(p) else '')) for p, d in runs
    ]
    run = choose(f'Which night\'s backup of the {label}? The newest is first.', shown, prompt, output)
    if run is None:
        return
    target = restore_target(config, f'{kind}-{stamp()}', prompt, output)
    if target is None:
        output(NOTHING_CHANGED)
        return
    rows = [
        ('Restore', dict(MENU)[kind]),
        ('From', f'{run} (the night of {dict(shown)[run]})'),
        ('Into', f'{target} (a new folder)'),
        (
            'Overwritten',
            'nothing. The settings the apps use now stay as they are. To use a restored file you copy it back '
            'yourself afterwards; the steps are said at the end.',
        ),
        APPS_KEEP_RUNNING,
    ]
    if not review(rows, prompt, output):
        output(NOTHING_CHANGED)
        return
    make_home(config, target)
    # With their owners and permissions, so a file copied back works for its app.
    if tool(script, 'restore', '--snapshot', str(run), '--destination', str(target), '--preserve-owners'):
        say(
            output,
            f'\nDone: restored into {target} (checksums verified), with each file\'s owner and permissions.',
            *(
                ('This night has no Steam files (Steam was open then): choose another night for those.',)
                if kind == 'host' and steam_left_out(run)
                else ()
            ),
            *(
                (
                    'Its folders are named source0, source1 and so on, one for each folder that was backed up;',
                    f'{target / "restore-report.json"} says which original folder each one is.',
                )
                if kind == 'host'
                else ()
            ),
            'To use a file from it, stop the app first, copy the file back keeping its owner',
            '(sudo cp -a <file> <where it belongs>), then start the app.',
            'See APP-BACKUPS.md or HOST-BACKUPS.md for the exact steps.',
        )


def restore_monthly(config, prompt, output):
    runs = monthly_snapshots(Path(config['media_mount']) / MONTHLY_DIR, need='restore-tests.json')
    if not runs:
        output('No complete monthly snapshot on the HDD yet.')
        return
    run = choose('Which monthly snapshot? The newest is first.', [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = restore_target(config, f'monthly-{stamp()}', prompt, output)
    if target is None:
        output(NOTHING_CHANGED)
        return
    images, versions = app_versions(run, prompt, output)
    rows = [
        ('Restore', dict(MENU)['monthly']),
        ('From', f'{run} (made {dict(runs)[run]})'),
        ('Apps in it', ', '.join(snapshot_apps(run)) or 'not listed in this snapshot'),
        (
            'Not in it',
            'Linux itself, your photos, your Nextcloud files, movies, shows and music. A monthly snapshot never holds those.',
        ),
        ('Into', f'{target} (a new folder)'),
        ('App versions', versions),
        (
            'Overwritten',
            'nothing. The snapshot is checked and unpacked into the new folder; no app\'s settings or database is '
            'replaced. Rebuilding a server from it is a separate job, described at the end.',
        ),
        APPS_KEEP_RUNNING,
    ]
    if not review(rows, prompt, output):
        output(NOTHING_CHANGED)
        return
    make_home(config, target)
    args = ['--snapshot', str(run), '--stage', str(target)] + (['--load-images'] if images else [])
    if tool('monthly-recovery.py', *args):
        try:
            sealed = json.loads((Path(run) / MANIFEST).read_text()).get('password_files') != 'unencrypted_in_data_tar'
        except (OSError, ValueError, AttributeError):
            sealed = True
        say(
            output,
            f'\nDone: checked and unpacked into {target}.',
            *(
                (
                    'Nothing live was replaced. The app password files (.env files and Nextcloud\'s',
                    'config.php) are not in it: get them with "The list of app passwords" in this menu.',
                )
                if sealed
                else (
                    'Nothing live was replaced. This snapshot was made without a backup key, so the app',
                    'password files (.env files and Nextcloud\'s config.php) are in it, unencrypted.',
                )
            ),
            'To rebuild a server from it, follow "disaster recovery" in MONTHLY-MAINTENANCE.md.',
        )


IMAGES = 'images.tar'  # the saved app versions (Docker images) in a monthly snapshot
DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
IMAGE_ID = re.compile(r'^[0-9a-f]{64}$')


def snapshot_apps(run):
    """The apps a monthly snapshot says it holds (its manifest's "apps")."""
    try:
        apps = json.loads((Path(run) / MANIFEST).read_text()).get('apps')
    except (OSError, ValueError, AttributeError):
        return []
    return [name for name in apps if isinstance(name, str)] if isinstance(apps, list) else []


def saved_app_versions(run):
    """The IDs of the app versions (Docker images) a monthly snapshot saved, from the list
    Docker wrote inside its images.tar; None when that list can't be read. Only read for
    what to offer: the archive is checked against its checksum before anything is loaded
    (monthly-recovery.py)."""
    import tarfile

    try:
        with tarfile.open(Path(run) / IMAGES) as archive:
            rows = json.loads(archive.extractfile('manifest.json').read(4 << 20))
        names = {Path(row['Config']).name.removesuffix('.json') for row in rows}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        return None
    return sorted('sha256:' + name for name in names) if names and all(map(IMAGE_ID.match, names)) else None


def versions_missing(ids, run=subprocess.run):
    """Which of those app versions Docker on this PC does not have; None when Docker is
    not installed or does not answer."""
    try:
        if run([*DOCKER, 'version'], capture_output=True, timeout=60, check=False).returncode:
            return None
        return [
            name
            for name in ids
            if run([*DOCKER, 'image', 'inspect', name], capture_output=True, timeout=60, check=False).returncode
        ]
    except (OSError, subprocess.SubprocessError):
        return None


def app_versions(run, prompt, output):
    """Whether the app versions saved in a monthly snapshot are put back into Docker with
    it, and the review's words for that: (load them, words). Decided from what the
    snapshot holds and what this PC has, never from "is this a new server?". Asked only
    when there is a real choice: some of them are not on this PC."""
    archive = Path(run) / IMAGES
    if not archive.is_file():
        return False, 'this snapshot has no saved app versions, so none is put back'
    ids = saved_app_versions(run)
    gone = versions_missing(ids or [])
    if gone is None:
        return False, (
            'not put back: the program that runs the apps (Docker) is not installed or not running on this PC. '
            'To put them back, install NekoServer first (sudo bash install.sh), then restore this snapshot again'
        )
    if ids is not None and not gone:
        return (
            False,
            f'all {len(ids)} app versions this snapshot was made with are on this PC already, so none is put back',
        )
    found = (
        'Which of the app versions in this snapshot are already on this PC could not be told.'
        if ids is None
        else f'{len(gone)} of the {len(ids)} app versions this snapshot was made with are not on this PC.'
    )
    size = size_words(archive.stat().st_size)
    if asked('restore_app_versions', prompt, output, found=found, size=size):
        return True, f'put back too ({size} more in Docker\'s storage). {found} No app is started or replaced'
    return False, f'not put back: you chose to unpack only the settings and databases. {found}'


PROBLEM_LISTS = (DAMAGED, 'both_damaged', 'disagree', MISSING)


def integrity_sections(config):
    """What the last checksum checks found, per backup tree (reports/integrity.json)."""
    try:
        value = json.loads((Path(config['data_mount']) / INTEGRITY_REPORT).read_text())
    except (OSError, ValueError):
        return {}
    return value.get('sections', {}) if isinstance(value, dict) and value.get('format') == INTEGRITY_FORMAT else {}


def listed(section):
    """Files a check reported as damaged or mismatched (only plain relative paths)."""
    return sorted(
        {
            key
            for name in PROBLEM_LISTS
            for key in section.get(name, [])
            if isinstance(key, str) and valid_ledger_path(key)
        }
    )


DRIVE_NAMES = {'HDD': 'HDD', 'USB': 'USB drive'}


class DriveGone(OSError):
    """A drive the repair writes to is not the one it opened any more (unplugged, or
    another drive mounted at its folder)."""


class Tree:
    """One backup tree the repair reads and writes (the HDD's photo archive, the USB
    drive's copies), on a drive whose identity was checked (`ready`: its UUID, filesystem
    and Btrfs subvolume, as the settings name them). The drive's folder is opened once and
    every file is reached from that open folder, never by its path again: when the drive
    is unplugged mid-repair (or another drive is mounted there), a write can't land in the
    empty folder on the system disk, nor on the other drive. A folder inside the tree that
    is on another filesystem is refused too. Before each write the drive is checked again,
    so a drive that went away is said, and nothing more is written to it."""

    def __init__(self, label, mount, rel, ready):
        self.label, self.mount, self.ready = label, Path(mount), ready
        self.parts = rel.split('/') if rel else []
        self.root = self.mount.joinpath(*self.parts)
        self.fd = self.dev = None
        self.gone = (
            None
            if rel is not None
            else (
                f'the {self.drive()}\'s backup folder in the settings ("archive_dir") is not a plain folder inside it'
            )
        )

    def drive(self):
        return DRIVE_NAMES[self.label]

    def open(self):
        """Open the drive's folder; True when it is the right drive. Otherwise `gone` says why."""
        if self.gone is None and self.ready():
            self.fd = os.open(self.mount, FOLDER_FLAGS)
            self.dev = os.fstat(self.fd).st_dev
            if self.same():  # still the drive that was checked, now that it is open
                return True
            self.close()
        self.gone = self.gone or f'the {self.drive()} is not mounted at {self.mount} (or another drive is there)'
        return False

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def same(self):
        """The drive mounted at its folder is still the right one, and the one opened."""
        try:
            now, held = os.stat(self.mount), os.fstat(self.fd)
        except OSError:
            return False
        return (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino) and self.ready()

    def usable(self):
        """Checked right before each write: False (and `gone` says why) once the drive left."""
        if self.gone is None and not self.same():
            self.gone = f'the {self.drive()} was disconnected or changed during the repair'
        return self.gone is None

    def step(self, folder, part, create):
        """Open the folder `part` inside the open `folder`, made when missing and `create`."""
        try:
            found = os.open(part, FOLDER_FLAGS, dir_fd=folder)
        except FileNotFoundError:
            if not create:
                raise
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=folder)
            found = os.open(part, FOLDER_FLAGS, dir_fd=folder)
        if os.fstat(found).st_dev != self.dev:
            os.close(found)
            raise DriveGone(errno.EXDEV, f'"{part}" in the {self.drive()} is on another drive (one mounted there)')
        return found

    def folder(self, parts, create=False):
        """An open folder of this tree (`parts` below its root), reached from the drive's
        open folder without following a link. The caller closes it."""
        if self.fd is None:
            raise DriveGone(errno.ENODEV, self.gone or f'the {self.drive()} is not open')
        fd = os.dup(self.fd)
        try:
            for part in (*self.parts, *parts):
                found = self.step(fd, part, create)
                os.close(fd)
                fd = found
        except BaseException:
            os.close(fd)
            raise
        return fd


class Saved:
    """One saved copy (`key`) in a Tree, read and written only through the tree's open folders."""

    def __init__(self, tree, key):
        self.tree, self.key = tree, key
        *self.dirs, self.name = key.split('/')

    def __str__(self):
        return str(self.tree.root / self.key)

    def reader(self):
        """The copy, open for reading; OSError when it is not a regular file there."""
        folder = self.tree.folder(self.dirs)
        try:
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=folder)
        finally:
            os.close(folder)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(errno.EINVAL, 'not a regular file', str(self))
        return os.fdopen(fd, 'rb')

    def checksum(self):
        """Its SHA-256, or None when there is no regular file to read."""
        try:
            with self.reader() as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()
        except OSError:
            return None

    def stat(self):
        with self.reader() as stream:
            return os.fstat(stream.fileno())

    def lexists(self):
        """Something (a file, a link, anything) is there under its name."""
        try:
            folder = self.tree.folder(self.dirs)
        except OSError:
            return False
        try:
            os.stat(self.name, dir_fd=folder, follow_symlinks=False)
            return True
        except OSError:
            return False
        finally:
            os.close(folder)

    def set_aside(self, aside):
        """Move it to <tree>/neko-damaged/<aside>/<key> (kept, never deleted)."""
        folder = self.tree.folder(self.dirs)
        try:
            kept = self.tree.folder([QUARANTINE, aside, *self.dirs], create=True)
            try:
                os.rename(self.name, self.name, src_dir_fd=folder, dst_dir_fd=kept)
            finally:
                os.close(kept)
        finally:
            os.close(folder)

    def replace(self, good, expected, aside):
        """Move this copy (if any) aside, then copy the good one (a Saved, or the live
        original's path) in its place, checked against `expected` (SHA-256). DriveGone
        when the drive is not the one opened any more: then nothing is written."""
        if not self.tree.usable():
            raise DriveGone(errno.ENODEV, self.tree.gone)
        if self.lexists():
            self.set_aside(aside)
        folder = self.tree.folder(self.dirs, create=True)
        try:
            with good.reader() if isinstance(good, Saved) else open(good, 'rb') as source:
                copy_checked_into(source, folder, self.name, expected, str(good))
        finally:
            os.close(folder)


def describe(label, path):
    info = path.stat()
    when = dt.datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M')
    return f'{label}: {info.st_size} bytes, last changed {when}'


def settle_unrecorded(key, present, sums, live, prompt, output):
    """A file with no recorded checksum whose copies differ: the original decides when it
    matches one of them, otherwise you choose. Returns (checksum, good path) or (None, why)."""
    candidates = [(label, path, sums[label]) for label, _, path in present if sums[label]]
    chosen = next((c for c in candidates if live and c[2] == live), None)
    if chosen is None and len(candidates) < 2:
        return None, 'skipped: no second copy to compare with'
    if chosen is None:
        output(f'\n{key} differs between the backups and has no recorded checksum:')
        options = [(c, describe(c[0], c[1])) for c in candidates]
        chosen = choose('Which copy is the right one?', options + [(None, 'Keep both as they are')], prompt, output)
        if chosen is None:
            return None, 'kept both as they are'
    return chosen[2], chosen[1]


def settle_conflict(key, present, sums, recorded, live, original, prompt, output):
    """The ledgers record different checksums for this file: neither decides. Each copy is
    shown with the ledger it matches and you choose, or keep both as they are. Returns
    (checksum, good path) or (None, why)."""
    matches = {sha: [label for label, sha2 in recorded.items() if sha2 == sha] for sha in recorded.values()}
    candidates = [(label, path, sums[label]) for label, _, path in present if sums[label] in matches]
    if live in matches:
        candidates.append(('Original', original, live))
    if not candidates:
        return None, 'the backups record different checksums and no copy matches either; every copy was kept untouched'
    output(f'\n{key}: the backups disagree about this file. Each one\'s checksum list records a')
    output('different version, so neither can be trusted to decide. Choose the right one, or keep both.')
    options = [
        (c, describe(c[0], c[1]) + ' (matches the ' + ' and '.join(matches[c[2]]) + ' list)') for c in candidates
    ]
    chosen = choose('Which copy is the right one?', options + [(None, 'Keep both as they are')], prompt, output)
    if chosen is None:
        return None, 'the backups disagree; both copies were kept as they are'
    return chosen[2], chosen[1]


NO_GOOD_COPY = 'no copy matches its recorded checksum any more; every copy was kept untouched'
LOST_OPTION = (
    'Accept that this saved copy is lost: move it to the "neko-damaged" folder (kept, never '
    'deleted) and stop checking it, so the monthly snapshot and updates go on'
)


def decide(key, present, sums, recorded, expected, live, original, prompt, output):
    """Which content is right for this file: (checksum, a path holding it), or (None, why
    nothing was changed)."""
    if len(set(recorded.values())) > 1:
        # Two ledgers, two answers: nothing changes until you choose.
        return settle_conflict(key, present, sums, recorded, live, original, prompt, output)
    if expected is None:
        return settle_unrecorded(key, present, sums, live, prompt, output)
    good = next((path for label, _, path in present if sums[label] == expected), None)
    good = good or (original if live == expected else None)
    if good is None:
        return None, NO_GOOD_COPY
    return expected, good


def undecided(key, why, present, ledgers, every_copy, asking):
    """Nothing could be repaired (`why`). With no good copy left anywhere (and every copy
    looked at), you may accept that the saved copy is lost; otherwise nothing changes."""
    if why == NO_GOOD_COPY and every_copy and all(tree.usable() for _, tree, _ in present):
        return accept_lost(key, present, ledgers, *asking)
    return why, set()


def accept_lost(key, present, ledgers, prompt, output, aside):
    """No copy holds what was recorded for this file, and its original can't stand in
    (edited since, or gone): it can't be repaired. You decide: keep it as it is (the
    monthly check then keeps stopping the monthly snapshot and updates), or accept that
    the saved copy is lost: it goes to <tree>/neko-damaged/<time>/ (kept, never
    deleted), and its checksum entry is retired, with why, so the checks pass again."""
    gone = not any(saved.lexists() for _, _, saved in present)
    say(
        output,
        f'\n{key}: the saved copy '
        + ('is missing' if gone else 'no longer matches its recorded checksum')
        + ', and no other copy (nor the original, which changed or is gone) holds what was saved,'
        ' so it cannot be repaired. Until you decide, the monthly check finds it again and the'
        ' monthly snapshot and updates wait.',
    )
    pick = choose(
        'What do you want to do?', [('keep', 'Keep it as it is for now'), ('lost', LOST_OPTION)], prompt, output
    )
    if pick != 'lost':
        return NO_GOOD_COPY, set()
    if not all(tree.usable() for _, tree, _ in present):  # a drive went away while you chose
        return 'nothing was changed: ' + '; '.join(t.gone for _, t, _ in present if t.gone), set()
    for _, _, saved in present:
        if saved.lexists():
            saved.set_aside(aside)
    why = ('missing' if gone else f'damaged; kept in {QUARANTINE}/{aside}') + ', accepted as lost in the recovery menu'
    for tree, known in ledgers:
        if key in known and tree.usable():
            record_ledger(tree.root, {}, 'lost', retire={key: why})
            del known[key]
    return (
        'accepted as lost' + ('' if gone else f' (the damaged copy is kept in {QUARANTINE}/{aside})'),
        {label for label, _, _ in present},
    )


def put_copies(present, sums, good, expected, aside):
    """Write the good copy over each copy that does not match `expected`. Returns (labels
    repaired, {label: why it was NOT repaired}) (a drive that went away, a write refused)."""
    fixed, failed = [], {}
    for label, _, saved in present:
        if sums[label] == expected:
            continue
        try:
            saved.replace(good, expected, aside)
            fixed.append(label)
        except OSError as exc:
            failed[label] = exc.strerror or str(exc)
    return fixed, failed


def record_sums(key, ledgers, expected, settled, failed):
    """The checksum recorded in each tree's ledger, after the files are in place (it is
    never replaced): not in a tree whose copy could not be repaired, nor on a drive that
    went away. Returns {label: why} for the trees whose checksum could not be recorded."""
    unrecorded = {}
    for tree, known in ledgers:
        if tree.label in failed or (key in known and known[key] == expected):
            continue
        if not tree.usable():
            unrecorded[tree.label] = tree.gone
            continue
        record_ledger(tree.root, {key: expected}, 'chosen', replace=settled)
        known[key] = expected
    return unrecorded


def repair_said(fixed, failed, unrecorded):
    """What happened to one file's copies, in words."""
    done = [label for label in fixed if label not in unrecorded]
    said = ['repaired the ' + ' and '.join(done) + ' copy'] if done else []
    said += [f'the {label} copy was NOT repaired: {why}' for label, why in failed.items()]
    said += [
        f'the {label} copy was written, but its checksum was NOT recorded ({why}), so it stays listed'
        for label, why in unrecorded.items()
    ]
    return '; '.join(said) or 'already intact'


def repair_one(key, trees, ledgers, original, prompt, output, aside, every_copy=True, hint=None):
    """Repair one file of a tree. `trees`: the Trees that should hold it under `key` (HDD
    archive and USB copy, or just one of them); `ledgers`: [(tree, its ledger)];
    `original`: a live source (the Immich library or Nextcloud) that may still hold it, or
    None. `every_copy`: all of the file's copies are here (False while the drive with its
    other photo copy is not connected: a copy can't be given up as lost then). `hint`: the
    checksum a restore recorded for it, used when no ledger has one.
    Returns (what happened, labels of the copies now verified to match)."""
    present = [(tree.label, tree, Saved(tree, key)) for tree in trees]
    recorded = {tree.label: known[key] for tree, known in ledgers if key in known}
    expected = next(iter(recorded.values()), hint)
    sums = {label: saved.checksum() for label, _, saved in present}
    live = sha256_file(original) if original and original.is_file() else None
    if live is not None and live_name(key) != key and expected is None:
        # The live file is a later edit of a dated version: it can only stand in for
        # it when it matches the recorded checksum, and there is none.
        live = None
    conflict = len(set(recorded.values())) > 1
    expected, good = decide(key, present, sums, recorded, expected, live, original, prompt, output)
    if expected is None:
        return undecided(key, good, present, ledgers, every_copy, (prompt, output, aside))
    fixed, failed = put_copies(present, sums, good, expected, aside)
    unrecorded = record_sums(key, ledgers, expected, {key} if conflict else set(), failed)
    return repair_said(fixed, failed, unrecorded), {label for label, _, _ in present} - set(failed) - set(unrecorded)


INTEGRITY_SECTIONS = ('archive', 'usb_photos', 'usb_nextcloud')
# A warning is cleared only for the copies it is about, once each was checked: the
# archive check is about the HDD copy, the USB checks about the USB copies.
NEEDS = {'archive': {'HDD'}, 'usb_photos': {'HDD', 'USB'}, 'usb_nextcloud': {'USB'}}
CHECKSUMS = 'checksums'  # {saved copy: recorded SHA-256} beside a restore's findings (note_findings)


def usb_ready(config, mount):
    """The USB drive mounted at `mount` is the one the settings name: its UUID and
    filesystem, the whole filesystem (as the weekly backup and Usb mount it)."""
    photos = config.get('photos', {})
    found = mounted_identity(Path(mount))
    return (
        found is not None
        and found[0].lower() == str(photos.get('external_backup_uuid', '')).lower()
        and found[1] == photos.get('external_backup_fstype', 'ext4')
        and found[2] == ''
    )


def open_trees(config, usb, output):
    """{'hdd', 'usb', 'cloud'}: the backup trees the repair works on, each opened only when
    its drive is the one the settings name. One that can't be used is said, once."""
    rel = str(config.get('photos', {}).get('archive_dir', ARCHIVE_DIR)).rstrip('/')
    trees = {
        'hdd': Tree(
            'HDD', config['media_mount'], rel if valid_ledger_path(rel) else None, functools.partial(hdd_ready, config)
        )
    }
    if usb:
        ready = functools.partial(usb_ready, config, usb)
        trees.update(usb=Tree('USB', usb, '', ready), cloud=Tree('USB', usb, 'nextcloud', ready))
    for tree in trees.values():
        tree.open()
    for tree in {tree.label: tree for tree in trees.values() if tree.gone}.values():
        output(
            f'  Not used: {tree.gone}. Its copies were NOT repaired, and their warnings stay listed. '
            f'Connect the right drive (or restart the computer), then choose this again.'
        )
    return trees


def repair_jobs(config, work, trees, output):
    """[(keys, trees, originals folder or None, sections, every copy here, trees away)]."""
    hdd, usb = trees["hdd"], trees.get("usb")
    photos = [tree for tree in (hdd, usb) if tree is not None and not tree.gone]
    away = [tree for tree in (hdd, usb) if tree is not None and tree.gone]
    keys = sorted(set(work['archive']) | set(work['usb_photos']))
    # A live original is read only from the drive the settings name (Immich's on the
    # SSD, Nextcloud's on the HDD).
    jobs = []
    if keys and photos:
        # A photo copy is given up as lost only when both of its copies were looked at.
        jobs.append(
            (
                keys,
                photos,
                photo_library(config) if holder_ready(config, 'photos') else None,
                ('archive', 'usb_photos'),
                len(photos) == 2,
                away,
            )
        )
    jobs += cloud_job(work, trees, config, output)
    return jobs


def cloud_job(work, trees, config, output):
    """The Nextcloud copy's repair job (repair_jobs), or none: Nextcloud's live files are
    read only from the drive the settings name for them."""
    usb, cloud = trees.get('usb'), trees.get('cloud')
    if cloud is not None and not cloud.gone:
        live = cloud_files(config) if holder_ready(config, 'cloud') else None
        return [(work['usb_nextcloud'], [cloud], live, ('usb_nextcloud',), True, [])]
    if usb is None and (work['usb_nextcloud'] or work['usb_photos']):
        output('  The USB drive is not plugged in: its copies were not checked and stay listed.')
    return []


def reported_sums(sections):
    """{saved copy: checksum} a restore recorded beside its findings (when the sections agree)."""
    sums = {}
    for name in INTEGRITY_SECTIONS:
        found = sections.get(name, {}).get(CHECKSUMS)
        for key, sha in found.items() if isinstance(found, dict) else ():
            if valid_ledger_path(key) and isinstance(sha, str) and SHA256.match(sha):
                sums[key] = sha if sums.get(key, sha) == sha else None
    return {key: sha for key, sha in sums.items() if sha}


def repair_job(job, hints, asking, resolved):
    """Repair every file of one job (repair_jobs), adding what is now fixed to `resolved`."""
    keys, trees, originals, names, every_copy, away = job
    prompt, output, aside = asking
    ledgers = [(tree, read_ledger(tree.root)) for tree in trees]
    for key in keys:
        original = originals / live_name(key, trees[0].root) if originals else None
        result, verified = repair_one(key, trees, ledgers, original, prompt, output, aside, every_copy, hints.get(key))
        said = [result] + [f'the {tree.label} copy was NOT repaired: {tree.gone}' for tree in away]
        output(f'  {key}: ' + '; '.join(said))
        for name in names:
            if NEEDS[name] <= verified:
                resolved[name].append(key)


# What writes to NekoServer's drives: stopped before a drive is checked, started again after.
# The same names the runtime's NAS check uses (runtime/src/shared.json).
WRITERS = tuple(SHARED['writers'])
NAS_WATCH = SHARED['nas_watch']['service']
NAS_WATCH_STATE = Path(SHARED['nas_watch']['state'])
NAS_WATCH_LOCK = SHARED['nas_watch']['lock']  # its record's lock (nas_watch.rs state_lock)
PROBE_DIR = SHARED['nas_watch']['probe_dir']


WRITE_WAIT = 20  # seconds a test write may take (as nas_watch.rs): a NAS away makes it wait


def takes_writes(folder, wait=WRITE_WAIT):
    """A small file written, synced and removed at the drive's top (nas_watch.rs does the
    same): Linux still shows an aborted ext4 as mounted read-write, so only a write tells.
    A write still waiting after `wait` seconds (the NAS away, within its ten-minute wait)
    stops the check with a message instead of freezing the menu."""
    import threading

    # A daemon thread: quitting the menu never waits for a write still stuck (a pool's
    # threads are waited for at exit, the audit of 0.46.2).
    answered, answer = threading.Event(), {}

    def write():
        answer['writes'] = test_write(folder)
        answered.set()

    threading.Thread(target=write, daemon=True).start()
    if not answered.wait(wait):
        raise RuntimeError(
            f'A test write to {folder} is still waiting after {wait} s: the network storage box is not answering '
            '(writes wait up to ten minutes for it). Check it is on and connected, then choose this again.'
        )
    return answer['writes']


def test_write(folder):
    """takes_writes' write itself: True when it worked."""
    probe = Path(folder) / PROBE_DIR
    try:
        probe.mkdir(mode=0o700, exist_ok=True)
        if probe.is_symlink():
            return False
        target = probe / f'menu-{os.getpid()}'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b'NekoServer checks that this drive takes writes\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        target.unlink()
        return True
    except OSError:
        return False


def ext4_errors(folder, sys_ext4=Path('/sys/fs/ext4')):
    """The errors Linux counted on the ext4 filesystem mounted at `folder` (common.rs
    ext4_errors): kept until the filesystem is checked and repaired. 0 when unknown."""
    source = (findmnt('SOURCE', folder) or '').split('[')[0]
    if not source.startswith('/dev/'):
        return 0
    try:
        return int((sys_ext4 / Path(os.path.realpath(source)).name / 'errors_count').read_text().strip())
    except (OSError, ValueError):
        return 0


def drive_rows(config):
    """(name, folder, its settings) of the SSD and the media drive."""
    storage = config.get('storage', {})
    return [
        ('the application SSD', Path(config['data_mount']), storage.get('data_ssd', {})),
        ('the media drive', Path(config['media_mount']), storage.get('media_hdd', {})),
    ]


AT_WORK = 'activating,deactivating,reloading'


def setup_lock_is_ours(lock=None):
    """True when this very process holds setup's lock. install.sh takes the lock before it
    starts the recovery menu (installer.py --recover inherits the open file), so the menu
    holds it for as long as it is open: a try of the lock from inside the menu fails, and
    the read-only check reported "Setup is running" with no setup anywhere (0.47.10)."""
    if lock is None:
        lock = SHARED['setup_lock']
    try:
        wanted = os.stat(lock)
    except OSError:
        return False
    for name in os.listdir('/proc/self/fd'):
        try:
            opened = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        if (opened.st_dev, opened.st_ino) == (wanted.st_dev, wanted.st_ino):
            return True
    return False


def repair_blocked(config, run=subprocess.run):
    """Why a drive can't be checked now, or None: a drive move, an upgrade, an unfinished
    app update, setup, or a backup or job at work (the 0.46.0 review, H07: the repair
    stopped the apps and unmounted a drive under them). The backups' lock is on the SSD,
    so it is only tried here, never held: holding it would keep the SSD from unmounting."""
    import upgrade

    recovery = Path(config['data_mount']) / 'installer/recovery.lock'
    # Setup's lock is left out when this menu holds it itself (see setup_lock_is_ours):
    # tried here, the menu would count as the running setup and refuse every time.
    setup_lock = None if setup_lock_is_ours() else upgrade.SETUP_LOCK
    busy = upgrade.nothing_at_work(lock=setup_lock, recovery=recovery if recovery.parent.is_dir() else None)
    if busy:
        return busy
    listed = run(
        ['systemctl', 'list-units', '--plain', '--no-legend', '--state=' + AT_WORK, 'nekoserver-*'],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    working = [line.split()[0] for line in (listed.stdout or '').splitlines() if line.split()]
    working = [unit for unit in working if unit != NAS_WATCH]
    return (', '.join(working) + ' is at work') if working else None


CHECK, REPAIR = 'check', 'repair'  # section 3's two actions on a drive
# A shared folder's filesystem is on the machine that shares it: never checked or repaired from here.
SHARES = ('nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'fuse.sshfs', '9p')


def check_read_only(
    config, prompt, output, run=subprocess.run, writes=takes_writes, errors=ext4_errors, hold=None, action=CHECK
):
    """See checked_drive: the same, holding the operation lock (in /run, which drive moves
    and upgrades take too) from start to end, so none of them can start meanwhile."""
    held = (hold or hold_operation)(OPERATION_LOCK)
    if held is None:
        output('A drive move or a NekoServer upgrade is at work; choose this again once it has finished.')
        return
    with held:
        checked_drive(config, prompt, output, run, writes, errors, action)


def mounted_drive(folder, run):
    """(device, kind of filesystem, UUID) of what is mounted at `folder` now, each '' when
    it can't be told: what is shown, and what the check or repair would work on."""
    shown = run(
        ['findmnt', '-rno', 'SOURCE,FSTYPE,UUID', '--mountpoint', str(folder)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = (shown.stdout or '').strip().splitlines()
    found = lines[-1].split() if lines else []  # mounts stacked on one folder: the last is in use
    return tuple((found + ['', '', ''])[:3])


def on_the_drive(config, folder):
    """What NekoServer keeps on the drive mounted at `folder`, in words."""
    if Path(folder) == Path(config['data_mount']):
        return 'the apps themselves, their settings and their databases'
    return 'movies, shows and music, and the backups NekoServer keeps there'


def drive_facts(name, folder, found, config, writes, errors):
    """The exact drive a check or repair is about, and what it costs: said before the question."""
    device, fstype, uuid = found
    counted = errors(folder)
    return [
        f'\nThe drive: {name}',
        f'  Device:       {device or "unknown"}' + (f' (identity {uuid})' if uuid else ''),
        f'  Connected at: {folder} (formatted as {fstype or "unknown"})',
        f'  Holds:        {on_the_drive(config, folder)}',
        '  Problem:      '
        + ('it takes new files, ' if writes(folder) else 'it does NOT take new files any more, ')
        + f'and Linux counted {counted} error{"" if counted == 1 else "s"} on it',
        '  Apps:         EVERY app is stopped while this runs, the ones on the other drive too, and so are the '
        'shared folders (Samba). Nothing can be watched, uploaded or downloaded meanwhile.',
        '  How long:     a few minutes on a small drive; an hour or more on a large or badly damaged one.',
    ]


SHARED_FOLDER = (
    'NekoServer never checks or repairs a shared folder from this PC: the drive behind it belongs to the '
    'network storage box (NAS), and only the NAS can check it.'
)


def checked_drive(config, prompt, output, run, writes, errors, action=CHECK):
    """Section 3, for a drive Linux stopped writing to after errors (a NAS away for over
    ten minutes, a loose cable). Two separate actions, each with its own approval, asked
    before anything is stopped:
    a check (CHECK) only looks (e2fsck -n) and changes nothing on the drive;
    a repair (REPAIR) changes the drive (e2fsck -p) and is approved by typing its name.
    For either, the apps are stopped and the drive is unmounted (never by force); a drive
    that is still mounted anywhere is never touched, and a shared folder never at all."""
    failing = [(n, f, r) for n, f, r in drive_rows(config) if os.path.ismount(f) and (not writes(f) or errors(f))]
    if not failing:
        output(f'Both drives take new files and Linux counted no errors on them: nothing to {action}.')
        return
    name, folder, record = failing[0]
    export = nfs_exports.export_of(record)
    # A share by the settings is never even looked at; else what is mounted there decides.
    found = ('', '', '') if export else mounted_drive(folder, run)
    if export or found[1] in SHARES:
        say(
            output,
            f'\n{name.capitalize()} ({folder}) is a folder the NAS shares ({export or found[0]}), and it does not '
            'take new files.',
            SHARED_FOLDER,
            'On the NAS, check that its disks are healthy, that the volume is not full or read-only,',
            'and that this PC still has read and write access to the shared folder.',
            'The apps start again by themselves once it takes new files. Nothing was changed.',
        )
        return
    uuid, fstype = named_drive(name, folder, record, found)
    say(output, *drive_facts(name, folder, found, config, writes, errors))
    if not drive_approved(action, found[0], fstype, prompt, output):
        output('Nothing was changed: the apps keep running and the drive is as it was.')
        return
    busy = repair_blocked(config, run)
    if busy:
        output(f'{busy}: choose this again once it has finished. Nothing was changed.')
        return
    run(['systemctl', 'stop', *WRITERS], capture_output=True, text=True, timeout=600, check=False)
    unmounted(folder, run)
    device = f'/dev/disk/by-uuid/{uuid}'
    not_mounted(uuid, found[0], run)
    if fstype != 'ext4':
        raise RuntimeError(
            f'The apps are stopped and {folder} is disconnected, as its own tool needs. Now run  '
            f'{own_tool(action, fstype, device)} ; then run  sudo bash install.sh  to connect it again and start '
            'the apps.'
        )
    damaged = looked_at(device, output, run) if action == CHECK else repaired(device, output, run)
    mounted_again(folder, run, writes if not damaged else None)
    if damaged:
        say(
            output,
            f'The check found damage on {found[0]}. Nothing on the drive was changed.',
            f'It is connected again at {folder}, so you can copy what you can\'t lose to another drive.',
            'The apps stay stopped, so that they write nothing more to a damaged drive.',
            'Next: choose "Repair a drive" in this menu. To start the apps without a repair, run  sudo bash install.sh',
        )
        return
    output(
        f'{folder}: the check found no damage. It is connected again and takes new files.'
        if action == CHECK
        else f'{folder} is repaired, connected again and takes new files.'
    )
    start_apps(run, output, config)


def named_drive(name, folder, record, found):
    """(UUID, filesystem) of the drive the settings name for `folder`, checked to be the
    one mounted there (`found`: mounted_drive's answer). Raises when it is not."""
    uuid, fstype = record.get('uuid', ''), record.get('fstype', '')
    if not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid or ''):
        raise RuntimeError(f'{name} has no drive identity in the settings')
    if found[2] != uuid:
        # The review of 0.46.1: with another drive mounted there (or settings out of date),
        # one filesystem was unmounted and another, the saved UUID's, checked.
        raise RuntimeError(
            f'{folder} holds another drive than the settings name (UUID {found[2] or "unknown"}, '
            f'not {uuid}). Nothing was changed; run  sudo bash install.sh  so setup sees which drive it is.'
        )
    return uuid, fstype


def own_tool(action, fstype, device):
    """The command that checks (or repairs) a drive NekoServer does not check itself."""
    tools = {
        ('xfs', CHECK): f'sudo xfs_repair -n {device}',
        ('xfs', REPAIR): f'sudo xfs_repair {device}',
        ('btrfs', CHECK): f'sudo btrfs check {device}',
        ('btrfs', REPAIR): f'sudo btrfs check {device}',
    }
    return tools.get((fstype, action), f'the checking tool of {fstype or "its kind of formatting"}')


def drive_approved(action, device, fstype, prompt, output):
    """The approval of a check or of a repair, asked before anything is stopped. A check: a
    typed yes. A repair: the drive's own name typed out, after the warning; Enter alone,
    "yes" or anything else leaves the drive as it is."""
    import questions

    if fstype != 'ext4':
        say(
            output,
            f'NekoServer can {action} only drives formatted as ext4 itself. This one is {fstype or "something else"}.',
            'It can stop the apps and disconnect the drive for you, then say the command to run for it.',
        )
    if action == CHECK:
        return asked('check_drive', prompt, output)
    name = Path(device).name
    if not name:
        raise RuntimeError('the drive\'s device name could not be read, so a repair cannot be approved')
    say(
        output,
        '\nWhat a repair does: it rewrites parts of the drive\'s filing system, the index that says where every',
        'file is. It can NOT be undone. A file that was damaged can come back empty, cut short, or be moved to a',
        'folder named lost+found under a number instead of its name; some may be lost.',
        'First copy anything on this drive that you can\'t lose and that has no backup to another drive. Run',
        '"Check a drive" first if you have not: it shows the damage and changes nothing.',
    )
    return questions.ask('repair_drive', prompt, name=name).strip() == name


def not_mounted(uuid, device, run):
    """Refuse to go on while the drive is still mounted anywhere (a second folder of it
    connected elsewhere, an unmount that did not take): a check or repair of a drive in
    use reads it wrongly, and a repair would damage it."""
    still = run(
        ['findmnt', '-rno', 'TARGET', '-S', f'UUID={uuid}'], capture_output=True, text=True, timeout=30, check=False
    )
    where = (still.stdout or '').split()
    if where:
        raise RuntimeError(
            f'{device} is still connected at {", ".join(where)}, so it was NOT checked or repaired: that is never '
            'done on a drive in use. Nothing on the drive was changed. The apps stay stopped; run  sudo bash '
            'install.sh  to connect everything again and start them.'
        )


def unmounted(folder, run):
    """The drive unmounted, never by force: a program still using it is named."""
    done = run(['umount', str(folder)], capture_output=True, text=True, timeout=120, check=False)
    if done.returncode:
        users = run(['fuser', '-vm', str(folder)], capture_output=True, text=True, timeout=30, check=False)
        raise RuntimeError(
            f'{folder} could not be unmounted ({(done.stderr or "").strip()[:200]}); a program still uses it:\n'
            + ((users.stderr or '') + (users.stdout or '')).strip()[:1500]
            + '\nClose it (or restart the PC), then choose this again. The apps stay stopped until then.'
        )


def looked_at(device, output, run):
    """The unmounted ext4 drive only looked at (e2fsck -n opens it read-only and answers
    "no" to every fix): True when it found damage, False when it found none."""
    output('Checking, without changing anything (on a large drive this takes a few minutes)...')
    fsck = run(['e2fsck', '-f', '-n', device], capture_output=True, text=True, timeout=6 * 3600, check=False)
    output((fsck.stdout or '').strip()[-1500:])
    # 0: no damage. 4: damage found and, as asked, left as it is. Anything else (8 and up:
    # the check itself could not run; a negative code: it was stopped) is not an answer.
    if fsck.returncode not in (0, 4):
        raise RuntimeError(
            f'the check could not be done (the checking program, e2fsck, stopped with code {fsck.returncode}). '
            'Nothing on the drive was changed. The apps stay stopped; run  sudo bash install.sh  to connect the '
            'drive again and start them.'
        )
    return fsck.returncode == 4


def repaired(device, output, run):
    """The unmounted ext4 drive repaired (e2fsck -p: the fixes it makes without asking).
    Returns False (no damage is left) once it finished."""
    output('Repairing (on a large drive this takes a few minutes)...')
    fsck = run(['e2fsck', '-f', '-p', device], capture_output=True, text=True, timeout=6 * 3600, check=False)
    output((fsck.stdout or '').strip()[-1500:])
    # 0: clean, 1: repaired, 2: repaired, restart advised. Anything else, also a negative
    # code (e2fsck killed part-way, the review of 0.46.1), is not a finished repair.
    if fsck.returncode not in (0, 1, 2):
        what = (
            'was stopped before it finished'
            if fsck.returncode < 0
            else f'found problems it does not fix without asking (exit {fsck.returncode})'
        )
        raise RuntimeError(
            f'the repair program (e2fsck) {what}. The drive is not repaired, and it stays disconnected with the apps '
            f'stopped. Run  sudo e2fsck -f {device}  yourself: it asks before each change. Then run  sudo bash '
            'install.sh  to connect the drive again and start the apps.'
        )
    return False


def mounted_again(folder, run, writes):
    """The drive mounted again by its own mount unit, and taking writes (`writes` None: not
    tried, after a check that found damage)."""
    unit = run(
        ['systemd-escape', '--path', '--suffix=mount', str(folder)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()
    run(['systemctl', 'start', unit], capture_output=True, text=True, timeout=300, check=True)
    if writes is not None and not writes(folder):
        raise RuntimeError(
            f'{folder} is mounted again but still does not take writes; check the drive (or the NAS) and its cable'
        )


def nas_main_drive(config):
    """True when the SSD or the HDD is on a NAS (the NAS check starts the apps then). Only the
    backup drive there: the apps are started here (the audit of 0.46.2: the NAS check has
    no drive to look at then, and its hold was never lifted)."""
    return any(isinstance(r, dict) and r.get('network') for r in (config or {}).get('storage', {}).values())


def start_apps(run, output, config=None):
    """The apps started again: through the NAS check when it is there (it starts them only
    once every drive on the NAS takes writes, through the drive-return checks), else the
    services that write to the drives, those switched on."""
    if nas_main_drive(config) and Path('/etc/systemd/system', NAS_WATCH).is_file():
        # Under the check's own lock (a check at work would write over the note), then a
        # check of its own, which starts them through the drive checks.
        with open(NAS_WATCH_LOCK, 'a') as turn:
            fcntl.flock(turn, fcntl.LOCK_EX)
            saved = nas_watch_state()
            atomic_json(NAS_WATCH_STATE, {**saved, 'held': True})
        ran = run(['systemctl', 'start', NAS_WATCH], capture_output=True, text=True, timeout=1200, check=False)
        if ran.returncode or nas_watch_state().get('held'):
            output(
                'The apps were not started yet: the NAS check refused (see  journalctl -u '
                f'{NAS_WATCH} ). They start by themselves once every drive on the NAS checks out.'
            )
            return
    else:
        failed = [
            unit
            for unit in WRITERS
            if run(['systemctl', 'is-enabled', '--quiet', unit], timeout=30, check=False).returncode == 0
            and run(['systemctl', 'start', unit], capture_output=True, text=True, timeout=300, check=False).returncode
        ]
        if failed:
            output(f'Not everything started again: {", ".join(failed)} (see  journalctl -u {failed[0]} ).')
            return
    output('The apps are started again.')


def nas_watch_state():
    """What the NAS check wrote last ({} when nothing, or it can't be read)."""
    try:
        saved = json.loads(NAS_WATCH_STATE.read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def repair_backups(config, prompt, output):
    """Fix the files the checksum checks reported, from a copy that still matches. Only
    drives that are the ones the settings name are read or written (see Tree)."""
    from recovery_tools import lock

    sections = integrity_sections(config)
    work = {name: listed(sections.get(name, {})) for name in INTEGRITY_SECTIONS}
    if not any(work.values()):
        output('The last checksum checks found nothing to repair.')
        return
    aside = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    counts = {
        'the photo copy on the HDD': len(work['archive']),
        'the photo copy on the USB drive': len(work['usb_photos']),
        'the Nextcloud copy on the USB drive': len(work['usb_nextcloud']),
    }
    say(
        output,
        '\nFix damaged backup copies',
        'The last checks found backup copies that are damaged or missing:',
        *(f'  {file_count(count)} in {place}' for place, count in counts.items() if count),
        'Each one is compared with the checksum recorded when it was backed up (its fingerprint), and',
        'replaced by a copy that still matches it: the other backup\'s, or your own file in the app.',
        'A damaged copy is moved to a folder named "neko-damaged" on the same drive, never deleted.',
        'Your own photos and files in the apps are only read. No app is stopped.',
    )
    if not asked('fix_copies', prompt, output):
        output('Nothing was changed.')
        return
    resolved = {name: [] for name in work}
    hints = reported_sums(sections)
    with lock(config, changes_apps=False), Usb(config, writable=True) as usb:
        trees = open_trees(config, usb, output)
        try:
            for job in repair_jobs(config, work, trees, output):
                repair_job(job, hints, (prompt, output, aside), resolved)
        finally:
            # Closed before the USB drive is unmounted: an open folder would keep it busy.
            for tree in trees.values():
                tree.close()
    clear_resolved(config, resolved)
    say(output, '\nDone. The next backup run checks everything again.')


def clear_resolved(config, resolved):
    """Take repaired files off the dashboard's list until the next check runs (on the SSD
    the settings name only)."""
    path = Path(config['data_mount']) / INTEGRITY_REPORT
    try:
        value = json.loads(path.read_text()) if ssd_ready(config) else None
    except (OSError, ValueError):
        value = None
    if not isinstance(value, dict):
        return
    for name, keys in resolved.items():
        section = value.get('sections', {}).get(name)
        if isinstance(section, dict) and keys:
            drop_resolved(section, keys)
    atomic_json(path, value)


def drop_resolved(section, keys):
    """One integrity section without the repaired files `keys` (lists, counts, checksums)."""
    for kind in PROBLEM_LISTS:
        rows = section.get(kind, [])
        section[kind] = [k for k in rows if k not in keys]
        counts = section.setdefault('counts', {})
        counts[kind] = max(0, counts.get(kind, len(rows)) - (len(rows) - len(section[kind])))
    if isinstance(section.get(CHECKSUMS), dict):
        section[CHECKSUMS] = {k: v for k, v in section[CHECKSUMS].items() if k not in keys}


def settings(config_path=None, output=None):
    """The chosen settings file (--config), else my-config.json, else the settings of the
    server set up here (a release folder fresh from git pull has none), else config.json."""
    candidates = [settings_path(config_path)] if config_path else [ROOT / 'my-config.json']
    if not config_path and not candidates[0].is_file():
        import wizard

        earlier = wizard.earlier_settings(ROOT)
        if earlier is not None:
            if output:
                output('Using the settings of the server set up here: ' + str(earlier))
            candidates.append(earlier)
        candidates.append(ROOT / 'config.json')
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text())
    raise RuntimeError('Settings file not found: ' + ', '.join(str(p) for p in candidates))


def stop_on_sigterm():
    """A terminal closed or a `kill` ends the menu the way Ctrl+C does: the USB drive it
    mounted is unmounted again (left mounted read-only, the weekly copy could not use it)."""

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)


def run(prompt=input, output=print, config_path=None):
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh --recover')
        return 2
    stop_on_sigterm()
    config = settings(config_path, output)
    if prompt is not builtins.input:
        return menu_loop(config, prompt, output)  # asked by a test, or another program: no window
    import setup_gui

    # With a desktop, the setup window shows the menu too and either place answers
    # (0.50.0); without one, or with a broken one, the terminal alone, as before.
    with setup_gui.session('NekoServer recovery', config, output):
        return menu_loop(config, prompt, output)


RESTORES = {
    'photos': restore_photos,
    'cloud': restore_cloud,
    'passwords': restore_passwords,
    'apps': lambda c, p, o: restore_nightly(c, 'apps', p, o),
    'host': lambda c, p, o: restore_nightly(c, 'host', p, o),
    'monthly': restore_monthly,
}
DRIVE_ACTIONS = {
    'check': check_read_only,
    'repair': lambda c, p, o: check_read_only(c, p, o, action=REPAIR),
    'copies': repair_backups,
}


def restore_section(config, prompt, output):
    """Sections 1 and 2: what to restore is chosen here; each restore then asks for its
    backup, date, files and folder, shows the review, and writes only once approved."""
    say(
        output,
        '\nEvery restore goes into a NEW folder: nothing you have now is overwritten, and no app is stopped.',
        'You first choose what, from which backup and where to. Then you see all of it together and approve',
        'it once. Nothing is written before that.',
        'A restore brings back files, settings and databases. It does not reinstall Linux or the apps.',
    )
    pick = choose('What would you like to restore?', menu(config), prompt, output)
    if pick is not None:
        RESTORES[pick](config, prompt, output)


def drive_section(config, prompt, output):
    """Section 3: a check, a repair and the fixing of backup copies, each its own action."""
    say(
        output,
        '\nA check and a repair are two different things. A check only looks. A repair changes the drive,',
        'so it has a warning and an approval of its own. Neither is started by choosing it here: each',
        'first shows the exact drive and asks.',
    )
    pick = choose('Check or repair a drive', DRIVE_MENU, prompt, output)
    if pick is not None:
        DRIVE_ACTIONS[pick](config, prompt, output)


def menu_loop(config, prompt, output):
    """The recovery menu itself: one restore, check or repair after another, until Quit."""
    say(
        output,
        '\nNekoServer recovery',
        'Choosing something here changes nothing yet: you are always shown exactly what will happen first,',
        'and asked. Pressing Enter alone never approves anything.',
    )
    sections = {'restore': restore_section, 'drive': drive_section}
    started = False
    try:
        while True:
            pick = choose('What do you need to do?', SECTIONS, prompt, output, leave='Quit')
            if pick is None:
                output('Done.')
                return 0
            started = True
            try:
                sections[pick](config, prompt, output)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                output(f'That did not go through: {exc}')
    except (EOFError, KeyboardInterrupt):
        # Restores and repairs finished before this stay as they are.
        output(
            '\nStopped. '
            + (
                'Nothing more was changed; what was already restored or repaired stays as it is. If a drive '
                'check or repair was under way, the apps may still be stopped: run  sudo bash install.sh  to '
                'connect the drives and start them.'
                if started
                else 'Nothing was changed.'
            )
        )
        return 1
