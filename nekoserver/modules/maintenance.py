"""Install the native monthly backup/rehearsal/update/rollback transaction."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import native_runtime
from neko_common import (
    VERSION,
    atomic_json,
    refresh_settings,
    safe_path,
    verify_storage,
    host_timezone,
    check_units,
    write_units,
)
from neko_common import MAINTENANCE_JOURNAL, maintenance_running

# No photo_backup: since v0.21.1 the monthly snapshot stays on the HDD, so a missing
# or unconfigured USB drive must not block monthly updates.
DEPENDENCIES = ['docker', 'recovery', 'photos', 'nextcloud']
DESCRIPTION = (
    'Daily check for app updates, weekly protected updates (each app on its own, after a snapshot of it), '
    'and a monthly check of every backup copy with a restore test'
)
RESUME_SERVICE = 'nekoserver-maintenance-resume.service'
RETRY_SERVICE = 'nekoserver-maintenance-retry.service'
RETRY_TIMER = 'nekoserver-maintenance-retry.timer'
UPDATES_TIMER = 'nekoserver-updates.timer'
CHECK_TIMER = 'nekoserver-update-check.timer'
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def options(config):
    opts = config.get('maintenance', {})
    if not isinstance(opts, dict):
        raise RuntimeError('maintenance must be an object')
    time = opts.get('time', '02:30')
    if not isinstance(time, str) or not re.fullmatch(r'0[0-5]:[0-5][0-9]', time):
        raise RuntimeError('Maintenance must start at night, HH:MM between 00:00 and 05:59')
    zone = host_timezone(config)
    if not (Path('/usr/share/zoneinfo') / zone).is_file():
        raise RuntimeError('Use an installed IANA maintenance timezone')
    for key, default, low, high in (
        ('minimum_free_gb', 20, 2, 100000),
        ('health_wait_seconds', 300, 30, 1800),
        ('night_end_hour', 6, 1, 7),
        ('keep_snapshots', 3, 0, 120),
        # How many days a new app version waits before the weekly update installs it.
        ('settle_days', 7, 1, 30),
    ):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid maintenance.' + key)
    # Major versions are installed only when you approve them (see MONTHLY-MAINTENANCE.md).
    for key, pattern in (('immich_major_update', r'(v\d+(\.\d+\.\d+)?)?'), ('nextcloud_major_update', r'(\d{1,3})?')):
        value = opts.get(key, '')
        if not isinstance(value, str) or not re.fullmatch(pattern, value.strip()):
            raise RuntimeError(
                'Invalid maintenance.' + key + ': leave it empty, or use the version the dashboard shows'
            )
    if opts.get('update_day', 'Wed') not in DAYS:
        raise RuntimeError('Invalid maintenance.update_day: use Mon, Tue, Wed, Thu, Fri, Sat or Sun')
    if int(time[:2]) >= opts.get('night_end_hour', 6):
        raise RuntimeError('Maintenance starts after its allowed night window')
    return opts


def definitions(config, binary, guard):
    opts = options(config)
    start = opts.get('time', '02:30')
    zone = host_timezone(config)
    command = f'{binary} maintenance-resume --config {guard}'
    shared = f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\nAfter=docker.service\nRequires=docker.service\n'
    # A rollback is never cut off part-way (that would leave the apps stopped): it may
    # take as long as it needs. Each step inside it has its own time limit.
    return {
        'nekoserver-maintenance.service': '[Unit]\nDescription=NekoServer monthly check of every backup copy, snapshot and restore test\n'
        + shared
        + '[Service]\nType=oneshot\nUMask=0077\nEnvironment=TZ='
        + zone
        + '\nTimeoutStartSec=18h\nTimeoutStopSec=infinity\n'
        + f'ExecStart={binary} maintenance --config {guard}\nExecStopPost={command}\n',
        # No daytime catch-up after a powered-off night. Native worker also checks the window.
        'nekoserver-maintenance.timer': '[Unit]\nDescription=NekoServer monthly night maintenance\n[Timer]\n'
        + f'OnCalendar=*-*-01 {start}:00 {zone}\nPersistent=false\nRandomizedDelaySec=0\n[Install]\nWantedBy=timers.target\n',
        'nekoserver-maintenance-resume.service': '[Unit]\nDescription=NekoServer recover interrupted monthly maintenance\n'
        + shared
        + '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=infinity\n'
        + f'ExecStart={command}\n[Install]\nWantedBy=multi-user.target\n',
        # An update whose finishing failed (Immich not ready in time, say) keeps its journal,
        # and the nightly app backups wait for it: tried again every hour, not only at the
        # next restart. It runs only while a journal is there, and never waits for the lock
        # (NEKOSERVER_MAINTENANCE_RETRY): with another job at work it tries again next hour.
        RETRY_SERVICE: '[Unit]\nDescription=NekoServer retry an unfinished monthly update\n'
        + shared
        + f'ConditionPathExists={MAINTENANCE_JOURNAL}\n'
        '[Service]\nType=oneshot\nUMask=0077\nEnvironment=NEKOSERVER_MAINTENANCE_RETRY=1\nTimeoutStartSec=infinity\n'
        + f'ExecStart={command}\n',
        RETRY_TIMER: '[Unit]\nDescription=NekoServer hourly retry of an unfinished monthly update\n[Timer]\n'
        f'OnCalendar=*-*-* *:17:00 {zone}\nPersistent=false\n[Install]\nWantedBy=timers.target\n',
        # The weekly protected app updates: each app with a version that has settled is
        # updated on its own, after a snapshot of it, on the update night at the monthly
        # time (never on the 1st: that night is the monthly check's). The same journal and
        # resume service as the monthly run finish or undo one that was cut off.
        'nekoserver-updates.service': '[Unit]\nDescription=NekoServer weekly protected app updates\n'
        + shared
        + '[Service]\nType=oneshot\nUMask=0077\nEnvironment=TZ='
        + zone
        + '\nTimeoutStartSec=18h\nTimeoutStopSec=infinity\n'
        + f'ExecStart={binary} update-apps --config {guard}\nExecStopPost={command}\n',
        # "Update now" on the dashboard (the control service starts it, for one app):
        # the same steps, at any time of day; it goes on if the page is closed.
        'nekoserver-update-now@.service': '[Unit]\nDescription=NekoServer update %i now\n'
        + shared
        + '[Service]\nType=oneshot\nUMask=0077\nEnvironment=TZ='
        + zone
        + '\nTimeoutStartSec=6h\nTimeoutStopSec=infinity\n'
        + f'ExecStart={binary} update-apps --config {guard} --app %i\nExecStopPost={command}\n',
        UPDATES_TIMER: '[Unit]\nDescription=NekoServer weekly app update night\n[Timer]\n'
        + f'OnCalendar={opts.get("update_day", "Wed")} *-*-02..31 {start}:00 {zone}\nPersistent=false\nRandomizedDelaySec=0\n'
        '[Install]\nWantedBy=timers.target\n',
        # The daily look for new versions, while the apps run: it downloads them and notes
        # the day each was first seen; it stops and changes nothing.
        'nekoserver-update-check.service': '[Unit]\nDescription=NekoServer daily check for app updates\n'
        + shared
        + '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=4h\n'
        + f'ExecStart={binary} update-check --config {guard}\n',
        CHECK_TIMER: '[Unit]\nDescription=NekoServer daily check for app updates\n[Timer]\n'
        f'OnCalendar=*-*-* 13:41:00 {zone}\nPersistent=true\n[Install]\nWantedBy=timers.target\n',
    }


GUARD = Path('/etc/nekoserver-backups/maintenance.json')


def refresh(config, log, target=None):
    """After every setup run: the installed monthly update works on the apps set up now
    (an app added later is backed up and updated too; one switched off is left alone)."""
    try:
        options(config)
    except RuntimeError as exc:
        log(f'Monthly update: settings not refreshed ({exc})')
        return False
    return refresh_settings(target or GUARD, config, log, 'Monthly update')


def run(ctx):
    options(ctx.config)
    if not ctx.apply:
        ctx.log(
            'Monthly '
            + options(ctx.config).get('time', '02:30')
            + ' backup, isolated restore rehearsal, container updates and rollback; OS packages excluded'
        )
        return
    if os.geteuid() != 0:
        raise RuntimeError('Maintenance requires root')
    config = ctx.config
    verify_storage(config)
    binary = native_runtime.deploy(
        Path(__file__).resolve().parents[1], config, Path(config['data_mount']) / 'installer/runtime' / ('v' + VERSION)
    )
    guard = safe_path(GUARD)
    # Installed native worker has no dependency on the original working directory.
    units = definitions(config, binary, guard)
    check_units(units, 'maintenance')
    import backup_secrets

    if not backup_secrets.configured():
        if sys.stdin.isatty():
            # No key chosen (three mismatches, or Ctrl+D): the monthly update is still
            # installed; its snapshots keep the password files unencrypted until a key exists.
            try:
                backup_secrets.setup(out=ctx.log)
            except (RuntimeError, EOFError) as exc:
                ctx.log(
                    f'Backup key not set up ({exc or "stopped at the prompt"}): run  sudo python3 backup_secrets.py --setup  '
                    'later; until then monthly snapshots keep the app password files UNENCRYPTED inside data.tar (readable by root only)'
                )
        else:
            ctx.log(
                'Backup key not set up: until you run  sudo python3 backup_secrets.py --setup  in a terminal, monthly snapshots keep the app password files '
                'UNENCRYPTED inside data.tar on the HDD (readable by root only), so a restore still has them'
            )
    atomic_json(guard, config)
    write_units(units)
    for args in (
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'enable', '--now', '--no-block', RESUME_SERVICE],
        ['systemctl', 'enable', '--now', 'nekoserver-maintenance.timer'],
        ['systemctl', 'enable', '--now', RETRY_TIMER],
        ['systemctl', 'enable', '--now', UPDATES_TIMER],
        ['systemctl', 'enable', '--now', CHECK_TIMER],
    ):
        subprocess.run(args, check=True, capture_output=True, timeout=1200)
    opts = options(ctx.config)
    ctx.log(
        'App updates enabled: a daily check for new versions; on '
        + opts.get('update_day', 'Wed')
        + ' nights each app whose new version has waited '
        + str(opts.get('settle_days', 7))
        + ' days is updated on its own, after a snapshot of it; on the 1st, a check of every backup copy, a snapshot '
        'of every app and a restore test. The newest '
        + str(opts.get('keep_snapshots', 3) or 'all')
        + ' complete monthly snapshots are kept.'
    )


FINISHING = (RESUME_SERVICE, RETRY_SERVICE, RETRY_TIMER)


def finish_update(log=print, run=subprocess.run, guard=None, journal=None, deploy=None, running=None):
    """`sudo bash install.sh --finish-update`: an unfinished monthly update (its journal is
    there) finished by THIS release. Setup refuses to run while one is unfinished, so a
    fix to the way back in a newer release could otherwise never reach a server whose
    older release keeps failing to finish it. Only this release's runtime is put in place
    and the services that finish the update are pointed at it; nothing else is set up,
    changed or updated. Returns True when the update was finished (or there was none)."""
    journal = Path(journal or MAINTENANCE_JOURNAL)
    if not journal.exists():
        log('No monthly update is waiting to be finished. Run  sudo bash install.sh  as usual.')
        return True
    if (running or maintenance_running)():
        raise RuntimeError(
            'The monthly update (or the service that finishes it) is running right now. '
            'Let it finish: sudo systemctl status nekoserver-maintenance-resume.service'
        )
    guard = safe_path(Path(guard or GUARD))
    config = json.loads(guard.read_text())
    options(config)
    verify_storage(config)
    binary = (deploy or native_runtime.deploy)(
        Path(__file__).resolve().parents[1], config, Path(config['data_mount']) / 'installer/runtime' / ('v' + VERSION)
    )
    units = {name: text for name, text in definitions(config, binary, guard).items() if name in FINISHING}
    check_units(units, 'maintenance')
    write_units(units)
    for args in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', RETRY_TIMER]):
        run(args, check=True, capture_output=True, timeout=120)
    log(
        f'Finishing the unfinished monthly update with NekoServer v{VERSION} (this can take a while: '
        'a way back unpacks the snapshot)...'
    )
    # Run as its service, so a closed terminal or Ctrl+C never stops a way back half-way.
    done = run(['systemctl', 'start', RESUME_SERVICE], check=False).returncode == 0 and not journal.exists()
    if not done:
        raise RuntimeError(
            'The monthly update could not be finished. What happened is in  '
            'sudo journalctl -u nekoserver-maintenance-resume.service  (it is tried again every hour)'
        )
    log('The monthly update is finished. Now run  sudo bash install.sh  as usual.')
    return True
