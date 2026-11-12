"""Safe existing-filesystem mounting by explicitly configured UUID; never formats."""

import contextlib
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from neko_common import (
    atomic_json,
    atomic_write,
    drives_in_use,
    extra_drives,
    findmnt,
    mount_path,
    nas_disk_wwn,
    safe_path,
)

DEPENDENCIES = ['system', 'network_storage']
# The two roles every server has: (its record under "storage", the setting that says where it is mounted).
OLD_ROLES = (('data_ssd', 'data_mount'), ('media_hdd', 'media_mount'))
DESCRIPTION = 'Inspect disks; --apply mounts explicitly identified data SSD and media HDD by UUID'


# How /etc/fstab names a drive by its UUID ("UUID=1234-..."), and the file itself.
BY_UUID_WORD = 'UUID='
FSTAB = Path('/etc/fstab')
# How the setup window names a row of the drive list in an answer ("drive:4").
WINDOW_ROW = 'drive:'


def window_row(answer):
    """The number of the row the setup window names ("drive:4" is "4"), or None."""
    return answer.removeprefix(WINDOW_ROW) if answer.startswith(WINDOW_ROW) else None


def call(argv, timeout=30):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError(f'{argv[0]} failed: {p.stderr.strip()[:400]}')
    return p.stdout.strip()


def inventory_now():
    """Every disk and partition, as lsblk JSON (one command, used by all drive pickers)."""
    return json.loads(
        call(['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINT,MOUNTPOINTS,MODEL,RM,TRAN,LABEL,WWN,ROTA'])
    )


# What can hold a filesystem: a disk or partition, an opened encrypted drive (LUKS), an
# LVM volume or a software RAID array. The last three sit on top of one or more disks.
VOLUME_KINDS = ('part', 'disk', 'crypt', 'lvm', 'raid0', 'raid1', 'raid4', 'raid5', 'raid6', 'raid10', 'md')
DEV = '/dev/'  # where Linux shows drives
BY_UUID = DEV + 'disk/by-uuid/'  # each filesystem by its UUID
# Where Linux keeps itself: the root and the boot folders (Arch often mounts the EFI
# partition at /efi). A disk holding any of them is the system disk.
SYSTEM_MOUNTS = ('/', '/boot', '/boot/efi', '/efi')


def mount_points(dev):
    """Every folder an lsblk row is mounted at. MOUNTPOINT names only one of them: a
    Btrfs system partition mounted as several subvolumes (/, /home, /.snapshots) may
    show /home there, so MOUNTPOINTS (util-linux 2.37 and newer) is read too."""
    found = {dev.get('mountpoint')} | set(dev.get('mountpoints') or ())
    return {m for m in found if m}


def holds_system(dev):
    """This lsblk row, or anything under it, holds the system (SYSTEM_MOUNTS)."""
    return bool(mount_points(dev) & set(SYSTEM_MOUNTS)) or any(holds_system(c) for c in dev.get('children', []))


def device_sub_uuid(name):
    """A Btrfs device's own UUID (UUID_SUB): each device of a pool has its own, a cloned
    drive the same as its original. None when it can't be read."""
    for path in (DEV + name, DEV + 'mapper/' + name):
        if os.path.exists(path):
            try:
                return call(['blkid', '-s', 'UUID_SUB', '-o', 'value', path]) or None
            except (RuntimeError, OSError, subprocess.SubprocessError):
                return None
    return None


def merged(rows, sub_uuid=device_sub_uuid):
    """One candidate per filesystem. A filesystem lsblk lists more than once is still one:
    an LVM volume or RAID array under each disk it spans (same name), or a Btrfs pool
    over several drives (one UUID, a device each, each device with its own UUID_SUB). Two
    other devices with one UUID are clones, which mounting by UUID can't tell apart."""
    by_uuid = {}
    for row in rows:
        by_uuid.setdefault(row['uuid'], []).append(row)
    return [one_filesystem(group, sub_uuid) for group in by_uuid.values()]


CLONED = 'Duplicate filesystem UUID detected; identify cloned drives before continuing'


def one_filesystem(group, sub_uuid=device_sub_uuid):
    """The lsblk rows of one UUID as one candidate, on every disk they span. Btrfs rows are
    one pool only when each device has a UUID_SUB of its own: a copy of a Btrfs drive (dd,
    a disk cloner) has the same one, and the kernel could then write to either drive."""
    names = sorted({r['name'] for r in group})
    mountpoint = next((r['mountpoint'] for r in group if r['mountpoint']), None)
    sized = {}
    if len(names) > 1:
        if not all(r['fstype'] == 'btrfs' for r in group):
            raise RuntimeError(CLONED)
        pool_members(names, sub_uuid)
        sized = pool_size(group, mountpoint)
    disks = sorted({d for r in group for d in r['physical_disks'] if d})
    return {
        **group[0],
        'physical_disks': disks,
        'physical_disk': disks[0] if disks else None,
        'mountpoint': mountpoint,
        'unlocks': sorted({n for r in group for n in r.get('unlocks', ())}),
        **sized,
    }


# How many copies of the data a Btrfs data profile keeps, for working out how much of a
# pool over several drives is usable. RAID5 and RAID6 are counted as 2 and 3 copies,
# which is never more than they really hold (their parity takes less), so they are safe.
PROFILE_COPIES = {'SINGLE': 1, 'DUP': 2, 'RAID1': 2, 'RAID10': 2, 'RAID1C3': 3, 'RAID1C4': 4, 'RAID5': 2, 'RAID6': 3}


def kept_copies(sizes, copies):
    """Bytes usable on drives of these sizes when every piece of data is kept on `copies`
    different drives (RAID1 and the like): the most x with sum(min(size, x)) >= copies * x.
    A 32 GB drive mirrored with a 4 TB one holds 32 GB, not 2 TB."""
    if len(sizes) < copies:
        return 0
    low, high = 0, sum(sizes) // copies
    while low < high:
        middle = (low + high + 1) // 2
        if sum(min(s, middle) for s in sizes) >= copies * middle:
            low = middle
        else:
            high = middle - 1
    return low


def profile_capacity(profile, sizes):
    """Usable bytes of a Btrfs pool of drives of `sizes` keeping its data as `profile`."""
    if profile == 'SINGLE':
        return sum(sizes)
    if profile == 'DUP':  # both copies on the same drive
        return sum(sizes) // 2
    if profile == 'RAID0':  # striped over at least two drives
        return min(sum(sizes), 2 * (sum(sizes) - max(sizes)))
    return kept_copies(sizes, PROFILE_COPIES.get(profile, len(sizes)))


def data_profiles(names):
    """The data profiles of a Btrfs pool (RAID1, SINGLE...), read from one of its drives
    (`btrfs inspect-internal dump-tree -t chunk`, which needs root and the btrfs tool),
    or None when they can't be read. More than one while a conversion is unfinished."""
    for name in names:
        path = DEV + name if os.path.exists(DEV + name) else DEV + 'mapper/' + name
        if not os.path.exists(path):
            continue
        try:
            text = call(['btrfs', 'inspect-internal', 'dump-tree', '-t', 'chunk', path], timeout=60)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            continue
        found = {
            (set(kind.split('|')) - {'DATA', 'METADATA', 'SYSTEM'} or {'SINGLE'}).pop()
            for kind in re.findall(r'\btype ([A-Z0-9|]+)', text)
            if 'DATA' in kind.split('|')
        }
        if found:
            return found
    return None


def mounted_capacity(folder):
    """Usable bytes of the filesystem mounted at `folder`, as it reports them (used and
    still free: Btrfs works out the free space by its data profile), or None."""
    try:
        found = os.statvfs(folder)
    except OSError:
        return None
    return (found.f_blocks - found.f_bfree + found.f_bavail) * found.f_frsize


def pool_size(group, mountpoint):
    """{'size', 'size_note'} for a Btrfs pool over several drives (its lsblk rows). Not the
    first drive's size (that made the answer depend on the order lsblk lists them): the
    pool's own figure when it is mounted, else what its data profile makes usable, else,
    when the profile can't be read, only its smallest drive, to be safe (and said so)."""
    members = {r['name']: size_bytes(r.get('size')) for r in group}
    sizes = sorted(s for s in members.values() if s)
    shown = f"a Btrfs pool of {len(members)} drives ({' + '.join(size_words(f'{s}B') for s in sizes)})"
    usable = mounted_capacity(mountpoint) if mountpoint else None
    if usable is not None:
        return {'size': f'{usable}B', 'size_note': f'{shown}, usable space as the pool itself reports it'}
    profiles = data_profiles(sorted(members)) if sizes else None
    if profiles:
        return {
            'size': f'{min(profile_capacity(p, sizes) for p in profiles)}B',
            'size_note': f"{shown} keeping its data as {' and '.join(sorted(profiles))}: this much is usable",
        }
    return {
        'size': f'{sizes[0]}B' if sizes else None,
        'size_note': f'{shown} whose layout (RAID1 or not) could not be read, so only its smallest drive is '
        'counted, to be safe. Run setup with sudo, with the btrfs tool installed, to read it',
    }


def pool_members(names, sub_uuid=device_sub_uuid):
    """Several devices with one Btrfs UUID are one pool only when each has a UUID_SUB of
    its own. One that can't be read (blkid needs root to read a device that is not in its
    cache) fails closed too, but says so: the drives are not known to be copies."""
    subs = [sub_uuid(n) for n in names]
    unread = [n for n, sub in zip(names, subs) if sub is None]
    if unread:
        raise RuntimeError(
            f'Could not read the Btrfs device ID of /dev/{unread[0]}, which tells the drives of a Btrfs '
            'pool apart from copies of one drive. Run setup with sudo (sudo bash install.sh) so it can '
            'read it; if it still can\'t, check that the drive is connected and working.'
        )
    if len(set(subs)) != len(subs):
        raise RuntimeError(CLONED)


CRYPTTAB = Path('/etc/crypttab')


def unlocked_at_boot(name, crypttab=CRYPTTAB):
    """An opened encrypted drive (`name` as lsblk shows it) that Linux unlocks at every
    start, so it can be mounted then: it is listed in /etc/crypttab, without `noauto`
    (a line with it is only unlocked by hand)."""
    try:
        lines = crypttab.read_text().splitlines()
    except OSError:
        return False
    rows = [line.split() for line in lines if line.strip() and not line.lstrip().startswith('#')]
    return any(row[0] == name and 'noauto' not in (row[3].split(',') if len(row) > 3 else ()) for row in rows)


def boot_unlock_problem(device, crypttab=CRYPTTAB):
    """Why an encrypted drive can't be used yet, or None. That is the drive itself, or the
    encrypted drives under it (an LVM volume or RAID array on top of encrypted drives)."""
    names = [*(device.get('unlocks') or ()), *([device['name']] if device.get('kind') == 'crypt' else [])]
    locked = [n for n in names if not unlocked_at_boot(n, crypttab)]
    if not locked:
        return None
    return (
        f"/dev/mapper/{locked[0]} is an encrypted drive that you opened by hand. Setup mounts it at every "
        'start, so Linux must unlock it then: add it to /etc/crypttab (with a key file, or a password asked '
        'at start-up), restart, and run setup again.'
    )


def candidate(dev, parent, physical, unlocks=(), tran=None, wwn=None):
    """One lsblk row as a filesystem setup could use, or None. `unlocks`: the encrypted
    drives it sits on, which Linux must open before it can be mounted."""
    uuid = dev.get('uuid')
    fs = dev.get('fstype')
    kind = dev.get('type', '')
    if kind not in VOLUME_KINDS or not uuid or fs not in ('ext4', 'xfs', 'btrfs'):
        return None
    return {
        'uuid': uuid,
        'fstype': fs,
        'name': dev.get('name', ''),
        'size': dev.get('size'),
        'label': dev.get('label'),
        'model': dev.get('model') or parent,
        'mountpoint': dev.get('mountpoint') or next(iter(sorted(mount_points(dev))), None),
        'physical_disks': [physical],
        'kind': kind,
        'unlocks': list(unlocks),
        # On a NAS (iSCSI): its disk's transport and identity (WWN), never its sdX name.
        'network': tran == 'iscsi',
        'wwn': wwn,
    }


def unlocked(device):
    """The chosen drive, unless it is an encrypted one Linux doesn't unlock at start."""
    problem = boot_unlock_problem(device)
    if problem:
        raise RuntimeError(problem)
    return device


# Where Linux reports the software RAID arrays (each /sys/block/<md>/md).
SYS_BLOCK = '/sys/block'
RAID_KINDS = ('md', 'linear', 'raid0', 'raid1', 'raid4', 'raid5', 'raid6', 'raid10')


def raid_lost(name, sys_block=None):
    """What a software RAID array is missing, or None: the array `name` (md0, as lsblk
    and /sys/block name it) with a failed or absent drive. Reads nothing for other names."""
    md = Path(sys_block or SYS_BLOCK) / name / 'md'
    if not md.is_dir():
        return None
    try:
        missing = int((md / 'degraded').read_text().strip() or 0)
    except (OSError, ValueError):
        missing = 0
    failed = []
    for member in sorted(md.glob('dev-*')):
        try:
            states = (member / 'state').read_text().split(',')
        except OSError:
            continue
        if any(s.strip() == 'faulty' for s in states):
            failed.append(member.name[len('dev-') :])
    if not missing and not failed:
        return None
    return ('the drive ' + ', '.join(failed) + ' failed') if failed else 'a drive is missing'


def whole(device, sys_block=None):
    """The chosen drive, unless it is a RAID array that has lost a drive: what you put on
    it would start life with no second copy, and the mirror may be about to go too."""
    if device.get('kind') not in RAID_KINDS:
        return device
    lost = raid_lost(device.get('name', ''), sys_block)
    if lost:
        raise RuntimeError(
            f"The RAID array {device['name']} has lost a drive ({lost}), so what you put on it "
            'would have no second copy. Replace or reconnect the drive and let the array rebuild '
            '(cat /proc/mdstat shows the progress), then run setup again.'
        )
    return device


def discover_filesystems(inventory, sub_uuid=device_sub_uuid):
    """Return candidates, never auto-select a device on model/size alone."""
    rows, placed = [], []

    def walk(dev, parent='', disk=None, unlocks=(), tran=None, wwn=None):
        physical = dev.get('name', '') if dev.get('type') == 'disk' else disk
        if dev.get('type') == 'disk':
            tran, wwn = dev.get('tran'), dev.get('wwn')
        placed.append((dev, physical))
        row = candidate(dev, parent, physical, unlocks, tran, wwn)
        if row:
            rows.append(row)
        below = (*unlocks, dev.get('name', '')) if dev.get('type') == 'crypt' else unlocks
        for child in dev.get('children', []):
            walk(child, dev.get('model') or parent, physical, below, tran, wwn)

    for device in inventory.get('blockdevices', []):
        walk(device)
    os_disks, system_ids = system_disks(placed)
    # The system's own filesystems (and anything sharing a disk with them) are never offered.
    mine = [r for r in rows if r['uuid'] not in system_ids]
    return [v for v in merged(mine, sub_uuid) if not set(v['physical_disks']) & os_disks]


