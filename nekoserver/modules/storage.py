"""Safe existing-filesystem mounting by explicitly configured UUID; never formats."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from neko_common import mount_path, atomic_json, atomic_write, findmnt, confirmed, safe_path

DEPENDENCIES = ['system']
DESCRIPTION = 'Inspect disks; --apply mounts explicitly identified data SSD and media HDD by UUID'


def call(argv, timeout=30):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError(f'{argv[0]} failed: {p.stderr.strip()[:400]}')
    return p.stdout.strip()


def inventory_now():
    """Every disk and partition, as lsblk JSON (one command, used by all drive pickers)."""
    return json.loads(call(['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINT,MOUNTPOINTS,MODEL,RM,TRAN,LABEL']))


# What can hold a filesystem: a disk or partition, an opened encrypted drive (LUKS), an
# LVM volume or a software RAID array. The last three sit on top of one or more disks.
VOLUME_KINDS=('part','disk','crypt','lvm','raid0','raid1','raid4','raid5','raid6','raid10','md')
# Where Linux keeps itself: the root and the boot folders (Arch often mounts the EFI
# partition at /efi). A disk holding any of them is the system disk.
SYSTEM_MOUNTS=('/','/boot','/boot/efi','/efi')


def mount_points(dev):
    """Every folder an lsblk row is mounted at. MOUNTPOINT names only one of them: a
    Btrfs system partition mounted as several subvolumes (/, /home, /.snapshots) may
    show /home there, so MOUNTPOINTS (util-linux 2.37 and newer) is read too."""
    found={dev.get('mountpoint')}|set(dev.get('mountpoints') or ())
    return {m for m in found if m}


def holds_system(dev):
    """This lsblk row, or anything under it, holds the system (SYSTEM_MOUNTS)."""
    return bool(mount_points(dev)&set(SYSTEM_MOUNTS)) or any(holds_system(c) for c in dev.get('children',[]))


def device_sub_uuid(name):
    """A Btrfs device's own UUID (UUID_SUB): each device of a pool has its own, a cloned
    drive the same as its original. None when it can't be read."""
    for path in ('/dev/' + name, '/dev/mapper/' + name):
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
    by_uuid={}
    for row in rows:by_uuid.setdefault(row['uuid'],[]).append(row)
    return [one_filesystem(group, sub_uuid) for group in by_uuid.values()]


CLONED='Duplicate filesystem UUID detected; identify cloned drives before continuing'


def one_filesystem(group, sub_uuid=device_sub_uuid):
    """The lsblk rows of one UUID as one candidate, on every disk they span. Btrfs rows are
    one pool only when each device has a UUID_SUB of its own: a copy of a Btrfs drive (dd,
    a disk cloner) has the same one, and the kernel could then write to either drive."""
    names=sorted({r['name'] for r in group})
    if len(names)>1:
        if not all(r['fstype']=='btrfs' for r in group):
            raise RuntimeError(CLONED)
        pool_members(names, sub_uuid)
    disks=sorted({d for r in group for d in r['physical_disks'] if d})
    return {**group[0],'physical_disks':disks,'physical_disk':disks[0] if disks else None,
            'mountpoint':next((r['mountpoint'] for r in group if r['mountpoint']),None),
            'unlocks':sorted({n for r in group for n in r.get('unlocks',())})}


def pool_members(names, sub_uuid=device_sub_uuid):
    """Several devices with one Btrfs UUID are one pool only when each has a UUID_SUB of
    its own. One that can't be read (blkid needs root to read a device that is not in its
    cache) fails closed too, but says so: the drives are not known to be copies."""
    subs=[sub_uuid(n) for n in names]
    unread=[n for n, sub in zip(names, subs) if sub is None]
    if unread:
        raise RuntimeError(f'Could not read the Btrfs device ID of /dev/{unread[0]}, which tells the drives of a Btrfs '
                           'pool apart from copies of one drive. Run setup with sudo (sudo bash install.sh) so it can '
                           'read it; if it still can\'t, check that the drive is connected and working.')
    if len(set(subs))!=len(subs):
        raise RuntimeError(CLONED)


CRYPTTAB=Path('/etc/crypttab')


