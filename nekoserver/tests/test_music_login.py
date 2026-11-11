"""0.50.0, item 11: the person is an administrator in every app. Navidrome was the one
exception: the login the guides gave for it was the non-admin "listener". On a server
whose Navidrome logins are made by 0.50.0 or later the person's login is the
administrator; the listener stays NekoServer's own account for checking the music
library. A server set up before keeps the listener on its phones and is told of both
(tests/test_phone_guide.py, tests/test_show_login.py)."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_store  # noqa: E402
import neko_common  # noqa: E402
from modules import account_credentials  # noqa: E402


class MusicLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            'data_mount': self.tmp.name,
            'accounts': {'enabled': True},
            'media': {'services': ['jellyfin', 'navidrome']},
            'music': {'backup_enabled': False},
        }

    def step(self, saved):
        """The App passwords step with `saved` logins already in the store. Returns what
        the settings said when the logins were collected, and the context."""
        ctx = SimpleNamespace(config=self.config, apply=True, log=lambda line: None, plan=('media',))
        ctx.persist_config = mock.Mock()
        seen = []

        def collect(config, apps, allow_corrections):
            seen.append((config['music'].get('your_login'), ctx.persist_config.call_count, list(apps)))

        with (
            mock.patch.object(account_credentials, 'verify_storage'),
            mock.patch.object(account_credentials, 'lock'),
            mock.patch.object(account_credentials.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(account_store, 'load', return_value={'apps': dict.fromkeys(saved, {})}),
            mock.patch.object(account_store, 'collect', side_effect=collect),
        ):
            account_credentials.run(ctx)
        return seen[0], ctx

    def test_a_new_server_gives_the_person_the_administrator(self):
        (yours, saved_before, apps), ctx = self.step(saved=())
        # Marked and saved before the logins are made: a run cut off in between still knows.
        self.assertEqual((yours, saved_before), ('administrator', 1))
        self.assertEqual(neko_common.navidrome_login(self.config), 'navidrome')
        # The listener is still made: it is the account the music check signs in with.
        self.assertEqual(apps, ['jellyfin', 'navidrome', 'navidrome_listener'])

    def test_a_server_that_has_its_navidrome_logins_is_left_as_it_is(self):
        (yours, saved_before, _), _ = self.step(saved=('jellyfin', 'navidrome', 'navidrome_listener'))
        self.assertEqual((yours, saved_before), (None, 0))
        self.assertEqual(neko_common.navidrome_login(self.config), 'navidrome_listener')

    def test_navidrome_with_data_of_its_own_is_not_a_new_one(self):
        # Set up by hand (or with account setup switched off) before: its logins exist in
        # Navidrome, and phones may use them.
        data = Path(self.tmp.name, 'appdata', 'navidrome')
        data.mkdir(parents=True)
        self.assertTrue(account_credentials.navidrome_is_new(self.config))  # an empty folder
        (data / 'navidrome.db').write_text('x')
        self.assertFalse(account_credentials.navidrome_is_new(self.config))
        (yours, _, _), _ = self.step(saved=())
        self.assertIsNone(yours)

    def test_a_folder_that_leads_off_the_ssd_is_never_looked_at(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(self.tmp.name, 'appdata').mkdir()
        Path(self.tmp.name, 'appdata', 'navidrome').symlink_to(outside.name)
        with self.assertRaisesRegex(RuntimeError, 'must lie on the app SSD'):
            account_credentials.navidrome_is_new(self.config)

    def test_the_music_check_still_signs_in_as_a_non_administrator(self):
        # The listener's rights are what the check may use: an administrator is refused.
        self.assertIn('navidrome_listener', account_store.APPS)
        self.assertIn('non-admin', account_store.APPS['navidrome_listener'][0])
        client = (ROOT / 'music_client.py').read_text()
        self.assertIn("user.get('adminRole') is not False", client)
        made = (ROOT / 'account_setup.py').read_text()
        self.assertIn("result.get('isAdmin') is True", made)  # the administrator really is one
        self.assertIn("value.get('isAdmin') is False", made)  # and the listener is not


if __name__ == '__main__':
    unittest.main()
