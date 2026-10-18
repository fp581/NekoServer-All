"""Install Rust snapshots of media application state on the separate HDD."""
import os
from pathlib import Path
import re
import subprocess

import native_runtime
from neko_common import VERSION, atomic_json, atomic_write, safe_path, verify_storage

DEPENDENCIES = ['media', 'recovery']
DESCRIPTION = 'Nightly private media application snapshots with automatic restart recovery'


def definitions(config, binary, guard):
    data, media = config['data_mount'], config['media_mount']
    time = config.get('app_backup', {}).get('time', '04:15')
    if not isinstance(time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', time):
        raise RuntimeError('app_backup.time must be HH:MM')
    resume = f'{binary} app-resume --config {guard}'
    return {
        'nekoserver-app-backup.service': (
            '[Unit]\nDescription=NekoServer media application snapshot\n'
            f'RequiresMountsFor={data} {media}\nAfter=docker.service\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=2h\n'
            f'ExecStart={binary} app-snapshot --config {guard}\n'
            f'ExecStopPost={resume}\nTimeoutStopSec=20min\n'
        ),
        'nekoserver-app-backup.timer': (
            '[Unit]\nDescription=NekoServer nightly media application snapshot\n'
            f'[Timer]\nOnCalendar=*-*-* {time}:00\nPersistent=true\n'
            '[Install]\nWantedBy=timers.target\n'
        ),
        'nekoserver-app-resume.service': (
            '[Unit]\nDescription=NekoServer restart applications after interrupted snapshot\n'
            f'RequiresMountsFor={data}\nAfter=docker.service\nRequires=docker.service\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=20min\n'
            f'ExecStart={resume}\n[Install]\nWantedBy=multi-user.target\n'
        ),
    }


def run(context):
    if not context.apply:
        context.log('Application snapshots available with --apply; scheduled worker requires Rust')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    config = context.config
    if native_runtime.backend(config) != 'rust':
        raise RuntimeError('Application snapshots require runtime.backend=rust')
    # Validate calendar and native payload before touching installed files.
    definitions(config, '/placeholder/runtime', '/placeholder/config')
    verify_storage(config)
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, config, Path(config['data_mount']) / 'installer/runtime' / ('v' + VERSION))
    guard = safe_path('/etc/nekoserver-backups/applications.json')
    options = native_runtime.storage_config(config)
    options['media'] = {'services': config['media']['services']}
    units = definitions(config, binary, guard)
    for name in units:
        path = safe_path(Path('/etc/systemd/system') / name)
        if path.exists() and 'Description=NekoServer ' not in path.read_text():
            raise RuntimeError('Unmanaged application backup unit; preserved')
    atomic_json(guard, options)
    for name, contents in units.items():
        atomic_write(Path('/etc/systemd/system') / name, contents, 0o644)
    for command in (
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'enable', '--now', 'nekoserver-app-resume.service'],
        ['systemctl', 'enable', '--now', 'nekoserver-app-backup.timer'],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=1200)
    context.log('Nightly private app snapshots enabled on media HDD; apps stop briefly during each run. No automatic backup deletion.')
