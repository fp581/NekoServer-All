"""Check drive space right after the drives are chosen, before anything is installed."""
import glob
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import guided_setup
from neko_common import regular_files

DEPENDENCIES = ['storage']
DESCRIPTION = 'Check there is enough space on the SSD, HDD and USB drive before installing'

GIB = 1024 ** 3
# (hard minimum, recommended) free space in GiB.
SSD = (20, 100)   # Docker images (~20 GB), app data, photos and music live here.
HDD = (50, 500)   # Movies, shows, Nextcloud files and every HDD backup.
USB_MIN = 16      # The USB copy must at least fit photos + Nextcloud files.


def free_bytes(path):
    return shutil.disk_usage(path).free


def used_bytes(path):
    """Apparent size of a folder in bytes; 0 if it does not exist."""
    path = Path(path)
    if not path.is_dir():
        return 0
    try:
        result = subprocess.run(['du', '-s', '-B1', '--apparent-size', '--', str(path)],
                                capture_output=True, text=True, timeout=900)
        return int(result.stdout.split()[0]) if result.returncode == 0 else 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


PHOTO_SECTIONS = ('upload', 'library')
FULL_CHECK_DAYS = 28  # the weekly USB job compares every copy by checksum this often


def archive_folder(config):
    """The permanent photo archive on the HDD (what the weekly USB job copies)."""
    return Path(config['media_mount']) / config.get('photos', {}).get('archive_dir', 'photos/archive')


def usb_sources(config):
    """(folder on this server, matching folder on the USB drive) for everything the weekly
    job copies there; mirrors the layout the server program writes. Photos come from the
    HDD archive, which keeps photos deleted from Immich and every saved version."""
    media = Path(config['media_mount'])
    pairs = [(archive_folder(config) / name, name) for name in PHOTO_SECTIONS]
    cloud = media / 'cloud/files'
    try:
        users = sorted(cloud.iterdir()) if cloud.is_dir() else []
    except OSError:
        users = []
    for user in users:
        if user.name.startswith(('appdata_', '.')) or user.is_symlink() or not (user / 'files').is_dir():
            continue
        pairs.append((user / 'files', f'nextcloud/{user.name}/files'))
    return pairs


def awaiting_archive(config):
    """Bytes of photos and videos on the SSD that the HDD archive does not hold yet; the
    nightly archive copy adds them there, and the weekly USB copy then adds them too."""
    library = Path(config['data_mount']) / 'photos/immich-library'
    return pending_bytes(archive_folder(config), [(library / name, name) for name in PHOTO_SECTIONS])


def newest_dump_bytes(config):
    """Size of the newest Immich database copy; the weekly job adds one to the USB drive."""
    folder = Path(config['media_mount']) / 'backups/immich-database'
    try:
        newest = max((f for f in folder.glob('immich-*.sql') if f.is_file()), default=None)
        return newest.stat().st_size if newest else 0
    except OSError:
        return 0


def pending_bytes(root, pairs, mirror=(), full=False):
    """Bytes the next weekly copy still has to write to the USB drive at root: every file
    it does not hold yet, in the current content. Earlier versions already on the drive
    are not counted as reusable room: the backup only ever adds. Folders named in
    `mirror` are copies of the HDD photo archive and follow the mirror's rules
    (`mirrored`); `full`: the 4-weekly checksum comparison of every copy is due."""
    total = 0
    for source, name in pairs:
        noted = current_files(source) if name in mirror else set()
        for path, size in regular_files(source):
            if path.name.endswith(('.part', '.neko-part')):
                continue
            target = root / name / path.relative_to(source)
            done = (mirrored(path, target, full or path in noted) if name in mirror
                    else copied(path, target))
            if not done:
                total += size
    return total


def current_files(folder):
    """Archive files named by a `.neko-latest` note: the weekly mirror checks these by
    checksum every week."""
    named = set()
    for note in Path(folder).rglob('.*.neko-latest'):
        try:
            name = note.read_text(encoding='utf-8').strip()
        except (OSError, UnicodeDecodeError):
            continue
        if name and '/' not in name:
            named.add(note.parent / name)
    return named


def mirrored(source, target, verify):
    """Does the USB drive hold this archive file intact, as the weekly mirror decides?
    The archive never changes a saved file, so a different copy is damaged: the mirror
    keeps it aside and writes the file again in full. A same-size copy written after the
    file last changed is trusted without reading it, unless `verify` (a full check, or a
    file a current-version note names), which compares checksums like the mirror does."""
    try:
        info, copy = source.lstat(), target.stat()
    except OSError:
        return False
    if copy.st_size != info.st_size:
        return False
    if not verify and copy.st_mtime_ns >= info.st_ctime_ns:
        return True
    try:
        return digest(source) == digest(target)
    except OSError:
        return False


def full_check_due(config):
    """Is the weekly USB job's 4-weekly checksum comparison of every copy due?"""
    report = Path(config['data_mount']) / 'reports/jobs/external-full-check.json'
    try:
        last = float(json.loads(report.read_text()).get('at', 0))
    except (OSError, ValueError, TypeError, AttributeError):
        last = 0.0
    return time.time() - last >= FULL_CHECK_DAYS * 86400


