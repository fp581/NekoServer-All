"""Install the weekly add-only USB copy of Immich's photos and Nextcloud's files (either
or both, whichever is set up), run as root, failing closed if the USB drive is missing."""
import native_runtime
import os, re, subprocess, sys, json
from pathlib import Path
from neko_common import VERSION, safe_path, mount_path, relative_path, atomic_write, atomic_json, host_timezone, SYSTEMD_UNITS
# The drives and their space check only: a server with Nextcloud and no Immich gets its
# USB copy too, and a photo step that failed doesn't hold back the Nextcloud copy. In a
# full setup it still runs after the photo steps (the profile's order).
DEPENDENCIES=['storage','capacity']
DESCRIPTION='Install the weekly add-only USB copy of photos and Nextcloud files (requires --apply and external UUID)'

SYSTEM_MOUNTS=('/','/boot','/boot/efi','/home','/var')
BACKUP_FILESYSTEMS=('ext4','xfs','btrfs','exfat')

def disk_layout(config, inventory):
    """([(filesystem row, its physical disk)], disks holding the system, the SSD or the HDD)."""
    forbidden={u for u in (config.get('storage',{}).get(k,{}).get('uuid') for k in ('data_ssd','media_hdd')) if u}
    rows=[];busy_disks=set()
    def walk(row,disk=None):
        physical=row.get('name') if row.get('type')=='disk' else disk
        if row.get('uuid') in forbidden or row.get('mountpoint') in SYSTEM_MOUNTS:busy_disks.add(physical)
        rows.append((row,physical))
        for child in row.get('children',[]):walk(child,physical)
    for row in inventory.get('blockdevices',[]):walk(row)
    return rows,busy_disks,forbidden

def eligible(config, inventory):
    """Filesystems that may hold the USB backup. It must be on its own physical disk: a
    partition of the system disk, the SSD or the media HDD would be lost together with
    what it is meant to protect."""
    rows,busy_disks,forbidden=disk_layout(config, inventory)
    def usable_mount(mount):
        return not mount or mount==config['external_backup_mount'] or (isinstance(mount,str) and mount.startswith(('/run/media/','/media/')))
    return [{**row,'physical_disk':physical} for row,physical in rows
            if row.get('type') in ('disk','part') and row.get('uuid') and row['uuid'] not in forbidden
            and row.get('fstype') in BACKUP_FILESYSTEMS and usable_mount(row.get('mountpoint'))
            and physical and physical not in busy_disks]

def shared_disk(config, inventory, uuid):
    """Why a saved USB drive cannot be used (it shares a disk with the system, the SSD
    or the HDD), or None. A drive that is not plugged in is checked by the weekly job."""
    rows,busy_disks,_=disk_layout(config, inventory)
    for row,physical in rows:
        if row.get('uuid')==uuid and physical in busy_disks:
            return (f'The saved USB backup drive (UUID {uuid}) is a partition of /dev/{physical}, which also holds '
                    'the system, the SSD or the media HDD. One disk failure would lose both copies. '
                    'Choose a separate drive: remove "external_backup_uuid" under "photos" in your settings and run setup again.')
    return None

def select_external(config, inventory, input_fn=input, out_fn=print):
    """Explicitly identify a formatted external filesystem; never guess or format."""
    candidates=eligible(config, inventory)
    if len({v['uuid'] for v in candidates})!=len(candidates):raise RuntimeError('Duplicate external filesystem UUID; identify cloned drives first')
    if not candidates:raise RuntimeError('No eligible backup drive: it must be a formatted drive of its own, not a partition of the system disk, the SSD or the media HDD; connect it first')
    for index,row in enumerate(candidates,1):out_fn(f"  {index}) /dev/{row['name']} {row.get('size','')} {row['fstype']} UUID={row['uuid']} mounted={row.get('mountpoint') or '-'}")
    answer=input_fn('Choose the USB backup drive by number (Enter to skip for now): ').strip()
    if not answer.isdigit() or not 1<=int(answer)<=len(candidates):raise RuntimeError('External backup selection aborted')
    chosen=candidates[int(answer)-1]
    if input_fn(f"Type {chosen['name']} to confirm it is the USB backup drive: ").strip() not in (chosen['name'],'/dev/'+chosen['name'],chosen['uuid']):raise RuntimeError('External backup drive confirmation failed')
    config.setdefault('photos',{}).update(external_backup_uuid=chosen['uuid'],external_backup_fstype=chosen['fstype'])
    return chosen

def copies(config):
    """What the weekly USB copy takes: (Immich's photo archive, Nextcloud's files)."""
    return (bool(config.get('photos',{}).get('deploy_immich',False)),
            bool(config.get('nextcloud',{}).get('enabled',False)))

