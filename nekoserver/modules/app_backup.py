"""Install Rust snapshots of media application state on the separate HDD."""

import os
from pathlib import Path
import subprocess

import native_runtime
from neko_common import (
    RESUME_TIMEOUT,
    media_services,
    VERSION,
    atomic_json,
    host_timezone,
    nightly_timer,
    refresh_settings,
    safe_path,
    verify_storage,
    check_units,
    write_units,
    job_times,
    SYSTEMD_UNITS,
)

DEPENDENCIES = ['media', 'recovery']
DESCRIPTION = 'Nightly private media application snapshots with automatic restart recovery'


def retention(keep):
    """What happens to older snapshots, as setup says it."""
    if keep == 0:
        return 'Every snapshot is kept (none are deleted automatically).'
    return f'The newest {keep} complete snapshots are kept; older ones are deleted after each successful run.'


# How many nightly backups are kept (0.50.0): 3 on a new server, a whole number from 1 to
# 7. One count for the app backups ("app_backup") and one for the settings backups
# ("host_backup"); nothing else is counted by them.
KEEP = 'keep_snapshots'
KEEP_DEFAULT, KEEP_LEAST, KEEP_MOST = 3, 1, 7
# Beside a count from before 0.50.0 that is outside 1 to 7 (up to 3650 then, and 0 kept
# every backup): setup notes that it is the earlier setting (installer.py,
# review_backup_counts). It goes on working exactly as it did, and nothing is removed for
# the new limit, until the person changes it.
KEEP_EARLIER = 'keep_snapshots_earlier'
EARLIER_MOST = 3650
APP_BACKUPS = 'nightly app backups'


def kept_count(config, group, what):
    """(how many `what` the settings keep under `group`, whether that is an earlier
    setting outside 1 to 7, kept as it was). Anything else is refused in plain words."""
    options = config.get(group, {})
    if not isinstance(options, dict):
        raise RuntimeError(f'"{group}" in my-config.json must be an object')
    keep = options.get(KEEP, KEEP_DEFAULT)
    if type(keep) is int and KEEP_LEAST <= keep <= KEEP_MOST:
        return keep, False
    if options.get(KEEP_EARLIER) is True and type(keep) is int and 0 <= keep <= EARLIER_MOST:
        return keep, True
    raise RuntimeError(
        f'"{KEEP}" under "{group}" in my-config.json is {keep!r}. NekoServer keeps from {KEEP_LEAST} to '
        f'{KEEP_MOST} {what}: write a whole number from {KEEP_LEAST} to {KEEP_MOST} there (a new server keeps '
        f'{KEEP_DEFAULT})'
    )


def runtime_count(config, group, what):
    """The count as the scheduled job is given it: with "earlier" when it is an earlier
    setting outside 1 to 7, which the job then keeps to as before (0: every backup)."""
    keep, earlier = kept_count(config, group, what)
    return {KEEP: keep, **({'earlier': True} if earlier else {})}


def keep_snapshots(config):
    """Nightly app snapshots to keep on the HDD: 1 to 7 (an earlier setting: see KEEP_EARLIER)."""
    return kept_count(config, 'app_backup', APP_BACKUPS)[0]


def guard_config(config):
    """The private settings the nightly app backup runs with. The acceptance check
    compares the installed file against this same function, so the two cannot drift."""
    options = native_runtime.storage_config(config)
    options['media'] = {'services': media_services(config)}
    options['app_backup'] = runtime_count(config, 'app_backup', APP_BACKUPS)
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
    time = job_times(config)['app']  # the daily time setup asked for (backup_times)
    keep_snapshots(config)
    resume = f'{binary} app-resume --config {guard}'
    # A night missed while the PC was off is caught up at boot, when the resume service
    # runs too and holds the same job lock: the backup waits for it instead of failing.
    return {
        'nekoserver-app-backup.service': (
            '[Unit]\nDescription=NekoServer media application snapshot\n'
            f'RequiresMountsFor={data} {media}\nAfter=docker.service nekoserver-app-resume.service nekoserver-maintenance-resume.service\n'
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'
            f'ExecStart={binary} app-snapshot --config {guard}\n'
            f'ExecStopPost={resume}\nTimeoutStopSec={RESUME_TIMEOUT}\n'
        ),
        'nekoserver-app-backup.timer': nightly_timer(
            'NekoServer nightly media application snapshot', time, host_timezone(config), persistent=True
        ),
        'nekoserver-app-resume.service': (
            '[Unit]\nDescription=NekoServer restart applications after interrupted snapshot\n'
            f'RequiresMountsFor={data}\nAfter=docker.service\nRequires=docker.service\n'
            f'[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec={RESUME_TIMEOUT}\n'
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
    if not media_services(config):
        # None of its apps on this server: this backup is theirs, so its schedule is retired
        # (the snapshots already on the HDD stay). The restart-after-an-interrupted-
        # snapshot service stays: with nothing interrupted it does nothing.
        from modules._schedules import retire

        retire([SYSTEMD_UNITS / f'nekoserver-app-backup.{suffix}' for suffix in ('timer', 'service')], subprocess.run)
        context.log('Nightly app backup: off, as none of its apps is set up; snapshots already made are kept')
        return
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
    context.log(
        'Nightly private app snapshots enabled on media HDD; apps stop briefly during each run. '
        + retention(keep_snapshots(context.config))
    )
