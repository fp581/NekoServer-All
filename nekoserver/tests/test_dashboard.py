"""The dashboard: its files, the server that serves them, and how it is installed."""

import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules import dashboard  # noqa: E402

SERVE = (ROOT / 'runtime/src/serve.rs').read_text()
APP = (ROOT / 'dashboard/app.js').read_text()
PAGE = (ROOT / 'dashboard/index.html').read_text()


HOST_NETWORK = 'net:[1]'  # PID 1's network namespace, as /proc shows it
NOWHERE = Path('/nonexistent')


def enabled(unit):
    """The systemctl call that enables and starts a unit."""
    return ('enable', '--now', unit)


def served_files():
    block = SERVE[SERVE.index('const ASSETS') : SERVE.index('];', SERVE.index('const ASSETS'))]
    return {m.group(1) for m in re.finditer(r'\(\s*"/[^"]*",\s*"([^"]+)"', block)}


def server_codes():
    block = SERVE[SERVE.index('pub const ERRORS') : SERVE.index('];', SERVE.index('pub const ERRORS'))]
    return set(re.findall(r'\("(E\d\d)"', block))


class DashboardFilesTests(unittest.TestCase):
    def test_the_installer_copies_exactly_what_the_server_serves(self):
        self.assertEqual(set(dashboard.ASSETS), served_files())
        for name in dashboard.ASSETS:
            self.assertTrue((ROOT / 'dashboard' / name).is_file(), name)

    def test_every_element_the_script_uses_is_on_the_page(self):
        ids = set(re.findall(r'\$\(\'([a-z0-9-]+)\'\)', APP)) | set(re.findall(r"put\('([a-z0-9-]+)'", APP))
        ids |= set(re.findall(r"setBadge\('([a-z0-9-]+)'", APP))
        missing = {i for i in ids if f'id="{i}"' not in PAGE}
        self.assertEqual(missing, set())

    def test_the_page_loads_nothing_from_elsewhere(self):
        # The server's Content-Security-Policy allows only its own files.
        for ref in re.findall(r'(?:src|href)="([^"#]+)"', PAGE):
            self.assertFalse(ref.startswith(('http:', 'https:', '//')), ref)
        self.assertNotIn('<script>', PAGE)
        self.assertNotIn(' style="', PAGE)

    def test_error_codes_are_explained_everywhere(self):
        page_codes = set(re.findall(r'^\s+(E\d\d): \[', APP, re.M))
        self.assertLessEqual(server_codes(), page_codes)
        guide = (ROOT / 'DASHBOARD.md').read_text()
        for code in page_codes:
            self.assertIn('| ' + code + ' |', guide, code)
        rows = re.findall(r"^\s+(E\d\d): \[\s*'([^']+)',\s*'([^']+)'\s*\]", APP, re.M)
        self.assertEqual({r[0] for r in rows}, page_codes)
        for code, title, fix in rows:
            self.assertLessEqual(len(title.split()), 7, code)  # short enough to read at a glance
            self.assertLessEqual(len(fix), 110, code)

    def test_closing_a_desktop_app_is_confirmed_like_force_close(self):
        # A plain "please quit" can lose unsaved work too: no menu entry closes without asking.
        menu = APP[APP.index('function desktopMenu') : APP.index('function processMenu')]
        self.assertIn('run: () => confirmClose(r, label)', menu)
        self.assertNotIn('closeDesktop(', menu)
        self.assertNotIn('like closing its window', APP)
        close = APP[APP.index('function confirmClose') : APP.index('function confirmForce')]
        self.assertIn('may be lost', close)

    def test_the_app_manifest_points_at_real_icons(self):
        import json

        manifest = json.loads((ROOT / 'dashboard/manifest.webmanifest').read_text())
        self.assertEqual(manifest['display'], 'standalone')
        for icon in manifest['icons']:
            self.assertIn(icon['src'], dashboard.ASSETS)
        worker = (ROOT / 'dashboard/sw.js').read_text()
        self.assertIn("url.pathname.startsWith('/api/')", worker)  # readings are never cached
        for name in re.findall(r"'/([a-z0-9.-]+)'", worker.split('const SHELL')[1].split(';')[0]):
            self.assertIn(name, dashboard.ASSETS)


class ReadingFreshnessTests(unittest.TestCase):
    def test_late_readings_use_the_configured_interval_everywhere(self):
        import neko_common

        self.assertEqual(neko_common.reading_max_age(60), 150)
        self.assertEqual(neko_common.reading_max_age(300), 630)
        self.assertEqual(neko_common.reading_max_age('x'), 150)
        # The page uses the same rule (statusStale in app.js).
        self.assertIn('return 2 * interval + 30;', APP)
        settings = dashboard.collector_settings(
            {
                'data_mount': '/srv',
                'media_mount': '/m',
                'external_backup_mount': '/u',
                'storage': {},
                'media': {'services': []},
            },
            1800,
            300,
        )
        self.assertEqual(settings['dashboard']['interval_seconds'], 300)
        self.assertIn('"collect_interval"', (ROOT / 'runtime/src/collect.rs').read_text())

    def test_the_acceptance_check_accepts_a_five_minute_schedule(self):
        import io
        import json
        import time
        from unittest import mock
        import acceptance

        now = time.time()

        def answer(age, interval):
            body = json.dumps(
                {'format': 'neko-dashboard-v1', 'generated_at': now - age, 'collect_interval': interval}
            ).encode()
            response = mock.MagicMock(status=200)
            response.read = io.BytesIO(body).read
            response.__enter__.return_value = response
            return response

        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 300)):
            self.assertTrue(acceptance.dashboard_check(8787, now))
        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 60)):
            self.assertFalse(acceptance.dashboard_check(8787, now))

    def test_the_acceptance_check_covers_the_control_service_and_its_network(self):
        """The control service must run on the computer's own network: there it can see a
        restarted app answer. OpenRGB must not: there any program could reach it."""
        import acceptance

        nets = {'1': HOST_NETWORK, '7': HOST_NETWORK, '8': 'net:[2]'}
        pids = {acceptance.CONTROL_UNIT: '7', acceptance.OPENRGB_UNIT: '8'}

        def run(args, timeout=8):
            return 0, pids[args[2]] + '\n'

        with (
            mock.patch.object(acceptance, 'service', return_value=True),
            mock.patch.object(acceptance.health, 'run', side_effect=run),
            mock.patch.object(acceptance.os, 'readlink', side_effect=lambda path: nets[path.split('/')[2]]),
        ):
            self.assertEqual(acceptance.control_check()[0], 'pass')
            self.assertEqual(acceptance.lighting_check()[0], 'pass')
            nets['7'], nets['8'] = 'net:[2]', HOST_NETWORK  # as 0.39.6 to 0.40.0 (or after an upgrade) left them
            self.assertEqual(acceptance.control_check()[0], 'fail')
            self.assertIn('never answer', acceptance.control_check()[1])
            self.assertEqual(acceptance.lighting_check()[0], 'fail')
            pids[acceptance.CONTROL_UNIT] = '0'  # not running: can't be told
            self.assertEqual(acceptance.control_check()[0], 'needs_setup')
        with mock.patch.object(acceptance, 'service', return_value=False):
            self.assertEqual(acceptance.control_check()[0], 'fail')
        # Part of the dashboard's checks; lighting only when it is on and set up.
        with (
            mock.patch.object(acceptance, 'dashboard_check', return_value=True),
            mock.patch.object(acceptance, 'service', return_value=True),
            mock.patch.object(acceptance, 'control_check', return_value=('pass', 'ok')),
            mock.patch.object(acceptance, 'lighting_check', return_value=('pass', 'ok')) as lighting,
            mock.patch.object(acceptance.Path, 'is_file', return_value=True),
        ):
            ids = [c['id'] for c in acceptance.dashboard_checks({'dashboard': {'port': 8787}}, 0)]
            self.assertIn('dashboard:control', ids)
            self.assertIn('dashboard:lighting', ids)
            lighting.reset_mock()
            ids = [c['id'] for c in acceptance.dashboard_checks({'dashboard': {'port': 8787, 'rgb': False}}, 0)]
            self.assertNotIn('dashboard:lighting', ids)
            lighting.assert_not_called()


