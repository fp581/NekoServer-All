"""0.50.0, the wizard (P09): the finishing page says how things stand for each app,
kept apart: installed, signed in, opens, tried by you; and for the server: a backup made,
a restore tried. Nothing is summed into one "all done". A test nobody tried, an empty
library included, is said as that and holds nothing up (T26, T37, T38).

What each state is decided from is finish_states's (tests/test_finish_states.py)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import finish_states  # noqa: E402
import wizard  # noqa: E402


def state(word, reason=None):
    return {'state': word, 'reason': reason, 'action': None}


SUMMARY = {
    'outcome': 'pending',
    'apps': [
        {
            'id': 'jellyfin',
            'name': 'Jellyfin',
            'states': {
                'installed': state('done'),
                'login': state('done'),
                'address': state('done'),
                'tried': state('not_tested', 'empty_library'),
            },
        },
        {
            'id': 'sonarr',
            'name': 'Sonarr',
            'states': {
                'installed': state('done'),
                'login': state('failed', 'saved_login_refused'),
                'address': state('waiting', 'still_starting'),
            },
        },
    ],
    'server': {'backup': state('not_tested', 'has_not_run_yet'), 'restore': state('not_tested', 'nobody_tried_yet')},
    'tests': [
        {'id': 'play_video', **state('not_tested', 'empty_library')},
        {'id': 'phone_photo', **state('done')},
        {'id': 'tv_sound', **state('waiting', 'you_chose_later')},
    ],
}


class FinishPageTests(unittest.TestCase):
    def test_each_apps_states_are_said_apart(self):
        lines = wizard.states_lines(SUMMARY)
        self.assertIn('How things stand: Your apps are installed. Some setup is still waiting for you.', lines)
        self.assertIn(
            '  Jellyfin. Installed: done. Signed in: done. Opens: done. Tried by you: not tried yet (your library is '
            'empty: add a video or a song you own, or leave this test for later).',
            lines,
        )
        # An app without a test has no "tried" at all; a failed and a waiting state say why.
        self.assertIn(
            '  Sonarr. Installed: done. Signed in: needs fixing (the app did not take the saved login). Opens: '
            'waiting (it is still starting; look again in a minute).',
            lines,
        )
        self.assertIn(
            '  Backups. Backup made: not tried yet (this backup has not run yet). Restore tried: not tried yet '
            '(nobody has tried this yet).',
            lines,
        )

    def test_open_tests_are_listed_and_hold_nothing_up(self):
        lines = wizard.states_lines(SUMMARY)
        self.assertIn('You can try these whenever you like; nothing waits for them:', lines)
        self.assertIn(
            '  - Play a video in Jellyfin: not tried yet (your library is empty: add a video or a song you own, or '
            'leave this test for later)',
            lines,
        )
        self.assertIn('  - Sound comes from the TV: waiting (you chose to try this later)', lines)
        self.assertFalse([line for line in lines if 'A photo taken on the phone' in line])  # done: not listed
        # Nothing to try: no such list.
        self.assertFalse(
            [line for line in wizard.states_lines({**SUMMARY, 'tests': []}) if 'whenever you like' in line]
        )

    def test_no_line_sums_everything_into_one_word(self):
        said = ' '.join(wizard.states_lines({**SUMMARY, 'outcome': 'ready'})).lower()
        for word in ('all done', 'everything works', 'all passed', 'fully'):
            self.assertNotIn(word, said)
        self.assertEqual(
            (wizard.OUTCOMES['ready'], wizard.OUTCOMES['attention']), ('NekoServer is ready.', 'Setup needs attention.')
        )

    def test_every_reason_and_state_the_judge_can_give_has_its_words(self):
        source = (ROOT / 'finish_states.py').read_text()
        import re

        reasons = set(re.findall(r"row\([A-Z_]+, '([a-z_]+)'", source))
        self.assertTrue(reasons)
        self.assertEqual(reasons - set(wizard.REASONS), set())
        self.assertEqual(set(wizard.STATE_WORDS), {'done', 'failed', 'not_tested', 'waiting'})
        self.assertEqual({test for test, *_ in finish_states.APP_TESTS} - set(wizard.TEST_TITLES), set())
        self.assertEqual(set(finish_states.TV_TESTS) - set(wizard.TEST_TITLES), set())

    def test_the_screen_shows_them_and_a_server_that_cannot_be_read_says_nothing(self):
        said = []
        with (
            mock.patch.object(wizard, 'show_end'),
            mock.patch.object(wizard, 'planned', return_value=(['system'], {'system': {'status': 'success'}})),
        ):
            wizard.finishing_screen({'data_mount': '/srv/nekoserver'}, {}, output=said.append, summary=SUMMARY)
            self.assertTrue(any(line.startswith('  Jellyfin. Installed: done.') for line in said))
            said.clear()
            wizard.finishing_screen({'data_mount': '/srv/nekoserver'}, {}, output=said.append)
            self.assertFalse([line for line in said if 'How things stand' in str(line)])
        # A PC where no app was installed is not probed at all; a read that fails says nothing.
        with mock.patch.object(finish_states, 'collect', side_effect=AssertionError('probed')):
            self.assertIsNone(wizard.read_states({'data_mount': '/nonexistent/neko-test'}, ['system']))
        with (
            mock.patch.object(wizard.Path, 'is_dir', return_value=True),
            mock.patch.object(finish_states, 'collect', side_effect=OSError('no app drive')),
        ):
            self.assertIsNone(wizard.read_states({'data_mount': '/nonexistent/neko-test'}, ['system']))


if __name__ == '__main__':
    unittest.main()
