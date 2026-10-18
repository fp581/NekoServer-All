"""Local visual health dashboard with a privileged collector and unprivileged web server."""
import native_runtime
from acceptance_summary import scope_digest
import importlib.util,os,platform,shutil,subprocess
from pathlib import Path
from neko_common import VERSION,safe_path,mount_path,atomic_write,atomic_json
DEPENDENCIES=['storage','recovery']
DESCRIPTION='Install the localhost visual dashboard, read-only collector and local alerts'
ASSETS=('index.html','style.css','app.js','logo.svg')

def options(cfg):
    values=cfg.get('dashboard',{});port=values.get('port',8787);interval=values.get('interval_seconds',60);smart=values.get('smart_interval_seconds',1800)
    if type(port) is not int or not 1024<=port<=65535 or port in (2283,2284,8096,8080,5055,4533,8989,7878,8686,9696):raise RuntimeError('Dashboard requires a distinct unprivileged localhost port')
    if type(interval) is not int or not 30<=interval<=300 or type(smart) is not int or not 300<=smart<=86400:raise RuntimeError('Dashboard collection/SMART intervals are out of range')
    if 'install_smartmontools' in values and type(values['install_smartmontools']) is not bool:raise RuntimeError('SMART package switch must be a boolean')
    return port,interval,smart

def units(data,code,config,port,interval):
    serve=str(code/"nekoserver-runtime")+" serve"
    collect=str(code/"nekoserver-runtime")+" collect"
    common='NoNewPrivileges=yes\nProtectHome=yes\nProtectSystem=strict\nPrivateTmp=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
    return {
    'nekoserver-dashboard-collect.service':'[Unit]\nDescription=NekoServer dashboard collection\n[Service]\nType=oneshot\nRuntimeDirectory=nekoserver-dashboard\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\nExecStart='+collect+' --config '+str(config)+' --output /run/nekoserver-dashboard/status.json --state '+str(data/'dashboard-state')+'\nTimeoutStartSec=180\n'+common+'ReadWritePaths=/run/nekoserver-dashboard -'+str(data/'dashboard-state')+'\nRestrictAddressFamilies=AF_UNIX\n',
    'nekoserver-dashboard-collect.timer':f'[Unit]\nDescription=NekoServer periodic dashboard collection\n[Timer]\nOnBootSec=15s\nOnUnitInactiveSec={interval}s\nUnit=nekoserver-dashboard-collect.service\n[Install]\nWantedBy=timers.target\n',
    'nekoserver-dashboard.service':'[Unit]\nDescription=NekoServer local dashboard\nWants=nekoserver-dashboard-collect.service\nAfter=nekoserver-dashboard-collect.service\n[Service]\nType=simple\nDynamicUser=yes\nExecStart='+serve+' --assets '+str(code/'dashboard')+f' --status /run/nekoserver-dashboard/status.json --port {port}\nRestart=on-failure\n'+common+'PrivateDevices=yes\nRestrictAddressFamilies=AF_INET AF_UNIX\nInaccessiblePaths=-/run/docker.sock -/var/run/docker.sock '+str(data)+' '+str(config.parent)+'\n[Install]\nWantedBy=multi-user.target\n'}

