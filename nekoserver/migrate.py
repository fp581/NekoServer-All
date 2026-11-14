#!/usr/bin/env python3
"""Move the application SSD or the media HDD to a new (bigger) drive, while both are
connected. Nothing is ever written to the old drive: until you remove it, you can go back.

  sudo python3 migrate.py list                      the drives you can move to
  sudo python3 migrate.py move --drive media_hdd --to <UUID>
  sudo python3 migrate.py back [--drive media_hdd]  go back to the old drive (--drive when both were moved)
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
    RECOVERY,
    SETUP_LOCK,
    SHARED,
    atomic_json,
    atomic_write,
    checked_settings,
    default_settings,
    ensure_directory,
    held_lock_reason,
    hold_operation,
    mount_path,
    read_journal,
    settings_path,
)

JOURNAL = Path('/etc/nekoserver-backups/migrate.json')
UPGRADE_JOURNAL = RECOVERY['upgrade'][0]
FORMAT = 'neko-migrate-v1'
DRIVES = {'data_ssd': ('data_mount', 'application SSD'), 'media_hdd': ('media_mount', 'media HDD')}
FILESYSTEMS = ('ext4', 'xfs', 'btrfs')
STAGING = Path('/mnt/neko-migrate')
FSTAB = Path('/etc/fstab')
# Owners, permissions, hard links, ACLs, extended attributes and sparse files kept, by
# number (the accounts are the same on this PC); never into another filesystem mounted
# inside (-x).
RSYNC = ['rsync', '-aHAXS', '--numeric-ids', '-x']
# Room left free on the new drive beyond what the old one holds.
MARGIN = 1.05
# The services stopped for the last copy: everything that writes to the drives.
WRITERS = (*SHARED['writers'], 'nmb.service', 'nmbd.service')


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
    return read_journal(journal, FORMAT)


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
    """Every filesystem lsblk knows: name, UUID, type, size, mounted where, its disk, and
    how a disk is connected (TRAN: iscsi for a disk on a NAS)."""
    shown = json.loads(
        run(['lsblk', '-J', '-b', '-p', '-o', 'NAME,UUID,FSTYPE,SIZE,MOUNTPOINTS,PKNAME,TYPE,TRAN'], 60).stdout
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


# What Linux itself runs from: the system, its boot and EFI partitions (often on a disk of
# their own), its separate /usr, /var or /home, and its swap.
SYSTEM_MOUNTS = {'/', '/boot', '/boot/efi', '/efi', '/usr', '/var', '/home', '[SWAP]'}


def system_disks(rows):
    """Every physical disk Linux runs from. A Btrfs system over several drives shows its
    folder on one of them only: each drive with that filesystem's UUID is a member too."""
    mounted = [r for r in rows if SYSTEM_MOUNTS & set(r.get('mountpoints') or [])]
    pools = {r['uuid'] for r in mounted if r.get('uuid')}
    members = mounted + [r for r in rows if r.get('uuid') in pools]
    return set().union(*(physical_disks(rows, r['name']) for r in members))


def disk_of(rows, uuid):
    """The physical disks the filesystem `uuid` is on (none when it is not found)."""
    row = next((r for r in rows if r.get('uuid') == uuid), None)
    return physical_disks(rows, row['name']) if row else set()


def on_nas(rows, disks):
    """True when one of `disks` is a disk on a NAS (iSCSI)."""
    return any(r.get('tran') == 'iscsi' for r in rows if r.get('name') in disks)


def setup_would_refuse(inventory, uuid):
    """Why setup's drive step would refuse the filesystem `uuid` after the move (a loop
    device, a filesystem it can't use, an encrypted drive Linux doesn't unlock at start),
    or None (the 0.46.1 audit: such a move switched, then setup refused, and it could go
    neither on nor back)."""
    from modules import storage

    offered = {device['uuid']: device for device in storage.discover_filesystems(inventory)}
    if uuid not in offered:
        return storage.not_offered(inventory, uuid) or 'not a drive setup can use'
    return storage.boot_unlock_problem(offered[uuid])


