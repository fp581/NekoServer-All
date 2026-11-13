"""Gate run C10: the app SSD was replaced and setup run again, while the Music group's
settings stayed on a drive of their own. Setup made new logins for Navidrome and Lidarr,
which the apps refused, and connecting the apps stopped. An app whose settings are kept
is never given a login it does not know: its login comes from a list of app passwords
the recovery menu restored, or is typed; and "not verified" says what to do."""

import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_setup  # noqa: E402
import account_store  # noqa: E402
import neko_common  # noqa: E402

# Made when the tests run: nothing that looks like a real password is written here.
KEPT = 'kept-' + secrets.token_hex(10)


class KeptSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.ssd = self.top / 'ssd'
        (self.ssd / 'appdata').mkdir(parents=True)
        self.drives = self.top / 'drives'
        patch = mock.patch.object(neko_common, 'EXTRA_MOUNTS', self.drives)
        patch.start()
        self.addCleanup(patch.stop)
        self.config = {
            'data_mount': str(self.ssd),
            'media_mount': str(self.top / 'media'),
            'accounts': {'enabled': True},
            'storage': {
                'data_ssd': {'uuid': 'new-ssd', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'hdd-1', 'fstype': 'ext4'},
                'd3': {'uuid': 'd3-1', 'fstype': 'ext4'},
            },
            'install': {'music': 'd3'},
            'media': {'services': ['navidrome', 'lidarr', 'jellyfin']},
        }
        self.kept = self.drives / 'd3' / 'nekoserver' / 'appdata'

    def keep(self, app):
        """The app's settings, kept on its own drive (not yet mounted at the SSD's place)."""
        (self.kept / app).mkdir(parents=True)
        (self.kept / app / 'navidrome.db').write_text('x')

    def test_an_app_whose_settings_are_kept_on_its_drive_has_data(self):
        self.assertTrue(account_store.fresh(self.config, 'navidrome'))
        self.keep('navidrome')
        self.assertFalse(account_store.fresh(self.config, 'navidrome'))
        self.assertFalse(account_store.fresh(self.config, 'navidrome_listener'))  # the same app
        # An app on the SSD looks at the SSD alone, as before.
        self.assertTrue(account_store.fresh(self.config, 'jellyfin'))
        (self.ssd / 'appdata/jellyfin').mkdir()
        (self.ssd / 'appdata/jellyfin/x').write_text('x')
        self.assertFalse(account_store.fresh(self.config, 'jellyfin'))

    def test_a_link_out_of_its_drive_is_refused(self):
        (self.drives / 'd3' / 'nekoserver').mkdir(parents=True)
        (self.drives / 'd3' / 'nekoserver' / 'appdata').symlink_to(self.top)
        with self.assertRaises(RuntimeError):
            account_store.fresh(self.config, 'navidrome')

    def store(self, apps=None):
        saved = {}
        stored = {'format': 1, 'mode': 'generate', 'apps': apps or {}}
        for name, stand_in in (('load', lambda config: stored), ('save', lambda config, value: saved.update(value))):
            patch = mock.patch.object(account_store, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)
        return saved

    def test_a_login_made_before_the_drive_was_seen_is_not_used(self):
        """The accounts page was answered (strong passwords made) before the storage step
        mounted the drive: by then Navidrome is known to have its own account."""
        saved = self.store()
        made = {'username': 'neko-admin', 'password': 'made-' + secrets.token_hex(8), 'state': 'planned',
                'allow_create': True, 'source': 'generate'}  # fmt: skip
        account_store.AHEAD.clear()
        account_store.AHEAD.update(mode='generate', apps={'navidrome': made}, others={})
        self.addCleanup(account_store.AHEAD.clear)
        self.keep('navidrome')
        typed = iter(['dana'])
        passwords = iter([KEPT, KEPT])
        said = []
        with mock.patch.object(account_store, 'restored_login', return_value=None):
            account_store.collect(
                self.config,
                ['navidrome'],
                prompt=lambda q='': next(typed),
                password_prompt=lambda q='': next(passwords),
                output=said.append,
            )
        entry = saved['apps']['navidrome']
        self.assertEqual((entry['username'], entry['password'], entry['allow_create']), ('dana', KEPT, False))
        self.assertIn('  Existing app data: enter the current login; it will not be reset.', said)

    def test_the_login_comes_from_a_password_list_the_recovery_menu_restored(self):
        restored = self.top / 'root' / 'nekoserver-passwords-20261112-1200' / 'secrets' / 'accounts'
        restored.mkdir(parents=True)
        listed = restored / 'credentials.json'
        entry = {'username': 'neko-admin', 'password': KEPT, 'state': 'verified', 'allow_create': False}
        listed.write_text(json.dumps({'format': 1, 'mode': 'generate', 'apps': {'navidrome': entry}}))
        os.chmod(listed, 0o600)
        with mock.patch.object(account_store, 'private'):
            found = account_store.restored_login('navidrome', root=self.top / 'root')
            self.assertEqual(found, ('neko-admin', KEPT))
            self.assertIsNone(account_store.restored_login('lidarr', root=self.top / 'root'))
        said = []
        with mock.patch.object(account_store, 'restored_login', return_value=('neko-admin', KEPT)):
            got = account_store.existing_login(self.config, 'navidrome', {}, None, None, said.append)
        self.assertEqual(got, ('neko-admin', KEPT))
        self.assertTrue(said[0].startswith('  Its login is taken from the list of app passwords you restored'))
        self.assertFalse([line for line in said if KEPT in line], 'the password is never said')

    def test_without_one_it_says_where_a_backups_list_is_and_asks(self):
        asked = []

        def ask_login(*given, **named):
            asked.append(given)
            return 'dana', KEPT

        with (
            mock.patch.object(account_store, 'restored_login', return_value=None),
            mock.patch.object(account_store, 'password_lists', return_value=True),
            mock.patch.object(account_store, 'ask_login', side_effect=ask_login),
        ):
            said = []
            got = account_store.existing_login(self.config, 'lidarr', {}, None, None, said.append)
        self.assertEqual(got, ('dana', KEPT))
        self.assertEqual(said, [account_store.LIST_IN_BACKUP])
        self.assertIn('The list of app passwords', said[0])
        self.assertTrue(asked[0][1], 'asked as the login of an app that has its account')

    def test_a_list_in_a_backup_is_found_where_the_backups_keep_it(self):
        media = Path(self.config['media_mount'])
        self.assertFalse(account_store.password_lists(self.config))
        run = media / 'backups/password-lists/20261112T000000Z'
        run.mkdir(parents=True)
        (run / 'secrets.tar.cms').write_bytes(b'sealed')
        self.assertTrue(account_store.password_lists(self.config))
        # The recovery menu's own names: if they change there, this is told.
        source = (ROOT / 'recovery_wizard.py').read_text()
        for folder in account_store.PASSWORD_LIST_FOLDERS:
            self.assertIn(f"'{folder}'", source)
        self.assertIn("SEALED = 'secrets.tar.cms'", source)

    def test_not_verified_says_what_to_do(self):
        self.keep('navidrome')
        made = {'username': 'neko-admin', 'allow_create': True}
        said = account_setup.what_to_do(self.config, 'navidrome', made)
        self.assertIn('Navidrome administrator has an account of its own', said)
        self.assertIn('choose c when it asks', said)
        self.assertIn('The list of app passwords', said)
        fresh = account_setup.what_to_do(self.config, 'jellyfin', {'allow_create': True})
        self.assertIn('setup tries Jellyfin administrator again', fresh)
        # The retry question says why the made login cannot work.
        saved = self.store({'navidrome': {**made, 'password': 'made-' + secrets.token_hex(8),
                                          'state': 'needs_setup', 'source': 'generate'}})  # fmt: skip
        lines = []
        account_store.collect(self.config, ['navidrome'], prompt=lambda q='': '', output=lines.append)
        self.assertIn('has its own account from before (its settings were kept)', ' '.join(lines))
        self.assertEqual(saved['apps']['navidrome']['state'], 'needs_setup')  # kept, never replaced unasked


if __name__ == '__main__':
    unittest.main()
