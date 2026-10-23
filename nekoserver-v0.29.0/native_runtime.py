"""Verify the bundled native runtime and deploy without compiling as root."""
import hashlib,json,platform
from pathlib import Path
from neko_common import SYSTEMD_UNITS,VERSION,safe_path,atomic_write,atomic_json
TARGET='x86_64-unknown-linux-musl'
NAME='nekoserver-runtime'
def backend(config):
    """Scheduled jobs always use the bundled Rust runtime since v0.21.1.

    The Python fallback was removed. Older configs that say runtime.backend=python,
    or have no runtime setting, are treated as Rust; rerunning the installer
    rewrites their units. install.sh already requires x86_64, which the bundled
    runtime supports.
    """
    options=config.get('runtime',{})
    if not isinstance(options,dict):raise RuntimeError('runtime must be an object')
    value=options.get('backend','rust')
    if value not in ('rust','python'):raise RuntimeError('runtime.backend must be rust')
    return 'rust'

def payload(project,config):
    backend(config)
    if platform.system()!='Linux' or platform.machine()!='x86_64':raise RuntimeError('The bundled Rust runtime requires x86_64 Linux')
    root=safe_path(Path(project)/'runtime');manifest=json.loads(safe_path(root/'build.json').read_text())
    if manifest.get('version')!=VERSION or manifest.get('target')!=TARGET:raise RuntimeError('Native build identity does not match this release')
    binary=safe_path(root/'bin'/NAME).read_bytes()
    if hashlib.sha256(binary).hexdigest()!=manifest.get('binary_sha256') or binary[:6]!=b'\x7fELF\x02\x01':raise RuntimeError('Native executable checksum or format mismatch')
    expected={'Cargo.toml','Cargo.lock','build.py',*[str(p.relative_to(root)) for p in (root/'src').glob('*.rs')]}
    hashes=manifest.get('source_sha256',{})
    if set(hashes)!=expected:raise RuntimeError('Native build source manifest is incomplete')
    for rel in expected:
        if hashlib.sha256(safe_path(root/rel).read_bytes()).hexdigest()!=hashes[rel]:raise RuntimeError('Native source differs from the bundled build; rebuild explicitly')
    return binary

def deploy(project,config,destination):
    blob=payload(project,config)
    target=safe_path(Path(destination)/NAME)
    if target.exists() and target.read_bytes()!=blob:
        raise RuntimeError(f'The runtime copy at {target} differs from the one in this release (same version {VERSION}). '
                           'Two builds of one release are never mixed: if this release is the newer download (its runtime was '
                           f'rebuilt), move the old copy away (sudo mv {target} {target}.old) and run setup again; a file you '
                           'put there yourself is kept')
    if not target.exists():atomic_write(target,blob,0o750)
    elif target.stat().st_mode&0o111==0:raise RuntimeError('Existing native executable has no execute permission; review locally')
    return target

# Where each release keeps its own copy of the runtime (modules/*.py, gateway_tools.py).
SYSTEM_HOMES=('nekoserver-dashboard','nekoserver-os','nekoserver-heartbeat','nekoserver-notifications','nekoserver-gateway')
KEEP_BUNDLES=2   # release bundles (installer/releases/vX) kept on the SSD: this one and the one before

def release_number(name):
    """(0, 28, 10) from "v0.28.11"; None for any other folder name."""
    parts=name[1:].split('.') if name.startswith('v') else []
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None

def in_use(folder,units=SYSTEMD_UNITS):
    """Some installed service definition still names this folder."""
    text=str(folder)
    for unit in Path(units).glob('*'):
        try:
            if unit.is_file() and text in unit.read_text(errors='replace'):return True
        except OSError:
            return True
    return False

def release_folders(home):
    """This home's per-release folders (v0.28.9, ...), oldest first."""
    if not home.is_dir():return []
    found=[p for p in home.glob('v*') if p.is_dir() and not p.is_symlink() and release_number(p.name)]
    return sorted(found,key=lambda p:release_number(p.name))

def retire_old(data,log=print,lib=Path('/usr/local/lib'),units=SYSTEMD_UNITS):
    """After a setup run finished: remove the runtime copies of earlier releases that no
    installed service uses any more (each release deploys its own copies, which otherwise
    stay for good), and release bundles older than the KEEP_BUNDLES newest. The current
    release, and any folder a service still points at, are never touched."""
    import shutil
    data=Path(data);current='v'+VERSION
    old=[]
    # Docker's start guard (modules/docker.py) has its own per-release folder on the SSD.
    for home in [Path(lib)/name for name in SYSTEM_HOMES]+[data/'installer/runtime',data/'installer/guards']:
        old+=[p for p in release_folders(home) if p.name!=current and not in_use(p,units)]
    old+=[p for p in release_folders(data/'installer/releases')[:-KEEP_BUNDLES] if p.name!=current]
    for folder in old:shutil.rmtree(folder,ignore_errors=True)
    if old:log('Removed the files of earlier releases no longer in use: '+', '.join(str(p) for p in old))
    return old

def storage_config(config):return {k:config[k] for k in ('data_mount','media_mount','storage')}
def backup_guard(config):
    path=safe_path('/etc/nekoserver-backups/storage.json');atomic_json(path,storage_config(config));return path