def copied(source, target):
    """Does the USB drive already hold this content? Decided like the backup itself: a
    copy of the same size written after the source last changed (its status-change time,
    which cannot be set back) counts without reading it; otherwise the copy and its saved
    versions are compared by checksum. A same-size edit therefore needs room again."""
    try:
        info, copy = source.lstat(), target.stat()
    except OSError:
        return False
    if copy.st_size == info.st_size and copy.st_mtime_ns >= info.st_ctime_ns:
        return True
    stem, dot, ext = target.name.rpartition('.')
    stem, ext = (stem, dot + ext) if stem else (target.name, '')
    versions = [v for v in target.parent.glob(glob.escape(stem) + '.neko-*' + glob.escape(ext))
                if not v.name.endswith('.neko-part')]
    wanted = None
    for saved in [target, *versions]:
        try:
            if saved.stat().st_size != info.st_size:
                continue
            wanted = wanted or digest(source)
            if digest(saved) == wanted:
                return True
        except OSError:
            continue
    return False


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def usb_room(uuid, pairs, full=False):
    """(free bytes, bytes still to copy) on the USB drive, or None if it is not attached.
    A drive that is not open anywhere is opened read-only, measured and closed again;
    nothing on it is changed."""
    device = Path('/dev/disk/by-uuid') / uuid
    if not uuid or not device.exists():
        return None
    try:
        found = subprocess.run(['findmnt', '-n', '-o', 'TARGET', '--source', 'UUID=' + uuid],
                               capture_output=True, text=True, timeout=20)
        targets = found.stdout.split('\n') if found.returncode == 0 else []
        if targets and targets[0].strip():
            return measure(Path(targets[0].strip()), pairs, full)
        with tempfile.TemporaryDirectory(prefix='neko-usb-') as folder:
            opened = subprocess.run(['mount', '-o', 'ro,nosuid,nodev,noexec', 'UUID=' + uuid, folder],
                                    capture_output=True, timeout=60)
            if opened.returncode:
                return None
            try:
                return measure(Path(folder), pairs, full)
            finally:
                subprocess.run(['umount', folder], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def measure(root, pairs, full=False):
    return free_bytes(root), pending_bytes(root, pairs, mirror=PHOTO_SECTIONS, full=full)


def gib(value):
    return f'{value / GIB:.0f} GB'


def assess(config, free=free_bytes, used=used_bytes, usb_space=usb_room, dump=newest_dump_bytes,
           unarchived=awaiting_archive):
    """Return (problems that stop the install, warnings worth confirming)."""
    data, media = Path(config['data_mount']), Path(config['media_mount'])
    stop, warn = [], []
    for name, path, (minimum, recommended), purpose in (
        ('SSD', data, SSD, 'apps, photos and music'),
        ('HDD', media, HDD, 'movies, shows, Nextcloud files and backups'),
    ):
        space = free(path)
        if space < minimum * GIB:
            stop.append(f'The {name} has only {gib(space)} free. NekoServer needs at least '
                        f'{minimum} GB there for {purpose}. Free up space or choose a bigger drive.')
        elif space < recommended * GIB:
            warn.append(f'The {name} has {gib(space)} free. {recommended} GB or more is recommended '
                        f'for {purpose}; it will fill up sooner.')
    # The archive also keeps photos deleted from Immich and earlier versions.
    photos = max(used(data / 'photos/immich-library'), used(archive_folder(config)))
    cloud = used(media / 'cloud/files')
    room = usb_space(config.get('photos', {}).get('external_backup_uuid', ''), usb_sources(config),
                     full=full_check_due(config))
    if room is not None:
        # What the next copy must write (new files, changed files, this week's database
        # copy) plus room to grow. Old versions already on the drive are never reused.
        free_now, pending = room
        pending += unarchived(config) + dump(config)
        needed = max(USB_MIN * GIB, pending + int((photos + cloud) * 0.2))
        if free_now < needed:
            warn.append(f'The USB drive has {gib(free_now)} free. The next weekly copy still has to add '
                        f'{gib(pending)} (photos and versions from the HDD archive, photos not archived yet, Nextcloud files and a database copy); '
                        f'{gib(needed)} or more free is recommended so it fits and has room to grow. '
                        'Free up space on it or use a bigger one.')
    return stop, warn


def ask(question, prompt=input, output=print):
    """yes -> True; no, Ctrl+D or Ctrl+C -> False."""
    return bool(guided_setup.yes_no(question, prompt, output))


def installed(config):
    """True once Docker and the apps already live on the data SSD (a re-run)."""
    return (Path(config.get('data_mount', '/srv/nekoserver')) / 'docker-engine').is_dir()


def run(ctx, prompt=input, output=print):
    if not ctx.apply:
        ctx.log('Capacity check runs after the drives are chosen')
        return
    stop, warn = assess(ctx.config)
    if installed(ctx.config):
        # The minimums are for a new install. Afterwards the drives fill with your own
        # photos and files; say so, but never block the rest of a re-run.
        for line in stop + warn:
            ctx.log('Drive space: ' + line)
        if not (stop or warn):
            ctx.log('Drive space: enough on every drive')
        return
    if stop:
        raise RuntimeError(' '.join(stop))
    if not warn:
        ctx.log('Drive space: enough on every drive')
        return
    for line in warn:
        ctx.log('Drive space: ' + line)
    if not guided_setup.interactive(ctx):
        return
    output('\n=== Drive space ===')
    for number, line in enumerate(warn, 1):
        output(f'{number}. {line}')
    if ask('Continue with these drives anyway?', prompt, output):
        return
    return {'status': 'needs_setup', 'reason': 'Stopped at the drive space check; free up space or '
            'choose bigger drives, then run the installer again'}

