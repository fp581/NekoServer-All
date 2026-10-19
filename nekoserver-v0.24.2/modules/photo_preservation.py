"""Install combined weekly photo preservation, retiring old frequent archive timers."""
import native_runtime
import os,subprocess
from neko_common import VERSION
from pathlib import Path
from neko_common import safe_path, relative_path, atomic_write, host_timezone
DEPENDENCIES=['photos']
DESCRIPTION='Weekly additive archival of SSD Immich originals and database backup'

def command_line(binary,guard,data,media,age,archive,keep):
    """The runtime command the timer runs (tests check it against the real program)."""
    return f'{binary} preservation --config {guard} --data {data} --media {media} --min-age {age} --archive-dir {archive} --db-keep {keep}'

def run(ctx):
    if not ctx.apply:ctx.log('Photo preservation: apply installs the configured weekly originals/database timer');return
    if os.geteuid()!=0:raise RuntimeError('Root required')
    cfg=ctx.config;data=Path(cfg['data_mount']);media=Path(cfg['media_mount']);opts=cfg['photos']
    for p in (data,media):
        if p.is_symlink() or subprocess.run(['findmnt','-n','--mountpoint',str(p)],stdout=subprocess.DEVNULL).returncode:raise RuntimeError(f'Missing mount: {p}')
    runtime=data/'installer'/'runtime'/('v'+VERSION)
    interval=opts.get('archive_interval_minutes',360);age=opts.get('archive_min_age_seconds',600)
    if not isinstance(interval,int) or interval<15 or not isinstance(age,int) or age<60:raise RuntimeError('Invalid archive interval/age')
    tm=opts.get('database_backup_time','02:15')
    import re
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',tm):raise RuntimeError('Invalid database backup time')
    archive=relative_path(opts.get('archive_dir','photos/archive'))
    binary=native_runtime.deploy(Path(__file__).resolve().parents[1],cfg,runtime)
    keep=opts.get('database_keep',8)
    if type(keep) is not int or not 0<=keep<=520:raise RuntimeError('photos.database_keep must be a whole number from 0 to 520')
    base=command_line(binary,native_runtime.backup_guard(cfg),data,media,age,archive,keep)
    schedule=opts.get('backup_schedule','legacy')
    if schedule not in ('legacy','weekly'):raise RuntimeError('Invalid photo backup schedule')
    day=opts.get('backup_weekday','Sun')
    if day not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'):raise RuntimeError('Invalid photo backup weekday')
    if schedule=='weekly':
        from modules._schedules import retire
        retire([Path('/etc/systemd/system')/('nekoserver-photo-preservation.'+suffix) for suffix in ('timer','service')],subprocess.run)
    services={
      'nekoserver-photo-preservation':(base,f'OnBootSec=10min\nOnUnitInactiveSec={interval}min\n'),
      'nekoserver-immich-db-backup':(base+' --db-backup',f'OnCalendar=*-*-* {tm}:00 {host_timezone(cfg)}\nPersistent=true\n')}
    if schedule=='weekly':services={'nekoserver-immich-db-backup':(base+' --db-backup',f'OnCalendar={day} *-*-* {tm}:00 {host_timezone(cfg)}\nPersistent=true\n')}
    # The DB job also archives to keep an identical fail-closed precondition.
    for name,(cmd,calendar) in services.items():
        service=Path('/etc/systemd/system')/(name+'.service');timer=Path('/etc/systemd/system')/(name+'.timer')
        service_text=f'[Unit]\nDescription={name}\nRequiresMountsFor={data} {media}\n[Service]\nType=oneshot\nExecStart={cmd}\n'
        timer_text=f'[Unit]\nDescription={name} timer\n[Timer]\n{calendar}\n[Install]\nWantedBy=timers.target\n'
        for target,content in ((service,service_text),(timer,timer_text)):
            content=content.replace('\\n','\n')
            if target.exists() and target.read_text()!=content:
                previous=target.read_text()
                # Require this unit's exact managed description before updating it.
                if f'Description={name}' not in previous:raise RuntimeError(f'Unmanaged existing unit: {target}')
            safe_path(target)
            atomic_write(target,content,0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    for name in services:subprocess.run(['systemctl','enable','--now',name+'.timer'],check=True)
    ctx.log('Enabled '+('weekly combined photo originals/database backup' if schedule=='weekly' else 'periodic archive and nightly SQL')+'; no deletions')
