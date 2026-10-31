"""Guided recovery: `sudo bash install.sh --recover`.

A menu over the existing restore tools. Everything is restored into a NEW folder;
live data is never overwritten. Each restore ends with what to do next.
"""
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
import shutil
import stat
import subprocess
import sys
import zlib

import guided_setup
from neko_common import (LEDGER_NAME, QUARANTINE, BackupDamaged, atomic_json, backup_files, copy_checked, copy_checked_into,
                         ensure_room, findmnt, identity_matches, mounted_identity, read_ledger,
                         record_ledger, settings_path, sha256_file, valid_ledger_path, usb_mount_options,
                         private_mount_options, latest_note, saved_names, saved_versions)

ROOT = Path(__file__).resolve().parent
# The weekly USB backup holds this lock while it mounts, copies and unmounts the drive.
USB_LOCK = Path('/run/nekoserver-photo-backup.lock')
RESTORE = 'The restore'   # how the room check names a restore in its message
SNAPSHOT = re.compile(r'^snapshot-(\d+)-\d+$')
MONTHLY = re.compile(r'^(\d+)-\d+$')

MENU = [
    ('photos', 'Photos and videos (Immich)'),
    ('cloud', 'Nextcloud files'),
    ('passwords', 'App password list (and the apps\' password files)'),
    ('apps', 'App settings (nightly backup)'),
    ('host', 'Steam and Samba settings (nightly backup)'),
    ('monthly', 'Everything from a monthly snapshot (for rebuilding a server)'),
    ('repair', 'Repair damaged backup copies (found by the checksum checks)'),
    # Only for a USB drive used before v0.21.1: last, and said to be advanced.
    ('old-usb', 'Advanced: old full snapshots on the USB drive (from before v0.21.1): review or remove'),
]


MONTHLY_DIR = 'backups/monthly'    # monthly snapshots, on the HDD (and old ones on the USB drive)
MANIFEST = 'manifest.json'      # what every monthly snapshot folder must contain
SEALED = 'secrets.tar.cms'          # a monthly snapshot's encrypted password list
ARCHIVE_DIR = 'photos/archive'      # the photo archive's default folder on the HDD


def say(output, *lines):
    for line in lines:
        output(line)


def ask(question, prompt=input, output=print):
    """yes -> True, no -> False; Ctrl+D or Ctrl+C stops the recovery menu."""
    answer = guided_setup.yes_no(question, prompt, output)
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
    while True:
        answer = prompt('Choose a number: ').strip()
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


def monthly_snapshots(folder, need=None):
    """(path, label) for complete monthly snapshots, newest first; `need` = required file."""
    folder = Path(folder)
    found = []
    for path in folder.iterdir() if folder.is_dir() else ():
        match = MONTHLY.match(path.name)
        if (match and path.is_dir() and not path.is_symlink() and (path / MANIFEST).is_file()
                and (need is None or (path / need).is_file())):
            found.append((int(match[1]), path))
    return [(path, when(stamp)) for stamp, path in sorted(found, reverse=True)]


def cloud_users(usb):
    folder = Path(usb) / 'nextcloud'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if (p / 'files').is_dir() and not p.is_symlink())


def folder_problem(path, avoid=(), refuse=()):
    """Why a restore destination cannot be used, or None if it can. `avoid`: the backup
    trees: a restore inside one would become part of that backup (and of the next restore
    from it), and one around a backup would hold it. `refuse`: where a drive should be
    mounted but is not: a folder there would be on the system disk."""
    for mount in refuse:
        mount = Path(mount)
        if path == mount or mount in path.parents:
            return f'The HDD is not mounted at {mount}, so that folder would be on the system disk. Pick a folder on another drive.'
    for tree in avoid:
        tree = Path(tree)
        if path == tree or tree in path.parents or path in tree.parents:
            return f'That folder is inside (or around) a backup ({tree}); a restore there would mix with it. Pick another.'
    if path.exists():
        return 'That folder already exists; restoring never overwrites anything. Pick another.'
    if not path.parent.is_dir():
        return 'Its parent folder does not exist. Pick another.'
    return None


def typed_folder(prompt, output):
    """A typed absolute path without '..', or None when the user goes back."""
    while True:
        typed = prompt('Type a full path for a NEW folder (or press Enter to go back): ').strip()
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


def new_folder(suggested, prompt=input, output=print, avoid=(), refuse=()):
    """Agree a destination that does not exist yet, inside an existing folder and outside
    every backup tree in `avoid` (and every missing drive's folder in `refuse`). With no
    suggestion (None), you type one."""
    path = Path(suggested) if suggested is not None else typed_folder(prompt, output)
    while path is not None:
        output(f'\nRestore into this new folder:\n  {path}')
        if ask('Use this folder?', prompt, output):
            problem = folder_problem(path, avoid, refuse)
            if problem is None:
                return path
            output(problem)
        path = typed_folder(prompt, output)
    return None


