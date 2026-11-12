"""The accounts page and the backups page before the one approval (0.50.0, after gate run A;
the user: nothing is asked after "Install selected apps" but what needs the running
server). The dashboard PIN and the shared folders' password are on the accounts page,
made for you or typed, and the steps after the approval take them without asking."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_store  # noqa: E402
import drive_table  # noqa: E402
import setup_progress  # noqa: E402
from modules import storage  # noqa: E402


ORIGINAL_OTHERS_WANTED = account_store.others_wanted


def settings(folder, **more):
    return {
        'data_mount': folder,
        'accounts': {'enabled': True},
        'media_apps': {'photos': True, 'movies': False, 'music': False},
        'photos': {'deploy_immich': True},
        'nextcloud': {'enabled': False},
        **more,
    }


class AheadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.addCleanup(account_store.AHEAD.clear)
        self.said = []
        wanted = mock.patch.object(account_store, 'others_wanted', return_value=['dashboard_pin', 'shared_folders'])
        wanted.start()
        self.addCleanup(wanted.stop)

    def test_made_for_you_the_logins_pin_and_shared_folders_password_are_kept_for_the_run(self):
        config = settings(self.folder)
        typed = iter(['1'])
        account_store.ask_ahead(
            config, lambda q='': next(typed), mock.Mock(side_effect=AssertionError), self.said.append
        )
        self.assertEqual(account_store.AHEAD['mode'], 'generate')
        self.assertEqual(sorted(account_store.AHEAD['apps']), ['immich'])
        pin = account_store.AHEAD['others']['dashboard_pin']
        self.assertRegex(pin, r'^\d{8}$')
        self.assertEqual(len(account_store.AHEAD['others']['shared_folders']), 32)
        # Nothing was written: the app drive is not open yet.
        self.assertFalse((Path(self.folder) / 'secrets').exists())
        self.assertFalse(any(pin in line for line in self.said))  # never said

    def test_enter_alone_takes_the_recommended_passwords_made_for_you(self):
        # Gate run B1: "Type 1 or 2." answered Enter alone, the one question that did.
        typed = iter([''])
        account_store.ask_ahead(
            settings(self.folder), lambda q='': next(typed), mock.Mock(side_effect=AssertionError), self.said.append
        )
        self.assertEqual(account_store.AHEAD['mode'], 'generate')
        self.assertNotIn('Type 1 or 2.', self.said)

    def test_typed_ones_are_checked_and_a_pin_may_be_left_out(self):
        config = settings(self.folder)
        answers = iter(['2', 'neko@example.org'])
        secrets_typed = iter(['p' * 12, 'p' * 12, '', '', 's' * 14, 's' * 14])
        account_store.ask_ahead(config, lambda q='': next(answers), lambda q='': next(secrets_typed), self.said.append)
        self.assertEqual(account_store.AHEAD['apps']['immich']['username'], 'neko@example.org')
        self.assertEqual(account_store.AHEAD['others'], {'dashboard_pin': '', 'shared_folders': 's' * 14})
        with self.assertRaisesRegex(ValueError, '6 to 8 digits'):
            account_store.validate_other('dashboard_pin', '12ab')
        with self.assertRaisesRegex(ValueError, '12-128'):
            account_store.validate_other('shared_folders', 'short')

    def test_three_wrong_tries_are_said_and_left_for_later(self):
        typed = iter(['1234', '1234'] * 3)
        got = account_store.asked_other('dashboard_pin', 'custom', None, lambda q='': next(typed), self.said.append)
        self.assertEqual(got, '')
        self.assertIn('  Dashboard PIN: left without a PIN for now. Run setup again to choose one.', self.said)

    def test_a_login_the_app_has_already_is_told_its_own_limits(self):
        with self.assertRaisesRegex(ValueError, 'Password must contain 1-128 characters'):
            account_store.validate_login('jellyfin', 'mary', '', existing=True)
        with self.assertRaisesRegex(ValueError, 'Password must contain 12-128 characters'):
            account_store.validate_login('jellyfin', 'mary', 'short', existing=False)

    def test_a_samba_tool_that_hangs_still_lets_the_page_ask(self):
        import subprocess

        hung = subprocess.TimeoutExpired(['pdbedit'], 15)
        with (
            mock.patch.object(account_store.Path, 'exists', return_value=True),  # a PIN is set already
            mock.patch('shutil.which', return_value='/usr/bin/pdbedit'),
            mock.patch('subprocess.run', side_effect=hung),
        ):
            wanted = ORIGINAL_OTHERS_WANTED({'samba': {'enabled': True}})
        self.assertEqual(wanted, ['shared_folders'])

    def test_the_page_has_a_row_for_each_secret_with_no_user_name(self):
        with account_store.accounts_page(settings(self.folder), ['immich'], ['dashboard_pin', 'shared_folders']):
            page = setup_progress.page_shown()
        rows = {row['id']: row for row in page['apps']}
        self.assertTrue(rows['dashboard_pin']['secret_only'] and rows['dashboard_pin']['pin'])
        self.assertTrue(rows['shared_folders']['secret_only'])
        self.assertIn('pass:dashboard_pin', page['fields'])

    def test_after_the_approval_the_store_takes_them_and_nothing_is_asked(self):
        config = settings(self.folder)
        account_store.AHEAD.update(
            mode='generate',
            apps={'immich': {'username': 'neko_1@server.invalid', 'password': 'x' * 32, 'state': 'planned',
                             'allow_create': True, 'source': 'generate'}},
            others={'dashboard_pin': '12345678', 'shared_folders': 'y' * 20},
        )  # fmt: skip
        never = mock.Mock(side_effect=AssertionError('asked'))
        with mock.patch.object(account_store, 'check_parent'), mock.patch.object(account_store, 'private'):
            account_store.collect(config, ['immich'], never, never, self.said.append)
            stored = account_store.load(config)
            self.assertEqual(stored['apps']['immich']['username'], 'neko_1@server.invalid')
            self.assertEqual(stored['others']['dashboard_pin'], '12345678')
            # Show login reads a secret like an app's login.
            self.assertEqual(account_store.get(config, 'shared_folders')['password'], 'y' * 20)
            account_store.AHEAD.clear()
            self.assertEqual(account_store.other(config, 'dashboard_pin'), '12345678')

    def test_a_server_whose_apps_have_logins_asks_nothing(self):
        config = settings(self.folder)
        stored = {'format': 1, 'mode': 'generate', 'apps': {'immich': {}}, 'others': {}}
        with (
            mock.patch.object(account_store, 'load', return_value=stored),
            mock.patch.object(account_store, 'others_wanted', return_value=[]),
        ):
            account_store.ask_ahead(config, mock.Mock(side_effect=AssertionError('asked')))
        self.assertEqual(account_store.AHEAD, {})


class StepsTakeThemTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(account_store.AHEAD.clear)

    def test_the_dashboard_sets_the_pin_chosen_ahead_without_asking(self):
        from modules import dashboard

        account_store.AHEAD.update(others={'dashboard_pin': '24681357'})
        with (
            mock.patch.object(dashboard, 'CONTROL_CONFIG', Path(tempfile.mkdtemp())),
            mock.patch.object(dashboard, 'save_pin', return_value=True) as saved,
            mock.patch.object(dashboard.setup_progress, 'password_page', side_effect=AssertionError('asked')),
        ):
            self.assertTrue(dashboard.choose_pin(Path('/x/runtime'), lambda line: None))
        saved.assert_called_once_with(Path('/x/runtime'), mock.ANY, '24681357')

    def test_samba_takes_the_password_chosen_ahead(self):
        from modules import samba

        account_store.AHEAD.update(others={'shared_folders': 'z' * 16})
        ran = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
        with mock.patch('guided_setup.new_password', side_effect=AssertionError('asked')):
            self.assertTrue(samba.samba_password('neko', ran))
        self.assertEqual(ran.call_args.kwargs['input'], 'z' * 16 + '\n' + 'z' * 16 + '\n')

    def test_backup_times_chosen_ahead_are_not_asked_again(self):
        self.addCleanup(drive_table.TIMES_AHEAD.clear)
        drive_table.TIMES_AHEAD.update(asked=True)
        with tempfile.TemporaryDirectory() as folder:
            context = SimpleNamespace(config={'data_mount': folder}, log=mock.Mock(), plan=None)
            with mock.patch('guided_setup.choose_backup_times', side_effect=AssertionError('asked')):
                storage.choose_backup_times(context)
            kept = json.loads(next(Path(folder).rglob('*.json')).read_text())
        self.assertIn('backup_times', kept)

    def test_the_backups_answers_are_carried_through_the_approval(self):
        before = {'backup_times': None, 'app_backup': {'keep_snapshots': 7}}
        after = {
            'backup_times': {'daily': '23:00'},
            'backup_counts_reviewed': True,
            'app_backup': {'keep_snapshots': 3},
        }
        changes = storage.section_changes(before, after)
        self.assertEqual(changes['backup_times'], {'daily': '23:00'})
        self.assertIs(changes['backup_counts_reviewed'], True)
        self.assertEqual(changes['app_backup'], ({'keep_snapshots': 3}, []))


if __name__ == '__main__':
    unittest.main()