class DashboardInstallTests(unittest.TestCase):
    def test_the_web_server_starts_with_the_computer_and_comes_back(self):
        units = dashboard.units(Path('/srv/nekoserver'), Path('/usr/local/lib/x'), Path('/etc/x.json'), 8787, 60)
        service = units[dashboard.SERVICE]
        self.assertIn('Restart=always', service)
        self.assertIn('StartLimitIntervalSec=0', service)
        self.assertIn('WantedBy=multi-user.target', service)
        self.assertIn('DynamicUser=yes', service)  # never root
        self.assertIn('OnBootSec=', units['nekoserver-dashboard-collect.timer'])
        self.assertIn('WantedBy=timers.target', units['nekoserver-dashboard-collect.timer'])

    def test_the_launcher_opens_an_app_window_and_falls_back(self):
        text = dashboard.launcher(8787)
        self.assertIn('--app="$url"', text)
        self.assertIn('url=http://127.0.0.1:8787/', text)
        self.assertTrue(text.rstrip().endswith('exec xdg-open "$url"'))
        entry = dashboard.desktop_entry(Path('/usr/local/lib/nekoserver-dashboard/v1'))
        self.assertIn('Exec=' + str(dashboard.LAUNCHER), entry)
        self.assertIn('icon-512.png', entry)

    def test_a_launcher_someone_else_wrote_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            mine, theirs = Path(d) / 'mine', Path(d) / 'theirs'
            theirs.write_text('#!/bin/sh\necho custom\n')
            logs = []
            self.assertFalse(dashboard.write_managed(theirs, dashboard.launcher(8787), 0o755, logs.append))
            self.assertEqual(theirs.read_text(), '#!/bin/sh\necho custom\n')
            self.assertTrue(dashboard.write_managed(mine, dashboard.launcher(8787), 0o755, logs.append))
            self.assertEqual(os.stat(mine).st_mode & 0o777, 0o755)
            # Ours is replaced on the next release (a new port, say).
            self.assertTrue(dashboard.write_managed(mine, dashboard.launcher(9000), 0o755, logs.append))
            self.assertIn('9000', mine.read_text())


