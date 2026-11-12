"""0.50.0, the wizard (P00): when something of NekoServer is on this PC already, setup
says what it found and offers the choices that apply. It replaces "This PC already has a
NekoServer server. Keep its settings?". A PC with nothing on it never sees the page (T01).

What is found, and which choices apply, is install_state's (tests/test_install_state.py);
here: the words, the choosing, and what setup does with the choice."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import questions  # noqa: E402
import wizard  # noqa: E402


def found(kind, choices, **more):
    base = {
        'kind': kind, 'show': kind != 'nothing', 'version': None, 'this_version': '0.50.0', 'location': None,
        'settings': None, 'settings_from': None, 'job': None, 'waiting': [], 'choices': list(choices),
        'default': choices[0] if choices else None,
    }  # fmt: skip
    return {**base, **more}


def typed(*answers):
    given = iter(answers)
    return lambda question='': next(given)


class WordsTests(unittest.TestCase):
    def test_an_installed_server_says_its_release_its_place_and_what_waits(self):
        here = found(
            'installed',
            ['update', 'change', 'restore'],
            version='0.49.0',
            location='/srv/nekoserver',
            waiting=['tv', 'heartbeat'],
        )
        self.assertEqual(
            wizard.found_said(here),
            [
                'NekoServer 0.49.0 is set up on this PC. Its apps and files are on /srv/nekoserver.',
                '2 things still wait for you: TV setup, Server heartbeat.',
            ],
        )
        self.assertEqual(wizard.choice_said(here, 'update')[0], 'Review this NekoServer update')
        self.assertIn('From 0.49.0 to 0.50.0.', wizard.choice_said(here, 'update')[1])
        self.assertEqual(wizard.choice_said(here, 'change')[0], 'Change apps or settings')
        self.assertIn('Nothing is restored before you approve it.', wizard.choice_said(here, 'restore')[1])

    def test_a_setup_or_a_job_cut_off_says_where_it_stopped(self):
        setup = found('unfinished', ['continue', 'change'], job={'kind': 'setup', 'step': 'docker', 'left': ['docker']})
        self.assertEqual(
            wizard.found_said(setup), ['Setup was started here and stopped before it finished. It stopped at: Docker.']
        )
        self.assertEqual(wizard.choice_said(setup, 'continue')[0], 'Continue unfinished setup')
        update = found('unfinished', ['continue'], job={'kind': 'upgrade', 'step': None, 'left': []})
        self.assertIn('An update of NekoServer was cut off part-way.', wizard.found_said(update)[0])
        self.assertEqual(wizard.choice_said(update, 'continue')[0], 'Finish what was cut off')

    def test_saved_answers_alone_are_offered_to_be_used_never_to_start_fresh(self):
        for kind in ('settings_only', 'older_folder'):
            here = found(kind, ['change'], settings_from='0.37.2')
            self.assertEqual(wizard.choice_said(here, 'change')[0], 'Use these saved answers')
        self.assertIn('(0.37.2)', wizard.found_said(found('older_folder', ['change'], settings_from='0.37.2'))[0])
        self.assertNotIn('earlier_settings', questions.Q)  # "Keep its settings?" with "Start fresh" is gone
        self.assertNotIn('Start fresh', (ROOT / 'wizard.py').read_text())


class ChoosingTests(unittest.TestCase):
    HERE = found('installed', ['update', 'change', 'restore'], version='0.49.0', location='/srv/nekoserver')

    def choose(self, *answers):
        said = []
        return wizard.existing_installation(self.HERE, typed(*answers), said.append), said

    def test_the_choices_are_numbered_and_enter_takes_the_first_which_is_said(self):
        choice, said = self.choose('')
        self.assertEqual(choice, 'update')
        self.assertIn('=== NekoServer was found on this PC. What would you like to do? ===', said)
        self.assertIn('  1) Review this NekoServer update', said)
        self.assertIn('  3) Restore from a backup', said)
        self.assertEqual(self.choose('2')[0], 'change')
        self.assertEqual(self.choose('3')[0], 'restore')

    def test_no_or_ctrl_d_stops_and_a_wrong_number_is_asked_again(self):
        self.assertIsNone(self.choose('no')[0])
        self.assertIsNone(wizard.existing_installation(self.HERE, mock.Mock(side_effect=EOFError), lambda line: None))
        choice, said = self.choose('7', '2')
        self.assertEqual(choice, 'change')
        self.assertIn('Type a number from 1 to 3, or no.', said)


class RunTests(unittest.TestCase):
    def run_with(self, here, *answers):
        said = []
        execute = mock.Mock()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch('os.geteuid', return_value=0),
            mock.patch.object(wizard, 'what_is_here', return_value=here),
            mock.patch.object(wizard, 'settings_for_run', return_value=None) as settings,
        ):
            code = wizard.run(execute, Path(folder) / 'state', prompt=typed(*answers), output=said.append, root=folder)
        return code, said, settings

    def test_a_pc_with_nothing_on_it_never_sees_the_page(self):
        for here in (found('nothing', []), None):
            code, said, settings = self.run_with(here)
            settings.assert_called_once()  # straight on to the settings and the first page
            self.assertFalse([line for line in said if 'was found on this PC' in str(line)])

    def test_restore_names_the_one_command_that_starts_it_and_changes_nothing(self):
        here = found('installed', ['change', 'restore'], location='/srv/nekoserver')
        code, said, settings = self.run_with(here, '2')
        self.assertEqual(code, 0)
        settings.assert_not_called()
        self.assertIn('To restore from a backup, run:  sudo bash install.sh --recover', said)
        self.assertNotIn('import recovery_wizard', (ROOT / 'wizard.py').read_text())

    def test_stopping_changes_nothing(self):
        code, said, settings = self.run_with(found('installed', ['change']), 'no')
        self.assertEqual(code, 0)
        settings.assert_not_called()
        self.assertIn('Nothing was changed. Run  sudo bash install.sh  when you are ready.', said)


if __name__ == '__main__':
    unittest.main()