def tree_bytes(folder):
    """Total size of the regular files in a folder (symlinks are not followed)."""
    total = 0
    for path in Path(folder).rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def file_count(count):
    """"1 file", "2 files"."""
    return f'{count} file' + ('' if count == 1 else 's')


# Inside a restore folder: the damaged copies a restore found, as they are on the backup
# (a photo or PDF with a few bad bytes often still opens in part). Never checked.
DAMAGED_AS_IS = 'damaged-as-is'


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
    return {key: row['why'] for key, row in retired.items()
            if valid_ledger_path(key) and isinstance(row, dict) and isinstance(row.get('why'), str)}


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
        self.destination = None
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
        """The new restore folder is made; files are copied into it next."""
        destination.mkdir(mode=0o700)
        self.destination = destination

    def keep_damaged(self, target):
        """Move a damaged copy just written to `target` into the damaged-as-is folder."""
        if self.destination is None:
            Path(target).unlink(missing_ok=True)
            return
        kept = self.destination / DAMAGED_AS_IS / Path(target).relative_to(self.destination)
        kept.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(target, kept)

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
            copy_checked(source, target, expected)
        except BackupDamaged:
            self.keep_damaged(target)
            self.damaged.append(key or str(source))
            return
        if expected:
            self.verified += 1
        else:
            self.unverified += 1

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
            here = {path.relative_to(self.root).as_posix() for path, _ in backup_files(folder)} \
                if Path(folder).is_dir() else set()
            missing += sorted(k for k in self.sums if k.startswith(prefix) and k not in here)
        return missing

    def where_else(self):
        """What to do about files this backup can't give back."""
        if self.reported:
            fix = ('They are now listed for this menu\'s "Repair damaged backup copies", '
                   'which fixes them from a copy that still matches.')
        else:
            fix = ('This menu\'s "Repair damaged backup copies" fixes damaged or missing ones from a copy that '
                   'still matches.')
        return (f'Get them from {self.other}, if it has them. ' if self.other else '') + fix

    def summary(self, destination=None):
        text = f'Checksums matched for {file_count(self.verified)}'
        if self.unverified:
            text += (f'; {file_count(self.unverified)} had no recorded checksum yet (the backup that copied it did '
                     'not finish, or it was backed up before checksums were kept) and could not be checked')
        text += '.'
        if self.ledger_problem:
            text += f' This backup\'s checksum list could not be used ({self.ledger_problem}), so no file was checked.'
        if self.uncertain:
            text += f' Left out ({file_count(len(self.uncertain))}): {UNCERTAIN_WHY}'
        if self.accepted:
            text += f' Not restored ({file_count(len(self.accepted))}): you accepted them as lost in the repair menu.'
        left = self.damaged + self.missing
        if left:
            text += (f' NOT restored ({file_count(len(left))}), because this backup has them damaged ({len(self.damaged)}) '
                     f'or no longer has them ({len(self.missing)}). {self.where_else()}')
            if self.damaged and destination is not None:
                text += (f' The damaged copies are kept as they are in {Path(destination) / DAMAGED_AS_IS} (they may '
                         'still open in part; nothing checked them).')
        if (left or self.accepted) and destination is not None and Path(destination).is_dir():
            listed = Path(destination) / 'NOT-RESTORED.txt'
            listed.write_text(self.not_restored())
            text += f' Their names are in {listed}.'
        return text

    def not_restored(self):
        return (''.join(f'damaged: {k}\n' for k in self.damaged) + ''.join(f'missing: {k}\n' for k in self.missing)
                + ''.join(f'accepted as lost: {k} ({why})\n' for k, why in self.accepted))


def copy_new(sources, destination, checked=None):
    """Copy every file (every version) into a new destination. A missing or unreadable
    file, or one that fails its checksum, stops the restore. Returns file count."""
    checked = checked or Checked(None)
    files = [(name, Path(source), path) for name, source in sources if Path(source).is_dir()
             for path, _ in backup_files(source)]
    ensure_room(destination.parent, sum(p.stat().st_size for _, _, p in files), RESTORE)
    checked.begin(destination)
    for name, source, path in files:
        checked.copy(path, destination / name / path.relative_to(source))
    return len(files)


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
        return dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).astimezone().strftime(SHOWN)
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


def copy_latest(sources, destination, output=print, checked=None):
    """Copy only the newest version of each file, under its own name. Shows the dates
    of files that had several versions, so you can see which one you get."""
    checked = checked or Checked(None)
    chosen = latest_versions(sources)
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
INVENTORY_LIMIT = 1 << 30   # unpacked; a list this big is not trusted


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


LAST_RUN = 'last-run.json'   # which list stands for the newest run (runtime inventory.rs LAST_RUN)
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


REPLACED = 'neko-replaced'   # <user>/neko-replaced/<when>/<path in files> (runtime backup.rs)