def targets(config, drive, rows=None, used=None, inventory=None):
    """The filesystems `drive` can move to: a supported type, not mounted, on a disk of
    its own (not the old drive's, the other drive's, the USB backup drive's or the
    system's), big enough, and for the SSD on this PC (setup refuses a NAS disk for it).
    Each as (uuid, fstype, size, name); the reasons the others are left out, as words."""
    if rows is None:
        from modules.storage import inventory_now

        rows, inventory = lsblk(), inventory_now()
    mount_key = DRIVES[drive][0]
    need = int((used if used is not None else used_bytes(mount_path(config[mount_key]))) * MARGIN)
    taken = set().union(
        *(disk_of(rows, config['storage'][k]['uuid']) for k in DRIVES if config.get('storage', {}).get(k))
    )
    backup = config.get('photos', {}).get('external_backup_uuid')
    backup_disks = disk_of(rows, backup) if backup else set()
    system = system_disks(rows)
    found, left_out = [], []
    for row in rows:
        disks = physical_disks(rows, row.get('name'))
        if not row.get('uuid') or row.get('fstype') not in FILESYSTEMS or disks & (taken | system):
            continue
        why = refused_target(row, need, inventory) or kept_for(drive, rows, disks, backup_disks)
        if why:
            left_out.append(f'{row.get("name")}: {why}')
        else:
            found.append((row['uuid'], row['fstype'], int(row['size']), row.get('name')))
    return found, left_out


def refused_target(row, need, inventory):
    """Why the filesystem can't take the drive (mounted, too small, or one setup would
    refuse), or None. `inventory`: lsblk's tree, for setup's own checks."""
    return why_not(row, need) or (inventory is not None and setup_would_refuse(inventory, row['uuid'])) or None


def kept_for(drive, rows, disks, backup_disks):
    """Why a disk is kept out for this drive: the USB backup's, or a NAS disk for the SSD."""
    if disks & backup_disks:
        return 'the USB backup drive (a backup must stay on a drive of its own)'
    if drive == 'data_ssd' and on_nas(rows, disks):
        return 'a disk on the NAS; the SSD for the apps must be connected to this PC (it holds the databases)'
    return None


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
    return held_lock_reason(
        [
            (lock, 'Setup (sudo bash install.sh) is running'),
            (recovery, 'A backup or an app update is running'),
        ]
    )


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


BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


def this_boot():
    """This boot's ID: it changes at every start of the computer."""
    try:
        return BOOT_ID.read_text().strip()
    except OSError:
        return ''


def monotonic_usec():
    """Now, on the clock systemd times its services by (microseconds since boot)."""
    return int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000)


def started_since(units, since):
    """Those of `units` started after `since` (monotonic_usec, this boot): running now,
    or started and stopped again meanwhile (by you, or by a recovery that stopped them).
    None when systemd could not say for one of them: then nobody knows."""
    started = []
    for unit in units:
        shown = run(['systemctl', 'show', '--property=InactiveExitTimestampMonotonic', unit], 30, check=False)
        value = (getattr(shown, 'stdout', '') or '').strip().partition('=')[2]
        if getattr(shown, 'returncode', 0) or not value.isdigit():
            return None
        if int(value) > since:
            started.append(unit)
    return started


def copy_note(journal):
    """The record of the copy a move made on its new drive (beside the move's record), so
    a move started again may go on with that copy instead of refusing a drive that is no
    longer empty. It names both drives: any other drive must still be empty."""
    return Path(journal).with_name('migrate-copy.json')


