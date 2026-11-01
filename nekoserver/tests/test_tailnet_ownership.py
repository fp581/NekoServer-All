"""NekoServer's Tailscale Serve routes are recognised by their port and what they point
at, not by the server's name or the dashboard's port: a renamed server, a dashboard
moved to another port, or a route of your own on another port never stops remote-access
setup, and a route of your own is never touched."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway_tools as g  # noqa: E402


class FakeTailscale:
    """tailscale serve, as far as setup uses it: per-port HTTPS routes to a local address."""

    def __init__(self, host):
        self.host, self.state, self.calls = host, {'TCP': {}, 'Web': {}}, []

    def serve(self, args, timeout=30, env=None):
        self.calls.append(args)
        port = args[3].split('=')[1]
        if args[4] == 'off':
            self.state['TCP'].pop(port, None)
            self.state['Web'] = {k: v for k, v in self.state['Web'].items() if k.rpartition(':')[2] != port}
        else:
            self.state['TCP'][port] = {'HTTPS': True}
            self.state['Web'][self.host + ':' + port] = {'Handlers': {'/': {'Proxy': args[4]}}}
        return ''

    def current(self):
        return json.loads(json.dumps(self.state))


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / 'gateway').mkdir()
        self.config = {
            'data_mount': str(self.data),
            'gateway': {'services': ['jellyfin'], 'confirmed_services': ['jellyfin']},
        }
        self.ts = FakeTailscale('neko.tail1.ts.net')
        self.board = 'http://127.0.0.1:8787'

    def apply(self):
        with (
            mock.patch.object(g.recovery_tools, 'require_root'),
            mock.patch.object(g.recovery_tools, 'mounts'),
            mock.patch.object(g, 'readiness'),
            mock.patch.object(g, 'configure_cloud_origin'),
            mock.patch.object(g, 'tailnode', side_effect=lambda: self.ts.host),
            mock.patch.object(g, 'dashboard_target', side_effect=lambda config: self.board),
            mock.patch.object(g, 'serve_state', side_effect=self.ts.current),
            mock.patch.object(g, 'command', side_effect=self.ts.serve),
            mock.patch.object(g, 'installed', return_value=True),
        ):
            return g._apply_tailnet(self.config)

    def ledger(self):
        return json.loads((self.data / 'gateway/tailnet-owned.json').read_text())

    def test_a_renamed_server_gets_its_routes_under_the_new_name(self):
        self.apply()
        self.ts.host = 'neko-1.tail1.ts.net'  # renamed, or added again after a reinstall
        self.apply()
        self.assertEqual(sorted(self.ts.state['Web']), ['neko-1.tail1.ts.net:8443', 'neko-1.tail1.ts.net:8448'])
        self.assertEqual(self.ledger()['host'], 'neko-1.tail1.ts.net')

    def test_the_dashboard_moving_port_twice_is_followed(self):
        self.apply()
        for port in ('9000', '9100'):
            self.board = 'http://127.0.0.1:' + port
            self.apply()
            self.assertEqual(self.ts.state['Web']['neko.tail1.ts.net:8448']['Handlers']['/']['Proxy'], self.board)

    def test_a_route_of_your_own_on_another_port_stays_and_does_not_stop_setup(self):
        self.ts.state = {
            'TCP': {'443': {'HTTPS': True}},
            'Web': {'neko.tail1.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8123'}}}},
        }
        self.apply()
        self.apply()
        self.assertIn('443', self.ts.state['TCP'])
        self.assertFalse(any(c[3] == '--https=443' for c in self.ts.calls))
        self.assertNotIn('443', self.ledger()['owned']['TCP'])

    def test_a_route_of_your_own_on_a_port_nekoserver_needs_stops_with_what_to_do(self):
        self.ts.state = {
            'TCP': {'8443': {'HTTPS': True}},
            'Web': {'neko.tail1.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}},
        }
        with self.assertRaisesRegex(RuntimeError, 'port 8443 already has a route of your own'):
            self.apply()
        self.assertEqual(self.ts.calls, [])

    def test_an_app_taken_out_loses_only_its_own_route(self):
        self.config['gateway']['services'] = self.config['gateway']['confirmed_services'] = ['jellyfin', 'immich']
        self.apply()
        self.assertIn('8445', self.ts.state['TCP'])
        self.config['gateway']['services'] = self.config['gateway']['confirmed_services'] = ['jellyfin']
        self.apply()
        self.assertEqual(sorted(self.ts.state['TCP']), ['8443', '8448'])

    def test_a_dashboard_moved_later_is_followed_by_the_refresh_alone(self):
        self.apply()
        self.board = 'http://127.0.0.1:9000'
        logged = []
        with (
            mock.patch.object(g, 'tailnode', side_effect=lambda: self.ts.host),
            mock.patch.object(g, 'dashboard_target', side_effect=lambda config: self.board),
            mock.patch.object(g, 'serve_state', side_effect=self.ts.current),
            mock.patch.object(g, 'command', side_effect=self.ts.serve),
        ):
            self.assertTrue(g.refresh_dashboard_route(self.config, logged.append))
            self.assertFalse(g.refresh_dashboard_route(self.config, logged.append))
        self.assertEqual(self.ts.state['Web']['neko.tail1.ts.net:8448']['Handlers']['/']['Proxy'], self.board)
        self.assertEqual(self.ledger()['owned']['Web']['neko.tail1.ts.net:8448']['Handlers']['/']['Proxy'], self.board)
        self.assertIn('8443', self.ledger()['owned']['TCP'])
        import installer

        self.assertIn('gateway_tailnet', installer.REFRESHED)

    def test_a_journal_that_is_not_nekoservers_shape_still_stops_setup(self):
        (self.data / 'gateway/tailnet-owned.json').write_text(
            json.dumps(
                {
                    'format': 'neko-tailnet-owned-v1',
                    'status': 'configured',
                    'host': 'x',
                    'owned': {'TCP': {'22': {'HTTPS': True}}, 'Web': {}},
                }
            )
        )
        with self.assertRaisesRegex(RuntimeError, 'Malformed'):
            self.apply()


class PortArgumentTests(unittest.TestCase):
    def test_only_a_port_number_reaches_tailscale_serve(self):
        self.assertEqual(g.https('8443'), '--https=8443')
        self.assertEqual(g.https(443), '--https=443')
        for bad in ('0', '65536', '-1', '443 --yes', '--set-path=/x', ''):
            with self.assertRaises(ValueError):
                g.https(bad)


class UninstallerRoutesTests(unittest.TestCase):
    def test_the_uninstaller_recognises_them_the_same_way(self):
        import uninstaller

        owned = g.desired('old.tail1.ts.net', ['jellyfin'])
        renamed = g.desired('new.tail1.ts.net', ['jellyfin'])
        mine_elsewhere = {
            'TCP': {**renamed['TCP'], '443': {'HTTPS': True}},
            'Web': {**renamed['Web'], 'new.tail1.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8123'}}}},
        }
        self.assertEqual(uninstaller.unchanged_routes(owned, mine_elsewhere), ['8443'])
        changed = g.desired('new.tail1.ts.net', ['jellyfin'])
        changed['Web']['new.tail1.ts.net:8443']['Handlers']['/']['Proxy'] = 'http://127.0.0.1:1'
        self.assertEqual(uninstaller.unchanged_routes(owned, changed), [])


if __name__ == '__main__':
    unittest.main()
