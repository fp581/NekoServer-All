"""Docker foundation with guarded, non-migrating data-root configuration."""
import native_runtime
import json
import os
import shutil
import subprocess
from pathlib import Path
import host
from neko_common import safe_path, mount_path, atomic_write, atomic_json, findmnt, VERSION, DOCKER_STORAGE_DROPIN

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
    mountpoint = findmnt('TARGET', root, '--target')
    if mountpoint == '/':
        raise RuntimeError(f'{root} is on the OS filesystem; mount the data SSD first')
    if mountpoint is None or Path(mountpoint) != root:
        raise RuntimeError(f'Data root must itself be a mountpoint, not a child of {mountpoint}')
    return mountpoint, findmnt('SOURCE', root, '--target')


def already_used(where, target):
    """Docker was already set up on this PC: NekoServer never moves or deletes its data."""
    return RuntimeError(f'Docker is already set up on this PC and keeps its data in {where}. NekoServer runs Docker '
                        f'from the SSD ({target}) and never moves or deletes existing Docker data by itself: see '
                        f'"If Docker is already used on this PC" in STORAGE.md, then run setup again')


def prepare_daemon_config(path, target):
    """Return new config; never silently migrate or replace an existing data-root."""
    content = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(content, dict):
        raise RuntimeError('Existing Docker daemon.json must be a JSON object')
    existing = content.get('data-root')
    if existing and existing != str(target):
        raise already_used(existing, target)
    features=content.setdefault('features',{})
    if not isinstance(features,dict) or features.get('containerd-snapshotter') is True or features.get('containerd-migration') is True:
        raise RuntimeError('containerd image store requires separate verified migration; refusing backend switch')
    features['containerd-snapshotter']=False
    content['data-root'] = str(target)
    bound_logs(content)
    return content


# Docker keeps container logs forever by default (json-file without rotation), which
# can fill the SSD. Applies to containers created after Docker next starts.
LOG_LIMITS = {'max-size': '10m', 'max-file': '3'}


def bound_logs(content):
    """Add log rotation defaults to daemon.json without overriding your own choices:
    another log driver (journald, local, ...) or limits you set are kept as they are."""
    if content.get('log-driver', 'json-file') != 'json-file':
        return content
    opts = content.setdefault('log-opts', {})
    if not isinstance(opts, dict):
        raise RuntimeError('Existing Docker daemon.json log-opts must be a JSON object')
    for key, value in LOG_LIMITS.items():
        opts.setdefault(key, value)
    return content


def unbounded_logs(inspected):
    """Names of containers (from docker inspect) that write json-file logs with no size limit."""
    names = []
    for c in inspected:
        log = (c.get('HostConfig') or {}).get('LogConfig') or {}
        if log.get('Type', 'json-file') == 'json-file' and not (log.get('Config') or {}).get('max-size'):
            names.append(str(c.get('Name', '?')).lstrip('/'))
    return sorted(names)


def check_existing_docker(target):
    """Never convert an existing Docker installation automatically."""
    default = Path('/var/lib/docker')
    if default.exists() and any(default.iterdir()):
        raise already_used(default, target)
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
    guard=str(runtime/'nekoserver-runtime')+' storage-guard'
    return ('# Managed by NekoServer\n[Unit]\nRequiresMountsFor='+' '.join(paths)+'\n'
            'BindsTo='+' '.join(units)+'\nAfter='+' '.join(units)+'\n'
            '[Service]\nExecStartPre='+guard+
            ' --config '+str(runtime/'storage.json')+'\n')


def install_storage_guard(config):
    root = mount_path(config['data_mount'])
    runtime = safe_path(root/'installer'/'guards'/('v'+VERSION))
    runtime.mkdir(parents=True,exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    atomic_json(runtime/'storage.json',{k:config[k] for k in ('data_mount','media_mount','storage')})
    native_runtime.deploy(project,config,runtime)
    override = DOCKER_STORAGE_DROPIN
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
    host.current().install(['docker-compose'])
    call(['docker', 'compose', 'version'])


def run(context):
    settings = context.config.get('docker', {})
    root = settings.get('persistent_root', context.config.get('data_mount', '/srv/nekoserver'))
    if root != context.config.get('data_mount'):
        raise RuntimeError('docker.persistent_root must equal data_mount')
    root = str(mount_path(root))   # the same spelling the drop-in uses (no trailing slash)
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
            raise already_used(actual, target)
        if not daemon_file.exists() or json.loads(daemon_file.read_text()).get('data-root') != str(target):
            raise RuntimeError('Active Docker root is correct but daemon.json differs; inspect before changing')
        override = DOCKER_STORAGE_DROPIN
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
        host.current().install(['docker', 'docker-compose'])
    for sub in ('compose', 'appdata', 'vm', 'installer', 'secrets', 'docker-engine'):
        (Path(root) / sub).mkdir(parents=True, exist_ok=True, mode=0o750)
    write_atomic(daemon_file, json.dumps(daemon, indent=2) + '\n')
    # A service-level mount requirement ensures Docker fails rather than writing to OS disk.
    override = DOCKER_STORAGE_DROPIN
    # RequiresMountsFor works for UUID-backed fstab or native systemd mounts.
    install_storage_guard(context.config)
    if settings.get('enable_service', True):
        # Earlier failed starts (the guard missing after a drive problem, say) make systemd
        # refuse a start with "attempted too often"; that count is cleared first.
        subprocess.run(['systemctl', 'reset-failed', 'docker.service', 'docker.socket'], capture_output=True, timeout=30, check=False)
        call(['systemctl', 'enable', '--now', 'docker'], timeout=90)
        actual = call(['docker', 'info', '--format', '{{.DockerRootDir}}'])
        if actual != str(target):
            call(['systemctl', 'stop', 'docker'])
            raise RuntimeError(f'Docker reports incorrect root {actual}; service stopped')
        verify_classic_store()
    context.log(f'Docker data-root set to {target}; systemd mount requirement installed')
