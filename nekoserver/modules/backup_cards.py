"""Backup cards (0.52.0): the nightly copy onto SD cards and USB sticks of the set.

Switched on on setup's first page (explanations.EXTRAS "backup_cards"); off, this step
writes nothing, and retires a schedule it wrote before. The cards themselves are chosen
on the dashboard's Backup cards page."""

import os
from pathlib import Path
import subprocess

import native_runtime
from neko_common import (
    SHARED,
    SYSTEMD_UNITS,
    VERSION,
    cards_enabled,
    check_units,
    host_timezone,
    job_times,
    nightly_timer,
    verify_storage,
    write_units,
)

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Nightly copy of photos and documents onto the backup cards plugged in'

SERVICE = SHARED['cards']['service']
TIMER = SHARED['cards']['timer']


def definitions(config, binary, guard):
    """The job's service, which "Write to the card now" on the dashboard starts too, and
    its nightly timer, an hour after the other nightly jobs (job_times "cards"). A night
    missed while the PC was off is not caught up: the next night writes it."""
    return {
        SERVICE: (
            '[Unit]\nDescription=NekoServer backup cards\n'
            f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=12h\n'
            f'ExecStart={binary} cards --config {guard}\n'
        ),
        TIMER: nightly_timer(
            'NekoServer nightly backup cards', job_times(config)['cards'], host_timezone(config), persistent=False
        ),
    }


def run(ctx):
    if not cards_enabled(ctx.config):
        if ctx.apply:
            if os.geteuid() != 0:
                raise RuntimeError('Root required')
            from modules._schedules import retire

            retire([SYSTEMD_UNITS / TIMER, SYSTEMD_UNITS / SERVICE], subprocess.run)
        ctx.log('Backup cards: off (switch them on on setup\'s first page, under Storage)')
        return
    definitions(ctx.config, '/placeholder/runtime', '/placeholder/config')
    if not ctx.apply:
        ctx.log(
            'Backup cards: every night at '
            + job_times(ctx.config)['cards']
            + ', a copy of what is new onto the card plugged in; choose the cards on the dashboard'
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
    check_units(units, 'backup cards')
    native_runtime.backup_guard(ctx.config)
    write_units(units)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'enable', '--now', TIMER], check=True, capture_output=True, timeout=30)
    ctx.log(
        'Backup cards: every night at '
        + job_times(ctx.config)['cards']
        + '. Choose the cards on the dashboard\'s Backup cards page (it asks for your PIN).'
    )
