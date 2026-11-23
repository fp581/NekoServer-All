"""Read-only live Nextcloud acceptance checks; never read administrator secrets."""

import json
from pathlib import Path

import status_checks as health
from neko_common import app_port, cloud_files, local_opener

DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']
SERVICES = {'nextcloud', 'database', 'redis', 'cron'}


def check(config):
    rows = []

    def record(key, ok, detail):
        rows.append(
            {'id': 'cloud:' + key, 'name': 'Nextcloud ' + key, 'status': 'pass' if ok else 'fail', 'detail': detail}
        )

    inspected = inspected_containers()
    found = set()
    good = len(inspected) == len(SERVICES)
    for row in inspected:
        service, ok = container_as_set_up(config, row)
        found.add(service)
        good = good and ok
    record(
        'containers',
        good and found == SERVICES,
        'All four managed containers must run with expected persistent bind mounts and localhost-only publication.',
    )
    record(
        'status',
        live_status_ok(),
        'Installed Nextcloud must answer its live status endpoint with no pending migration or maintenance mode.',
    )
    return rows


def inspected_containers():
    """Docker's view of Nextcloud's four containers ([] unless there are exactly four)."""
    code, text = health.run(
        [*DOCKER, 'ps', '-aq', '--filter', 'label=com.docker.compose.project=neko-nextcloud'],
        15,
    )
    ids = text.split()
    if code != 0 or len(ids) != len(SERVICES):
        return []
    code, text = health.run([*DOCKER, 'inspect', *ids], 15)
    try:
        return json.loads(text) if code == 0 else []
    except ValueError:
        return []


def container_as_set_up(config, row):
    """(its service, whether it runs with only Nextcloud's own port on 127.0.0.1 and the
    folders it should have) for one inspected container."""
    service = row.get('Config', {}).get('Labels', {}).get('com.docker.compose.service')
    binds = row.get('HostConfig', {}).get('PortBindings') or {}
    if service == 'nextcloud':
        published = binds.get('80/tcp') == [{'HostIp': '127.0.0.1', 'HostPort': '8088'}]
    else:
        published = not any(binds.values())
    mounts = {m.get('Destination'): m.get('Source') for m in row.get('Mounts', []) if m.get('Type') == 'bind'}
    folders = all(mounts.get(dest) == source for dest, source in wanted_folders(config, service).items())
    return service, row.get('State', {}).get('Running') is True and published and folders


def wanted_folders(config, service):
    """{folder in the container: folder on this PC} a service of Nextcloud must have."""
    if service in ('nextcloud', 'cron'):
        return {
            '/var/www/html': str(Path(config['data_mount']) / 'appdata/nextcloud/html'),
            '/var/www/html/data': str(cloud_files(config)),
        }
    if service == 'database':
        return {'/var/lib/postgresql/data': str(Path(config['data_mount']) / 'appdata/nextcloud/postgres')}
    return {}


def live_status_ok():
    """Nextcloud answers its status page: installed, not in maintenance, no upgrade waiting."""
    try:
        opener = local_opener()
        with opener.open(f'http://127.0.0.1:{app_port("nextcloud")}/status.php', timeout=10) as response:
            status = json.loads(response.read(64000))
        return (
            status.get('installed') is True
            and status.get('maintenance') is False
            and status.get('needsDbUpgrade') is False
        )
    except (OSError, ValueError, AttributeError):
        return False
