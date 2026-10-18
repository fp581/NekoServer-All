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
        occ = args[args.index('occ') + 1:]
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
                array[index:index + 1] = [value]
            else:
                self.system[key] = value
            return ''
        raise AssertionError(args)


CONFIG = {'data_mount': '/srv/nekoserver', 'gateway': {'services': ['nextcloud', 'jellyfin']},
          'nextcloud': {'enabled': True}, 'media': {'services': ['jellyfin']}}


class CloudOriginTests(unittest.TestCase):
    def run_gateways(self, fake, *hosts):
        with mock.patch.object(gateway_tools, 'command', fake), \
                mock.patch('neko_common.compose_image_override', return_value=[]):
            for host in hosts:
                gateway_tools.configure_cloud_origin(CONFIG, host, 8447)

    def test_lan_then_tailnet_keeps_both_addresses_working(self):
        fake = FakeNextcloud()
        self.run_gateways(fake, '192.168.1.20', 'neko.tail1234.ts.net')
        self.assertNotIn('overwritehost', fake.system)
        self.assertEqual(fake.system['trusted_domains'],
                         ['localhost', '127.0.0.1', '192.168.1.20', 'neko.tail1234.ts.net'])
        self.assertEqual(fake.system['overwriteprotocol'], 'https')
        # The first gateway's address stays the canonical one for background jobs.
        self.assertEqual(fake.system['overwrite.cli.url'], 'https://192.168.1.20:8447')

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
        self.assertEqual(fake.system['trusted_proxies'], ['172.19.0.1', '172.20.0.1'])

    def test_rerun_is_idempotent(self):
        fake = FakeNextcloud()
        self.run_gateways(fake, '192.168.1.20', '192.168.1.20')
        self.assertEqual(fake.system['trusted_domains'].count('192.168.1.20'), 1)
        self.assertEqual(fake.system['trusted_proxies'], ['172.19.0.1'])

    def test_refuses_public_or_missing_gateway(self):
        fake = FakeNextcloud(gateways=('8.8.8.8',))
        with self.assertRaises(RuntimeError):
            self.run_gateways(fake, '192.168.1.20')

    def test_skipped_when_nextcloud_not_exposed(self):
        fake = FakeNextcloud()
        with mock.patch.object(gateway_tools, 'command', fake):
            gateway_tools.configure_cloud_origin({**CONFIG, 'gateway': {'services': ['jellyfin']}}, '192.168.1.20', 8447)
        self.assertEqual(fake.calls, [])


if __name__ == '__main__':
    unittest.main()
