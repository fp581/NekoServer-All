"""Explicit, local Docker recovery and isolated Immich restore rehearsal."""
import copy
import datetime as dt
import fcntl
import sys
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import urllib.error
from media_libraries import NoRedirect
import uuid
from contextlib import contextmanager
from pathlib import Path
import neko_common
from neko_common import VERSION, safe_path, mount_path, atomic_json, atomic_write
from installer import validate_config
from modules import docker as docker_module

DOCKER=['docker','--host','unix:///var/run/docker.sock']
SERVICES={'database','redis','immich-server','immich-machine-learning'}
FORMAT='neko-immich-snapshot-v1'

def command(argv, timeout=60, stdin=None, stdout=None):
    # Never print command output: Compose can render passwords, SQL can contain personal data.
    try:result=subprocess.run(argv,stdin=stdin,stdout=stdout if stdout is not None else subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    except (OSError,subprocess.TimeoutExpired):raise RuntimeError('Recovery command unavailable or timed out: '+argv[0]) from None
    if result.returncode:raise RuntimeError('Recovery command failed: '+argv[0]+'; inspect the service locally')
    if stdout is not None:return ''
    value=result.stdout
    return value.decode() if isinstance(value,bytes) else value

def json_command(argv):return json.loads(command(argv))

def digest(path):
    h=hashlib.sha256()
    with safe_path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def regular_files(root):
    root=safe_path(root)
    if not root.is_dir():raise RuntimeError('Recovery source directory missing')
    result=[]
    for base,dirs,files in os.walk(root,followlinks=False):
        for name in dirs:
            if (Path(base)/name).is_symlink():raise RuntimeError('Symlink in recovery tree')
        for name in files:
            path=safe_path(Path(base)/name)
            if not path.is_file():raise RuntimeError('Non-regular file in recovery tree')
            result.append(path)
    return sorted(result)

def copy_verified(source,target):
    source=safe_path(source);target=safe_path(target);before=source.stat()
    if target.exists():raise RuntimeError('Recovery destination already exists; never overwrite')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with source.open('rb') as inp,target.open('xb') as out:
        os.fchmod(out.fileno(),0o600);shutil.copyfileobj(inp,out,1024*1024);out.flush();os.fsync(out.fileno())
    after=source.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino) or digest(source)!=digest(target):
        raise RuntimeError('Recovery source changed or copy checksum failed; incomplete copy retained')

def mounts(config):
    validate_config(config)
    spec=importlib.util.spec_from_file_location('recovery_storage_guard',Path(__file__).with_name('storage-guard.py'))
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard);guard.verify(config)

@contextmanager
def lock(config,changes_apps=True):
    """The shared lock that backups and monthly updates hold too. A monthly update can
    start (and fail, keeping its journal) while this waits, so whatever changes or starts
    apps checks for an unfinished update again once the lock is held, right before it
    begins. Docker re-attachment and backup repair (changes_apps=False) stay allowed:
    they may be what lets a stuck update finish."""
    path=safe_path(Path(config['data_mount'])/'installer/recovery.lock');path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as stream:
        os.fchmod(stream.fileno(),0o600)
        wait_for(stream,sys.stdin.isatty())
        if changes_apps:
            blocked=neko_common.pending_maintenance(neko_common.MAINTENANCE_JOURNAL,'Setup stopped before changing anything else.')
            if blocked:raise RuntimeError(blocked)
        yield

BUSY='A NekoServer backup or monthly update is running right now. Wait for it to finish (usually under an hour), then run  sudo bash install.sh  again.'

def wait_for(stream,patient,limit=1800,sleep=None):
    """Take the shared lock. Night-time backups hold it too: an interactive run waits
    for them (up to 30 minutes, saying so) instead of failing with a puzzling error."""
    import time
    sleep=sleep or time.sleep
    waited=0
    while True:
        try:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);return
        except BlockingIOError:
            if not patient or waited>=limit:raise RuntimeError(BUSY) from None
            if waited==0:print('Waiting for a NekoServer backup that is running right now to finish (up to 30 minutes)...',flush=True)
            sleep(5);waited+=5

