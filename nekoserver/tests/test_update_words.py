"""Gate run D (an update from 0.48.2): setup said a first install's words on a server set
up already: "Nothing is installed yet", "Apps to install", "a PIN you choose", "about an
hour", and the welcome's list of what a first install does."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import explanations  # noqa: E402
import neko_common  # noqa: E402

SETTINGS = {'media_apps': {'photos': True, 'movies': True, 'music': True}, 'photos': {'deploy_immich': True}}


def sections(exists):
    with mock.patch.object(neko_common, 'server_exists', return_value=exists):
        return dict(explanations.review(SETTINGS))


class UpdateWordsTests(unittest.TestCase):
    def test_a_first_install_keeps_its_words(self):
        first = sections(False)
        self.assertIn('Apps to install', first)
        self.assertIn('The dashboard, with a PIN you choose', first['Always set up'])
        self.assertTrue(first['What happens next'][0].startswith('Setup takes about an hour'))

    def test_an_update_says_what_is_there_and_kept(self):
        update = sections(True)
        self.assertIn('Apps on this server', update)
        self.assertNotIn('Apps to install', update)
        self.assertIn('The dashboard, with the PIN it has', update['Always set up'])
        self.assertIn('Your accounts, settings and files are kept.', update['What happens next'][0])

    def test_the_update_welcome_is_short_and_says_nothing_changes_before_the_approval(self):
        import wizard

        said = ' '.join(wizard.UPDATE_WELCOME)
        self.assertIn('nothing changes until you approve', said)
        self.assertNotIn('Have ready', said)

    def test_a_first_install_has_no_settings_yet_and_gets_the_full_welcome(self):
        # The fresh install of the gate's J01 run stopped here: the settings were None.
        import wizard

        with mock.patch.object(neko_common, 'server_exists', return_value=False) as exists:
            self.assertIs(wizard.welcome(None), wizard.WELCOME)
        exists.assert_called_once_with({})
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            self.assertIs(wizard.welcome({'data_mount': '/srv/nekoserver'}), wizard.UPDATE_WELCOME)


if __name__ == '__main__':
    unittest.main()
