"""Docker foundation with guarded, non-migrating data-root configuration."""
import native_runtime
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from neko_common import safe_path, mount_path, atomic_write, atomic_json, VERSION

DEPENDENCIES = ['storage','recovery']
DESCRIPTION = 'Verify dedicated SSD and Docker data-root; --apply configures fresh daemon and mount guard'


def call(args, timeout=120):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode:
        raise RuntimeError(f"Command failed ({proc.returncode}): {args[0]}: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()


def mounted_data_root(root):
    root = safe_path(root)
    if not root.is_absolute() or str(root) in ('/', '/home', '/var', '/tmp', '/mnt', '/srv'):
        raise RuntimeError('Persistent root must be a dedicated absolute directory under a separately mounted filesystem')
    if not shutil.which('findmnt'):
        raise RuntimeError('findmnt required to verify mounted storage')
    if not root.is_dir():
        raise RuntimeError(f'Persistent data path does not exist: {root}')
    mountpoint = call(['findmnt', '-n', '-o', 'TARGET', '--target', str(root)])
    if mountpoint == '/':
        raise RuntimeError(f'{root} is on the OS filesystem; mount the data SSD first')
    if Path(mountpoint) != root:
        raise RuntimeError(f'Data root must itself be a mountpoint, not a child of {mountpoint}')
    source = call(['findmnt', '-n', '-o', 'SOURCE', '--target', str(root)])
    return mountpoint, source


def prepare_daemon_config(path, target):
    """Return new config; never silently migrate or replace an existing data-root."""
    content = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(content, dict):
        raise RuntimeError('Existing Docker daemon.json must be a JSON object')
    existing = content.get('data-root')
    if existing and existing != str(target):
        raise RuntimeError(f'Existing Docker data-root points to {existing}; manual migration required')
    features=content.setdefault('features',{})
    if not isinstance(features,dict) or features.get('containerd-snapshotter') is True or features.get('containerd-migration') is True:
        raise RuntimeError('containerd image store requires separate verified migration; refusing backend switch')
    features['containerd-snapshotter']=False
    content['data-root'] = str(target)
    return content


def check_existing_docker(target):
    """Never convert an existing Docker installation automatically."""
    default = Path('/var/lib/docker')
    if default.exists() and any(default.iterdir()):
        raise RuntimeError('Existing /var/lib/docker contains data; manual migration required before changing Docker data-root')
    if target.exists() and any(target.iterdir()):
        # Safe to reuse a target only when Docker already points to it.
        raise RuntimeError(f'{target} is not empty; refusing to adopt existing Docker data without migration verification')


def write_atomic(path, payload):
    atomic_write(path, payload, 0o644)


def verify_classic_store():
    status=call(['docker','info','--format','{{json .DriverStatus}}'])
    if 'io.containerd.snapshotter' in status:
        raise RuntimeError('Docker containerd image store is outside this data-root plan; separate migration required')
    try: json.loads(status)
    except ValueError: raise RuntimeError('Cannot verify Docker storage backend') from None


def guard_contents(config, runtime):
    paths = [str(mount_path(config[k])) for k in ('data_mount','media_mount')]
    units = [call(['systemd-escape','--path','--suffix=mount',p]) for p in paths]
    guard=(str(runtime/'nekoserver-runtime')+' storage-guard') if native_runtime.backend(config)=='rust' else ('/usr/bin/python3 '+str(runtime/'storage-guard.py'))
    return ('# Managed by NekoServer\n[Unit]\nRequiresMountsFor='+' '.join(paths)+'\n'
            'BindsTo='+' '.join(units)+'\nAfter='+' '.join(units)+'\n'
            '[Service]\nExecStartPre='+guard+
            ' --config '+str(runtime/'storage.json')+'\n')


def install_storage_guard(config):
    root = mount_path(config['data_mount'])
    runtime = safe_path(root/'installer'/'guards'/('v'+VERSION))
    runtime.mkdir(parents=True,exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    for name in ('neko_common.py','storage-guard.py'):
        source = project/name
        target = safe_path(runtime/name)
        if target.exists() and target.read_bytes()!=source.read_bytes():
            raise RuntimeError(f'Installed guard differs: {target}')
        if not target.exists(): atomic_write(target,source.read_bytes(),0o750)
    atomic_json(runtime/'storage.json',{k:config[k] for k in ('data_mount','media_mount','storage')})
    native_runtime.deploy(project,config,runtime)
    override = Path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf')
    desired = guard_contents(config,runtime)
    if override.exists():
        old = safe_path(override).read_text()
        legacy = f'[Unit]\nRequiresMountsFor={root}\n'
        if old != legacy and not old.startswith('# Managed by NekoServer\n'):
            raise RuntimeError('Unmanaged Docker storage override; refusing overwrite')
    write_atomic(override,desired)
    call(['systemctl','daemon-reload'])


def ensure_active_compose(settings):
    """An already running managed engine may still lack its Compose plugin."""
    result = subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=False)
    if result.returncode == 0:
        return
    if not settings.get('install_engine', False):
        raise RuntimeError('Docker Compose missing; set docker.install_engine=true before --apply')
    if not shutil.which('pacman') or platform.system() != 'Linux':
        raise RuntimeError('Automatic Compose installation supports Arch/Nyarch only')
    call(['pacman', '-Si', 'docker-compose'])
    call(['pacman', '-S', '--needed', '--noconfirm', 'docker-compose'], timeout=900)
    call(['docker', 'compose', 'version'])


def run(context):
    settings = context.config.get('docker', {})
    root = settings.get('persistent_root', context.config.get('data_mount', '/srv/nekoserver'))
    if root != context.config.get('data_mount'):
        raise RuntimeError('docker.persistent_root must equal data_mount')
    mount, source = mounted_data_root(root)
    target = Path(root) / 'docker-engine'
    context.log(f'SSD mounted at {mount} from {source}; Docker data-root requested: {target}')
    if not context.apply:
        context.log('Read-only: data-root is not modified. --apply will configure only a fresh Docker installation.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('--apply requires root')
    if not shutil.which('systemctl'):
        raise RuntimeError('systemctl required')
    daemon_file = Path('/etc/docker/daemon.json')
    daemon = prepare_daemon_config(daemon_file, target)
    # Do not mutate a running daemon or silently relocate existing images/volumes.
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'docker'], check=False).returncode == 0
    if active:
        actual = call(['docker', 'info', '--format', '{{.DockerRootDir}}'])
        if actual != str(target):
            raise RuntimeError(f'Active Docker uses {actual}, expected {target}; use separate migration script')
        if not daemon_file.exists() or json.loads(daemon_file.read_text()).get('data-root') != str(target):
            raise RuntimeError('Active Docker root is correct but daemon.json differs; inspect before changing')
        override = Path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf')
        if not override.exists() or f'RequiresMountsFor={root}' not in override.read_text():
            raise RuntimeError('Active Docker lacks verified storage mount guard; stop service before configuring')
        ensure_active_compose(settings)
        context.log('Docker already running with expected data-root and existing SSD guard')
        verify_classic_store()
        install_storage_guard(context.config)
        write_atomic(daemon_file,json.dumps(daemon,indent=2)+'\n')
        context.log('Verified SSD/HDD startup guard installed; running containers were not restarted')
        return
    already_configured = daemon_file.exists() and json.loads(daemon_file.read_text()).get('data-root') == str(target)
    if not already_configured:
        check_existing_docker(target)
    elif target.exists() and any(target.iterdir()) and json.loads(daemon_file.read_text()).get('features',{}).get('containerd-snapshotter') is not False:
        raise RuntimeError('Stopped existing Docker backend is unverified; start and inspect it before changing storage settings')
    has_docker = shutil.which('docker') is not None
    has_compose = has_docker and subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=False).returncode == 0
    if not (has_docker and has_compose):
        if not settings.get('install_engine', False):
            raise RuntimeError('Docker missing; set docker.install_engine=true before --apply')
        if not shutil.which('pacman') or platform.system() != 'Linux':
            raise RuntimeError('Automatic installation supports Arch/Nyarch only')
        call(['pacman', '-S', '--needed', '--noconfirm', 'docker', 'docker-compose'], timeout=900)
    for sub in ('compose', 'appdata', 'vm', 'installer', 'secrets', 'docker-engine'):
        (Path(root) / sub).mkdir(parents=True, exist_ok=True, mode=0o750)
    write_atomic(daemon_file, json.dumps(daemon, indent=2) + '\n')
    # A service-level mount requirement ensures Docker fails rather than writing to OS disk.
    override = Path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf')
    # RequiresMountsFor works for UUID-backed fstab or native systemd mounts.
    install_storage_guard(context.config)
    if settings.get('enable_service', True):
        call(['systemctl', 'enable', '--now', 'docker'], timeout=90)
        actual = call(['docker', 'info', '--format', '{{.DockerRootDir}}'])
        if actual != str(target):
            call(['systemctl', 'stop', 'docker'])
            raise RuntimeError(f'Docker reports incorrect root {actual}; service stopped')
        verify_classic_store()
    context.log(f'Docker data-root set to {target}; systemd mount requirement installed')