def acceptance_checks(config,modules):
    """Inspect native selection, executable bytes, private guard and loaded commands."""
    import status_checks as health
    root=Path(__file__).resolve().parent;expected=hashlib.sha256(payload(root,config)).hexdigest();data=Path(config['data_mount']);rows=[]
    jobs=[]
    if 'dashboard' in modules:
        code=Path('/usr/local/lib/nekoserver-dashboard')/('v'+VERSION)/NAME
        jobs += [('nekoserver-dashboard.service',code,'serve',False),('nekoserver-dashboard-collect.service',code,'collect',False)]
        if config.get('dashboard',{}).get('fan_control',True):jobs += [('nekoserver-control.service',code,'control',False)]
    if 'server_os' in modules and config.get('server_os',{}).get('hdd_policy','keep_spinning')in ('keep_spinning','idle'):jobs += [('nekoserver-hdd-policy.service',Path('/usr/local/lib/nekoserver-os')/('v'+VERSION)/NAME,'hdd-policy',False)]
    if 'heartbeat' in modules:jobs += [('nekoserver-heartbeat.service',Path('/usr/local/lib/nekoserver-heartbeat')/('v'+VERSION)/NAME,'heartbeat',False)]
    if 'notifications' in modules:jobs += [('nekoserver-notifications.service',Path('/usr/local/lib/nekoserver-notifications')/('v'+VERSION)/NAME,'notify',False)]
    code=data/'installer/runtime'/('v'+VERSION)/NAME
    if 'media_monitor' in modules and config.get('monitoring',{}).get('enabled',True):jobs += [('nekoserver-media-health.service',code,'media-health',False)]
    if 'photo_preservation' in modules:
        jobs += [('nekoserver-immich-db-backup.service',code,'preservation',True)]
        if config.get('photos',{}).get('backup_schedule')!='weekly':jobs += [('nekoserver-photo-preservation.service',code,'preservation',True)]
    if 'host_backup' in modules:jobs += [('nekoserver-host-backup.service',code,'host-snapshot',False)]
    if 'maintenance' in modules:jobs += [('nekoserver-maintenance.service',code,'maintenance',False)]
    if 'app_backup' in modules:jobs += [('nekoserver-app-backup.service',code,'app-snapshot',False)]
    if 'music_backup' in modules and config.get('music',{}).get('backup_enabled',True):jobs += [('nekoserver-music-backup.service',code,'music-backup',True)]
    if 'photo_backup' in modules:jobs += [('nekoserver-photo-backup.service',code,'weekly',True)]
    for unit,binary,command,guard in jobs:
        good=False
        try:
            good=hashlib.sha256(safe_path(binary).read_bytes()).hexdigest()==expected
            rc,loaded=health.run(['systemctl','show',unit,'--property=ExecStart,FragmentPath'])
            good=good and rc==0 and ('argv[]='+str(binary)+' '+command+' ') in loaded and ('FragmentPath='+str(SYSTEMD_UNITS/unit)) in loaded
            if guard:good=good and health.read_json('/etc/nekoserver-backups/storage.json')==storage_config(config) and '--config /etc/nekoserver-backups/storage.json' in loaded
            if command=='maintenance':
                private=safe_path('/etc/nekoserver-backups/maintenance.json')
                good=good and health.read_json(private)==config and private.stat().st_uid==0 and private.stat().st_mode&0o077==0 and '--config '+str(private) in loaded
                rc,recovery=health.run(['systemctl','show',unit,'--property=ExecStopPost'])
                good=good and rc==0 and str(binary)+' maintenance-resume --config '+str(private) in recovery
            if command=='app-snapshot':
                from modules import app_backup
                expected_guard=app_backup.guard_config(config)
                good=good and health.read_json('/etc/nekoserver-backups/applications.json')==expected_guard and '--config /etc/nekoserver-backups/applications.json' in loaded
                rc,recovery=health.run(['systemctl','show',unit,'--property=ExecStopPost'])
                good=good and rc==0 and str(binary)+' app-resume --config /etc/nekoserver-backups/applications.json' in recovery
            if command=='host-snapshot':
                from modules import host_backup
                good=good and health.read_json('/etc/nekoserver-backups/host.json')==host_backup.expected(config) and '--config /etc/nekoserver-backups/host.json' in loaded
                rc,recovery=health.run(['systemctl','show',unit,'--property=ExecStopPost'])
                good=good and rc==0 and str(binary)+' host-resume --config /etc/nekoserver-backups/host.json' in recovery
            if command=='hdd-policy':
                from modules import server_os
                good=good and health.read_json('/etc/nekoserver-os/storage.json')==server_os.hdd_guard(config) and '--config /etc/nekoserver-os/storage.json' in loaded
            if command=='heartbeat':
                private=safe_path('/etc/nekoserver-heartbeat/config.json')
                from modules import heartbeat
                good=good and '--config '+str(private) in loaded and private.stat().st_uid==0 and private.stat().st_mode&0o077==0 and heartbeat.endpoint(health.read_json(private).get('endpoint'))
            if command=='notify':
                private=safe_path('/etc/nekoserver-notifications/config.json')
                good=good and '--config '+str(private) in loaded and private.stat().st_uid==0 and private.stat().st_mode&0o077==0
            if command=='serve':good=good and binary.stat().st_mode&0o005==0o005
        except (OSError,RuntimeError):pass
        rows.append({'id':'runtime:'+unit,'name':'Native '+unit.removesuffix('.service'),'status':'pass' if good else 'needs_setup','detail':'Selected native executable checksum, loaded command and applicable storage guard must match this release.'})
    return rows
