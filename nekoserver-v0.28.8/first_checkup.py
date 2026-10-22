"""The first checkup and backup, run by the wizard once every setup step has finished.

Without it the first backup would wait for the first night (the photo copy to the USB
drive for the first Sunday), and nothing would have checked the finished server as a
whole. Each backup runs through its own scheduled service, exactly as it will at night;
only the backups whose schedules are switched on run. Then the read-only acceptance
inspection checks the apps, backups and drives, and saves its report for the dashboard.
"""
import json
import subprocess
from pathlib import Path

# (service, what it is, its job report in reports/jobs/), in the order they run. The
# health check comes first so a problem with an app is reported before its backup.
JOBS = (
    ('nekoserver-media-health', 'App health check', None),
    ('nekoserver-app-backup', 'App settings and databases (the apps pause for a few minutes)', 'applications'),
    ('nekoserver-host-backup', 'System and user settings', 'host'),
    ('nekoserver-music-backup', 'Music library', 'music'),
    ('nekoserver-immich-db-backup', 'Photos and the Immich database, to the HDD', 'database'),
    ('nekoserver-photo-backup', 'Photos and files, to the USB drive', 'external'),
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


def outcome(config, report):
    """How a backup that finished ended, from its own report: 'pass', 'partial' (it left
    something out, which still ends without an error) or 'fail', and why."""
    if report is None:
        return 'pass', ''
    try:
        row = json.loads((Path(config['data_mount']) / 'reports/jobs' / (report + '.json')).read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return 'fail', 'it left no report'
    status = row.get('status') if isinstance(row, dict) else None
    missing = row.get('missing') if isinstance(row, dict) else None
    why = (missing.get('source') or missing.get('reason') or '') if isinstance(missing, dict) else ''
    if status == 'pass':
        return 'pass', ''
    if status == 'partial':
        return 'partial', why or 'part of it was left out'
    return 'fail', str(row.get('reason', '')) if isinstance(row, dict) else ''


def inspect(config):
    """The acceptance checks that run by themselves: (passed, the names of the others)."""
    import acceptance
    report = acceptance.inspect(config, 'full')
    acceptance.save(config, report)
    checks = report.get('automatic', [])
    # Anything not passed needs a look, a check still waiting for setup included.
    problems = [c.get('name', c.get('id', '?')) + (' (still to set up)' if c.get('status') == 'needs_setup' else '')
                for c in checks if c.get('status') != 'pass']
    return sum(c.get('status') == 'pass' for c in checks), problems


def run(config, output=print, run=subprocess.run, checks=inspect):
    """Returns True when every backup and check passed."""
    output('\nFirst checkup and backup')
    output('Everything is set up, so the first backups run now instead of tonight, and the')
    output('whole server is checked once. This takes a few minutes to an hour, depending on')
    output('how much is on the drives. You can stop it with Ctrl+C; the backups then run at')
    output('their usual times.\n')
    failed = []
    for unit, label, report in JOBS:
        if not scheduled(unit, run):
            continue
        output(f'  {label} ...')
        status, why = outcome(config, report) if start(unit, run) else ('fail', '')
        if status == 'pass':
            output('    done')
            continue
        failed.append(unit)
        if status == 'partial':
            output(f'    done in part: {why}')
        else:
            output(f'    did not finish{": " + why if why else ""}. Its log:  journalctl -u {unit}.service -n 50')
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