def our_copy(journal, drive, old_uuid, new_uuid):
    try:
        note = json.loads(copy_note(journal).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(note, dict) and note == {'drive': drive, 'old': old_uuid, 'new': new_uuid}


def switching(journal):
    """The record just before the switch, with this boot and the time: what the move
    stopped must not have run again since (see ran_again_on_old)."""
    record(journal, phase='switching', boot=this_boot(), since=monotonic_usec())


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
    # A timer still active would start a backup during the quiet copy and the switch
    # (the review of 0.47.9: a stop that failed was passed over).
    ticking = [u for u in nekoserver_units('active,' + AT_WORK) if u.endswith(TIMER)]
    if ticking:
        raise RuntimeError('These did not stop: ' + ', '.join(ticking))
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


def nas_disk_of(folder):
    """The WWN of the disk under what is mounted at `folder` ('' when Linux shows none), read
    as the runtime reads it (backup.rs checked_drive): findmnt's source device, then lsblk
    from it down to its disk."""
    shown = run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(folder)], 30, check=False)
    sources = shown.stdout.split() if shown.returncode == 0 else []
    if not sources:
        return ''
    device = sources[-1].split('[', 1)[0]
    disks = run(['lsblk', '-n', '-s', '-o', 'TYPE,WWN', device], 30, check=False)
    for line in disks.stdout.splitlines() if disks.returncode == 0 else []:
        fields = line.split()
        if fields[:1] == ['disk']:
            return fields[1] if len(fields) > 1 else ''
    return ''


def other_nas_disk(folder, old):
    """Why the drive mounted at `folder` is not the NAS disk the settings keep for the old
    drive (its WWN), or None: a restored or copied LUN carries the same filesystem UUID
    (the runtime refuses it the same way). Nothing to check for a drive not on a NAS."""
    wwn = (old.get('network') or {}).get('wwn') or ''
    if not wwn:
        return None
    seen = nas_disk_of(folder)
    if seen == wwn:
        return None
    if seen:
        return f'has its UUID but is on another NAS disk ({seen}, not {wwn}): a restored or copied LUN, most likely'
    return "is on a NAS disk that can't be told apart (Linux shows no WWN for it)"


def same_drives(mountpoint, old, staging, new):
    """Both drives are still the ones the move began with, before each step: a drive that
    dropped out would leave its folder an empty one on the system disk, and the last copy
    (which deletes what is not on the old drive) and an empty-to-empty comparison would
    then pass over nothing. The old drive is also still the NAS disk setup chose."""
    if mounted_identity(mountpoint) != (old['uuid'], old['fstype'], old.get('subvolume') or ''):
        raise RuntimeError(f'the old drive is no longer the one mounted at {mountpoint} (unplugged, or it failed)')
    problem = other_nas_disk(mountpoint, old)
    if problem:
        raise RuntimeError(f'the old drive mounted at {mountpoint} {problem}')
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


def other_subvolume(old, mounted, label):
    """Why the drive is mounted with another Btrfs subvolume than the settings name, or None.
    A subvolume the settings name must be the one mounted (the review of 0.47.2: the
    mounted one was taken instead, so another tree could be copied); settings that name
    none take the mounted one."""
    named = (old.get('subvolume') or '').rstrip('/')
    if not named or named == mounted.rstrip('/'):
        return None
    return (
        f'The {label} is mounted with the Btrfs subvolume {mounted or "at the top level"}, but the settings '
        f'name {named}; nothing was changed. Mount the one the settings name (sudo bash install.sh does), '
        'then start the move again'
    )


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
    if (old.get('network') or {}).get('transport') == 'nfs':
        # A folder the NAS shares is no drive this can unmount, copy as a whole and swap.
        out(
            f"The {label} is a folder the NAS shares over NFS ({old['network'].get('export')}), which this move "
            'cannot handle: it moves a drive to another drive. STORAGE.md, "Folders a NAS shares over NFS", says '
            'how to go back to a drive. Nothing was changed.'
        )
        return 2
    # The drive as it is mounted now, its Btrfs subvolume included (the settings may not
    # name it): the move checks it is still that one before each step, and `back` needs it.
    mounted = mounted_identity(mountpoint)
    if not mounted or mounted[:2] != (old.get('uuid'), old.get('fstype')):
        out(f'The {label} in the settings is not the drive mounted at {mountpoint}; nothing was changed')
        return 2
    other = other_subvolume(old, mounted[2], label)
    if other:
        out(other)
        return 2
    problem = other_nas_disk(mountpoint, old)
    if problem:
        out(f'The {label} mounted at {mountpoint} {problem}. Nothing was changed')
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
        copy_to_new(journal, drive, old, new, (mountpoint, staging, name), out)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return stopped_safely(exc, label, journal, staging, out)
    switching(journal)
    return switch(path, config, drive, journal, staging, fstab, out)


