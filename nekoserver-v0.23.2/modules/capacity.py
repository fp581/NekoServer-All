"""Check drive space right after the drives are chosen, before anything is installed."""
from pathlib import Path
import shutil
import subprocess
import tempfile

import guided_setup

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


# What the weekly job keeps on the USB drive; already-copied files need no new room.
USB_COPIES = ('upload', 'library', 'nextcloud')


def usb_room(uuid):
    """(free bytes, bytes NekoServer already copied there) on the USB drive, or None if
    it is not attached. A drive that is not open anywhere is opened read-only, measured
    and closed again; nothing on it is changed."""
    device = Path('/dev/disk/by-uuid') / uuid
    if not uuid or not device.exists():
        return None
    try:
        found = subprocess.run(['findmnt', '-n', '-o', 'TARGET', '--source', 'UUID=' + uuid],
                               capture_output=True, text=True, timeout=20)
        targets = found.stdout.split('\n') if found.returncode == 0 else []
        if targets and targets[0].strip():
            return measure(Path(targets[0].strip()))
        with tempfile.TemporaryDirectory(prefix='neko-usb-') as folder:
            opened = subprocess.run(['mount', '-o', 'ro,nosuid,nodev,noexec', 'UUID=' + uuid, folder],
                                    capture_output=True, timeout=60)
            if opened.returncode:
                return None
            try:
                return measure(Path(folder))
            finally:
                subprocess.run(['umount', folder], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def measure(root):
    return free_bytes(root), sum(used_bytes(root / name) for name in USB_COPIES)


def gib(value):
    return f'{value / GIB:.0f} GB'


def assess(config, free=free_bytes, used=used_bytes, usb_space=usb_room):
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
    room = usb_space(config.get('photos', {}).get('external_backup_uuid', ''))
    if room is not None:
        # Room that counts: free space plus what an earlier weekly copy already holds.
        free_now, copied = room
        available = free_now + copied
        needed = max(USB_MIN * GIB, int((photos + cloud) * 1.2))
        if available < needed:
            warn.append(f'The USB drive has {gib(free_now)} free. Your photos and Nextcloud files take '
                        f'{gib(photos + cloud)} today; {gib(needed)} or more of room is recommended so '
                        'the weekly copy fits and has room to grow. Free up space on it or use a bigger one.')
    return stop, warn


def ask(question, prompt=input, output=print):
    while True:
        try:
            answer = prompt(question + ' [yes/no]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            output('')
            return False
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
        output('Please type yes or no.')


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