def unlocked_at_boot(name,crypttab=CRYPTTAB):
    """An opened encrypted drive (`name` as lsblk shows it) that Linux unlocks at every
    start, so it can be mounted then: it is listed in /etc/crypttab, without `noauto`
    (a line with it is only unlocked by hand)."""
    try:
        lines=crypttab.read_text().splitlines()
    except OSError:
        return False
    rows=[line.split() for line in lines if line.strip() and not line.lstrip().startswith('#')]
    return any(row[0]==name and 'noauto' not in (row[3].split(',') if len(row)>3 else ()) for row in rows)


def boot_unlock_problem(device,crypttab=CRYPTTAB):
    """Why an encrypted drive can't be used yet, or None. That is the drive itself, or the
    encrypted drives under it (an LVM volume or RAID array on top of encrypted drives)."""
    names=[*(device.get('unlocks') or ()),*([device['name']] if device.get('kind')=='crypt' else [])]
    locked=[n for n in names if not unlocked_at_boot(n,crypttab)]
    if not locked:
        return None
    return (f"/dev/mapper/{locked[0]} is an encrypted drive that you opened by hand. Setup mounts it at every "
            'start, so Linux must unlock it then: add it to /etc/crypttab (with a key file, or a password asked '
            'at start-up), restart, and run setup again.')


def candidate(dev, parent, physical, unlocks=()):
    """One lsblk row as a filesystem setup could use, or None. `unlocks`: the encrypted
    drives it sits on, which Linux must open before it can be mounted."""
    uuid=dev.get('uuid'); fs=dev.get('fstype'); kind=dev.get('type','')
    if kind not in VOLUME_KINDS or not uuid or fs not in ('ext4','xfs','btrfs'):
        return None
    return {'uuid':uuid,'fstype':fs,'name':dev.get('name',''),'size':dev.get('size'),'label':dev.get('label'),'model':dev.get('model') or parent,
            'mountpoint':dev.get('mountpoint') or next(iter(sorted(mount_points(dev))),None),'physical_disks':[physical],'kind':kind,
            'unlocks':list(unlocks)}


def unlocked(device):
    """The chosen drive, unless it is an encrypted one Linux doesn't unlock at start."""
    problem=boot_unlock_problem(device)
    if problem:raise RuntimeError(problem)
    return device


def discover_filesystems(inventory, sub_uuid=device_sub_uuid):
    """Return candidates, never auto-select a device on model/size alone."""
    rows,placed=[],[]
    def walk(dev, parent='', disk=None, unlocks=()):
        physical=dev.get('name','') if dev.get('type')=='disk' else disk
        placed.append((dev,physical))
        row=candidate(dev,parent,physical,unlocks)
        if row:rows.append(row)
        below=(*unlocks,dev.get('name','')) if dev.get('type')=='crypt' else unlocks
        for child in dev.get('children',[]): walk(child,dev.get('model') or parent,physical,below)
    for device in inventory.get('blockdevices',[]): walk(device)
    os_disks,system_ids=system_disks(placed)
    # The system's own filesystems (and anything sharing a disk with them) are never offered.
    mine=[r for r in rows if r['uuid'] not in system_ids]
    return [v for v in merged(mine, sub_uuid) if not set(v['physical_disks'])&os_disks]


def system_disks(placed):
    """The disks and filesystem UUIDs of the system, from (lsblk row, its disk) pairs. A
    Btrfs system pool over two drives shows as mounted on one of them only (lsblk names
    one member): every disk with a device of a system filesystem is a system disk."""
    system_ids={dev.get('uuid') for dev, _ in placed if mount_points(dev)&set(SYSTEM_MOUNTS)}
    os_disks={disk for dev, disk in placed if mount_points(dev)&set(SYSTEM_MOUNTS)}
    os_disks|={disk for dev, disk in placed if dev.get('uuid') and dev.get('uuid') in system_ids}
    return os_disks,system_ids


NOT_LINUX = {'ntfs': 'NTFS (Windows)', 'exfat': 'exFAT', 'vfat': 'FAT'}


