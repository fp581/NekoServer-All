"""The home gateway set up (gateway_tools._apply_lan, `gateway-tools.py apply-lan`): what
it writes and where, that the settings are checked before anything is replaced, how its
service is started, and which of the ports in use are its own."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools
import native_runtime

LAN_IP = '192.168.1.5'
PORTS = {'portal': 8442, 'nextcloud': 8447, 'jellyfin': 8096}


class ApplyLanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.config = {
            'data_mount': str(self.data),
            'media_mount': '/mnt/nekoserver-media',
            'storage': {},
            'gateway': {'lan_ip': LAN_IP, 'lan_cidr': '192.168.1.0/24', 'other': 'x'},
        }
        self.done = []  # (what, details) in the order they happened
        self.caddy = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

        def note(what, result=None):
            def record(*args, **kwargs):
                self.done.append((what, args))
                return result

            return record

        fakes = {
            'verify_lan': note('verify_lan'),
            'readiness': note('readiness'),
            'lan_model': lambda config, web: {'web': str(web)},
            'unit': lambda config, code, state: f'unit {code} {state}',
            'portal': lambda config, address: f'portal {address}',
            'lan': lambda config: (LAN_IP, '192.168.1.0/24'),
            'lan_ports': lambda config: dict(PORTS),
            'selected': lambda config: ['jellyfin'],
            'preflight': note('preflight', []),
            'port_preflight': note('port_preflight'),
            'caddy_account': lambda: self.caddy,
            'adopt_state': note('adopt_state', 'The old Caddy state was taken over'),
            'check_caddy_can_reach': note('check_caddy_can_reach'),
            'validate_caddy': note('validate_caddy'),
            'ensure_directory': note('ensure_directory'),
            'set_aside': lambda aside: [],
            'move_unit_copies': lambda: [],
            'atomic_write': note('atomic_write'),
            'atomic_json': note('atomic_json'),
            'configure_cloud_origin': note('configure_cloud_origin'),
            'seerr_play_links': note('seerr_play_links', ''),
            'start_lan_gateway': note('start_lan_gateway'),
        }
        for name, fake in fakes.items():
            patch = mock.patch.object(gateway_tools, name, fake)
            patch.start()
            self.addCleanup(patch.stop)
        for target, name, fake in (
            (gateway_tools.recovery_tools, 'require_root', note('require_root')),
            (gateway_tools.recovery_tools, 'mounts', note('mounts')),
            (native_runtime, 'payload', lambda root, config: b'the runtime'),
            (gateway_tools.os, 'chown', note('chown')),
        ):
            patch = mock.patch.object(target, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def steps(self, what):
        return [args for name, args in self.done if name == what]

    def order(self):
        return [name for name, _ in self.done]

    def test_the_files_written_are_the_gateways_settings_service_page_and_program(self):
        report = gateway_tools._apply_lan(self.config)
        code = Path('/usr/local/lib/nekoserver-gateway') / ('v' + gateway_tools.VERSION)
        web = Path('/usr/local/share/nekoserver-gateway/lan')
        state = self.data / 'gateway/caddy-state'
        written = {args[0]: (args[1], args[2]) for args in self.steps('atomic_write')}
        self.assertEqual(
            written,
            {
                gateway_tools.LAN_CONFIG: ('{\n  "web": "' + str(web) + '"\n}\n', 0o644),
                gateway_tools.SYSTEMD_UNITS / gateway_tools.GATEWAY_LAN_UNIT: (f'unit {code} {state}', 0o644),
                web / 'index.html': (f'portal {LAN_IP}', 0o644),
                code / native_runtime.NAME: (b'the runtime', 0o750),
            },
        )
        self.assertEqual(gateway_tools.GATEWAY_LAN_UNIT, 'nekoserver-gateway-lan.service')
        # Caddy's own state: a private folder for the Caddy account.
        self.assertTrue(state.is_dir())
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.steps('chown'), [(state, self.caddy.pw_uid, self.caddy.pw_gid)])
        # The folders Caddy must pass through: checked for it first, then made open to read for it.
        public = (self.data / 'gateway', code.parent, code, web.parent, web, Path('/etc/nekoserver-gateway'))
        self.assertEqual(self.steps('check_caddy_can_reach'), [(public, self.caddy)])
        self.assertEqual(self.steps('ensure_directory'), [(folder, 0o755) for folder in public])
        saved = {args[0]: args[1] for args in self.steps('atomic_json')}
        self.assertEqual(
            saved[Path('/etc/nekoserver-gateway/storage.json')],
            {
                'data_mount': str(self.data),
                'media_mount': '/mnt/nekoserver-media',
                'storage': {},
                'gateway': {'lan_ip': LAN_IP, 'lan_cidr': '192.168.1.0/24'},
            },
        )
        record = saved[self.data / 'gateway/lan-managed.json']
        self.assertEqual(saved[gateway_tools.LAN_RECORD], record)
        self.assertEqual((record['uid'], record['gid']), (self.caddy.pw_uid, self.caddy.pw_gid))
        self.assertEqual(set(record['sha256']), {str(p) for p in written})
        self.assertEqual(self.steps('configure_cloud_origin'), [(self.config, LAN_IP, 8447)])
        self.assertEqual(self.steps('seerr_play_links'), [(self.config, f'https://{LAN_IP}:8096', 'lan')])
        self.assertEqual(saved[self.data / 'reports/gateway-lan.json'], report)
        self.assertEqual(
            (report['mode'], report['portal'], report['services'], report['status']),
            ('lan', f'https://{LAN_IP}:8442/', ['jellyfin'], 'configured'),
        )
        self.assertNotIn('notes', report)

    def test_the_settings_and_drives_are_checked_before_anything_is_replaced(self):
        gateway_tools._apply_lan(self.config)
        order = self.order()
        first_write = order.index('atomic_write')
        for check in ('preflight', 'port_preflight', 'check_caddy_can_reach', 'validate_caddy'):
            self.assertLess(order.index(check), first_write, check)
        # The drives and the home address once more, after the slow Caddy check.
        self.assertEqual(order.count('mounts'), 2)
        self.assertEqual(order.count('verify_lan'), 2)
        self.assertLess(order.index('validate_caddy'), len(order) - 1 - order[::-1].index('mounts'))
        self.assertLess(len(order) - 1 - order[::-1].index('mounts'), first_write)
        # The service starts once every file is in place; the report is written last.
        last_write = len(order) - 1 - order[::-1].index('atomic_write')
        self.assertLess(last_write, order.index('start_lan_gateway'))
        self.assertEqual(order[-2:], ['start_lan_gateway', 'atomic_json'])
        self.assertEqual([args[0] for args in self.steps('mounts')], [self.config, self.config])
        self.assertEqual([args[0] for args in self.steps('verify_lan')], [self.config, self.config])
        self.assertEqual(self.steps('readiness'), [(self.config,)])
        self.assertEqual(self.steps('port_preflight'), [(self.config,)])
        self.assertEqual(
            self.steps('validate_caddy'), [('{\n  "web": "/usr/local/share/nekoserver-gateway/lan"\n}\n',)]
        )

    def test_a_caddy_setting_refused_changes_nothing(self):
        def refuse(settings):
            raise RuntimeError('caddy validate failed')

        with mock.patch.object(gateway_tools, 'validate_caddy', refuse):
            with self.assertRaisesRegex(RuntimeError, 'caddy validate failed'):
                gateway_tools._apply_lan(self.config)
        for what in ('atomic_write', 'atomic_json', 'start_lan_gateway', 'ensure_directory', 'chown'):
            self.assertEqual(self.steps(what), [], what)

    def test_caddy_state_left_by_another_account_is_taken_over_and_said(self):
        state = self.data / 'gateway/caddy-state'
        state.mkdir(parents=True)
        for uid, gid in ((os.getuid() + 1, os.getgid()), (os.getuid(), os.getgid() + 1)):
            with self.subTest(uid=uid, gid=gid):
                self.done.clear()
                self.caddy.pw_uid, self.caddy.pw_gid = uid, gid
                report = gateway_tools._apply_lan(self.config)
                self.assertEqual(len(self.steps('adopt_state')), 1)
                self.assertEqual(report['notes'][0], 'The old Caddy state was taken over')

    def test_caddy_state_already_the_caddy_accounts_is_left_alone(self):
        (self.data / 'gateway/caddy-state').mkdir(parents=True)
        report = gateway_tools._apply_lan(self.config)
        self.assertEqual(self.steps('adopt_state'), [])
        self.assertNotIn('notes', report)


class StartTests(unittest.TestCase):
    def test_the_service_is_reloaded_enabled_and_restarted(self):
        with mock.patch.object(gateway_tools, 'command') as command:
            gateway_tools.start_lan_gateway()
        unit = 'nekoserver-gateway-lan.service'
        self.assertEqual(
            [call.args[0] for call in command.call_args_list],
            [['systemctl', 'daemon-reload'], ['systemctl', 'enable', unit], ['systemctl', 'restart', unit]],
        )

    def test_a_service_that_does_not_start_is_stopped_and_the_failure_kept(self):
        def command(argv, **more):
            if argv[1] == 'restart':
                raise RuntimeError('Job for nekoserver-gateway-lan.service failed')

        with mock.patch.object(gateway_tools, 'command', side_effect=command) as called:
            with self.assertRaisesRegex(RuntimeError, 'Job for nekoserver-gateway-lan.service failed'):
                gateway_tools.start_lan_gateway()
        self.assertEqual(called.call_args_list[-1].args[0], ['systemctl', 'stop', 'nekoserver-gateway-lan.service'])


class OwnListenersTests(unittest.TestCase):
    """The gateway's own ports are not "taken" by another program, but only while its
    service runs and its settings file is the one it wrote."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.settings = self.data / 'lan.json'
        self.settings.write_text('{}')
        import hashlib

        digest = hashlib.sha256(self.settings.read_bytes()).hexdigest()
        (self.data / 'gateway').mkdir()
        (self.data / 'gateway/lan-managed.json').write_text('{"sha256": {"%s": "%s"}}' % (self.settings, digest))
        self.model = {'apps': {'http': {'servers': {'portal': {'listen': ['192.168.1.5:8442']}}}}}

    def owned(self, active):
        with (
            mock.patch.object(gateway_tools, 'LAN_CONFIG', self.settings),
            mock.patch.object(
                gateway_tools.health,
                'read_json',
                side_effect=lambda path: (
                    self.model if Path(path) == self.settings else json.loads(Path(path).read_text())
                ),
            ),
            mock.patch.object(gateway_tools.health, 'run', return_value=(0 if active else 3, '')) as run,
        ):
            owned = gateway_tools.owned_listeners({'data_mount': str(self.data)})
        run.assert_called_once_with(['systemctl', 'is-active', '--quiet', 'nekoserver-gateway-lan.service'])
        return owned

    def test_a_running_gateways_ports_are_its_own(self):
        self.assertEqual(self.owned(True), {'192.168.1.5:8442'})

    def test_a_stopped_gateways_ports_are_not(self):
        self.assertEqual(self.owned(False), set())