class FanAndLightControlTests(unittest.TestCase):
    code = Path('/usr/local/lib/nekoserver-dashboard/v1')

    def test_setup_waits_as_long_as_the_control_service_may_take_to_stop(self):
        # The service lets an app restart finish before it stops; its unit, and setup's
        # restart of it, must allow at least that long, plus the fans' hand-back.
        apps = (ROOT / 'runtime/src/apps.rs').read_text()
        longest = int(
            re.search(r'LONGEST_CHANGE_SECS: u64 = 30 \+ MOST_PARTS \* \(STOP_SECS \+ START_SECS\)', apps)
            and 30 + 2 * (120 + 120)
        )
        for name, value in (('STOP_SECS', 120), ('START_SECS', 120), ('MOST_PARTS', 2)):
            self.assertIn(f'const {name}: u64 = {value};', apps)
        self.assertIn('Duration::from_secs(apps::LONGEST_CHANGE_SECS)', (ROOT / 'runtime/src/control.rs').read_text())
        self.assertGreaterEqual(dashboard.CONTROL_STOP_SECONDS, longest + 60)
        self.assertGreater(dashboard.CONTROL_WAIT, dashboard.CONTROL_STOP_SECONDS)
        from unittest import mock

        with (
            mock.patch.object(dashboard, 'systemctl') as systemctl,
            mock.patch.object(dashboard, 'choose_pin'),
            mock.patch.object(dashboard, 'stop_control'),
            mock.patch.object(dashboard, 'CONTROL_CONFIG', Path('/nonexistent')),
        ):
            try:
                dashboard.start_services({dashboard.CONTROL: ''}, self.code, False, lambda m: None)
            except (OSError, RuntimeError):
                pass
        stops = {c.args[1]: c.kwargs.get('timeout') for c in systemctl.call_args_list if c.args[0] == 'stop'}
        self.assertEqual(stops[dashboard.CONTROL], dashboard.CONTROL_WAIT)

    @staticmethod
    def section(text, name):
        return text.split('[' + name + ']\n', 1)[1].split('\n[', 1)[0].split('\n')

    def test_the_control_service_stays_on_the_computers_own_network(self):
        """It checks that a restarted app answers on 127.0.0.1, where Docker publishes
        the apps: a private network of its own (0.39.6 to 0.40.0) has its own 127.0.0.1,
        where no app ever answers, so every restart from the dashboard failed."""
        for lighting in (True, False):
            control = dashboard.control_units(self.code, lighting)[dashboard.CONTROL]
            self.assertNotIn('PrivateNetwork', control)
            self.assertNotIn('JoinsNamespaceOf', control)
            self.assertNotIn('NetworkNamespacePath', control)
            self.assertIn('IPAddressDeny=any\nIPAddressAllow=localhost\n', control)
            self.assertIn('RestrictAddressFamilies=AF_UNIX AF_INET\n', control)

    def test_only_the_control_service_can_reach_openrgb(self):
        """OpenRGB runs as root and drives the hardware: it is in a private network of
        its own, and the only way in is a root-only socket whose proxy joins that network,
        so no other program can skip the PIN through it."""
        section = self.section
        units = dashboard.control_units(self.code, True, proxyd=Path('/usr/lib/systemd/systemd-socket-proxyd'))
        openrgb, proxy, socket = units[dashboard.OPENRGB], units[dashboard.PROXY], units[dashboard.PROXY_SOCKET]
        for text in (openrgb, proxy):
            self.assertIn('PrivateNetwork=yes', section(text, 'Service'))
            # Both create the namespaces whichever starts first, so neither can ever end
            # up on the computer's own network (or /tmp).
            self.assertIn('PrivateTmp=yes', section(text, 'Service'))
            self.assertIn('IPAddressDeny=any', section(text, 'Service'))
        self.assertIn('JoinsNamespaceOf=' + dashboard.PROXY, section(openrgb, 'Unit'))
        self.assertIn('JoinsNamespaceOf=' + dashboard.OPENRGB, section(proxy, 'Unit'))
        self.assertIn('BindsTo=' + dashboard.OPENRGB, section(proxy, 'Unit'))
        self.assertIn('After=' + dashboard.OPENRGB + ' ' + dashboard.PROXY_SOCKET, section(proxy, 'Unit'))
        self.assertIn('ExecStart=/usr/lib/systemd/systemd-socket-proxyd 127.0.0.1:6742', section(proxy, 'Service'))
        self.assertIn('--server-port 6742', openrgb)
        self.assertNotIn('[Install]', proxy)  # started by its socket only
        # The socket: root only, at the place the runtime connects to.
        listen = section(socket, 'Socket')
        self.assertIn('ListenStream=' + dashboard.OPENRGB_SOCKET, listen)
        self.assertIn('SocketMode=0600', listen)
        self.assertIn('SocketUser=root', listen)
        self.assertIn('DirectoryMode=0700', listen)
        self.assertIn(
            'pub const SOCKET: &str = "' + dashboard.OPENRGB_SOCKET + '";',
            (ROOT / 'runtime/src/openrgb.rs').read_text(),
        )
        self.assertIn('PathBuf::from(openrgb::SOCKET)', (ROOT / 'runtime/src/control.rs').read_text())
        # The control service waits for the socket; it shares no namespace (or /tmp) with OpenRGB.
        control = units[dashboard.CONTROL]
        self.assertIn('Wants=' + dashboard.OPENRGB + ' ' + dashboard.PROXY_SOCKET, section(control, 'Unit'))
        self.assertNotIn('JoinsNamespaceOf', control)
        for name, text in units.items():
            self.assertTrue(text.startswith(dashboard.MANAGED_UNIT), name)
        # Lighting off: none of it.
        self.assertEqual(set(dashboard.control_units(self.code, False)), {dashboard.CONTROL})

    def test_the_socket_proxy_is_looked_up_where_each_system_keeps_it(self):
        with tempfile.TemporaryDirectory() as d:
            usr, lib = Path(d) / 'usr-lib', Path(d) / 'lib'
            self.assertIsNone(dashboard.socket_proxyd((usr, lib)))
            lib.write_text('#!/bin/sh\n')
            lib.chmod(0o755)
            self.assertEqual(dashboard.socket_proxyd((usr, lib)), lib)  # a Debian that keeps /lib apart
            usr.write_text('#!/bin/sh\n')
            usr.chmod(0o755)
            self.assertEqual(dashboard.socket_proxyd((usr, lib)), usr)
            with mock.patch.object(dashboard, 'PROXYD_PLACES', (lib,)):
                self.assertIn(
                    'ExecStart=' + str(lib) + ' 127.0.0.1:6742',
                    dashboard.control_units(self.code, True)[dashboard.PROXY],
                )
        self.assertEqual(dashboard.PROXYD_PLACES[0], Path('/usr/lib/systemd/systemd-socket-proxyd'))

    def test_lighting_is_skipped_without_the_socket_proxy(self):
        definitions = dashboard.control_units(self.code, True)
        logs = []
        with (
            mock.patch.object(dashboard, 'control_group'),
            mock.patch.object(dashboard, 'socket_proxyd', return_value=None),
            mock.patch.object(dashboard, 'install_openrgb') as install,
        ):
            self.assertFalse(dashboard.prepare_control(self.code, definitions, True, logs.append))
        install.assert_not_called()
        self.assertEqual(set(definitions), {dashboard.CONTROL})
        self.assertIn(' --no-lights ', definitions[dashboard.CONTROL])
        self.assertIn('systemd-socket-proxyd', logs[0])
        # With the proxy there, lighting stays on.
        definitions = dashboard.control_units(self.code, True)
        with (
            mock.patch.object(dashboard, 'control_group'),
            mock.patch.object(dashboard, 'socket_proxyd', return_value=Path('/p')),
            mock.patch.object(dashboard, 'install_openrgb', return_value=True),
        ):
            self.assertTrue(dashboard.prepare_control(self.code, definitions, True, logs.append))
        self.assertIn(dashboard.PROXY_SOCKET, definitions)

    def test_lighting_and_control_are_stopped_together_then_started_in_order(self):
        """After an upgrade, a namespace an older release set up stays in use while any
        unit sharing it runs: so all of them stop first, then OpenRGB starts (and
        creates its private network), then its socket, then the control service."""
        calls, private = self.setup_calls(dashboard.control_units(self.code, True))
        stop = calls.index(('stop', dashboard.CONTROL, *dashboard.LIGHTING_UNITS))
        starts = [calls.index(enabled(u)) for u in (dashboard.OPENRGB, dashboard.PROXY_SOCKET, dashboard.CONTROL)]
        self.assertEqual(starts, sorted(starts))
        self.assertLess(stop, starts[0])
        self.assertFalse(
            [c for c in calls if c[0] == 'restart' and c[1] != dashboard.SERVICE], 'nothing is restarted one by one'
        )
        self.assertNotIn(enabled(dashboard.PROXY), calls)  # its socket starts it
        private.assert_called_once()
        # Lighting off: the control service alone is stopped and started again.
        calls, private = self.setup_calls(dashboard.control_units(self.code, False))
        self.assertLess(calls.index(('stop', dashboard.CONTROL)), calls.index(enabled(dashboard.CONTROL)))
        private.assert_not_called()

    def setup_calls(self, definitions):
        """The systemctl calls setup makes for these units, and the namespace check."""
        with (
            mock.patch.object(dashboard, 'systemctl') as systemctl,
            mock.patch.object(dashboard, 'choose_pin'),
            mock.patch.object(dashboard, 'check_openrgb_private') as private,
            mock.patch.object(dashboard, 'CONTROL_CONFIG', NOWHERE),
            mock.patch.object(dashboard, 'SYSTEMD_UNITS', NOWHERE),
        ):  # not this PC's own units
            dashboard.start_services(definitions, self.code, False, lambda m: None)
        return [c.args for c in systemctl.call_args_list], private

    def test_openrgb_on_the_computers_own_network_is_stopped(self):
        nets = {1: HOST_NETWORK, 42: HOST_NETWORK}
        with (
            mock.patch.object(dashboard, 'main_pid', return_value=42),
            mock.patch.object(dashboard, 'network_of', side_effect=nets.get),
            mock.patch.object(dashboard.time, 'sleep'),
            mock.patch.object(dashboard.subprocess, 'run') as run,
        ):
            logs = []
            self.assertFalse(dashboard.check_openrgb_private(logs.append))
            self.assertEqual(
                run.call_args.args[0], ['systemctl', 'stop', dashboard.PROXY_SOCKET, dashboard.PROXY, dashboard.OPENRGB]
            )
            self.assertIn('private network', logs[0])
            run.reset_mock()
            nets[42] = 'net:[2]'  # its own network: kept
            self.assertTrue(dashboard.check_openrgb_private(logs.append))
            run.assert_not_called()
        # It gets a moment: systemd moves it just after starting it.
        seen = iter([HOST_NETWORK, HOST_NETWORK, 'net:[9]', HOST_NETWORK])
        with (
            mock.patch.object(dashboard, 'main_pid', return_value=42),
            mock.patch.object(
                dashboard, 'network_of', side_effect=lambda pid: HOST_NETWORK if pid == 1 else next(seen)
            ),
        ):
            self.assertTrue(dashboard.openrgb_private(sleep=lambda _: None))
        with mock.patch.object(dashboard, 'main_pid', return_value=None):
            self.assertTrue(dashboard.openrgb_private(sleep=lambda _: None))  # not running: nothing to tell

    def test_only_the_control_service_runs_as_root_and_it_always_gives_fans_back(self):
        units = dashboard.control_units(self.code, True, data=Path('/srv/n'))
        control = units[dashboard.CONTROL]
        self.assertIn(
            'ExecStart='
            + str(self.code / 'nekoserver-runtime')
            + ' control --config '
            + str(dashboard.COLLECTOR_SETTINGS)
            + '\n',
            control,
        )
        self.assertIn('ExecStopPost=' + str(self.code / 'nekoserver-runtime') + ' control --restore\n', control)
        self.assertIn('Restart=always', control)
        self.assertIn('TimeoutStopSec=600', control)
        self.assertIn('Group=' + dashboard.CONTROL_GROUP, control)
        self.assertNotIn('ProtectKernelTunables', control)  # fan outputs live in /sys
        self.assertIn('IPAddressDeny=any', control)
        self.assertIn('Wants=' + dashboard.OPENRGB, control)
        openrgb = units[dashboard.OPENRGB]
        self.assertIn('--server-port 6742', openrgb)
        self.assertIn('IPAddressDeny=any\nIPAddressAllow=localhost', openrgb)  # never on the network
        self.assertIn('QT_QPA_PLATFORM=offscreen', openrgb)
        self.assertNotIn(dashboard.OPENRGB, dashboard.control_units(self.code, False))
        # Lighting off: the fan service is told so, and never touches lighting.
        self.assertIn(' control --no-lights --config ', dashboard.control_units(self.code, False)[dashboard.CONTROL])
        self.assertNotIn('--no-lights', control)
        # Fan control off: the service still runs (app controls), but takes no fan over.
        self.assertIn(
            ' control --no-lights --no-fans --config ',
            dashboard.control_units(self.code, False, fans=False)[dashboard.CONTROL],
        )
        self.assertNotIn('--no-fans', control)
        # App controls: closing a desktop app (CAP_KILL) and the shared backup lock, nothing more.
        self.assertIn('CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_KILL\n', control)
        self.assertIn('ReadWritePaths=' + str(dashboard.CONTROL_CONFIG) + ' -/srv/n/installer\n', control)
        self.assertIn('ProtectHome=yes', control)
        # The web server stays unprivileged and only gains the control socket's group.
        web = dashboard.units(Path('/srv/n'), self.code, Path('/etc/x.json'), 8787, 60, control=True)[dashboard.SERVICE]
        self.assertIn('DynamicUser=yes', web)
        self.assertIn('SupplementaryGroups=' + dashboard.CONTROL_GROUP, web)
        self.assertNotIn(
            'SupplementaryGroups',
            dashboard.units(Path('/srv/n'), self.code, Path('/etc/x.json'), 8787, 60)[dashboard.SERVICE],
        )
        for name, text in {**units, dashboard.SERVICE: web}.items():
            self.assertTrue(text.startswith('[Unit]\nDescription=NekoServer '), name)

    def test_switches_are_checked(self):
        cfg = {'dashboard': {'port': 8787}}
        self.assertEqual(dashboard.control_options(cfg), (True, True))
        self.assertEqual(dashboard.control_options({'dashboard': {'rgb': False}}), (True, False))
        self.assertEqual(dashboard.control_options({'dashboard': {'fan_control': False}}), (False, False))
        with self.assertRaises(RuntimeError):
            dashboard.options({'dashboard': {'fan_control': 'yes'}})

    def test_the_pin_is_asked_twice_and_only_passed_to_the_runtime(self):
        from unittest import mock

        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d)),
            mock.patch.object(dashboard.subprocess, 'run') as run,
            mock.patch('builtins.print'),
        ):
            logs = []
            run.return_value = mock.Mock(returncode=0)
            with mock.patch.object(dashboard.sys.stdin, 'isatty', return_value=False):
                self.assertFalse(dashboard.choose_pin(Path('/bin/rt'), logs.append, ask=lambda _: '1234'))
            run.assert_not_called()
            with mock.patch.object(dashboard.sys.stdin, 'isatty', return_value=True):
                # Too short (4 digits is no longer enough), differ, then good.
                answers = iter(['1234', '123456', '999999', '246813', '246813'])
                self.assertTrue(dashboard.choose_pin(Path('/bin/rt'), logs.append, ask=lambda _: next(answers)))
                self.assertEqual(run.call_args.args[0], ['/bin/rt', 'control', '--set-pin'])
                self.assertEqual(run.call_args.kwargs['input'], '246813\n')
                self.assertNotIn('246813', ' '.join(logs))
                run.reset_mock()
                # Digits from another script look numeric to Python but not to the
                # control service: asked again, never sent. A refusal by the service
                # asks again too, instead of failing the step.
                run.return_value = mock.Mock(returncode=0)
                answers = iter(['\u0661\u0662\u0663\u0664\u0665\u0666', '135790', '135790', '246813', '246813'])
                run.side_effect = [mock.Mock(returncode=1, stderr='A PIN is 6 to 8 digits\n'), mock.Mock(returncode=0)]
                self.assertTrue(dashboard.choose_pin(Path('/bin/rt'), logs.append, ask=lambda _: next(answers)))
                self.assertEqual([c.kwargs['input'] for c in run.call_args_list], ['135790\n', '246813\n'])
                run.reset_mock(side_effect=True)
                self.assertFalse(dashboard.choose_pin(Path('/bin/rt'), logs.append, ask=lambda _: ''))  # Enter skips
                run.assert_not_called()
                (Path(d) / 'pin.json').write_text('{}')
                self.assertFalse(dashboard.choose_pin(Path('/bin/rt'), logs.append, ask=lambda _: '1111'))  # kept
                run.assert_not_called()