def empty_disk(dev):
    """A whole disk with nothing on it yet (a new drive straight from the box): no
    filesystem, no partitions, not in use. Not RAM disks, eMMC boot areas or empty slots;
    a partition without a filesystem may be one Windows keeps for itself, so never those."""
    return (dev.get('type') == 'disk' and not dev.get('fstype') and not dev.get('children')
            and not dev.get('mountpoint') and dev.get('size') not in (None, '0B')
            and not re.match(r'(zram|ram|nbd)\d|mmcblk\d+(boot|rpmb)', dev.get('name', '')))


def nothing_eligible(inventory, items, selected, label):
    """Why no drive can be picked for `label`, drive by drive, and what to do."""
    lines = []
    def walk(dev):
        if dev.get('type') in ('part', 'disk') and dev.get('fstype') in NOT_LINUX:
            fs = dev['fstype']
            lines.append(f"/dev/{dev.get('name')} is formatted {NOT_LINUX[fs]}, which Linux apps can't keep their files "
                         'on. To use it, copy its files elsewhere first, then format it as ext4 (this erases it) with '
                         'the Disks app (GNOME) or KDE Partition Manager.')
        elif empty_disk(dev):
            lines.append(f"/dev/{dev.get('name')} ({dev['size']}) has no filesystem yet. To use it, format it as "
                         'ext4 (this erases it) with the Disks app (GNOME) or KDE Partition Manager.')
        for child in dev.get('children', []):
            walk(child)
    for device in inventory.get('blockdevices', []):
        if not holds_system(device):   # its boot partition (FAT) is never a choice
            walk(device)
    for v in items:
        if v['uuid'] in selected:
            continue
        lines.append(f"/dev/{v['name']} is already in use at {v['mountpoint']}. To use it, unmount it first "
                     f"(sudo umount {v['mountpoint']}, and take out its line in /etc/fstab if you added one).")
    found = ('\n'.join('  - ' + line for line in lines) + '\n') if lines else \
        '  No other drive was found: check that it is connected and shows up in the Disks app.\n'
    return (f'No drive can be used for {label}. NekoServer never formats a drive by itself, and never uses the '
            f'system disk or a drive already in use.\n{found}Then run  sudo bash install.sh  again.')


# The drives setup asks for: (settings key, what it is for, its short name).
DRIVES = (('data_ssd', 'the SSD (apps, photos and music)', 'the SSD'),
          ('media_hdd', 'the HDD (movies, shows, Nextcloud files and backups)', 'the HDD'))
GIB = 1024 ** 3
UNITS = {'B': 0, 'K': 1, 'M': 2, 'G': 3, 'T': 4, 'P': 5}


def short_name(key):
    """'the SSD' or 'the HDD'."""
    return next(short for name, _, short in DRIVES if name == key)


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
        return f'{found / 1024 ** 2:.0f} MB'
    return f'{gib / 1024:.1f} TB' if gib >= 1024 else f'{gib:.0f} GB'


def sizes(key):
    """(smallest, recommended) free space in GB for this drive: the drive space step's own
    numbers (capacity.py), so the two never disagree."""
    from modules import capacity
    return capacity.SSD if key == 'data_ssd' else capacity.HDD


def big_enough(device, smallest):
    """The drive is at least `smallest` GB in all (a smaller one can never have that much
    free). Unknown sizes are left to the drive space step."""
    found = size_bytes(device.get('size'))
    return found is None or found >= smallest * GIB


def describe(device):
    """One drive as the person knows it: its size, the name they gave it (the label in the
    Disks app), its model, where the desktop opened it."""
    words = [size_words(device.get('size'))]
    if device.get('label'):
        words.append(f'named "{device["label"]}"')
    if device.get('model'):
        words.append(str(device['model']).strip())
    if device.get('mountpoint'):
        words.append(f'open at {device["mountpoint"]}')
    return f"{device['name']}: " + ', '.join(words) + f" ({device['fstype']})"