def run(ctx):
    port,interval,smart=options(ctx.config)
    if not ctx.apply:ctx.log('Local dashboard available with --apply at http://127.0.0.1:'+str(port));return
    if os.geteuid()!=0:raise RuntimeError('Dashboard installation requires sudo')
    if not shutil.which('systemctl') or platform.system()!='Linux':raise RuntimeError('Dashboard installation requires Linux/systemd')
    cfg=ctx.config;data=mount_path(cfg['data_mount']);project=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('dashboard_storage_guard',project/'storage-guard.py');guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard);guard.verify(cfg)
    code=safe_path(Path('/usr/local/lib/nekoserver-dashboard')/('v'+VERSION));config=safe_path(Path('/etc/nekoserver-dashboard/config.json'))
    payloads={code/'dashboard'/name:(project/'dashboard'/name).read_bytes() for name in ASSETS}
    payloads[code/native_runtime.NAME]=native_runtime.payload(project,cfg)
    definitions=units(data,code,config,port,interval)
    for path,payload in payloads.items():
        safe_path(path)
        if path.exists() and path.read_bytes()!=payload:raise RuntimeError('Installed dashboard release differs; custom files preserved')
        if path.name==native_runtime.NAME and path.exists() and path.stat().st_mode&0o005!=0o005:raise RuntimeError('Dashboard native executable must be readable/executable by its unprivileged account; review its permissions')
    for name,text in definitions.items():
        path=safe_path(Path('/etc/systemd/system')/name)
        if path.exists() and not path.read_text().startswith('[Unit]\nDescription=NekoServer '):raise RuntimeError('Unmanaged dashboard systemd unit preserved')
    public_directories=(code.parent,code,code/'dashboard')
    for folder in public_directories:
        safe_path(folder)
        if folder.exists() and (not folder.is_dir() or folder.stat().st_mode&0o005!=0o005):raise RuntimeError('Existing dashboard asset directory permissions block its unprivileged account; review locally')
    if cfg.get('dashboard',{}).get('install_smartmontools',True) and not shutil.which('smartctl'):
        if not shutil.which('pacman'):ctx.log('SMART readings unavailable: install smartmontools for this OS')
        else:
            check=subprocess.run(['pacman','-Si','smartmontools'],capture_output=True,timeout=30)
            if check.returncode:raise RuntimeError('smartmontools unavailable; update the system normally and retry')
            subprocess.run(['pacman','-S','--needed','--noconfirm','smartmontools'],capture_output=True,check=True,timeout=900)
    for folder in public_directories:
        if not folder.exists():
            folder.mkdir(parents=True,mode=0o755);folder.chmod(0o755)
    for path,payload in payloads.items():
        if not path.exists():atomic_write(path,payload,0o755 if path.name==native_runtime.NAME else 0o644)
    clean={k:cfg[k] for k in ('data_mount','media_mount','external_backup_mount','storage')};clean['media']={'services':cfg['media']['services']};clean['photos']={'deploy_immich':cfg.get('photos',{}).get('deploy_immich',False),'archive_interval_minutes':cfg.get('photos',{}).get('archive_interval_minutes',60),'backup_schedule':cfg.get('photos',{}).get('backup_schedule','legacy')};clean['music']={'backup_enabled':cfg.get('music',{}).get('backup_enabled',True)};clean['monitoring']={'enabled':cfg.get('monitoring',{}).get('enabled',True),'interval_minutes':cfg.get('monitoring',{}).get('interval_minutes',60)};clean['dashboard']={'smart_interval_seconds':smart};clean['heartbeat']={'interval_seconds':cfg.get('heartbeat',{}).get('interval_seconds',300)};clean['runtime']={'backend':cfg.get('runtime',{}).get('backend')};clean['gateway']={k:cfg.get('gateway',{}).get(k) for k in ('services','lan_ip','lan_cidr')}
    clean['nextcloud']={'enabled':cfg.get('nextcloud',{}).get('enabled',False)}
    clean['acceptance_scope']=scope_digest(cfg)
    atomic_json(config,clean);safe_path(data/'dashboard-state').mkdir(mode=0o700,exist_ok=True)
    for name,text in definitions.items():atomic_write(Path('/etc/systemd/system')/name,text,0o644)
    subprocess.run(['systemctl','daemon-reload'],capture_output=True,check=True,timeout=30)
    subprocess.run(['systemctl','start','nekoserver-dashboard-collect.service'],capture_output=True,check=True,timeout=190)
    for unit in ('nekoserver-dashboard-collect.timer','nekoserver-dashboard.service'):subprocess.run(['systemctl','enable','--now',unit],capture_output=True,check=True,timeout=30)
    subprocess.run(['systemctl','restart','nekoserver-dashboard.service'],capture_output=True,check=True,timeout=30)
    ctx.log(f'NekoServer dashboard: http://127.0.0.1:{port}/ · local, read-only; backup history starts after updated jobs run')
