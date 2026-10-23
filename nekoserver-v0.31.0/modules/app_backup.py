"""Install Rust snapshots of media application state on the separate HDD."""
import os
from pathlib import Path
import re
import subprocess

import native_runtime
from neko_common import VERSION, atomic_json, host_timezone, refresh_settings, safe_path, verify_storage, check_units, write_units

DEPENDENCIES = ['media', 'recovery']
DESCRIPTION = 'Nightly private media application snapshots with automatic restart recovery'


def retention(keep):
    """What happens to older snapshots, as setup says it."""
    if keep == 0:
        return 'Every snapshot is kept (none are deleted automatically).'
    return f'The newest {keep} complete snapshots are kept; older ones are deleted after each successful run.'


def keep_snapshots(config):
    """Nightly snapshots to keep on the HDD; 0 keeps every one (pre-v0.21.1 behaviour)."""
    keep = config.get('app_backup', {}).get('keep_snapshots', 7)
    if type(keep) is not int or not 0 <= keep <= 3650:
        raise RuntimeError('app_backup.keep_snapshots must be a whole number from 0 to 3650')
    return keep


def guard_config(config):
    """The private settings the nightly app backup runs with. The acceptance check
    compares the installed file against this same function, so the two cannot drift."""
    options = native_runtime.storage_config(config)
    options['media'] = {'services': config['media']['services']}
    options['app_backup'] = {'keep_snapshots': keep_snapshots(config)}
    return options


GUARD = Path('/etc/nekoserver-backups/applications.json')


def refresh(config, log, target=None):
    """After every setup run: the installed nightly backup covers the apps selected now
    (an app added with  --module media  is backed up from the next night on)."""
    try:
        wanted = guard_config(config)
    except (KeyError, RuntimeError) as exc:
        log(f'Nightly app backup: settings not refreshed ({exc})')
        return False
    return refresh_settings(target or GUARD, wanted, log, 'Nightly app backup')


def definitions(config, binary, guard):
    data, media = config['data_mount'], config['media_mount']
    time = config.get('app_backup', {}).get('time', '04:15')
    if not isinstance(time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', time):
        raise RuntimeError('app_backup.time must be HH:MM')
    keep_snapshots(config)
    resume = f'{binary} app-resume --config {guard}'
    return {
        'nekoserver-app-backup.service': (
            '[Unit]\nDescription=NekoServer media application snapshot\n'
            f'RequiresMountsFor={data} {media}\nAfter=docker.service\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'
            f'ExecStart={binary} app-snapshot --config {guard}\n'
            f'ExecStopPost={resume}\nTimeoutStopSec=20min\n'
        ),
        'nekoserver-app-backup.timer': (
            '[Unit]\nDescription=NekoServer nightly media application snapshot\n'
            f'[Timer]\nOnCalendar=*-*-* {time}:00 {host_timezone(config)}\nPersistent=true\n'
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
    # Validate calendar and native payload before touching installed files.
    definitions(config, '/placeholder/runtime', '/placeholder/config')
    verify_storage(config)
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, config, Path(config['data_mount']) / 'installer/runtime' / ('v' + VERSION))
    guard = safe_path(GUARD)
    options = guard_config(config)
    units = definitions(config, binary, guard)
    check_units(units, 'application backup')
    atomic_json(guard, options)
    write_units(units)
    for command in (
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'enable', '--now', 'nekoserver-app-resume.service'],
        ['systemctl', 'enable', '--now', 'nekoserver-app-backup.timer'],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=1200)
    context.log('Nightly private app snapshots enabled on media HDD; apps stop briefly during each run. '+retention(keep_snapshots(context.config)))
