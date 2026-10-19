"""Guided recovery: `sudo bash install.sh --recover`.

A menu over the existing restore tools. Everything is restored into a NEW folder;
live data is never overwritten. Each restore ends with what to do next.
"""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import guided_setup
from neko_common import (QUARANTINE, atomic_json, backup_files, copy_checked, ensure_room, read_ledger,
                         record_ledger, settings_path, sha256_file, valid_ledger_path)

ROOT = Path(__file__).resolve().parent
# The weekly USB backup holds this lock while it mounts, copies and unmounts the drive.
USB_LOCK = Path('/run/nekoserver-photo-backup.lock')
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


def folder_problem(path):
    """Why a restore destination cannot be used, or None if it can."""
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


def new_folder(suggested, prompt=input, output=print):
    """Agree a destination that does not exist yet, inside an existing folder."""
    path = Path(suggested)
    while path is not None:
        output(f'\nRestore into this new folder:\n  {path}')
        if ask('Use this folder?', prompt, output):
            problem = folder_problem(path)
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
    ledger has no checksum for yet is restored but counted as unverified."""

    def __init__(self, ledger_root):
        self.root = Path(ledger_root) if ledger_root else None
        self.sums = read_ledger(self.root) if self.root else {}
        self.verified = 0
        self.unverified = 0

    def copy(self, source, target):
        key = None
        if self.root:
            try:
                key = Path(source).relative_to(self.root).as_posix()
            except ValueError:
                key = None
        expected = self.sums.get(key)
        copy_checked(source, target, expected)
        if expected:
            self.verified += 1
        else:
            self.unverified += 1

    def summary(self):
        text = f'{self.verified} files matched their recorded checksums'
        if self.unverified:
            text += (f'; {self.unverified} had no recorded checksum yet (backed up before checksums '
                     'were kept), so they could not be checked')
        return text + '.'


def copy_new(sources, destination, checked=None):
    """Copy every file (every version) into a new destination. A missing or unreadable
    file, or one that fails its checksum, stops the restore. Returns file count."""
    checked = checked or Checked(None)
    files = [(name, Path(source), path) for name, source in sources if Path(source).is_dir()
             for path, _ in backup_files(source)]
    ensure_room(destination.parent, sum(p.stat().st_size for _, _, p in files), 'The restore')
    destination.mkdir(mode=0o700)
    for name, source, path in files:
        checked.copy(path, destination / name / path.relative_to(source))
    return len(files)


# The weekly backup never overwrites: the first copy keeps the file's own name, and
# each later edit is kept beside it as name.neko-<UTC time the backup saw it>[-n].ext.
# So the dated copies are the NEWER versions.
VERSIONED = re.compile(r'^(?P<stem>.+)\.neko-(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<n>\d+))?(?:\.(?P<ext>[^.]+))?$')


def version_of(name):
    """(the file's own name, sort key); the first copy (no date) sorts oldest."""
    match = VERSIONED.match(name)
    if not match:
        return name, ('', 0)
    original = match['stem'] + ('.' + match['ext'] if match['ext'] else '')
    return original, (match['stamp'], int(match['n'] or 0))


def readable(stamp):
    try:
        return dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').strftime('%Y-%m-%d %H:%M UTC')
    except ValueError:
        return 'first backup'


LATEST_NOTE = '.neko-latest'


def noted_latest(folder, original):
    """The saved copy the weekly backup last saw as current (from its note), or None.
    Backups made before 0.24.3 have no notes; then the newest date wins."""
    try:
        name = (folder / f'.{original}{LATEST_NOTE}').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return None
    return name if name and '/' not in name else None


def latest_versions(sources):
    """[(source file, path in the restore, date of that version, how many versions)]:
    the current version of every file, restored under the file's own name."""
    groups = {}
    for name, source in sources:
        if not Path(source).is_dir():
            continue
        for path, _ in backup_files(source):
            original, key = version_of(path.name)
            groups.setdefault(Path(name) / path.parent.relative_to(source) / original, []).append((key, path, original))
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
    ensure_room(destination.parent, sum(p.stat().st_size for p, _, _, _ in chosen), 'The restore')
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


def copy_choice(sources, target, prompt, output, ledger_root=None):
    """Newest version of each file (recommended), or every version as stored. Every
    file is checked against the checksum ledger at `ledger_root`."""
    checked = Checked(ledger_root)
    if ask('Restore only the NEWEST version of each file? (recommended; "no" copies every version)',
           prompt, output):
        result = copy_latest(sources, target, output, checked), True
    else:
        result = copy_new(sources, target, checked), False
    output(checked.summary())
    return result


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
        if subprocess.run(['findmnt', '-n', '--mountpoint', str(self.mount)],
                          capture_output=True).returncode != 0:
            if not (Path('/dev/disk/by-uuid') / self.uuid).exists():
                return None
            self.mount.mkdir(parents=True, exist_ok=True)
            options = ('rw' if self.writable else 'ro') + ',nosuid,nodev,noexec'
            if subprocess.run(['mount', '-o', options, f'UUID={self.uuid}', str(self.mount)],
                              capture_output=True, timeout=60).returncode != 0:
                return None
            self.mounted_here = True
        found = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(self.mount)],
                               capture_output=True, text=True).stdout.strip()
        return self.mount if found.lower() == self.uuid.lower() else None

    def __exit__(self, *exc):
        try:
            if self.mounted_here:
                subprocess.run(['umount', str(self.mount)], capture_output=True, timeout=60)
        finally:
            if self.lock is not None:
                os.close(self.lock)
                self.lock = None


