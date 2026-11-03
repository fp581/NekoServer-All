"""The downloaded Immich release's Compose file is checked before setup starts it, with
the same rules as the monthly update (maintenance.rs valid_model): an altered file gets
no more of this PC than NekoServer gives Immich."""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import photos  # noqa: E402

UPLOAD, DB = Path('/srv/nekoserver/photos/immich-library'), Path('/srv/nekoserver/appdata/immich/postgres')
ENV = {'DB_PASSWORD': 'x', 'DB_USERNAME': 'postgres', 'DB_DATABASE_NAME': 'immich'}
# Immich's own file as `docker compose config --format json` shows it (trimmed).
MODEL = {
    'services': {
        'immich-server': {
            'image': 'ghcr.io/immich-app/immich-server:v2.1.0',
            'environment': dict(ENV),
            'volumes': [
                {'type': 'bind', 'source': str(UPLOAD), 'target': '/data'},
                {'type': 'bind', 'source': '/etc/localtime', 'target': '/etc/localtime', 'read_only': True},
            ],
            'ports': [{'mode': 'ingress', 'host_ip': '127.0.0.1', 'target': 2283, 'published': '2283'}],
        },
        'immich-machine-learning': {
            'image': 'ghcr.io/immich-app/immich-machine-learning:v2.1.0',
            'volumes': [{'type': 'volume', 'source': 'model-cache', 'target': '/cache'}],
        },
        'redis': {'image': 'docker.io/valkey/valkey:8'},
        'database': {
            'image': 'ghcr.io/immich-app/postgres:14',
            'environment': {'POSTGRES_PASSWORD': 'x', 'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'immich'},
            'volumes': [{'type': 'bind', 'source': str(DB), 'target': '/var/lib/postgresql/data'}],
        },
    },
    'volumes': {'model-cache': {'name': 'neko-immich_model-cache'}},
}


class ImmichComposeTests(unittest.TestCase):
    def check(self, change):
        model = copy.deepcopy(MODEL)
        change(model)
        photos.checked_model(model, UPLOAD, DB)

    def refused(self, change):
        with self.assertRaisesRegex(RuntimeError, 'asks for more than NekoServer gives Immich'):
            self.check(change)

    def test_immichs_own_file_and_the_graphics_chip_pass(self):
        self.check(lambda m: None)
        self.check(lambda m: m['services']['immich-server'].update(devices=['/dev/dri:/dev/dri']))
        self.check(
            lambda m: m['services']['immich-server'].update(
                devices=[{'source': '/dev/dri', 'target': '/dev/dri', 'permissions': 'rwm'}]
            )
        )
        self.check(lambda m: m['services']['redis'].update(security_opt=['no-new-privileges:true']))

    def test_a_volume_bound_to_this_pcs_files_is_refused(self):
        """0.45.8, the owner's review: a named volume defined as a bind of / passed."""
        for definition in (
            {'driver_opts': {'type': 'none', 'o': 'bind', 'device': '/'}},
            {'driver': 'local', 'driver_opts': {'device': '/etc', 'o': 'bind'}},
            {'driver': 'sshfs'},
            {'external': True, 'name': 'someone-elses'},
        ):
            self.refused(lambda m: m['volumes'].update({'model-cache': definition}))

    def test_devices_beyond_the_chip_and_wider_rights_are_refused(self):
        server = lambda **kw: lambda m: m['services']['immich-server'].update(**kw)  # noqa: E731
        for change in (
            server(devices=['/dev/sda:/dev/sda']),
            server(devices=['/dev/dri:/dev/mem']),
            server(devices=['/dev/kmsg']),
            server(device_cgroup_rules=['a *:* rwm']),
            server(cap_add=['SYS_ADMIN']),
            server(security_opt=['apparmor:unconfined']),
            server(runtime='nvidia'),
            server(gpus='all'),
            server(ports=[{'host_ip': '0.0.0.0', 'target': 2283, 'published': '2283'}]),
            server(privileged=True),
            lambda m: m['services']['redis'].update(devices=['/dev/dri:/dev/dri']),
            lambda m: m['services']['immich-server']['volumes'].append(
                {'type': 'bind', 'source': '/', 'target': '/host'}
            ),
        ):
            self.refused(change)

    def test_setup_checks_the_file_before_starting_it(self):
        source = (ROOT / 'modules/photos.py').read_text()
        self.assertLess(
            source.index('checked_model(json.loads(shown.stdout), upload, db)'),
            source.index("compose_up(base, 'Immich', ctx.log, 1200)"),
        )


if __name__ == '__main__':
    unittest.main()
