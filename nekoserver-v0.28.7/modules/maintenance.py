"""Install the native monthly backup/rehearsal/update/rollback transaction."""
import os
from pathlib import Path
import re
import subprocess
import sys

import native_runtime
from neko_common import VERSION, atomic_json, refresh_settings, safe_path, verify_storage, host_timezone, check_units, write_units

# No photo_backup: since v0.21.1 the monthly snapshot stays on the HDD, so a missing
# or unconfigured USB drive must not block monthly updates.
DEPENDENCIES = ['docker', 'recovery', 'photos', 'nextcloud']
DESCRIPTION = 'Monthly overnight app-state snapshot on the HDD, isolated restore tests and container updates'


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
    for key, default, low, high in (('minimum_free_gb', 20, 2, 100000), ('health_wait_seconds', 300, 30, 1800), ('night_end_hour', 6, 1, 7), ('keep_snapshots', 3, 0, 120)):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid maintenance.'+key)
    # Major versions are installed only when you approve them (see MONTHLY-MAINTENANCE.md).
    for key, pattern in (('immich_major_update', r'(v\d+(\.\d+\.\d+)?)?'), ('nextcloud_major_update', r'(\d{1,3})?')):
        value = opts.get(key, '')
        if not isinstance(value, str) or not re.fullmatch(pattern, value.strip()):
            raise RuntimeError('Invalid maintenance.'+key+': leave it empty, or use the version the dashboard shows')
    if int(time[:2]) >= opts.get('night_end_hour', 6):
        raise RuntimeError('Maintenance starts after its allowed night window')
    # require_external is obsolete since v0.21.1: monthly snapshots stay on the HDD and
    # the USB drive only receives the weekly add-only photo/Nextcloud copies.
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
        'nekoserver-maintenance.service': '[Unit]\nDescription=NekoServer monthly verified backup and container update\n'+shared+
        '[Service]\nType=oneshot\nUMask=0077\nEnvironment=TZ='+zone+'\nTimeoutStartSec=18h\nTimeoutStopSec=infinity\n'+
        f'ExecStart={binary} maintenance --config {guard}\nExecStopPost={command}\n',
        # No daytime catch-up after a powered-off night. Native worker also checks the window.
        'nekoserver-maintenance.timer': '[Unit]\nDescription=NekoServer monthly night maintenance\n[Timer]\n'+
        f'OnCalendar=*-*-01 {start}:00 {zone}\nPersistent=false\nRandomizedDelaySec=0\n[Install]\nWantedBy=timers.target\n',
        'nekoserver-maintenance-resume.service': '[Unit]\nDescription=NekoServer recover interrupted monthly maintenance\n'+shared+
        '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=infinity\n'+f'ExecStart={command}\n[Install]\nWantedBy=multi-user.target\n',
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
        ctx.log('Monthly 02:30 backup, isolated restore rehearsal, container updates and rollback; OS packages excluded')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Maintenance requires root')
    config = ctx.config
    verify_storage(config)
    binary = native_runtime.deploy(Path(__file__).resolve().parents[1], config, Path(config['data_mount'])/'installer/runtime'/('v'+VERSION))
    guard = safe_path(GUARD)
    # Installed native worker has no dependency on the original working directory.
    units=definitions(config,binary,guard)
    check_units(units, 'maintenance')
    import backup_secrets
    if not backup_secrets.configured():
        if sys.stdin.isatty():
            backup_secrets.setup(out=ctx.log)
        else:
            ctx.log('Backup key not set up: until you run  sudo python3 backup_secrets.py --setup  in a terminal, monthly snapshots keep the app password files '
                    'UNENCRYPTED inside data.tar on the HDD (readable by root only), so a restore still has them')
    atomic_json(guard,config)
    write_units(units)
    for args in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'nekoserver-maintenance-resume.service'], ['systemctl', 'enable', '--now', 'nekoserver-maintenance.timer']):
        subprocess.run(args, check=True, capture_output=True, timeout=1200)
    ctx.log('Monthly overnight maintenance enabled: app-state snapshot on the HDD, then updates. The newest '+str(options(ctx.config).get('keep_snapshots',3) or 'all')+' complete snapshots are kept.')