class SwitchingOffAndBackupTests(unittest.TestCase):
    def test_setup_stops_when_fan_control_cant_be_switched_off(self):
        from unittest import mock

        failed = mock.Mock(returncode=1, stderr='simulated stop failure')
        with (
            mock.patch.object(Path, 'exists', return_value=True),
            mock.patch.object(dashboard, 'systemctl') as ctl,
            mock.patch.object(dashboard.subprocess, 'run', return_value=failed),
        ):
            with self.assertRaises(RuntimeError) as err:
                dashboard.start_services({dashboard.SERVICE: 'unit'}, Path('/unused'), False, lambda *_: None)
        self.assertIn('could not be switched off', str(err.exception))
        self.assertNotIn(('restart', dashboard.SERVICE), [c.args for c in ctl.call_args_list])

    def test_setup_stops_when_a_fan_was_not_handed_back(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'originals.json'
            journal.write_text('{"x": {"enable": "5", "pwm": "128"}}')
            stopped = mock.Mock(returncode=0, stderr='')
            inactive = mock.Mock(returncode=3)
            with (
                mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)),
                mock.patch.object(dashboard.subprocess, 'run', side_effect=[stopped, inactive]),
            ):
                with self.assertRaises(RuntimeError) as err:
                    dashboard.stop_control([dashboard.CONTROL])
            self.assertIn('did not go back', str(err.exception))

    def test_switching_off_only_lighting_keeps_fans_under_control(self):
        """RGB off, fan control on: setup stops OpenRGB only, and the fan records of the
        fans still under control are expected, not a failure."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            units = Path(d) / 'units'
            units.mkdir()
            journal = Path(d) / 'originals.json'
            journal.write_text('{"x": {"enable": "5", "pwm": "128"}}')  # a fan under control
            for name, text in dashboard.control_units(Path('/x'), True).items():
                (units / name).write_text(text)  # RGB was on before
            definitions = dashboard.control_units(Path('/x'), False)  # now RGB off
            definitions[dashboard.SERVICE] = 'unit'
            calls = []

            def run(args, **_):
                calls.append(args)
                return mock.Mock(returncode=3 if 'is-active' in args else 0, stderr='')

            with (
                mock.patch.object(dashboard, 'SYSTEMD_UNITS', units),
                mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)),
                mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d) / 'cfg'),
                mock.patch.object(dashboard, 'choose_pin'),
                mock.patch.object(dashboard, 'systemctl') as ctl,
                mock.patch.object(dashboard.subprocess, 'run', side_effect=run),
            ):
                dashboard.start_services(definitions, Path('/x'), True, lambda *_: None)
            self.assertIn(
                ['systemctl', 'disable', '--now', dashboard.PROXY_SOCKET, dashboard.PROXY, dashboard.OPENRGB], calls
            )
            self.assertFalse(any(dashboard.CONTROL in c for c in calls), 'fan control is not switched off')
            self.assertIn(('stop', dashboard.CONTROL), [c.args for c in ctl.call_args_list])
            self.assertIn(enabled(dashboard.CONTROL), [c.args for c in ctl.call_args_list])
            self.assertTrue(journal.exists())

    def test_switching_fan_control_off_finishes_only_once_every_fan_is_back(self):
        """The control service keeps running for app controls; restarted without fan
        control it hands every fan back, and setup checks that it did."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'originals.json'
            definitions = dashboard.control_units(Path('/x'), False, fans=False)
            definitions[dashboard.SERVICE] = 'unit'
            patches = (
                mock.patch.object(dashboard, 'SYSTEMD_UNITS', Path(d)),
                mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)),
                mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d) / 'cfg'),
                mock.patch.object(dashboard, 'choose_pin'),
                mock.patch.object(dashboard, 'systemctl'),
            )
            for patch in patches:
                patch.start()
                self.addCleanup(patch.stop)
            dashboard.start_services(definitions, Path('/x'), True, print, fans=False)  # every fan is back
            journal.write_text('{"x": {"enable": "5", "pwm": "128"}}')
            with mock.patch.object(dashboard.time, 'sleep'), self.assertRaisesRegex(RuntimeError, 'did not go back'):
                dashboard.start_services(definitions, Path('/x'), True, print, fans=False)
            # The service is still handing fans back as setup looks: a moment is given.
            ticks = iter(range(100))

            def hand_back(_):
                if next(ticks) == 3:
                    journal.unlink()

            journal.write_text('{"x": {"enable": "5", "pwm": "128"}}')
            with mock.patch.object(dashboard.time, 'sleep', side_effect=hand_back):
                dashboard.start_services(definitions, Path('/x'), True, print, fans=False)
            self.assertFalse(journal.exists())
            dashboard.start_services(definitions, Path('/x'), True, print, fans=True)  # its own fans

    def test_control_keeps_its_records_when_it_stops(self):
        unit = dashboard.control_units(Path('/x'), False)[dashboard.CONTROL]
        self.assertIn('RuntimeDirectoryPreserve=yes', unit)

    def test_host_snapshots_include_fan_light_and_dashboard_settings(self):
        from modules import host_backup

        for path in ('/etc/nekoserver-control', '/etc/nekoserver-dashboard'):
            self.assertIn(path, host_backup.HOST_DIRS)
        # OpenRGB's settings files, never its log folder (written all the time).
        self.assertIn('/var/lib/nekoserver-openrgb/OpenRGB/OpenRGB.json', host_backup.HOST_FILES)
        self.assertFalse(any(p.startswith('/var/lib/nekoserver-openrgb') for p in host_backup.HOST_DIRS))
        self.assertIn(str(dashboard.I2C_MODULES), host_backup.HOST_FILES)
        self.assertIn(str(dashboard.CONTROL_CONFIG), host_backup.HOST_DIRS)

    def test_an_installed_host_snapshot_list_is_refreshed(self):
        from unittest import mock
        from modules import host_backup

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'host.json'
            logs = []
            with mock.patch.object(host_backup, 'guard', return_value={'host_backup': {'sources': ['new']}}):
                self.assertFalse(host_backup.refresh({}, logs.append, target))  # not installed
                target.write_text('{"host_backup": {"sources": ["old"]}}')
                self.assertTrue(host_backup.refresh({}, logs.append, target))
                self.assertIn('new', target.read_text())
                self.assertFalse(host_backup.refresh({}, logs.append, target))  # already current
        import installer

        self.assertIn('host_backup', installer.REFRESHED)


