"""Immich upstream Compose deployment and additive photo archive configuration."""
from neko_common import checked_run, compose_image_override
import os, re, secrets, subprocess, urllib.request, json, shutil
from pathlib import Path
from neko_common import safe_path, relative_path, atomic_write, ensure_directory, verify_storage, host_timezone
DEPENDENCIES=['docker','account_credentials']  # Immich's login is chosen before it first starts
DESCRIPTION='Configure Immich with upstream Compose and separate append-only photo archive'
IMMICH_COMPOSE='https://github.com/immich-app/immich/releases/download/{version}/docker-compose.yml'
ROOT_UID = 0  # setup files must belong to root (tests run as a normal user)

def checked_mount(target):
    target=Path(target)
    if not target.is_dir() or target.is_symlink(): raise RuntimeError(f'Missing/symlink mount: {target}')
    p=subprocess.run(['findmnt','-n','-o','TARGET','--mountpoint',str(target)],capture_output=True,text=True,check=False)
    if p.returncode or p.stdout.strip()!=str(target): raise RuntimeError(f'Expected distinct mounted filesystem: {target}')
    return subprocess.check_output(['findmnt','-n','-o','SOURCE','--mountpoint',str(target)],text=True).strip()

def version_ok(value):
    return bool(re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',value))

def resolve_immich_version(value, opener=urllib.request.urlopen):
    """Find a release tag once; installed compose/.env remains pinned on reruns."""
    if value and value!='auto':
        if not version_ok(value): raise RuntimeError('Invalid Immich release version')
        return value
    request=urllib.request.Request('https://api.github.com/repos/immich-app/immich/releases/latest',headers={'User-Agent':'NekoServer-installer','Accept':'application/vnd.github+json'})
    with opener(request,timeout=20) as response:
        data=json.loads(response.read(100000))
    tag=data.get('tag_name','')
    if not version_ok(tag): raise RuntimeError('GitHub did not return a valid stable Immich release')
    return tag


def has_files(folder):
    """True if folder exists and holds anything (an initialised database, say)."""
    try:
        return any(Path(folder).iterdir())
    except FileNotFoundError:
        return False


def env_value(content, key):
    return next((line.split('=',1)[1] for line in content.splitlines() if line.startswith(key+'=')), '')


def checked_env(env, upload, db):
    """Contents of an existing Immich .env after checking it is private and points at this layout."""
    if env.stat().st_uid != ROOT_UID or env.stat().st_mode & 0o077:
        raise RuntimeError('Existing Immich environment must be root-owned and private; file preserved')
    content=env.read_text(); required=('UPLOAD_LOCATION='+str(upload),'DB_DATA_LOCATION='+str(db))
    if any(r not in content.splitlines() for r in required): raise RuntimeError('Existing Immich paths differ; refusing overwrite. Follow STORAGE.md for a verified migration before applying this layout')
    return content


def localize_manifest(content):
    """Accept only the known 2283 mapping, regardless of YAML quoting style."""
    text=content.decode('utf-8') if isinstance(content,bytes) else content
    pattern=r"(?m)^([ \t]*-[ \t]*)(['\"]?)2283:2283\2[ \t]*$"
    text,count=re.subn(pattern,lambda m:m[1]+m[2]+'127.0.0.1:2283:2283'+m[2],text)
    if count!=1:raise RuntimeError('Cannot safely localize upstream Immich port; review official manifest')
    return text


def switch_off(ctx, folder, run=subprocess.run, journal=None):
    """Immich switched off after it was set up: its containers are removed (under the
    shared lock, see run()), so it doesn't keep running unwatched, without monitoring,
    backups or monthly updates. Its photos, database and settings stay on the SSD:
    setting deploy_immich back to true brings it back as it was."""
    compose, env = folder/'compose.yaml', folder/'.env'
    if not compose.is_file():
        ctx.log('Immich: switched off in the settings, not set up');return
    import neko_common
    if (journal or neko_common.MAINTENANCE_JOURNAL).exists():
        # Finishing (or rolling back) that update starts Immich again.
        raise RuntimeError('A monthly update did not finish; it is completed (or rolled back) the next time maintenance runs, '
                           'and that starts Immich again. Run  sudo bash install.sh  again after it has finished to switch Immich off.')
    project=['docker','compose','-p','neko-immich','-f',str(compose),*(['--env-file',str(env)] if env.is_file() else [])]
    result=run([*project,'down','--timeout','60'],check=False,capture_output=True,text=True,timeout=300)
    if result.returncode:raise RuntimeError('Immich (docker compose down) failed: '+(result.stderr or '').strip()[-500:])
    left=run(['docker','ps','-q','--filter','label=com.docker.compose.project=neko-immich'],
             check=True,capture_output=True,text=True,timeout=60).stdout.split()
    if left:
        raise RuntimeError('Immich is switched off in the settings, but '+str(len(left))+' of its containers are still running; check  docker ps')
    ctx.log('Immich switched off: its containers were removed. Its photos, database and settings are kept on the SSD; '
            'set "deploy_immich": true under "photos" and run setup again to bring it back.')


def _run(ctx):
    cfg=ctx.config; opts=cfg.get('photos',{})
    if not ctx.apply:
        ctx.log('Photo service not changed; use --module photos --apply; configure UUID for weekly external backup separately')
        return
    if os.geteuid()!=0: raise RuntimeError('Root required')
    if not opts.get('deploy_immich',False):
        return switch_off(ctx, Path(cfg['data_mount'])/'compose'/'immich')
    data=Path(cfg['data_mount']); media=Path(cfg['media_mount'])
    if data==media or checked_mount(data)==checked_mount(media): raise RuntimeError('Data SSD and media HDD must be distinct mounts')
    verify_storage(cfg)
    ver=opts.get('immich_version','auto'); bind=opts.get('bind_ip','127.0.0.1')
    if bind!='127.0.0.1': raise RuntimeError('Invalid Immich bind address')
    if not shutil.which('docker'): raise RuntimeError('Docker is not installed')
    subprocess.run(['docker','info'],check=True,stdout=subprocess.DEVNULL)
    compose_dir=data/'compose'/'immich'; db=data/'appdata'/'immich'/'postgres'; upload=data/'photos'/'immich-library'; archive=media/relative_path(opts.get('archive_dir','photos/archive')); inbox=media/relative_path(opts.get('inbox_dir','photos/inbox'))
    for target in (compose_dir,db,upload,archive,inbox):
        safe_path(target)
    compose=safe_path(compose_dir/'compose.yaml'); env=safe_path(compose_dir/'.env')
    if compose.exists() and not env.exists():
        # Setup stopped between the two files (releases up to 0.24.1 wrote compose.yaml
        # first). Without a database there is nothing the missing .env could unlock, so
        # start this step again; with one, its password is unknown and a person must look.
        if has_files(db): raise RuntimeError('Immich has a database but its .env file is missing; files preserved for inspection')
        ctx.log('Finishing an Immich setup that was interrupted')
        compose.unlink()
    if not compose.exists():
        if env.exists():
            # .env is written first and compose.yaml last, so this is an interrupted setup:
            # keep its version and database password and fetch the matching compose file.
            ver=env_value(checked_env(env,upload,db),'IMMICH_VERSION')
            if not version_ok(ver): raise RuntimeError('Existing Immich .env has no valid IMMICH_VERSION; file preserved for inspection')
            ctx.log(f'Finishing an interrupted Immich setup (release {ver})')
        else:
            ver=resolve_immich_version(ver)
            ctx.log(f'Pinned Immich release: {ver}')
        # Network is required to obtain an upstream version-compatible manifest.
        with urllib.request.urlopen(IMMICH_COMPOSE.format(version=ver),timeout=30) as response:
            content=response.read(200000)
        if b'immich-server' not in content or b'DB_DATA_LOCATION' not in content: raise RuntimeError('Unexpected upstream Immich manifest')
        verify_storage(cfg)
        # Allow storage and port values from a restricted .env; keep upstream manifest unmodified.
        ensure_directory(data/'appdata', 0o755)
        ensure_directory(data/'appdata/immich', 0o700)
        ensure_directory(compose_dir, 0o700)
        ensure_directory(db, 0o700)
        for target in (upload, archive, inbox): ensure_directory(target, 0o755)
        # The upstream file binds 2283 to all host IPs; localize it explicitly.
        text=localize_manifest(content)
        if not env.exists():
            atomic_write(env,'UPLOAD_LOCATION='+str(upload)+'\nDB_DATA_LOCATION='+str(db)+'\nIMMICH_VERSION='+ver+'\nDB_PASSWORD='+secrets.token_hex(24)+'\nDB_USERNAME=postgres\nDB_DATABASE_NAME=immich\nTZ='+host_timezone(cfg)+'\n',0o600)
        # Written last: a compose.yaml means this step finished writing its settings.
        atomic_write(compose,text,0o640)
    else:
        checked_env(env,upload,db)
    ensure_directory(db, 0o700)
    for target in (upload, archive, inbox): ensure_directory(target, 0o755)
    verify_storage(cfg)
    base=['docker','compose','-p','neko-immich','-f',str(compose),'--env-file',str(env),*compose_image_override(compose)]
    checked_run([*base,'config','-q'],60,'Immich (docker compose config)')
    checked_run([*base,'up','-d'],1200,'Immich (docker compose up)')
    ctx.log('Immich deployed; upload library and permanent photo archive are separate. Configure phone settings and verify no deletion sync before use.')


def run(context):
    if not context.apply:return _run(context)
    from recovery_tools import mounts, lock
    mounts(context.config)
    with lock(context.config):return _run(context)