def copy_to_new(journal, drive, old, new, places, out):
    """The copy of move_held, before its switch: the new drive mounted at the staging
    folder, everything copied twice (the second time with the writers stopped), then every
    file compared. `places`: (the drive's mount folder, the staging folder, the new drive's
    name). Raises when the move must stop."""
    mountpoint, staging, name = places
    mount_staging(new['uuid'], new['fstype'], staging)
    if not empty(staging) and not our_copy(journal, drive, old.get('uuid'), new['uuid']):
        raise RuntimeError(f'{name} is not empty; NekoServer only moves to an empty filesystem')
    # A move started again goes on with the copy this one makes (its last pass removes
    # what is no longer on the old drive, and every file is compared after it).
    ensure_directory(Path(journal).parent, 0o700)
    atomic_json(copy_note(journal), {'drive': drive, 'old': old.get('uuid'), 'new': new['uuid']})
    record(journal, phase='copying')
    same_drives(mountpoint, old, staging, new)
    copy(mountpoint, staging, out=out)
    stop_writers(journal)
    same_drives(mountpoint, old, staging, new)
    copy(mountpoint, staging, final=True, out=out)
    record(journal, phase='verifying')
    out('Comparing every file on both drives by checksum (this reads both drives once more)...')
    # Mounted again first, so the comparison reads the new drive, not Linux's memory of
    # what was just copied onto it: a new drive that kept other bytes passed before.
    run(['umount', str(staging)], 300)
    mount_staging(new['uuid'], new['fstype'], staging)
    same_drives(mountpoint, old, staging, new)
    differ = differences(mountpoint, staging)
    if differ:
        raise RuntimeError(f'{len(differ)} files differ after the copy (the first: {differ[0]})')
    same_drives(mountpoint, old, staging, new)


UUID = re.compile(r'[A-Za-z0-9-]{8,80}')
SUBVOLUME = re.compile(r'(/[A-Za-z0-9@._-]+)*')


def stopped_safely(exc, label, journal, staging, out=print):
    """The move stopped before its switch: the new drive is unmounted and what the move
    stopped (as its record says: also when stopping failed part-way) is started again.
    The record goes only once all of it runs again; otherwise it stays, with what is left.
    Only the move's own new drive is unmounted: a folder that was in use already (the move
    refused it) keeps what someone else mounted there (the review of 0.47.2)."""
    saved = read(journal) or {}
    ours = (saved.get('new') or {}).get('uuid')
    there = mounted_identity(staging)
    if ours and there and there[0] == ours:
        run(['umount', str(staging)], 120, check=False)
    failed = start_again(saved.get('stopped'), out)
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


def fstab_names_new(mountpoint, new, fstab):
    """The one active line that mounts the folder names the new drive (a comment or
    another line naming it does not count): the switch was made before a restart."""
    lines = fstab.read_text().splitlines()
    found = mount_lines(lines, mountpoint)
    return len(found) == 1 and names_drive(lines[found[0]], new['uuid'])


def mounted_in_place(mountpoint, new, there, fstab):
    """Already switched and mounted (resume after setup or the check failed, with the apps
    started again on it): nothing to unmount; setup and the check run again."""
    return there == (new['uuid'], new['fstype'], new.get('subvolume') or '') and fstab_names_new(mountpoint, new, fstab)


