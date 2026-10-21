"""Install OS-wide no-sleep policy, bounded journal and guarded HDD boot policy."""
import os
from pathlib import Path
import shutil
import subprocess

import native_runtime
from neko_common import SYSTEMD_UNITS, VERSION, atomic_json, atomic_write, check_units, safe_path, verify_storage, write_units

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
    if policy == 'unchanged':
        previous=safe_path(SYSTEMD_UNITS/'nekoserver-hdd-policy.service')
        if previous.exists():
            if 'Description=NekoServer verified media HDD standby policy\n' not in previous.read_text():raise RuntimeError('Unmanaged HDD unit preserved')
            subprocess.run(['systemctl','disable','--now','nekoserver-hdd-policy.service'],check=True,capture_output=True,timeout=30)
        context.log('No HDD command issued; earlier managed boot policy disabled. Restore previously changed hardware settings explicitly if needed.')
        return
    verify_storage(context.config)
    if not shutil.which('hdparm') and shutil.which('pacman'):
        subprocess.run(['pacman','-Si','hdparm'],check=True,capture_output=True,timeout=30)
        subprocess.run(['pacman','-S','--needed','--noconfirm','hdparm'],check=True,capture_output=True,timeout=900)
    if not shutil.which('hdparm'):
        return {'status': 'needs_setup', 'reason': 'No-sleep/log limits installed; install hdparm then reapply server_os for the SATA HDD policy'}
    binary = native_runtime.deploy(Path(__file__).resolve().parents[1], context.config, Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION))
    guard = safe_path('/etc/nekoserver-os/storage.json')
    check_units(['nekoserver-hdd-policy.service'], 'HDD policy')
    atomic_json(guard, hdd_guard(context.config))
    write_units({'nekoserver-hdd-policy.service': unit(binary, guard, context.config)})
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    result = subprocess.run(['systemctl', 'enable', 'nekoserver-hdd-policy.service'], capture_output=True, timeout=190)
    if result.returncode:
        return {'status': 'needs_setup', 'reason': 'No-sleep/log limits installed; HDD command failed or hardware is unsupported. Inspect the policy service; no blanket drive setting was used'}
    repeated=subprocess.run(['systemctl','restart','nekoserver-hdd-policy.service'],capture_output=True,timeout=190)
    if repeated.returncode:return {'status':'needs_setup','reason':'HDD policy rerun failed; inspect the managed service'}
    context.log('Verified SATA media HDD policy applied at boot: '+policy+'; firmware/bridge behavior still needs hardware verification.')