def system_disks(placed):
    """The disks and filesystem UUIDs of the system, from (lsblk row, its disk) pairs. A
    Btrfs system pool over two drives shows as mounted on one of them only (lsblk names
    one member): every disk with a device of a system filesystem is a system disk."""
    system_ids = {dev.get('uuid') for dev, _ in placed if mount_points(dev) & set(SYSTEM_MOUNTS)}
    os_disks = {disk for dev, disk in placed if mount_points(dev) & set(SYSTEM_MOUNTS)}
    os_disks |= {disk for dev, disk in placed if dev.get('uuid') and dev.get('uuid') in system_ids}
    return os_disks, system_ids


NOT_LINUX = {'ntfs': 'NTFS (Windows)', 'exfat': 'exFAT', 'vfat': 'FAT'}


def empty_disk(dev):
    """A whole disk with nothing on it yet (a new drive straight from the box): no
    filesystem, no partitions, not in use. Not RAM disks, eMMC boot areas or empty slots;
    a partition without a filesystem may be one Windows keeps for itself, so never those."""
    return (
        dev.get('type') == 'disk'
        and not dev.get('fstype')
        and not dev.get('children')
        and not dev.get('mountpoint')
        and dev.get('size') not in (None, '0B')
        and not re.match(r'(zram|ram|nbd)\d|mmcblk\d+(boot|rpmb)', dev.get('name', ''))
    )


# The drives setup asks for: (settings key, what it is for, its short name).
DRIVES = (
    ('data_ssd', 'the SSD (apps, photos and music)', 'the SSD'),
    ('media_hdd', 'the HDD (movies, shows, Nextcloud files and backups)', 'the HDD'),
)


def drive_label(config, key, label):
    """What setup calls a drive when it asks for it: what goes on it on this server."""
    from neko_common import drive_holds

    # What goes on it on this server only (gate run C).
    return label.split(' (')[0] + ' (' + drive_holds(config, key) + ')'


GIB = 1024**3
UNITS = {'B': 0, 'K': 1, 'M': 2, 'G': 3, 'T': 4, 'P': 5}


def size_bytes(text):
    """lsblk's size ("465.8G", "1.8T", "9.6M") in bytes, or None when it says none."""
    match = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*([BKMGTP])i?B?\s*', str(text or ''), re.IGNORECASE)
    if not match:
        return None
    return int(float(match[1].replace(',', '.')) * 1024 ** UNITS[match[2].upper()])


def size_words(text):
    """A size as people read it on a box: "16 GB", "1.8 TB" (lsblk's own words when unknown)."""
    found = size_bytes(text)
    if found is None:
        return str(text or 'size unknown')
    gib = found / GIB
    if gib < 1:
        return f'{found / 1024**2:.0f} MB'
    return f'{gib / 1024:.1f} TB' if gib >= 1024 else f'{gib:.0f} GB'


def sizes(key):
    """(smallest, recommended) free space in GB for this drive: the drive space step's own
    numbers (capacity.py), so the two never disagree."""
    from modules import capacity

    return capacity.SSD if key == 'data_ssd' else capacity.HDD


# A filesystem keeps a share of its drive for itself (ext4 about 5 %: blocks reserved
# for root, its own tables), so a drive of exactly the minimum size never has that much
# free. The picker allows for it, so it never accepts a drive the drive space step then
# refuses.
OVERHEAD = 1.07


def big_enough(device, smallest):
    """The drive is big enough to have `smallest` GB free once formatted (a smaller one
    never can). Unknown sizes are left to the drive space step."""
    found = size_bytes(device.get('size'))
    return found is None or found >= smallest * GIB * OVERHEAD


def drive_needed(smallest):
    """The drive size that has `smallest` GB free once formatted, in the GB `size_words`
    shows drives in: said with the minimum, so a 20 GB drive is never refused as "too
    small: it needs at least 20 GB"."""
    return math.ceil(smallest * OVERHEAD)


def local_ssd(key, device):
    """The application SSD is on this PC: it holds the databases and setup's own records,
    and makes the apps fast. A disk on a NAS is for the media HDD and the backup drive."""
    if key == 'data_ssd' and device.get('network'):
        import drive_table

        raise RuntimeError(
            f"{drive_table.said(device, first=True)} is a disk on the NAS. The SSD for the apps must be connected to "
            'this PC (by USB 3, or inside it): it holds the databases and setup\'s own records, and is what makes '
            'the apps fast. Choose the NAS disk for the media HDD, then run setup again. Nothing was changed.'
        )
    return device


def new_drive(record, device):
    """The settings of a drive just chosen: its UUID and filesystem, and nothing kept from
    the drive it replaces. The old drive's Btrfs subvolume ("/@data", say) would make an
    ext4 replacement fail its check, and a new Btrfs one look for a subvolume it may not
    have; the new drive's own subvolume is found again when it is mounted
    (remember_subvolumes, mount_all)."""
    record.clear()
    record.update(uuid=device['uuid'], fstype=device['fstype'])
    if device.get('network'):
        record['network'] = {'transport': 'iscsi', 'wwn': device.get('wwn')}


def role_drive(config, key, label, found, asking):
    """The drive of one role, by its UUID: the one in the settings, checked, or one
    chosen now. None when the role has no drive of its own to count: it is kept on the
    disk Linux runs from, or nothing is chosen in a run that only looks.
    `found`: (the filesystems setup may use, the drive list, lsblk's listing);
    `asking`: (apply, interactive, input_fn, out_fn)."""
    items, table, inventory = found
    apply, interactive, input_fn, out_fn = asking
    record = config['storage'][key]
    uuid = record.get('uuid', '').strip()
    if uuid:
        uuid = saved_choice(
            record,
            uuid,
            items,
            key,
            label,
            apply,
            inventory=inventory,
            config=config,
            interactive=interactive,
            out_fn=out_fn,
            table=table,
        )
    if uuid:
        return uuid
    if not apply:
        out_fn(f'{key} is not configured; {len(items)} formatted candidates discovered (dry-run only)')
        return None
    if not interactive:
        raise RuntimeError(
            f'No drive is chosen for {label} yet. Run  sudo bash install.sh  in a terminal on the server to choose it.'
        )
    row = choose_new(config, table, key, input_fn, out_fn)
    if row['system']:
        on_system_disk(record, key)
        return None
    other = config['storage'].get('data_ssd') or {}
    if key == 'media_hdd' and row['uuid'] and other.get('uuid') == row['uuid']:
        # The drive the apps were just given: both roles become folders of it.
        on_one_drive_both(config, row)
        return None
    device = whole(unlocked(row['device']))
    local_ssd(key, device)
    new_drive(record, device)
    return device['uuid']


def resolve_storage_identity(config, inventory, apply=False, interactive=False, input_fn=input, out_fn=print):
    """Identify disks by verified UUID. Explicit confirmation is required for unknown devices."""
    import drive_table

    items = discover_filesystems(inventory)
    configured = config.setdefault('storage', {})
    # Everything connected, each drive with the number it is chosen by and named with after.
    table = drive_table.rows(inventory, config, items) if apply else []
    selected = set()
    selected_disks = set()
    for key, label, _ in DRIVES:
        label = drive_label(config, key, label)
        record = configured.setdefault(key, {})
        if shared_folder(record, key):
            # A folder the NAS shares (NFS): no disk to look for here; network_storage
            # tried it out, and mounting it below shows whether it is still there.
            selected.add(record.get('uuid', '').strip())
            continue
        if system_folder(record, key):
            continue  # a folder of the disk Linux runs from: checked when it is mounted
        uuid = role_drive(config, key, label, (items, table, inventory), (apply, interactive, input_fn, out_fn))
        if uuid is None:
            continue
        if uuid in selected:
            raise RuntimeError('SSD and HDD cannot use the same filesystem')
        selected_disks |= own_disks(uuid, items, selected_disks, apply)
        selected.add(uuid)
    return configured


# ---- a role kept on the disk Linux runs from (0.50.0) -----------------------------------
# The person may choose that disk for the apps, for the media and backups, or for both (a
# PC with one drive). The role is then a folder of that disk, mounted at the role's usual
# place by a bind mount, so every path and every check stays what it is: findmnt shows
# such a mount as /dev/sda2[/var/lib/nekoserver/apps], the way it shows a Btrfs
# subvolume, and the record keeps that folder as its "subvolume", which every drive
# check (here and in the runtime) already compares.
# The folders are fixed, one for each role, and never one inside the other (the app
# backup copies the app folder: the backups must not be in it).
SYSTEM_FOLDERS = {'data_ssd': '/var/lib/nekoserver/apps', 'media_hdd': '/var/lib/nekoserver/media'}
# Both roles on one drive of its own (0.50.0, the user: any number of drives works, one
# drive included): the drive is mounted once here, and each role is a folder of it,
# mounted at the role's usual place as on the disk Linux runs from.
ONE_DRIVE = '/mnt/nekoserver-drives/one'
ONE_DRIVE_KEY = 'one_drive'  # the drive's own line among the mounts (mount_specs)
SHARED_FOLDERS = {'data_ssd': ONE_DRIVE + '/nekoserver/apps', 'media_hdd': ONE_DRIVE + '/nekoserver/media'}


def on_one_drive(folder):
    """The folder is a role's on a drive both roles share (not the disk Linux runs from)."""
    return isinstance(folder, str) and folder in SHARED_FOLDERS.values()


def inside_filesystem(folder):
    """The folder as findmnt shows it inside its filesystem: /nekoserver/apps for one of
    the shared drive's, the folder itself on the disk Linux runs from."""
    return folder[len(ONE_DRIVE) :] if on_one_drive(folder) else folder


def system_folder(record, key):
    """The folder of the system disk this role is kept in, or None for a drive of its
    own. The settings name it ("folder"); only the role's own fixed folder is accepted,
    and that constant is what is used."""
    if not isinstance(record, dict) or record.get('folder') is None:
        return None
    if key in SHARED_FOLDERS and record['folder'] == SHARED_FOLDERS[key]:
        return SHARED_FOLDERS[key]
    if key not in SYSTEM_FOLDERS or record['folder'] != SYSTEM_FOLDERS[key]:
        raise RuntimeError(
            f'"folder" of "{key}" under "storage" in my-config.json must be {SYSTEM_FOLDERS.get(key, "left out")}: '
            'it is set by setup when the disk Linux runs from is chosen. Take it out and run setup again to choose a drive.'
        )
    return SYSTEM_FOLDERS[key]


def system_folders(config):
    """{where a role is mounted: its folder on the system disk} for the roles kept there."""
    found = {}
    for key, mount_key in (('data_ssd', 'data_mount'), ('media_hdd', 'media_mount')):
        folder = system_folder(config.get('storage', {}).get(key), key)
        if folder:
            found[str(mount_path(config[mount_key]))] = folder
    return found


def system_filesystem():
    """(UUID, filesystem type) of the filesystem Linux runs from."""
    found = (findmnt('UUID,FSTYPE', '/', '--target') or '').split()
    if len(found) != 2:
        raise RuntimeError('Setup cannot tell which filesystem Linux runs from (findmnt / gave no UUID)')
    return found[0], found[1]


def system_free_gb():
    """The room left on the filesystem Linux runs from, in GB."""
    return shutil.disk_usage('/').free // 10**9


def on_one_drive_both(config, row):
    """Both roles placed on the drive in this row of the list (the media role chosen on
    the drive the apps were just given): each a folder of it."""
    device = whole(unlocked(row['device']))
    for key in ('data_ssd', 'media_hdd'):
        record = config['storage'].setdefault(key, {})
        record.clear()
        record.update(uuid=device['uuid'], fstype=device['fstype'], folder=SHARED_FOLDERS[key])


def shared_with(config):
    """(the shared drive's UUID, its filesystem type) when both roles are on one drive of
    its own; None otherwise."""
    storage = config.get('storage', {})
    records = [storage.get(key) if isinstance(storage.get(key), dict) else {} for key in ('data_ssd', 'media_hdd')]
    if all(on_one_drive(record.get('folder')) for record in records) and records[0].get('uuid') == records[1].get(
        'uuid'
    ):
        return records[0]['uuid'], records[0].get('fstype', '')
    return None


def on_system_disk(record, key):
    """The settings of a role just placed on the disk Linux runs from."""
    uuid, fstype = system_filesystem()
    record.clear()
    record.update(uuid=uuid, fstype=fstype, folder=SYSTEM_FOLDERS[key])


def validate_system_folder(uuid, fstype):
    """A role kept on the system disk names the filesystem Linux runs from now (a
    reinstalled Linux has another: setup stops instead of using a folder of it unasked)."""
    if system_filesystem() != (uuid, fstype):
        raise RuntimeError(
            f'The settings keep a part of NekoServer on the disk Linux runs from (UUID {uuid}), but Linux now runs '
            'from another filesystem: it was reinstalled, or the settings are from another PC. Take that drive\'s '
            'entry out of "storage" in my-config.json and run setup again to choose where it goes. Nothing was changed.'
        )


def bind_line(folder, mountpoint):
    """The fstab line that mounts a folder of the system disk, or of the drive both roles
    share (once that drive is mounted), at a role's place."""
    if any(c.isspace() for c in mountpoint):
        raise RuntimeError('Mountpoint cannot contain whitespace')
    if on_one_drive(folder):
        return f'{folder} {mountpoint} none bind,x-systemd.requires-mounts-for={ONE_DRIVE} 0 0'
    return f'{folder} {mountpoint} none bind 0 0'


def own_bind_line(found):
    if len(found) != 6 or found[2] != 'none' or found[4:] != ['0', '0']:
        return False
    if found[0] in SYSTEM_FOLDERS.values():
        return found[3] == 'bind'
    return on_one_drive(found[0]) and found[3] == f'bind,x-systemd.requires-mounts-for={ONE_DRIVE}'


def bind_line_index(lines, mountpoint, entry, connected=None, notes=None):
    """The index of the fstab line to replace with `entry` (a role's folder on the system
    disk), None when its place has no line yet. NekoServer's own line there is replaced:
    its bind line, or its line for a drive that is not connected any more (the drive given
    up for the system disk). Any other line for the place stops setup."""
    connected = connected or drive_connected
    folder = fstab_folder(mountpoint)
    matches = [
        (i, s.split())
        for i, s in enumerate(lines)
        if s.strip() and not s.lstrip().startswith('#') and len(s.split()) >= 2 and fstab_folder(s.split()[1]) == folder
    ]
    if not matches:
        return None
    index, found = matches[0]
    if len(matches) == 1 and own_bind_line(found):
        return index
    if len(matches) == 1 and own_line(found) and not connected(found[0][len(BY_UUID_WORD) :]):
        if notes is not None:
            notes.append(
                f'{mountpoint}: the earlier drive ({found[0]}) is not connected; its line in /etc/fstab '
                f'is replaced by one for the folder {entry.split()[0]} on the disk Linux runs from'
            )
        return index
    theirs = '\n'.join('    ' + lines[i].strip() for i, _ in matches)
    raise RuntimeError(
        f'/etc/fstab already has a line for {mountpoint}, which NekoServer did not write for the folder '
        f'{entry.split()[0]}:\n{theirs}\nTake that line out of /etc/fstab, unmount what is mounted there '
        '(sudo umount <folder>), and run  sudo bash install.sh  again.'
    )


