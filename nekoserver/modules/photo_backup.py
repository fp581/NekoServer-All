"""Install the weekly add-only USB copy of Immich's photos and Nextcloud's files (either
or both, whichever is set up), run as root, failing closed if the USB drive is missing."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import native_runtime
from modules.photo_preservation import resume_command, resume_unit
from neko_common import (
    nas_disk_wwn,
    cloud_files,
    MANAGED_UNIT,
    RESUME_TIMEOUT,
    SYSTEMD_UNITS,
    VERSION,
    atomic_json,
    atomic_write,
    forget_last_run,
    host_timezone,
    immich_wanted,
    mount_path,
    relative_path,
    safe_path,
    job_times,
)

# The drives and their space check only: a server with Nextcloud and no Immich gets its
# USB copy too, and a photo step that failed doesn't hold back the Nextcloud copy. In a
# full setup it still runs after the photo steps (the profile's order).
DEPENDENCIES = ['storage', 'capacity']
DESCRIPTION = 'Install the weekly add-only USB copy of photos and Nextcloud files (requires --apply and external UUID)'

SYSTEM_MOUNTS = ('/', '/boot', '/boot/efi', '/efi', '/home', '/var')  # /efi: where Arch often mounts the EFI partition
BACKUP_FILESYSTEMS = ('ext4', 'xfs', 'btrfs', 'exfat')


# How the weekly copy's command names Nextcloud's files (it copies them when there).
CLOUD_FLAG = ' --cloud '


def disk_layout(config, inventory):
    """([(filesystem row, its physical disk)], disks holding the system, the SSD or the HDD)."""
    forbidden = {u for u in (config.get('storage', {}).get(k, {}).get('uuid') for k in ('data_ssd', 'media_hdd')) if u}
    rows = []
    busy_disks = set()

    def walk(row, disk=None):
        physical = row.get('name') if row.get('type') == 'disk' else disk
        # Every folder it is mounted at: a Btrfs system partition may show /home in MOUNTPOINT.
        mounted = {row.get('mountpoint')} | set(row.get('mountpoints') or ())
        if row.get('uuid') in forbidden or mounted & set(SYSTEM_MOUNTS):
            busy_disks.add(physical)
        rows.append((row, physical))
        for child in row.get('children', []):
            walk(child, physical)

    for row in inventory.get('blockdevices', []):
        walk(row)
    return rows, busy_disks | system_pool_disks(rows), forbidden


def system_pool_disks(rows):
    """A Btrfs system pool over several disks shows its mount on one member only: every
    disk with a device of a system filesystem (the same UUID) is a system disk."""

    def mounted(row):
        return {row.get('mountpoint')} | set(row.get('mountpoints') or ())

    system_ids = {row.get('uuid') for row, _ in rows if row.get('uuid') and mounted(row) & set(SYSTEM_MOUNTS)}
    return {physical for row, physical in rows if row.get('uuid') in system_ids}


def eligible(config, inventory):
    """Filesystems that may hold the USB backup. It must be on its own physical disk: a
    partition of the system disk, the SSD or the media HDD would be lost together with
    what it is meant to protect."""
    rows, busy_disks, forbidden = disk_layout(config, inventory)

    def usable_mount(mount):
        return (
            not mount
            or mount == config['external_backup_mount']
            or (isinstance(mount, str) and mount.startswith(('/run/media/', '/media/')))
        )

    return [
        {**row, 'physical_disk': physical}
        for row, physical in rows
        if row.get('type') in ('disk', 'part')
        and row.get('uuid')
        and row['uuid'] not in forbidden
        and row.get('fstype') in BACKUP_FILESYSTEMS
        and usable_mount(row.get('mountpoint'))
        and physical
        and physical not in busy_disks
    ]


def shared_disk(config, inventory, uuid):
    """Why a saved USB drive cannot be used (it shares a disk with the system, the SSD
    or the HDD), or None. A drive that is not plugged in is checked by the weekly job."""
    rows, busy_disks, _ = disk_layout(config, inventory)
    for row, physical in rows:
        if row.get('uuid') == uuid and physical in busy_disks:
            return (
                f'The saved USB backup drive (UUID {uuid}) is a partition of /dev/{physical}, which also holds '
                'the system, the SSD or the media HDD. One disk failure would lose both copies. '
                'Choose a separate drive: remove "external_backup_uuid" under "photos" in your settings and run setup again.'
            )
    return None


# What a drive's filesystem is called in the message about one that can't be used.
UNSUITABLE = {'vfat': 'FAT (FAT32)', 'ntfs': 'NTFS (Windows)'}


def plugged_in(row):
    """A disk lsblk reports as removable or on USB: a stick or an external drive, never
    an internal disk (a second disk with Windows on it, say)."""
    return row.get('rm') in (True, '1', 1) or row.get('tran') == 'usb'


def no_backup_drive(config, inventory):
    """Why no drive can hold the USB backup: a plugged-in USB or removable drive whose
    filesystem can't (FAT32 on many small sticks, NTFS) is named, with what to do.
    Formatting is only ever suggested for such a drive."""
    rows, busy_disks, _ = disk_layout(config, inventory)
    external = {row.get('name') for row, _ in rows if row.get('type') == 'disk' and plugged_in(row)}
    wrong = [
        f"/dev/{row.get('name')} is formatted {UNSUITABLE[row['fstype']]}"
        for row, physical in rows
        if row.get('type') in ('disk', 'part')
        and row.get('fstype') in UNSUITABLE
        and physical in external
        and physical not in busy_disks
    ]
    if wrong:
        return (
            'No drive can hold the USB backup yet: ' + '; '.join(wrong) + '. A backup drive must be exFAT or ext4 '
            '(FAT32 cannot hold files over 4 GB, such as videos). Copy off anything you need from it, then format it '
            'as exFAT or ext4 (this erases it) with the Disks app (GNOME) or KDE Partition Manager, and run setup again'
        )
    return (
        'No eligible backup drive: it must be a formatted drive of its own, not a partition of the system disk, '
        'the SSD or the media HDD; connect it first'
    )


def select_external(config, inventory, input_fn=input, out_fn=print):
    """Explicitly identify a formatted external filesystem; never guess or format."""
    candidates = eligible(config, inventory)
    if len({v['uuid'] for v in candidates}) != len(candidates):
        raise RuntimeError('Duplicate external filesystem UUID; identify cloned drives first')
    if not candidates:
        raise RuntimeError(no_backup_drive(config, inventory))
    import drive_table

    # Chosen in the same list as the app SSD and the media HDD, by the same numbers.
    table = drive_table.rows(inventory, config)
    drive_table.show_once(table, config, out_fn)
    row = drive_table.ask(table, 'usb', config, input_fn, out_fn, skippable=True)
    if row is None:
        return None  # skipped for now, as offered
    chosen = row['backup']
    photos = config.setdefault('photos', {})
    photos.update(external_backup_uuid=chosen['uuid'], external_backup_fstype=chosen['fstype'])
    photos.pop('external_backup_network', None)  # an earlier drive's; check_drive notes this one's
    return chosen


def copies(config):
    """What the weekly USB copy takes: (Immich's photo archive, Nextcloud's files)."""
    return (
        immich_wanted(config),
        bool(config.get('nextcloud', {}).get('enabled', False)),
    )


def command_line(config, binary, guard, uuid, fstype, source, dest):
    """The runtime command the weekly timer runs (tests check it against the real program).
    `source`: the photo archive, or None when Immich isn't set up."""
    cmd = (
        f'{binary} weekly --config {guard} --uuid {uuid} --fstype {fstype}'
        + (f' --source {source}' if source else '')
        + f' --dest {dest} --media-root {config["media_mount"]} --data {config["data_mount"]}'
    )
    if copies(config)[1]:
        # Nextcloud user files get the same add-only weekly copy as photos.
        cmd += CLOUD_FLAG + str(cloud_files(config))
    return cmd