def tool(name, *args):
    """Run one of NekoServer's restore tools; True on success."""
    return subprocess.run([sys.executable, str(ROOT / name), *args]).returncode == 0


def restore_photos(config, prompt, output):
    media, data = Path(config['media_mount']), Path(config['data_mount'])
    archive = media / config.get('photos', {}).get('archive_dir', 'photos/archive')
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
        target = new_folder(restored(config) / f'photos-{stamp()}', prompt, output)
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


def restore_cloud(config, prompt, output):
    with Usb(config) as usb:
        users = cloud_users(usb) if usb else []
        if not users:
            output('No Nextcloud copy found on the USB drive (is it plugged in?).')
            return
        user = choose('Restore whose Nextcloud files?', [(u, u) for u in users], prompt, output)
        if user is None:
            return
        target = new_folder(restored(config) / f'nextcloud-{user}-{stamp()}', prompt, output)
        if target is None:
            return
        output('Copying...')
        count, latest = copy_choice([('files', usb / 'nextcloud' / user / 'files')], target, prompt, output,
                                    ledger_root=usb / 'nextcloud')
    say(output, f'\nDone: {count} files copied to {target}',
        *(() if latest else (VERSIONS_EXPLAINED,)),
        'To put them back: open Nextcloud in a browser (or the desktop app) and upload the',
        'files you need. Your current Nextcloud files were not touched.')


