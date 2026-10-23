"""Install OS-wide no-sleep policy, bounded journal and guarded HDD boot policy."""
import os
from pathlib import Path
import shutil
import subprocess

import native_runtime
import host
from neko_common import SYSTEMD_UNITS, VERSION, atomic_json, atomic_write, check_units, safe_path, verify_storage, write_units

HDD_UNIT = 'nekoserver-hdd-policy.service'
# Brings the apps back when the SSD or HDD returns after a drop-out (see runtime drive_return.rs).
DRIVE_UNIT = 'nekoserver-drive-return.service'
DRIVE_SETTINGS = Path('/etc/nekoserver-os/drives.json')

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Disable OS sleep, bound system logs and configure verified media HDD standby'
TARGETS = ('sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target', 'suspend-then-hibernate.target')
MARKER = '# Managed by NekoServer server_os\n'


def settings(config):
    opts = config.get('server_os', {})
    if not isinstance(opts, dict):
        raise RuntimeError('server_os must be an object')
    policy = opts.get('hdd_policy', 'keep_spinning')
    if policy not in ('keep_spinning', 'idle', 'unchanged'):
        raise RuntimeError('hdd_policy must be keep_spinning, idle or unchanged')
    minutes = opts.get('hdd_idle_minutes', 60)
    if type(minutes) is not int or minutes not in range(30, 331, 30):
        raise RuntimeError('hdd_idle_minutes must be 30..330 in 30-minute steps')
    values = {}
    for key, default, low, high in (
        ('journal_disk_mb', 512, 64, 4096), ('journal_ram_mb', 64, 16, 256), ('journal_retention_days', 14, 1, 90),
    ):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid journal bound')
        values[key] = value
    return policy, values


def hdd_guard(config):
    policy, _ = settings(config)
    return {**native_runtime.storage_config(config), 'server_os': {'hdd_policy':policy, 'hdd_idle_minutes':config.get('server_os',{}).get('hdd_idle_minutes',60)}}


def files(config):
    _, values = settings(config)
    return {
        '/etc/systemd/sleep.conf.d/90-nekoserver.conf': MARKER + '[Sleep]\nAllowSuspend=no\nAllowHibernation=no\nAllowHybridSleep=no\nAllowSuspendThenHibernate=no\n',
        '/etc/systemd/logind.conf.d/90-nekoserver.conf': MARKER + '[Login]\nIdleAction=ignore\n',
        '/etc/systemd/journald.conf.d/90-nekoserver.conf': MARKER + (
            f'[Journal]\nSystemMaxUse={values["journal_disk_mb"]}M\nRuntimeMaxUse={values["journal_ram_mb"]}M\n'
            f'MaxRetentionSec={values["journal_retention_days"]}day\nSystemKeepFree=1G\n'
        ),
    }


def drive_devices(config, run=subprocess.run):
    """systemd's names for the SSD's and HDD's devices, by their IDs."""
    names = []
    for key in ('data_ssd', 'media_hdd'):
        uuid = config['storage'][key]['uuid']
        shown = run(['systemd-escape', '--path', '--suffix=device', '/dev/disk/by-uuid/' + uuid],
                    capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        names.append(shown)
    return names


def drive_unit(binary, devices):
    """Run whenever either drive appears (at boot too, when there is nothing to do), after
    the drives have been mounted at boot."""
    return (
        '[Unit]\nDescription=NekoServer bring the apps back when a drive returns\n'
        'After=local-fs.target\n'
        '[Service]\nType=oneshot\n'
        f'ExecStart={binary} drive-return --config {DRIVE_SETTINGS}\n'
        'TimeoutStartSec=900\n'
        '[Install]\nWantedBy=' + ' '.join(devices) + '\n'
    )


def install_drive_return(config, project, run=subprocess.run):
    binary = native_runtime.deploy(project, config, Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION))
    check_units([DRIVE_UNIT], 'Drive return')
    atomic_json(safe_path(DRIVE_SETTINGS), native_runtime.storage_config(config))
    write_units({DRIVE_UNIT: drive_unit(binary, drive_devices(config, run))})
    run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    # Re-enabled each run: the drives' IDs (and so the devices it waits for) may change.
    run(['systemctl', 'reenable', DRIVE_UNIT], check=True, capture_output=True, timeout=30)


def unit(binary, guard, config):
    return (
        '[Unit]\nDescription=NekoServer verified media HDD standby policy\n'
        f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        f'ExecStart={binary} hdd-policy --config {guard}\nTimeoutStartSec=180\n'
        '[Install]\nWantedBy=multi-user.target\n'
    )