def shared_folder(record, key):
    """The address of the folder a NAS shares over NFS that this drive's saved record names
    (nfs_exports.py), or None for a drive that is a disk. Only the media drive may be one:
    the SSD holds the databases and setup's own records."""
    import nfs_exports

    export = nfs_exports.checked_record(record, f'"{key}" under "storage"')
    if export and key == 'data_ssd':
        raise RuntimeError(
            f'The SSD for the apps cannot be a folder shared over NFS ({export}): it holds the databases and '
            "setup's own records, and must be connected to this PC (by USB 3, or inside it). Take \"data_ssd\" out "
            'of "storage" in my-config.json and run  sudo bash install.sh  again. Nothing was changed.'
        )
    return export


def shared_folders(config):
    """{identity: address} of the drives in the settings that are folders shared over NFS."""
    found = {}
    for key, record in config.get('storage', {}).items():
        export = shared_folder(record, key) if isinstance(record, dict) else None
        if export:
            found[record['uuid']] = export
    return found


def choose_new(config, table, key, input_fn, out_fn):
    """The drive for `key`, asked for in the list of everything connected (drive_table.py):
    by its number there (nothing is typed back; the choice is said in words). A drive that cannot have the
    role (the disk Linux runs from, one in use elsewhere, the USB backup drive, a disk on
    the NAS for the app SSD, one too small) is in the list with the reason, and refused."""
    import drive_table

    if not drive_table.usable(table, key, config):
        raise RuntimeError(drive_table.none_usable(table, key, config))
    drive_table.show_once(table, config, out_fn)
    return drive_table.ask(table, key, config, input_fn, out_fn)


def not_offered(inventory, uuid):
    """Why a connected filesystem with this UUID is not one setup offers (a loop device, a
    filesystem Linux apps can't use, the system disk), or None when none is connected."""

    def walk(dev, system):
        here = system or holds_system(dev)
        if dev.get('uuid') == uuid:
            return why_not(dev, here)
        return next((found for child in dev.get('children', []) if (found := walk(child, here))), None)

    return next((found for dev in inventory.get('blockdevices', []) if (found := walk(dev, False))), None)


def why_not(dev, system):
    """The reason one lsblk row is not offered as a drive."""
    name, kind, fs = DEV + str(dev.get('name')), dev.get('type', ''), dev.get('fstype')
    if system:
        return f'{name} is on the disk Linux runs from, which NekoServer never uses for your files'
    if kind not in VOLUME_KINDS:
        what = 'a loop device (a file attached as a disk)' if kind == 'loop' else f'a "{kind}" device'
        return (
            f'{name} is {what}, which NekoServer does not use for its drives: they must be real drives (or their '
            'partitions, LVM volumes, RAID arrays or encrypted drives)'
        )
    if fs not in ('ext4', 'xfs', 'btrfs'):
        return f'{name} is formatted {NOT_LINUX.get(fs, fs or "with nothing yet")}, not ext4, XFS or Btrfs'
    return None


def saved_choice(
    record, uuid, items, key, label, apply, inventory=None, config=None, interactive=False, out_fn=print, table=()
):
    """A drive already in the settings: it must be connected, of the same type and, when
    setup goes on to mount it, unlocked by Linux at every start as a newly chosen one must
    be (settings reused after a reinstall may name an encrypted drive opened by hand).
    Returns its UUID, or '' when setup asks for the drive again: it is too small, or it
    is not connected (replaced, gate run C10) and someone is at the keyboard."""
    matches = [d for d in items if d['uuid'] == uuid]
    if len(matches) > 1:
        raise RuntimeError(f'Two connected drives have the UUID {uuid} of {label}. Disconnect one and run setup again.')
    if not matches:
        asking = apply and interactive
        said = gone_said(key, uuid, not_offered(inventory or {}, uuid), asking)
        if not asking:
            raise RuntimeError(said)
        out_fn(said)
        record.pop('uuid', None)
        record.pop('fstype', None)
        return ''
    if apply and too_small(matches[0], key, label, config or {}, interactive, out_fn):
        record.pop('uuid', None)
        record.pop('fstype', None)
        return ''
    if record.get('fstype') and record['fstype'] != matches[0]['fstype']:
        raise RuntimeError(f'Filesystem type mismatch for {key}')
    if apply:
        unlocked(matches[0])
        local_ssd(key, matches[0])
        same_nas_disk(record, matches[0], label)
        said_last_time(table, uuid, key, out_fn)
    record['fstype'] = matches[0]['fstype']
    return uuid


HELD = {'data_ssd': 'the apps', 'media_hdd': 'the media files and the backups'}


def gone_said(key, uuid, reason, asking):
    """A saved drive that setup cannot use now, in plain words: the drive that held the
    apps is not connected (or is connected but cannot be used, `reason`), and what to do:
    choose the new one now when setup asks (`asking`), else in a terminal. Nothing is
    formatted either way. "(UUID ...) is not connected" is what known_fixes.py knows."""
    drive = f'The drive that held {HELD.get(key, "your files")} (UUID {uuid})'
    if asking:
        unplugged, choose = 'stop here (Ctrl+C), connect it', 'choose the new one below'
    else:
        unplugged = 'connect it'
        choose = 'run  sudo bash install.sh  in a terminal on the server and choose the new one'
    if reason:
        return f'{drive} is connected, but {reason}. To use another one, {choose}. Nothing is formatted.'
    return (
        f'{drive} is not connected. If it is only unplugged, {unplugged} and run setup again. '
        f'If you replaced it, {choose}. Nothing is formatted.'
    )


def said_last_time(table, uuid, key, out_fn):
    """A rerun says which drive it uses again: as the list names it, with its UUID."""
    import drive_table

    row = drive_table.saved_row(table, uuid)
    if row:
        out_fn(drive_table.last_time(row, key))


def same_nas_disk(record, device, label):
    """A drive saved as being on the NAS is still that NAS disk (the same WWN, when the NAS
    reports one), and a drive on the NAS is noted as one (chosen before 0.46.0)."""
    saved = record.get('network')
    if device.get('network'):
        wwn = nas_disk_wwn(
            (saved or {}).get('wwn'),
            device.get('wwn'),
            label.removeprefix('the '),
            'Check on the NAS which LUN is which, then run setup again.',
        )
        record['network'] = {'transport': 'iscsi', 'wwn': wwn}
    elif saved:
        record.pop('network', None)


def too_small(device, key, label, config, interactive, out_fn):
    """A saved drive smaller than the drive needs (saved by a release that checked only
    after mounting it): True when setup asks for the drive again. A server already set up
    on it is left as it is (the drive space step says how full it is), and so is one the
    settings mount already: choosing another then means taking that one out first, which
    the message says how to do."""
    smallest, _ = sizes(key)
    folder = config.get('data_mount' if key == 'data_ssd' else 'media_mount', '')
    if big_enough(device, smallest) or (Path(str(config.get('data_mount', ''))) / 'docker-engine').is_dir():
        return False
    import drive_table

    counted = f" ({device['size_note']})" if device.get('size_note') else ''
    said = (
        f'{drive_table.said(device, first=True)}, the drive saved as {drive_table.role(key)}, is too small{counted}: '
        f'it needs a drive of at least {drive_needed(smallest)} GB ({smallest} GB free once formatted).'
    )
    if device.get('mountpoint') and device['mountpoint'] == folder:
        raise RuntimeError(
            f'{said} To choose another, take it out first:  sudo umount {folder} , take the line with '
            f"UUID={device['uuid']} out of /etc/fstab (sudo nano /etc/fstab), then run  sudo bash install.sh  "
            'again in a terminal: it asks for the drive anew. Nothing on the drive is changed.'
        )
    if not interactive:
        raise RuntimeError(f'{said} Run  sudo bash install.sh  in a terminal to choose another one.')
    out_fn(f'{said} Choose {label} again:')
    return True


def own_disks(uuid, items, taken, apply):
    """The physical disks the chosen filesystem is on (all of them, for a pool or an
    array), none of which the other drive may share."""
    physical = set(next(v['physical_disks'] for v in items if v['uuid'] == uuid))
    if physical & taken:
        raise RuntimeError('SSD and HDD must be on different physical disks')
    if not physical and apply:
        name = next((v['name'] for v in items if v['uuid'] == uuid), uuid)
        raise RuntimeError(
            f'Setup cannot tell which physical drive /dev/{name} is on (it sits on something Linux does not '
            'list as a disk, such as a loop device or a disk image), so it cannot make sure the SSD and the '
            'HDD are two different drives. Choose a partition of a real drive instead.'
        )
    return physical


def network_uuids(config):
    """The drives in the settings that are on a NAS (recorded when they were chosen)."""
    return frozenset(
        record['uuid']
        for record in config.get('storage', {}).values()
        if isinstance(record, dict) and record.get('network') and record.get('uuid')
    )


def role_spec(config, key, mount_key):
    """What to mount for one of the two roles, from the settings, checked: (the role, the
    UUID, the filesystem type, where it is mounted, its Btrfs subvolume or None)."""
    record = config.get('storage', {}).get(key, {})
    uuid = record.get('uuid', '').strip()
    fstype = record.get('fstype', '').strip()
    target = str(mount_path(config[mount_key]))
    if not uuid or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid):
        raise RuntimeError(f'storage.{key}.uuid must be explicitly configured from lsblk -f')
    if not shared_folder(record, key) and fstype not in ('ext4', 'xfs', 'btrfs'):
        raise RuntimeError(f'storage.{key}.fstype must be ext4, xfs or btrfs')
    if not Path(target).is_absolute() or str(Path(target)) in ('/', '/mnt', '/srv', '/home', '/var'):
        raise RuntimeError(f'Unsafe mount location: {target}')
    if Path(target).is_symlink():
        raise RuntimeError(f'Mount path must not be symlink: {target}')
    return key, uuid, fstype, target, subvolume_of(record)


def mount_specs(config):
    roles = [role_spec(config, key, mount_key) for key, mount_key in OLD_ROLES]
    if Path(roles[0][3]) in Path(roles[1][3]).parents or Path(roles[1][3]) in Path(roles[0][3]).parents:
        raise RuntimeError('Data and media mountpoints must not be nested')
    shared = shared_with(config)
    # One drive for both roles: the drive itself first, then the two folders of it.
    specs = [(ONE_DRIVE_KEY, shared[0], shared[1], ONE_DRIVE, None)] if shared else []
    specs += roles + extra_specs(config)
    # Two roles kept on the system disk are two folders of one filesystem: the same UUID,
    # told apart by their folders. Two drives of their own are two filesystems.
    drives = [s[1] for s in specs if s[3] not in system_folders(config)]
    if len(set(drives)) != len(drives) or len({s[3] for s in specs}) != len(specs):
        raise RuntimeError('Data and media require different UUIDs and mountpoints')
    return specs


def extra_specs(config):
    """The drives beyond the SSD and the media drive that something is placed on (0.50.0,
    "locations" naming a drive under "storage"): each mounted at its own folder under
    /mnt/nekoserver-drives, by the same rules as the two."""
    from neko_common import EXTRA_MOUNTS, drives_in_use, extra_drives

    specs = []
    for name in drives_in_use(config):
        record = extra_drives(config)[name]
        uuid, fstype = str(record.get('uuid', '')).strip(), str(record.get('fstype', '')).strip()
        if not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid):
            raise RuntimeError(f'storage.{name}.uuid must be explicitly configured from lsblk -f')
        if not shared_folder(record, name) and fstype not in ('ext4', 'xfs', 'btrfs'):
            raise RuntimeError(f'storage.{name}.fstype must be ext4, xfs or btrfs')
        specs.append((name, uuid, fstype, str(EXTRA_MOUNTS / name), subvolume_of(record)))
    return specs


def subvolume_of(record):
    """The Btrfs subvolume a drive is used with ("/" + path inside the filesystem, from
    `storage.<drive>.subvolume`), or None for the filesystem's default (and other types)."""
    value = record.get('subvolume')
    if value in (None, '', '/'):
        return None
    if record.get('folder'):
        # A role on the system disk: the folder as findmnt shows it, learned when it was mounted.
        if not isinstance(value, str) or not value.startswith('/') or any(c.isspace() for c in value):
            raise RuntimeError(
                'storage subvolume must be the folder findmnt shows for the role, such as /var/lib/nekoserver/apps'
            )
        return value.rstrip('/')
    if (
        record.get('fstype') != 'btrfs'
        or not isinstance(value, str)
        or not value.startswith('/')
        or '..' in value.split('/')
        or any(c.isspace() or c == ',' for c in value)
    ):
        raise RuntimeError('storage subvolume must be a Btrfs subvolume path such as /@data')
    return value.rstrip('/')


def mounted_subvolume(target, run=subprocess.run):
    """The subvolume a Btrfs drive is mounted with at `target` ("/@data"), or None when
    nothing is mounted there or it is the filesystem's top level."""
    source = findmnt('SOURCE', target, run=run) or ''
    if '[' not in source:
        return None
    inside = source.split('[', 1)[1].rstrip(']')
    return inside.rstrip('/') or None


def remember_subvolumes(config, run=subprocess.run):
    """A Btrfs drive that is already mounted in a subvolume other than the default keeps
    that subvolume: fstab is written with it, so a restart mounts the same tree, not the
    filesystem's default one (whose files the apps would find missing)."""
    for key, mount_key in [('data_ssd', 'data_mount'), ('media_hdd', 'media_mount')]:
        record = config.get('storage', {}).get(key, {})
        if record.get('subvolume') or (record.get('fstype') != 'btrfs' and not record.get('folder')):
            continue
        found = mounted_subvolume(config[mount_key], run)
        if found:
            record['subvolume'] = found


def validate_device(uuid, fstype):
    source = Path(BY_UUID + uuid)
    if not source.exists():
        raise RuntimeError(f'Drive UUID {uuid} not found; connect the disk and use lsblk -f')
    actual_type = call(['blkid', '-s', 'TYPE', '-o', 'value', str(source)])
    if actual_type != fstype:
        raise RuntimeError(f'Filesystem mismatch for {uuid}: actual={actual_type}, configured={fstype}')
    real = source.resolve()
    host_source = (findmnt('SOURCE', '/', '--target') or '').split('[')[0]
    host_block = Path(host_source)
    # Refuse using the exact OS filesystem even when identified by alternate device path,
    # or by its UUID (by-uuid may name another device of a Btrfs system pool).
    if (host_block.exists() and host_block.resolve() == real) or findmnt('UUID', '/', '--target') == uuid:
        raise RuntimeError(f'UUID {uuid} belongs to the OS filesystem')
    return str(source)


