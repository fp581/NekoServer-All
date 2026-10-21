"""Read-only live Nextcloud acceptance checks; never read administrator secrets."""
import json
from pathlib import Path

import status_checks as health
from neko_common import app_port, local_opener


def check(config):
    rows = []
    def record(key, ok, detail):
        rows.append({'id': 'cloud:'+key, 'name': 'Nextcloud '+key, 'status': 'pass' if ok else 'fail', 'detail': detail})
    code, text = health.run(['docker', '--host', 'unix:///var/run/docker.sock', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=neko-nextcloud'], 15)
    ids = text.split()
    inspected = []
    if code == 0 and len(ids) == 4:
        code, text = health.run(['docker', '--host', 'unix:///var/run/docker.sock', 'inspect', *ids], 15)
        try:
            inspected = json.loads(text) if code == 0 else []
        except ValueError:
            pass
    expected = {'nextcloud', 'database', 'redis', 'cron'}
    found = set()
    good = len(inspected) == 4
    for row in inspected:
        service = row.get('Config', {}).get('Labels', {}).get('com.docker.compose.service')
        found.add(service)
        good = good and row.get('State', {}).get('Running') is True
        binds = row.get('HostConfig', {}).get('PortBindings') or {}
        if service == 'nextcloud':
            good = good and binds.get('80/tcp') == [{'HostIp': '127.0.0.1', 'HostPort': '8088'}]
        else:
            good = good and not any(binds.values())
        wanted = {'/var/www/html': str(Path(config['data_mount'])/'appdata/nextcloud/html'), '/var/www/html/data': str(Path(config['media_mount'])/'cloud/files')} if service in ('nextcloud', 'cron') else {'/var/lib/postgresql/data': str(Path(config['data_mount'])/'appdata/nextcloud/postgres')} if service == 'database' else {}
        mounts = {m.get('Destination'): m.get('Source') for m in row.get('Mounts', []) if m.get('Type') == 'bind'}
        good = good and all(mounts.get(dest) == source for dest, source in wanted.items())
    record('containers', good and found == expected, 'All four managed containers must run with expected persistent bind mounts and localhost-only publication.')
    try:
        opener = local_opener()
        with opener.open(f'http://127.0.0.1:{app_port("nextcloud")}/status.php', timeout=10) as response:
            status = json.loads(response.read(64000))
        ok = status.get('installed') is True and status.get('maintenance') is False and status.get('needsDbUpgrade') is False
    except (OSError, ValueError, AttributeError):
        ok = False
    record('status', ok, 'Installed Nextcloud must answer its live status endpoint with no pending migration or maintenance mode.')
    return rows
