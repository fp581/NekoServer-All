"""Install weekly systemd photo backup, run as root, fail closed if USB missing."""
import native_runtime
import os, re, subprocess, sys, json
from pathlib import Path
from neko_common import VERSION, safe_path, mount_path, relative_path, atomic_write, atomic_json, host_timezone
DEPENDENCIES=['storage','photo_preservation']
DESCRIPTION='Install weekly add-only external photo backup timer (requires --apply and external UUID)'

def select_external(config, inventory, input_fn=input, out_fn=print):
    """Explicitly identify a formatted external filesystem; never guess or format."""
    forbidden={config.get('storage',{}).get(k,{}).get('uuid') for k in ('data_ssd','media_hdd')}
    candidates=[]
    def walk(row):
        uuid=row.get('uuid');fs=row.get('fstype');mount=row.get('mountpoint')
        if row.get('type') in ('disk','part') and uuid and uuid not in forbidden and fs in ('ext4','xfs','btrfs','exfat') and (not mount or mount==config['external_backup_mount'] or (isinstance(mount,str) and mount.startswith(('/run/media/','/media/')))):
            candidates.append(row)
        for child in row.get('children',[]):walk(child)
    for row in inventory.get('blockdevices',[]):walk(row)
    if len({v['uuid'] for v in candidates})!=len(candidates):raise RuntimeError('Duplicate external filesystem UUID; identify cloned drives first')
    if not candidates:raise RuntimeError('No eligible formatted external backup filesystem; connect it first')
    for index,row in enumerate(candidates,1):out_fn(f"  {index}) /dev/{row['name']} {row.get('size','')} {row['fstype']} UUID={row['uuid']} mounted={row.get('mountpoint') or '-'}")
    answer=input_fn('Choose the USB backup drive by number (Enter to skip for now): ').strip()
    if not answer.isdigit() or not 1<=int(answer)<=len(candidates):raise RuntimeError('External backup selection aborted')
    chosen=candidates[int(answer)-1]
    if input_fn(f"Type {chosen['name']} to confirm it is the USB backup drive: ").strip() not in (chosen['name'],'/dev/'+chosen['name'],chosen['uuid']):raise RuntimeError('External backup drive confirmation failed')
    config.setdefault('photos',{}).update(external_backup_uuid=chosen['uuid'],external_backup_fstype=chosen['fstype'])
    return chosen

def run(ctx):
    opts=ctx.config.setdefault('photos',{})
    if not ctx.apply:
        ctx.log('Weekly photo backup available; --apply installs timer with external UUID.');return
    if os.geteuid()!=0: raise RuntimeError('Root required')
    uuid=opts.get('external_backup_uuid','').strip()
    if not uuid:
        if not sys.stdin.isatty():
            return {'status':'needs_setup','reason':'Connect the external photo disk and rerun photo_backup interactively to confirm its UUID'}
        from modules.storage import inventory_now
        select_external(ctx.config,inventory_now(),out_fn=ctx.log)
        uuid=opts['external_backup_uuid']
        if hasattr(ctx,'persist_config'):
            ctx.persist_config()
        elif getattr(ctx,'config_path',None):
            saved=json.loads(ctx.config_path.read_text());saved['photos']=opts;atomic_json(ctx.config_path,saved)
    if not re.fullmatch(r'[A-Za-z0-9-]{8,80}',uuid): raise RuntimeError('Set photos.external_backup_uuid from lsblk -f')
    fstype=opts.get('external_backup_fstype','ext4')
    if fstype not in ('ext4','xfs','btrfs','exfat'): raise RuntimeError('Unexpected backup filesystem')
    for key in ('data_ssd','media_hdd'):
        if uuid==ctx.config.get('storage',{}).get(key,{}).get('uuid'): raise RuntimeError('USB UUID must differ from SSD/HDD')
    source=safe_path(Path(ctx.config['media_mount'])/relative_path(opts.get('archive_dir','photos/archive')))
    if source.is_symlink() or not source.is_dir(): raise RuntimeError(f'Archive folder missing: {source}')
    dest=mount_path(ctx.config['external_backup_mount'])
    if not dest.is_absolute() or str(dest) in ('/','/home','/mnt','/srv') or dest.is_symlink(): raise RuntimeError('Unsafe USB destination')
    when=opts.get('backup_time','03:00');day=opts.get('backup_weekday','Sun')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',when) or day not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'): raise RuntimeError('Invalid schedule')
    unit=Path('/etc/systemd/system/nekoserver-photo-backup.service');timer=Path('/etc/systemd/system/nekoserver-photo-backup.timer')
    binary=native_runtime.deploy(Path(__file__).resolve().parents[1],ctx.config,Path(ctx.config['data_mount'])/'installer'/'runtime'/('v'+VERSION))
    command=str(binary)+' weekly --config '+str(native_runtime.backup_guard(ctx.config))
    cmd=f'{command} --uuid {uuid} --fstype {fstype} --source {source} --dest {dest} --media-root {ctx.config["media_mount"]} --data {ctx.config["data_mount"]}'
    if ctx.config.get('nextcloud',{}).get('enabled',False):
        # Nextcloud user files get the same add-only weekly copy as photos.
        cmd+=' --cloud '+str(Path(ctx.config['media_mount'])/'cloud/files')
    service='[Unit]\nDescription=NekoServer add-only USB backup of photos and Nextcloud files\nRequiresMountsFor='+ctx.config['media_mount']+'\nAfter=nekoserver-immich-db-backup.service\n[Service]\nType=oneshot\nExecStart='+cmd+'\n'
    schedule='[Unit]\nDescription=Weekly NekoServer photo backup\n[Timer]\nOnCalendar='+day+' *-*-* '+when+':00 '+host_timezone(ctx.config)+'\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
    for path,text in ((unit,service),(timer,schedule)):
        if path.exists() and path.read_text()!=text and 'NekoServer' not in path.read_text() and 'nekoserver-' not in path.read_text(): raise RuntimeError(f'Unmanaged systemd unit differs: {path}')
    atomic_write(unit,service,0o644);atomic_write(timer,schedule,0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',timer.name],check=True)
    ctx.log('Enabled weekly USB copy of Immich photos/videos'+(' and Nextcloud files' if '--cloud' in cmd else '')+'; when the USB drive is absent the run fails visibly instead of writing to the OS disk')