def require_root():
    if os.geteuid()!=0:raise RuntimeError('Recovery changes require sudo')

def host_supported():
    from neko_common import arch_host
    return platform.machine()=='x86_64' and arch_host()

def checkpoint(config,apply=False):
    mounts(config)
    info=json_command(DOCKER+['info','--format','{{json .}}'])
    target=str(Path(config['data_mount'])/'docker-engine')
    version=info.get('ServerVersion','')
    if info.get('DockerRootDir')!=target or info.get('Driver')!='overlay2' or 'io.containerd.snapshotter' in json.dumps(info.get('DriverStatus')) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):
        raise RuntimeError('Checkpoint supports verified classic overlay2 Docker at the application SSD only')
    result={'format':'neko-host-checkpoint-v1','created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'version':VERSION,'architecture':platform.machine(),'docker_version':version,'driver':'overlay2','docker_root':target,'storage':copy.deepcopy(config['storage']),'data_mount':config['data_mount'],'media_mount':config['media_mount'],'uid':config['media']['uid'],'gid':config['media']['gid']}
    if apply:
        require_root();atomic_json(Path(config['data_mount'])/'installer/host-checkpoint.json',result)
    return result

def verify_bundle(bundle,config):
    bundle=safe_path(bundle);manifest=json.loads(safe_path(bundle/'release-manifest.json').read_text())
    entries=manifest.get('sha256')
    if not isinstance(entries,dict) or not entries or not all(x in entries for x in ('installer.py','neko_common.py','storage-guard.py','modules/docker.py')):raise RuntimeError('Incomplete preserved installer manifest')
    for name,expected in entries.items():
        rel=Path(name)
        if rel.is_absolute() or '..' in rel.parts or not re.fullmatch(r'[0-9a-f]{64}',expected):raise RuntimeError('Invalid preserved installer checksum entry')
        if digest(bundle/rel)!=expected:raise RuntimeError('Preserved installer checksum mismatch')
    saved=json.loads(safe_path(bundle/'config.json').read_text())
    for key in ('data_mount','media_mount','storage'):
        if saved.get(key)!=config.get(key):raise RuntimeError('Preserved installer storage configuration differs')
    return manifest['version']

def reattach(config,bundle,apply=False,confirm=''):
    mounts(config);verify_bundle(bundle,config)
    saved=json.loads(safe_path(Path(config['data_mount'])/'installer/host-checkpoint.json').read_text())
    if saved.get('format')!='neko-host-checkpoint-v1' or saved.get('architecture')!=platform.machine() or saved.get('driver')!='overlay2':raise RuntimeError('Host checkpoint is incompatible')
    for key in ('storage','data_mount','media_mount'):
        if saved.get(key)!=config.get(key):raise RuntimeError('Recovery checkpoint storage differs')
    if saved.get('uid')!=config['media']['uid'] or saved.get('gid')!=config['media']['gid']:raise RuntimeError('Recovery media UID/GID differs; no automatic ownership changes')
    target=Path(config['data_mount'])/'docker-engine'
    if saved.get('docker_root')!=str(target) or not target.is_dir() or not any(target.iterdir()):raise RuntimeError('Expected persistent Docker store is missing or empty')
    if not host_supported():raise RuntimeError('Automatic reattachment supports x86_64 Nyarch/Arch only; Bazzite migration remains unvalidated')
    for argv in (DOCKER+['--version'],['dockerd','--version']):
        installed=command(argv).strip()
        if not installed.startswith('Docker version '+saved['docker_version']+','):raise RuntimeError('Install the checkpoint Docker version for both client and daemon before reattachment; upgrades require separate validation')
    for unit in ('docker.service','docker.socket'):
        result=subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True,timeout=15)
        if result.returncode==0:raise RuntimeError('Stop Docker service and socket before reattachment')
    daemon=safe_path(Path('/etc/docker/daemon.json'))
    desired=docker_module.prepare_daemon_config(daemon,target)
    default=safe_path(Path('/var/lib/docker'))
    if default.exists() and any(default.iterdir()):raise RuntimeError('Default Docker store contains data; manual migration required')
    override=safe_path(Path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf'))
    if override.exists() and not override.read_text().startswith('# Managed by NekoServer\n'):raise RuntimeError('Unmanaged Docker guard preserved; review it first')
    if not apply:return {'status':'needs_setup','reason':'Verified reattachment preview; explicit SSD UUID confirmation required','docker_version':saved['docker_version']}
    require_root()
    if confirm!=config['storage']['data_ssd']['uuid']:raise RuntimeError('Confirm the application SSD UUID with --confirm')
    with lock(config,changes_apps=False):
        # Recheck disk identities immediately before writing OS configuration.
        mounts(config)
        docker_module.install_storage_guard(config)
        atomic_write(daemon,json.dumps(desired,indent=2)+'\n',0o644)
        try:
            command(['systemctl','enable','--now','docker'],90)
            actual=checkpoint(config)
            if actual['docker_version']!=saved['docker_version']:raise RuntimeError('Running Docker version differs from checkpoint')
        except BaseException:
            # A failed adoption must not keep a mismatched daemon running.
            stopped=subprocess.run(['systemctl','stop','docker.service','docker.socket'],capture_output=True,timeout=30)
            if stopped.returncode:raise RuntimeError('Post-start verification failed and Docker could not be stopped; inspect service/socket locally') from None
            raise
    return {'status':'needs_setup','reason':'Docker store reattached; rerun deployment profiles and verify applications/reboots before acceptance'}

def compose_args(config):
    root=Path(config['data_mount'])/'compose/immich'
    compose=safe_path(root/'compose.yaml');env=safe_path(root/'.env')
    if not compose.is_file() or not env.is_file():raise RuntimeError('Existing Immich manifest and environment required')
    return DOCKER+['compose','-p','neko-immich','-f',str(compose),'--env-file',str(env)]

def validate_model(model,upload,database):
    services=model.get('services',{})
    if set(services)!=SERVICES:raise RuntimeError('Restore currently supports the four standard Immich services; custom services require review')
    for name,svc in services.items():
        if not isinstance(svc,dict) or not isinstance(svc.get('image'),str):raise RuntimeError('Invalid Immich service')
        if any(key in svc for key in ('build','privileged','devices','cap_add','network_mode','pid','ipc','user','secrets','configs','volumes_from','extends')):raise RuntimeError('Custom Immich runtime settings require manual isolated recovery')
        seen=set()
        for volume in svc.get('volumes',[]):
            if not isinstance(volume,dict):raise RuntimeError('Canonical Compose volume definitions required')
            target=volume.get('target');source=volume.get('source')
            if not isinstance(target,str) or target in seen:raise RuntimeError('Invalid/duplicate Immich volume target')
            seen.add(target)
            if volume.get('type')=='bind':
                if source=='/etc/localtime' and volume.get('read_only'):continue
                expected=upload if name=='immich-server' else database if name=='database' else None
                expected_target=('/data','/usr/src/app/upload') if name=='immich-server' else ('/var/lib/postgresql/data',)
                if expected is None or source!=str(expected) or target not in expected_target:raise RuntimeError('External/custom Immich mounts are not covered; no incomplete snapshot allowed')
            elif volume.get('type')=='volume' and name=='immich-machine-learning' and isinstance(source,str) and re.fullmatch(r'[A-Za-z0-9_-]+',source):pass
            else:raise RuntimeError('Unsupported Immich persistent volume')
        required=('/data','/usr/src/app/upload') if name=='immich-server' else ('/var/lib/postgresql/data',) if name=='database' else ()
        if required and not any(x in seen for x in required):raise RuntimeError('Required Immich storage binding is missing')
    server_env=services['immich-server'].get('environment',{});db_env=services['database'].get('environment',{})
    if not isinstance(server_env,dict) or not isinstance(db_env,dict):raise RuntimeError('Canonical Immich environment required')
    if server_env.get('DB_URL') or server_env.get('REDIS_URL') or server_env.get('DB_HOSTNAME','database')!='database' or server_env.get('REDIS_HOSTNAME','redis')!='redis':raise RuntimeError('Custom database/cache routing requires manual isolated recovery')
    for key,dbkey,default in [('DB_USERNAME','POSTGRES_USER','postgres'),('DB_DATABASE_NAME','POSTGRES_DB','immich'),('DB_PASSWORD','POSTGRES_PASSWORD',None)]:
        if server_env.get(key,default)!=db_env.get(dbkey,default):raise RuntimeError('Immich database credentials/name do not match its database service')
    return services

def freeze_images(base,model):
    model=copy.deepcopy(model)
    for name,service in model['services'].items():
        ids=command(base+['ps','-a','-q',name]).split()
        if len(ids)!=1:raise RuntimeError('Exactly one existing Immich container per service is required')
        image=command(DOCKER+['inspect','--format','{{.Image}}',ids[0]]).strip()
        user=command(DOCKER+['image','inspect','--format','{{.Config.User}}',image]).strip()
        if user not in ('','0','root','0:0','root:root'):raise RuntimeError('Non-root image permissions require manual isolated recovery')
        values=json_command(DOCKER+['image','inspect','--format','{{json .RepoDigests}}',image])
        if not isinstance(values,list) or not values or not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}',values[0]):raise RuntimeError('Immich image lacks an immutable repository digest')
        service['image']=values[0]
    return model