def routes(host, **ports):
    """A Tailscale Serve state: each port to the address it leads to."""
    return {
        'TCP': {port: {'HTTPS': True} for port in ports},
        'Web': {f'{host}:{port}': {'Handlers': {'/': {'Proxy': to}}} for port, to in ports.items()},
    }


class TailnetRoutesTests(unittest.TestCase):
    """Tailscale's routes brought to what NekoServer wants: its own unwanted ones off
    first, then each one not yet as wanted (with time for Tailscale to answer)."""

    def test_unwanted_routes_go_off_and_new_ones_are_set_and_the_rest_left(self):
        host = 'neko.tail1234.ts.net'
        state = routes(host, **{'8443': 'http://127.0.0.1:8096', '8444': 'http://127.0.0.1:5055'})
        target = routes(host, **{'8443': 'http://127.0.0.1:8096', '8445': 'http://127.0.0.1:4533'})
        with mock.patch.object(gateway_tools, 'command') as command:
            gateway_tools.set_tailnet_routes(state, {'8443', '8444'}, target, host)
        self.assertEqual(
            command.call_args_list,
            [
                mock.call(['tailscale', 'serve', '--bg', '--https=8444', 'off']),
                mock.call(['tailscale', 'serve', '--bg', '--https=8445', 'http://127.0.0.1:4533'], 45),
            ],
        )


if __name__ == '__main__':
    unittest.main()
