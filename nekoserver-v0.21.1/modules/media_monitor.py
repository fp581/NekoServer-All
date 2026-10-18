"""Install a scheduled, read-only audit after media deployment."""
import os
from pathlib import Path
import shutil
import subprocess

import native_runtime
from neko_common import VERSION, atomic_json, atomic_write, safe_path

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Install scheduled media connection checks (Rust runtime)'


def run(context):
    enabled = context.config.get('monitoring',{}).get('enabled',True)
    if type(enabled) is not bool:
        raise RuntimeError('Schedule enable switch must be boolean')
    if not enabled:
        if context.apply:
            if os.geteuid() != 0:
                raise RuntimeError('Root required')
            from modules._schedules import retire
            retire([Path('/etc/systemd/system')/('nekoserver-media-health.'+suffix) for suffix in ('timer','service')], subprocess.run)
        context.log('nekoserver-media-health: disabled; any old managed schedule is retired when applied')
        return
    if not context.apply:
        context.log('Media monitor available; use --apply to install its systemd timer')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Requires sudo')
    data = safe_path(context.config['data_mount'])
    if not data.is_dir() or not os.path.ismount(data):
        raise RuntimeError('Dedicated SSD mount required')
    if not shutil.which('systemctl'):
        raise RuntimeError('systemd required')
    interval = context.config.get('monitoring', {}).get('interval_minutes', 60)
    if type(interval) is not int or not 15 <= interval <= 1440:
        raise RuntimeError('Monitor interval must be an integer from 15 to 1440 minutes')
    project = Path(__file__).resolve().parents[1]
    # Validate native integrity before writing any live monitoring files.
    native_runtime.payload(project, context.config)
    config = safe_path(data / 'scripts' / 'config.json')
    report = data / 'reports' / 'media' / 'latest.json'
    runtime = data / 'installer' / 'runtime' / ('v' + VERSION)
    binary = native_runtime.deploy(project, context.config, runtime)
    command = f'{binary} media-health --config {config} --output {report}'
    service = (
        '[Unit]\nDescription=NekoServer read-only media health audit\n'
        f'RequiresMountsFor={data} {context.config["media_mount"]}\n'
        'After=docker.service\n[Service]\nType=oneshot\n'
        # Exit 3 means remaining setup; a runtime failure (1) stays a failed unit.
        'SuccessExitStatus=3\n'
        f'TimeoutStartSec=300\nExecStart={command}\n'
    )
    timer = (
        '[Unit]\nDescription=NekoServer scheduled media audit\n[Timer]\n'
        f'OnBootSec=5min\nOnUnitInactiveSec={interval}min\n'
        'Persistent=true\nUnit=nekoserver-media-health.service\n'
        '[Install]\nWantedBy=timers.target\n'
    )
    definitions = {
        'nekoserver-media-health.service': service,
        'nekoserver-media-health.timer': timer,
    }
    for name in definitions:
        path = safe_path(Path('/etc/systemd/system') / name)
        if path.exists() and 'Description=NekoServer ' not in path.read_text():
            raise RuntimeError('Unmanaged existing media monitor unit')
    atomic_json(config, context.config)
    for name, contents in definitions.items():
        atomic_write(Path('/etc/systemd/system') / name, contents, 0o644)
    for command in (
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'enable', '--now', 'nekoserver-media-health.timer'],
    ):
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    context.log('Installed scheduled media audit; results at ' + str(report))