def replaced_since(key, sums):
    """A Nextcloud file (<user>/files/<path>) whose place was taken over by a folder, or
    one of whose folders was once a file: the backup moved what stood in the way into
    <user>/neko-replaced/<when>/, so a name in an old list may now hold other content."""
    user, _, rest = key.partition('/')
    if not rest.startswith('files/'):
        return False
    inside = rest[len('files/'):]
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
UNCERTAIN_WHY = ('they became a folder (or a folder became a file) after that backup, and its list is from before '
                 '0.39.7, which keeps no checksums, so which saved copy is theirs can\'t be told. "Everything ever '
                 'kept" brings back every saved copy of them.')


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
                chosen.append((Path(root) / saved, Path(name) / key[len(prefix):], sha))
    return chosen, missing, uncertain


def copy_listed(chosen, destination, checked):
    """Copy each listed file's saved copy under the file's own name, checked against the
    checksum the list recorded (when it has one)."""
    ensure_room(destination.parent, sum(p.stat().st_size for p, _, _ in chosen), RESTORE)
    checked.begin(destination)
    for path, target, sha in chosen:
        checked.copy(path, destination / target, sha)
    return len(chosen)


NO_INVENTORY = ('This backup has no list yet of which files you had at each backup (it is written by each '
                'weekly copy since 0.39.6), so files you deleted or renamed come back too.')


def stale_list_options(listed, newest_run, output):
    """The choices when the newest backup has no list of its own: everything ever kept
    first (it has that backup's files), the older list named by its date."""
    output(f'\nThe newest backup ({newest_run}) did not finish: it was cut off part-way (a power cut, a '
           'restart), or its list of the files you had then could not be written. So it has no such list. '
           f'The newest complete list is from {listed[0][1]} and leaves out every file backed up after it.')
    return [('everything', ('Everything ever kept, also files you deleted or renamed (recommended: it has '
                            'the newest backup\'s files)')),
            ('last', f'As of {listed[0][1]} (an older list: files backed up after it are left out)')]


def restore_scope(listed, prompt, output, newest_run=None):
    """('as_of', inventory path, date) or ('everything', None, None); None to go back.
    `newest_run`: the date of a newer backup run that has no list (see list_gap)."""
    if not listed:
        output(NO_INVENTORY)
        return 'everything', None, None
    if newest_run:
        options = stale_list_options(listed, newest_run, output)
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
    return None if pick is None else ('everything', None, None)


COPYING = 'Copying... this can take a while for a big backup.'


def missing_check(missing, checked, prompt, output, copies):
    """Say which checked files this backup lost, and stop if you say so. They are listed
    for the repair option first (`copies`: {saved copy: its recorded checksum}), so it
    has them whether you go on or stop here."""
    checked.missing = missing
    checked.publish(MISSING, copies)
    if missing:
        output(f'Missing from this backup, though backed up and checked before: {file_count(len(missing))} '
               f'(for example {", ".join(missing[:5])}).' + (f' {checked.other.capitalize()} may have them.'
                                                             if checked.other else ''))
        if not ask('Restore everything else from this backup now? ("no" stops, with nothing restored)', prompt, output):
            raise RuntimeError('Nothing was restored.' + (f' Use {checked.other} instead, or choose' if checked.other
                                                          else ' Choose') + ' "Repair damaged backup copies" in this menu.')


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


ACCEPTED = ', accepted as lost in the recovery menu'   # how accept_lost ends its reason
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
        output(f'Not restored, as you accepted them as lost in the repair menu: {file_count(len(accepted))} '
               f'(for example {accepted[0][0]}: {accepted[0][1]}).')
    checked.accepted = accepted
    return rest


def copy_as_of(sources, target, checked, inventory, output, prompt):
    """Only the files an inventory lists, each as it was then. None: the list is unusable."""
    path, date = inventory
    listed = read_inventory(path)
    if listed is None:
        output(f'The list of files from {date} could not be read, so everything ever kept is offered instead.')
        return None
    chosen, missing, uncertain = listed_versions(sources, checked.root, listed, checked.sums)
    if uncertain:
        output(f'Left out ({file_count(len(uncertain))}, for example {", ".join(uncertain[:5])}): {UNCERTAIN_WHY}')
    checked.uncertain = uncertain
    missing = accepted_check(missing, checked, listed, output)
    missing_check(missing, checked, prompt, output, missing_copies(missing, listed, checked.sums))
    output(f'Restoring the {file_count(len(chosen))} you had at the backup of {date}. {COPYING}')
    return copy_listed(chosen, target, checked)


