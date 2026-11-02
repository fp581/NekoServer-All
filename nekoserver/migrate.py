#!/usr/bin/env python3
"""Move the application SSD or the media HDD to a new (bigger) drive, while both are
connected. Nothing is ever written to the old drive: until you remove it, you can go back.

  sudo python3 migrate.py list                      the drives you can move to
  sudo python3 migrate.py move --drive media_hdd --to <UUID>
  sudo python3 migrate.py back                      go back to the old drive
  sudo python3 migrate.py resume                    after a restart: put a cut-off move right

Before you start, make the new drive's filesystem yourself (NekoServer never formats a
drive), for example  sudo mkfs.ext4 -L neko-media /dev/sdX1 , and leave it empty. What
`move` does, in order (a record in /etc/nekoserver-backups/migrate.json says how far it
got, so a power cut at any point is put right):
1. Checks the new drive: a filesystem of its own on another disk than the old one and
   the system, empty, and big enough for what the old one holds.
2. Copies everything while the apps still run (the long part), keeping owners,
   permissions, hard links, ACLs and extended attributes.
3. Stops the apps, Docker, Samba and NekoServer's jobs, and copies what changed meanwhile.
4. Compares every file on both drives by checksum. Any difference stops the move: the
   apps are started again on the old drive, as before.
5. Switches: the new drive is mounted where the old one was (the old line in /etc/fstab
   is kept aside), the settings name the new drive, and setup runs again (without
   questions, without a system update) so every service knows it.
The old drive is left exactly as it was."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from neko_common import (
    MAINTENANCE_JOURNAL,
    atomic_json,
    atomic_write,
    checked_settings,
    default_settings,
    ensure_directory,
    mount_path,
    settings_path,
)

JOURNAL = Path('/etc/nekoserver-backups/migrate.json')
UPGRADE_JOURNAL = Path('/etc/nekoserver-backups/upgrade.json')
FORMAT = 'neko-migrate-v1'
DRIVES = {'data_ssd': ('data_mount', 'application SSD'), 'media_hdd': ('media_mount', 'media HDD')}
FILESYSTEMS = ('ext4', 'xfs', 'btrfs')
SETUP_LOCK = Path('/run/nekoserver-setup.lock')
STAGING = Path('/mnt/neko-migrate')
FSTAB = Path('/etc/fstab')
# Owners, permissions, hard links, ACLs, extended attributes and sparse files kept, by
# number (the accounts are the same on this PC); never into another filesystem mounted
# inside (-x).
RSYNC = ['rsync', '-aHAXS', '--numeric-ids', '-x']
# Room left free on the new drive beyond what the old one holds.
MARGIN = 1.05
# The services stopped for the last copy: everything that writes to the drives.
WRITERS = ('docker.socket', 'docker.service', 'smb.service', 'smbd.service', 'nmb.service', 'nmbd.service')


def run(argv, timeout=None, check=True, capture=True):
    return subprocess.run(argv, capture_output=capture, text=True, timeout=timeout, check=check)


def read(journal=JOURNAL):
    try:
        saved = json.loads(Path(journal).read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and saved.get('format') == FORMAT else None


def record(journal=JOURNAL, **fields):
    saved = read(journal) or {'format': FORMAT, 'started_at': time.time()}
    saved.update(at=time.time(), **fields)
    ensure_directory(journal.parent, 0o700)
    atomic_json(journal, saved)
    return saved


def load_settings(settings):
    path = settings_path(settings)
    return path, json.loads(path.read_text())


def lsblk():
    """Every filesystem lsblk knows: name, UUID, type, size, mounted where, its disk."""
    shown = json.loads(
        run(['lsblk', '-J', '-b', '-p', '-o', 'NAME,UUID,FSTYPE,SIZE,MOUNTPOINTS,PKNAME,TYPE'], 60).stdout
    )

    def walk(rows):
        for row in rows:
            yield row
            yield from walk(row.get('children') or [])

    return list(walk(shown.get('blockdevices', [])))


def used_bytes(folder):
    stats = os.statvfs(folder)
    return (stats.f_blocks - stats.f_bfree) * stats.f_frsize


def disk_of(rows, uuid):
    """The disk (sda, nvme0n1) the filesystem `uuid` is on, or None."""
    row = next((r for r in rows if r.get('uuid') == uuid), None)
    return row and (row.get('pkname') or row.get('name'))


def targets(config, drive, rows=None, used=None):
    """The filesystems `drive` can move to: a supported type, not mounted, on a disk of
    its own (not the old drive's, the other drive's or the system's), big enough. Each
    as (uuid, fstype, size, name); the reasons the others are left out, as words."""
    rows = rows if rows is not None else lsblk()
    mount_key = DRIVES[drive][0]
    need = int((used if used is not None else used_bytes(mount_path(config[mount_key]))) * MARGIN)
    taken = {disk_of(rows, config['storage'][k]['uuid']) for k in DRIVES if config.get('storage', {}).get(k)}
    system = {r.get('pkname') or r.get('name') for r in rows if '/' in (r.get('mountpoints') or [])}
    found, left_out = [], []
    for row in rows:
        uuid, fstype = row.get('uuid'), row.get('fstype')
        if not uuid or fstype not in FILESYSTEMS:
            continue
        disk = row.get('pkname') or row.get('name')
        name = row.get('name')
        if disk in taken or disk in system:
            continue
        if any(row.get('mountpoints') or []):
            left_out.append(f'{name}: mounted at {", ".join(m for m in row["mountpoints"] if m)} (unmount it first)')
        elif int(row.get('size') or 0) < need:
            left_out.append(f'{name}: {int(row["size"]) / 1e9:.0f} GB is less than the {need / 1e9:.0f} GB needed')
        else:
            found.append((uuid, fstype, int(row['size']), name))
    return found, left_out


def nothing_at_work(lock=SETUP_LOCK):
    """Why the move cannot start now, or None."""
    for path, what in (
        (MAINTENANCE_JOURNAL, 'An app update or the monthly check is not finished yet'),
        (UPGRADE_JOURNAL, 'A NekoServer upgrade is at work'),
        (JOURNAL, 'A move to another drive is not finished yet (sudo python3 migrate.py resume)'),
    ):
        if Path(path).exists():
            return what
    try:
        with open(lock, 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 'Setup (sudo bash install.sh) is running'
    except OSError:
        pass
    return None


def empty(folder):
    """A new filesystem holds nothing but what mkfs makes (lost+found)."""
    return all(p.name == 'lost+found' for p in Path(folder).iterdir())


def mount_staging(uuid, fstype, staging=STAGING):
    ensure_directory(staging, 0o700)
    if os.path.ismount(staging):
        raise RuntimeError(f'{staging} is in use already; unmount it first')
    run(['mount', '-t', fstype, f'UUID={uuid}', str(staging)], 120)
    return staging


def copy(source, target, final=False, out=print):
    """One pass of the copy; the last one (`final`, with the apps stopped) also removes
    from the new drive what was deleted on the old one meanwhile."""
    argv = [*RSYNC, *(['--delete'] if final else []), f'{source}/', f'{target}/']
    out('Copying ' + ('what changed (the apps are stopped)' if final else 'everything (the apps keep running)') + '...')
    done = run(argv, check=False, capture=False)
    # 24: files vanished while copying (the apps were running): the last pass gets them.
    if done.returncode not in ((0,) if final else (0, 24)):
        raise RuntimeError(f'The copy stopped (rsync exit code {done.returncode}); the old drive is unchanged')


def differences(source, target):
    """Every file compared by checksum: what differs (empty when the copy is exact)."""
    shown = run(
        [*RSYNC, '--delete', '--checksum', '--dry-run', '--itemize-changes', f'{source}/', f'{target}/'], check=False
    )
    if shown.returncode != 0:
        return [f'the comparison stopped (rsync exit code {shown.returncode})']
    return [line for line in shown.stdout.splitlines() if line.strip()]


def active(units):
    return [u for u in units if run(['systemctl', 'is-active', '--quiet', u], 30, check=False).returncode == 0]


def nekoserver_units():
    """NekoServer's own services and timers that run now (the dashboard included)."""
    listed = run(['systemctl', 'list-units', '--plain', '--no-legend', '--state=active', 'nekoserver-*'], 30).stdout
    return [line.split()[0] for line in listed.splitlines() if line.split()]


def stop_writers(journal=JOURNAL):
    """Everything that writes to the drives stops; what ran is noted to start again."""
    stopped = [u for u in nekoserver_units() if u.endswith('.timer')] + active(WRITERS)
    stopped += [u for u in nekoserver_units() if not u.endswith('.timer')]
    record(journal, phase='stopped', stopped=stopped)
    for unit in stopped:
        run(['systemctl', 'stop', unit], 600, check=False)
    still = active(WRITERS)
    if still:
        raise RuntimeError('These did not stop: ' + ', '.join(still))
    return stopped


def start_again(stopped, out=print):
    for unit in reversed(stopped or []):
        if run(['systemctl', 'start', unit], 600, check=False).returncode != 0:
            out(f'{unit} did not start again: sudo systemctl start {unit}')


def switch_fstab(mountpoint, old_uuid, new_uuid, fstype, fstab=FSTAB, now=None):
    """The line that mounts `mountpoint` names the new drive; the fstab as it was is
    kept beside it (fstab.neko-before-move-<time>)."""
    from modules.storage import fstab_folder, fstab_line

    lines = fstab.read_text().splitlines()
    wanted = os.path.normpath(mountpoint)
    found = [i for i, line in enumerate(lines) if line.split()[1:2] and fstab_folder(line.split()[1]) == wanted]
    if len(found) != 1 or f'UUID={old_uuid}' not in lines[found[0]].split()[0]:
        raise RuntimeError(f'/etc/fstab does not have exactly one line mounting {mountpoint} from the old drive')
    kept = fstab.with_name(f'{fstab.name}.neko-before-move-{int(now or time.time())}')
    shutil.copy2(fstab, kept)
    lines[found[0]] = fstab_line(new_uuid, fstype, mountpoint)
    atomic_write(fstab, '\n'.join(lines) + '\n', 0o644)
    return kept


def set_drive(settings, config, drive, uuid, fstype, previous=None):
    """The settings name the new drive (and keep the old one under "previous_drive", to
    go back): nothing of the old drive's record, its Btrfs subvolume say, is kept."""
    record_ = {'uuid': uuid, 'fstype': fstype}
    if previous:
        record_['previous_drive'] = previous
    config['storage'][drive] = record_
    atomic_json(settings, config)


def setup_again(config, settings, log_file, out=print):
    """This release's setup, without questions and without a system update, so every
    service and backup learns the new drive (upgrade.py runs it the same way)."""
    import upgrade

    from neko_common import VERSION

    bundle = upgrade.kept_release(config, mount_path(config['data_mount']) / 'installer/releases' / ('v' + VERSION))
    out('Running setup again so every service knows the new drive (this takes a while)...')
    code = upgrade.run_setup(bundle, settings, upgrade.owner(config), log_file)
    if code not in (0, 1):
        raise RuntimeError(f'setup stopped (exit code {code}; the log says why: {log_file})')


def move(settings, drive, new_uuid, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """The whole move (see the top of this file). 0: done; 1: stopped, nothing changed; 2: needs you."""
    path, config = load_settings(settings)
    if drive not in DRIVES:
        out('Use --drive data_ssd (the application SSD) or --drive media_hdd (the media HDD)')
        return 2
    busy = nothing_at_work()
    if busy:
        out(busy + '; nothing was changed. Try again later.')
        return 2
    mount_key, label = DRIVES[drive]
    mountpoint = mount_path(config[mount_key])
    here = Path(__file__).resolve()
    if any(mountpoint == p or mountpoint in p.parents for p in (path.resolve(), here)):
        out(
            f'Run this from the NekoServer folder you downloaded (on the system disk), with its my-config.json: '
            f'this copy or its settings are on the {label} itself, which is unmounted during the move'
        )
        return 2
    old = dict(config['storage'][drive])
    found, left_out = targets(config, drive)
    target = next((t for t in found if t[0] == new_uuid), None)
    if target is None:
        out(f'The drive {new_uuid} cannot take the {label}. ' + '; '.join(left_out or ['it was not found']))
        return 2
    _, fstype, size, name = target
    record(
        journal, phase='checking', drive=drive, mount=str(mountpoint), old=old, new={'uuid': new_uuid, 'fstype': fstype}
    )
    stopped = []
    try:
        mount_staging(new_uuid, fstype, staging)
        if not empty(staging):
            raise RuntimeError(f'{name} is not empty; NekoServer only moves to an empty filesystem')
        record(journal, phase='copying')
        copy(mountpoint, staging, out=out)
        stopped = stop_writers(journal)
        copy(mountpoint, staging, final=True, out=out)
        record(journal, phase='verifying')
        out('Comparing every file on both drives by checksum (this reads both drives once more)...')
        differ = differences(mountpoint, staging)
        if differ:
            raise RuntimeError(f'{len(differ)} files differ after the copy (the first: {differ[0]})')
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        run(['umount', str(staging)], 120, check=False)
        start_again(stopped, out)
        journal.unlink(missing_ok=True)
        out(f'The move stopped: {exc}. The {label} is the old drive, unchanged; the apps run as before.')
        return 1
    record(journal, phase='switching')
    return switch(path, config, drive, journal, staging, fstab, out)


def switch(settings, config, drive, journal=JOURNAL, staging=STAGING, fstab=FSTAB, out=print):
    """The new drive takes the old one's place; idempotent, so a restart in the middle
    is finished by `resume`."""
    saved = read(journal)
    mountpoint, old, new = saved['mount'], saved['old'], saved['new']
    for folder in (staging, mountpoint):
        run(['umount', str(folder)], 600, check=False)
    if os.path.ismount(mountpoint):
        out(f'{mountpoint} could not be unmounted (something still uses it); nothing was switched')
        return 2
    lines = fstab.read_text()
    if f'UUID={new["uuid"]} ' not in lines:
        switch_fstab(mountpoint, old['uuid'], new['uuid'], new['fstype'], fstab)
    set_drive(settings, config, drive, new['uuid'], new['fstype'], {'uuid': old['uuid'], 'fstype': old['fstype']})
    run(['mount', mountpoint], 120)
    record(journal, phase='setup')
    log_file = Path(mountpoint if drive == 'data_ssd' else config['data_mount']) / 'installer/migrate.log'
    try:
        setup_again(config, settings, log_file, out)
    except RuntimeError as exc:
        out(
            f'The new drive is in place, but {exc}. Run  sudo bash install.sh  to finish; to go back: sudo python3 migrate.py back'
        )
        return 2
    start_again(saved.get('stopped'), out)
    journal.unlink(missing_ok=True)
    out(
        f'Done: the {DRIVES[drive][1]} is the new drive, mounted at {mountpoint}. The old drive was not changed: '
        'keep it until you are sure, then disconnect it. To go back to it: sudo python3 migrate.py back'
    )
    return 0


def back(settings, out=print, fstab=FSTAB):
    """Go back to the old drive: the settings and /etc/fstab name it again, and setup runs.
    What was written to the new drive since the move is not on the old one."""
    path, config = load_settings(settings)
    for drive, (mount_key, label) in DRIVES.items():
        previous = config['storage'].get(drive, {}).get('previous_drive')
        if not previous:
            continue
        mountpoint = config[mount_key]
        current = config['storage'][drive]
        if not os.path.exists('/dev/disk/by-uuid/' + previous['uuid']):
            out(f'The old {label} ({previous["uuid"]}) is not connected')
            return 2
        stopped = stop_writers(Path('/run/neko-migrate-back.json'))
        run(['umount', mountpoint], 600, check=False)
        switch_fstab(mountpoint, current['uuid'], previous['uuid'], previous['fstype'], fstab)
        set_drive(path, config, drive, previous['uuid'], previous['fstype'])
        run(['mount', mountpoint], 120)
        setup_again(config, path, Path(config['data_mount']) / 'installer/migrate.log', out)
        start_again(stopped, out)
        out(f'The {label} is the old drive again. Anything saved to the new one since the move is still on it.')
        return 0
    out('No drive was moved (the settings name no earlier drive)')
    return 1


def resume(settings, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """After a restart: a move cut off before its switch changed nothing (the apps are
    started again on the old drive); one cut off while it switched is finished."""
    saved = read(journal)
    if saved is None:
        return 0
    if saved.get('phase') in ('switching', 'setup'):
        path, config = load_settings(settings)
        return switch(path, config, saved['drive'], journal, staging, fstab, out)
    run(['umount', str(staging)], 120, check=False)
    start_again(saved.get('stopped'), out)
    journal.unlink(missing_ok=True)
    out(
        'A move to another drive was cut off before it switched: nothing changed, the old drive is in use. Run it again.'
    )
    return 1


def list_targets(settings, out=print):
    _, config = load_settings(settings)
    for drive, (_, label) in DRIVES.items():
        if not config.get('storage', {}).get(drive):
            continue
        found, left_out = targets(config, drive)
        out(f'The {label} ({drive}) can move to:')
        for uuid, fstype, size, name in found:
            out(f'  {name}  {fstype}  {size / 1e9:.0f} GB   sudo python3 migrate.py move --drive {drive} --to {uuid}')
        if not found:
            out(
                '  no empty filesystem big enough on another disk (make one first, for example  sudo mkfs.ext4 /dev/sdX1)'
            )
        for why in left_out:
            out('  not offered: ' + why)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('list', 'move', 'back', 'resume'))
    parser.add_argument('--drive', choices=tuple(DRIVES))
    parser.add_argument('--to', help='with move: the new filesystem\'s UUID (see list)')
    parser.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    if not shutil.which('rsync'):
        parser.error('rsync is needed: run  sudo bash install.sh  (it installs it), then try again')
    if args.action == 'move':
        if not (args.drive and args.to and re.fullmatch(r'[A-Za-z0-9-]{8,80}', args.to)):
            parser.error('move needs --drive and --to (the UUID list shows)')
        return move(args.config, args.drive, args.to)
    return {'list': list_targets, 'back': back, 'resume': resume}[args.action](args.config)


if __name__ == '__main__':
    sys.exit(main())