class HostBackupUserTests(unittest.TestCase):
    def test_auto_keeps_the_account_chosen_the_first_time(self):
        from unittest import mock
        from modules import host_backup

        first, admin = mock.Mock(pw_name='nekofan', pw_uid=1000), mock.Mock(pw_name='admin2', pw_uid=1001)
        users = {'nekofan': first, 'admin2': admin}
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(host_backup.pwd, 'getpwnam', side_effect=users.__getitem__),
            mock.patch.object(host_backup.pwd, 'getpwuid', side_effect={1000: first, 1001: admin}.__getitem__),
            mock.patch.dict(host_backup.os.environ, {'SUDO_USER': 'admin2'}),
        ):
            target = Path(d) / 'host.json'
            self.assertIsNone(host_backup.previous_user(target))
            # Not installed yet: the person running setup.
            self.assertIs(host_backup.choose_user({}, {'user': 'auto'}, None), admin)
            # Installed for nekofan: another administrator running setup changes nothing.
            target.write_text('{"host_backup": {"user": "nekofan"}}')
            self.assertIs(host_backup.choose_user({}, {'user': 'auto'}, host_backup.previous_user(target)), first)
            # Lists from before 0.27.4 only kept the Steam account's number.
            target.write_text('{"host_backup": {"steam_uid": 1000}}')
            self.assertIs(host_backup.previous_user(target), first)
            # A user named in the settings always wins.
            self.assertIs(host_backup.choose_user({}, {'user': 'admin2'}, first), admin)