def unmounted_and_switched(mountpoint, old, new, fstab, out):
    """The old drive unmounted and /etc/fstab naming the new one; False (said) when the
    folder can't be unmounted."""
    run(['umount', str(mountpoint)], 600, check=False)
    if os.path.ismount(mountpoint):
        out(f'{mountpoint} could not be unmounted (something still uses it); nothing was switched')
        return False
    if not fstab_names_new(mountpoint, new, fstab):
        switch_fstab(mountpoint, old['uuid'], new['uuid'], new['fstype'], fstab, subvolume=new.get('subvolume'))
    return True


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
    run(['umount', str(staging)], 600, check=False)
    in_place = mounted_in_place(mountpoint, new, there, fstab)
    if not in_place and not unmounted_and_switched(mountpoint, old, new, fstab, out):
        return 2
    set_drive(settings, config, drive, new, old)
    if not in_place:
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
    copy_note(journal).unlink(missing_ok=True)
    label = DRIVES[drive][1]
    if going_back:
        out(f'The {label} is the old drive again. Anything saved to the other one since the move is still on it.')
    else:
        out(
            f'Done: the {label} is the new drive, mounted at {saved.get("mount")}. The old drive was not changed: '
            'keep it until you are sure, then disconnect it. To go back to it: sudo python3 migrate.py back'
        )
    return 0


def back(settings, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB, drive=None):
    """Go back to the old drive of `drive` (needed when both drives were moved): the
    settings and /etc/fstab name it again (its Btrfs subvolume too), and setup runs. The
    same checks, lock and durable record as a move, so it never overlaps another job, and
    `resume` finishes it after a restart. What was written to the newer drive since the
    move stays on it."""
    return with_operation(out, lambda: back_held(settings, out, journal, staging, fstab, drive))


NOT_MOVED = 'No drive was moved (the settings name no earlier drive)'


def which_back(config, drive):
    """The drive `back` takes back, or (None, why not)."""
    moved = [d for d in DRIVES if config['storage'].get(d, {}).get('previous_drive')]
    if drive:
        if drive not in DRIVES:
            return None, 'Use --drive data_ssd (the application SSD) or --drive media_hdd (the media HDD)'
        if drive not in moved:
            return None, f'The {DRIVES[drive][1]} was not moved (the settings name no earlier drive for it)'
        return drive, None
    if len(moved) > 1:
        return None, (
            'Both drives were moved: say which one goes back, with  --drive data_ssd  (the application SSD) or  '
            '--drive media_hdd  (the media HDD)'
        )
    if not moved:
        return None, NOT_MOVED
    return moved[0], None


def connected_uuids(folder='/dev/disk/by-uuid'):
    """The UUIDs of the filesystems connected now (no path is made from a UUID the
    settings hold)."""
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


def back_problem(config, path, drive, previous, current):
    """Why `back` can't take `drive` back now, or None: another job at work, the settings'
    records not valid, run from the drive it moves, the old drive not connected."""
    mount_key, label = DRIVES[drive]
    busy = nothing_at_work(recovery=recovery_lock(config))
    if busy:
        return busy + TRY_LATER
    if (
        not all(UUID.fullmatch(str(d.get('uuid', ''))) for d in (previous, current))
        or previous.get('fstype') not in FILESYSTEMS
        or not SUBVOLUME.fullmatch(str(previous.get('subvolume') or ''))
    ):
        return f'The settings\' record of the {label} is not valid; nothing was changed'
    if on_the_moved_drive(path, mount_path(config[mount_key])):
        return FROM_THE_DOWNLOAD.format(label=label)
    if previous['uuid'] not in connected_uuids():
        return f'The old {label} ({previous["uuid"]}) is not connected'
    return None


def back_held(settings, out, journal, staging, fstab, wanted=None):
    path, config = load_settings(settings)
    drive, why = which_back(config, wanted)
    if drive is None:
        out(why)
        return 1 if why == NOT_MOVED else 2
    mount_key, label = DRIVES[drive]
    previous = config['storage'][drive]['previous_drive']
    current = {k: v for k, v in config['storage'][drive].items() if k != 'previous_drive'}
    problem = back_problem(config, path, drive, previous, current)
    if problem:
        out(problem)
        return 2
    record(
        journal,
        phase='stopping',
        direction='back',
        drive=drive,
        mount=str(mount_path(config[mount_key])),
        old=current,
        new=previous,
        before=steps_working(config),
    )
    try:
        stop_writers(journal)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return stopped_safely(exc, label, journal, staging, out)
    switching(journal)
    return switch(path, config, drive, journal, staging, fstab, out)