def within(path,parent):return path==parent or parent in path.parents

def new_folder(config,path,kind):
    path=mount_path(path)
    parents=[Path(config['data_mount'])/kind,Path(config['media_mount'])/kind]
    if not any(path.parent==p for p in parents):raise RuntimeError('Use a new named directory directly inside '+kind+' on the SSD or HDD')
    if path.exists():raise RuntimeError('Recovery destination exists; use a new empty name')
    return path

def enough_space(path,required):
    ancestor=path
    while not ancestor.exists():ancestor=ancestor.parent
    if shutil.disk_usage(ancestor).free<required+100*1024*1024:raise RuntimeError('Insufficient free space for recovery copy plus reserve')

def dump_complete(path):
    """A disk that fills up mid-dump can leave a cut-off file even when every program
    reports success; pg_dump ends a finished plain dump with this line."""
    with open(path,'rb') as stream:
        stream.seek(max(0,os.fstat(stream.fileno()).st_size-4096))
        return b'-- PostgreSQL database dump complete' in stream.read()

def snapshot_plan(config,destination):
    """Read what the snapshot records: Compose settings, the pinned Immich version, the
    exact images and the running services. On --apply this runs only while holding the
    shared lock, so a monthly update cannot change any of it before the copy is made."""
    mounts(config);base=compose_args(config);model=json_command(base+['config','--format','json'])
    upload=safe_path(Path(config['data_mount'])/'photos/immich-library');database=safe_path(Path(config['data_mount'])/'appdata/immich/postgres')
    validate_model(model,upload,database)
    version=None
    for line in safe_path(Path(config['data_mount'])/'compose/immich/.env').read_text().splitlines():
        if line.startswith('IMMICH_VERSION='):version=line.split('=',1)[1]
    if not isinstance(version,str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',version):raise RuntimeError('A pinned Immich version is required')
    destination=new_folder(config,destination,'recovery-snapshots')
    files=regular_files(upload);dbfiles=regular_files(database)
    enough_space(destination,sum(p.stat().st_size for p in files)+2*sum(p.stat().st_size for p in dbfiles))
    model=freeze_images(base,model)
    running=command(base+['ps','--status','running','--services']).split()
    if 'database' not in running or any(x not in SERVICES for x in running):raise RuntimeError('Immich database must be running with only standard services')
    resume=sorted(set(running)-{'database'})
    return base,model,upload,database,version,destination,files,resume

def snapshot(config,destination,apply=False):
    if not apply:
        plan=snapshot_plan(config,destination);version,files=plan[4],plan[6]
        return {'status':'needs_setup','reason':'Preview: pause running Immich services, copy full upload tree and dump its database; source services then resume','files':len(files),'immich_version':version}
    require_root()
    with lock(config):
        # Everything recorded is read (and the drives re-checked) only now, under the lock.
        base,model,upload,database,version,destination,files,resume=snapshot_plan(config,destination)
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700);destination.mkdir(mode=0o700)
        atomic_json(destination/'.incomplete',{'reason':'Snapshot is not usable until manifest publication','source_project':'neko-immich','resume_services':resume})
        paused=False
        try:
            if resume:
                paused=True;command(base+['stop','-t','60',*resume],180)
            # Rescan after writers stop, including all original/generated/profile/marker files.
            files=regular_files(upload)
            database_bytes=sum(p.stat().st_size for p in regular_files(database))
            enough_space(destination,sum(p.stat().st_size for p in files)+2*database_bytes)
            photo=destination/'upload';photo.mkdir(mode=0o700)
            for directory in sorted(p for p in upload.rglob('*') if p.is_dir()):
                safe_path(directory);(photo/directory.relative_to(upload)).mkdir(parents=True,exist_ok=True,mode=0o700)
            for source in files:copy_verified(source,photo/source.relative_to(upload))
            sql=destination/'database.sql'
            with sql.open('xb') as output:
                os.fchmod(output.fileno(),0o600)
                command(base+['exec','-T','database','sh','-ec','exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl'],1800,stdout=output)
                output.flush();os.fsync(output.fileno())
            if sql.stat().st_size<100:raise RuntimeError('Database dump unexpectedly small')
            if not dump_complete(sql):raise RuntimeError('Database dump is incomplete (no end marker); the disk may be full')
            atomic_json(destination/'compose-resolved.json',model)
            entries={str(p.relative_to(destination)):digest(p) for p in regular_files(destination) if p!=destination/'.incomplete'}
            metadata={'format':FORMAT,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),'immich_version':version,'source_upload':str(upload),'source_database':str(database),'database_bytes':database_bytes,'sha256':entries,'empty_directories':[str(p.relative_to(photo)) for p in sorted(photo.rglob('*')) if p.is_dir()]}
            atomic_json(destination/'snapshot.json',metadata)
            (destination/'.incomplete').unlink()
        finally:
            if paused:command(base+['start',*resume],180)
    return {'status':'pass','reason':'Matched full-upload/database snapshot created; restore acceptance still required','snapshot':str(destination),'files':len(files)}

