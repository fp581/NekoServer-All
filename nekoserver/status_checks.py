"""Small, bounded, read-only checks of this server (commands, saved reports, drives,
apps) used by setup, the gateway and the acceptance checker. The state of the backups,
the outage monitor and the phone alerts is the runtime's to say (collect.rs, asked with
`nekoserver-runtime job-states`); this kept a copy of those rules until 0.47.2."""

import json
import math
import os
import re
import subprocess
from pathlib import Path

from neko_common import APPS, findmnt, safe_path

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
        p = subprocess.run(
            [m.group(0) for m in plain],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'},
            check=False,
        )
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
    found = [
        (state, status)
        for labels, state, status in records
        if labels.get('com.docker.compose.project') == project and labels.get('com.docker.compose.service') == service
    ]
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
        out.append(
            {
                'id': app,
                'name': name,
                'description': description,
                'state': state,
                'health': health,
                'url': f'http://127.0.0.1:{port}/',
            }
        )
    return out