def check_drive(config, uuid, fstype):
    """The saved USB drive is a disk of its own, of a filesystem it may use, and when it is
    a disk on a NAS, the same NAS disk as before (its WWN)."""
    from modules.storage import inventory_now

    if shared_backup_folder(config):
        return  # tried out by the network storage step; the weekly copy mounts it by its address
    inventory = inventory_now()
    problem = shared_disk(config, inventory, uuid)
    if problem:
        raise RuntimeError(problem)
    same_nas_disk(config.setdefault('photos', {}), nas_identity(inventory, uuid))
    if fstype not in BACKUP_FILESYSTEMS:
        raise RuntimeError('Unexpected backup filesystem')
    if uuid in {config.get('storage', {}).get(key, {}).get('uuid') for key in ('data_ssd', 'media_hdd')}:
        raise RuntimeError('USB UUID must differ from SSD/HDD')


def shared_backup_folder(config):
    """The address of the folder a NAS shares over NFS that is the backup drive (the
    network storage step chose it and tried it out, nfs_exports.py), or None for a drive.
    It is never the media drive's own folder: a copy kept with what it protects is lost
    with it."""
    import nfs_exports

    photos = config.get('photos', {})
    record = {
        'uuid': photos.get('external_backup_uuid'),
        'fstype': photos.get('external_backup_fstype'),
        'network': photos.get('external_backup_network'),
    }
    export = nfs_exports.checked_record(record, '"external_backup_uuid" (and "external_backup_network") under "photos"')
    if export and record['uuid'] == config.get('storage', {}).get('media_hdd', {}).get('uuid'):
        raise RuntimeError(
            f'The backup drive and the media drive are the same shared folder ({export}): a backup kept with '
            'what it protects is lost with it. Share a second folder on the NAS for the backup, take "backup" '
            'out of "nfs" under "network_storage" in my-config.json, and run setup again.'
        )
    return export


