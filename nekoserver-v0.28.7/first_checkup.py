"""The first checkup and backup, run by the wizard once every setup step has finished.

Without it the first backup would wait for the first night (the photo copy to the USB
drive for the first Sunday), and nothing would have checked the finished server as a
whole. Each backup runs through its own scheduled service, exactly as it will at night;
only the backups whose schedules are switched on run. Then the read-only acceptance
inspection checks the apps, backups and drives, and saves its report for the dashboard.
"""
import subprocess

# (service, what it is), in the order they run. The health check comes first so a
# problem with an app is reported before its backup.
JOBS = (
    ('nekoserver-media-health', 'App health check'),
    ('nekoserver-app-backup', 'App settings and databases (the apps pause for a few minutes)'),
    ('nekoserver-host-backup', 'System and user settings'),
    ('nekoserver-music-backup', 'Music library'),
    ('nekoserver-immich-db-backup', 'Photos and the Immich database, to the HDD'),
    ('nekoserver-photo-backup', 'Photos and files, to the USB drive'),
)


def scheduled(unit, run=subprocess.run):
    """This backup's schedule is switched on (a feature you skipped has none)."""
    result = run(['systemctl', 'is-enabled', unit + '.timer'], capture_output=True, text=True,
                 timeout=30, check=False)
    return result.returncode == 0 and result.stdout.strip() == 'enabled'


def start(unit, run=subprocess.run):
    """Run the job now and wait for it: True when it finished without an error."""
    result = run(['systemctl', 'start', unit + '.service'], capture_output=True, text=True, check=False)
    return result.returncode == 0


def inspect(config):
    """The acceptance checks that run by themselves: (passed, the names of the others)."""
    import acceptance
    report = acceptance.inspect(config, 'full')
    acceptance.save(config, report)
    checks = report.get('automatic', [])
    problems = [c.get('name', c.get('id', '?')) for c in checks if c.get('status') not in ('pass', 'needs_setup')]
    return sum(c.get('status') == 'pass' for c in checks), problems


def run(config, output=print, run=subprocess.run, checks=inspect):
    """Returns True when every backup and check passed."""
    output('\nFirst checkup and backup')
    output('Everything is set up, so the first backups run now instead of tonight, and the')
    output('whole server is checked once. This takes a few minutes to an hour, depending on')
    output('how much is on the drives. You can stop it with Ctrl+C; the backups then run at')
    output('their usual times.\n')
    failed = []
    for unit, label in JOBS:
        if not scheduled(unit, run):
            continue
        output(f'  {label} ...')
        if start(unit, run):
            output('    done')
        else:
            failed.append(unit)
            output(f'    did not finish. Its log:  journalctl -u {unit}.service -n 50')
    try:
        passed, problems = checks(config)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        output(f'  Checkup could not finish ({exc}). Run  sudo python3 acceptance.py --save  later.')
        return False
    output(f'  Checkup: {passed} checks passed.')
    for name in problems:
        output(f'    Needs a look: {name}')
    if problems:
        output('  The checklist on the dashboard explains each one.')
    return not failed and not problems