class RemoteAccessLaterTests(unittest.TestCase):
    def test_remote_access_set_up_later_reaches_the_web_service(self):
        from unittest import mock

        unit = dashboard.units(
            Path('/srv/n'), Path('/usr/local/lib/nekoserver-dashboard/v1'), Path('/etc/x.json'), 8787, 60
        )[dashboard.SERVICE]
        self.assertNotIn('--public-host', unit)  # installed before Tailscale was connected
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dashboard, 'systemctl') as ctl:
            path = Path(d) / dashboard.SERVICE
            path.write_text(unit)
            logs = []
            with mock.patch.object(dashboard, 'tailnet_name', return_value='neko.tail1234.ts.net'):
                self.assertTrue(dashboard.refresh_public_host({}, logs.append, path))
                self.assertIn('--port 8787 --public-host neko.tail1234.ts.net\n', path.read_text())
                self.assertIn(('restart', dashboard.SERVICE), [c.args for c in ctl.call_args_list])
                ctl.reset_mock()
                self.assertFalse(dashboard.refresh_public_host({}, logs.append, path))  # already right
                ctl.assert_not_called()
            # A new Tailscale name replaces the old one; remote access off removes it.
            with mock.patch.object(dashboard, 'tailnet_name', return_value='neko2.tail1234.ts.net'):
                self.assertTrue(dashboard.refresh_public_host({}, logs.append, path))
            self.assertEqual(path.read_text().count('--public-host'), 1)
            self.assertIn('neko2.tail1234.ts.net', path.read_text())
            with mock.patch.object(dashboard, 'tailnet_name', return_value=None):
                self.assertTrue(dashboard.refresh_public_host({}, logs.append, path))
            self.assertEqual(path.read_text(), unit)
            # A unit someone else wrote is left alone.
            path.write_text('[Service]\nExecStart=/x serve --port 1\n')
            with mock.patch.object(dashboard, 'tailnet_name', return_value='neko.tail1234.ts.net'):
                self.assertFalse(dashboard.refresh_public_host({}, logs.append, path))


