"""Check drive space right after the drives are chosen, before anything is installed."""
from pathlib import Path
import shutil
import subprocess

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


def device_bytes(uuid):
    """Total size of the USB drive by UUID, or None if it is not attached."""
    device = Path('/dev/disk/by-uuid') / uuid
    if not uuid or not device.exists():
        return None
    try:
        result = subprocess.run(['lsblk', '-b', '-n', '-d', '-o', 'SIZE', str(device.resolve())],
                                capture_output=True, text=True, timeout=20)
        return int(result.stdout.split()[0]) if result.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def gib(value):
    return f'{value / GIB:.0f} GB'


def assess(config, free=free_bytes, used=used_bytes, device=device_bytes):
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
    usb = device(config.get('photos', {}).get('external_backup_uuid', ''))
    if usb is not None:
        needed = max(USB_MIN * GIB, int((photos + cloud) * 1.2))
        if usb < needed:
            warn.append(f'The USB drive is {gib(usb)}. Your photos and Nextcloud files take '
                        f'{gib(photos + cloud)} today; {gib(needed)} or more is recommended so the '
                        'weekly copy has room to grow.')
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


def run(ctx, prompt=input, output=print):
    if not ctx.apply:
        ctx.log('Capacity check runs after the drives are chosen')
        return
    stop, warn = assess(ctx.config)
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