def verify_snapshot(path):
    path=safe_path(path)
    if (path/'.incomplete').exists():raise RuntimeError('Incomplete snapshot cannot be restored')
    meta=json.loads(safe_path(path/'snapshot.json').read_text())
    if meta.get('format')!=FORMAT or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',meta.get('immich_version','')):raise RuntimeError('Unsupported snapshot format/version')
    entries=meta.get('sha256')
    if not isinstance(entries,dict) or not {'database.sql','compose-resolved.json'}<=set(entries):raise RuntimeError('Snapshot lacks database/Compose checksums')
    actual={str(p.relative_to(path)) for p in regular_files(path)}-{'snapshot.json'}
    if actual!=set(entries):raise RuntimeError('Snapshot file inventory differs')
    for name,expected in entries.items():
        rel=Path(name)
        if rel.is_absolute() or '..' in rel.parts or not re.fullmatch(r'[0-9a-f]{64}',expected):raise RuntimeError('Invalid snapshot checksum entry')
        if digest(path/rel)!=expected:raise RuntimeError('Snapshot checksum mismatch')
    payload=(path/'compose-resolved.json').read_bytes()
    if hashlib.sha256(payload).hexdigest()!=entries['compose-resolved.json']:raise RuntimeError('Snapshot Compose changed during verification')
    model=json.loads(payload)
    if type(meta.get('database_bytes')) is not int or meta['database_bytes']<1:raise RuntimeError('Snapshot database size estimate missing')
    validate_model(model,Path(meta['source_upload']),Path(meta['source_database']))
    if not all(re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}',s['image']) for s in model['services'].values()):raise RuntimeError('Snapshot images are not pinned by digest')
    dirs=meta.get('empty_directories',[])
    if not isinstance(dirs,list) or any(not isinstance(x,str) or Path(x).is_absolute() or '..' in Path(x).parts for x in dirs):raise RuntimeError('Invalid snapshot directory inventory')
    return meta,model