# A drive on a NAS (iSCSI, network_storage.py) is mounted once the network
# is up and this PC has logged in to the NAS (_netdev, and the login service required);
# an ext4 one that loses the NAS long enough to fail writes turns read-only rather than
# carry on half-written (errors=remount-ro), so nothing writes past a fault unseen.
NETWORK_OPTIONS = ',_netdev,x-systemd.requires={unit}'
NETWORK_EXT4 = ',errors=remount-ro'


def login_unit():
    """The service that logs this PC in to the NAS at every start, on this Linux."""
    import host

    return host.service('iscsi-login')


def disk_of(uuid, sys_class=Path('/sys/class/block')):
    """The disk (sdc) the filesystem with this UUID is on: itself, or the disk its
    partition belongs to. None when it is not connected."""
    try:
        name = Path(os.path.realpath(BY_UUID + uuid)).name
    except OSError:
        return None
    entry = Path(sys_class) / name
    if not entry.exists():
        return None
    return Path(os.path.realpath(entry)).parent.name if (entry / 'partition').exists() else name


def on_network(uuid):
    """Where the filesystem with this UUID comes from when it is on a NAS's iSCSI disk
    ({'portal', 'iqn', 'lun'}), else None."""
    from modules.network_storage import where

    disk = disk_of(uuid)
    return where(disk) if disk else None


# How long a drive may take to appear at boot before its mount is given up. Large hard
# drives, in USB enclosures or spinning up one after another, can take half a minute;
# 15 s (before 0.28.7) could leave the HDD unmounted, and Docker and every app with it.
# `nofail` keeps the rest of the boot from waiting for it.
MOUNT_OPTIONS = 'defaults,nofail,x-systemd.device-timeout=90s'
# Lines NekoServer wrote before, upgraded in place.
OLDER_OPTIONS = ('defaults,nofail,x-systemd.device-timeout=15s',)


def fstab_line(uuid, fstype, mountpoint, subvolume=None, network=None):
    """The line that mounts a drive at every start. `network`: the drive is on a NAS
    (None: found out from the drive itself, on_network)."""
    if any(c.isspace() for c in mountpoint):
        raise RuntimeError('Mountpoint cannot contain whitespace')
    if network is None:
        network = on_network(uuid) is not None
    options = MOUNT_OPTIONS
    if network:
        options += NETWORK_OPTIONS.format(unit=login_unit()) + (NETWORK_EXT4 if fstype == 'ext4' else '')
    options += f',subvol={subvolume}' if subvolume else ''
    return f'UUID={uuid} {mountpoint} {fstype} {options} 0 2'


def uuid_of(source):
    """The filesystem UUID an fstab source names (LABEL=, PARTUUID=, PARTLABEL=, a /dev
    path), or None when it can't be found (a drive not connected)."""
    try:
        device = source if source.startswith(DEV) else call(['findfs', source])
        return call(['blkid', '-s', 'UUID', '-o', 'value', device]) or None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def fstab_folder(field):
    """An fstab mount folder as a plain path (\\040 is a space; /srv/x/ is /srv/x)."""
    return os.path.normpath(field.replace('\\040', ' '))


def same_drive(source, uuid, resolve=uuid_of):
    """An fstab source that names this drive, however it is written."""
    plain = source.replace('"', '')
    if plain in (f'UUID={uuid}', BY_UUID + uuid):
        return True
    if plain.startswith(BY_UUID_WORD):
        return False
    return plain.startswith(('LABEL=', 'PARTUUID=', 'PARTLABEL=', DEV)) and resolve(plain) == uuid


def drive_connected(uuid):
    """A drive with this filesystem UUID is connected to this PC."""
    return os.path.exists(BY_UUID + uuid)


def own_line(found):
    """The fields are a line NekoServer wrote (UUID=, the folder, a type, its own options,
    then 0 2), for whatever drive."""
    options = found[3].split(',') if len(found) == 6 else []
    added = ('subvol=', '_netdev', 'x-systemd.requires=', NETWORK_EXT4.lstrip(','))
    base = ','.join(o for o in options if not o.startswith(added))
    return found[0].startswith(BY_UUID_WORD) and found[4:] == ['0', '2'] and base in (MOUNT_OPTIONS, *OLDER_OPTIONS)


def existing_line(lines, uuid, mountpoint, entry, resolve=uuid_of, connected=drive_connected, notes=None):
    """The index of the fstab line already mounting this drive or folder, None when there
    is none. One NekoServer wrote with older options may be brought up to date, and one
    NekoServer wrote for a drive that is not connected any more (the SSD or the HDD
    replaced after it broke) is replaced, said through `notes`; any other line for the
    same drive or folder (however its drive or folder is written) stops setup: two lines
    for one folder would mount whichever comes first."""
    wanted = entry.split()
    folder = fstab_folder(mountpoint)
    matches = [
        (i, s.split())
        for i, s in enumerate(lines)
        if s.strip()
        and not s.lstrip().startswith('#')
        and len(s.split()) >= 2
        and (fstab_folder(s.split()[1]) == folder or same_drive(s.split()[0], uuid, resolve))
    ]
    if not matches:
        return None
    index, found = matches[0]
    if (
        len(matches) == 1
        and own_line(found)
        and fstab_folder(found[1]) == folder
        and found[0] != wanted[0]
        and not connected(found[0][len(BY_UUID_WORD) :])
    ):
        if notes is not None:
            notes.append(
                f'{mountpoint}: the earlier drive ({found[0]}) is not connected; its line in /etc/fstab '
                f'is replaced by one for the new drive (UUID {uuid})'
            )
        return index
    # Older options are upgraded; with a subvolume the line must carry the same one. A
    # line of NekoServer's without one mounted the filesystem's default tree: once that
    # tree is known (and saved as the subvolume), the line is pinned to it.
    older = tuple(o + wanted[3][len(MOUNT_OPTIONS) :] for o in OLDER_OPTIONS)
    default = (MOUNT_OPTIONS, *OLDER_OPTIONS) if wanted[3] != MOUNT_OPTIONS else ()

    # One of NekoServer's own lines for this drive, with or without the NAS options (the
    # drive moved to or from a NAS), and the same subvolume or none yet: brought up to date.
    def subvolume(options):
        return next((o for o in options.split(',') if o.startswith('subvol=')), None)

    ours = own_line(found) and subvolume(found[3]) in (subvolume(wanted[3]), None)
    if (
        len(matches) != 1
        or found[:3] != wanted[:3]
        or len(found) < 4
        or not (found[3] in (wanted[3], *older, *default) or ours)
    ):
        theirs = '\n'.join('    ' + lines[i].strip() for i, _ in matches)
        raise RuntimeError(
            f'/etc/fstab already has a line for this drive (UUID {uuid}) or for {mountpoint}, '
            f'which NekoServer did not write:\n{theirs}\nNekoServer mounts the drive itself at '
            f'{mountpoint} (with options that let the PC start even when the drive is missing). '
            f'Take that line out of /etc/fstab, unmount the drive if it is mounted there '
            f'(sudo umount <folder>), and run  sudo bash install.sh  again.'
        )
    return index


