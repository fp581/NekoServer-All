"""Check drive space right after the drives are chosen, before anything is installed."""
from pathlib import Path
import shutil
import subprocess
import tempfile

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


def usb_sources(config):
    """(folder on this server, matching folder on the USB drive) for everything the weekly
    job copies there; mirrors the layout the server program writes."""
    data, media = Path(config['data_mount']), Path(config['media_mount'])
    pairs = [(data / 'photos/immich-library' / name, name) for name in ('upload', 'library')]
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


def newest_dump_bytes(config):
    """Size of the newest Immich database copy; the weekly job adds one to the USB drive."""
    folder = Path(config['media_mount']) / 'backups/immich-database'
    try:
        newest = max((f for f in folder.glob('immich-*.sql') if f.is_file()), default=None)
        return newest.stat().st_size if newest else 0
    except OSError:
        return 0


def pending_bytes(root, pairs):
    """Bytes the next weekly copy still has to write to the USB drive at root: every file
    that is not there yet, or whose copy differs in size (a changed file is kept as an extra
    dated version, so it needs its full size again). Earlier versions already on the drive
    are not counted as reusable room: the backup only ever adds."""
    total = 0
    for source, name in pairs:
        for path, size in regular_files(source):
            if not path.name.endswith(('.part', '.neko-part')) and not copied(root / name / path.relative_to(source), size):
                total += size
    return total


def copied(target, size):
    """True if the USB drive already has this file at the same size."""
    try:
        return target.stat().st_size == size
    except OSError:
        return False


def usb_room(uuid, pairs):
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
            return measure(Path(targets[0].strip()), pairs)
        with tempfile.TemporaryDirectory(prefix='neko-usb-') as folder:
            opened = subprocess.run(['mount', '-o', 'ro,nosuid,nodev,noexec', 'UUID=' + uuid, folder],
                                    capture_output=True, timeout=60)
            if opened.returncode:
                return None
            try:
                return measure(Path(folder), pairs)
            finally:
                subprocess.run(['umount', folder], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def measure(root, pairs):
    return free_bytes(root), pending_bytes(root, pairs)


def gib(value):
    return f'{value / GIB:.0f} GB'


def assess(config, free=free_bytes, used=used_bytes, usb_space=usb_room, dump=newest_dump_bytes):
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
    photos = used(data / 'photos/immich-library')
    cloud = used(media / 'cloud/files')
    room = usb_space(config.get('photos', {}).get('external_backup_uuid', ''), usb_sources(config))
    if room is not None:
        # What the next copy must write (new files, changed files, this week's database
        # copy) plus room to grow. Old versions already on the drive are never reused.
        free_now, pending = room
        pending += dump(config)
        needed = max(USB_MIN * GIB, pending + int((photos + cloud) * 0.2))
        if free_now < needed:
            warn.append(f'The USB drive has {gib(free_now)} free. The next weekly copy still has to add '
                        f'{gib(pending)} (new and changed photos, Nextcloud files and a database copy); '
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

