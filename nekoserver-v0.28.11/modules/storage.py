"""Safe existing-filesystem mounting by explicitly configured UUID; never formats."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from neko_common import mount_path, atomic_json, atomic_write, findmnt, pick_drive, safe_path

DEPENDENCIES = ['system']
DESCRIPTION = 'Inspect disks; --apply mounts explicitly identified data SSD and media HDD by UUID'


def call(argv):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    if p.returncode:
        raise RuntimeError(f'{argv[0]} failed: {p.stderr.strip()[:400]}')
    return p.stdout.strip()


def inventory_now():
    """Every disk and partition, as lsblk JSON (one command, used by all drive pickers)."""
    return json.loads(call(['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINT,MODEL']))


def discover_filesystems(inventory):
    """Return candidates, never auto-select a device on model/size alone."""
    candidates=[]
    os_disks=set()
    def walk(dev, parent='', disk=None):
        name=dev.get('name',''); kind=dev.get('type',''); uuid=dev.get('uuid')
        physical=name if kind=='disk' else disk
        fs=dev.get('fstype'); mp=dev.get('mountpoint')
        if mp in ('/','/boot','/boot/efi') and physical:os_disks.add(physical)
        if kind in ('part','disk') and uuid and fs in ('ext4','xfs','btrfs') and mp not in ('/','/boot','/boot/efi'):
            candidates.append({'uuid':uuid, 'fstype':fs,'name':name,'size':dev.get('size'),'model':dev.get('model') or parent,'mountpoint':mp,'physical_disk':physical})
        for child in dev.get('children',[]): walk(child,dev.get('model') or parent,physical)
    for device in inventory.get('blockdevices',[]): walk(device)
    if len({x['uuid'] for x in candidates}) != len(candidates):
        raise RuntimeError('Duplicate filesystem UUID detected; identify cloned drives before continuing')
    return [v for v in candidates if v['physical_disk'] not in os_disks]


NOT_LINUX = {'ntfs': 'NTFS (Windows)', 'exfat': 'exFAT', 'vfat': 'FAT'}


def nothing_eligible(inventory, items, selected, label):
    """Why no drive can be picked for `label`, drive by drive, and what to do."""
    lines = []
    def system_disk(dev):
        return dev.get('mountpoint') == '/' or any(system_disk(child) for child in dev.get('children', []))
    def walk(dev):
        if dev.get('type') in ('part', 'disk') and dev.get('fstype') in NOT_LINUX:
            fs = dev['fstype']
            lines.append(f"/dev/{dev.get('name')} is formatted {NOT_LINUX[fs]}, which Linux apps can't keep their files "
                         'on. To use it, copy its files elsewhere first, then format it as ext4 (this erases it) with '
                         'the Disks app (GNOME) or KDE Partition Manager.')
        for child in dev.get('children', []):
            walk(child)
    for device in inventory.get('blockdevices', []):
        if not system_disk(device):   # its boot partition (FAT) is never a choice
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


def resolve_storage_identity(config, inventory, apply=False, interactive=False, input_fn=input, out_fn=print):
    """Identify disks by verified UUID. Explicit confirmation is required for unknown devices."""
    items=discover_filesystems(inventory)
    configured=config.setdefault('storage',{})
    selected=set()
    selected_disks=set()
    for key, label in [('data_ssd','the SSD (apps, photos and music)'),('media_hdd','the HDD (movies, shows, Nextcloud files and backups)')]:
        record=configured.setdefault(key,{})
        uuid=record.get('uuid','').strip()
        if uuid:
            matches=[d for d in items if d['uuid']==uuid]
            if len(matches)!=1: raise RuntimeError(f'Configured {key} UUID not present or not supported: {uuid}')
            if record.get('fstype') and record['fstype']!=matches[0]['fstype']:
                raise RuntimeError(f'Filesystem type mismatch for {key}')
            record['fstype']=matches[0]['fstype']
        elif apply:
            if not interactive: raise RuntimeError(f'{key} is unassigned; rerun interactively on the server to select it safely')
            # A drive the desktop opened by itself (/run/media/...) is still eligible.
            options=[v for v in items if v['uuid'] not in selected and (not v['mountpoint'] or v['mountpoint']==config.get('data_mount' if key=='data_ssd' else 'media_mount') or str(v['mountpoint']).startswith(('/run/media/','/media/')))]
            if not options: raise RuntimeError(nothing_eligible(inventory, items, selected, label))
            out_fn(f'Select {label}:')
            device=pick_drive(options, label, input_fn, out_fn)
            record['uuid']=device['uuid']; record['fstype']=device['fstype']
            uuid=device['uuid']
        else:
            out_fn(f'{key} is not configured; {len(items)} formatted candidates discovered (dry-run only)')
            continue
        if uuid in selected: raise RuntimeError('SSD and HDD cannot use the same filesystem')
        physical=next(v['physical_disk'] for v in items if v['uuid']==uuid)
        if physical:
            if physical in selected_disks:raise RuntimeError('SSD and HDD must be on different physical disks')
            selected_disks.add(physical)
        elif apply:
            raise RuntimeError('Cannot verify the physical parent disk; inspect full lsblk topology first')
        selected.add(uuid)
    return configured


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
    # Refuse using the exact OS filesystem even when identified by alternate device path.
    if host_block.exists() and host_block.resolve() == real:
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


def existing_line(lines, uuid, mountpoint, entry):
    """The index of the fstab line already mounting this drive or folder, None when there
    is none. One NekoServer wrote with older options may be brought up to date; any
    other line for the same drive or folder stops setup."""
    wanted = entry.split()
    matches = [(i, s.split()) for i, s in enumerate(lines)
               if s.strip() and not s.lstrip().startswith('#') and len(s.split()) >= 2
               and (s.split()[0] == f'UUID={uuid}' or s.split()[1] == mountpoint)]
    if not matches:
        return None
    index, found = matches[0]
    # Older options are upgraded; with a subvolume the line must carry the same one.
    older = tuple(o + wanted[3][len(MOUNT_OPTIONS):] for o in OLDER_OPTIONS)
    if len(matches) != 1 or found[:3] != wanted[:3] or len(found) < 4 or found[3] not in (wanted[3], *older):
        theirs = '\n'.join('    ' + lines[i].strip() for i, _ in matches)
        raise RuntimeError(f'/etc/fstab already has a line for this drive (UUID {uuid}) or for {mountpoint}, '
                           f'which NekoServer did not write:\n{theirs}\nNekoServer mounts the drive itself at '
                           f'{mountpoint} (with options that let the PC start even when the drive is missing). '
                           f'Take that line out of /etc/fstab, unmount the drive if it is mounted there '
                           f'(sudo umount <folder>), and run  sudo bash install.sh  again.')
    return index


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
        with backup.open('xb') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(path.read_bytes());stream.flush();os.fsync(stream.fileno())
    payload = '\n'.join(lines).rstrip('\n') + '\n' + ''.join(a + '\n' for a in additions)
    atomic_write(path,payload,0o644)
    return True


def run(context):
    inventory = inventory_now()
    for disk in inventory.get('blockdevices', []):
        context.log(f"Device {disk.get('name')}: {disk.get('size')} / {disk.get('model') or 'unknown'}")
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
    # Persist selected identities in a root-owned config for unattended reruns.
    config_path = context.config_path
    # The controller may supply a working config without a writable config file.
    if hasattr(context,'persist_config'):
        context.persist_config()
    elif config_path is not None and config_path.is_file():
        payload=json.loads(config_path.read_text())
        payload['storage']=context.config['storage']
        atomic_json(config_path,payload)
    specs = mount_specs(context.config)
    for _, uuid, fstype, target, _ in specs:
        validate_device(uuid, fstype)  # raises if the drive identity is wrong
        current = findmnt('SOURCE', target)
        if current is not None:
            mounted = current.split('[')[0]
            if not Path(mounted).exists() or Path(mounted).resolve() != source_path(uuid):
                raise RuntimeError(f'Wrong filesystem already mounted at {target}: {mounted}')
        elif Path(target).exists() and any(Path(target).iterdir()):
            raise RuntimeError(f'Refusing mount over nonempty directory {target}')
    install_fstab(specs)
    call(['systemctl', 'daemon-reload'])
    for _, uuid, _, target, subvolume in specs:
        mount_and_check(uuid, target, subvolume)
        context.log(f'Mounted UUID={uuid} on {target}; fstab configured for reboot')
    choose_usb(context)


def mount_and_check(uuid, target, subvolume):
    """Mount the drive (unless it is) and make sure what is mounted is the drive, and the
    Btrfs subvolume, the settings name."""
    Path(target).mkdir(parents=True, exist_ok=True)
    if findmnt('TARGET', target) is None:
        call(['mount', target])
    current = (findmnt('SOURCE', target) or '').split('[')[0]
    if not Path(current).exists() or Path(current).resolve() != source_path(uuid):
        raise RuntimeError(f'Post-mount validation failed for {target}')
    found = mounted_subvolume(target)
    if found != subvolume:
        raise RuntimeError(f'{target} is mounted with Btrfs subvolume {found or "(default)"}, but the settings say '
                           f'{subvolume or "(default)"}; a restart would mount a different tree')


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


def source_path(uuid):
    return (Path('/dev/disk/by-uuid') / uuid).resolve()