def isolated_model(model,meta,workspace,port):
    result={'services':{},'networks':{'default':{'internal':True}}}
    validate_model(model,Path(meta['source_upload']),Path(meta['source_database']))
    allowed=('image','command','entrypoint','environment','healthcheck','shm_size','stop_grace_period','init')
    for name,source in model['services'].items():
        service={k:copy.deepcopy(source[k]) for k in allowed if k in source}
        service['restart']='no';service['volumes']=[]
        env=service.get('environment',{})
        if not isinstance(env,dict):raise RuntimeError('Canonical service environment required')
        for key in ('UPLOAD_LOCATION','DB_DATA_LOCATION'):env.pop(key,None)
        if name=='immich-server':
            env['DB_HOSTNAME']='database';env['REDIS_HOSTNAME']='redis';env['IMMICH_MACHINE_LEARNING_URL']='http://immich-machine-learning:3003'
            service['environment']=env
        # Self-hosted integrations cannot call out through the internal test network.
        for key,value in env.items():
            if value is not None and '$' in str(value):raise RuntimeError('Unresolved environment interpolation in snapshot')
        for volume in source.get('volumes',[]):
            if volume.get('source')=='/etc/localtime':continue
            folder=workspace/'upload' if name=='immich-server' else workspace/'postgres' if name=='database' else workspace/'cache'/volume['source']
            service['volumes'].append({'type':'bind','source':str(folder),'target':volume['target'],'bind':{'create_host_path':False}})
        if name=='immich-server':service['ports']=[{'target':2283,'published':str(port),'host_ip':'127.0.0.1','protocol':'tcp'}]
        if 'depends_on' in source:
            if not isinstance(source['depends_on'],dict) or not set(source['depends_on'])<=SERVICES:raise RuntimeError('Unsupported service dependency')
            service['depends_on']=copy.deepcopy(source['depends_on'])
        result['services'][name]=service
    return result