def ask_drive(options, key, label, input_fn=input, out_fn=print, attempts=3):
    """The drive for `key`, chosen by its number and confirmed by typing its name. What it
    needs comes first, then the drives with what tells them apart; one that is too small
    is refused at once and another asked for, before anything is mounted or saved."""
    short = short_name(key)
    smallest, recommended = sizes(key)
    if not any(big_enough(v, smallest) for v in options):
        found = ', '.join(describe(v) for v in options)
        raise RuntimeError(f'No drive here is big enough for {label}: it needs at least {smallest} GB ({recommended} GB '
                           f'or more is recommended), and these are smaller: {found}. Connect a bigger one (formatted '
                           'ext4), then run  sudo bash install.sh  again. Nothing was changed.')
    out_fn(f'Now choose {label}. It needs at least {smallest} GB free ({recommended} GB or more is recommended).')
    for n, v in enumerate(options, 1):
        out_fn(f'  {n}) {describe(v)}' + ('' if big_enough(v, smallest) else f'  - too small, needs {smallest} GB'))
    out_fn('  (The disk Linux runs from is not in the list: it is never used for this.)')
    for _ in range(attempts):
        try:
            choice = input_fn(f'Number of {short} (Enter alone stops setup here): ').strip()
        except EOFError:
            choice = ''
        if not choice:
            break
        if not (choice.isdigit() and 1 <= int(choice) <= len(options)):
            out_fn(f'Type a number from 1 to {len(options)}.')
            continue
        device = options[int(choice) - 1]
        if big_enough(device, smallest):
            return confirmed(device, f"{short} ({size_words(device.get('size'))})", input_fn, out_fn, attempts)
        out_fn(f"{device['name']} is {size_words(device.get('size'))}, too small for {short}: it needs at least "
               f'{smallest} GB. Choose another one from the list.')
    raise RuntimeError(f'No drive was chosen for {label}. Run  sudo bash install.sh  again to choose it; nothing was changed.')


def resolve_storage_identity(config, inventory, apply=False, interactive=False, input_fn=input, out_fn=print):
    """Identify disks by verified UUID. Explicit confirmation is required for unknown devices."""
    items=discover_filesystems(inventory)
    configured=config.setdefault('storage',{})
    selected=set()
    selected_disks=set()
    for key, label, _ in DRIVES:
        record=configured.setdefault(key,{})
        uuid=record.get('uuid','').strip()
        if uuid:
            uuid=saved_choice(record, uuid, items, key, label, apply, inventory=inventory, config=config,
                              interactive=interactive, out_fn=out_fn)
        if not uuid and apply:
            if not interactive: raise RuntimeError(f'{key} is unassigned; rerun interactively on the server to select it safely')
            device=unlocked(choose_new(config, inventory, items, selected, key, label, input_fn, out_fn))
            record['uuid']=device['uuid']; record['fstype']=device['fstype']
            uuid=device['uuid']
        elif not uuid:
            out_fn(f'{key} is not configured; {len(items)} formatted candidates discovered (dry-run only)')
            continue
        if uuid in selected: raise RuntimeError('SSD and HDD cannot use the same filesystem')
        selected_disks|=own_disks(uuid,items,selected_disks,apply)
        selected.add(uuid)
    return configured


def choose_new(config, inventory, items, selected, key, label, input_fn, out_fn):
    """The drive for `key`, asked for among those not chosen yet and not in use elsewhere."""
    # A drive the desktop opened by itself (/run/media/...) is still eligible.
    folder = config.get('data_mount' if key == 'data_ssd' else 'media_mount')
    options=[v for v in items if v['uuid'] not in selected and (not v['mountpoint'] or v['mountpoint']==folder or str(v['mountpoint']).startswith(('/run/media/','/media/')))]
    if not options: raise RuntimeError(nothing_eligible(inventory, items, selected, label))
    return ask_drive(options, key, label, input_fn, out_fn)


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
    name, kind, fs = '/dev/' + str(dev.get('name')), dev.get('type', ''), dev.get('fstype')
    if system:
        return f'{name} is on the disk Linux runs from, which NekoServer never uses for your files'
    if kind not in VOLUME_KINDS:
        what = 'a loop device (a file attached as a disk)' if kind == 'loop' else f'a "{kind}" device'
        return (f'{name} is {what}, which NekoServer does not use for its drives: they must be real drives (or their '
                'partitions, LVM volumes, RAID arrays or encrypted drives)')
    if fs not in ('ext4', 'xfs', 'btrfs'):
        return f'{name} is formatted {NOT_LINUX.get(fs, fs or "with nothing yet")}, not ext4, XFS or Btrfs'
    return None


