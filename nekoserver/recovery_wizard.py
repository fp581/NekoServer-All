"""Guided recovery: `sudo bash install.sh --recover`.

A menu over the existing restore tools. Everything is restored into a NEW folder;
live data is never overwritten. Each restore ends with what to do next.
"""
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zlib

import guided_setup
from neko_common import (QUARANTINE, BackupDamaged, atomic_json, backup_files, copy_checked, ensure_room, findmnt, read_ledger,
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
    ('old-usb', 'Old full snapshots on the USB drive (from before v0.21.1): review or remove'),
    ('repair', 'Repair damaged backup copies (found by the checksum checks)'),
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


def choose(title, options, prompt=input, output=print):
    """Numbered choice; returns the chosen value or None for 0 / back."""
    output('\n' + title)
    for number, (_, label) in enumerate(options, 1):
        output(f'  {number}. {label}')
    output('  0. Back')
    while True:
        answer = prompt('Choose a number: ').strip()
        if answer == '0':
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        output('Please type one of the numbers shown.')


def when(micros):
    try:
        return dt.datetime.fromtimestamp(int(micros) / 1e6).strftime('%Y-%m-%d %H:%M')
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


def folder_problem(path, avoid=()):
    """Why a restore destination cannot be used, or None if it can. `avoid`: the backup
    trees: a restore inside one would become part of that backup (and of the next restore
    from it), and one around a backup would hold it."""
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


def new_folder(suggested, prompt=input, output=print, avoid=()):
    """Agree a destination that does not exist yet, inside an existing folder and outside
    every backup tree in `avoid`."""
    path = Path(suggested)
    while path is not None:
        output(f'\nRestore into this new folder:\n  {path}')
        if ask('Use this folder?', prompt, output):
            problem = folder_problem(path, avoid)
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


class Checked:
    """Restores check every file against the backup tree's checksum ledger. A file the
    ledger has no checksum for yet is restored but counted as unverified. A file damaged
    on this backup, or missing from it, is not restored and is listed (the other backup
    may have it): one bad file never keeps all the good ones from coming back."""

    def __init__(self, ledger_root):
        self.root = Path(ledger_root) if ledger_root else None
        self.ledger_problem = None
        try:
            self.sums = read_ledger(self.root) if self.root else {}
        except RuntimeError as exc:
            # A checksum list the drive lost mid-write: the files are restored unchecked.
            self.sums, self.ledger_problem = {}, str(exc)
        self.verified = 0
        self.unverified = 0
        self.damaged = []
        self.missing = []

    def copy(self, source, target):
        key = None
        if self.root:
            try:
                key = Path(source).relative_to(self.root).as_posix()
            except ValueError:
                key = None
        expected = self.sums.get(key)
        try:
            copy_checked(source, target, expected)
        except BackupDamaged:
            Path(target).unlink(missing_ok=True)
            self.damaged.append(key or str(source))
            return
        if expected:
            self.verified += 1
        else:
            self.unverified += 1

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
        self.missing = missing
        return missing

    def summary(self, destination=None):
        text = f'{self.verified} files matched their recorded checksums'
        if self.unverified:
            text += (f'; {self.unverified} had no recorded checksum yet (backed up before checksums '
                     'were kept), so they could not be checked')
        text += '.'
        if self.ledger_problem:
            text += f' This backup\'s checksum list could not be used ({self.ledger_problem}), so no file was checked.'
        left = self.damaged + self.missing
        if left:
            text += (f' {len(left)} files were NOT restored, because this backup has them damaged ({len(self.damaged)}) '
                     f'or no longer has them ({len(self.missing)}). Get those from the other backup (HDD or USB), '
                     'or try the recovery menu\'s repair option.')
            if destination is not None and Path(destination).is_dir():
                listed = Path(destination) / 'NOT-RESTORED.txt'
                listed.write_text(''.join(f'damaged: {k}\n' for k in self.damaged) + ''.join(f'missing: {k}\n' for k in self.missing))
                text += f' Their names are in {listed}.'
        return text


def copy_new(sources, destination, checked=None):
    """Copy every file (every version) into a new destination. A missing or unreadable
    file, or one that fails its checksum, stops the restore. Returns file count."""
    checked = checked or Checked(None)
    files = [(name, Path(source), path) for name, source in sources if Path(source).is_dir()
             for path, _ in backup_files(source)]
    ensure_room(destination.parent, sum(p.stat().st_size for _, _, p in files), RESTORE)
    destination.mkdir(mode=0o700)
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
        return dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').strftime('%Y-%m-%d %H:%M UTC')
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
    destination.mkdir(mode=0o700)
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
# .neko-inventory/<UTC time>.json.gz at the backup's root (runtime inventory.rs).
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


def read_inventory(path):
    """{path in the backup: name of the saved copy beside it}, or None when the list is
    missing, damaged or of an unknown kind (then nothing is restored from it)."""
    try:
        with gzip.open(path, 'rb') as stream:
            raw = stream.read(INVENTORY_LIMIT + 1)
        value = json.loads(raw) if len(raw) <= INVENTORY_LIMIT else None
    except (OSError, EOFError, ValueError, zlib.error):
        return None
    files = value.get('files') if isinstance(value, dict) and value.get('format') == 'neko-inventory-v1' else None
    if not isinstance(files, dict):
        return None
    for key, copy in files.items():
        if not valid_ledger_path(key) or not isinstance(copy, str) or not valid_ledger_path(copy) or '/' in copy:
            return None
    return files


def plain_file(root, key):
    """A regular file at `key` under `root`, reached without following any link."""
    path = Path(root)
    for part in key.split('/'):
        path = path / part
        if path.is_symlink():
            return False
    return path.is_file()


def listed_versions(sources, root, files):
    """([(saved copy, path in the restore)], [paths missing from the backup]) for the
    files an inventory lists under the restored folders."""
    chosen, missing = [], []
    for name, folder in sources:
        try:
            prefix = Path(folder).relative_to(root).as_posix() + '/'
        except ValueError:
            continue
        for key, copy in sorted(files.items()):
            if not key.startswith(prefix):
                continue
            saved = key.rsplit('/', 1)[0] + '/' + copy
            if plain_file(root, saved):
                chosen.append((Path(root) / saved, Path(name) / key[len(prefix):]))
            else:
                missing.append(key)
    return chosen, missing


def copy_listed(chosen, destination, checked):
    """Copy each listed file's saved copy under the file's own name."""
    ensure_room(destination.parent, sum(p.stat().st_size for p, _ in chosen), RESTORE)
    destination.mkdir(mode=0o700)
    for path, target in chosen:
        checked.copy(path, destination / target)
    return len(chosen)


NO_INVENTORY = ('This backup has no list yet of which files you had at each backup (it is written by each '
                'weekly copy since 0.39.6), so files you deleted or renamed come back too.')


def restore_scope(listed, prompt, output):
    """('as_of', inventory path, date) or ('everything', None, None); None to go back."""
    if not listed:
        output(NO_INVENTORY)
        return 'everything', None, None
    options = [('last', f'As of the last backup ({listed[0][1]}): only the files you had then (recommended)')]
    if len(listed) > 1:
        options.append(('date', 'As of an earlier backup: choose the date'))
    options.append(('everything', 'Everything ever kept, also files you deleted or renamed'))
    pick = choose('Which files?', options, prompt, output)
    if pick == 'date':
        path = choose('As of which backup?', listed[1:], prompt, output)
        return None if path is None else ('as_of', path, dict(listed)[path])
    if pick == 'last':
        return 'as_of', listed[0][0], listed[0][1]
    return None if pick is None else ('everything', None, None)


def missing_check(missing, checked, prompt, output):
    """Say which checked files this backup lost, and stop if you say so."""
    if missing:
        output(f'{len(missing)} files that were backed up and checked are missing from this backup '
               f'(for example {", ".join(missing[:5])}). The other backup (HDD or USB) may have them.')
        if not ask('Restore everything else from this backup now? ("no" stops, with nothing restored)', prompt, output):
            raise RuntimeError('Nothing was restored. Use the other backup (HDD or USB), or the recovery menu\'s repair option.')
    checked.missing = missing


def copy_as_of(sources, target, checked, inventory, output, prompt):
    """Only the files an inventory lists, each as it was then. None: the list is unusable."""
    path, date = inventory
    files = read_inventory(path)
    if files is None:
        output(f'The list of files from {date} could not be read, so everything ever kept is offered instead.')
        return None
    chosen, missing = listed_versions(sources, checked.root, files)
    missing_check(missing, checked, prompt, output)
    output(f'Restoring the {len(chosen)} files you had at the backup of {date}.')
    return copy_listed(chosen, target, checked)


def copy_choice(sources, target, prompt, output, ledger_root=None):
    """The files you had at a backup (when the backup lists them; recommended), else the
    newest version of each file ever kept, or every version as stored. Every file is
    checked against the checksum ledger at `ledger_root`."""
    checked = Checked(ledger_root)
    scope = restore_scope(inventories(ledger_root) if ledger_root else [], prompt, output)
    if scope is None:
        raise RuntimeError('Nothing was restored (you went back).')
    count = copy_as_of(sources, target, checked, scope[1:], output, prompt) if scope[0] == 'as_of' else None
    if count is not None:
        output(checked.summary(target))
        return count - len(checked.damaged), True
    missing_check(checked.require_present(sources), checked, prompt, output)
    if ask('Restore only the NEWEST version of each file? (recommended; "no" copies every version)',
           prompt, output):
        count, latest = copy_latest(sources, target, output, checked), True
    else:
        count, latest = copy_new(sources, target, checked), False
    output(checked.summary(target))
    return count - len(checked.damaged), latest


VERSIONS_EXPLAINED = ('Every version was copied. A file\'s own name is its FIRST backed-up version; '
                      'copies named ".neko-<date>" are LATER edits, saved on the date in the name. '
                      'An edit can be undone later, so the latest date is not always the current '
                      'file: "newest only" restores the one the backup last saw.')


def restored(config):
    """The HDD folder that holds restores, created privately on first use."""
    folder = Path(config['media_mount']) / 'restored'
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def stamp():
    return dt.datetime.now().strftime('%Y%m%d-%H%M')


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
        target = new_folder(restored(config) / f'photos-{stamp()}', prompt, output, backup_trees(config, usb))
        if target is None:
            return
        output('Copying... this can take a while for a big library.')
        count, latest = copy_choice([('upload', base / 'upload'), ('library', base / 'library')], target, prompt, output,
                                    ledger_root=base)
    say(output, f'\nDone: {count} files copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        'To get them back into Immich: open Immich in a browser, click "Upload" and choose',
        'files from that folder (for thousands of files, the Immich command-line uploader is',
        'faster: see https://immich.app/docs/features/command-line-interface).',
        f'Your current Immich library ({data / "photos/immich-library"}) was not touched.')


# Beside a user's backed-up files: what a later change of shape replaced (a file that
# became a folder, or a folder that became a file), under the date it was set aside and
# its own path (backup.rs REPLACED).
REPLACED = 'neko-replaced'


def cloud_sources(usb, user):
    """The user's files, and what was set aside when a file became a folder (or back)."""
    sources = [('files', usb / 'nextcloud' / user / 'files')]
    replaced = usb / 'nextcloud' / user / REPLACED
    if replaced.is_dir() and not replaced.is_symlink():
        sources.append(('replaced earlier', replaced))
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
        target = new_folder(restored(config) / f'nextcloud-{user}-{stamp()}', prompt, output, backup_trees(config, usb))
        if target is None:
            return
        output('Copying...')
        sources = cloud_sources(usb, user)
        count, latest = copy_choice(sources, target, prompt, output, ledger_root=usb / 'nextcloud')
    if encrypted_by_nextcloud(target):
        say(output, f'\nDone: {count} files copied to {target}, but they are ENCRYPTED by Nextcloud\'s',
            'server-side encryption, and the keys to read them are not in this backup.',
            'Uploading them would not make them readable. They open only on the Nextcloud',
            'server that encrypted them (with its data folder and keys). Your current Nextcloud',
            'files were not touched.')
        return
    say(output, f'\nDone: {count} files copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        *(() if len(sources) == 1 else (
            '"replaced earlier" holds what was in the way when a file became a folder (or a folder',
            'a file), in a folder named after the date it was set aside.',)),
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
    target = new_folder(Path('/root') / f'nekoserver-passwords-{stamp()}', prompt, output, backup_trees(config))
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
    target = new_folder(restored(config) / f'{kind}-{stamp()}', prompt, output, backup_trees(config))
    if target is None:
        return
    # With their owners and permissions, so a file copied back works for its app.
    if tool(script, 'restore', '--snapshot', str(run), '--destination', str(target), '--preserve-owners'):
        say(output, f'\nDone: restored into {target} (checksums verified), with each file\'s owner and permissions.',
            *(('This night has no Steam files (Steam was open then): choose another night for those.',)
              if kind == 'host' and steam_left_out(run) else ()),
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
    target = new_folder(restored(config) / f'monthly-{stamp()}', prompt, output, backup_trees(config))
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


PROBLEM_LISTS = ('damaged', 'both_damaged', 'disagree', 'missing')


def integrity_sections(config):
    """What the last checksum checks found, per backup tree (reports/integrity.json)."""
    try:
        value = json.loads((Path(config['data_mount']) / 'reports/integrity.json').read_text())
    except (OSError, ValueError):
        return {}
    return value.get('sections', {}) if isinstance(value, dict) and value.get('format') == 'neko-integrity-v1' else {}


def listed(section):
    """Files a check reported as damaged or mismatched (only plain relative paths)."""
    return sorted({key for name in PROBLEM_LISTS for key in section.get(name, [])
                   if isinstance(key, str) and valid_ledger_path(key)})


def replace_copy(good, bad, root, key, expected, aside):
    """Move a bad copy (if any) to <root>/neko-damaged/<time>/<key>, then copy the good
    one in its place, checked against `expected` (SHA-256)."""
    if bad.exists() or bad.is_symlink():
        kept = Path(root) / QUARANTINE / aside / key
        kept.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(bad, kept)
    copy_checked(good, bad, expected)


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
        return None, 'no copy matches its recorded checksum any more; every copy was kept untouched'
    return expected, good


def repair_one(key, copies, ledgers, original, prompt, output, aside):
    """Repair one file of a tree. `copies`: [(label, root)] that should hold it under `key`
    (HDD archive and USB copy, or just the USB copy); `ledgers`: [(root, its ledger)];
    `original`: a live source (the Immich library or Nextcloud) that may still hold it.
    Returns (what happened, labels of the copies now verified to match)."""
    present = [(label, root, Path(root) / key) for label, root in copies]
    labels = {str(root): label for label, root in copies}
    recorded = {labels.get(str(root), str(root)): known[key] for root, known in ledgers if key in known}
    expected = next(iter(recorded.values()), None)
    sums = {label: sha256_file(path) if path.is_file() else None for label, _, path in present}
    live = sha256_file(original) if original and original.is_file() else None
    if live is not None and live_name(key) != key and expected is None:
        # The live file is a later edit of a dated version: it can only stand in for
        # it when it matches the recorded checksum, and there is none.
        live = None
    conflict = len(set(recorded.values())) > 1
    expected, good = decide(key, present, sums, recorded, expected, live, original, prompt, output)
    if expected is None:
        return good, set()
    settled = {key} if conflict else set()
    fixed = []
    for label, root, path in present:
        if sums[label] != expected:
            replace_copy(good, path, root, key, expected, aside)
            fixed.append(label)
    # The checksum is recorded after the files are in place (it is never replaced).
    for root, known in ledgers:
        if key not in known or known[key] != expected:
            record_ledger(root, {key: expected}, 'chosen', replace=settled)
            known[key] = expected
    text = 'repaired the ' + ' and '.join(fixed) + ' copy' if fixed else 'already intact'
    return text, {label for label, _, _ in present}


def repair_backups(config, prompt, output):
    """Fix the files the checksum checks reported, from a copy that still matches."""
    from recovery_tools import lock
    sections = integrity_sections(config)
    work = {name: listed(sections.get(name, {})) for name in ('archive', 'usb_photos', 'usb_nextcloud')}
    if not any(work.values()):
        output('The last checksum checks found nothing to repair.')
        return
    media, data = Path(config['media_mount']), Path(config['data_mount'])
    archive = media / config.get('photos', {}).get('archive_dir', ARCHIVE_DIR)
    aside = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    say(output, '\nRepair damaged backup copies',
        'Each file is compared with its recorded SHA-256 checksum and fixed from a copy that',
        'still matches it. Damaged copies are moved to a "neko-damaged" folder, never deleted.')
    resolved = {name: [] for name in work}
    # A warning is cleared only for the copies it is about, once each was checked:
    # the archive check is about the HDD copy, the USB checks about the USB copies.
    needs = {'archive': {'HDD'}, 'usb_photos': {'HDD', 'USB'}, 'usb_nextcloud': {'USB'}}
    with lock(config, changes_apps=False), Usb(config, writable=True) as usb:
        copies = [('HDD', archive)] + ([('USB', usb)] if usb else [])
        jobs = [(sorted(set(work['archive']) | set(work['usb_photos'])), copies,
                 data / 'photos/immich-library', ('archive', 'usb_photos'))]
        if usb:
            jobs.append((work['usb_nextcloud'], [('USB', usb / 'nextcloud')], media / 'cloud/files', ('usb_nextcloud',)))
        elif work['usb_nextcloud'] or work['usb_photos']:
            output('  The USB drive is not plugged in: its copies were not checked and stay listed.')
        for keys, trees, originals, names in jobs:
            ledgers = [(root, read_ledger(root)) for _, root in trees]
            for key in keys:
                result, verified = repair_one(key, trees, ledgers, originals / live_name(key, trees[0][1]), prompt, output, aside)
                output(f'  {key}: {result}')
                for name in names:
                    if needs[name] <= verified:
                        resolved[name].append(key)
    clear_resolved(config, resolved)
    say(output, '\nDone. The next backup run checks everything again.')


def clear_resolved(config, resolved):
    """Take repaired files off the dashboard's list until the next check runs."""
    path = Path(config['data_mount']) / 'reports/integrity.json'
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    for name, keys in resolved.items():
        section = value.get('sections', {}).get(name)
        if not isinstance(section, dict) or not keys:
            continue
        for kind in PROBLEM_LISTS:
            rows = section.get(kind, [])
            section[kind] = [k for k in rows if k not in keys]
            counts = section.setdefault('counts', {})
            counts[kind] = max(0, counts.get(kind, len(rows)) - (len(rows) - len(section[kind])))
    atomic_json(path, value)


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
            target = new_folder(restored(config) / f'old-usb-snapshots-{stamp()}', prompt, output, backup_trees(config, usb))
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
    try:
        while True:
            pick = choose('What do you want to get back?', MENU, prompt, output)
            if pick is None:
                output('Done.')
                return 0
            try:
                handlers[pick](config, prompt, output)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                output(f'That restore did not finish: {exc}')
    except (EOFError, KeyboardInterrupt):
        output('\nStopped. Nothing was changed.')
        return 1
