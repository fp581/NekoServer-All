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
   questions, without a system update) so every service knows it. The apps start again,
   and the server is checked the way an upgrade checks it: every setup step that worked
   before works again, and every NekoServer service runs this release.
The old drive is left exactly as it was. A move holds the same lock as a NekoServer
upgrade from start to end, so the two never overlap."""

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
    VERSION,
    OPERATION_LOCK,
    atomic_json,
    atomic_write,
    checked_settings,
    default_settings,
    ensure_directory,
    hold_operation,
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


# What may be passed to a program: a plain word, path, option, UUID= name or a unit
# pattern (nekoserver-*: systemctl matches it itself), never a shell construct or a
# control character (the commands run without a shell anyway).
ARGUMENT = re.compile(r'[A-Za-z0-9_@%+=:,./*-]+')
UNIT = re.compile(r'[A-Za-z0-9_@.-]+\.(?:service|timer|socket)')


def run(argv, timeout=None, check=True, capture=True):
    checked = [a for a in argv if ARGUMENT.fullmatch(a)]
    if len(checked) != len(argv):
        raise RuntimeError('A command was refused: one of its arguments is not a plain word or path')
    return subprocess.run(checked, capture_output=capture, text=True, timeout=timeout, check=check)


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


def physical_disks(rows, name):
    """The physical disks (sda, nvme0n1) the device `name` is built on: through an
    encrypted (LUKS) layer, LVM or a RAID array, down to the disks themselves. A system
    on /dev/mapper/root is on the disk under its encrypted partition, not on that partition."""
    found, todo, seen = set(), [name], set()
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        same = [r for r in rows if r.get('name') == current]
        parents = [r['pkname'] for r in same if r.get('pkname')]
        if not parents or any(r.get('type') == 'disk' for r in same):
            found.add(current)
        else:
            todo.extend(parents)
    return found


def disk_of(rows, uuid):
    """The physical disks the filesystem `uuid` is on (none when it is not found)."""
    row = next((r for r in rows if r.get('uuid') == uuid), None)
    return physical_disks(rows, row['name']) if row else set()


def targets(config, drive, rows=None, used=None):
    """The filesystems `drive` can move to: a supported type, not mounted, on a disk of
    its own (not the old drive's, the other drive's or the system's), big enough. Each
    as (uuid, fstype, size, name); the reasons the others are left out, as words."""
    rows = rows if rows is not None else lsblk()
    mount_key = DRIVES[drive][0]
    need = int((used if used is not None else used_bytes(mount_path(config[mount_key]))) * MARGIN)
    taken = set().union(
        *(disk_of(rows, config['storage'][k]['uuid']) for k in DRIVES if config.get('storage', {}).get(k))
    )
    system = set().union(*(physical_disks(rows, r['name']) for r in rows if '/' in (r.get('mountpoints') or [])))
    found, left_out = [], []
    for row in rows:
        disks = physical_disks(rows, row.get('name'))
        if not row.get('uuid') or row.get('fstype') not in FILESYSTEMS or disks & (taken | system):
            continue
        why = why_not(row, need)
        if why:
            left_out.append(f'{row.get("name")}: {why}')
        else:
            found.append((row['uuid'], row['fstype'], int(row['size']), row.get('name')))
    return found, left_out


def why_not(row, need):
    """Why a filesystem on a disk of its own still can't take the drive, or None."""
    if any(row.get('mountpoints') or []):
        return f'mounted at {", ".join(m for m in row["mountpoints"] if m)} (unmount it first)'
    if int(row.get('size') or 0) < need:
        return f'{int(row.get("size") or 0) / 1e9:.0f} GB is less than the {need / 1e9:.0f} GB needed'
    return None


def nothing_at_work(lock=SETUP_LOCK, recovery=None):
    """Why a move (either way) cannot start now, or None: setup running, an unfinished app
    update, monthly check, upgrade or move, or a backup or app update at work (the
    backups' shared lock, `recovery`)."""
    for path, what in (
        (MAINTENANCE_JOURNAL, 'An app update or the monthly check is not finished yet'),
        (UPGRADE_JOURNAL, 'A NekoServer upgrade is at work'),
        (JOURNAL, 'A move to another drive is not finished yet (sudo python3 migrate.py resume)'),
    ):
        if Path(path).exists():
            return what
    for path, what in (
        (lock, 'Setup (sudo bash install.sh) is running'),
        (recovery, 'A backup or an app update is running'),
    ):
        if path is None:
            continue
        try:
            with open(path, 'a') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return what
        except OSError:
            continue
    return None


def recovery_lock(config):
    return mount_path(config['data_mount']) / 'installer/recovery.lock'


def on_the_moved_drive(path, mountpoint):
    """This copy of migrate.py, or the settings, is on the drive being moved, which is
    unmounted during the switch."""
    here = Path(__file__).resolve()
    return any(mountpoint == p or mountpoint in p.parents for p in (Path(path).resolve(), here))


FROM_THE_DOWNLOAD = (
    'Run this from the NekoServer folder you downloaded (on the system disk), with its my-config.json: '
    'this copy or its settings are on the {label} itself, which is unmounted during the switch'
)


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


def nekoserver_units(states='active'):
    """NekoServer's own services and timers in `states` (the dashboard included). A
    oneshot job (a backup) at work is "activating", not "active"."""
    listed = run(['systemctl', 'list-units', '--plain', '--no-legend', '--state=' + states, 'nekoserver-*'], 30).stdout
    return [line.split()[0] for line in listed.splitlines() if line.split()]


AT_WORK = 'activating,deactivating,reloading'
TIMER = '.timer'


def stop_writers(journal=JOURNAL):
    """Everything that writes to the drives stops; what ran is noted to start again. The
    timers go first, so no job starts meanwhile; a job already at work (a backup worker,
    a oneshot service: "activating") stops the move rather than being cut off half-way."""
    timers = [u for u in nekoserver_units() if u.endswith(TIMER)]
    record(journal, phase='stopped', stopped=timers)
    for unit in timers:
        run(['systemctl', 'stop', unit], 600, check=False)
    busy = [u for u in nekoserver_units(AT_WORK) if not u.endswith(TIMER)]
    if busy:
        raise RuntimeError(', '.join(busy) + ' is at work (a backup or another job); try again once it has finished')
    rest = active(WRITERS) + [u for u in nekoserver_units() if not u.endswith(TIMER)]
    stopped = timers + rest
    record(journal, phase='stopped', stopped=stopped)
    for unit in rest:
        run(['systemctl', 'stop', unit], 600, check=False)
    still = active(WRITERS) + [u for u in nekoserver_units('active,' + AT_WORK) if not u.endswith(TIMER)]
    if still:
        raise RuntimeError('These did not stop: ' + ', '.join(still))
    return stopped


def start_again(stopped, out=print):
    """Start again what the move stopped; returns those that did not start."""
    failed = []
    for unit in reversed([u for u in stopped or [] if isinstance(u, str) and UNIT.fullmatch(u)]):
        if run(['systemctl', 'start', unit], 600, check=False).returncode != 0:
            out(f'{unit} did not start again: sudo systemctl start {unit}')
            failed.append(unit)
    return failed


def mounted_identity(folder):
    """(UUID, filesystem, Btrfs subvolume or "") of what is mounted at `folder` now (the
    last of mounts stacked there), or None when nothing is."""
    shown = run(['findmnt', '-n', '-o', 'UUID,FSTYPE,SOURCE', '--mountpoint', str(folder)], 30, check=False)
    lines = [line.split() for line in shown.stdout.splitlines() if line.split()] if shown.returncode == 0 else []
    if not lines or len(lines[-1]) < 3:
        return None
    uuid, fstype, source = lines[-1][:3]
    subvolume = source.split('[', 1)[1].rstrip(']') if '[' in source else ''
    return uuid, fstype, '' if subvolume == '/' else subvolume


def same_drives(mountpoint, old, staging, new):
    """Both drives are still the ones the move began with, before each step: a drive that
    dropped out would leave its folder an empty one on the system disk, and the last copy
    (which deletes what is not on the old drive) and an empty-to-empty comparison would
    then pass over nothing."""
    if mounted_identity(mountpoint) != (old['uuid'], old['fstype'], old.get('subvolume') or ''):
        raise RuntimeError(f'the old drive is no longer the one mounted at {mountpoint} (unplugged, or it failed)')
    if (mounted_identity(staging) or ('',))[0] != new['uuid']:
        raise RuntimeError(f'the new drive is no longer the one mounted at {staging}')


def mount_lines(lines, mountpoint):
    """The lines of /etc/fstab that mount `mountpoint` (a comment is not one)."""
    from modules.storage import fstab_folder

    wanted = os.path.normpath(mountpoint)
    return [
        i
        for i, line in enumerate(lines)
        if not line.lstrip().startswith('#') and line.split()[1:2] and fstab_folder(line.split()[1]) == wanted
    ]


def names_drive(line, uuid):
    """The fstab line's source is the drive `uuid` (UUID=..., quoted or not)."""
    return line.split()[0].replace('"', '') == f'UUID={uuid}'


def switch_fstab(mountpoint, old_uuid, new_uuid, fstype, fstab=FSTAB, now=None, subvolume=None):
    """The line that mounts `mountpoint` names the new drive; the fstab as it was is
    kept beside it (fstab.neko-before-move-<time>)."""
    from modules.storage import fstab_line

    lines = fstab.read_text().splitlines()
    found = mount_lines(lines, mountpoint)
    if len(found) != 1 or not names_drive(lines[found[0]], old_uuid):
        raise RuntimeError(f'/etc/fstab does not have exactly one line mounting {mountpoint} from the old drive')
    kept = fstab.with_name(f'{fstab.name}.neko-before-move-{int(now or time.time())}')
    shutil.copy2(fstab, kept)
    lines[found[0]] = fstab_line(new_uuid, fstype, mountpoint, subvolume or None)
    atomic_write(fstab, '\n'.join(lines) + '\n', 0o644)
    return kept


def set_drive(settings, config, drive, record_, previous=None):
    """The settings name the drive `record_` (its UUID, filesystem and Btrfs subvolume,
    if any; nothing of the record it replaces), and keep the one it replaces, whole,
    under "previous_drive", to go back to."""
    record_ = {k: v for k, v in record_.items() if k != 'previous_drive'}
    if previous:
        record_['previous_drive'] = {k: v for k, v in previous.items() if k != 'previous_drive'}
    config['storage'][drive] = record_
    atomic_json(settings, config)


def setup_again(config, settings, log_file, out=print):
    """This release's setup, without questions and without a system update, so every
    service and backup learns the new drive (upgrade.py runs it the same way)."""
    import upgrade

    bundle = upgrade.kept_release(config, mount_path(config['data_mount']) / 'installer/releases' / ('v' + VERSION))
    out('Running setup again so every service knows the new drive (this takes a while)...')
    problem = upgrade.setup_problem(upgrade.run_setup, bundle, settings, upgrade.owner(config), log_file)
    if problem:
        raise RuntimeError(problem.replace('its setup', 'setup', 1))


def steps_working(config):
    """The setup steps that work now (recorded before the move, for the check after it)."""
    import upgrade

    return sorted(upgrade.steps_done(config))


def server_check(config, before):
    """Why the server does not pass the upgrade's check after the move, or None: a setup
    step that worked before does not now (setup's exit code 1 can mean one failed), the
    dashboard does not answer, or a NekoServer service does not run this release."""
    import upgrade

    return upgrade.verify(config, 'v' + VERSION, before)


BUSY = 'A NekoServer upgrade or another drive move is at work'
TRY_LATER = '; nothing was changed. Try again later.'


def with_operation(out, work, wait=False):
    """`work()` holding the operation lock (shared with upgrade.py) from start to end."""
    held = hold_operation(OPERATION_LOCK, wait)
    if held is None:
        out(BUSY + TRY_LATER)
        return 2
    with held:
        return work()


def move(settings, drive, new_uuid, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """The whole move (see the top of this file). 0: done; 1: stopped, nothing changed; 2: needs you."""
    return with_operation(out, lambda: move_held(settings, drive, new_uuid, out, journal, staging, fstab))


def move_held(settings, drive, new_uuid, out, journal, staging, fstab):
    path, config = load_settings(settings)
    if drive not in DRIVES:
        out('Use --drive data_ssd (the application SSD) or --drive media_hdd (the media HDD)')
        return 2
    busy = nothing_at_work(recovery=recovery_lock(config))
    if busy:
        out(busy + TRY_LATER)
        return 2
    mount_key, label = DRIVES[drive]
    mountpoint = mount_path(config[mount_key])
    if on_the_moved_drive(path, mountpoint):
        out(FROM_THE_DOWNLOAD.format(label=label))
        return 2
    old = {k: v for k, v in config['storage'][drive].items() if k != 'previous_drive'}
    # The drive as it is mounted now, its Btrfs subvolume included (the settings may not
    # name it): the move checks it is still that one before each step, and `back` needs it.
    mounted = mounted_identity(mountpoint)
    if not mounted or mounted[:2] != (old.get('uuid'), old.get('fstype')):
        out(f'The {label} in the settings is not the drive mounted at {mountpoint}; nothing was changed')
        return 2
    if mounted[2]:
        old['subvolume'] = mounted[2]
    found, left_out = targets(config, drive)
    target = next((t for t in found if t[0] == new_uuid), None)
    if target is None:
        out(f'The drive {new_uuid} cannot take the {label}. ' + '; '.join(left_out or ['it was not found']))
        return 2
    _, fstype, _, name = target
    record(
        journal,
        phase='checking',
        drive=drive,
        mount=str(mountpoint),
        old=old,
        new={'uuid': new_uuid, 'fstype': fstype},
        before=steps_working(config),
    )
    new = {'uuid': new_uuid, 'fstype': fstype}
    try:
        mount_staging(new_uuid, fstype, staging)
        if not empty(staging):
            raise RuntimeError(f'{name} is not empty; NekoServer only moves to an empty filesystem')
        record(journal, phase='copying')
        same_drives(mountpoint, old, staging, new)
        copy(mountpoint, staging, out=out)
        stop_writers(journal)
        same_drives(mountpoint, old, staging, new)
        copy(mountpoint, staging, final=True, out=out)
        record(journal, phase='verifying')
        out('Comparing every file on both drives by checksum (this reads both drives once more)...')
        same_drives(mountpoint, old, staging, new)
        differ = differences(mountpoint, staging)
        if differ:
            raise RuntimeError(f'{len(differ)} files differ after the copy (the first: {differ[0]})')
        same_drives(mountpoint, old, staging, new)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return stopped_safely(exc, label, journal, staging, out)
    record(journal, phase='switching')
    return switch(path, config, drive, journal, staging, fstab, out)


UUID = re.compile(r'[A-Za-z0-9-]{8,80}')
SUBVOLUME = re.compile(r'(/[A-Za-z0-9@._-]+)*')


def stopped_safely(exc, label, journal, staging, out=print):
    """The move stopped before its switch: the new drive is unmounted and what the move
    stopped (as its record says: also when stopping failed part-way) is started again.
    The record goes only once all of it runs again; otherwise it stays, with what is left."""
    run(['umount', str(staging)], 120, check=False)
    failed = start_again((read(journal) or {}).get('stopped'), out)
    if failed:
        record(journal, phase='restart_failed', stopped=failed)
        out(
            f'The move stopped: {exc}. The {label} is the old drive, unchanged, but these did not start again: '
            + ', '.join(failed)
            + '. Start them (sudo systemctl start <name>), then  sudo python3 migrate.py resume'
        )
        return 2
    journal.unlink(missing_ok=True)
    out(f'The move stopped: {exc}. The {label} is the old drive, unchanged; the apps run as before.')
    return 1


def checked_move(saved, config, drive):
    """The move's record, checked against the settings before anything is done with it:
    the folder is the one the settings mount the drive at, both drives' UUIDs are UUIDs
    and the filesystems ones NekoServer moves to."""
    mountpoint = mount_path(config[DRIVES[drive][0]])
    old, new = saved.get('old') or {}, saved.get('new') or {}
    if (
        str(mountpoint) != saved.get('mount')
        or not all(UUID.fullmatch(str(d.get('uuid', ''))) for d in (old, new))
        or new.get('fstype') not in FILESYSTEMS
        or not all(SUBVOLUME.fullmatch(str(d.get('subvolume') or '')) for d in (old, new))
    ):
        raise RuntimeError(f'The move\'s record ({JOURNAL}) does not match the settings; look at it, then remove it')
    return str(mountpoint), old, new


def switch(settings, config, drive, journal=JOURNAL, staging=STAGING, fstab=FSTAB, out=print):
    """The new drive takes the old one's place; idempotent, so a restart in the middle
    is finished by `resume`."""
    mountpoint, old, new = checked_move(read(journal), config, drive)
    # Whatever is mounted at the folder must be one of the two drives (after a restart
    # in the middle, fstab may already name the new one).
    there = mounted_identity(mountpoint)
    if (
        there is not None
        and there != (old['uuid'], old['fstype'], old.get('subvolume') or '')
        and there[0] != new['uuid']
    ):
        out(
            f'{mountpoint} has another drive mounted ({there[0]}), neither the old nor the new one; nothing was switched'
        )
        return 2
    for folder in (staging, mountpoint):
        run(['umount', str(folder)], 600, check=False)
    if os.path.ismount(mountpoint):
        out(f'{mountpoint} could not be unmounted (something still uses it); nothing was switched')
        return 2
    # The one active line that mounts the folder (a comment or another line naming the
    # new drive does not count): already the new drive after a restart, else the old one.
    lines = fstab.read_text().splitlines()
    found = mount_lines(lines, mountpoint)
    if len(found) != 1 or not names_drive(lines[found[0]], new['uuid']):
        switch_fstab(mountpoint, old['uuid'], new['uuid'], new['fstype'], fstab, subvolume=new.get('subvolume'))
    set_drive(settings, config, drive, new, old)
    run(['mount', mountpoint], 120, check=False)
    there = mounted_identity(mountpoint)
    if there is None or there[0] != new['uuid']:
        out(
            f'{mountpoint} did not mount the new drive {new["uuid"]} (it shows {there[0] if there else "nothing"}). '
            'Look at /etc/fstab (the line before the move is kept beside it), then  sudo python3 migrate.py resume'
        )
        return 2
    record(journal, phase='setup')
    log_file = Path(mountpoint if drive == 'data_ssd' else config['data_mount']) / 'installer/migrate.log'
    try:
        setup_again(config, settings, log_file, out)
    except RuntimeError as exc:
        out(f'The new drive is in place, but {exc}. Put that right, then finish with  sudo python3 migrate.py resume')
        return 2
    return started_and_checked(config, drive, journal, out)


def started_and_checked(config, drive, journal, out=print):
    """After the switch: what the move stopped starts again on the drive now in place,
    and the server passes the upgrade's check. Until both have, the record stays (phase
    "started_partly" or "setup"), and `resume` carries on from there, never back."""
    saved = read(journal) or {}
    failed = start_again(saved.get('stopped'), out)
    if failed:
        record(journal, phase='started_partly', stopped=failed)
        out(
            'The drives are switched, but these did not start again: '
            + ', '.join(failed)
            + '. Start them (sudo systemctl start <name>), then  sudo python3 migrate.py resume'
        )
        return 2
    record(journal, phase='checking_server', stopped=[])
    before = [step for step in saved.get('before') or [] if isinstance(step, str)]
    problem = server_check(config, before)
    if problem:
        record(journal, phase='setup')
        out(
            f'The drive is in place, but the server does not pass its check: {problem}. '
            'Put that right, then  sudo python3 migrate.py resume  (it runs setup and the check again)'
        )
        return 2
    going_back = saved.get('direction') == 'back'
    journal.unlink(missing_ok=True)
    label = DRIVES[drive][1]
    if going_back:
        out(f'The {label} is the old drive again. Anything saved to the other one since the move is still on it.')
    else:
        out(
            f'Done: the {label} is the new drive, mounted at {saved.get("mount")}. The old drive was not changed: '
            'keep it until you are sure, then disconnect it. To go back to it: sudo python3 migrate.py back'
        )
    return 0


def back(settings, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """Go back to the old drive: the settings and /etc/fstab name it again (its Btrfs
    subvolume too), and setup runs. The same checks, lock and durable record as a move,
    so it never overlaps another job, and `resume` finishes it after a restart. What was
    written to the newer drive since the move stays on it."""
    return with_operation(out, lambda: back_held(settings, out, journal, staging, fstab))


def back_held(settings, out, journal, staging, fstab):
    path, config = load_settings(settings)
    for drive, (mount_key, label) in DRIVES.items():
        previous = config['storage'].get(drive, {}).get('previous_drive')
        if not previous:
            continue
        busy = nothing_at_work(recovery=recovery_lock(config))
        if busy:
            out(busy + TRY_LATER)
            return 2
        mountpoint = mount_path(config[mount_key])
        current = {k: v for k, v in config['storage'][drive].items() if k != 'previous_drive'}
        if (
            not all(UUID.fullmatch(str(d.get('uuid', ''))) for d in (previous, current))
            or previous.get('fstype') not in FILESYSTEMS
            or not SUBVOLUME.fullmatch(str(previous.get('subvolume') or ''))
        ):
            out(f'The settings\' record of the {label} is not valid; nothing was changed')
            return 2
        if on_the_moved_drive(path, mountpoint):
            out(FROM_THE_DOWNLOAD.format(label=label))
            return 2
        if not os.path.exists('/dev/disk/by-uuid/' + previous['uuid']):
            out(f'The old {label} ({previous["uuid"]}) is not connected')
            return 2
        record(
            journal,
            phase='stopping',
            direction='back',
            drive=drive,
            mount=str(mountpoint),
            old=current,
            new=previous,
            before=steps_working(config),
        )
        try:
            stop_writers(journal)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return stopped_safely(exc, label, journal, staging, out)
        record(journal, phase='switching')
        return switch(path, config, drive, journal, staging, fstab, out)
    out('No drive was moved (the settings name no earlier drive)')
    return 1


def resume(settings, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """After a restart (or once you put right what it said): a move cut off before its
    switch changed nothing (the apps are started again on the old drive); one cut off
    while it switched, or after it, is finished on the new drive. It waits for the lock."""
    return with_operation(out, lambda: resume_held(settings, out, journal, staging, fstab), wait=True)


def resume_held(settings, out, journal, staging, fstab):
    saved = read(journal)
    if saved is None:
        return 0
    phase = saved.get('phase')
    if phase in ('switching', 'setup'):
        path, config = load_settings(settings)
        return switch(path, config, saved['drive'], journal, staging, fstab, out)
    # "restart_failed": a move stopped before its switch, whose apps did not all start
    # again; 0.45.4 also wrote it after a switch, so the drive mounted tells them apart.
    switched = phase in ('started_partly', 'checking_server') or (
        phase == 'restart_failed'
        and (mounted_identity(saved.get('mount', '')) or ('',))[0] == (saved.get('new') or {}).get('uuid')
    )
    if switched:
        path, config = load_settings(settings)
        checked_move(saved, config, saved['drive'])
        return started_and_checked(config, saved['drive'], journal, out)
    return stopped_safely('it was cut off before it switched (a restart, say)', 'drive', journal, staging, out)


def list_targets(settings, out=print):
    _, config = load_settings(settings)
    for drive, (_, label) in DRIVES.items():
        if not config.get('storage', {}).get(drive):
            continue
        try:
            found, left_out = targets(config, drive)
        except OSError as exc:
            out(f'The {label} ({drive}): its folder can\'t be read ({exc.strerror}); is it mounted?')
            continue
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
        if not (args.drive and args.to and UUID.fullmatch(args.to)):
            parser.error('move needs --drive and --to (the UUID list shows)')
        return move(args.config, args.drive, args.to)
    return {'list': list_targets, 'back': back, 'resume': resume}[args.action](args.config)


if __name__ == '__main__':
    sys.exit(main())