def nas_identity(inventory, uuid):
    """The WWN of the NAS disk (iSCSI) the filesystem `uuid` is on; None when it is not
    on a NAS, or not plugged in now."""
    rows, _, _ = disk_layout({}, inventory)
    disks = {row.get('name'): row for row, _ in rows if row.get('type') == 'disk'}
    for row, physical in rows:
        disk = disks.get(physical) or {}
        if row.get('uuid') == uuid and disk.get('tran') == 'iscsi':
            return disk.get('wwn') or ''
    return None


def same_nas_disk(photos, wwn):
    """A backup drive on a NAS is the NAS disk it was (the 0.46.1 audit: a restored or copied
    LUN carries the same filesystem UUID). Saved the first time (also for settings from
    before 0.46.2), and checked by the weekly copy (backup.rs) through the drives file."""
    if wwn is None:
        return
    saved = (photos.get('external_backup_network') or {}).get('wwn')
    choose = (
        'Choose which one to use: remove "external_backup_uuid" under "photos" in my-config.json and run setup again.'
    )
    photos['external_backup_network'] = {
        'transport': 'iscsi',
        'wwn': nas_disk_wwn(saved, wwn, 'USB backup drive', choose),
    }


def left_out_by_choice(ctx):
    """The weekly copy left out: no removable drive was chosen for it."""
    switch_off(ctx.log)
    ctx.log('Offline backup: left out by choice (no removable drive was chosen)')
    return {'status': 'unavailable', 'reason': 'Left out by choice: no removable drive for the weekly copy'}


