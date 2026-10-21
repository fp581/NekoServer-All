"""Safe existing-filesystem mounting by explicitly configured UUID; never formats."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from neko_common import mount_path, atomic_json, atomic_write, safe_path

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
            if not options: raise RuntimeError(f'No eligible formatted Linux filesystem for {label}; formatting is never automatic')
            out_fn(f'Select {label}:')
            for n,v in enumerate(options,1): out_fn(f"  {n}) /dev/{v['name']} {v['size']} {v['fstype']} UUID={v['uuid']} mounted={v['mountpoint'] or '-'}")
            choice=input_fn('Choose number (or ENTER to abort): ').strip()
            if not choice.isdigit() or not 1<=int(choice)<=len(options): raise RuntimeError('Selection aborted')
            device=options[int(choice)-1]
            confirm=input_fn(f"Type {device['name']} to confirm it is {label}: ").strip()
            if confirm not in (device['name'],'/dev/'+device['name'],device['uuid']): raise RuntimeError('Drive confirmation failed')
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
        specs.append((key, uuid, fstype, target))
    if Path(specs[0][3]) in Path(specs[1][3]).parents or Path(specs[1][3]) in Path(specs[0][3]).parents:
        raise RuntimeError('Data and media mountpoints must not be nested')
    if len({s[1] for s in specs}) != len(specs) or len({s[3] for s in specs}) != len(specs):
        raise RuntimeError('Data and media require different UUIDs and mountpoints')
    return specs


def validate_device(uuid, fstype):
    source = Path('/dev/disk/by-uuid') / uuid
    if not source.exists():
        raise RuntimeError(f'Drive UUID {uuid} not found; connect the disk and use lsblk -f')
    actual_type = call(['blkid', '-s', 'TYPE', '-o', 'value', str(source)])
    if actual_type != fstype:
        raise RuntimeError(f'Filesystem mismatch for {uuid}: actual={actual_type}, configured={fstype}')
    real = source.resolve()
    host_source = call(['findmnt', '-n', '-o', 'SOURCE', '--target', '/']).split('[')[0]
    host_block = Path(host_source)
    # Refuse using the exact OS filesystem even when identified by alternate device path.
    if host_block.exists() and host_block.resolve() == real:
        raise RuntimeError(f'UUID {uuid} belongs to the OS filesystem')
    return str(source)


def fstab_line(uuid, fstype, mountpoint):
    if any(c.isspace() for c in mountpoint):
        raise RuntimeError('Mountpoint cannot contain whitespace')
    return f'UUID={uuid} {mountpoint} {fstype} defaults,nofail,x-systemd.device-timeout=15s 0 2'


def install_fstab(specs, path=Path('/etc/fstab')):
    path = safe_path(path)
    existing = path.read_text() if path.exists() else ''
    lines = existing.splitlines()
    additions = []
    for _, uuid, fstype, mountpoint in specs:
        entry = fstab_line(uuid, fstype, mountpoint)
        active = [s.split() for s in lines if s.strip() and not s.lstrip().startswith('#')]
        matches = [s for s in active if len(s) >= 2 and (s[0] == f'UUID={uuid}' or s[1] == mountpoint)]
        if matches:
            if len(matches) != 1 or ' '.join(matches[0][:4]) != ' '.join(entry.split()[:4]):
                raise RuntimeError(f'Conflicting existing fstab entry for {mountpoint} or UUID {uuid}')
        else:
            additions.append(entry)
    if not additions:
        return False
    backup = safe_path(path.with_name(path.name + '.nekoserver.bak'))
    if backup.exists():
        raise RuntimeError(f'Existing fstab backup {backup}; inspect before modifying')
    if path.exists():
        with backup.open('xb') as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(path.read_bytes());stream.flush();os.fsync(stream.fileno())
    payload = existing.rstrip('\n') + '\n' + '\n'.join(additions) + '\n'
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
    for _, uuid, fstype, target in specs:
        validate_device(uuid, fstype)  # raises if the drive identity is wrong
        current = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', target], capture_output=True, text=True, check=False)
        if current.returncode == 0:
            mounted = current.stdout.strip().split('[')[0]
            if not Path(mounted).exists() or Path(mounted).resolve() != source_path(uuid):
                raise RuntimeError(f'Wrong filesystem already mounted at {target}: {mounted}')
        elif Path(target).exists() and any(Path(target).iterdir()):
            raise RuntimeError(f'Refusing mount over nonempty directory {target}')
    install_fstab(specs)
    call(['systemctl', 'daemon-reload'])
    for _, uuid, _, target in specs:
        Path(target).mkdir(parents=True, exist_ok=True)
        if subprocess.run(['findmnt', '-n', '--mountpoint', target], stdout=subprocess.DEVNULL, check=False).returncode != 0:
            call(['mount', target])
        current = call(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', target]).split('[')[0]
        if not Path(current).exists() or Path(current).resolve() != source_path(uuid):
            raise RuntimeError(f'Post-mount validation failed for {target}')
        context.log(f'Mounted UUID={uuid} on {target}; fstab configured for reboot')
    choose_usb(context)


def choose_usb(context):
    """Pick the USB backup drive with the other drives, so the space check covers it."""
    photos = context.config.setdefault('photos', {})
    if photos.get('external_backup_uuid') or not sys.stdin.isatty():
        return
    from modules.photo_backup import select_external
    inventory = inventory_now()
    context.log('Now the USB backup drive (it gets a weekly copy of your photos and Nextcloud files):')
    try:
        select_external(context.config, inventory, out_fn=context.log)
    except RuntimeError as exc:
        context.log(f'USB backup drive not chosen yet ({exc}); you will be asked again at the weekly backup step')
        return
    if hasattr(context, 'persist_config'):
        context.persist_config()


def source_path(uuid):
    return (Path('/dev/disk/by-uuid') / uuid).resolve()