def resume(settings, out=print, journal=JOURNAL, staging=STAGING, fstab=FSTAB):
    """After a restart (or once you put right what it said): a move cut off before its
    switch changed nothing (the apps are started again on the old drive); one cut off
    while it switched, or after it, is finished on the new drive. It waits for the lock."""
    return with_operation(out, lambda: resume_held(settings, out, journal, staging, fstab), wait=True)


def ran_again_on_old(saved, fstab):
    """A move recorded as switching whose /etc/fstab still names the old drive, while
    something it stopped (Docker, Samba, a NekoServer service) ran again since: it runs
    now, the computer was started again (Docker starts at boot), or it was started and
    stopped again meanwhile (stopped for a recovery, say). Its writes are on the old
    drive only."""
    new = saved.get('new') or {}
    if fstab_names_new(saved.get('mount', ''), new, fstab):
        return False
    stopped = [u for u in saved.get('stopped') or [] if isinstance(u, str) and UNIT.fullmatch(u)]
    if active(stopped):
        return True
    # What can't be known counts as "ran again" (the 0.46.0 review): a record without the
    # boot and the time (one written before 0.45.8), this boot's ID unreadable, or systemd
    # not saying when a service last started. Starting over only copies again; switching
    # to a copy that is out of date would lose what was saved since.
    boot, since, now = saved.get('boot'), saved.get('since'), this_boot()
    if not (isinstance(boot, str) and boot and now and type(since) is int) or boot != now:
        return True
    started = started_since(stopped, since)
    return started is None or bool(started)


def resume_held(settings, out, journal, staging, fstab):
    saved = read(journal)
    if saved is None:
        if Path(journal).exists():
            # A record that can't be read is not "nothing to do": it still keeps upgrades
            # and setup waiting (the review of 0.47.2: resume said it was done).
            out(
                f'The drive move record {journal} can not be read. Nothing was changed. Look at it and at the drives '
                '(sudo findmnt), then remove the record once the drive is where it should be'
            )
            return 2
        return 0
    phase = saved.get('phase')
    if phase == 'switching' and ran_again_on_old(saved, fstab):
        # Not switched yet, and what the move stopped runs again (a restart started it on
        # the old drive): what was written since is not on the new drive, so switching to
        # it would lose it. The move starts over instead (its copy is quick the second time).
        again = (
            'Go back again (sudo python3 migrate.py back)'
            if saved.get('direction') == 'back'
            else 'Start the move again (sudo python3 migrate.py move ...): it goes on with the copy already there, '
            'so only what changed is copied'
        )
        return stopped_safely(
            'the apps ran again on the old drive before it switched (a restart, say), or that cannot be ruled '
            f"out, so the new drive's copy may not be up to date. {again}",
            'drive',
            journal,
            staging,
            out,
        )
    if phase in ('switching', 'setup'):
        path, config = load_settings(settings)
        return switch(path, config, saved['drive'], journal, staging, fstab, out)
    # "restart_failed": a move stopped before its switch, whose apps did not all start
    # again; 0.45.7 also wrote it after a switch, so the drive mounted tells them apart.
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
        if not (args.drive and args.to):
            parser.error('move needs --drive and --to (the UUID list shows)')
        if not UUID.fullmatch(args.to):
            # Said apart: "needs --drive and --to" was all a mistyped UUID got.
            parser.error(f'--to {args.to} is not a filesystem UUID: copy one from  sudo python3 migrate.py list')
        return move(args.config, args.drive, args.to)
    if args.action == 'back':
        return back(args.config, drive=args.drive)
    return {'list': list_targets, 'resume': resume}[args.action](args.config)


if __name__ == '__main__':
    sys.exit(main())