def copy_choice(sources, target, prompt, output, ledger_root=None, other=None, report=None):
    """The files you had at a backup (when the backup lists them; recommended), else the
    newest version of each file ever kept, or every version as stored. Every file is
    checked against the checksum ledger at `ledger_root`; `other`: where else these
    files are backed up, if anywhere; `report`: where damaged and missing copies are
    published for the repair option (Checked.report). Returns (files restored, newest
    only, the check)."""
    checked = Checked(ledger_root, other)
    checked.report = report
    listed = inventories(ledger_root) if ledger_root else []
    scope = restore_scope(listed, prompt, output, list_gap(ledger_root, listed) if ledger_root else None)
    if scope is None:
        raise RuntimeError('Nothing was restored (you went back).')
    count = copy_as_of(sources, target, checked, scope[1:], output, prompt) if scope[0] == 'as_of' else None
    if count is not None:
        return count - len(checked.damaged), True, checked
    missing = checked.require_present(sources)
    missing_check(missing, checked, prompt, output, {key: checked.sums[key] for key in missing})
    latest = ask('Restore only the NEWEST version of each file? (recommended; "no" copies every version)',
                 prompt, output)
    output(COPYING)
    count = copy_latest(sources, target, output, checked) if latest else copy_new(sources, target, checked)
    return count - len(checked.damaged), latest, checked


VERSIONS_EXPLAINED = ('Every version was copied. A file\'s own name is its FIRST backed-up version; '
                      'copies named ".neko-<date>" are LATER edits, saved on the date in the name. '
                      'An edit can be undone later, so the latest date is not always the current '
                      'file: "newest only" restores the one the backup last saw.')


def restored(config):
    """The HDD folder that holds restores, created privately on first use."""
    folder = Path(config['media_mount']) / 'restored'
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def hdd_ready(config):
    """The HDD is mounted where the settings say, and it is the drive they name. When it
    has failed or is unplugged, its folder is a plain one on the system disk."""
    return identity_matches(Path(config['media_mount']), config.get('storage', {}).get('media_hdd', {}))


HDD_MISSING = ('The HDD is not mounted at {mount} (or another drive is there), so the usual restore folder '
               'there would be on the system disk and could fill it up. Connect the HDD (or restart) and '
               'try again, or type a folder on another drive with enough room.')


def restore_target(config, name, prompt, output, usb=None):
    """A NEW folder for a restore: suggested in the HDD's restore folder, but only while
    the HDD is mounted and is the right drive (never on the system disk under its empty
    folder); otherwise you type one on another drive."""
    avoid = backup_trees(config, usb)
    if hdd_ready(config):
        return new_folder(unused(restored(config) / name), prompt, output, avoid)
    output(HDD_MISSING.format(mount=config['media_mount']))
    return new_folder(None, prompt, output, avoid, refuse=(Path(config['media_mount']),))


def stamp():
    return dt.datetime.now().strftime('%Y%m%d-%H%M%S')


def unused(path):
    """`path`, or path-2, path-3... when it exists already: a restore made again soon
    after (one file was missing, say) is offered a folder of its own, not a refusal."""
    path = candidate = Path(path)
    number = 1
    while os.path.lexists(candidate):   # False when it can't be looked at: new_folder then says so
        number += 1
        candidate = path.with_name(f'{path.name}-{number}')
    return candidate


class Usb:
    """Mount the backup USB drive (read-only unless `writable`), if it is not mounted."""

    def __init__(self, config, writable=False):
        self.writable = writable
        self.mount = Path(config.get('external_backup_mount', '/mnt/nekoserver-photos'))
        photos = config.get('photos', {})
        self.uuid = photos.get('external_backup_uuid', '')
        self.fstype = photos.get('external_backup_fstype', 'ext4')
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
            raise RuntimeError('the weekly backup is using the USB drive right now. '
                               'Try again when it has finished (usually within an hour)') from None
        self.lock = fd
        try:
            return self.open()
        except BaseException:
            # A refused or failed open still unmounts what it mounted and frees the drive.
            self.__exit__()
            raise

    def open(self):
        if subprocess.run(['findmnt', '-n', '--mountpoint', str(self.mount)],
                          capture_output=True, check=False).returncode != 0:
            if not (Path('/dev/disk/by-uuid') / self.uuid).exists():
                return None
            self.mount.mkdir(parents=True, exist_ok=True)
            options = usb_mount_options(self.fstype, self.writable)
            if subprocess.run(['mount', '-o', options, f'UUID={self.uuid}', str(self.mount)],
                              capture_output=True, timeout=60, check=False).returncode != 0:
                return None
            self.mounted_here = True
        found = (findmnt('UUID,OPTIONS', self.mount) or '').split()
        if len(found) != 2 or found[0].lower() != self.uuid.lower():
            return None
        if not private_mount_options(found[1], self.fstype):
            raise RuntimeError('the USB drive is mounted so that other accounts on this computer could read '
                               'it. Unmount it (sudo umount ' + str(self.mount) + ') and try again: it is then mounted privately')
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
    media, data = Path(config['media_mount']), Path(config['data_mount'])
    archive = media / config.get('photos', {}).get('archive_dir', ARCHIVE_DIR)
    with Usb(config) as usb:
        sources = [('hdd', 'The HDD (weekly copy)')] if archive.is_dir() else []
        if usb and ((usb / 'upload').is_dir() or (usb / 'library').is_dir()):
            sources.append(('usb', 'The USB drive (weekly copy)'))
        if not sources:
            output('No photo backup found on the HDD or the USB drive (is the USB drive plugged in?).')
            return
        where = choose('Restore photos from:', sources, prompt, output)
        if where is None:
            return
        base = archive if where == 'hdd' else usb
        target = restore_target(config, f'photos-{stamp()}', prompt, output, usb)
        if target is None:
            return
        sources = [('upload', base / 'upload'), ('library', base / 'library')]
        # What a file that became a folder (or back) replaced, kept at the archive's top.
        if (base / REPLACED).is_dir() and not (base / REPLACED).is_symlink():
            sources.append((REPLACED_FOLDER, base / REPLACED))
        section = 'archive' if where == 'hdd' else 'usb_photos'
        count, latest, checked = copy_choice(sources, target, prompt, output, ledger_root=base,
                                             other='the USB drive' if where == 'hdd' else 'the HDD',
                                             report=functools.partial(note_findings, config, section))
        report_restore(checked, target, output)
    say(output, f'\nDone: {file_count(count)} copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        *handed_over(config, target),
        'To get them back into Immich: open Immich in a browser, click "Upload" and choose',
        'files from that folder (for thousands of files, the Immich command-line uploader is',
        'faster: see https://immich.app/docs/features/command-line-interface).',
        f'Your current Immich library ({data / "photos/immich-library"}) was not touched.')


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
        return False   # never written over: the runtime may be the one that can read it
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
    sums.update((k, sha) for k, sha in copies.items() if valid_ledger_path(k) and isinstance(sha, str) and SHA256.match(sha))
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