def run(context):
    policy, _ = settings(context.config)
    definitions = files(context.config)
    if not context.apply:
        context.log('Server OS: disable suspend/hibernate, cap journal; HDD policy=' + policy + '. Linux memory cache stays managed by the kernel.')
        return
    if os.geteuid() != 0 or not shutil.which('systemctl'):
        raise RuntimeError('Server OS setup requires root and systemd')
    for name in definitions:
        path = safe_path(name)
        if path.exists() and not path.read_text().startswith(MARKER):
            raise RuntimeError('Unmanaged OS drop-in preserved')
    # Masks are deliberate symlinks to /dev/null; inspect their parents separately.
    for name in TARGETS:
        path = SYSTEMD_UNITS / name
        safe_path(path.parent)
        if path.is_symlink():
            if os.readlink(path) != '/dev/null':
                raise RuntimeError('Custom sleep target link preserved')
        elif path.exists():
            raise RuntimeError('Custom sleep target unit preserved')
    for name, content in definitions.items():
        atomic_write(Path(name), content, 0o644)
    subprocess.run(['systemctl', 'mask', *TARGETS], check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'restart', 'systemd-journald.service'], check=True, capture_output=True, timeout=30)
    # Existing logind sessions are retained. Masks/sleep policy apply immediately;
    # IdleAction is also loaded on the next normal reboot.
    context.log('OS sleep/hibernation blocked; journal bounds installed. Display blanking may remain enabled.')
    install_drive_return(context.config, Path(__file__).resolve().parents[1])
    context.log('Drive return: when the SSD or HDD comes back after it disappeared, its mount is renewed and the apps start again by themselves')
    if policy == 'unchanged':
        previous=safe_path(SYSTEMD_UNITS/HDD_UNIT)
        if previous.exists():
            if 'Description=NekoServer verified media HDD standby policy\n' not in previous.read_text():raise RuntimeError('Unmanaged HDD unit preserved')
            subprocess.run(['systemctl','disable','--now',HDD_UNIT],check=True,capture_output=True,timeout=30)
        context.log('No HDD command issued; earlier managed boot policy disabled. Restore previously changed hardware settings explicitly if needed.')
        return
    verify_storage(context.config)
    if not shutil.which('hdparm'):
        family=host.current()
        if family.available('hdparm'):family.install(['hdparm'])
    if not shutil.which('hdparm'):
        return {'status': 'needs_setup', 'reason': 'No-sleep/log limits installed; install hdparm then reapply server_os for the SATA HDD policy'}
    binary = native_runtime.deploy(Path(__file__).resolve().parents[1], context.config, Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION))
    guard = safe_path('/etc/nekoserver-os/storage.json')
    check_units([HDD_UNIT], 'HDD policy')
    atomic_json(guard, hdd_guard(context.config))
    write_units({HDD_UNIT: unit(binary, guard, context.config)})
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    result = subprocess.run(['systemctl', 'enable', HDD_UNIT], capture_output=True, timeout=190, check=False)
    if result.returncode:
        return {'status': 'needs_setup', 'reason': 'No-sleep/log limits installed; HDD command failed or hardware is unsupported. Inspect the policy service; no blanket drive setting was used'}
    repeated=subprocess.run(['systemctl','restart',HDD_UNIT],capture_output=True,timeout=190,check=False)
    if repeated.returncode:
        def hdd_service_said(run=subprocess.run):
            """The runtime's own last words from the service's log (it says, for example,
            that only an internal rotating SATA HDD is supported)."""
            try:
                shown=run(['journalctl','-u',HDD_UNIT,'-n','20','-o','cat','--no-pager'],capture_output=True,text=True,timeout=20,check=False).stdout
            except (OSError,subprocess.SubprocessError):
                shown=''
            lines=[line.strip() for line in shown.splitlines() if 'runtime failed' in line]
            return lines[-1].split('runtime failed:',1)[-1].strip().rstrip('.') if lines else 'no reason recorded'
        return {'status':'needs_setup','reason':'The HDD power-saving service did not start: '+hdd_service_said()+
                '. Setup goes on without it (the drive keeps its own power settings); the log is  sudo journalctl -u '+HDD_UNIT}
    context.log('Verified SATA media HDD policy applied at boot: '+policy+'; firmware/bridge behavior still needs hardware verification.')
