"""Guided recovery: `sudo bash install.sh --recover`.

A menu over the existing restore tools. Everything is restored into a NEW folder;
live data is never overwritten. Each restore ends with what to do next.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SNAPSHOT = re.compile(r'^snapshot-([0-9]+)-[0-9]+$')
MONTHLY = re.compile(r'^([0-9]+)-[0-9]+$')

MENU = [
    ('photos', 'Photos and videos (Immich)'),
    ('cloud', 'Nextcloud files'),
    ('passwords', 'App password list'),
    ('apps', 'App settings (nightly backup)'),
    ('host', 'Steam and Samba settings (nightly backup)'),
    ('monthly', 'Everything from a monthly snapshot (for rebuilding a server)'),
]


def say(output, *lines):
    for line in lines:
        output(line)


def ask(question, prompt=input, output=print):
    while True:
        answer = prompt(question + ' [yes/no]: ').strip().lower()
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
        output('Please type yes or no.')


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
        if match and path.is_dir() and not path.is_symlink() and (path / 'manifest.json').is_file():
            found.append((int(match[1]), path))
    return [(path, when(stamp)) for stamp, path in sorted(found, reverse=True)]


def monthly_snapshots(folder, need=None):
    """(path, label) for complete monthly snapshots, newest first; `need` = required file."""
    folder = Path(folder)
    found = []
    for path in folder.iterdir() if folder.is_dir() else ():
        match = MONTHLY.match(path.name)
        if (match and path.is_dir() and not path.is_symlink() and (path / 'manifest.json').is_file()
                and (need is None or (path / need).is_file())):
            found.append((int(match[1]), path))
    return [(path, when(stamp)) for stamp, path in sorted(found, reverse=True)]


def cloud_users(usb):
    folder = Path(usb) / 'nextcloud'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if (p / 'files').is_dir() and not p.is_symlink())


def new_folder(suggested, prompt=input, output=print):
    """Agree a destination that does not exist yet, inside an existing folder."""
    path = Path(suggested)
    while True:
        output(f'\nRestore into this new folder:\n  {path}')
        if ask('Use this folder?', prompt, output):
            if path.exists():
                output('That folder already exists; restoring never overwrites anything. Pick another.')
            elif not path.parent.is_dir():
                output('Its parent folder does not exist. Pick another.')
            else:
                return path
        while True:
            typed = prompt('Type a full path for a NEW folder (or press Enter to go back): ').strip()
            if not typed:
                return None
            candidate = Path(typed)
            if candidate.is_absolute() and '..' not in candidate.parts:
                path = candidate
                break
            output('Please type a full path starting with /, without "..".')


def copy_new(sources, destination):
    """Copy folders into a new destination, never following symlinks. Returns file count."""
    destination.mkdir(mode=0o700)
    count = 0
    for name, source in sources:
        if Path(source).is_dir():
            shutil.copytree(source, destination / name, symlinks=True)
            count += sum(1 for p in (destination / name).rglob('*') if p.is_file())
    return count


def restored(config):
    """The HDD folder that holds restores, created privately on first use."""
    folder = Path(config['media_mount']) / 'restored'
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def stamp():
    return dt.datetime.now().strftime('%Y%m%d-%H%M')


class Usb:
    """Mount the backup USB drive read-only for the restore, if it is not mounted."""

    def __init__(self, config):
        self.mount = Path(config.get('external_backup_mount', '/mnt/nekoserver-photos'))
        photos = config.get('photos', {})
        self.uuid = photos.get('external_backup_uuid', '')
        self.mounted_here = False

    def __enter__(self):
        if not self.uuid:
            return None
        if subprocess.run(['findmnt', '-n', '--mountpoint', str(self.mount)],
                          capture_output=True).returncode != 0:
            if not (Path('/dev/disk/by-uuid') / self.uuid).exists():
                return None
            self.mount.mkdir(parents=True, exist_ok=True)
            if subprocess.run(['mount', '-o', 'ro,nosuid,nodev,noexec', f'UUID={self.uuid}', str(self.mount)],
                              capture_output=True, timeout=60).returncode != 0:
                return None
            self.mounted_here = True
        found = subprocess.run(['findmnt', '-n', '-o', 'UUID', '--mountpoint', str(self.mount)],
                               capture_output=True, text=True).stdout.strip()
        return self.mount if found.lower() == self.uuid.lower() else None

    def __exit__(self, *exc):
        if self.mounted_here:
            subprocess.run(['umount', str(self.mount)], capture_output=True, timeout=60)


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
        count = copy_new([('upload', base / 'upload'), ('library', base / 'library')], target)
    say(output, f'\nDone: {count} files copied to {target}',
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
        count = copy_new([('files', usb / 'nextcloud' / user / 'files')], target)
    say(output, f'\nDone: {count} files copied to {target}',
        'Files with ".neko-<date>" in their name are older versions kept by the backup.',
        'To put them back: open Nextcloud in a browser (or the desktop app) and upload the',
        'files you need. Your current Nextcloud files were not touched.')


def restore_passwords(config, prompt, output):
    runs = monthly_snapshots(Path(config['media_mount']) / 'backups/monthly', need='secrets.tar.cms')
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
        say(output, f'\nDone. Open it with:  sudo cat {target}/secrets/accounts/APP-CREDENTIALS.txt')


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
    runs = monthly_snapshots(Path(config['media_mount']) / 'backups/monthly', need='restore-tests.json')
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
            'Nothing live was replaced. To rebuild a server from it, follow',
            '"disaster recovery" in MONTHLY-MAINTENANCE.md step by step.')


def settings():
    for name in ('my-config.json', 'config.json'):
        path = ROOT / name
        if path.is_file():
            return json.loads(path.read_text())
    raise RuntimeError('Settings file not found next to the installer')


def run(prompt=input, output=print):
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh --recover')
        return 2
    config = settings()
    say(output, '\nNekoServer recovery',
        'Everything is restored into a NEW folder. Nothing you have now is changed.')
    handlers = {
        'photos': restore_photos, 'cloud': restore_cloud, 'passwords': restore_passwords,
        'apps': lambda c, p, o: restore_nightly(c, 'apps', p, o),
        'host': lambda c, p, o: restore_nightly(c, 'host', p, o),
        'monthly': restore_monthly,
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
