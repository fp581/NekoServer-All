"""Small, bounded, read-only checks of this server (commands, saved reports, drives,
apps, backup schedules) used by setup, the gateway and the acceptance checker.
The dashboard itself is read by the Rust collector (runtime/src/collect.rs)."""
import json
import math
import os
import re
import subprocess
from pathlib import Path

from neko_common import APPS, findmnt, safe_path

JOBS = {'music': ('Music library backup', 'nekoserver-music-backup', 172800),
        'archive': ('Photo archive', 'nekoserver-photo-preservation', 10800),
        'database': ('Immich database', 'nekoserver-immich-db-backup', 172800),
        'external': ('USB copy (photos and files)', 'nekoserver-photo-backup', 864000),
        'applications': ('Media application snapshot', 'nekoserver-app-backup', 172800),
        'host': ('Host and user snapshot', 'nekoserver-host-backup', 172800),
        'maintenance': ('Monthly backup and updates', 'nekoserver-maintenance', 3024000)}
# Only these programs are run, with plain arguments (no shell, no control characters).
PROGRAMS = {'systemctl', 'docker', 'findmnt', 'tailscale'}
PLAIN_ARGUMENT = re.compile(r'[A-Za-z0-9 _@%:=,./+{}-]{0,512}')
PLAIN_PATH = re.compile(r'/[A-Za-z0-9_.@/-]{1,4095}')
CONTAINER_STATES = ('running', 'exited', 'restarting', 'paused', 'created', 'dead')
HEALTH_MARKERS = (('(unhealthy)', 'unhealthy'), ('(health: starting)', 'starting'), ('(healthy)', 'healthy'))