def saved_choice(record, uuid, items, key, label, apply, inventory=None, config=None, interactive=False, out_fn=print):
    """A drive already in the settings: it must be connected, of the same type and, when
    setup goes on to mount it, unlocked by Linux at every start as a newly chosen one must
    be (settings reused after a reinstall may name an encrypted drive opened by hand).
    Returns its UUID, or '' when it is too small and setup asks for the drive again."""
    matches=[d for d in items if d['uuid']==uuid]
    if len(matches)!=1:
        reason = not_offered(inventory or {}, uuid) if len(matches) == 0 else None
        if reason:
            raise RuntimeError(f'The drive chosen as {label} (UUID {uuid}) is connected, but {reason}. Choose another: '
                               f'take "uuid" out of "{key}" under "storage" in my-config.json (next to the installer) and run '
                               'setup again: it asks for the drive anew. Nothing is formatted.')
        raise RuntimeError(f'The drive chosen as {label} (UUID {uuid}) is not connected, or is not formatted ext4, xfs or btrfs. '
                           f'Connect it and run setup again. If you replaced that drive, take "uuid" out of "{key}" under "storage" in '
                           'my-config.json (next to the installer) and run setup again: it asks for the drive anew. Nothing is formatted.')
    if apply and too_small(matches[0], key, label, config or {}, interactive, out_fn):
        record.pop('uuid', None)
        record.pop('fstype', None)
        return ''
    if record.get('fstype') and record['fstype']!=matches[0]['fstype']:
        raise RuntimeError(f'Filesystem type mismatch for {key}')
    if apply:
        unlocked(matches[0])
    record['fstype']=matches[0]['fstype']
    return uuid


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
    short = short_name(key)
    said = (f"{device['name']} ({size_words(device.get('size'))}), the drive saved as {short}, is too small: it needs at "
            f'least {smallest} GB.')
    if device.get('mountpoint') and device['mountpoint'] == folder:
        raise RuntimeError(f'{said} To choose another, take it out first:  sudo umount {folder} , take the line with '
                           f"UUID={device['uuid']} out of /etc/fstab (sudo nano /etc/fstab), take \"uuid\" out of \"{key}\" "
                           'under "storage" in my-config.json, then run  sudo bash install.sh  again: it asks for the '
                           'drive anew. Nothing on the drive is changed.')
    if not interactive:
        raise RuntimeError(f'{said} Run  sudo bash install.sh  in a terminal to choose another one.')
    out_fn(f'{said} Choose {label} again:')
    return True


def own_disks(uuid,items,taken,apply):
    """The physical disks the chosen filesystem is on (all of them, for a pool or an
    array), none of which the other drive may share."""
    physical=set(next(v['physical_disks'] for v in items if v['uuid']==uuid))
    if physical&taken:raise RuntimeError('SSD and HDD must be on different physical disks')
    if not physical and apply:
        name = next((v['name'] for v in items if v['uuid'] == uuid), uuid)
        raise RuntimeError(f'Setup cannot tell which physical drive /dev/{name} is on (it sits on something Linux does not '
                           'list as a disk, such as a loop device or a disk image), so it cannot make sure the SSD and the '
                           'HDD are two different drives. Choose a partition of a real drive instead.')
    return physical


def mount_specs(config):
    specs = []
    for key, mount_key in [('data_ssd', 'data_mount'), ('media_hdd', 'media_mount')]:
        record = config.get('storage', {}).get(key, {})
        uuid = record.get('uuid', '').strip()
        fstype = record.get('fstype', '').strip()
        target = str(mount_path(config[mount_key]))
        if not uuid or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', uuid):
            raise RuntimeError(f'storage.{key}.uuid must be explicitly configured from lsblk -f')
        if fstype not in ('ext4', 'xfs', 'btrfs'):
            raise RuntimeError(f'storage.{key}.fstype must be ext4, xfs or btrfs')
        if not Path(target).is_absolute() or str(Path(target)) in ('/', '/mnt', '/srv', '/home', '/var'):
            raise RuntimeError(f'Unsafe mount location: {target}')
        if Path(target).is_symlink():
            raise RuntimeError(f'Mount path must not be symlink: {target}')
        specs.append((key, uuid, fstype, target, subvolume_of(record)))
    if Path(specs[0][3]) in Path(specs[1][3]).parents or Path(specs[1][3]) in Path(specs[0][3]).parents:
        raise RuntimeError('Data and media mountpoints must not be nested')
    if len({s[1] for s in specs}) != len(specs) or len({s[3] for s in specs}) != len(specs):
        raise RuntimeError('Data and media require different UUIDs and mountpoints')
    return specs


