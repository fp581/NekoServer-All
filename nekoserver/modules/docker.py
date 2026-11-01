"""Docker foundation with guarded, non-migrating data-root configuration."""

import native_runtime
import json
import os
import shutil
import subprocess
from pathlib import Path
import host
from neko_common import safe_path, mount_path, atomic_write, atomic_json, findmnt, VERSION, DOCKER_STORAGE_DROPIN

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Verify dedicated SSD and Docker data-root; --apply configures fresh daemon and mount guard'
DAEMON_FILE = Path('/etc/docker/daemon.json')  # Docker's own settings (tests point it elsewhere)


def call(args, timeout=120):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode:
        raise RuntimeError(f"Command failed ({proc.returncode}): {args[0]}: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()


def mounted_data_root(root):
    root = safe_path(root)
    if not root.is_absolute() or str(root) in ('/', '/home', '/var', '/tmp', '/mnt', '/srv'):
        raise RuntimeError(
            'Persistent root must be a dedicated absolute directory under a separately mounted filesystem'
        )
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
    return RuntimeError(
        f'Docker is already set up on this PC and keeps its data in {where}. NekoServer runs Docker '
        f'from the SSD ({target}) and never moves or deletes existing Docker data by itself: see '
        f'"If Docker is already used on this PC" in STORAGE.md, then run setup again'
    )


def prepare_daemon_config(path, target):
    """Return new config; never silently migrate or replace an existing data-root."""
    content = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(content, dict):
        raise RuntimeError('Existing Docker daemon.json must be a JSON object')
    existing = content.get('data-root')
    if existing and existing != str(target):
        raise already_used(existing, target)
    features = content.setdefault('features', {})
    if (
        not isinstance(features, dict)
        or features.get('containerd-snapshotter') is True
        or features.get('containerd-migration') is True
    ):
        raise RuntimeError('containerd image store requires separate verified migration; refusing backend switch')
    features['containerd-snapshotter'] = False
    content['data-root'] = str(target)
    bound_logs(content)
    return content


# Docker's systemd units: the service, and the socket that starts it again on demand.
DOCKER_SERVICE = 'docker.service'
DOCKER_SOCKET = 'docker.socket'

# Settings that daemon.json and a flag on Docker's service command line can both set:
# set in both, Docker refuses to start ("specified both as a flag and in the
# configuration file").
FLAG_KEYS = {
    '--data-root': 'data-root',
    '-g': 'data-root',
    '--graph': 'data-root',
    '--log-driver': 'log-driver',
    '--log-opt': 'log-opts',
    '--storage-driver': 'storage-driver',
    '-s': 'storage-driver',
}


def service_flags(run=subprocess.run):
    """The command line Docker's service starts it with (its ExecStart, drop-ins included)."""
    try:
        shown = run(
            ['systemctl', 'show', DOCKER_SERVICE, '-p', 'ExecStart', '--value'],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return shown.stdout if shown.returncode == 0 else ''


def flag_conflicts(content, command):
    """The daemon.json settings this Docker's service already sets as flags."""
    words = [w.split('=', 1)[0] for w in command.replace(';', ' ').split()]
    return sorted({FLAG_KEYS[w] for w in words if w in FLAG_KEYS and FLAG_KEYS[w] in content})


def validate_daemon(content, run=subprocess.run):
    """Docker's own check of the new daemon.json, before it replaces the old one: a file
    Docker refuses would leave it unable to start. Docker before 23 has no such check."""
    conflicts = flag_conflicts(content, service_flags(run))
    if conflicts:
        raise RuntimeError(
            'Docker\'s service already sets ' + ', '.join(conflicts) + ' on its command line (a drop-in '
            'in /etc/systemd/system/docker.service.d, say), and NekoServer sets it in /etc/docker/daemon.json: '
            'Docker refuses to start with both. Take the flag out of the service, then run setup again'
        )
    dockerd = shutil.which('dockerd')
    if not dockerd:
        return
    import tempfile

    with tempfile.NamedTemporaryFile('w', suffix='.json') as draft:  # no secrets in it
        json.dump(content, draft)
        draft.flush()
        checked = run(
            [dockerd, '--validate', '--config-file', draft.name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    said = (checked.stdout + checked.stderr).strip()
    if checked.returncode and 'unknown flag' not in said:
        raise RuntimeError(f'Docker refuses the new /etc/docker/daemon.json ({said[-300:]}); nothing was changed')


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


def store_in_use(default):
    """Whether Docker's own store holds anything of anyone's: a container, an image or a
    volume. Docker makes the folder tree on its first start, so the folder alone (as after
    the package was installed, or after NekoServer was uninstalled) proves nothing."""
    default = Path(default)
    places = [default / 'containers', default / 'volumes', *default.glob('image/*/imagedb/content/sha256')]
    return any(
        place.is_dir() and any(p.name not in ('metadata.db', 'backingFsBlockDev') for p in place.iterdir())
        for place in places
    )


def active_store_in_use(call=None):
    """The same for a running Docker: anything in it, by its own count."""
    call = call or globals()['call']
    counts = call(['docker', 'info', '--format', '{{.Containers}} {{.Images}}']).split()
    return counts != ['0', '0'] or bool(call(['docker', 'volume', 'ls', '-q']).strip())


def check_existing_docker(target):
    """Never convert an existing Docker installation that holds something; one with
    nothing in it is taken over (it starts again from the SSD)."""
    default = Path('/var/lib/docker')
    if default.exists() and store_in_use(default):
        raise already_used(default, target)
    if target.exists() and any(target.iterdir()):
        # Safe to reuse a target only when Docker already points to it.
        if (target.parent / 'installer/host-checkpoint.json').is_file():
            # The usual reason: Linux was reinstalled and the SSD put back. Name the way out.
            raise RuntimeError(
                f'The SSD already holds the apps of an earlier NekoServer install ({target}); after '
                f'reinstalling Linux they are taken over with a check of the Docker version first, not by '
                f'setup itself. Follow "Replace the OS and reattach the existing store" in RECOVERY.md '
                f'(sudo python3 {target.parent}/installer/releases/v{VERSION}/recover.py reattach), '
                f'then run setup again'
            )
        raise RuntimeError(
            f'{target} is not empty; refusing to adopt existing Docker data without migration verification'
        )


def write_atomic(path, payload):
    atomic_write(path, payload, 0o644)


def verify_classic_store():
    status = call(['docker', 'info', '--format', '{{json .DriverStatus}}'])
    if 'io.containerd.snapshotter' in status:
        raise RuntimeError('Docker containerd image store is outside this data-root plan; separate migration required')
    try:
        json.loads(status)
    except ValueError:
        raise RuntimeError('Cannot verify Docker storage backend') from None


def guard_contents(config, runtime):
    paths = [str(mount_path(config[k])) for k in ('data_mount', 'media_mount')]
    units = [call(['systemd-escape', '--path', '--suffix=mount', p]) for p in paths]
    guard = str(runtime / 'nekoserver-runtime') + ' storage-guard'
    return (
        '# Managed by NekoServer\n[Unit]\nRequiresMountsFor=' + ' '.join(paths) + '\n'
        'BindsTo=' + ' '.join(units) + '\nAfter=' + ' '.join(units) + '\n'
        '[Service]\nExecStartPre=' + guard + ' --config ' + str(runtime / 'storage.json') + '\n'
    )


def install_storage_guard(config):
    root = mount_path(config['data_mount'])
    runtime = safe_path(root / 'installer' / 'guards' / ('v' + VERSION))
    runtime.mkdir(parents=True, exist_ok=True)
    project = Path(__file__).resolve().parents[1]
    atomic_json(runtime / 'storage.json', {k: config[k] for k in ('data_mount', 'media_mount', 'storage')})
    guard = native_runtime.deploy(project, config, runtime)
    # Docker's start guard must run on this Linux, or Docker never starts: a copy built on
    # a newer Linux (kept on the SSD through a reinstall) may not. Checked before Docker's
    # settings are touched.
    if not native_runtime.runs_here(guard):
        raise RuntimeError(
            f'The NekoServer program at {guard} does not start on this Linux (it was built on another '
            'one), so Docker could not start with it. Run  sudo bash install.sh  first: it builds the '
            'program for this Linux. Docker\'s settings were not changed.'
        )
    override = DOCKER_STORAGE_DROPIN
    desired = guard_contents(config, runtime)
    if override.exists():
        old = safe_path(override).read_text()
        legacy = f'[Unit]\nRequiresMountsFor={root}\n'
        if old != legacy and not old.startswith('# Managed by NekoServer\n'):
            raise RuntimeError('Unmanaged Docker storage override; refusing overwrite')
    write_atomic(override, desired)
    call(['systemctl', 'daemon-reload'])


def ensure_active_compose(settings):
    """An already running managed engine may still lack its Compose plugin."""
    result = subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=False, timeout=60)
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
    root = str(mount_path(root))  # the same spelling the drop-in uses (no trailing slash)
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
    if host.snap_docker():
        raise RuntimeError(host.SNAP_DOCKER)
    daemon_file = DAEMON_FILE
    daemon = prepare_daemon_config(daemon_file, target)
    # Do not mutate a running daemon or silently relocate existing images/volumes.
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'docker'], check=False, timeout=30).returncode == 0
    if active and call(['docker', 'info', '--format', '{{.DockerRootDir}}']) != str(target):
        actual = call(['docker', 'info', '--format', '{{.DockerRootDir}}'])
        if active_store_in_use():
            raise already_used(actual, target)
        # Nothing in it (just installed, or NekoServer uninstalled): it runs from the SSD from now on.
        context.log(
            f'Docker runs from {actual} with nothing in it (no container, image or volume): stopped, to run from the SSD'
        )
        subprocess.run(['systemctl', 'stop', DOCKER_SOCKET, DOCKER_SERVICE], check=True, timeout=300)
        active = False
    if active:
        if not daemon_file.exists() or json.loads(daemon_file.read_text()).get('data-root') != str(target):
            raise RuntimeError('Active Docker root is correct but daemon.json differs; inspect before changing')
        override = DOCKER_STORAGE_DROPIN
        if not override.exists() or f'RequiresMountsFor={root}' not in override.read_text():
            raise RuntimeError('Active Docker lacks verified storage mount guard; stop service before configuring')
        ensure_active_compose(settings)
        context.log('Docker already running with expected data-root and existing SSD guard')
        verify_classic_store()
        install_storage_guard(context.config)
        validate_daemon(daemon)
        write_atomic(daemon_file, json.dumps(daemon, indent=2) + '\n')
        context.log('Verified SSD/HDD startup guard installed; running containers were not restarted')
        return
    already_configured = daemon_file.exists() and json.loads(daemon_file.read_text()).get('data-root') == str(target)
    if not already_configured:
        check_existing_docker(target)
    elif (
        target.exists()
        and any(target.iterdir())
        and json.loads(daemon_file.read_text()).get('features', {}).get('containerd-snapshotter') is not False
    ):
        raise RuntimeError(
            'Stopped existing Docker backend is unverified; start and inspect it before changing storage settings'
        )
    has_docker = shutil.which('docker') is not None
    has_compose = (
        has_docker
        and subprocess.run(['docker', 'compose', 'version'], capture_output=True, check=False, timeout=60).returncode
        == 0
    )
    if not (has_docker and has_compose):
        if not settings.get('install_engine', False):
            raise RuntimeError('Docker missing; set docker.install_engine=true before --apply')
        host.current().install(['docker', 'docker-compose'])
    for sub in ('compose', 'appdata', 'vm', 'installer', 'secrets', 'docker-engine'):
        (Path(root) / sub).mkdir(parents=True, exist_ok=True, mode=0o750)
    # The mount requirement first, then the SSD data-root: a run that stops in between
    # (Ctrl+C, a failed runtime build) never leaves Docker set to the SSD folder without
    # the guard, which would let it start at boot before the SSD and fill that folder
    # on the system disk. RequiresMountsFor works for UUID-backed fstab or native mounts.
    install_storage_guard(context.config)
    validate_daemon(daemon)
    write_atomic(daemon_file, json.dumps(daemon, indent=2) + '\n')
    if settings.get('enable_service', True):
        # Earlier failed starts (the guard missing after a drive problem, say) make systemd
        # refuse a start with "attempted too often"; that count is cleared first.
        subprocess.run(
            ['systemctl', 'reset-failed', DOCKER_SERVICE, DOCKER_SOCKET], capture_output=True, timeout=30, check=False
        )
        call(['systemctl', 'enable', '--now', 'docker'], timeout=90)
        actual = call(['docker', 'info', '--format', '{{.DockerRootDir}}'])
        if actual != str(target):
            call(['systemctl', 'stop', 'docker'])
            raise RuntimeError(f'Docker reports incorrect root {actual}; service stopped')
        verify_classic_store()
    context.log(f'Docker data-root set to {target}; systemd mount requirement installed')