def ready_database(base,attempts=60):
    for _ in range(attempts):
        try:
            command(base+['exec','-T','database','sh','-ec','exec pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],10);return
        except RuntimeError:time.sleep(2)
    raise RuntimeError('Isolated database did not become ready')

def restored_tables(base):
    value=command(base+['exec','-T','database','sh','-ec',"exec psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -Atc \"SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'\""],30).strip()
    if not value.isdecimal() or int(value)<1:raise RuntimeError('Restored database contains no verified application tables')
    return int(value)

def http_ready(port):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    for _ in range(60):
        try:
            with opener.open(f'http://127.0.0.1:{port}/',timeout=3) as response:
                if response.status==200:return True
        except urllib.error.HTTPError as exc:exc.close()
        except OSError:pass
        time.sleep(2)
    return False

def restore_test(config,snapshot_path,workspace,port=2284,apply=False,confirm=''):
    mounts(config);snapshot_path=safe_path(snapshot_path);meta,model=verify_snapshot(snapshot_path)
    workspace=new_folder(config,workspace,'recovery-tests')
    if within(workspace,snapshot_path) or within(snapshot_path,workspace):raise RuntimeError('Restore workspace and backup must be separate')
    if type(port) is not int or not 1024<=port<=65535 or port==2283:raise RuntimeError('Use a distinct unprivileged restore-test port')
    with socket.socket() as sock:
        try:sock.bind(('127.0.0.1',port))
        except OSError:raise RuntimeError('Restore-test port is already in use') from None
    enough_space(workspace,sum(p.stat().st_size for p in regular_files(snapshot_path))+2*meta['database_bytes'])
    isolated=isolated_model(model,meta,workspace,port)
    if not apply:return {'status':'needs_setup','reason':'Verified snapshot preview; isolated DB/files restore only','immich_version':meta['immich_version'],'url':f'http://127.0.0.1:{port}/'}
    require_root()
    if confirm!='RESTORE-ISOLATED':raise RuntimeError('Use --confirm RESTORE-ISOLATED to create the rehearsal')
    project='neko-restore-'+uuid.uuid4().hex[:12]
    for argv in (DOCKER+['ps','-a','--filter','label=com.docker.compose.project='+project,'-q'],DOCKER+['network','ls','--filter','label=com.docker.compose.project='+project,'-q']):
        if command(argv).strip():raise RuntimeError('Restore project identity already exists')
    with lock(config):
        workspace.parent.mkdir(parents=True,exist_ok=True,mode=0o700);workspace.mkdir(mode=0o700)
        atomic_write(workspace/'.incomplete','Restore rehearsal incomplete\n')
        base=DOCKER+['compose','-p',project,'-f',str(workspace/'compose.json')]
        started=False
        try:
            (workspace/'upload').mkdir(mode=0o700)
            for name in meta.get('empty_directories',[]):(workspace/'upload'/name).mkdir(parents=True,exist_ok=True,mode=0o700)
            for source in regular_files(snapshot_path/'upload'):
                target=workspace/'upload'/source.relative_to(snapshot_path/'upload');copy_verified(source,target)
                if digest(target)!=meta['sha256'][str(source.relative_to(snapshot_path))]:raise RuntimeError('Snapshot photo changed after verification')
            for service in isolated['services'].values():
                for volume in service['volumes']:safe_path(volume['source']).mkdir(parents=True,exist_ok=True,mode=0o700)
            sql=workspace/'restore.sql'
            if digest(snapshot_path/'database.sql')!=meta['sha256']['database.sql']:raise RuntimeError('Snapshot SQL changed after verification')
            with (snapshot_path/'database.sql').open('rb') as inp,sql.open('xb') as out:
                os.fchmod(out.fileno(),0o600)
                old=b"SELECT pg_catalog.set_config('search_path', '', false);";new=b"SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);"
                for line in inp:out.write(line.replace(old,new))
                out.flush();os.fsync(out.fileno())
            if digest(snapshot_path/'database.sql')!=meta['sha256']['database.sql']:raise RuntimeError('Snapshot SQL changed during restore staging')
            atomic_json(workspace/'compose.json',isolated)
            command(base+['config','-q'])
            # Only fresh isolated Postgres starts before import; Immich cannot migrate it first.
            started=True;command(base+['up','-d','--no-deps','--pull','missing','database'],900)
            ready_database(base)
            with sql.open('rb') as inp:
                command(base+['exec','-T','database','sh','-ec','exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --single-transaction --set ON_ERROR_STOP=on -f -'],1800,stdin=inp)
            tables=restored_tables(base)
            command(base+['up','-d','--pull','missing'],900)
            healthy=http_ready(port)
            result={'status':'needs_setup','project':project,'immich_version':meta['immich_version'],'database_tables':tables,'http_ready':healthy,'url':f'http://127.0.0.1:{port}/','reason':'Isolated import completed; sign in, check original files/albums/video and deletion behavior before accepting recovery'}
            atomic_json(workspace/'restore-report.json',result)
            if not healthy:raise RuntimeError('Isolated application did not become HTTP-ready; report retained')
            (workspace/'.incomplete').unlink()
            return result
        except BaseException:
            if started:
                try:command(base+['stop'],180)
                except Exception:raise RuntimeError('Isolated restore failed and its test services could not be stopped; inspect the workspace project') from None
            raise