def command_line(config,binary,guard,uuid,fstype,source,dest):
    """The runtime command the weekly timer runs (tests check it against the real program).
    `source`: the photo archive, or None when Immich isn't set up."""
    cmd=f'{binary} weekly --config {guard} --uuid {uuid} --fstype {fstype}'+(f' --source {source}' if source else '')+f' --dest {dest} --media-root {config["media_mount"]} --data {config["data_mount"]}'
    if copies(config)[1]:
        # Nextcloud user files get the same add-only weekly copy as photos.
        cmd+=' --cloud '+str(Path(config['media_mount'])/'cloud/files')
    return cmd

def check_drive(config,uuid,fstype):
    """The saved USB drive is a disk of its own, of a filesystem it may use."""
    from modules.storage import inventory_now
    problem=shared_disk(config,inventory_now(),uuid)
    if problem: raise RuntimeError(problem)
    if fstype not in BACKUP_FILESYSTEMS: raise RuntimeError('Unexpected backup filesystem')
    if uuid in {config.get('storage',{}).get(key,{}).get('uuid') for key in ('data_ssd','media_hdd')}:
        raise RuntimeError('USB UUID must differ from SSD/HDD')

def chosen_drive(ctx,opts):
    """The USB drive's UUID: the saved one, or the one you pick now (saved for next
    time). None when it has to be picked and nobody is at the keyboard."""
    uuid=opts.get('external_backup_uuid','').strip()
    if uuid:return uuid
    if not sys.stdin.isatty():return None
    from modules.storage import inventory_now
    select_external(ctx.config,inventory_now(),out_fn=ctx.log)
    if hasattr(ctx,'persist_config'):
        ctx.persist_config()
    elif getattr(ctx,'config_path',None):
        saved=json.loads(ctx.config_path.read_text());saved['photos']=opts;atomic_json(ctx.config_path,saved)
    return opts['external_backup_uuid']

def run(ctx):
    opts=ctx.config.setdefault('photos',{})
    if not ctx.apply:
        ctx.log('Weekly photo backup available; --apply installs timer with external UUID.');return
    if os.geteuid()!=0: raise RuntimeError('Root required')
    photos,cloud=copies(ctx.config)
    if not (photos or cloud):
        ctx.log('USB backup: neither Immich nor Nextcloud is set up, so there is nothing for it to copy')
        return {'status':'unavailable','reason':'Nothing to copy: Immich and Nextcloud are both switched off'}
    uuid=chosen_drive(ctx,opts)
    if uuid is None:
        return {'status':'needs_setup','reason':'Connect the external photo disk and rerun photo_backup interactively to confirm its UUID'}
    if not re.fullmatch(r'[A-Za-z0-9-]{8,80}',uuid): raise RuntimeError('Set photos.external_backup_uuid from lsblk -f')
    fstype=opts.get('external_backup_fstype','ext4')
    check_drive(ctx.config,uuid,fstype)
    source=None
    if photos:
        source=safe_path(Path(ctx.config['media_mount'])/relative_path(opts.get('archive_dir','photos/archive')))
        if source.is_symlink() or not source.is_dir(): raise RuntimeError(f'Archive folder missing: {source}')
    dest=mount_path(ctx.config['external_backup_mount'])
    if not dest.is_absolute() or str(dest) in ('/','/home','/mnt','/srv') or dest.is_symlink(): raise RuntimeError('Unsafe USB destination')
    when=opts.get('backup_time','03:00');day=opts.get('backup_weekday','Sun')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',when) or day not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'): raise RuntimeError('Invalid schedule')
    unit=SYSTEMD_UNITS/'nekoserver-photo-backup.service';timer=SYSTEMD_UNITS/'nekoserver-photo-backup.timer'
    binary=native_runtime.deploy(Path(__file__).resolve().parents[1],ctx.config,Path(ctx.config['data_mount'])/'installer'/'runtime'/('v'+VERSION))
    cmd=command_line(ctx.config,binary,native_runtime.backup_guard(ctx.config),uuid,fstype,source,dest)
    service='[Unit]\nDescription=NekoServer add-only USB backup of photos and Nextcloud files\nRequiresMountsFor='+ctx.config['media_mount']+'\n'+('After=nekoserver-immich-db-backup.service\n' if photos else '')+'[Service]\nType=oneshot\nExecStart='+cmd+'\n'
    schedule='[Unit]\nDescription=Weekly NekoServer photo backup\n[Timer]\nOnCalendar='+day+' *-*-* '+when+':00 '+host_timezone(ctx.config)+'\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
    for path,text in ((unit,service),(timer,schedule)):
        if path.exists() and path.read_text()!=text and 'NekoServer' not in path.read_text() and 'nekoserver-' not in path.read_text(): raise RuntimeError(f'Unmanaged systemd unit differs: {path}')
    atomic_write(unit,service,0o644);atomic_write(timer,schedule,0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',timer.name],check=True)
    what=' and '.join(name for name,on in (('Immich photos/videos',photos),('Nextcloud files',cloud)) if on)
    ctx.log('Enabled weekly USB copy of '+what+'; when the USB drive is absent the run fails visibly instead of writing to the OS disk')