def subvolume_of(record):
    """The Btrfs subvolume a drive is used with ("/" + path inside the filesystem, from
    `storage.<drive>.subvolume`), or None for the filesystem's default (and other types)."""
    value = record.get('subvolume')
    if value in (None, '', '/'):
        return None
    if record.get('fstype') != 'btrfs' or not isinstance(value, str) or not value.startswith('/') \
            or '..' in value.split('/') or any(c.isspace() or c == ',' for c in value):
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
        if record.get('fstype') != 'btrfs' or record.get('subvolume'):
            continue
        found = mounted_subvolume(config[mount_key], run)
        if found:
            record['subvolume'] = found


def validate_device(uuid, fstype):
    source = Path('/dev/disk/by-uuid') / uuid
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


# How long a drive may take to appear at boot before its mount is given up. Large hard
# drives, in USB enclosures or spinning up one after another, can take half a minute;
# 15 s (before 0.28.7) could leave the HDD unmounted, and Docker and every app with it.
# `nofail` keeps the rest of the boot from waiting for it.
MOUNT_OPTIONS = 'defaults,nofail,x-systemd.device-timeout=90s'
# Lines NekoServer wrote before, upgraded in place.
OLDER_OPTIONS = ('defaults,nofail,x-systemd.device-timeout=15s',)


def fstab_line(uuid, fstype, mountpoint, subvolume=None):
    if any(c.isspace() for c in mountpoint):
        raise RuntimeError('Mountpoint cannot contain whitespace')
    options = MOUNT_OPTIONS + (f',subvol={subvolume}' if subvolume else '')
    return f'UUID={uuid} {mountpoint} {fstype} {options} 0 2'


def uuid_of(source):
    """The filesystem UUID an fstab source names (LABEL=, PARTUUID=, PARTLABEL=, a /dev
    path), or None when it can't be found (a drive not connected)."""
    try:
        device = source if source.startswith('/dev/') else call(['findfs', source])
        return call(['blkid', '-s', 'UUID', '-o', 'value', device]) or None
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None


def fstab_folder(field):
    """An fstab mount folder as a plain path (\\040 is a space; /srv/x/ is /srv/x)."""
    return os.path.normpath(field.replace('\\040', ' '))


def same_drive(source, uuid, resolve=uuid_of):
    """An fstab source that names this drive, however it is written."""
    plain = source.replace('"', '')
    if plain in (f'UUID={uuid}', f'/dev/disk/by-uuid/{uuid}'):
        return True
    if plain.startswith('UUID='):
        return False
    return plain.startswith(('LABEL=', 'PARTUUID=', 'PARTLABEL=', '/dev/')) and resolve(plain) == uuid


def existing_line(lines, uuid, mountpoint, entry, resolve=uuid_of):
    """The index of the fstab line already mounting this drive or folder, None when there
    is none. One NekoServer wrote with older options may be brought up to date; any
    other line for the same drive or folder (however its drive or folder is written)
    stops setup: two lines for one folder would mount whichever comes first."""
    wanted = entry.split()
    folder = fstab_folder(mountpoint)
    matches = [(i, s.split()) for i, s in enumerate(lines)
               if s.strip() and not s.lstrip().startswith('#') and len(s.split()) >= 2
               and (fstab_folder(s.split()[1]) == folder or same_drive(s.split()[0], uuid, resolve))]
    if not matches:
        return None
    index, found = matches[0]
    # Older options are upgraded; with a subvolume the line must carry the same one. A
    # line of NekoServer's without one mounted the filesystem's default tree: once that
    # tree is known (and saved as the subvolume), the line is pinned to it.
    older = tuple(o + wanted[3][len(MOUNT_OPTIONS):] for o in OLDER_OPTIONS)
    default = (MOUNT_OPTIONS, *OLDER_OPTIONS) if wanted[3] != MOUNT_OPTIONS else ()
    if len(matches) != 1 or found[:3] != wanted[:3] or len(found) < 4 or found[3] not in (wanted[3], *older, *default):
        theirs = '\n'.join('    ' + lines[i].strip() for i, _ in matches)
        raise RuntimeError(f'/etc/fstab already has a line for this drive (UUID {uuid}) or for {mountpoint}, '
                           f'which NekoServer did not write:\n{theirs}\nNekoServer mounts the drive itself at '
                           f'{mountpoint} (with options that let the PC start even when the drive is missing). '
                           f'Take that line out of /etc/fstab, unmount the drive if it is mounted there '
                           f'(sudo umount <folder>), and run  sudo bash install.sh  again.')
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