def chosen_drive(ctx, opts):
    """The USB drive's UUID: the saved one, or the one you pick now (saved for next
    time). None when it has to be picked and nobody is at the keyboard."""
    uuid = opts.get('external_backup_uuid', '').strip()
    if uuid:
        return uuid
    if not sys.stdin.isatty():
        return None
    from modules.storage import inventory_now

    if select_external(ctx.config, inventory_now(), out_fn=ctx.log) is None:
        return None
    if hasattr(ctx, 'persist_config'):
        ctx.persist_config()
    elif getattr(ctx, 'config_path', None):
        saved = json.loads(ctx.config_path.read_text())
        saved['photos'] = opts
        atomic_json(ctx.config_path, saved)
    return opts['external_backup_uuid']


SERVICE_UNIT = SYSTEMD_UNITS / 'nekoserver-photo-backup.service'
TIMER_UNIT = SYSTEMD_UNITS / 'nekoserver-photo-backup.timer'


def archive_source(config):
    """Immich's photo archive folder (a real folder, never a link)."""
    source = safe_path(
        Path(config['media_mount']) / relative_path(config.get('photos', {}).get('archive_dir', 'photos/archive'))
    )
    if source.is_symlink():
        raise RuntimeError(f'Archive folder is a link, not a folder: {source}')
    return source


def destination(config):
    dest = mount_path(config['external_backup_mount'])
    if not dest.is_absolute() or str(dest) in ('/', '/home', '/mnt', '/srv') or dest.is_symlink():
        raise RuntimeError('Unsafe USB destination')
    return dest


def schedule(config):
    """(weekday, HH:MM) of the weekly copy: the weekly time setup asked for (backup_times)."""
    times = job_times(config)
    return times['usb_day'], times['usb']


# The weekly copy pauses Nextcloud for its last files and its database copy (backup.rs
# paused_cloud). Its resume starts Nextcloud again: when the run ends in any way
# (ExecStopPost), and at boot after a power cut.
CLOUD_RESUME = 'nekoserver-nextcloud-resume'


def runtime_of(cmd):
    """The program and the settings guard the weekly command (command_line) names."""
    binary, _, rest = cmd.partition(' weekly --config ')
    return binary, rest.split(' ', 1)[0]


def unit_texts(config, cmd, photos, day, when):
    """The weekly service and its timer, as install_units writes them."""
    binary, guard = runtime_of(cmd)
    cloud = CLOUD_FLAG in cmd
    service = (
        '[Unit]\nDescription=NekoServer add-only USB backup of photos and Nextcloud files\nRequiresMountsFor='
        + config['media_mount']
        + '\n'
        + ('After=nekoserver-immich-db-backup.service\n' if photos else '')
        + '[Service]\nType=oneshot\nExecStart='
        + cmd
        + '\n'
        + (
            f'ExecStopPost={resume_command(binary, guard, "cloud-resume")}\nTimeoutStopSec={RESUME_TIMEOUT}\n'
            if cloud
            else ''
        )
    )
    schedule = (
        '[Unit]\nDescription=NekoServer weekly USB backup schedule\n[Timer]\nOnCalendar='
        + day
        + ' *-*-* '
        + when
        + ':00 '
        + host_timezone(config)
        + '\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
    )
    return service, schedule