def keep_first(path, backup):
    """The fstab as it was before NekoServer, kept whole: written aside, then published
    under its name (never over one), so a power cut can't leave a cut-off copy behind
    that would then be kept for good."""
    temp = safe_path(backup.with_name(backup.name + '.part'))
    temp.unlink(missing_ok=True)
    with temp.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temp, backup)
    finally:
        temp.unlink()
    directory = os.open(backup.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def waits_for_first_use(target):
    """Only systemd's automount holds this folder: the shared folder behind it (NFS) is
    mounted the first time something uses it, and is not mounted now."""
    return findmnt('FSTYPE', target) == 'autofs'


def open_shared_folder(target):
    """The first use of a shared folder since the start: systemd mounts it now (with its
    line in /etc/fstab), or says within half a minute that the NAS does not answer."""
    try:
        os.listdir(target)
    except OSError as exc:
        raise RuntimeError(
            f'The shared folder for {target} could not be mounted ({exc.strerror}): check that the NAS is on '
            'and still shares it with this PC, then run setup again'
        ) from None


def shared_folder_line(lines, mountpoint, entry, connected=drive_connected, notes=None):
    """The index of the fstab line to replace with `entry`, the line of a shared folder
    (NFS) for `mountpoint`; None when the folder has no line yet. A line NekoServer wrote
    there is brought up to date: its own for a shared folder (another address, other
    options), or its own for a drive that is not connected any more (the HDD given up
    for the NAS). Any other line for the folder stops setup, as for a drive."""
    import nfs_exports

    folder = fstab_folder(mountpoint)
    matches = [
        (i, s.split())
        for i, s in enumerate(lines)
        if s.strip() and not s.lstrip().startswith('#') and len(s.split()) >= 2 and fstab_folder(s.split()[1]) == folder
    ]
    if not matches:
        return None
    index, found = matches[0]
    if len(matches) == 1 and nfs_exports.own_line(found):
        return index
    if len(matches) == 1 and own_line(found) and not connected(found[0][len(BY_UUID_WORD) :]):
        if notes is not None:
            notes.append(
                f'{mountpoint}: the earlier drive ({found[0]}) is not connected; its line in /etc/fstab '
                f'is replaced by one for the shared folder {entry.split()[0]}'
            )
        return index
    theirs = '\n'.join('    ' + lines[i].strip() for i, _ in matches)
    raise RuntimeError(
        f'/etc/fstab already has a line for {mountpoint}, which NekoServer did not write for the shared folder '
        f'{entry.split()[0]}:\n{theirs}\nNekoServer mounts the shared folder itself at {mountpoint} (with options '
        'that let the PC start even when the NAS is off). Take that line out of /etc/fstab, unmount what is '
        'mounted there (sudo umount <folder>), and run  sudo bash install.sh  again.'
    )


def line_for(spec, lines, kinds, notes):
    """(the fstab line for one drive to mount, the index of the line of `lines` it takes
    the place of or None): a folder of the disk Linux runs from, a folder a NAS shares,
    or a drive by its UUID. `kinds`: (the UUIDs on a NAS, {identity: address} of the
    shared folders, {mountpoint: folder} of the roles on the system disk)."""
    import nfs_exports

    _, uuid, fstype, mountpoint, subvolume = spec
    network, exports, folders = kinds
    if mountpoint in folders:
        entry = bind_line(folders[mountpoint], mountpoint)
        return entry, bind_line_index(lines, mountpoint, entry, notes=notes)
    if uuid in exports:
        entry = nfs_exports.fstab_line(exports[uuid], mountpoint, fstype)
        return entry, shared_folder_line(lines, mountpoint, entry, notes=notes)
    entry = fstab_line(uuid, fstype, mountpoint, subvolume, network=uuid in network)
    return entry, existing_line(lines, uuid, mountpoint, entry, notes=notes)


def install_fstab(specs, path=FSTAB, log=None, network=frozenset(), exports=None, folders=None):
    """Add (or bring up to date) the lines that mount the SSD and HDD at boot: NekoServer's
    own line with older options, or for a drive that is gone (replaced), is rewritten. A
    line for the same drive or folder that NekoServer didn't write stops setup instead.
    `network`: the UUIDs of the drives on a NAS, mounted once it can be reached;
    `exports`: {identity: address} of the drives that are folders a NAS shares (NFS);
    `folders`: {mountpoint: folder} of the roles kept on the disk Linux runs from."""
    exports = exports or {}
    folders = folders or {}
    path = safe_path(path)
    existing = path.read_text() if path.exists() else ''
    lines = existing.splitlines()
    additions, upgraded, notes = [], False, []
    for spec in specs:
        entry, index = line_for(spec, lines, (network, exports, folders), notes)
        if index is None:
            additions.append(entry)
        elif lines[index].split() != entry.split():
            lines[index] = entry
            upgraded = True
    for note in notes if log else ():
        log(note)
    if not additions and not upgraded:
        return False
    backup = safe_path(path.with_name(path.name + '.nekoserver.bak'))
    # The first backup is the fstab as it was before NekoServer: kept, never replaced.
    if path.exists() and not backup.exists():
        keep_first(path, backup)
    payload = '\n'.join(lines).rstrip('\n') + '\n' + ''.join(a + '\n' for a in additions)
    atomic_write(path, payload, 0o644)
    return True


# Not drives anyone plugs in: snap images and other files attached as disks, swap in
# memory, RAM disks, network block devices, CD drives.
NOT_DRIVES = re.compile(r'(loop|zram|ram|nbd|sr)\d')


def disk_line(inventory):
    """One line naming this PC's drives (for the log), without loop devices, swap in
    memory and anything under 1 GB, which only push the drive question off the screen."""
    found = [
        f"{d.get('name')} {size_words(d.get('size'))}"
        + (f" {str(d['model']).strip()}" if d.get('model') else '')
        + (' (Linux runs from it)' if holds_system(d) else '')
        for d in inventory.get('blockdevices', [])
        if not NOT_DRIVES.match(str(d.get('name', ''))) and (size_bytes(d.get('size')) or 0) >= GIB
    ]
    return 'Drives on this PC: ' + ('; '.join(found) if found else 'none found')


def run(context):
    inventory = inventory_now()
    context.log(disk_line(inventory))
    if not context.apply:
        resolve_storage_identity(context.config, inventory, apply=False, out_fn=context.log)
        context.log('Read-only inspection; no disk changes.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Mount configuration requires root')
    for tool in ('findmnt', 'blkid', 'mount', 'systemctl'):
        if not shutil.which(tool):
            raise RuntimeError(f'Required tool missing: {tool}')
    # Before the drives: what goes on them depends on it (guided_setup.choose_media_apps).
    choose_media_apps(context)
    # Every drive question of this step is one page in the setup window (storage_page).
    with storage_page(context, inventory):
        place_drives(context, inventory)
    choose_backup_times(context)


def place_drives(context, inventory):
    """The drives chosen, checked and mounted, then where each app keeps your files and
    the drive of the weekly offline copy."""
    interactive = sys.stdin.isatty()
    resolve_storage_identity(context.config, inventory, apply=True, interactive=interactive, out_fn=context.log)
    remember_subvolumes(context.config)
    installation_drives_connected(context.config, asked=lost_drive_asked if interactive else None)
    specs = mount_specs(context.config)
    checked_before_mount(context.config, specs)
    # Saved for unattended reruns only once the choice passed these checks: a wrong pick
    # is asked again on the next run instead of being kept.
    save_storage(context)
    mount_all(context, specs)
    remember_media_apps(context)
    before = drives_in_use(context.config)
    choose_locations(context, inventory=inventory)
    settle_installs(context)
    if drives_in_use(context.config) != before:
        # A drive of its own chosen for an app's files: checked, saved and mounted like the two.
        specs = mount_specs(context.config)
        checked_before_mount(context.config, specs)
        save_storage(context)
        mount_all(context, specs)
    mount_installed(context)
    choose_usb(context)
    existing_media_asked(context)


# ---- an app's settings on another drive (0.50.0) -----------------------------------------
# "install" in the settings places a group's apps on a drive of their own: each app's
# settings folder there (<drive>/nekoserver/appdata/<app>) is mounted at its usual place
# (<apps' drive>/appdata/<app>), so every path stays what it is. The folder underneath is
# made unwritable: with the drive away, the app finds a folder it cannot write to
# instead of starting over empty on the apps' drive.
INSTALL_BEGIN = '# NekoServer: apps installed on another drive (written by setup from my-config.json)'
INSTALL_END = '# NekoServer: end of apps installed on another drive'


def installed_binds(config):
    """[(app, its folder on its drive, its usual place, where that drive is mounted)]."""
    import placement
    from neko_common import drive_folder

    data = mount_path(config['data_mount'])
    binds = []
    for app, drive in sorted(placement.apps_elsewhere(config).items()):
        mounted = Path(drive_folder(config, drive))
        binds.append((app, mounted / 'nekoserver' / 'appdata' / app, data / 'appdata' / app, mounted))
    return binds


def installed_line(source, target, drive, data):
    """The fstab line that mounts an app's folder on its drive at its usual place, once
    both drives are there; a drive that is away never stops the PC from starting."""
    if any(c.isspace() for c in f'{source}{target}{drive}{data}'):
        raise RuntimeError('A folder of an app installed on another drive cannot contain spaces')
    wait = f'x-systemd.requires-mounts-for={drive},x-systemd.requires-mounts-for={data}'
    return f'{source} {target} none bind,nofail,{wait} 0 0'


def with_block(text, begin, end, lines):
    """The fstab text with NekoServer's block between `begin` and `end` holding `lines`
    (no block at all with none); everything outside it is as it was."""
    kept, inside = [], False
    for line in text.splitlines():
        if line.strip() == begin:
            inside = True
        elif line.strip() == end:
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise RuntimeError(f'/etc/fstab has "{begin}" without its end line; look at the file before setup changes it')
    body = '\n'.join(kept).rstrip('\n') + '\n'
    return body + ''.join(line + '\n' for line in ([begin, *lines, end] if lines else []))


def unwritable(folder, run=subprocess.run):
    """The folder under a mount, closed to everyone: nothing is written there while the
    drive is away. Immutable where the filesystem can; else nobody's to open."""
    try:
        done = run(['chattr', '+i', str(folder)], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or done.returncode:
        os.chown(folder, 0, 0, follow_symlinks=False)
        os.chmod(folder, 0o000)


def on_its_drive(target, drive):
    """The app's usual place is the mount of its folder on that drive."""
    try:
        return os.path.ismount(target) and os.stat(target).st_dev == os.stat(drive).st_dev
    except OSError:
        return False


def block_targets(text, begin, end):
    """The folders the lines of setup's block in /etc/fstab mount."""
    found, inside = [], False
    for line in text.splitlines():
        if line.strip() == begin:
            inside = True
        elif line.strip() == end:
            inside = False
        elif inside and len(line.split()) >= 2:
            found.append(line.split()[1])
    return found


GIVEN_UP_MOUNTED = (
    '{target} holds the settings of an app that is installed on another drive, and the settings no longer '
    'place it there. Setup does not move an app that is set up back to the drive of NekoServer: its settings and '
    'database would have to be copied with the app stopped. Put its group back under "install" in my-config.json '
    'and run setup again. Nothing was changed.'
)


def given_up(text, binds, log, run=subprocess.run):
    """The apps the settings placed on another drive at an earlier run and no longer do.
    With the folder still mounted from that drive the app is set up there: setup stops
    and says why. With the drive gone the closed folder underneath is opened again, so
    the app can be set up on the apps' drive; its settings come back from a backup."""
    wanted = {str(target) for _, _source, target, _drive in binds}
    for target in block_targets(text, INSTALL_BEGIN, INSTALL_END):
        if target in wanted:
            continue
        if os.path.ismount(target):
            raise RuntimeError(GIVEN_UP_MOUNTED.format(target=target))
        folder = safe_path(Path(target))
        if folder.is_dir():
            run(['chattr', '-i', str(folder)], capture_output=True, text=True, timeout=30, check=False)
            # Its owner and group only: the media step hands an empty folder of root's to the
            # app's user, as it makes new app folders (0o750).
            os.chmod(folder, 0o750)
            log(
                f'{target}: the drive this app was installed on is no longer in the settings, so the app is set up '
                'on the drive of NekoServer with empty settings. To bring its settings back from a backup, run  '
                'sudo bash install.sh --recover  (RECOVERY.md says how).'
            )


LOST_DRIVE = (
    'The drive that {apps} are installed on is not connected (UUID {uuid}). If it is only unplugged, connect it and '
    'run setup again. If it is lost, run  sudo bash install.sh  in a terminal on the server: it asks, and sets these '
    'apps up again on the drive of NekoServer with empty settings; bring their settings back from a backup with  '
    'sudo bash install.sh --recover  (it restores into a new folder, never over an app; RECOVERY.md says how to put '
    'them in place). Nothing was changed.'
)


def installation_drives_connected(config, by_uuid=None, asked=None):
    """Every drive a group's apps are installed on is connected. A drive that is not:
    `asked` (someone at the keyboard) says whether it is lost, and a lost one is given up
    (its groups are set up again on the drive of NekoServer, their settings brought back
    from a backup by the recovery menu). Otherwise setup stops and says what to do."""
    import placement
    from neko_common import APPS

    for drive in sorted(set(placement.apps_elsewhere(config).values())):
        uuid = str(extra_drives(config)[drive].get('uuid', ''))
        if Path((by_uuid or BY_UUID) + uuid).exists():
            continue
        apps = ', '.join(APPS[app][0] for app, on in sorted(placement.apps_elsewhere(config).items()) if on == drive)
        if asked is None or not asked(apps, uuid):
            raise RuntimeError(LOST_DRIVE.format(apps=apps, uuid=uuid))
        give_up_drive(config, drive)


def give_up_drive(config, drive):
    """A lost drive taken out of the settings: the groups installed on it go back to the
    drive of NekoServer, and the drive is no longer one of the server's, unless files of
    an app are kept there too (then the step that mounts it says it is missing)."""
    install = config.get('install') if isinstance(config.get('install'), dict) else {}
    for row in [row for row, on in install.items() if on == drive]:
        install.pop(row)
    places = config.get('locations') if isinstance(config.get('locations'), dict) else {}
    if drive not in places.values():
        config.get('storage', {}).pop(drive, None)


# Said on the storage page for a drive of a group's own that is not connected (gate run
# C17: the person was told to edit my-config.json).
LOST_GROUP_DRIVE = (
    'The drive for {what} (UUID {uuid}) is not connected. If it is only unplugged, stop here (Ctrl+C), connect '
    'it and run setup again. If it was lost, choose the drive that replaces it below: {what} start again there '
    'with empty settings, unless you bring them back from a backup afterwards (sudo bash install.sh --recover, '
    '"App settings, from a nightly backup"). Nothing is formatted.'
)


def lost_placed_drives(config, by_uuid=None):
    """The drives beyond the SSD and the HDD that something is placed on (a group's files
    or apps) and that are not connected: {name: (what it holds, in words; the apps on it)}.
    A shared folder on a NAS is never "lost" here (its NAS may only be off)."""
    import install_state
    import placement

    found = {}
    for name, words in install_state.missing_group_drives(config, by_uuid or BY_UUID).items():
        uuid = str(extra_drives(config)[name].get('uuid', ''))
        apps = [app for row in sorted(rows_on(config, name)) for app in placement.apps_of(row)]
        found[name] = (words, uuid, list(dict.fromkeys(apps)))
    return found


def rows_on(config, name):
    """The rows of the placement table (photos, movies, music, cloud) whose files or apps
    the settings put on drive `name`."""
    found = set()
    for section in ('locations', 'install'):
        saved = config.get(section) if isinstance(config.get(section), dict) else {}
        found |= {row for row, drive in saved.items() if drive == name}
    return found


def give_up_placed(config, name):
    """A lost drive taken out of the settings everywhere it is named: "storage",
    "locations", "install" and the placement table, so the storage page asks where its
    groups go now and nothing is left naming it (gate run C17)."""
    import existing_page

    config.get('storage', {}).pop(name, None)
    for section in ('locations', 'install'):
        saved = config.get(section) if isinstance(config.get(section), dict) else {}
        for row in [row for row, drive in saved.items() if drive == name]:
            saved.pop(row)
    if isinstance(config.get('placement'), dict):
        # Worked out again from the drives left, the person's own folders carried over.
        existing_page.keep_table(config)


def lost_ahead(draft, chosen, log, by_uuid=None):
    """On the storage page: each lost drive of a group's own is said, and given up on the
    copy of the settings, so the page asks for the drive that replaces it among the drive
    list. Kept in `chosen` for the settings once the person approves (keep_ahead)."""
    for name, (words, uuid, apps) in lost_placed_drives(draft, by_uuid).items():
        log(LOST_GROUP_DRIVE.format(what=words, uuid=uuid))
        give_up_placed(draft, name)
        chosen.setdefault('lost', {})[name] = {'what': words, 'apps': apps}


def settings_start_empty(lost, run=subprocess.run):
    """After the approval: the apps of each lost drive's groups start again on the drive
    that replaces it with empty settings. Their containers are removed now (never their
    data), so the media step makes them again on the new drive instead of leaving them
    running on the lost drive's empty folder; the finishing page says so (drive_table.STARTED_EMPTY)."""
    import drive_table
    from neko_common import EXTRA_MOUNTS

    for name, said in lost.items():
        for app in said['apps']:
            ids = containers_of(app, run)
            if ids:
                run(['docker', 'rm', '-f', *ids], capture_output=True, text=True, timeout=120, check=False)
        drive_table.STARTED_EMPTY[name] = said
        # The lost drive's mount folder keeps its name (never given to a new drive), with
        # the empty folders Docker made in it while the drive was away taken out.
        if (EXTRA_MOUNTS / name).is_dir():
            clear_leftovers(EXTRA_MOUNTS / name)


def containers_of(app, run=subprocess.run):
    """The ids of the containers of one app (its Compose project neko-<app>)."""
    try:
        found = run(
            ['docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project=neko-{app}'],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return found.stdout.split() if found.returncode == 0 else []


def lost_drive_asked(apps, uuid):
    """The person's answer: True when the drive is lost and its apps are set up again."""
    import questions

    return questions.yes_no('install_drive_lost', apps=apps, uuid=uuid) is True


def mount_installed(context, fstab=FSTAB):
    """Every app installed on another drive has its folder there mounted at its usual
    place, now and at every start. An app whose settings are on the apps' drive already
    is never moved: setup stops and says so before anything is changed."""
    config = context.config
    binds = installed_binds(config)
    data = mount_path(config['data_mount'])
    for app, _source, target, drive in binds:
        if not on_its_drive(target, drive) and has_files(target):
            raise RuntimeError(
                f'{app} already keeps its settings on the drive of the apps ({target}), and the settings place it '
                'on another drive. Moving an app that is set up is a job of its own, with the app stopped; take its '
                'group out of "install" in my-config.json and run setup again. Nothing was changed.'
            )
    lines = [installed_line(source, target, drive, data) for _, source, target, drive in binds]
    fstab = safe_path(fstab)
    text = fstab.read_text() if fstab.exists() else ''
    given_up(text, binds, context.log)
    wanted = with_block(text, INSTALL_BEGIN, INSTALL_END, lines)
    if wanted != text and (lines or INSTALL_BEGIN in text):
        atomic_write(fstab, wanted, 0o644)
        call(['systemctl', 'daemon-reload'])
    for app, source, target, drive in binds:
        if on_its_drive(target, drive):
            continue
        safe_path(source).mkdir(parents=True, exist_ok=True)
        safe_path(target).mkdir(parents=True, exist_ok=True)
        unwritable(target)
        call(['mount', str(target)])
        if not on_its_drive(target, drive):
            raise RuntimeError(f'{target} is not the folder of {app} on its drive after mounting it')
        context.log(f'{app}: its settings are kept on the drive at {drive} ({source})')


def existing_media_asked(context):
    """Films and shows the person already has (existing_page.py): asked once, with
    someone at the keyboard; the answer is saved with the drives."""
    import existing_page

    if not sys.stdin.isatty():
        if existing_page.keep_table(context.config) and hasattr(context, 'persist_config'):
            context.persist_config()
        return
    if existing_page.ask(context, output=context.log) and hasattr(context, 'persist_config'):
        context.persist_config()


# ---- chosen before the approval (0.50.0) -------------------------------------------------
# Setup's storage page comes before the review and the one approval (wizard.py), when
# nothing of NekoServer is mounted yet. The choices are made on a copy of the settings.
# The drives of the roles, the offline drive and the folders of films already there go
# into the settings file once the person approves. Where each app keeps its files is kept
# for this run only (drive_table.PLACES_AHEAD): only the mounted drives show whether such
# files are there already, and those stay where they are (choose_locations).
AHEAD_SECTIONS = ('storage', 'photos', 'placement', 'app_backup', 'host_backup')
# Top-level answers asked before the approval too: the backups page's times and its counts.
AHEAD_KEYS = ('backup_times', 'backup_counts_reviewed')


def choose_ahead(config, log):
    """The storage page, asked before anything is installed. Returns {'settings': what
    it changes in the settings file (section_changes), 'places': where each app's files
    go, 'lines': the choices in words, for the review}; all empty when nothing is left to
    ask or nobody is at the keyboard. Nothing is mounted, written or saved here."""
    chosen = {'settings': {}, 'places': {}, 'lines': []}
    if not sys.stdin.isatty():
        return chosen
    draft = copy.deepcopy(config)
    import install_state

    # A saved drive that is not connected (replaced, gate run C10) is asked for here too.
    if page_fields(config) or install_state.missing_drives(config, BY_UUID) or lost_placed_drives(config):
        drives_ahead(draft, chosen, log)
    else:
        # The drives are known (a run after the first, an update): the films question,
        # new in 0.50.0, is still asked before the approval, never after (gate run D).
        existing_ahead(draft, log)
    # The backups page and the accounts page: asked before the approval too (the user:
    # after it, only what needs the running server asks).
    others_ahead(draft, log)
    chosen['settings'] = section_changes(config, draft)
    return chosen


def existing_ahead(draft, log):
    """The films, shows and music already there (existing_page), on the copy of the
    settings, when the storage page has nothing else to ask."""
    import existing_page

    context = types.SimpleNamespace(config=draft, log=log, apply=True, config_path=None)
    existing_page.ask(context, output=log)
    # The offline drive left out at an earlier run is offered again: "run setup again" is
    # the way to add one later (gate run B4); "none for now" again keeps it out.
    photos = draft.get('photos') if isinstance(draft.get('photos'), dict) else {}
    if photos.get(OFFLINE_WANTED) is False and not photos.get('external_backup_uuid'):
        photos.pop(OFFLINE_WANTED)
        choose_usb(context)


def others_ahead(draft, log):
    """The backups page (times and counts) and the accounts page, on the copy of the
    settings. The answers to optional questions are kept on the app drive: one that is not
    mounted yet gets them in a folder of its own, left behind once the run ends."""
    import tempfile

    import account_store
    import drive_table
    import guided_setup

    mounted = findmnt('SOURCE', str(mount_path(draft['data_mount']))) is not None
    with tempfile.TemporaryDirectory(prefix='neko-ahead-') as scratch:
        times = copy.deepcopy(draft)
        if not mounted:
            times['data_mount'] = scratch
        context = types.SimpleNamespace(config=times, log=log, apply=True, config_path=None, plan=None, previous=None)
        guided_setup.choose_backup_times(context)
        if hasattr(context, 'ask_optional_again'):
            guided_setup.OPTIONAL_AGAIN['again'] = context.ask_optional_again
        for key in AHEAD_KEYS:
            if key in times:
                draft[key] = times[key]
        for section in ('app_backup', 'host_backup'):
            if section in times:
                draft[section] = times[section]
    drive_table.TIMES_AHEAD.clear()
    drive_table.TIMES_AHEAD.update(asked=True)
    account_store.ask_ahead(draft, output=log)


def drives_ahead(draft, chosen, log):
    """The storage page on the copy of the settings: the drives, where each app keeps its
    files and is installed, the offline drive, and films already there."""
    import drive_table
    import existing_page

    context = types.SimpleNamespace(config=draft, log=log, apply=True, config_path=None)
    inventory = inventory_now()
    log(disk_line(inventory))
    lost_ahead(draft, chosen, log)
    with storage_page(context, inventory):
        resolve_storage_identity(draft, inventory, apply=True, interactive=True, out_fn=log)
        chosen['places'] = wished_places(context, inventory)
        installs = wished_installs(context, inventory)
        if installs:
            chosen['installs'] = installs
        # A drive just chosen for files or installs is not the offline copy too (gate run C).
        drive_table.TAKEN_AHEAD[:] = taken_rows(chosen['places'], installs)
        try:
            choose_usb(context)
        finally:
            drive_table.TAKEN_AHEAD.clear()
        existing_page.ask(context, output=log)
    chosen['lines'] = ahead_lines(draft, inventory, chosen['places'], chosen.get('installs') or {})


def taken_rows(places, installs):
    """The UUIDs of the drives the storage page chose for files or installs (rows of the
    drive list; 'ssd' and 'media' are not rows)."""
    rows = [*places.values(), *(installs or {}).values()]
    return [row['uuid'] for row in rows if isinstance(row, dict) and row.get('uuid')]


def keys_changed(old, new):
    """(the keys `new` sets or changes, the keys it no longer has) of one section; each a
    dictionary, anything else counting as empty."""
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    return {key: value for key, value in new.items() if old.get(key) != value}, [key for key in old if key not in new]


def section_changes(before, after):
    """What the storage page changed, section by section: {section: (the keys it set,
    the keys it took out)}, and the top-level answers of the films question as they are."""
    import existing_page

    found = {section: keys_changed(before.get(section), after.get(section)) for section in AHEAD_SECTIONS}
    changes = {section: change for section, change in found.items() if change[0] or change[1]}
    # The films question's two answers of their own (that it was asked, the folders to
    # copy), and the backups page's.
    for key in (existing_page.ASKED, existing_page.COPIES, *AHEAD_KEYS):
        if after.get(key) and after.get(key) != before.get(key):
            changes[key] = after[key]
    return changes


def keep_ahead(config, chosen):
    """The storage page's choices put into the settings as they are now (the first page's
    switches were saved meanwhile), and the places kept for the storage step of this run."""
    import drive_table

    for section, change in chosen['settings'].items():
        if not isinstance(change, tuple):
            config[section] = change
            continue
        added, gone = change
        kept = config.setdefault(section, {})
        kept.update(added)
        for key in gone:
            kept.pop(key, None)
    # A lost drive of a group's own, given up on the page: out of the settings everywhere,
    # and its groups' apps made again on the drive that replaces it.
    for name in chosen.get('lost') or {}:
        give_up_placed(config, name)
    settings_start_empty(chosen.get('lost') or {})
    drive_table.PLACES_AHEAD.clear()
    drive_table.PLACES_AHEAD.update(chosen['places'])
    drive_table.INSTALL_AHEAD.clear()
    drive_table.INSTALL_AHEAD.update(chosen.get('installs') or {})


def wished_places(context, inventory):
    """Where each app is to keep your files, asked before the drives are mounted:
    {'photos': 'ssd' | 'media' | a row of the drive list}. Files found on a drive that is
    open already are not asked about: they stay where they are."""
    import drive_table
    import setup_progress

    config = context.config
    asked = page_fields(config)
    table = drive_table.rows(inventory, config, discover_filesystems(inventory))
    room = room_ahead(config, table)
    on_nas = bool(config.get('storage', {}).get('media_hdd', {}).get('network'))
    others = data_drives(config, inventory)
    places = {}
    for what, words, threshold in WHERE:
        if what not in asked:
            continue
        if kept_location(config, what):
            setup_progress.page_answer(what)  # not a choice: said once the drives are mounted
            continue
        best = recommended(room['ssd'], threshold)
        places[what] = asked_location(context, words, best, room, on_nas, key=what, others=others)
    return places


# What a group's apps are called when the page asks where they are installed.
INSTALLED_NAMES = {
    'photos': 'Immich',
    'movies': 'Jellyfin and the apps with it',
    'music': 'Navidrome',
    'cloud': 'Nextcloud',
}
INSTALL_HERE = 'Recommended: same drive as NekoServer and your apps'


def install_drives(config, inventory):
    """The drives a group's apps may be installed on besides the apps' own: connected to
    this PC itself (never a NAS), with a filesystem Linux apps can use, and with
    no role of their own."""
    import drive_table

    return [row for row in data_drives(config, inventory) if not drive_table.drive_verdict(row, 'data_ssd', config)]


def asked_install(context, what, others):
    """The drive a group's apps keep their settings and databases on, asked: the apps'
    drive (Enter, and the answer when nothing else is said) or one of `others`, returned
    as its row of the drive list. None for the apps' drive."""
    import drive_table

    name = INSTALLED_NAMES[what]
    context.log(f'Where should {name} be installed (the settings and the database)?')
    context.log('  1) the same drive as NekoServer and your apps  (recommended)')
    for n, row in enumerate(others, 2):
        context.log(f'  {n}) a drive of its own: {drive_table.brief(row)}')
    for _ in range(3):
        answer = drive_table.field('install_' + what, input, 'Number (Enter for the recommended one): ')
        if answer in ('', '1'):
            return None
        if window_row(answer) is not None:
            # The window names the row of the list.
            named = next((row for row in others if str(row['number']) == window_row(answer)), None)
            if named is not None:
                return named
        elif answer.isdigit() and 2 <= int(answer) < 2 + len(others):
            return others[int(answer) - 2]
        context.log(f'Type a number from 1 to {1 + len(others)}, or press Enter.')
    return None


def wished_installs(context, inventory):
    """Where each group's apps are to be installed, asked before the drives are mounted:
    {'photos': a row of the drive list} for the groups given a drive of their own.
    Nothing is asked on a PC with no drive that could be one."""
    config = context.config
    others = install_drives(config, inventory)
    if not others:
        return {}
    installs = {}
    for what in installs_asked(config):
        row = asked_install(context, what, others)
        if row is not None:
            installs[what] = row
    return installs


# An installation drive for one group's apps (0.50.0, free placement) is not offered in
# 0.50.0: its gate (runs C9, C16) failed with the group's drive away, so by the rule set
# before the gate it moves to 0.51.x. The storage page offers one installation drive for
# all the apps together, as 0.49.0 did. A server whose settings name one ("install") keeps
# working with it; the code stays, only the offer is off.
OFFER_GROUP_INSTALLS = False


def installs_asked(config):
    """The groups whose installation drive the page asks for: the ones it asks a place
    for (their app is new to this server), unless their settings name a drive already.
    None while OFFER_GROUP_INSTALLS is off."""
    if not OFFER_GROUP_INSTALLS:
        return []
    asked = page_fields(config)
    named = config.get('install') if isinstance(config.get('install'), dict) else {}
    return [what for what, _words, _threshold in WHERE if what in asked and what not in named]


def settle_installs(context):
    """The installation drives chosen on the storage page, now that the apps' drive is
    mounted. A group with settings on the apps' drive already stays there, and that is
    said: an app that is set up is never moved. Any other is written to "install" in the
    settings, its drive listed under "storage"; the caller mounts it."""
    import drive_table
    import placement

    config = context.config
    data = mount_path(config['data_mount'])
    for what, row in drive_table.INSTALL_AHEAD.items():
        name = INSTALLED_NAMES[what]
        if any(has_files(data / 'appdata' / app) for app in placement.apps_of(what)):
            context.log(f'{name}: kept on the drive of NekoServer and your apps, where its settings already are')
            continue
        config.setdefault('install', {})[what] = placed_on(config, row)
        context.log(f'{name}: installed on {drive_table.brief(row)}, a drive of its own')
    drive_table.INSTALL_AHEAD.clear()


def room_ahead(config, table):
    """The room, in GB, on the drive of each role, whether it is open yet or not."""
    room = {}
    for name, key, mount_key in (('ssd', 'data_ssd', 'data_mount'), ('media', 'media_hdd', 'media_mount')):
        record = config.get('storage', {}).get(key) or {}
        if findmnt('SOURCE', str(mount_path(config[mount_key]))) is not None:
            room[name] = free_gb(config[mount_key])
        elif system_folder(record, key) and not on_one_drive(system_folder(record, key)):
            room[name] = system_free_gb()
        else:
            room[name] = free_on(
                next((row for row in table if row['uuid'] and row['uuid'] == record.get('uuid')), None)
            )
    return room


def free_on(row):
    """The room on a drive of the list that is not open, from its size and how full it is."""
    if row is None:
        return 0
    return int((size_bytes(row['size']) or 0) * (100 - (row['full'] or 0)) / 100) // 10**9


PLACE_SAID = {'ssd': 'on the drive of NekoServer and your apps', 'media': 'on the drive of the backups'}


def place_line(config, what, words, place):
    """Where one kind of files is kept, as the review says it."""
    import drive_table

    if isinstance(place, str):
        said = PLACE_SAID[place]
    else:
        said = 'on ' + drive_table.brief(place) + ', a drive of their own'
    if place == 'media' and what in backed_up_beside(config):
        said += '. ' + SAME_DRIVE_AS_BACKUP
    return f'{words[0].upper()}{words[1:]}: {said}'


def own_folder_lines(config):
    """What happens to each folder of the person's own, as the review says it."""
    import existing_media
    import existing_page

    lines = []
    for entry in existing_media.extras(config):
        what = existing_media.WHAT[entry['holds']]
        if entry['use'] == existing_media.NEW:
            lines.append(f"New {what} are saved in {entry['path']}")
            continue
        new = ', and new ones are saved there' if entry['new_files'] else ''
        lines.append(f"Your {what} in {entry['path']}: used where they are{new}")
    for wanted in config.get(existing_page.COPIES) or []:
        what = existing_media.WHAT[wanted['holds']]
        lines.append(
            f"Your {what} in {wanted['path']}: copied into the NekoServer library; your folder is kept as it is"
        )
    return lines


def offline_line(config, named):
    """The offline backup's drive, or that there is none by choice; nothing when it is not chosen yet."""
    photos = config.get('photos', {})
    if photos.get('external_backup_uuid'):
        return ['Offline backup: ' + named(photos['external_backup_uuid'])]
    return ['Offline backup: none for now'] if photos.get(OFFLINE_WANTED) is False else []


def ahead_lines(config, inventory, places, installs=None):
    """The storage page's choices in words, for the review before the approval."""
    import drive_table

    table = drive_table.rows(inventory, config, discover_filesystems(inventory))
    by_uuid = {row['uuid']: row for row in table if row['uuid']}

    def named(uuid):
        return drive_table.brief(by_uuid[uuid]) if uuid in by_uuid else 'the drive chosen before'

    lines = []
    for key in ('data_ssd', 'media_hdd'):
        record = config.get('storage', {}).get(key) or {}
        folder = system_folder(record, key)
        where = 'the disk Linux runs from' if folder and not on_one_drive(folder) else named(record.get('uuid'))
        lines.append(f'{PAGE_ROLES[key]}: {where}')
    lines += [place_line(config, what, words, places[what]) for what, words, _ in WHERE if what in places]
    for what, row in (installs or {}).items():
        lines.append(f'{INSTALLED_NAMES[what]}: installed on {drive_table.brief(row)}, a drive of its own')
    return lines + offline_line(config, named) + own_folder_lines(config)


def checked_before_mount(config, specs):
    """Every drive to mount is the one the settings name, and nothing else is in its place."""
    exports = shared_folders(config)
    folders = system_folders(config)
    for _, uuid, fstype, target, _ in specs:
        if target in folders and not on_one_drive(folders[target]):
            validate_system_folder(uuid, fstype)
        elif uuid not in exports:
            validate_device(uuid, fstype)  # raises if the drive identity is wrong
        if waits_for_first_use(target):
            continue  # a shared folder not opened since the start: mount_all opens and checks it
        place_checked(target, uuid, fstype, target in folders)


def place_checked(target, uuid, fstype, a_folder):
    """What is at a drive's mount folder now is that drive (`a_folder`: a role kept in a
    folder of the disk Linux runs from), or nothing but empty folders left by Docker."""
    current = findmnt('SOURCE', target)
    if current is not None:
        mounted = current.split('[')[0]
        if mounted_filesystem(target) != (uuid, fstype):
            raise RuntimeError(f'Wrong filesystem already mounted at {target}: {mounted}')
        if not a_folder:
            only_a_folder(target, fstype, mounted_subvolume(target))
    elif Path(target).exists() and not clear_leftovers(Path(target)):
        raise RuntimeError(
            f'Refusing mount over nonempty directory {target}: there are files in that folder (on the system '
            'disk), and NekoServer never removes files. Move them elsewhere, or pick another drive, and run '
            'setup again'
        )


def clear_leftovers(folder):
    """The folder a drive is mounted at, with nothing mounted there: the empty folders
    left in it are removed (Docker made them while the drive was away, gate run C16).
    Nothing else is touched: no file, no link, nothing on another filesystem. True when
    the folder is empty now."""
    if os.path.ismount(folder) or folder.is_symlink():
        return False
    device = folder.lstat().st_dev
    for root, _dirs, files in os.walk(folder, topdown=False, followlinks=False):
        here = Path(root)
        if here == folder or files or here.lstat().st_dev != device or os.path.ismount(here):
            continue
        try:
            here.rmdir()  # an empty folder only: rmdir refuses anything else
        except OSError:
            continue
    return not any(folder.iterdir())


# false under "photos": no removable drive for the weekly offline copy, by choice (the storage page).
OFFLINE_WANTED = 'external_backup_wanted'
PAGE_TITLE = 'Where should NekoServer store your apps and files?'
PAGE_HINT = (
    'Choose a drive for each line. Setup never formats a drive, and choosing one changes nothing on it. '
    'A drive that cannot be used for a line says why.'
)
# How the page names the drive roles and the places (the terminal's own words are drive_table's).
PAGE_ROLES = {
    'data_ssd': 'NekoServer and your apps',
    'media_hdd': 'Backups',
    'usb': 'Offline backup on a removable drive (optional)',
}
PAGE_PLACES = {
    'ssd': 'Same drive as NekoServer and your apps',
    'media': 'Same drive as the backups',
}
# What the Backups line holds, as the page says it: your files are there too unless their
# own line names another drive.
BACKUPS_HOLD = 'every backup NekoServer makes, and your files where a line below says "Same drive as the backups"'
SAME_DRIVE_AS_BACKUP = (
    'Their backup is then on the same drive: it protects against a mistake, not against losing that drive. '
    'Only the offline backup is a copy on another drive'
)
SMB_SAID = (
    'A drive on a NAS (a storage device on your home network) is connected first (the switch on setup\'s first page): as a disk of its '
    'own (iSCSI) or a shared folder (NFS). Folders shared the Windows way (SMB) are not supported yet.'
)


def page_ssd_room(config, row):
    """The room, in GB, on the drive of NekoServer and your apps as the page opens: the
    drive that has the role, or the one recommended for it (`row`); 0 when there is none."""
    if findmnt('SOURCE', str(mount_path(config['data_mount']))) is not None:
        return free_gb(config['data_mount'])
    return free_on(row)


def place_options(best, others):
    """The places a kind of files may be kept, as the page lists them: the recommended
    one first and said so, then the other of the two, then the drives of their own."""
    other = 'ssd' if best == 'media' else 'media'
    words = PAGE_PLACES[best]
    return [[best, 'Recommended: ' + words[0].lower() + words[1:]], [other, PAGE_PLACES[other]], *others]


@contextlib.contextmanager
def storage_page(context, inventory):
    """While this is active, the drive questions of this step are one page in the setup
    window: a line for each role with the drives to choose from (each with why it cannot
    be that one, when it cannot), a line for where each app keeps your files, and the
    removable drive of the offline copy. The window sends them together; the terminal is
    asked line by line. Nothing is shown when nothing is left to ask, or with nobody there."""
    asked = page_fields(context.config) if sys.stdin.isatty() else {}
    if not asked:
        yield
        return
    import drive_table
    import setup_progress

    config = context.config
    table = drive_table.rows(inventory, config, discover_filesystems(inventory))
    best = drive_table.recommended(table, config)
    roles = [
        {
            'key': key,
            'title': PAGE_ROLES[key],
            'holds': BACKUPS_HOLD if key == 'media_hdd' else drive_table.holds(config, key),
            'needs': drive_table.needs(key),
            'optional': key == 'usb',
            'rows': drive_table.for_window(table, key, config),
            # Chosen already when the page opens: change it only if you want another.
            'recommended': str(best[key]['number']) if best.get(key) else '',
        }
        for key in ('data_ssd', 'media_hdd', 'usb')
        if key in asked
    ]
    others = [
        [f"drive:{row['number']}", f"On a drive of their own: {row['number']} · {drive_table.words(row)}"]
        for row in data_drives(config, inventory)
    ]
    # A group's line is a row of the table: the drive its apps are installed on, and the
    # drive its files are on. The first is offered only where there is a drive for it.
    install_on = [['', INSTALL_HERE]] + [
        [f"drive:{row['number']}", f"On a drive of its own: {row['number']} · {drive_table.words(row)}"]
        for row in install_drives(config, inventory)
    ]
    installs = installs_asked(config) if len(install_on) > 1 else []
    ssd_room = page_ssd_room(config, best.get('data_ssd'))
    places = [
        {
            'key': what,
            'title': words[0].upper() + words[1:],
            'options': place_options(recommended(ssd_room, threshold), others),
            # Chosen already when the page opens, and named: nothing is left for setup to decide unseen.
            'recommended': recommended(ssd_room, threshold),
            # Said under the line while the backups' drive is chosen for it.
            'beside_backup': SAME_DRIVE_AS_BACKUP + '.' if what in backed_up_beside(config) else '',
            **(
                {'group': INSTALLED_NAMES[what], 'install': {'key': 'install_' + what, 'options': install_on}}
                if what in installs
                else {}
            ),
        }
        for what, words, threshold in WHERE
        if what in asked
    ]
    asked = {**asked, **{'install_' + what: '' for what in installs}}
    with setup_progress.page(
        drive_table.STORAGE_PAGE,
        asked,
        title=PAGE_TITLE,
        hint=PAGE_HINT,
        roles=roles,
        places=places,
        names=drive_table.NAMES_CHANGE,
        network=SMB_SAID,
    ):
        yield


def page_fields(config):
    """What the storage page still has to ask on this server, as {field: ''}: a drive role
    with no drive yet, a kind of files with no place yet, the offline drive not chosen."""
    storage = config.get('storage', {})
    asked = {}
    for key, _label, _short in DRIVES:
        record = storage.get(key) if isinstance(storage.get(key), dict) else {}
        if not str(record.get('uuid', '')).strip():
            asked[key] = ''
    places = config.get('locations') if isinstance(config.get('locations'), dict) else {}
    for what, _words, _threshold in WHERE:
        if what == 'movies' and set_up_before(config):
            continue  # a server that is set up keeps its films where they are, unasked
        if (
            asked_where(config, what)
            and places.get(what) not in DRIVE_WORDS
            and places.get(what) not in extra_drives(config)
            and what not in drive_table_places()
        ):
            asked[what] = ''
    photos = config.get('photos', {})
    if not photos.get('external_backup_uuid') and photos.get(OFFLINE_WANTED) is not False:
        asked['usb'] = ''
    return asked


def save_storage(context):
    """The drives' saved identities written to the settings file (unattended reruns)."""
    config_path = context.config_path
    # The controller may supply a working config without a writable config file.
    if hasattr(context, 'persist_config'):
        context.persist_config()
    elif config_path is not None and config_path.is_file():
        payload = json.loads(config_path.read_text())
        payload['storage'] = context.config['storage']
        atomic_json(config_path, payload)


def mount_all(context, specs, fstab=FSTAB):
    """fstab written, the drives mounted and checked. A Btrfs drive mounted for the first
    time without a saved subvolume mounts the filesystem's default tree, which can be a
    subvolume (`btrfs subvolume set-default`, `/@data` say): that tree is saved as the
    drive's subvolume and its fstab line pinned to it, so a restart (or a later change of
    the default) mounts the same files, and the next run finds the line its own."""
    exports = shared_folders(context.config)
    folders = system_folders(context.config)
    install_fstab(specs, fstab, context.log, network_uuids(context.config), exports, folders)
    call(['systemctl', 'daemon-reload'])
    pinned = []
    for key, uuid, fstype, target, subvolume in specs:
        found = mount_and_check(uuid, target, subvolume, fstype, context.log, folders.get(target))
        note = raid_note(target)
        if note:
            context.log(note)
        if found != subvolume and key in context.config['storage']:
            context.config['storage'][key]['subvolume'] = found
            pinned.append((key, uuid, fstype, target, found))
            context.log(
                f'{target}: kept in the folder {found} of the disk Linux runs from; saved, so every check knows it'
                if target in folders
                else f'{target}: the drive\'s default Btrfs subvolume is {found}; saved, so it is always mounted'
            )
        named = folders.get(target) or exports.get(uuid) or f'UUID={uuid}'
        context.log(f'Mounted {named} on {target}; fstab configured for reboot')
    if pinned:
        save_storage(context)
        install_fstab(mount_specs(context.config), fstab, context.log, network_uuids(context.config), exports, folders)
        call(['systemctl', 'daemon-reload'])


MOUNT_WAIT = 600


def raid_note(target, sys_block=None):
    """A warning when the drive mounted at `target` is a RAID array that has lost a drive
    (a rerun with saved drives never stops a working server for it), or None."""
    source = (findmnt('SOURCE', target) or '').split('[')[0]
    name = os.path.basename(source)
    lost = raid_lost(name, sys_block) if name.startswith('md') else None
    if not lost:
        return None
    return (
        f'{target}: the RAID array {name} has lost a drive ({lost}), so your data there has one copy fewer. '
        "Replace the failed drive and let the array rebuild; the dashboard's Drives page shows the state."
    )


def only_a_folder(target, fstype, inside):
    """Only Btrfs has subvolumes. On an ext4 or XFS drive, findmnt showing a folder
    (/dev/sdb1[/films]) means a folder of the drive is mounted there (a bind mount), not
    the whole drive: the apps would see only that folder, and a restart would mount the
    whole drive instead. Setup stops and says so."""
    if fstype != 'btrfs' and inside:
        raise RuntimeError(
            f'Only the folder {inside} of the drive is mounted at {target}, not the whole drive. '
            f'Unmount it (sudo umount {target}, and take out its line in /etc/fstab if you added one), '
            'then run  sudo bash install.sh  again: setup mounts the whole drive there itself'
        )


def pool_lacks_a_drive(uuid):
    """A Btrfs pool over several drives with one of them gone: `btrfs filesystem show`
    lists the drives it finds and ends with "*** Some devices missing". Such a pool
    refuses an ordinary mount; only `-o degraded` opens it on the drives left."""
    try:
        shown = call(['btrfs', 'filesystem', 'show', uuid], timeout=30)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False
    return any(line.strip().startswith('***') and 'missing' in line for line in shown.splitlines())


def seal_extra(target, run=subprocess.run):
    """The empty folder a drive beyond the two is mounted on, closed before it is mounted:
    while that drive is away nothing can be made in its place (gate runs C9 and C16:
    Docker made the apps' folders on the system disk, and Lidarr started there)."""
    from neko_common import EXTRA_MOUNTS

    place = Path(target)
    if place.parent != Path(EXTRA_MOUNTS) or any(place.iterdir()):
        return
    unwritable(place, run)


def mount_target(target, uuid, fstype, log=None):
    """`mount target` (fstab's line), or, for a Btrfs pool missing a drive, the same with
    `degraded` added: a rerun of setup on a server whose mirror lost a drive goes on, and
    says so, as the drive-return service does at boot (drive_return.rs)."""
    try:
        call(['mount', target], timeout=MOUNT_WAIT)
    except RuntimeError:
        if fstype != 'btrfs' or not pool_lacks_a_drive(uuid):
            raise
        call(['mount', '-o', 'degraded', target], timeout=MOUNT_WAIT)
        if log:
            log(
                f'{target}: a drive of the Btrfs pool is missing, so it was mounted on the drives left (degraded). '
                'Your data there has one copy fewer until the drive is replaced; RECOVERY.md shows how'
            )


def mounted_now(target, uuid, fstype, log):
    """Mount a drive that is not mounted yet: its empty folder closed first (seal_extra).
    A big drive replaying its journal after a power cut can take minutes."""
    seal_extra(target)
    try:
        mount_target(target, uuid, fstype, log)
    except subprocess.TimeoutExpired:
        if findmnt('TARGET', target) is None:  # the kernel may still have finished it
            raise RuntimeError(
                f'Mounting {target} took longer than {MOUNT_WAIT // 60} minutes; check the drive '
                '(sudo dmesg shows what it is doing) and run setup again'
            ) from None


def the_folder_mounted(target, folder, found):
    """What findmnt shows at `target` is the folder of the disk Linux runs from the role
    is kept in."""
    if not found or not found.endswith(inside_filesystem(folder)):
        raise RuntimeError(
            f'{target} should be the folder {folder} of the disk Linux runs from, but something else is '
            f'mounted there ({found or "a whole filesystem"}). Unmount it (sudo umount {target}) and run setup again'
        )


def mount_and_check(uuid, target, subvolume, fstype='btrfs', log=None, folder=None):
    """Mount the drive (unless it is) and make sure what is mounted is the drive, and the
    Btrfs subvolume, the settings name. Returns the subvolume mounted: with none saved
    (the fstab line names none) that is the filesystem's default tree, whatever it is;
    always None for a drive that is not Btrfs. `folder`: the role is kept in this folder
    of the disk Linux runs from (made when it is not there); what is returned is that
    folder as findmnt shows it, and it must be that folder."""
    Path(target).mkdir(parents=True, exist_ok=True)
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
    if findmnt('TARGET', target) is None:
        mounted_now(target, uuid, fstype, log)
    elif waits_for_first_use(target):
        open_shared_folder(target)
    if mounted_filesystem(target) != (uuid, fstype):
        raise RuntimeError(f'Post-mount validation failed for {target}')
    found = mounted_subvolume(target)
    if folder:
        the_folder_mounted(target, folder, found)
    else:
        only_a_folder(target, fstype, found)
    if subvolume is not None and found != subvolume:
        raise RuntimeError(
            f'{target} is mounted with Btrfs subvolume {found or "(default)"}, but the settings say '
            f'{subvolume}; a restart would mount a different tree'
        )
    return found


# The three places setup asks about (neko_common.LOCATIONS), in the words it asks them.
WHERE = (
    ('photos', "Immich's photos and videos", 500),
    ('movies', 'the films and shows (and their downloads)', None),
    ('music', 'the music library (and its downloads)', 400),
    ('cloud', "Nextcloud's files", None),
)


def asked_where(config, what):
    """Whether this server keeps such files at all: no question about where the photos go
    without Immich, the music without the Music group, or Nextcloud's files with
    Nextcloud switched off (the setup review, S7)."""
    from neko_common import immich_wanted, media_group

    if what == 'photos':
        return immich_wanted(config)
    if what == 'movies':
        from neko_common import apps_to_connect

        return apps_to_connect(config)  # Jellyfin's library, or the download apps' folders
    if what == 'music':
        return media_group(config, 'music')
    return config.get('nextcloud', {}).get('enabled', True) is True


def recommended(ssd_gb, threshold):
    """The better drive on this server: the SSD (faster) when it is big enough to hold it
    for years (`threshold` GB), else the media drive (bigger). No threshold (Nextcloud's
    files, which grow without limit and need no speed): the media drive."""
    return 'ssd' if threshold is not None and ssd_gb >= threshold else 'media'


def has_files(folder):
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


def kept_location(config, what):
    """The drives where `what` already holds files: [] when it is nowhere yet, one drive
    when it stays there (moving it is a job of its own), or both."""
    from neko_common import DRIVE_KEYS, LOCATIONS

    return [
        drive
        for drive in ('ssd', 'media')
        if any(has_files(Path(config[DRIVE_KEYS[drive]]) / rel) for rel in LOCATIONS[what][1])
    ]


def free_gb(folder):
    try:
        found = os.statvfs(folder)
    except OSError:
        return 0
    return found.f_bavail * found.f_frsize // 10**9


DRIVE_WORDS = {'ssd': 'the SSD', 'media': 'the media drive'}


def taken_place(context, words, place):
    """The place chosen for a kind of files, as "locations" names it: "ssd", "media", or
    the name of a drive of their own, which is then listed under "storage"."""
    if isinstance(place, dict):
        context.log(f'{words.capitalize()}: on {drive_table_said(place)}, a drive of their own')
        return placed_on(context.config, place)
    context.log(f'{words.capitalize()}: on {DRIVE_WORDS[place]}')
    return place


def place_of(context, kind, asking):
    """One kind of files given its place in "locations". `kind`: its row of WHERE;
    `asking`: (the question to ask with, or False with nobody there; (room, on_nas);
    the drives beyond the two). Returns True when the settings changed."""
    import setup_progress

    what, words, threshold = kind
    asks, drives, others = asking
    config = context.config
    chosen = config['locations']
    kept = kept_location(config, what)
    if chosen.get(what) in DRIVE_WORDS:
        same_place(context, what, words, chosen[what], kept)
        return False
    if chosen.get(what) in extra_drives(config):
        return False  # on a drive of its own, as chosen before
    # Chosen on the storage page before the approval (choose_ahead); files found now win.
    wish = None if kept else drive_table_places().get(what)
    if what == 'movies' and (kept or set_up_before(config) or (wish is None and not asks)):
        # Where every server before 0.50.0 has them (the media drive): not written
        # down unless someone chose, so a server that is set up keeps its settings.
        setup_progress.page_answer(what)
        return False
    if kept:
        chosen[what] = where_kept(context, what, words, kept)
        setup_progress.page_answer(what)  # not a choice here: the files are where they are
        return True
    place = wish
    if place is None:
        place = new_place(context, what, (words, threshold), drives, asks, key=what, others=others)
    chosen[what] = taken_place(context, words, place)
    return True


def choose_locations(context, input_fn=None, interactive=None, inventory=None):
    """Where each app keeps your files, asked once (saved as "locations"): the SSD, the
    media drive (the better one pre-selected), or another drive connected to this PC
    (`inventory`: what lsblk lists; such a drive is added under "storage" and mounted
    by the caller). The backups, restores and checks all follow the choice
    (neko_common.files_root). A place already holding files stays as it is."""
    config = context.config
    chosen = config.setdefault('locations', {})
    interactive = sys.stdin.isatty() if interactive is None else interactive
    room = {'ssd': free_gb(config['data_mount']), 'media': free_gb(config['media_mount'])}
    on_nas = bool(config.get('storage', {}).get('media_hdd', {}).get('network'))
    others = data_drives(config, inventory) if interactive and inventory else []
    asking = (interactive and (input_fn or input), (room, on_nas), others)
    # A kind whose app is not part of this server is asked about if it is added later.
    changes = [
        place_of(context, (what, words, threshold), asking)
        for what, words, threshold in WHERE
        if asked_where(config, what)
    ]
    if chosen.get('photos') == 'media' and asked_where(config, 'photos'):
        context.log(photos_on_media(config))
    if chosen.get('music') == 'media' and 'music' in backed_up_beside(config) and asked_where(config, 'music'):
        context.log(MUSIC_ON_MEDIA)
    if any(changes) and hasattr(context, 'persist_config'):
        context.persist_config()


# Said when the photos are on the media drive: its weekly photo archive is on that drive
# too (backup.rs), so only the USB copy is on another drive (the 0.46.1 audit).
PHOTOS_ON_MEDIA = (
    "Immich's photos are on the media drive, and so is their weekly archive copy: it keeps older versions, "
    'but does not survive losing that drive. Your only copy on another drive is the weekly USB backup, so '
    'set it up (setup asks for the USB drive next).'
)


def photos_on_media(config):
    """What the photos' place means for their copies, as it is on this server (gate run
    B3: "your only copy on another drive is the weekly USB backup" with none chosen, on a
    server with one drive)."""
    photos = config.get('photos', {})
    where = 'the one drive of this server' if one_place(config) else DRIVE_WORDS['media']
    said = (
        f"Immich's photos are on {where}, and so is their weekly archive copy: it keeps older versions, "
        'but does not survive losing that drive.'
    )
    if photos.get('external_backup_uuid'):
        return said + ' Your only copy on another drive is the weekly USB backup.'
    if photos.get(OFFLINE_WANTED) is False:
        return said + (
            ' No copy is on another drive: to add one, connect a drive you can unplug and run  '
            'sudo bash install.sh  again.'
        )
    return PHOTOS_ON_MEDIA if where == DRIVE_WORDS['media'] else said + ' Set up the offline backup (asked next).'


def one_place(config):
    """The apps and the media are on one drive (a drive of its own holding both, or the
    disk Linux runs from): "the SSD" and "the media drive" are the same drive there."""
    import drive_table

    return shared_with(config) is not None or len(drive_table.system_roles(config)) == 2


def backed_up_beside(config):
    """The kinds of files whose backup copy is kept on the backups' drive: the photos
    (their weekly archive) and, where the music backup is switched on, the music. Kept on
    that same drive, the copy is lost with it."""
    return ('photos', 'music') if config.get('music', {}).get('backup_enabled') is True else ('photos',)


MUSIC_ON_MEDIA = (
    'The music is on the media drive, and so is its backup: that protects against a mistake, not against '
    'losing that drive.'
)


def data_drives(config, inventory):
    """The drives connected to this PC, beyond the two roles, that an app's files may be
    kept on: a filesystem Linux apps can use, a disk of its own (not the one Linux runs
    from, not one of the roles'), big enough, not in use elsewhere. Rows of the drive
    list (drive_table.py), with the numbers they have there."""
    import drive_table

    table = drive_table.rows(inventory, config, discover_filesystems(inventory))
    return [
        row
        for row in table
        if not row['system']
        and row['device'] is not None
        and not drive_table.role_of(row, config)
        and not drive_table.drive_verdict(row, 'media_hdd', config)
    ]


def placed_on(config, row):
    """The name ("ssd", "media", or one such as "d3" under "storage") of the drive in this
    row of the list: the role it has, the name it was given before, or a new one."""
    storage = config.setdefault('storage', {})
    for key, name in (('data_ssd', 'ssd'), ('media_hdd', 'media')):
        if isinstance(storage.get(key), dict) and storage[key].get('uuid') == row['uuid']:
            return name
    for name, record in extra_drives(config).items():
        if record.get('uuid') == row['uuid']:
            return name
    name = next(f'd{n}' for n in range(3, 100) if f'd{n}' not in storage and not used_before(f'd{n}'))
    storage[name] = {}
    new_drive(storage[name], whole(unlocked(row['device'])))
    return name


def used_before(name):
    """A drive name whose mount folder is still there (a lost drive's): never given to a
    new drive, so the apps' Compose files name the new one and their containers are made
    again on it (gate run C17: Lidarr kept running on the lost drive's empty folder)."""
    from neko_common import EXTRA_MOUNTS

    return (EXTRA_MOUNTS / name).exists()


def drive_table_places():
    import drive_table

    return drive_table.PLACES_AHEAD


def drive_table_said(row):
    import drive_table

    return drive_table.brief(row)


def new_place(context, what, asked, drives, input_fn, key=None, others=()):
    """The drive for `what`, which has no files yet. Asked (`input_fn`, False when no one is
    at the keyboard): the better drive is offered first. Not asked: the better one on a new
    server, and on one set up before 0.46.0 the layout it had, so nothing moves without
    being asked (the 0.46.1 audit)."""
    from neko_common import LOCATIONS

    words, threshold = asked
    room, on_nas = drives
    best = recommended(room['ssd'], threshold)
    if not input_fn:
        return LOCATIONS[what][0] if set_up_before(context.config) else best
    return asked_location(context, words, best, room, on_nas, input_fn, key=key, others=others)


def where_kept(context, what, words, kept):
    """The drive that already holds `what`'s files; with files on both, setup stops."""
    if len(kept) > 1:
        raise RuntimeError(
            f'{words.capitalize()} have files on both the SSD and the media drive, and setup can\'t tell '
            'which ones the app uses. Look in both folders, then say which one it is: put '
            f'"locations": {{"{what}": "ssd"}} (or "media") in my-config.json and run setup again. '
            'The other folder is left as it is. Nothing was changed.'
        )
    context.log(f'{words.capitalize()}: kept where they already are, on {DRIVE_WORDS[kept[0]]}')
    return kept[0]


def set_up_before(config):
    """True when apps were set up on this SSD before (their settings are there)."""
    appdata = Path(config['data_mount']) / 'appdata'
    try:
        return appdata.is_dir() and any(appdata.iterdir())
    except OSError:
        return False


def same_place(context, what, words, chosen, kept):
    """A saved choice must be where the files are: the app would start on an empty folder."""
    if kept and chosen not in kept:
        raise RuntimeError(
            f'The settings put {words} on {DRIVE_WORDS[chosen]}, but they are on {DRIVE_WORDS[kept[0]]}: the app '
            f'would start with an empty folder. Put "locations": {{"{what}": "{kept[0]}"}} back in my-config.json '
            '(moving them is a job of its own, with the apps stopped), then run setup again. Nothing was changed.'
        )
    if len(kept) > 1:
        other = DRIVE_WORDS['media' if chosen == 'ssd' else 'ssd']
        context.log(
            f'{words.capitalize()}: used from {DRIVE_WORDS[chosen]}, as your settings say. {other[0].upper()}{other[1:]} '
            'holds another folder of them too; NekoServer leaves it alone and does not back it up.'
        )


def asked_location(context, words, best, room, on_nas, input_fn=None, key=None, others=()):
    """The drive for `words`, asked: the better one first, Enter takes it. `others`: the
    drives beyond the two that it may be kept on (data_drives), offered after them; one
    chosen is returned as its row. `key`: its line on the storage page, where the window
    names the drive itself ("ssd", "media", "drive:4" for row 4 of the list) or leaves it
    to setup."""
    import drive_table

    # "The most room" only when it has it (seen on the rig: said of a media drive with
    # 24 GB free beside an SSD with 26).
    most = ', the most room' if room['media'] > room['ssd'] else ''
    said = {
        'media': f'the media drive{" (on the NAS)" if on_nas else ""}: {room["media"]} GB free{most}',
        'ssd': f'the SSD: {room["ssd"]} GB free, faster',
    }
    options = [best, 'ssd' if best == 'media' else 'media']
    if one_place(context.config):
        # One drive holds both (gate run B3 offered it twice, as "the media drive" and
        # "the SSD"): it is offered once, and not asked about with no other drive.
        if not others:
            context.log(f'{words[0].upper() + words[1:]}: kept on the one drive of this server.')
            return best
        options = [best]
        said[best] = f'the drive of this server, with the apps: {room[best]} GB free'
    context.log(f'Where should {words} be kept? (The backups follow wherever you choose.)')
    for n, drive in enumerate(options, 1):
        context.log(f'  {n}) {said[drive]}' + ('  (recommended)' if drive == best else ''))
    first = len(options) + 1
    for n, row in enumerate(others, first):
        context.log(f'  {n}) a drive of their own: {drive_table.brief(row)}')
    return place_asked(context, (options, others), input_fn, key)


def place_asked(context, offered, input_fn, key):
    """The place the person names among `offered` (the drives, the drives of their own),
    asked three times at most; the recommended one (the first) after that."""
    import drive_table

    options, others = offered
    for _ in range(3):
        answer = drive_table.field(key or 'place', input_fn or input, 'Number (Enter for the recommended one): ')
        if len(options) == 1 and answer.isdigit() and int(answer) >= 2:
            # Numbered from 2 here: the place_answered numbers count two drives first.
            answer = str(int(answer) + 1)
        place = place_answered(answer, options, others)
        if place is not None:
            return place
        context.log(f'Type a number from 1 to {len(options) + len(others)}, or press Enter.')
    return options[0]


def place_answered(answer, options, others):
    """The place an answer names: "ssd" or "media" (the window's words), a row of `others`
    ("drive:4" from the window, or its number in the terminal's list), the first of
    `options` for Enter alone. None for an answer that names none."""
    if answer in DRIVE_WORDS:
        return answer
    if window_row(answer) is not None:
        return next((row for row in others if str(row['number']) == window_row(answer)), None)
    if answer in ('', '1', '2'):
        return options[int(answer) - 1] if answer else options[0]
    if answer.isdigit() and 3 <= int(answer) < 3 + len(others):
        return others[int(answer) - 3]
    return None


def choose_usb(context):
    """Pick the USB backup drive with the other drives, so the space check covers it."""
    photos = context.config.setdefault('photos', {})
    if photos.get('external_backup_uuid') or not sys.stdin.isatty() or photos.get(OFFLINE_WANTED) is False:
        return
    from modules.photo_backup import copies, select_external

    if not any(copies(context.config)):
        # No Immich and no Nextcloud: the weekly copy has nothing to take (gate run C7),
        # so no drive is asked for it.
        return

    inventory = inventory_now()
    try:
        chosen = select_external(context.config, inventory, out_fn=context.log)
    except RuntimeError as exc:
        context.log(f'USB backup drive not chosen yet ({exc}); you will be asked again at the weekly backup step')
        return
    if chosen is None:
        # "None for now" is an answer: the weekly copy is left out, and nothing asks again.
        photos[OFFLINE_WANTED] = False
        context.log(
            'Offline backup: none, as you chose. The weekly copy to a removable drive is left out. To add one '
            'later, connect it and run  sudo bash install.sh  again: setup offers it before anything changes.'
        )
    if hasattr(context, 'persist_config'):
        context.persist_config()


# The steps that write a backup or update timer: the backup times are asked only when
# the run has one of them (a profile with no backups never asks).
JOB_STEPS = frozenset(
    ('photo_preservation', 'photo_backup', 'music_backup', 'app_backup', 'host_backup', 'maintenance')
)


def choose_media_apps(context):
    """The media groups, for setup run without its first page (guided_setup.choose_media_apps)."""
    from guided_setup import choose_media_apps as ask

    ask(context)


def remember_media_apps(context):
    from guided_setup import remember_media_apps as keep

    keep(context)


def choose_backup_times(context):
    """When the backups run (daily, weekly, monthly), asked here with the drives so the
    person answers every question at the start (guided_setup.choose_backup_times). Asked
    before the approval already in this run: kept as chosen, nothing asked again."""
    import drive_table

    if drive_table.TIMES_AHEAD.get('asked'):
        from neko_common import BACKUP_TIMES, backup_times_said, remember_answer

        remember_answer(context.config['data_mount'], BACKUP_TIMES, backup_times_said(context.config))
        context.log('Backup times: as chosen before the approval, ' + backup_times_said(context.config))
        return
    plan = getattr(context, 'plan', None)
    if isinstance(plan, (list, tuple)) and not JOB_STEPS & set(plan):
        return
    from guided_setup import choose_backup_times as ask

    ask(context)


def mounted_filesystem(target):
    """(UUID, filesystem type) of what is mounted at target, or None, read from the
    filesystem itself as identity_matches does: findmnt's UUID column, or blkid of the
    mounted device when that column is empty. Never by comparing device paths: every
    device of a Btrfs pool carries the pool's UUID, findmnt names one of them (the lowest
    device ID) as the source, and /dev/disk/by-uuid may point at another."""
    import nfs_exports

    found = findmnt('UUID,FSTYPE,SOURCE', target) or ''
    fields = found.split()
    if len(fields) == 3:
        return fields[0], fields[1]
    shared = nfs_exports.mounted(found)
    if shared:
        return shared
    if len(fields) == 2 and fields[1].startswith(DEV):
        return uuid_of(fields[1].split('[')[0]), fields[0]
    return None
