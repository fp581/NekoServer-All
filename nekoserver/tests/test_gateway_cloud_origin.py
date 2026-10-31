"""Nextcloud must keep working on every gateway address, in any setup order."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools  # noqa: E402


class FakeNextcloud:
    """Records occ config:system calls and answers docker inspect."""

    def __init__(self, system=None, gateways=('172.19.0.1',)):
        self.system = system if system is not None else {'trusted_domains': ['localhost', '127.0.0.1']}
        self.gateways = gateways
        self.calls = []

    def __call__(self, args, timeout=30, env=None):
        self.calls.append(args)
        if args[-3:-1] == ['ps', '-q']:
            return 'abc123\n'
        if 'inspect' in args:
            networks = {f'net{i}': {'Gateway': g} for i, g in enumerate(self.gateways)}
            return json.dumps([{'NetworkSettings': {'Networks': networks}}])
        occ = args[args.index('occ') + 1 :]
        action, key = occ[0], occ[1]
        if action == 'config:system:get':
            if key not in self.system:
                raise RuntimeError('missing key')
            return json.dumps(self.system[key])
        if action == 'config:system:delete':
            self.system.pop(key, None)
            return ''
        if action == 'config:system:set':
            value = occ[-1].split('=', 1)[1]
            if len(occ) == 4:  # indexed array entry
                array = self.system.setdefault(key, [])
                index = int(occ[2])
                array[index : index + 1] = [value]
            else:
                self.system[key] = value
            return ''
        raise AssertionError(args)


CONFIG = {
    'data_mount': '/srv/nekoserver',
    'gateway': {'services': ['nextcloud', 'jellyfin']},
    'nextcloud': {'enabled': True},
    'media': {'services': ['jellyfin']},
}


class CloudOriginTests(unittest.TestCase):
    def run_gateways(self, fake, *hosts):
        with (
            mock.patch.object(gateway_tools, 'command', fake),
            mock.patch('neko_common.compose_image_override', return_value=[]),
        ):
            for host in hosts:
                gateway_tools.configure_cloud_origin(CONFIG, host, 8447)

    def test_lan_then_tailnet_keeps_both_addresses_working(self):
        fake = FakeNextcloud()
        self.run_gateways(fake, '192.168.1.20', 'neko.tail1234.ts.net')
        self.assertNotIn('overwritehost', fake.system)
        self.assertEqual(
            fake.system['trusted_domains'], ['localhost', '127.0.0.1', '192.168.1.20', 'neko.tail1234.ts.net']
        )
        # No forced https: Nextcloud on this PC itself (http://127.0.0.1:8088) keeps working;
        # both gateways say the client used https.
        self.assertNotIn('overwriteprotocol', fake.system)
        # The first gateway's address stays the canonical one for background jobs.
        self.assertEqual(fake.system['overwrite.cli.url'], 'https://192.168.1.20:8447')

    def test_an_earlier_forced_https_is_taken_out(self):
        fake = FakeNextcloud({'trusted_domains': ['localhost'], 'overwriteprotocol': 'https'})
        self.run_gateways(fake, '192.168.1.20')
        self.assertNotIn('overwriteprotocol', fake.system)

    def test_the_link_address_does_not_flip_between_gateway_ports(self):
        # The home gateway on a spare port, then Tailscale on 8447, then the home one again.
        fake = FakeNextcloud()
        with (
            mock.patch.object(gateway_tools, 'command', fake),
            mock.patch('neko_common.compose_image_override', return_value=[]),
        ):
            gateway_tools.configure_cloud_origin(CONFIG, '192.168.1.20', 8452)
            gateway_tools.configure_cloud_origin(CONFIG, 'neko.tail1234.ts.net', 8447)
            gateway_tools.configure_cloud_origin(CONFIG, '192.168.1.20', 8452)
        self.assertEqual(fake.system['overwrite.cli.url'], 'https://192.168.1.20:8452')
        sets = [c for c in fake.calls if 'overwrite.cli.url' in c and 'config:system:set' in c]
        self.assertEqual(len(sets), 1)

    def test_removes_overwritehost_left_by_v0210(self):
        fake = FakeNextcloud({'trusted_domains': ['localhost'], 'overwritehost': '192.168.1.20:8447'})
        self.run_gateways(fake, 'neko.tail1234.ts.net')
        self.assertNotIn('overwritehost', fake.system)

    def test_keeps_an_overwritehost_the_user_set(self):
        fake = FakeNextcloud({'trusted_domains': ['localhost'], 'overwritehost': 'cloud.example.org'})
        self.run_gateways(fake, '192.168.1.20')
        self.assertEqual(fake.system['overwritehost'], 'cloud.example.org')

    def test_trusts_the_docker_gateway_hop(self):
        fake = FakeNextcloud(gateways=('172.19.0.1', '172.20.0.1'))
        self.run_gateways(fake, '192.168.1.20')
        # Docker's whole default range: a network made again with another number still works.
        self.assertEqual(fake.system['trusted_proxies'], ['172.16.0.0/12'])

    def test_a_gateway_outside_dockers_default_range_is_trusted_by_itself(self):
        # Never the whole 192.168.0.0/16: that is the usual home network range too.
        fake = FakeNextcloud(gateways=('192.168.48.1',))
        self.run_gateways(fake, '192.168.1.20')
        self.assertEqual(fake.system['trusted_proxies'], ['172.16.0.0/12', '192.168.48.1'])

    def test_rerun_is_idempotent(self):
        fake = FakeNextcloud()
        self.run_gateways(fake, '192.168.1.20', '192.168.1.20')
        self.assertEqual(fake.system['trusted_domains'].count('192.168.1.20'), 1)
        self.assertEqual(fake.system['trusted_proxies'], ['172.16.0.0/12'])

    def test_refuses_public_or_missing_gateway(self):
        fake = FakeNextcloud(gateways=('8.8.8.8',))
        with self.assertRaises(RuntimeError):
            self.run_gateways(fake, '192.168.1.20')

    def test_skipped_when_nextcloud_not_exposed(self):
        fake = FakeNextcloud()
        with mock.patch.object(gateway_tools, 'command', fake):
            gateway_tools.configure_cloud_origin(
                {**CONFIG, 'gateway': {'services': ['jellyfin']}}, '192.168.1.20', 8447
            )
        self.assertEqual(fake.calls, [])


class GatewayListTests(unittest.TestCase):
    """The shipped gateway list names every app; a media-only server (Immich and
    Nextcloud off) must not fail on it, while a list you wrote is taken as it is."""

    def test_the_default_list_follows_the_apps_set_up(self):
        config = {
            'gateway': {'services': list(gateway_tools.DEFAULT_SERVICES)},
            'media': {'services': ['jellyfin', 'seerr']},
            'photos': {'deploy_immich': False},
            'nextcloud': {'enabled': False},
        }
        self.assertEqual(gateway_tools.selected(config), ['jellyfin', 'seerr'])
        config['photos']['deploy_immich'] = True
        self.assertEqual(gateway_tools.selected(config), ['jellyfin', 'seerr', 'immich'])

    def test_a_list_you_wrote_names_what_is_missing(self):
        config = {
            'gateway': {'services': ['jellyfin', 'immich']},
            'media': {'services': ['jellyfin']},
            'photos': {'deploy_immich': False},
        }
        with self.assertRaisesRegex(RuntimeError, '"immich" is in the gateway list'):
            gateway_tools.selected(config)


class ReadinessTests(unittest.TestCase):
    CONFIG = {
        'gateway': {'services': ['navidrome'], 'confirmed_services': ['navidrome']},
        'media': {},
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
    }

    def setUp(self):
        installed = mock.patch.object(gateway_tools, 'installed', return_value=True)  # the app is set up
        installed.start()
        self.addCleanup(installed.stop)

    def opener(self, pages):
        """A local opener answering each path with its body."""

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, limit):
                return self.body.encode()

        fake = mock.Mock()
        fake.open.side_effect = lambda url, timeout=5: Response(next(b for p, b in pages.items() if url.endswith(p)))
        return fake

    def test_an_app_without_its_first_account_is_never_published(self):
        cases = (
            ('immich', {'/api/server/config': '{"isInitialized": false}'}),
            ('nextcloud', {'/status.php': '{"installed": false}'}),
            ('navidrome', {'/app/': '<script>window.__APP_CONFIG__ = "{\\"firstTime\\":true}"</script>'}),
        )
        for app, pages in cases:
            config = {'gateway': {'services': [app], 'confirmed_services': [app]}, 'media': {}}
            with (
                mock.patch.object(gateway_tools, 'local_opener', return_value=self.opener(pages)),
                self.assertRaisesRegex(RuntimeError, 'has no account yet'),
            ):
                gateway_tools.readiness(config)

    def test_navidrome_with_its_account_passes_its_check(self):
        page = '<script>window.__APP_CONFIG__ = "{\\"firstTime\\":false}"</script>'
        gateway_tools.navidrome_onboarded(self.opener({'/app/': page}))

    def test_a_snap_docker_is_named_before_any_container_is_checked(self):
        mock.patch.object(gateway_tools, 'navidrome_onboarded').start()
        self.addCleanup(mock.patch.stopall)
        # Tried on Ubuntu: the snap's Docker took over Docker's socket, so every app's
        # container looked missing and the gateway said only that containers were wrong.
        with (
            mock.patch.object(gateway_tools.host, 'snap_docker', return_value=True),
            mock.patch('acceptance.containers') as containers,
        ):
            with self.assertRaisesRegex(RuntimeError, 'Docker is installed here as a snap'):
                gateway_tools.readiness(dict(self.CONFIG))
        containers.assert_not_called()

    def test_an_app_that_does_not_answer_is_named(self):
        config = {'gateway': {'services': ['jellyfin'], 'confirmed_services': ['jellyfin']}, 'media': {}}
        refused = mock.Mock()
        refused.open.side_effect = OSError('Connection refused')
        with mock.patch.object(gateway_tools, 'local_opener', return_value=refused):
            with self.assertRaisesRegex(RuntimeError, r'Jellyfin did not answer on this PC \(http://127.0.0.1:8096'):
                gateway_tools.readiness(config)

    def test_the_containers_not_verified_are_named(self):
        mock.patch.object(gateway_tools, 'navidrome_onboarded').start()
        self.addCleanup(mock.patch.stopall)
        rows = [{'id': 'container:navidrome', 'name': 'Music / navidrome', 'status': 'fail'}]
        with (
            mock.patch.object(gateway_tools.host, 'snap_docker', return_value=False),
            mock.patch('acceptance.containers', return_value=rows),
        ):
            with self.assertRaisesRegex(RuntimeError, r'not verified: Music / navidrome \(sudo docker ps -a'):
                gateway_tools.readiness(dict(self.CONFIG))


if __name__ == '__main__':
    unittest.main()