class TailscaleLookupTests(unittest.TestCase):
    def test_a_passing_tailscale_failure_keeps_the_phone_address(self):
        from unittest import mock

        unit = dashboard.units(
            Path('/srv/n'),
            Path('/usr/local/lib/nekoserver-dashboard/v1'),
            Path('/etc/x.json'),
            8787,
            60,
            public='neko.tail1234.ts.net',
        )[dashboard.SERVICE]
        self.assertEqual(dashboard.public_host_of(unit), 'neko.tail1234.ts.net')
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dashboard, 'systemctl') as ctl:
            path = Path(d) / dashboard.SERVICE
            path.write_text(unit)
            import gateway_tools

            with mock.patch.object(gateway_tools, 'tailnode', side_effect=RuntimeError('Authenticate Tailscale')):
                self.assertFalse(dashboard.refresh_public_host({}, lambda *_: None, path))
                self.assertEqual(path.read_text(), unit, 'kept while Tailscale is away')
                ctl.assert_not_called()
                # Never set up: nothing to keep.
                self.assertIsNone(dashboard.tailnet_name({}))
            # Remote access switched off on purpose: removed.
            self.assertTrue(dashboard.refresh_public_host({'dashboard': {'remote': False}}, lambda *_: None, path))
            self.assertIsNone(dashboard.public_host_of(path.read_text()))

    def test_acceptance_expects_the_account_setup_keeps(self):
        from unittest import mock
        from modules import host_backup

        alice = mock.Mock(pw_name='alice')
        with (
            mock.patch.object(host_backup, 'previous_user', return_value=alice),
            mock.patch.object(host_backup, 'guard', return_value='for alice') as guard,
        ):
            self.assertEqual(host_backup.expected({'x': 1}), 'for alice')
        guard.assert_called_once_with({'x': 1}, alice)
        source = (Path(__file__).resolve().parents[1] / 'native_runtime.py').read_text()
        self.assertIn('host_backup.expected(config)', source)
        self.assertNotIn('host_backup.guard(config)', source)


class SharedNamesTests(unittest.TestCase):
    """Names the installer (Python) and the runtime (Rust) must agree on."""

    runtime = Path(__file__).resolve().parents[1] / 'runtime/src'

    def rust_const(self, file, name):
        text = (self.runtime / file).read_text()
        match = re.search(r'const ' + name + r'[^=]*= (.+?);', text)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_fan_recovery_records_are_found_where_the_runtime_writes_them(self):
        run_dir = self.rust_const('control.rs', 'RUN_DIR').strip('"')
        journals = re.findall(r'"([^"]+)"', self.rust_const('control.rs', 'JOURNALS'))
        self.assertEqual([str(p) for p in dashboard.CONTROL_JOURNALS], [run_dir + '/' + j for j in journals])
        self.assertEqual(self.rust_const('control.rs', 'CONFIG_DIR').strip('"'), str(dashboard.CONTROL_CONFIG))

    def test_the_phone_address_port_matches(self):
        import gateway_tools

        self.assertEqual(int(self.rust_const('serve.rs', 'PUBLIC_PORT')), gateway_tools.DASHBOARD_PORT)


class BackupWordingTests(unittest.TestCase):
    def test_setup_says_what_happens_to_older_app_snapshots(self):
        from modules import app_backup

        self.assertIn('newest 7', app_backup.retention(app_backup.keep_snapshots({})))
        self.assertIn('deleted', app_backup.retention(14))
        self.assertIn('none are deleted', app_backup.retention(0))
        self.assertNotIn('No automatic backup deletion', (Path(app_backup.__file__)).read_text())


class PreservedBundleTest(unittest.TestCase):
    """The installer copy kept on the SSD must be able to set the dashboard up again."""

    def test_preserved_bundle_reinstalls_the_dashboard(self):
        from unittest import mock

        from modules import recovery

        if not (ROOT / 'runtime/build.json').is_file():
            self.skipTest('the runtime is not built here (install.sh or runtime/build.py builds it)')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(recovery.os.path, 'ismount', return_value=True):
            dest = recovery.preserve(Path(tmp), ROOT, {'data_mount': tmp})
            payloads = dashboard.asset_payloads(dest, Path('/code'))
            self.assertEqual(sorted(path.name for path in payloads), sorted(dashboard.ASSETS))
            for path, data in payloads.items():
                self.assertEqual(data, (ROOT / 'dashboard' / path.name).read_bytes())

    def test_bundle_keeps_every_release_file(self):
        from modules import recovery

        kept = {path.relative_to(ROOT) for path in recovery.bundle_files(ROOT)}
        # The bundle keeps the server's own settings instead of config.json; ruff.toml only lays the code out.
        skipped = {Path('config.json'), Path('ruff.toml')}
        release = {path.relative_to(ROOT) for path in ROOT.rglob('*') if path.is_file()}
        # Not part of a release: tests, build output and tool caches.
        release = {
            rel
            for rel in release
            if rel.parts[0] != 'tests'
            and rel.parts[:2] != ('runtime', 'target')
            and not any(part == '__pycache__' or part.startswith('.') for part in rel.parts)
        }
        self.assertEqual(sorted(map(str, release - kept - skipped)), [])