def give_to(folder, uid, gid):
    """Make a restore folder and everything in it belong to `uid`:`gid`. Walked through
    open folders (os.fwalk), never through a link: nothing outside it changes owner."""
    for _, dirs, names, fd in os.fwalk(folder, follow_symlinks=False):
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


def handed_over(config, target):
    """Restored photos and files are for you to open (and upload) from your own account,
    which can't read root's private files: they are given to that account. The HDD's
    restore folder lets that account's group in (other restores in it stay private to
    root). Returns the lines that say so, or how to get at them when that did not work."""
    uid, gid = owner(config)
    name, home = account(uid)
    try:
        give_to(target, uid, gid)
        if Path(target).parent == Path(config['media_mount']) / 'restored':
            os.chown(Path(target).parent, 0, gid)
            os.chmod(Path(target).parent, 0o750)
    except OSError as exc:
        return [f'They belong to root ({exc.strerror}), so your account can\'t open them yet. Run:',
                f'  sudo chown -R {name}: "{target}"']
    if not reachable(target, uid, gid):
        return [f'The folder now belongs to {name}, but that account can\'t get to where it is. Move it home:',
                f'  sudo mv "{target}" "{home}/"']
    return [f'The folder and its files now belong to {name}, so you can open them from your desktop.']


# Beside a user's backed-up files: what a later change of shape replaced (a file that
# became a folder, or a folder that became a file), under the date it was set aside and
# its own path (backup.rs REPLACED).
REPLACED = 'neko-replaced'
REPLACED_FOLDER = 'replaced earlier'   # its name in a restore


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
        user = choose('Restore whose Nextcloud files?', [(u, u) for u in users], prompt, output)
        if user is None:
            return
        target = restore_target(config, f'nextcloud-{user}-{stamp()}', prompt, output, usb)
        if target is None:
            return
        sources = cloud_sources(usb, user)
        # The USB drive holds the only copy of Nextcloud files: no "other backup".
        count, latest, checked = copy_choice(sources, target, prompt, output, ledger_root=usb / 'nextcloud',
                                             report=functools.partial(note_findings, config, 'usb_nextcloud'))
        report_restore(checked, target, output)
    ready = handed_over(config, target)
    if encrypted_by_nextcloud(target):
        say(output, f'\nDone: {file_count(count)} copied to {target}, but they are ENCRYPTED by Nextcloud\'s',
            'server-side encryption, and the keys to read them are not in this backup.',
            'Uploading them would not make them readable. They open only on the Nextcloud',
            'server that encrypted them (with its data folder and keys). Your current Nextcloud',
            'files were not touched.')
        return
    say(output, f'\nDone: {file_count(count)} copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        *(() if not (target / REPLACED_FOLDER).is_dir() else (
            '"replaced earlier" holds what was in the way when a file became a folder (or a folder',
            'a file), in a folder named after the date it was set aside.',)),
        *ready,
        'To put them back: open Nextcloud in a browser (or the desktop app) and upload the',
        'files you need. Your current Nextcloud files were not touched.')


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
    return isinstance(manifest, dict) and manifest.get('password_files') == 'unencrypted_in_data_tar' \
        and 'secrets/accounts' in (manifest.get('paths') or {}).get('data', []) and (Path(run) / 'data.tar').is_file()


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
    """(path, label) of monthly snapshots holding the password list, newest first."""
    runs = monthly_snapshots(Path(config['media_mount']) / MONTHLY_DIR)
    return [(p, d + ('' if (p / SEALED).is_file() else ' (not encrypted: made without a backup key)'))
            for p, d in runs if (p / SEALED).is_file() or plain_password_list(p)]


