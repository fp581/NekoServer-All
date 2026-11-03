"""0.42.0: app updates leave the monthly run. A daily check finds new versions, the weekly
update night installs each app's version once it has settled (one app at a time, a
snapshot of it first, checks after, undone on failure), "Update now" on the dashboard
goes through the same steps, and the monthly run is a check of every backup copy."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_setup  # noqa: E402
from modules import maintenance  # noqa: E402

BIN, GUARD = Path('/srv/x/nekoserver-runtime'), Path('/etc/g.json')


def config(**maintenance_options):
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg['timezone'] = 'UTC'
    cfg['maintenance'].update(maintenance_options)
    return cfg


def tool():
    spec = importlib.util.spec_from_file_location('account_setup_tool', ROOT / 'account-setup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScheduleTests(unittest.TestCase):
    def test_the_update_night_is_weekly_and_never_the_monthly_checks_night(self):
        units = maintenance.definitions(config(), BIN, GUARD)
        timer = units[maintenance.UPDATES_TIMER]
        self.assertIn('OnCalendar=Wed *-*-02..31 02:30:00 UTC\n', timer)
        self.assertIn('Persistent=false', timer)  # no daytime catch-up after a night switched off
        service = units['nekoserver-updates.service']
        self.assertIn(f'ExecStart={BIN} update-apps --config {GUARD}\n', service)
        # The same journal and resume service finish or undo a run cut off.
        self.assertIn(f'ExecStopPost={BIN} maintenance-resume --config {GUARD}\n', service)
        # Another day, by choice.
        timer = maintenance.definitions(config(update_day='Sat'), BIN, GUARD)[maintenance.UPDATES_TIMER]
        self.assertIn('OnCalendar=Sat *-*-02..31 02:30:00 UTC\n', timer)
        # The monthly run is still on the 1st.
        self.assertIn('OnCalendar=*-*-01 02:30:00 UTC\n', units['nekoserver-maintenance.timer'])

    def test_the_daily_check_runs_while_the_apps_run_and_update_now_uses_the_same_steps(self):
        units = maintenance.definitions(config(), BIN, GUARD)
        check = units['nekoserver-update-check.service']
        self.assertIn(f'ExecStart={BIN} update-check --config {GUARD}\n', check)
        self.assertNotIn('ExecStopPost', check)  # it stops nothing, so nothing to finish
        self.assertIn('Persistent=true', units[maintenance.CHECK_TIMER])
        now = units['nekoserver-update-now@.service']
        self.assertIn(f'ExecStart={BIN} update-apps --config {GUARD} --app %i\n', now)
        self.assertIn(f'ExecStopPost={BIN} maintenance-resume --config {GUARD}\n', now)
        # The dashboard button starts that service through the control service: a background
        # job, so closing the page stops nothing.
        control = (ROOT / 'runtime/src/control.rs').read_text()
        self.assertIn('format!("nekoserver-update-now@{app}:{sig}.service")', control)
        self.assertIn('"systemctl", "start", "--no-block"', control)

    def test_settling_days_and_the_update_day_are_checked(self):
        self.assertEqual(maintenance.options(config(settle_days=3))['settle_days'], 3)
        for bad in (0, 31, '7', True):
            settings = config(settle_days=bad)
            with self.subTest(settle_days=bad), self.assertRaisesRegex(RuntimeError, 'settle_days'):
                maintenance.options(settings)
        settings = config(update_day='Wednesday')
        with self.assertRaisesRegex(RuntimeError, 'update_day'):
            maintenance.options(settings)


class SignInTests(unittest.TestCase):
    """account-setup.py signin APP: the weekly update's check that an app still takes its
    saved login. Read-only: it never makes or resets an account."""

    def setUp(self):
        self.tool = tool()
        self.said = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / 'config.json'
        self.settings.write_text(json.dumps({**config(), 'accounts': {'enabled': True}}))
        for target, value in (
            ('settings_path', lambda p: Path(p)),
            ('verify_storage', lambda *a, **k: None),
        ):
            patcher = mock.patch.object(self.tool, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        root = mock.patch.object(self.tool.os, 'geteuid', return_value=0)
        root.start()
        self.addCleanup(root.stop)

    def signin(self, app='jellyfin'):
        return self.tool.signin(self.settings, app, self.said.append)

    def test_the_answers_the_update_reads(self):
        with mock.patch.object(account_setup, 'signs_in', return_value=True):
            self.assertEqual(self.signin(), self.tool.SIGNED_IN)
        with mock.patch.object(account_setup, 'signs_in', return_value=None):
            self.assertEqual(self.signin(), self.tool.NO_LOGIN)
        with mock.patch.object(account_setup, 'signs_in', side_effect=RuntimeError('the app did not accept it')):
            self.assertEqual(self.signin(), self.tool.REFUSED)
        self.assertIn('jellyfin: the saved login was not accepted (the app did not accept it)', self.said)
        self.assertEqual(self.signin('not-an-app'), self.tool.NO_LOGIN)
        # The exit codes are the ones the runtime reads (maintenance.rs signs_in).
        source = (ROOT / 'runtime/src/maintenance.rs').read_text()
        self.assertIn('0 => Some(true),', source)
        self.assertIn('1 => {', source)

    def test_it_never_makes_or_resets_an_account(self):
        entry = {'username': 'admin', 'password': 'pw', 'allow_create': True, 'state': 'verified'}
        seen = []

        def jellyfin(client, given):
            seen.append(given['allow_create'])

        with (
            mock.patch.object(account_setup.store, 'get', return_value=entry),
            mock.patch.object(account_setup, 'verify_project'),
            mock.patch.object(account_setup, 'wait'),
            mock.patch.object(account_setup, 'jellyfin', jellyfin),
            mock.patch.object(account_setup.store, 'update') as update,
        ):
            self.assertTrue(account_setup.signs_in({}, 'jellyfin', client_factory=lambda app, port=None: mock.Mock()))
        self.assertEqual(seen, [False])
        update.assert_not_called()  # the saved logins stay as they are
        with mock.patch.object(account_setup.store, 'get', return_value=None):
            self.assertIsNone(account_setup.signs_in({}, 'jellyfin', client_factory=lambda app, port=None: mock.Mock()))

    def test_a_held_back_immich_or_nextcloud_is_signed_in_on_its_own_port(self):
        def container(project, service, source, inner, asked, given):
            return json.dumps(
                [
                    {
                        'State': {'Running': True},
                        'Config': {
                            'Labels': {
                                'com.docker.compose.project': 'neko-' + project,
                                'com.docker.compose.service': service,
                            }
                        },
                        'Mounts': [{'Type': 'bind', 'Source': source}],
                        'HostConfig': {'PortBindings': {inner: [{'HostIp': '127.0.0.1', 'HostPort': asked}]}},
                        'NetworkSettings': {'Ports': {inner: [{'HostIp': '127.0.0.1', 'HostPort': given}]}},
                    }
                ]
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'compose/immich').mkdir(parents=True)
            (root / 'compose/immich/compose.yaml').write_text('services: {}\n')
            config = {'data_mount': str(root)}
            inner = f'{account_setup.store.APPS["immich"][1]}/tcp'
            fixed = str(account_setup.store.APPS['immich'][1])

            def check(asked, given, quarantined=True, project='immich'):
                answers = iter(
                    [
                        'a' * 12,
                        container(project, 'immich-server', str(root / 'photos/immich-library'), inner, asked, given),
                    ]
                )
                with mock.patch.object(account_setup, 'docker', lambda argv: next(answers)):
                    return account_setup.verify_project(config, 'immich', quarantined=quarantined)

            self.assertEqual(check(fixed, fixed, quarantined=False), int(fixed))  # as usual
            self.assertEqual(check('0', '49153'), 49153)  # held back during the update
            for asked, given, quarantined in (
                ('0', '49153', False),  # only the update's login check accepts it
                ('0', '80', True),  # never a low port
                ('0', 'x', True),
                ('8080', '8080', True),  # another fixed port: not the app's
            ):
                with self.assertRaises(RuntimeError):
                    check(asked, given, quarantined)
            # Identity checks stay: another project's container on that port is refused.
            with self.assertRaises(RuntimeError):
                check('0', '49153', project='other')
        # The login check signs in on the port it proved.
        ports = []
        with (
            mock.patch.object(account_setup.store, 'get', return_value={'username': 'a', 'password': 'p'}),
            mock.patch.object(account_setup, 'verify_project', return_value=49153) as proved,
            mock.patch.object(account_setup, 'wait'),
            mock.patch.object(account_setup, 'immich'),
        ):
            account_setup.signs_in(
                {}, 'immich', client_factory=lambda app, port=None: ports.append(port) or mock.Mock()
            )
        self.assertEqual(ports, [49153])
        self.assertEqual(proved.call_args.kwargs, {'quarantined': True})

    def test_without_sudo_nothing_is_tried(self):
        with mock.patch.object(self.tool.os, 'geteuid', return_value=1000):
            self.assertEqual(self.signin(), self.tool.NO_LOGIN)


if __name__ == '__main__':
    unittest.main()
