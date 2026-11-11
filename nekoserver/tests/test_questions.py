"""0.50.0, item 13 redone: every question has its own words for the setup window (a title, a
hint, buttons that say what they do) and the terminal asks the same question in the same
words, with one line on how to answer (questions.py)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import questions  # noqa: E402
import setup_progress  # noqa: E402


class Asked:
    """A person at the terminal: the prompts shown, the window's page at each, the answers."""

    def __init__(self, *answers):
        self.answers, self.prompts, self.pages = list(answers), [], []

    def __call__(self, prompt=''):
        self.prompts.append(prompt)
        self.pages.append(setup_progress.page_shown())
        answer = self.answers.pop(0)
        if answer is EOFError:
            raise EOFError
        return answer


class WordsTests(unittest.TestCase):
    def test_the_terminal_asks_in_the_same_words_with_how_to_answer(self):
        self.assertEqual(
            questions.terminal('tailscale_connected'),
            '\nIs this server connected to Tailscale?\n'
            'The Tailscale page in your browser says so once you have signed in and pressed Connect.\n'
            'Type yes or no, then press Enter. ',
        )
        # A choice lists what each number means, in the buttons' words.
        self.assertEqual(
            questions.terminal('login_who', app='Jellyfin'),
            '\nWho picks the login for Jellyfin?\n'
            '  1) NekoServer makes one for me (recommended)\n'
            "  2) I'll choose my own\n"
            'Type the number of your choice, then press Enter. ',
        )
        # Something that can wait says what Enter alone does.
        self.assertTrue(questions.terminal('monitor_address').endswith('Press Enter alone to skip this for now. '))
        self.assertIn('It is not shown while you type.', questions.terminal('monitor_address'))

    def test_the_window_gets_a_title_a_hint_and_buttons_that_say_what_they_do(self):
        page = questions.window('earlier_settings')
        self.assertEqual(page['title'], 'This PC already has a NekoServer server. Keep its settings?')
        self.assertEqual(
            page['choices'], [{'answer': 'yes', 'label': 'Keep them'}, {'answer': 'no', 'label': 'Start fresh'}]
        )
        page = questions.window('monitor_address')
        self.assertEqual(
            (page['kind'], page['send'], page['skip'], page['skip_label']), ('secret', 'Save', '', 'Skip for now')
        )
        page = questions.window('nas_folder', words='your films')
        self.assertEqual(
            (page['title'], page['skip_label']),
            ('Which shared folder is for your films?', 'Use a drive on this PC instead'),
        )
        self.assertTrue(page['above'])

    def test_asked_in_both_places_and_the_answer_is_what_the_code_always_read(self):
        person = Asked('2')
        self.assertEqual(questions.ask('login_who', person, app='Immich'), '2')
        self.assertEqual(person.pages[0]['form'], 'ask')
        self.assertEqual(person.pages[0]['title'], 'Who picks the login for Immich?')
        self.assertEqual(person.prompts[0], questions.terminal('login_who', app='Immich'))
        self.assertIsNone(setup_progress.page_shown())  # the page ends with the question
        # Yes or no: True, False, a wrong word asked again, Ctrl+D left.
        said = []
        person = Asked('maybe', 'yes')
        self.assertIs(questions.yes_no('drive_space', person, said.append), True)
        self.assertEqual(said, ['Please type yes or no.'])
        self.assertIs(questions.yes_no('drive_space', Asked('no'), said.append), False)
        self.assertIsNone(questions.yes_no('drive_space', Asked(EOFError), said.append))
        # A step that can be left says so to the window.
        person = Asked('yes')
        questions.yes_no('tailscale_connected', person, said.append, leave=True)
        self.assertTrue(person.pages[0]['leave'])

    def test_a_list_made_at_run_time_is_the_windows_buttons_and_the_terminals_numbers(self):
        rows = [('0', 'No assistant'), ('1', 'qwen3.5:4b')]
        person = Asked('1')
        questions.ask('assistant_model', person, choices=rows, default='no assistant')
        self.assertEqual(
            person.pages[0]['choices'],
            [{'answer': '0', 'label': 'No assistant'}, {'answer': '1', 'label': 'qwen3.5:4b'}],
        )
        self.assertIn('  0) No assistant\n  1) qwen3.5:4b\n', person.prompts[0])
        self.assertTrue(person.prompts[0].endswith('Enter alone keeps no assistant. '))

    def test_the_questions_own_lines_are_not_shown_again_as_what_came_before(self):
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        with setup_progress._lock:
            setup_progress._recent.extend(['A list of drives.', 'Is this server connected to Tailscale?'])
        record = setup_progress.question(questions.terminal('tailscale_connected'))
        setup_progress.answered()
        self.assertEqual(record['context'], ['A list of drives.'])

    def test_every_question_is_asked_somewhere(self):
        """A text nobody asks would be reviewed for nothing."""
        source = '\n'.join(
            path.read_text(encoding='utf-8')
            for path in [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py')]
            if path.name != 'questions.py'
        )
        unused = [key for key in questions.Q if f"'{key}'" not in source]
        self.assertEqual(unused, [])


if __name__ == '__main__':
    unittest.main()
