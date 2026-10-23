"""The dashboard: its files, the server that serves them, and how it is installed."""
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


def served_files():
    block = SERVE[SERVE.index('const ASSETS'):SERVE.index('];', SERVE.index('const ASSETS'))]
    return {m.group(1) for m in re.finditer(r'\(\s*"/[^"]*",\s*"([^"]+)"', block)}


def server_codes():
    block = SERVE[SERVE.index('pub const ERRORS'):SERVE.index('];', SERVE.index('pub const ERRORS'))]
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
        rows = re.findall(r"^\s+(E\d\d): \['([^']+)', '([^']+)'\]", APP, re.M)
        self.assertEqual({r[0] for r in rows}, page_codes)
        for code, title, fix in rows:
            self.assertLessEqual(len(title.split()), 7, code)  # short enough to read at a glance
            self.assertLessEqual(len(fix), 110, code)

    def test_closing_a_desktop_app_is_confirmed_like_force_close(self):
        # A plain "please quit" can lose unsaved work too: no menu entry closes without asking.
        menu = APP[APP.index('function desktopMenu'):APP.index('function processMenu')]
        self.assertIn('run: () => confirmClose(r, label)', menu)
        self.assertNotIn('closeDesktop(', menu)
        self.assertNotIn('like closing its window', APP)
        close = APP[APP.index('function confirmClose'):APP.index('function confirmForce')]
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
            {'data_mount': '/srv', 'media_mount': '/m', 'external_backup_mount': '/u', 'storage': {}, 'media': {'services': []}},
            1800, 300)
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
            body = json.dumps({'format': 'neko-dashboard-v1', 'generated_at': now - age, 'collect_interval': interval}).encode()
            response = mock.MagicMock(status=200)
            response.read = io.BytesIO(body).read
            response.__enter__.return_value = response
            return response
        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 300)):
            self.assertTrue(acceptance.dashboard_check(8787, now))
        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 60)):
            self.assertFalse(acceptance.dashboard_check(8787, now))


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
        longest = int(re.search(r'LONGEST_CHANGE_SECS: u64 = 30 \+ MOST_PARTS \* \(STOP_SECS \+ START_SECS\)', apps)
                      and 30 + 2 * (120 + 120))
        for name, value in (('STOP_SECS', 120), ('START_SECS', 120), ('MOST_PARTS', 2)):
            self.assertIn(f'const {name}: u64 = {value};', apps)
        self.assertIn('Duration::from_secs(apps::LONGEST_CHANGE_SECS)', (ROOT / 'runtime/src/control.rs').read_text())
        self.assertGreaterEqual(dashboard.CONTROL_STOP_SECONDS, longest + 60)
        self.assertGreater(dashboard.CONTROL_WAIT, dashboard.CONTROL_STOP_SECONDS)
        from unittest import mock
        with mock.patch.object(dashboard, 'systemctl') as systemctl, mock.patch.object(dashboard, 'choose_pin'), \
                mock.patch.object(dashboard, 'stop_control'), mock.patch.object(dashboard, 'CONTROL_CONFIG', Path('/nonexistent')):
            try:
                dashboard.start_services({dashboard.CONTROL: ''}, self.code, False, lambda m: None)
            except (OSError, RuntimeError):
                pass
        restarts = {c.args[1]: c.kwargs.get('timeout') for c in systemctl.call_args_list if c.args[0] == 'restart'}
        self.assertEqual(restarts[dashboard.CONTROL], dashboard.CONTROL_WAIT)

    def test_only_the_control_service_runs_as_root_and_it_always_gives_fans_back(self):
        units = dashboard.control_units(self.code, True, data=Path('/srv/n'))
        control = units[dashboard.CONTROL]
        self.assertIn('ExecStart=' + str(self.code / 'nekoserver-runtime') + ' control --config '
                      + str(dashboard.COLLECTOR_SETTINGS) + '\n', control)
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
        self.assertIn(' control --no-lights --no-fans --config ', dashboard.control_units(self.code, False, fans=False)[dashboard.CONTROL])
        self.assertNotIn('--no-fans', control)
        # App controls: closing a desktop app (CAP_KILL) and the shared backup lock, nothing more.
        self.assertIn('CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_KILL\n', control)
        self.assertIn('ReadWritePaths=' + str(dashboard.CONTROL_CONFIG) + ' -/srv/n/installer\n', control)
        self.assertIn('ProtectHome=yes', control)
        # The web server stays unprivileged and only gains the control socket's group.
        web = dashboard.units(Path('/srv/n'), self.code, Path('/etc/x.json'), 8787, 60, control=True)[dashboard.SERVICE]
        self.assertIn('DynamicUser=yes', web)
        self.assertIn('SupplementaryGroups=' + dashboard.CONTROL_GROUP, web)
        self.assertNotIn('SupplementaryGroups', dashboard.units(Path('/srv/n'), self.code, Path('/etc/x.json'), 8787, 60)[dashboard.SERVICE])
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
        with tempfile.TemporaryDirectory() as d, mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d)), \
                mock.patch.object(dashboard.subprocess, 'run') as run, mock.patch('builtins.print'):
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
        with mock.patch.object(Path, 'exists', return_value=True), mock.patch.object(dashboard, 'systemctl') as ctl, \
                mock.patch.object(dashboard.subprocess, 'run', return_value=failed):
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
            with mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)), \
                    mock.patch.object(dashboard.subprocess, 'run', side_effect=[stopped, inactive]):
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
            with mock.patch.object(dashboard, 'SYSTEMD_UNITS', units), \
                    mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)), \
                    mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d) / 'cfg'), \
                    mock.patch.object(dashboard, 'choose_pin'), \
                    mock.patch.object(dashboard, 'systemctl') as ctl, \
                    mock.patch.object(dashboard.subprocess, 'run', side_effect=run):
                dashboard.start_services(definitions, Path('/x'), True, lambda *_: None)
            self.assertIn(['systemctl', 'disable', '--now', dashboard.OPENRGB], calls)
            self.assertFalse(any(dashboard.CONTROL in c for c in calls), 'fan control is not stopped')
            self.assertIn(('restart', dashboard.CONTROL), [c.args for c in ctl.call_args_list])
            self.assertTrue(journal.exists())

    def test_switching_fan_control_off_finishes_only_once_every_fan_is_back(self):
        """The control service keeps running for app controls; restarted without fan
        control it hands every fan back, and setup checks that it did."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'originals.json'
            definitions = dashboard.control_units(Path('/x'), False, fans=False)
            definitions[dashboard.SERVICE] = 'unit'
            patches = (mock.patch.object(dashboard, 'SYSTEMD_UNITS', Path(d)), mock.patch.object(dashboard, 'CONTROL_JOURNALS', (journal,)),
                       mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(d) / 'cfg'), mock.patch.object(dashboard, 'choose_pin'),
                       mock.patch.object(dashboard, 'systemctl'))
            for patch in patches:
                patch.start()
                self.addCleanup(patch.stop)
            dashboard.start_services(definitions, Path('/x'), True, print, fans=False)   # every fan is back
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
            dashboard.start_services(definitions, Path('/x'), True, print, fans=True)    # its own fans

    def test_control_keeps_its_records_when_it_stops(self):
        unit = dashboard.control_units(Path('/x'), False)[dashboard.CONTROL]
        self.assertIn('RuntimeDirectoryPreserve=yes', unit)

    def test_host_snapshots_include_fan_light_and_dashboard_settings(self):
        from modules import host_backup
        for path in ('/etc/nekoserver-control', '/etc/nekoserver-dashboard', '/var/lib/nekoserver-openrgb'):
            self.assertIn(path, host_backup.HOST_DIRS)
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
        with tempfile.TemporaryDirectory() as d, mock.patch.object(host_backup.pwd, 'getpwnam', side_effect=users.__getitem__), \
                mock.patch.object(host_backup.pwd, 'getpwuid', side_effect={1000: first, 1001: admin}.__getitem__), \
                mock.patch.dict(host_backup.os.environ, {'SUDO_USER': 'admin2'}):
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
        unit = dashboard.units(Path('/srv/n'), Path('/usr/local/lib/nekoserver-dashboard/v1'), Path('/etc/x.json'), 8787, 60)[dashboard.SERVICE]
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
        unit = dashboard.units(Path('/srv/n'), Path('/usr/local/lib/nekoserver-dashboard/v1'), Path('/etc/x.json'), 8787, 60,
                               public='neko.tail1234.ts.net')[dashboard.SERVICE]
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
        with mock.patch.object(host_backup, 'previous_user', return_value=alice), \
                mock.patch.object(host_backup, 'guard', return_value='for alice') as guard:
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
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(recovery.os.path, 'ismount', return_value=True):
            dest = recovery.preserve(Path(tmp), ROOT, {'data_mount': tmp})
            payloads = dashboard.asset_payloads(dest, Path('/code'))
            self.assertEqual(sorted(path.name for path in payloads), sorted(dashboard.ASSETS))
            for path, data in payloads.items():
                self.assertEqual(data, (ROOT / 'dashboard' / path.name).read_bytes())

    def test_bundle_keeps_every_release_file(self):
        from modules import recovery
        kept = {path.relative_to(ROOT) for path in recovery.bundle_files(ROOT)}
        skipped = {Path('config.json')}  # the bundle keeps the server's own settings instead
        release = {path.relative_to(ROOT) for path in ROOT.rglob('*') if path.is_file()}
        # Not part of a release: tests, build output and tool caches.
        release = {rel for rel in release if rel.parts[0] != 'tests' and rel.parts[:2] != ('runtime', 'target')
                   and not any(part == '__pycache__' or part.startswith('.') for part in rel.parts)}
        self.assertEqual(sorted(map(str, release - kept - skipped)), [])


class DashboardPortTests(unittest.TestCase):
    """The dashboard's port: a taken one is replaced by a free spare and kept; the page
    must answer before the step counts as done."""
    def test_a_taken_port_gets_a_spare_and_the_running_service_keeps_its_own(self):
        from modules import dashboard
        saved = []
        ctx = mock.Mock(config={'dashboard': {'port': 8787}, 'media': {}}, log=lambda m: None, persist_config=lambda: saved.append(1))
        with mock.patch.object(dashboard, 'port_free', side_effect=lambda port: port not in (8787, 8788)), \
                mock.patch.object(dashboard, 'installed_unit', return_value=None):
            self.assertEqual(dashboard.choose_port(ctx, 8787, running=None), 8789)
            self.assertEqual((ctx.config['dashboard']['port'], saved), (8789, [1]))
            # A rerun: the service itself holds 8789 now.
            self.assertEqual(dashboard.choose_port(ctx, 8789, running=8789), 8789)
        self.assertEqual(dashboard.own_port('[Service]\nExecStart=/x/nekoserver-runtime serve --assets /a --status /s --port 8789\n'), 8789)
        self.assertIsNone(dashboard.own_port(None))

    def test_a_page_that_never_answers_fails_the_step_with_the_log_command(self):
        from modules import dashboard
        with mock.patch.object(dashboard, 'answers', return_value=False), \
                mock.patch.object(dashboard, 'start_services'), \
                mock.patch.object(dashboard, 'write_files'), \
                mock.patch.object(dashboard, 'prepare_control', return_value=False), \
                mock.patch.object(dashboard, 'install_smart'), \
                mock.patch.object(dashboard, 'check_existing'), \
                mock.patch.object(dashboard, 'choose_port', side_effect=lambda ctx, port: port), \
                mock.patch.object(dashboard, 'tailnet_name', return_value=None), \
                mock.patch.object(dashboard, 'atomic_json'), \
                mock.patch.object(dashboard, 'collector_settings', return_value={}), \
                mock.patch.object(dashboard.native_runtime, 'payload', return_value=b'x'), \
                mock.patch.object(dashboard, 'asset_payloads', return_value={}), \
                mock.patch.object(dashboard, 'mount_path', return_value=Path('/srv/nekoserver')), \
                mock.patch.object(dashboard.importlib.util, 'spec_from_file_location') as spec, \
                mock.patch.object(dashboard.os, 'geteuid', return_value=0), \
                mock.patch.object(dashboard.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(dashboard.safe_path.__globals__['Path'], 'mkdir'):
            spec.return_value.loader.exec_module = lambda module: setattr(module, 'verify', lambda cfg: None)
            ctx = mock.Mock(config={'dashboard': {'port': 8787}, 'data_mount': '/srv/nekoserver', 'media': {}}, apply=True, log=lambda m: None)
            with self.assertRaisesRegex(RuntimeError, 'did not come up on port 8787.*journalctl -u nekoserver-dashboard.service'):
                dashboard.run(ctx)


if __name__ == '__main__':
    unittest.main()