def install_fstab(specs, path=Path('/etc/fstab')):
    """Add (or bring up to date) the lines that mount the SSD and HDD at boot. A line
    for the same drive or folder that NekoServer didn't write stops setup instead."""
    path = safe_path(path)
    existing = path.read_text() if path.exists() else ''
    lines = existing.splitlines()
    additions, upgraded = [], False
    for _, uuid, fstype, mountpoint, subvolume in specs:
        entry = fstab_line(uuid, fstype, mountpoint, subvolume)
        index = existing_line(lines, uuid, mountpoint, entry)
        if index is None:
            additions.append(entry)
        elif lines[index].split()[3] != entry.split()[3]:
            lines[index] = entry
            upgraded = True
    if not additions and not upgraded:
        return False
    backup = safe_path(path.with_name(path.name + '.nekoserver.bak'))
    # The first backup is the fstab as it was before NekoServer: kept, never replaced.
    if path.exists() and not backup.exists():
        keep_first(path, backup)
    payload = '\n'.join(lines).rstrip('\n') + '\n' + ''.join(a + '\n' for a in additions)
    atomic_write(path,payload,0o644)
    return True


# Not drives anyone plugs in: snap images and other files attached as disks, swap in
# memory, RAM disks, network block devices, CD drives.
NOT_DRIVES = re.compile(r'(loop|zram|ram|nbd|sr)\d')


def disk_line(inventory):
    """One line naming this PC's drives (for the log), without loop devices, swap in
    memory and anything under 1 GB, which only push the drive question off the screen."""
    found = [f"{d.get('name')} {size_words(d.get('size'))}" + (f" {str(d['model']).strip()}" if d.get('model') else '')
             + (' (Linux runs from it)' if holds_system(d) else '')
             for d in inventory.get('blockdevices', [])
             if not NOT_DRIVES.match(str(d.get('name', ''))) and (size_bytes(d.get('size')) or 0) >= GIB]
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
    resolve_storage_identity(context.config, inventory, apply=True, interactive=sys.stdin.isatty(), out_fn=context.log)
    remember_subvolumes(context.config)
    specs = mount_specs(context.config)
    for _, uuid, fstype, target, _ in specs:
        validate_device(uuid, fstype)  # raises if the drive identity is wrong
        current = findmnt('SOURCE', target)
        if current is not None:
            mounted = current.split('[')[0]
            if mounted_filesystem(target) != (uuid, fstype):
                raise RuntimeError(f'Wrong filesystem already mounted at {target}: {mounted}')
            only_a_folder(target, fstype, mounted_subvolume(target))
        elif Path(target).exists() and any(Path(target).iterdir()):
            raise RuntimeError(f'Refusing mount over nonempty directory {target}: something is already in that folder '
                               f'(on the system disk). Move it elsewhere, or pick another drive, and run setup again')
    # Saved for unattended reruns only once the choice passed these checks: a wrong pick
    # is asked again on the next run instead of being kept.
    save_storage(context)
    mount_all(context, specs)
    choose_usb(context)


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