def restore_passwords(config, prompt, output):
    runs = password_snapshots(config)
    if not runs:
        output('No monthly snapshot with a password list on the HDD yet.')
        return
    run = choose('Restore the password list from which monthly snapshot?',
                 [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = new_folder(unused(Path('/root') / f'nekoserver-passwords-{stamp()}'), prompt, output, backup_trees(config))
    if target is None:
        return
    if not (run / SEALED).is_file():
        # The archive is checked against the checksum recorded when the snapshot was
        # made before anything is taken out of it: a changed byte would change a password.
        problem = archive_problem(run, 'data')
        if problem:
            output(problem + ' Nothing was restored. Use another monthly snapshot.')
            return
        target.mkdir(mode=0o700)
        result = subprocess.run(['tar', '--extract', '--file', str(run / 'data.tar'), '--directory', str(target),
                                 '--no-same-owner', 'secrets/accounts'], capture_output=True, text=True, check=False)
        if result.returncode:
            output('Could not take the password list out of that snapshot: ' + (result.stderr.strip() or 'tar failed'))
            return
        say(output, f'\nDone. Open it with:  sudo cat {target}/secrets/accounts/APP-CREDENTIALS.txt')
        return
    output('It will ask for the backup password chosen during setup (for backups made before 0.28.5: the sudo password you had then).')
    if tool('backup_secrets.py', '--restore', str(run), '--to', str(target)):
        say(output, f'\nDone. Open it with:  sudo cat {target}/secrets/accounts/APP-CREDENTIALS.txt',
            f'The apps\' own password files are there too: {target}/compose/<app>/.env and',
            f'{target}/appdata/nextcloud/html/config/config.php (for rebuilding from a monthly snapshot).')


def restore_nightly(config, kind, prompt, output):
    folder, script, label = (('backups/media-applications', 'app-recovery.py', 'app settings') if kind == 'apps'
                             else ('backups/host-user', 'host-recovery.py', 'Steam and Samba settings'))
    runs = snapshots(Path(config['media_mount']) / folder)
    if not runs:
        output(f'No nightly backup of {label} found on the HDD yet.')
        return
    # A night Steam was open has no Steam files: said in the list, not found out later.
    shown = [(p, d + (' (no Steam files: Steam was open)' if kind == 'host' and steam_left_out(p) else '')) for p, d in runs]
    run = choose(f'Restore {label} from which night?', shown, prompt, output)
    if run is None:
        return
    target = restore_target(config, f'{kind}-{stamp()}', prompt, output)
    if target is None:
        return
    # With their owners and permissions, so a file copied back works for its app.
    if tool(script, 'restore', '--snapshot', str(run), '--destination', str(target), '--preserve-owners'):
        say(output, f'\nDone: restored into {target} (checksums verified), with each file\'s owner and permissions.',
            *(('This night has no Steam files (Steam was open then): choose another night for those.',)
              if kind == 'host' and steam_left_out(run) else ()),
            *(('Its folders are named source0, source1 and so on, one for each folder that was backed up;',
               f'{target / "restore-report.json"} says which original folder each one is.')
              if kind == 'host' else ()),
            'To use a file from it, stop the app first, copy the file back keeping its owner',
            '(sudo cp -a <file> <where it belongs>), then start the app.',
            'See APP-BACKUPS.md or HOST-BACKUPS.md for the exact steps.')


def restore_monthly(config, prompt, output):
    runs = monthly_snapshots(Path(config['media_mount']) / MONTHLY_DIR, need='restore-tests.json')
    if not runs:
        output('No complete monthly snapshot on the HDD yet.')
        return
    run = choose('Use which monthly snapshot?', [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = restore_target(config, f'monthly-{stamp()}', prompt, output)
    if target is None:
        return
    images = ask('Also load the saved app versions into Docker (needed on a new server)?', prompt, output)
    args = ['--snapshot', str(run), '--stage', str(target)] + (['--load-images'] if images else [])
    if tool('monthly-recovery.py', *args):
        try:
            sealed = json.loads((Path(run) / MANIFEST).read_text()).get('password_files') != 'unencrypted_in_data_tar'
        except (OSError, ValueError, AttributeError):
            sealed = True
        say(output, f'\nDone: checked and unpacked into {target}.',
            *(('Nothing live was replaced. The app password files (.env files and Nextcloud\'s',
               'config.php) are not in it: get them with "App password list" in this menu.') if sealed else
              ('Nothing live was replaced. This snapshot was made without a backup key, so the app',
               'password files (.env files and Nextcloud\'s config.php) are in it, unencrypted.')),
            'To rebuild a server from it, follow "disaster recovery" in MONTHLY-MAINTENANCE.md.')


def legacy_usb_snapshots(usb):
    """Full monthly snapshots releases before v0.21.1 wrote to the USB drive, oldest first."""
    found = []
    for path in monthly_snapshots(Path(usb) / MONTHLY_DIR):
        try:
            if json.loads((path[0] / MANIFEST).read_text()).get('format') == 'neko-maintenance-v1':
                found.append(path)
        except (OSError, ValueError):
            continue
    return list(reversed(found))


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
    return sorted({key for name in PROBLEM_LISTS for key in section.get(name, [])
                   if isinstance(key, str) and valid_ledger_path(key)})


FOLDER_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
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
        self.gone = None if rel is not None else (f'the {self.drive()}\'s backup folder in the settings ("archive_dir") '
                                                  'is not a plain folder inside it')

    def drive(self):
        return DRIVE_NAMES[self.label]

    def open(self):
        """Open the drive's folder; True when it is the right drive. Otherwise `gone` says why."""
        if self.gone is None and self.ready():
            self.fd = os.open(self.mount, FOLDER_FLAGS)
            self.dev = os.fstat(self.fd).st_dev
            if self.same():     # still the drive that was checked, now that it is open
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
            with (good.reader() if isinstance(good, Saved) else open(good, 'rb')) as source:
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
        chosen = choose('Which copy is the right one?', options + [(None, 'Keep both as they are')],
                        prompt, output)
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
    options = [(c, describe(c[0], c[1]) + ' (matches the ' + ' and '.join(matches[c[2]]) + ' list)') for c in candidates]
    chosen = choose('Which copy is the right one?', options + [(None, 'Keep both as they are')], prompt, output)
    if chosen is None:
        return None, 'the backups disagree; both copies were kept as they are'
    return chosen[2], chosen[1]


NO_GOOD_COPY = 'no copy matches its recorded checksum any more; every copy was kept untouched'
LOST_OPTION = ('Accept that this saved copy is lost: move it to the "neko-damaged" folder (kept, never '
               'deleted) and stop checking it, so the monthly snapshot and updates go on')


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
    say(output, f'\n{key}: the saved copy ' + ('is missing' if gone else 'no longer matches its recorded checksum')
        + ', and no other copy (nor the original, which changed or is gone) holds what was saved,'
        ' so it cannot be repaired. Until you decide, the monthly check finds it again and the'
        ' monthly snapshot and updates wait.')
    pick = choose('What do you want to do?', [('keep', 'Keep it as it is for now'), ('lost', LOST_OPTION)],
                  prompt, output)
    if pick != 'lost':
        return NO_GOOD_COPY, set()
    if not all(tree.usable() for _, tree, _ in present):   # a drive went away while you chose
        return 'nothing was changed: ' + '; '.join(t.gone for _, t, _ in present if t.gone), set()
    for _, _, saved in present:
        if saved.lexists():
            saved.set_aside(aside)
    why = ('missing' if gone else f'damaged; kept in {QUARANTINE}/{aside}') + ', accepted as lost in the recovery menu'
    for tree, known in ledgers:
        if key in known and tree.usable():
            record_ledger(tree.root, {}, 'lost', retire={key: why})
            del known[key]
    return ('accepted as lost' + ('' if gone else f' (the damaged copy is kept in {QUARANTINE}/{aside})'),
            {label for label, _, _ in present})


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
    said += [f'the {label} copy was written, but its checksum was NOT recorded ({why}), so it stays listed'
             for label, why in unrecorded.items()]
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
CHECKSUMS = 'checksums'   # {saved copy: recorded SHA-256} beside a restore's findings (note_findings)


def usb_ready(config, mount):
    """The USB drive mounted at `mount` is the one the settings name: its UUID and
    filesystem, the whole filesystem (as the weekly backup and Usb mount it)."""
    photos = config.get('photos', {})
    found = mounted_identity(Path(mount))
    return (found is not None and found[0].lower() == str(photos.get('external_backup_uuid', '')).lower()
            and found[1] == photos.get('external_backup_fstype', 'ext4') and found[2] == '')


def open_trees(config, usb, output):
    """{'hdd', 'usb', 'cloud'}: the backup trees the repair works on, each opened only when
    its drive is the one the settings name. One that can't be used is said, once."""
    rel = str(config.get('photos', {}).get('archive_dir', ARCHIVE_DIR)).rstrip('/')
    trees = {'hdd': Tree('HDD', config['media_mount'], rel if valid_ledger_path(rel) else None,
                         functools.partial(hdd_ready, config))}
    if usb:
        ready = functools.partial(usb_ready, config, usb)
        trees.update(usb=Tree('USB', usb, '', ready), cloud=Tree('USB', usb, 'nextcloud', ready))
    for tree in trees.values():
        tree.open()
    for tree in {tree.label: tree for tree in trees.values() if tree.gone}.values():
        output(f'  Not used: {tree.gone}. Its copies were NOT repaired, and their warnings stay listed. '
               f'Connect the right drive (or restart the computer), then choose this again.')
    return trees


def repair_jobs(config, work, trees, output):
    """[(keys, trees, originals folder or None, sections, every copy here, trees away)]."""
    media, data = Path(config['media_mount']), Path(config['data_mount'])
    hdd, usb = trees["hdd"], trees.get("usb")
    photos = [tree for tree in (hdd, usb) if tree is not None and not tree.gone]
    away = [tree for tree in (hdd, usb) if tree is not None and tree.gone]
    keys = sorted(set(work['archive']) | set(work['usb_photos']))
    # A live original is read only from the drive the settings name (Immich's on the
    # SSD, Nextcloud's on the HDD).
    jobs = []
    if keys and photos:
        # A photo copy is given up as lost only when both of its copies were looked at.
        jobs.append((keys, photos, data / 'photos/immich-library' if ssd_ready(config) else None,
                     ('archive', 'usb_photos'), len(photos) == 2, away))
    jobs += cloud_job(work, trees, media, output)
    return jobs


def cloud_job(work, trees, media, output):
    """The Nextcloud copy's repair job (repair_jobs), or none: Nextcloud's live files are
    read only from the HDD the settings name."""
    hdd, usb, cloud = trees['hdd'], trees.get('usb'), trees.get('cloud')
    if cloud is not None and not cloud.gone:
        return [(work['usb_nextcloud'], [cloud], None if hdd.gone else media / 'cloud/files', ('usb_nextcloud',),
                 True, [])]
    if usb is None and (work['usb_nextcloud'] or work['usb_photos']):
        output('  The USB drive is not plugged in: its copies were not checked and stay listed.')
    return []


def reported_sums(sections):
    """{saved copy: checksum} a restore recorded beside its findings (when the sections agree)."""
    sums = {}
    for name in INTEGRITY_SECTIONS:
        found = sections.get(name, {}).get(CHECKSUMS)
        for key, sha in (found.items() if isinstance(found, dict) else ()):
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
    say(output, '\nRepair damaged backup copies',
        'Each file is compared with its recorded SHA-256 checksum and fixed from a copy that',
        'still matches it. Damaged copies are moved to a "neko-damaged" folder, never deleted.')
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


def review_old_usb(config, prompt, output):
    """List the old snapshots; optionally keep a copy on the HDD; remove only on 'DELETE'.
    They may be the only place a long-deleted file still exists, so nothing is automatic."""
    with Usb(config, writable=True) as usb:
        runs = legacy_usb_snapshots(usb) if usb else []
        if not runs:
            output('No old full snapshots on the USB drive (or it is not plugged in).')
            return
        gb = 1024 ** 3
        output(f'\n{len(runs)} old full snapshots on the USB drive:')
        for path, label in runs:
            output(f'  {label}  {tree_bytes(path) / gb:.1f} GB')
        say(output, 'They were made before v0.21.1 and hold everything at that time, including photos and',
            'Nextcloud files. A file deleted from the server since then may exist only in them.')
        if ask('Keep a copy of them on the HDD first? (recommended)', prompt, output):
            target = restore_target(config, f'old-usb-snapshots-{stamp()}', prompt, output, usb)
            if target is None:
                return
            output('Copying...')
            count = copy_new([(path.name, path) for path, _ in runs], target)
            output(f'Copied {count} files to {target}.')
        answer = prompt('Type DELETE to remove them from the USB drive (anything else keeps them): ').strip()
        if answer != 'DELETE':
            output('Kept. Nothing was removed.')
            return
        for path, _ in runs:
            shutil.rmtree(path)
        output(f'Removed {len(runs)} old snapshots from the USB drive.')


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


def run(prompt=input, output=print, config_path=None):
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh --recover')
        return 2
    config = settings(config_path, output)
    say(output, '\nNekoServer recovery',
        'Everything is restored into a NEW folder. Nothing you have now is changed.')
    handlers = {
        'photos': restore_photos, 'cloud': restore_cloud, 'passwords': restore_passwords,
        'apps': lambda c, p, o: restore_nightly(c, 'apps', p, o),
        'host': lambda c, p, o: restore_nightly(c, 'host', p, o),
        'monthly': restore_monthly,
        'old-usb': review_old_usb,
        'repair': repair_backups,
    }
    started = False
    try:
        while True:
            pick = choose('What do you want to get back?', MENU, prompt, output, leave='Quit')
            if pick is None:
                output('Done.')
                return 0
            started = True
            try:
                handlers[pick](config, prompt, output)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                output(f'That restore did not finish: {exc}')
    except (EOFError, KeyboardInterrupt):
        # Restores and repairs finished before this stay as they are.
        output('\nStopped. ' + ('Nothing more was changed; what was already restored or repaired stays as it is.'
                               if started else 'Nothing was changed.'))
        return 1