class RuntimeCopyTests(unittest.TestCase):
    """The dashboard's own runtime copy follows the rule of every other deployed copy
    (native_runtime.deploy): one NekoServer put there, its build record beside it naming
    exactly that build, is replaced when the runtime was built again from other sources;
    one without such a record, and any other changed file, stays and stops the step."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.code = Path(self.tmp.name) / 'v0.38.1'
        (self.code / 'dashboard').mkdir(parents=True)
        self.runtime = self.code / dashboard.native_runtime.NAME
        self.record = Path(str(self.runtime) + dashboard.native_runtime.RECORD_SUFFIX)
        self.page = self.code / 'dashboard/index.html'
        self.payloads = {self.runtime: b'new build', self.record: self.record_for(b'new build'), self.page: b'<html>'}

    @staticmethod
    def record_for(blob):
        return json.dumps({'version': '0.38.1', 'binary_sha256': hashlib.sha256(blob).hexdigest()}).encode()

    def existing(self, runtime, record=None, page=b'<html>'):
        self.runtime.write_bytes(runtime)
        self.runtime.chmod(0o755)
        self.page.write_bytes(page)
        if record is not None:
            self.record.write_bytes(record)

    def test_a_rebuilt_runtime_replaces_the_copy_nekoserver_made(self):
        self.existing(b'old build', self.record_for(b'old build'))
        dashboard.check_payloads(self.payloads)
        dashboard.write_files((self.code,), self.payloads, {})
        self.assertEqual(self.runtime.read_bytes(), b'new build')
        self.assertEqual(self.record.read_bytes(), self.record_for(b'new build'))
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o755)

    def test_a_copy_without_a_record_is_never_overwritten(self):
        self.existing(b'someone else wrote this')
        with self.assertRaisesRegex(RuntimeError, 'differs from the one in this release'):
            dashboard.check_payloads(self.payloads)
        dashboard.write_files((self.code,), {self.runtime: b'new build'}, {})
        self.assertEqual(self.runtime.read_bytes(), b'someone else wrote this')

    def test_a_copy_its_record_does_not_describe_is_never_overwritten(self):
        self.existing(b'their own build', self.record_for(b'old build'))
        with self.assertRaisesRegex(RuntimeError, 'differs from the one in this release'):
            dashboard.check_payloads(self.payloads)

    def test_a_stale_record_beside_this_releases_runtime_is_refreshed(self):
        self.existing(b'new build', b'not even json')
        dashboard.check_payloads(self.payloads)
        dashboard.write_files((self.code,), self.payloads, {})
        self.assertEqual(self.record.read_bytes(), self.record_for(b'new build'))

    def test_a_changed_page_file_still_stops_the_step(self):
        self.existing(b'new build', self.record_for(b'new build'), page=b'<html>edited')
        with self.assertRaisesRegex(RuntimeError, 'index.html differs from the one in this release'):
            dashboard.check_payloads(self.payloads)

    def test_the_record_is_part_of_what_the_step_installs(self):
        source = inspect.getsource(dashboard.run)
        self.assertIn('native_runtime.RECORD_SUFFIX', source)


class DashboardPortTests(unittest.TestCase):
    """The dashboard's port: a taken one is replaced by a free spare and kept; the page
    must answer before the step counts as done."""

    def test_a_taken_port_gets_a_spare_and_the_running_service_keeps_its_own(self):
        from modules import dashboard

        saved = []
        ctx = mock.Mock(
            config={'dashboard': {'port': 8787}, 'media': {}},
            log=lambda m: None,
            persist_config=lambda: saved.append(1),
        )
        with (
            mock.patch.object(dashboard, 'port_free', side_effect=lambda port: port not in (8787, 8788)),
            mock.patch.object(dashboard, 'installed_unit', return_value=None),
        ):
            self.assertEqual(dashboard.choose_port(ctx, 8787, running=None), 8789)
            self.assertEqual((ctx.config['dashboard']['port'], saved), (8789, [1]))
            # A rerun: the service itself holds 8789 now.
            self.assertEqual(dashboard.choose_port(ctx, 8789, running=8789), 8789)
        self.assertEqual(
            dashboard.own_port(
                '[Service]\nExecStart=/x/nekoserver-runtime serve --assets /a --status /s --port 8789\n'
            ),
            8789,
        )
        self.assertIsNone(dashboard.own_port(None))

    def test_a_page_that_never_answers_fails_the_step_with_the_log_command(self):
        from modules import dashboard

        with (
            mock.patch.object(dashboard, 'answers', return_value=False),
            mock.patch.object(dashboard, 'start_services'),
            mock.patch.object(dashboard, 'write_files'),
            mock.patch.object(dashboard, 'prepare_control', return_value=False),
            mock.patch.object(dashboard, 'install_smart'),
            mock.patch.object(dashboard, 'check_existing'),
            mock.patch.object(dashboard, 'choose_port', side_effect=lambda ctx, port: port),
            mock.patch.object(dashboard, 'tailnet_name', return_value=None),
            mock.patch.object(dashboard, 'atomic_json'),
            mock.patch.object(dashboard, 'collector_settings', return_value={}),
            mock.patch.object(dashboard.native_runtime, 'payload', return_value=b'x'),
            mock.patch.object(dashboard, 'record_payload', return_value=b'{}'),
            mock.patch.object(dashboard, 'asset_payloads', return_value={}),
            mock.patch.object(dashboard, 'mount_path', return_value=Path('/srv/nekoserver')),
            mock.patch.object(dashboard.importlib.util, 'spec_from_file_location') as spec,
            mock.patch.object(dashboard.os, 'geteuid', return_value=0),
            mock.patch.object(dashboard.shutil, 'which', return_value='/usr/bin/systemctl'),
            mock.patch.object(dashboard.safe_path.__globals__['Path'], 'mkdir'),
        ):
            spec.return_value.loader.exec_module = lambda module: setattr(module, 'verify', lambda cfg: None)
            ctx = mock.Mock(
                config={'dashboard': {'port': 8787}, 'data_mount': '/srv/nekoserver', 'media': {}},
                apply=True,
                log=lambda m: None,
            )
            with self.assertRaisesRegex(
                RuntimeError, 'did not come up on port 8787.*journalctl -u nekoserver-dashboard.service'
            ):
                dashboard.run(ctx)


class TvChoiceTests(unittest.TestCase):
    """NP-2: the collector's own settings carry the TV choice, so a set-up TV keeps its
    Steam Big Picture card (the collector reads these settings, not my-config.json)."""

    def test_the_collector_settings_carry_the_tv_choice(self):
        from modules import dashboard

        cfg = {
            'data_mount': '/srv/n',
            'media_mount': '/mnt/m',
            'external_backup_mount': '/mnt/u',
            'storage': {},
            'media': {'services': []},
        }
        self.assertEqual(dashboard.collector_settings(dict(cfg, tv={'wanted': True}), 1800)['tv'], {'wanted': True})
        self.assertEqual(dashboard.collector_settings(dict(cfg, tv={'wanted': False}), 1800)['tv'], {'wanted': False})
        self.assertEqual(dashboard.collector_settings(cfg, 1800)['tv'], {'wanted': None})


if __name__ == '__main__':
    unittest.main()