def mount_all(context, specs, fstab=Path('/etc/fstab')):
    """fstab written, the drives mounted and checked. A Btrfs drive mounted for the first
    time without a saved subvolume mounts the filesystem's default tree, which can be a
    subvolume (`btrfs subvolume set-default`, `/@data` say): that tree is saved as the
    drive's subvolume and its fstab line pinned to it, so a restart (or a later change of
    the default) mounts the same files, and the next run finds the line its own."""
    install_fstab(specs, fstab)
    call(['systemctl', 'daemon-reload'])
    pinned = []
    for key, uuid, fstype, target, subvolume in specs:
        found = mount_and_check(uuid, target, subvolume, fstype)
        if found != subvolume:
            context.config['storage'][key]['subvolume'] = found
            pinned.append((key, uuid, fstype, target, found))
            context.log(f'{target}: the drive\'s default Btrfs subvolume is {found}; saved, so it is always mounted')
        context.log(f'Mounted UUID={uuid} on {target}; fstab configured for reboot')
    if pinned:
        save_storage(context)
        install_fstab(mount_specs(context.config), fstab)
        call(['systemctl', 'daemon-reload'])


MOUNT_WAIT = 600


def only_a_folder(target, fstype, inside):
    """Only Btrfs has subvolumes. On an ext4 or XFS drive, findmnt showing a folder
    (/dev/sdb1[/films]) means a folder of the drive is mounted there (a bind mount), not
    the whole drive: the apps would see only that folder, and a restart would mount the
    whole drive instead. Setup stops and says so."""
    if fstype != 'btrfs' and inside:
        raise RuntimeError(f'Only the folder {inside} of the drive is mounted at {target}, not the whole drive. '
                           f'Unmount it (sudo umount {target}, and take out its line in /etc/fstab if you added one), '
                           'then run  sudo bash install.sh  again: setup mounts the whole drive there itself')


def mount_and_check(uuid, target, subvolume, fstype='btrfs'):
    """Mount the drive (unless it is) and make sure what is mounted is the drive, and the
    Btrfs subvolume, the settings name. Returns the subvolume mounted: with none saved
    (the fstab line names none) that is the filesystem's default tree, whatever it is;
    always None for a drive that is not Btrfs."""
    Path(target).mkdir(parents=True, exist_ok=True)
    if findmnt('TARGET', target) is None:
        try:
            # A big drive replaying its journal after a power cut can take minutes.
            call(['mount', target], timeout=MOUNT_WAIT)
        except subprocess.TimeoutExpired:
            if findmnt('TARGET', target) is None:   # the kernel may still have finished it
                raise RuntimeError(f'Mounting {target} took longer than {MOUNT_WAIT // 60} minutes; check the drive '
                                   '(sudo dmesg shows what it is doing) and run setup again') from None
    if mounted_filesystem(target) != (uuid, fstype):
        raise RuntimeError(f'Post-mount validation failed for {target}')
    found = mounted_subvolume(target)
    only_a_folder(target, fstype, found)
    if subvolume is not None and found != subvolume:
        raise RuntimeError(f'{target} is mounted with Btrfs subvolume {found or "(default)"}, but the settings say '
                           f'{subvolume}; a restart would mount a different tree')
    return found


def choose_usb(context):
    """Pick the USB backup drive with the other drives, so the space check covers it."""
    photos = context.config.setdefault('photos', {})
    if photos.get('external_backup_uuid') or not sys.stdin.isatty():
        return
    from modules.photo_backup import select_external
    inventory = inventory_now()
    context.log('Now the USB backup drive (it gets a weekly copy of your photos and Nextcloud files):')
    try:
        chosen = select_external(context.config, inventory, out_fn=context.log)
    except RuntimeError as exc:
        context.log(f'USB backup drive not chosen yet ({exc}); you will be asked again at the weekly backup step')
        return
    if chosen is None:
        context.log('USB backup drive skipped for now; you will be asked again at the weekly backup step')
        return
    if hasattr(context, 'persist_config'):
        context.persist_config()


def mounted_filesystem(target):
    """(UUID, filesystem type) of what is mounted at target, or None, read from the
    filesystem itself as identity_matches does: findmnt's UUID column, or blkid of the
    mounted device when that column is empty. Never by comparing device paths: every
    device of a Btrfs pool carries the pool's UUID, findmnt names one of them (the lowest
    device ID) as the source, and /dev/disk/by-uuid may point at another."""
    fields = (findmnt('UUID,FSTYPE,SOURCE', target) or '').split()
    if len(fields) == 3:
        return fields[0], fields[1]
    if len(fields) == 2 and fields[1].startswith('/dev/'):
        return uuid_of(fields[1].split('[')[0]), fields[0]
    return None