def install_units(config, cmd, photos, day, when, unit=None, timer=None):
    """Write the weekly service and timer and switch the timer on. Only a unit NekoServer
    wrote is replaced (the timer's older wording included). With Nextcloud, also its
    resume service, beside the weekly one."""
    unit = unit or SERVICE_UNIT
    timer = timer or TIMER_UNIT
    cloud = CLOUD_FLAG in cmd
    binary, guard = runtime_of(cmd)
    service, schedule = unit_texts(config, cmd, photos, day, when)
    for path, text in ((unit, service), (timer, schedule)):
        current = path.read_text() if path.exists() else text
        if current != text and not current.startswith(
            (MANAGED_UNIT, '[Unit]\nDescription=Weekly NekoServer photo backup\n')
        ):
            raise RuntimeError(f'Unmanaged systemd unit preserved: {path}')
    resume = unit.parent / (CLOUD_RESUME + '.service')
    if cloud and resume.exists() and f'Description={CLOUD_RESUME}\n' not in resume.read_text():
        raise RuntimeError(f'Unmanaged systemd unit preserved: {resume}')
    forget_last_run(timer)  # a timer created again must not run every missed week at once
    atomic_write(unit, service, 0o644)
    atomic_write(timer, schedule, 0o644)
    if cloud:
        atomic_write(resume, resume_unit(binary, guard, config['data_mount'], CLOUD_RESUME, 'cloud-resume'), 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=120)
    subprocess.run(
        ['systemctl', 'enable', '--now', timer.name], check=True, capture_output=True, text=True, timeout=120
    )  # (see photo_preservation)
    if cloud:
        subprocess.run(['systemctl', 'enable', resume.name], check=True, capture_output=True, text=True, timeout=120)


def run(ctx):
    opts = ctx.config.setdefault('photos', {})
    if not ctx.apply:
        ctx.log('Weekly photo backup available; --apply installs timer with external UUID.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    photos, cloud = copies(ctx.config)
    if not (photos or cloud):
        switch_off(ctx.log)
        ctx.log('USB backup: neither Immich nor Nextcloud is set up, so there is nothing for it to copy')
        return {'status': 'unavailable', 'reason': 'Nothing to copy: Immich and Nextcloud are both switched off'}
    left_out = opts.get('external_backup_wanted') is False and not str(opts.get('external_backup_uuid', '')).strip()
    # Asked for by name (install.sh --module photo_backup, the way to add it later; gate
    # run B4): the drive is asked again, and "none for now" again keeps it left out.
    again = left_out and getattr(ctx, 'only', None) == 'photo_backup' and sys.stdin.isatty()
    if left_out and not again:
        # "None for now" on the storage page: left out by choice, and not asked about again.
        return left_out_by_choice(ctx)
    if again:
        opts.pop('external_backup_wanted')
    uuid = chosen_drive(ctx, opts)
    if uuid is None and again:
        opts['external_backup_wanted'] = False
        return left_out_by_choice(ctx)
    if uuid is None:
        return {
            'status': 'needs_setup',
            'reason': 'No USB backup drive is chosen yet, so the weekly copy of your photos and Nextcloud files waits for one. '
            'Connect it and run  sudo bash install.sh  again to choose it',
        }
    if not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid):
        raise RuntimeError('Set photos.external_backup_uuid from lsblk -f')
    fstype = opts.get('external_backup_fstype', 'ext4')
    known = opts.get('external_backup_network')
    check_drive(ctx.config, uuid, fstype)
    if opts.get('external_backup_network') != known and hasattr(ctx, 'persist_config'):
        # The NAS disk's WWN, learned now, is saved in my-config.json at once: a later setup
        # must compare against it, never take a replacement disk for a first look (the
        # review of 0.46.3: it was only in memory and in the runtime's drives file).
        ctx.persist_config()
    source = archive_source(ctx.config) if photos else None
    # The photos step makes this folder; when it could not run (Docker failed, say),
    # this step waits for it instead of failing on its account.
    if photos and not source.is_dir():
        return {
            'status': 'needs_setup',
            'reason': f'Waiting for the photos (Immich) step, which makes the photo archive {source}: finish that step, then run setup again',
        }
    dest = destination(ctx.config)
    day, when = schedule(ctx.config)
    binary = native_runtime.deploy(
        Path(__file__).resolve().parents[1],
        ctx.config,
        Path(ctx.config['data_mount']) / 'installer' / 'runtime' / ('v' + VERSION),
    )
    cmd = command_line(ctx.config, binary, native_runtime.backup_guard(ctx.config), uuid, fstype, source, dest)
    install_units(ctx.config, cmd, photos, day, when)
    seal_passwords(binary, native_runtime.backup_guard(ctx.config), ctx.log)
    ctx.log(
        'Enabled weekly USB copy of '
        + what_is_copied(photos, cloud)
        + '; when the USB drive is absent the run fails visibly instead of writing to the OS disk'
    )


