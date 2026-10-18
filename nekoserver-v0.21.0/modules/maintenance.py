"""Install the native monthly backup/rehearsal/update/rollback transaction."""
import os
from pathlib import Path
import re
import subprocess

import native_runtime
from neko_common import VERSION, atomic_json, atomic_write, safe_path, verify_storage, host_timezone

DEPENDENCIES = ['docker', 'recovery', 'photos', 'photo_backup', 'nextcloud']
DESCRIPTION = 'Monthly overnight three-drive backups, isolated restore tests and container updates'


def options(config):
    opts = config.get('maintenance', {})
    if not isinstance(opts, dict):
        raise RuntimeError('maintenance must be an object')
    time = opts.get('time', '02:30')
    if not isinstance(time, str) or not re.fullmatch(r'0[0-5]:[0-5][0-9]', time):
        raise RuntimeError('Maintenance must start at night, HH:MM between 00:00 and 05:59')
    zone = host_timezone(config)
    if not (Path('/usr/share/zoneinfo')/zone).is_file():
        raise RuntimeError('Use an installed IANA maintenance timezone')
    for key, default, low, high in (('minimum_free_gb', 20, 2, 100000), ('health_wait_seconds', 300, 30, 1800), ('night_end_hour', 6, 1, 7)):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid maintenance.'+key)
    if int(time[:2]) >= opts.get('night_end_hour', 6):
        raise RuntimeError('Maintenance starts after its allowed night window')
    if opts.get('require_external', True) is not True:
        raise RuntimeError('Three-drive maintenance requires the external copy')
    return opts


def definitions(config, binary, guard):
    opts = options(config)
    start = opts.get('time', '02:30')
    zone = host_timezone(config)
    command = f'{binary} maintenance-resume --config {guard}'
    shared = f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\nAfter=docker.service\nRequires=docker.service\n'
    return {
        'nekoserver-maintenance.service': '[Unit]\nDescription=NekoServer monthly verified backup and container update\n'+shared+
        '[Service]\nType=oneshot\nUMask=0077\nEnvironment=TZ='+zone+'\nTimeoutStartSec=18h\nTimeoutStopSec=6h\n'+
        f'ExecStart={binary} maintenance --config {guard}\nExecStopPost={command}\n',
        # No daytime catch-up after a powered-off night. Native worker also checks the window.
        'nekoserver-maintenance.timer': '[Unit]\nDescription=NekoServer monthly night maintenance\n[Timer]\n'+
        f'OnCalendar=*-*-01 {start}:00 {zone}\nPersistent=false\nRandomizedDelaySec=0\n[Install]\nWantedBy=timers.target\n',
        'nekoserver-maintenance-resume.service': '[Unit]\nDescription=NekoServer recover interrupted monthly maintenance\n'+shared+
        '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'+f'ExecStart={command}\n[Install]\nWantedBy=multi-user.target\n',
    }


def run(ctx):
    options(ctx.config)
    if not ctx.apply:
        ctx.log('Monthly 02:30 backup, isolated restore rehearsal, container updates and rollback; OS packages excluded')
        return
    if os.geteuid() != 0 or native_runtime.backend(ctx.config) != 'rust':
        raise RuntimeError('Maintenance requires root and runtime.backend=rust')
    config = ctx.config
    if not config.get('photos', {}).get('external_backup_uuid'):
        return {'status': 'needs_setup', 'reason': 'Select the external backup drive before enabling three-drive maintenance'}
    verify_storage(config)
    binary = native_runtime.deploy(Path(__file__).resolve().parents[1], config, Path(config['data_mount'])/'installer/runtime'/('v'+VERSION))
    guard = safe_path('/etc/nekoserver-backups/maintenance.json')
    # Installed native worker has no dependency on the original working directory.
    units=definitions(config,binary,guard)
    for name in units:
        target = safe_path(Path('/etc/systemd/system')/name)
        if target.exists() and 'Description=NekoServer ' not in target.read_text():
            raise RuntimeError('Unmanaged maintenance unit preserved')
    atomic_json(guard,config)
    for name, content in units.items():
        atomic_write(Path('/etc/systemd/system')/name, content, 0o644)
    for args in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'nekoserver-maintenance-resume.service'], ['systemctl', 'enable', '--now', 'nekoserver-maintenance.timer']):
        subprocess.run(args, check=True, capture_output=True, timeout=1200)
    ctx.log('Monthly overnight maintenance enabled; external drive must be connected. Missing/wrong/full drives block updates. Old backups are retained.')
