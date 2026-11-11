"""Install nightly, versioned SSD music backups on the verified HDD."""

import os
from pathlib import Path
import subprocess

import native_runtime
from neko_common import (
    VERSION,
    media_group,
    verify_storage,
    host_timezone,
    check_units,
    job_times,
    nightly_timer,
    write_units,
    SYSTEMD_UNITS,
)

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Nightly immutable music backup with checksum verification on the HDD'


def definitions(config, binary, guard):
    time = job_times(config)['music']  # half an hour before the daily time setup asked for (backup_times)
    return {
        'nekoserver-music-backup.service': (
            '[Unit]\nDescription=NekoServer versioned music backup\n'
            f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'
            f'ExecStart={binary} music-backup --config {guard}\n'
        ),
        'nekoserver-music-backup.timer': nightly_timer(
            'NekoServer nightly music backup', time, host_timezone(config), persistent=False
        ),
    }


def run(ctx):
    enabled = ctx.config.get('music', {}).get('backup_enabled', True)
    if type(enabled) is not bool:
        raise RuntimeError('Schedule enable switch must be boolean')
    if not enabled or not media_group(ctx.config, 'music'):
        if ctx.apply:
            if os.geteuid() != 0:
                raise RuntimeError('Root required')
            from modules._schedules import retire

            retire(
                [SYSTEMD_UNITS / ('nekoserver-music-backup.' + suffix) for suffix in ('timer', 'service')],
                subprocess.run,
            )
        ctx.log('nekoserver-music-backup: disabled; any old managed schedule is retired when applied')
        return
    definitions(ctx.config, '/placeholder/runtime', '/placeholder/config')
    if not ctx.apply:
        ctx.log(
            'Music: nightly '
            + ctx.config.get('music', {}).get('backup_time', '03:45')
            + ' backup to HDD; older versions retained, no automatic deletions'
        )
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    verify_storage(ctx.config)
    binary = native_runtime.deploy(
        Path(__file__).resolve().parents[1],
        ctx.config,
        Path(ctx.config['data_mount']) / 'installer/runtime' / ('v' + VERSION),
    )
    units = definitions(ctx.config, binary, '/etc/nekoserver-backups/storage.json')
    check_units(units, 'music backup')
    native_runtime.backup_guard(ctx.config)
    write_units(units)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    subprocess.run(
        ['systemctl', 'enable', '--now', 'nekoserver-music-backup.timer'], check=True, capture_output=True, timeout=30
    )