def restore_passwords(config, prompt, output):
    runs = monthly_snapshots(Path(config['media_mount']) / MONTHLY_DIR, need='secrets.tar.cms')
    if not runs:
        output('No monthly snapshot with an encrypted password list on the HDD yet.')
        return
    run = choose('Restore the password list from which monthly snapshot?',
                 [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = new_folder(Path('/root') / f'nekoserver-passwords-{stamp()}', prompt, output)
    if target is None:
        return
    output('It will ask for the sudo password you had when you first installed NekoServer.')
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
    run = choose(f'Restore {label} from which night?', [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = new_folder(restored(config) / f'{kind}-{stamp()}', prompt, output)
    if target is None:
        return
    if tool(script, 'restore', '--snapshot', str(run), '--destination', str(target)):
        say(output, f'\nDone: restored into {target} (checksums verified).',
            'To use a file from it, stop the app first, copy the file back, then start the app.',
            'See APP-BACKUPS.md or HOST-BACKUPS.md for the exact steps.')


def restore_monthly(config, prompt, output):
    runs = monthly_snapshots(Path(config['media_mount']) / MONTHLY_DIR, need='restore-tests.json')
    if not runs:
        output('No complete monthly snapshot on the HDD yet.')
        return
    run = choose('Use which monthly snapshot?', [(p, d) for p, d in runs], prompt, output)
    if run is None:
        return
    target = new_folder(restored(config) / f'monthly-{stamp()}', prompt, output)
    if target is None:
        return
    images = ask('Also load the saved app versions into Docker (needed on a new server)?', prompt, output)
    args = ['--snapshot', str(run), '--stage', str(target)] + (['--load-images'] if images else [])
    if tool('monthly-recovery.py', *args):
        say(output, f'\nDone: checked and unpacked into {target}.',
            'Nothing live was replaced. The app password files (.env files and Nextcloud\'s',
            'config.php) are not in it: get them with "App password list" in this menu.',
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


PROBLEM_LISTS = ('damaged', 'both_damaged', 'disagree')


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


def repair_one(key, copies, ledgers, original, prompt, output, aside):
    """Repair one file of a tree. `copies`: [(label, root)] holding the file under `key`
    (HDD archive and USB copy, or just the USB copy); `ledgers`: [(root, its ledger)];
    `original`: a live source (the Immich library or Nextcloud) that may still hold it.
    Returns what happened."""
    expected = None
    for _, known in ledgers:
        expected = expected or known.get(key)
    present = [(label, root, Path(root) / key) for label, root in copies]
    sums = {label: sha256_file(path) if path.is_file() else None for label, _, path in present}
    live = sha256_file(original) if original and original.is_file() else None
    if expected is None:
        # No recorded checksum: a matching original decides, otherwise you do.
        candidates = [(label, path, sums[label]) for label, _, path in present if sums[label]]
        chosen = next((c for c in candidates if live and c[2] == live), None)
        if chosen is None:
            if len(candidates) < 2:
                return 'skipped: no second copy to compare with'
            output(f'\n{key} differs between the backups and has no recorded checksum:')
            options = [(c, describe(c[0], c[1])) for c in candidates]
            chosen = choose('Which copy is the right one?', options + [(None, 'Keep both as they are')],
                            prompt, output)
            if chosen is None:
                return 'kept both as they are'
        expected = chosen[2]
        good = chosen[1]
    else:
        good = next((path for label, _, path in present if sums[label] == expected), None)
        if good is None and live == expected:
            good = original
        if good is None:
            return 'no copy matches its recorded checksum any more; every copy was kept untouched'
    fixed = []
    for label, root, path in present:
        if sums[label] != expected:
            replace_copy(good, path, root, key, expected, aside)
            fixed.append(label)
    # The checksum is recorded after the files are in place (it is never replaced).
    for root, known in ledgers:
        if key not in known:
            record_ledger(root, {key: expected}, 'chosen')
            known[key] = expected
    return 'repaired the ' + ' and '.join(fixed) + ' copy' if fixed else 'already intact'


def repair_backups(config, prompt, output):
    """Fix the files the checksum checks reported, from a copy that still matches."""
    from recovery_tools import lock
    sections = integrity_sections(config)
    work = {name: listed(sections.get(name, {})) for name in ('archive', 'usb_photos', 'usb_nextcloud')}
    if not any(work.values()):
        output('The last checksum checks found nothing to repair.')
        return
    media, data = Path(config['media_mount']), Path(config['data_mount'])
    archive = media / config.get('photos', {}).get('archive_dir', 'photos/archive')
    library = data / 'photos/immich-library'
    aside = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    say(output, '\nRepair damaged backup copies',
        'Each file is compared with its recorded SHA-256 checksum and fixed from a copy that',
        'still matches it. Damaged copies are moved to a "neko-damaged" folder, never deleted.')
    resolved = {name: [] for name in work}
    with lock(config), Usb(config, writable=True) as usb:
        photos = sorted(set(work['archive']) | set(work['usb_photos']))
        copies = [('HDD', archive)] + ([('USB', usb)] if usb else [])
        ledgers = [(root, read_ledger(root)) for _, root in copies]
        for key in photos:
            result = repair_one(key, copies, ledgers, library / key, prompt, output, aside)
            output(f'  {key}: {result}')
            if result.startswith(('repaired', 'already')):
                resolved['archive'].append(key)
                resolved['usb_photos'].append(key)
        if work['usb_nextcloud'] and usb:
            root = usb / 'nextcloud'
            ledgers = [(root, read_ledger(root))]
            for key in work['usb_nextcloud']:
                result = repair_one(key, [('USB', root)], ledgers, media / 'cloud/files' / key, prompt, output, aside)
                output(f'  {key}: {result}')
                if result.startswith(('repaired', 'already')):
                    resolved['usb_nextcloud'].append(key)
        elif work['usb_nextcloud']:
            output('  The Nextcloud files need the USB drive; plug it in and choose this again.')
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
            target = new_folder(restored(config) / f'old-usb-snapshots-{stamp()}', prompt, output)
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


def settings(config_path=None):
    """The chosen settings file (--config), else my-config.json, else config.json here."""
    candidates = [settings_path(config_path)] if config_path else [ROOT / 'my-config.json', ROOT / 'config.json']
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text())
    raise RuntimeError('Settings file not found: ' + ', '.join(str(p) for p in candidates))


def run(prompt=input, output=print, config_path=None):
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh --recover')
        return 2
    config = settings(config_path)
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
