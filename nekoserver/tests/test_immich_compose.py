"""The downloaded Immich release's Compose file is checked before setup starts it, with
the same rules as the monthly update (maintenance.rs valid_model): an altered file gets
no more of this PC than NekoServer gives Immich."""

import copy
import json
import os
import subprocess
import tempfile
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


CONFIG = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
BINARY = ROOT / 'runtime/bin/nekoserver-runtime'


# A stand-in for Docker: the volumes that exist now (NEKO_TEST_VOLUMES, as `docker volume
# inspect` shows them), so the check of existing volumes runs without a Docker daemon.
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
volumes = json.loads(os.environ.get('NEKO_TEST_VOLUMES', '[]'))
args = [a for a in sys.argv[1:] if not a.startswith('--host') and not a.startswith('unix:')]
if args[:2] == ['volume', 'ls']:
    print('\\n'.join(v['Name'] for v in volumes))
elif args[:2] == ['volume', 'inspect']:
    print(json.dumps([v for v in volumes if v['Name'] == args[2]]))
else:
    sys.exit(1)
"""


def runtime(project, config, args, volumes=()):
    """The runtime built from these sources (as CI builds it), asked directly."""
    with tempfile.TemporaryDirectory() as bin_dir:
        docker = Path(bin_dir) / 'docker'
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        env = {
            **os.environ,
            'PATH': f'{bin_dir}:{os.environ.get("PATH", "")}',
            'NEKO_TEST_VOLUMES': json.dumps(volumes),
        }
        done = subprocess.run([str(BINARY), *args], capture_output=True, text=True, timeout=60, check=False, env=env)
    return done.returncode, (done.stdout + done.stderr).strip()


def volume(**labels):
    """A volume as `docker volume inspect` shows it."""
    return {'Name': 'neko-immich_model-cache', 'Driver': 'local', 'Options': None, 'Labels': labels or None}


OURS = {'com.docker.compose.project': 'neko-immich', 'com.docker.compose.volume': 'model-cache'}


@unittest.skipUnless(BINARY.is_file(), 'the runtime is not built (python3 runtime/build.py)')
class ImmichComposeTests(unittest.TestCase):
    """0.46.1: the rules live once, in the runtime (check-compose); these run the real one."""

    def check(self, change, volumes=()):
        model = copy.deepcopy(MODEL)
        change(model)
        photos.checked_model(model, UPLOAD, DB, CONFIG, ask=lambda *a: runtime(*a, volumes=volumes))

    def refused(self, change, volumes=()):
        with self.assertRaisesRegex(RuntimeError, 'asks for more than NekoServer gives Immich'):
            self.check(change, volumes)

    def test_keys_that_give_more_access_are_refused(self):
        """The 0.46.0 review, P1: Docker's own socket, lifecycle hooks with every right, and
        secrets or configs read from this PC's files all passed both checks."""
        server = lambda **kw: lambda m: m['services']['immich-server'].update(**kw)  # noqa: E731
        for change in (
            server(use_api_socket=True),
            server(post_start=[{'command': 'sh -c id', 'user': 'root', 'privileged': True}]),
            server(pre_stop=[{'command': 'sh', 'privileged': True}]),
            server(secrets=[{'source': 'key', 'target': 'key'}]),
            lambda m: m.update(secrets={'key': {'file': '/root/.ssh/id_ed25519'}}),
            lambda m: m.update(configs={'shadow': {'file': '/etc/shadow'}}),
        ):
            self.refused(change)

    def test_only_a_volume_compose_made_for_immich_is_used(self):
        """The 0.46.0 review: any volume name passed, and Compose uses an existing volume of
        that name as it is (another app's files, or a bind of this PC's folders)."""
        self.check(lambda m: None)  # not there yet: Compose makes it
        self.check(lambda m: None, [volume(**OURS)])
        self.refused(lambda m: None, [volume()])  # made by hand
        bound = {**volume(**OURS), 'Options': {'type': 'none', 'o': 'bind', 'device': '/root'}}
        self.refused(lambda m: None, [bound])
        self.refused(lambda m: m['volumes']['model-cache'].update(name='someone-elses-files'))

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

    def test_the_snapshot_check_and_the_runtime_agree(self):
        """The 0.46.1 audit: the update took files the snapshot check refused (and the
        other way round), so a later setup run or snapshot failed."""
        import recovery_tools

        unconfined = copy.deepcopy(MODEL)
        unconfined['services']['redis']['security_opt'] = ['seccomp=unconfined']
        with self.assertRaises(RuntimeError):
            recovery_tools.validate_model(unconfined, UPLOAD, DB)
        self.refused(lambda m: m['services']['redis'].update(security_opt=['seccomp=unconfined']))
        # Immich running as another user, or its photos bound elsewhere: refused by both.
        self.refused(lambda m: m['services']['immich-server'].update(user='1000:1000'))
        self.refused(lambda m: m['services']['immich-server']['volumes'][0].update(target='/photos'))
        # A cache volume of its own for redis: taken by both.
        cache = lambda m: (  # noqa: E731
            m['services']['redis'].update(volumes=[{'type': 'volume', 'source': 'redis-data', 'target': '/data'}]),
            m['volumes'].update({'redis-data': {'name': 'neko-immich_redis-data'}}),
        )
        self.check(cache)

    def test_setup_checks_the_file_before_starting_it(self):
        source = (ROOT / 'modules/photos.py').read_text()
        self.assertNotIn('def chip_only', source)  # no second copy of the rules
        self.assertLess(
            source.index('checked_model(json.loads(shown.stdout), upload, db, cfg)'),
            source.index("compose_up(base, 'Immich', ctx.log, 1200)"),
        )


if __name__ == '__main__':
    unittest.main()