def seal_passwords(binary, guard, log):
    """Setup's own encrypted password list on the HDD (backup.rs seal_passwords), made
    again whenever a password or the backup key changed: the weekly USB copy takes it,
    so the USB drive holds the app passwords from the first week on, not only after the
    first monthly snapshot. A failure is said; the USB copy then reports itself
    incomplete until a list is there."""
    try:
        done = subprocess.run(
            [str(binary), 'seal-passwords', '--config', str(guard)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f'Password list: not made ({exc}); the USB copy says it is incomplete until there is one')
        return
    said = str(done.stdout or '').strip().splitlines()
    if done.returncode == 0 and said:
        log(said[-1])
    else:
        why = str(done.stderr or done.stdout or '').strip()[-300:]
        log(f'Password list: not made ({why}); the USB copy says it is incomplete until there is one')


def switch_off(log, unit=None, timer=None, run=None):
    """Nothing to copy any more (Immich and Nextcloud both switched off): an installed
    weekly copy is stopped and its schedule switched off, so it no longer runs with the
    old selection. Its unit files stay (the dashboard shows the job as switched off) and
    the copies already on the USB drive are kept."""
    from modules._schedules import retire

    units = [unit or SERVICE_UNIT, timer or TIMER_UNIT]
    if any(u.exists() for u in units):
        retire(units, run or subprocess.run)
        log('USB backup: switched off (nothing to copy); the copies on the USB drive stay')


def what_is_copied(photos, cloud):
    return ' and '.join(name for name, on in (('Immich photos/videos', photos), ('Nextcloud files', cloud)) if on)


def refresh(config, log, unit=None, timer=None):
    """After every setup run: the installed weekly copy takes what is set up now, so
    Nextcloud (or Immich) added later is on the USB drive from the next week on. The
    program the installed job already runs stays; nothing happens when the copy is not
    installed or has nothing to copy now."""
    unit = unit or SERVICE_UNIT
    timer = timer or TIMER_UNIT
    if not unit.is_file():
        return False
    current = unit.read_text()
    found = re.search(r'^ExecStart=(\S+) weekly ', current, re.MULTILINE)
    opts = config.get('photos', {})
    uuid = str(opts.get('external_backup_uuid', '')).strip()
    photos, cloud = copies(config)
    if not found or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid):
        return False
    if not (photos or cloud):
        switch_off(log, unit, timer)
        return False
    try:
        source = archive_source(config) if photos else None
        if source is not None and not source.is_dir():
            log(f'USB backup: not refreshed yet, the photo archive {source} does not exist')
            return False
        cmd = command_line(
            config,
            found.group(1),
            native_runtime.backup_guard(config),
            uuid,
            opts.get('external_backup_fstype', 'ext4'),
            source,
            destination(config),
        )
        # After every setup run, which is where passwords are made and changed.
        seal_passwords(found.group(1), native_runtime.backup_guard(config), log)
        day, when = schedule(config)
        # Current only when both files are as setup would write them now (the copy, and the
        # day and time) and the timer is on: a copy switched off while no app was set up,
        # or a new schedule, stayed as it was (the 0.46.0 review).
        service, timer_text = unit_texts(config, cmd, photos, day, when)
        on = subprocess.run(['systemctl', 'is-enabled', '--quiet', timer.name], check=False, timeout=30)
        if current == service and timer.is_file() and timer.read_text() == timer_text and on.returncode == 0:
            return False
        install_units(config, cmd, photos, day, when, unit, timer)
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        log(f'USB backup: settings not refreshed ({exc})')
        return False
    log('USB backup: the weekly copy now takes ' + what_is_copied(photos, cloud))
    return True