def run(args, timeout=8):
    """(exit code, output) of a read-only command; (None, '') when it can't run."""
    if not args or args[0] not in PROGRAMS:
        return None, ''
    plain = [PLAIN_ARGUMENT.fullmatch(str(a)) for a in args]
    if not all(plain):
        return None, ''
    try:
        p = subprocess.run([m.group(0) for m in plain], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'}, check=False)
        return p.returncode, p.stdout[:2000000]
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def read_json(path, limit=2000000):
    """A saved report or setting as a dict ({} when absent, too big or not readable).
    Only plain absolute paths are read."""
    plain = PLAIN_PATH.fullmatch(str(path))
    if not plain or '..' in Path(plain.group(0)).parts:
        return {}
    try:
        checked = safe_path(plain.group(0))
        if checked.stat().st_size > limit:
            return {}
        value = json.loads(checked.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, RuntimeError, ValueError):
        return {}


def number(value, lo=0, hi=1e18):
    return value if type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi else None


def mount_info(path, expected=None, fstype=None):
    if expected is not None and (not isinstance(expected, str) or not expected):
        return {'status': 'not_configured'}
    fields = (findmnt('UUID,FSTYPE,SOURCE', path) or '').split()
    if len(fields) < 3:
        return {'status': 'missing'}
    if expected and (fields[0] != expected or fields[1] != fstype):
        return {'status': 'wrong_drive'}
    return {'status': 'mounted', 'source': fields[2].split('[')[0]}


def container_records():
    """(exit code, [(labels, state, status)]) for every container."""
    code, text = run(['docker', '--host', 'unix:///var/run/docker.sock', 'ps', '-a', '--format', '{{json .}}'], 15)
    records = []
    for line in text.splitlines() if code == 0 else []:
        try:
            row = json.loads(line)
            labels = {k: v for k, _, v in (pair.partition('=') for pair in row.get('Labels', '').split(',')) if _}
            records.append((labels, row.get('State'), row.get('Status', '')))
        except (ValueError, TypeError, AttributeError):
            continue
    return code, records


def health_of(status):
    return next((name for marker, name in HEALTH_MARKERS if marker in status), 'not_defined')


def app_state(app, code, records):
    project, service = 'neko-' + app, 'immich-server' if app == 'immich' else app
    found = [(state, status) for labels, state, status in records
             if labels.get('com.docker.compose.project') == project and labels.get('com.docker.compose.service') == service]
    if len(found) > 1:
        return 'ambiguous', 'unknown'
    if len(found) == 1:
        state, status = found[0]
        return (state if state in CONTAINER_STATES else 'unknown'), health_of(status)
    return ('unknown' if code != 0 else 'missing'), 'unknown'


def applications(config):
    code, records = container_records()
    selected = set(config.get('media', {}).get('services', []))
    if config.get('photos', {}).get('deploy_immich'):
        selected.add('immich')
    if config.get('nextcloud', {}).get('enabled'):
        selected.add('nextcloud')
    out = []
    for app in (a for a in APPS if a in selected):
        state, health = app_state(app, code, records)
        name, description, port = APPS[app]
        out.append({'id': app, 'name': name, 'description': description, 'state': state, 'health': health,
                    'url': f'http://127.0.0.1:{port}/'})
    return out


def timer_and_known(unit):
    """(the timer as shown, whether systemd could be asked about it)."""
    code, text = run(['systemctl', 'show', unit + '.timer', '--property=LoadState,ActiveState,NextElapseUSecRealtime'])
    fields = {k: v for k, _, v in (line.partition('=') for line in text.splitlines()) if _}
    return ({'installed': code == 0 and fields.get('LoadState') == 'loaded', 'active': fields.get('ActiveState') == 'active',
             'next_run': fields.get('NextElapseUSecRealtime', '')[:80] or None}, code == 0 and 'LoadState' in fields)


def timer_status(unit):
    return timer_and_known(unit)[0]


def retired(config, key):
    """Why a job is intentionally not part of this server now (its app or backup switched
    off), or None when its schedule is expected (collect.rs retired says the same)."""
    immich = config.get('photos', {}).get('deploy_immich') is True
    if key == 'music' and config.get('music', {}).get('backup_enabled', True) is False:
        return 'Switched off: the music backup is turned off in the settings'
    if key in ('archive', 'database') and not immich:
        return 'Switched off: Immich is not set up'
    if key == 'external' and not immich and config.get('nextcloud', {}).get('enabled') is not True:
        return 'Switched off: neither Immich nor Nextcloud is set up'
    return None


def restore_tests(config, key):
    """The witnessed restore tests (acceptance.py manual tests) that show this backup can
    be restored: one per kind of data it holds (collect.rs restore_tests says the same)."""
    immich = config.get('photos', {}).get('deploy_immich') is True
    cloud = config.get('nextcloud', {}).get('enabled') is True
    if key == 'external':
        return [test for on, test in ((immich, 'independent_restore'), (cloud, 'cloud_restore')) if on]
    return {'archive': ['independent_restore'], 'database': ['independent_restore'], 'music': ['music_restore'],
            'applications': ['app_restore'], 'host': ['host_restore']}.get(key, [])


def schedule_stopped(timer, known, ever_ran):
    """A schedule that stopped while its job is still wanted: switched off, or gone after
    it had run. None while it is in place (or systemd could not be asked)."""
    if not known:
        return None
    if timer['installed'] and not timer['active']:
        return 'Run setup again to switch its schedule back on.'
    if not timer['installed'] and ever_ran:
        return 'Its schedule is gone, so it no longer runs: run setup again to put it back.'
    return None


def job_plan(config, key, unit, maximum):
    """(report name, unit, longest normal gap) for a job, following the photo schedule."""
    photos = config.get('photos', {})
    weekly = photos.get('backup_schedule') == 'weekly'
    if weekly and key in ('archive', 'database'):
        return 'database', 'nekoserver-immich-db-backup', 864000
    interval = photos.get('archive_interval_minutes', 60)
    if key == 'archive' and not weekly and type(interval) is int and 15 <= interval <= 10080:
        maximum = max(maximum, interval * 120)
    return key, unit, maximum


def job_reason(status, state, success):
    if status == 'fail':
        return 'Last run failed; inspect its local service logs'
    if success is None:
        return 'No recorded successful run'
    if state == 'stale':
        return 'Last success is overdue'
    return None


def job_row(config, now, data_ok, key, name, unit, maximum):
    report, unit, maximum = job_plan(config, key, unit, maximum)
    saved = read_json(Path(config['data_mount']) / 'reports/jobs' / f'{report}.json', 64000) if data_ok else {}
    status = saved.get('status') if saved.get('status') in ('running', 'pass', 'fail') else 'unknown'
    success = number(saved.get('last_success'), 0, now + 60)
    started = number(saved.get('started_at'), 0, now + 60)
    # Running too long, or not succeeding for too long: overdue.
    since = started if status == 'running' else success
    state = 'stale' if since is not None and now - since > maximum else status
    timer, known = timer_and_known(unit)
    row = {'id': key, 'name': name, 'status': state, 'last_success': success, 'last_attempt': started, 'timer': timer,
           'restore': restore_tests(config, key)}
    why = retired(config, key)
    if why:
        return {**row, 'status': 'disabled', 'reason': why, 'restore': []}
    stopped = None if state in ('fail', 'running') else schedule_stopped(timer, known, success is not None)
    if stopped:
        return {**row, 'status': 'stale', 'reason': stopped}
    return {**row, 'reason': job_reason(status, state, success)}


def backups(config, now, data_ok):
    return [job_row(config, now, data_ok, key, name, unit, maximum) for key, (name, unit, maximum) in JOBS.items()]


def heartbeat_status(config, now):
    saved = read_json('/run/nekoserver-heartbeat/status.json', 64000)
    timer = timer_status('nekoserver-heartbeat')
    stamp = number(saved.get('last_attempt'), 0, now + 30)
    valid = saved.get('format') == 'neko-heartbeat-v1' and saved.get('status') in ('pass', 'fail')
    state = saved.get('status') if valid else 'unknown'
    interval = config.get('heartbeat', {}).get('interval_seconds', 300)
    if type(interval) is not int or not 60 <= interval <= 3600:
        interval = 300
    # A timer switched off by choice ("wanted": false) is not a missed check-in.
    if timer['active'] and (stamp is None or now - stamp > interval + 90):
        state = 'stale'
    return {'status': state, 'timer': timer, 'last_attempt': stamp,
            'last_success': number(saved.get('last_success'), 0, now + 30), 'provider_notifications_verified': False}


def notification_status(now):
    saved = read_json('/run/nekoserver-notifications/delivery.json', 64000)
    timer = timer_status('nekoserver-notifications')
    stamp = number(saved.get('generated_at'), 0, now + 30)
    valid = saved.get('format') == 'neko-delivery-v1' and saved.get('status') in ('pass', 'fail', 'unknown')
    state = saved.get('status') if valid else 'unknown'
    if timer['active'] and (stamp is None or now - stamp > 180):
        state = 'stale'
    return {'status': state, 'timer': timer, 'last_success': number(saved.get('last_success'), 0, now + 30),
            'last_attempt': number(saved.get('last_attempt'), 0, now + 30), 'pending': number(saved.get('pending'), 0, 100)}
