"""0.50.0, item 13: several confirmations about one thing are one part with one question
(the phone at the end of setup was up to eight steps, each ending in the same "Have you
completed this step successfully?"), and every step's own question says what it asks.
In the setup window a part is one page with a sliding yes for each step."""

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import setup_progress  # noqa: E402
from modules import gateway_tailnet  # noqa: E402

STEPS = [
    {'id': 'one', 'title': 'The first thing', 'lines': ['Do this.', 'Then this.']},
    {'id': 'two', 'title': 'The second thing', 'lines': ['Do that.'], 'logins': []},
    {'id': 'three', 'title': 'The third thing', 'lines': ['And that.']},
]
QUESTION = 'Did all three work?'


def context(folder='/nonexistent/neko'):
    return SimpleNamespace(config={'data_mount': folder}, apply=True, log=lambda line: None)


class Person:
    """Someone at the terminal: the answers typed, the questions asked and the lines shown.
    `window`: what the setup window sent with the answer (its switches)."""

    def __init__(self, answers, window=None):
        self.answers, self.asked, self.shown, self.pages, self.window = iter(answers), [], [], [], window

    def prompt(self, question):
        self.asked.append(question)
        self.pages.append(setup_progress.page_shown())
        answer = next(self.answers)
        if answer is EOFError:
            raise EOFError
        if self.window is not None:
            setup_progress._page['answers']['done'] = self.window
        return answer

    def part(self, steps=STEPS, **more):
        with mock.patch.object(guided_setup, 'interactive', return_value=True):
            return guided_setup.part(
                context(), 'Three things', steps, QUESTION, prompt=self.prompt, output=self.shown.append, **more
            )


class PartTests(unittest.TestCase):
    def test_the_terminal_lists_every_step_and_asks_once(self):
        person = Person(['yes'])
        self.assertEqual(person.part(intro=['Keep this page open.']), {'one', 'two', 'three'})
        self.assertEqual(person.asked, ['Did all three work? [yes/no]: '])
        said = '\n'.join(person.shown)
        self.assertIn('=== Three things ===\nKeep this page open.', said)
        for heading in (
            'Step 1 of 3: The first thing',
            'Step 2 of 3: The second thing',
            'Step 3 of 3: The third thing',
        ):
            self.assertIn(heading, said)
        self.assertLess(said.index('  - Do this.'), said.index('  - Then this.'))
        self.assertNotIn('Kept for the next run', said)

    def test_no_and_ctrl_d_keep_the_part_for_the_next_run(self):
        for answer in ('no', EOFError):
            with self.subTest(answer=answer):
                person = Person([answer])
                self.assertEqual(person.part(), set())
                self.assertEqual(len(person.asked), 1)
                self.assertEqual(
                    person.shown[-1],
                    'Kept for the next run of setup. Still to do: The first thing; The second thing; The third thing.',
                )

    def test_what_was_done_in_an_earlier_run_is_not_asked_for_again(self):
        person = Person(['yes'])
        self.assertEqual(person.part(done=['one']), {'one', 'two', 'three'})
        said = '\n'.join(person.shown)
        self.assertIn('Step 1 of 3: The first thing  (done last time)', said)
        self.assertNotIn('Do this.', said)  # its instructions are not repeated
        # Everything done before: nothing is shown or asked.
        nothing = Person([])
        self.assertEqual(nothing.part(done=['one', 'two', 'three', 'gone']), {'one', 'two', 'three'})
        self.assertEqual((nothing.asked, nothing.shown), ([], []))

    def test_nobody_at_the_terminal_is_asked_nothing(self):
        with mock.patch.object(guided_setup, 'interactive', return_value=False):
            done = guided_setup.part(context(), 'Three things', STEPS, QUESTION, done=['two'], prompt=None)
        self.assertEqual(done, {'two'})

    def test_the_window_gets_one_page_with_every_step(self):
        person = Person(['yes'])
        person.part(done=['two'])
        [page] = person.pages
        self.assertEqual(
            (page['form'], page['asking'], page['title'], page['question']),
            ('part', 'answer', 'Three things', QUESTION),
        )
        self.assertEqual(
            page['steps'],
            [
                {'id': 'one', 'title': 'The first thing', 'lines': ['Do this.', 'Then this.'], 'done': False},
                {'id': 'two', 'title': 'The second thing', 'lines': ['Do that.'], 'done': True},
                {'id': 'three', 'title': 'The third thing', 'lines': ['And that.'], 'done': False},
            ],
        )

    def test_the_windows_switches_are_kept_when_some_are_still_off(self):
        # Continue with two of three on yes: the window answers no and says which are on.
        person = Person(['no'], window='one three')
        self.assertEqual(person.part(), {'one', 'three'})
        self.assertEqual(person.shown[-1], 'Kept for the next run of setup. Still to do: The second thing.')
        # Only ids of this part count, whatever is sent.
        person = Person(['no'], window='two bogus ../x')
        self.assertEqual(person.part(), {'two'})


class StepQuestionTests(unittest.TestCase):
    def test_a_steps_question_is_its_own_and_the_window_may_leave_it(self):
        person = Person(['yes'])
        with mock.patch.object(guided_setup, 'interactive', return_value=True):
            done = guided_setup.checkpoint(
                context(),
                'Tailscale: connect this server',
                ['Sign in.'],
                prompt=person.prompt,
                output=person.shown.append,
                question='Did the Tailscale page say this server is connected?',
            )
        self.assertTrue(done)
        self.assertEqual(person.asked, ['Did the Tailscale page say this server is connected? [yes/no]: '])
        self.assertEqual(person.pages[0]['form'], guided_setup.CONFIRM_STEP)

    def test_no_step_ends_in_a_question_that_could_be_any_steps(self):
        """Every guided step in setup passes its own question; none is left to a default."""
        missing = []
        for path in [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py')]:
            source = path.read_text(encoding='utf-8')
            self.assertNotIn('Have you completed this step successfully', source, path.name)
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, 'attr', getattr(node.func, 'id', '')) == 'checkpoint'
                ):
                    owner = getattr(getattr(node.func, 'value', None), 'id', '')
                    if owner in ('guided_setup', '') and not any(k.arg == 'question' for k in node.keywords):
                        if owner == 'guided_setup' or path.name == 'guided_setup.py':
                            missing.append(f'{path.name}:{node.lineno}')
        self.assertEqual(missing, [])
        # The playback test is called through a stand-in (not seen above): it has its own too.
        self.assertIn(
            "question='Did something you added play in Jellyfin?'", (ROOT / 'modules/onboarding.py').read_text()
        )
        js = (ROOT / 'setup-gui/setup.js').read_text(encoding='utf-8')
        self.assertNotIn('Have you completed this step successfully', js)


class PhoneTests(unittest.TestCase):
    """The phone at the end of setup: one part, one question, the report written once."""

    LINKS = {'jellyfin': 'https://neko.ts.net:8443/', 'immich': 'https://neko.ts.net:8445/'}

    def run_step(self, folder, part):
        ctx = context(folder)
        logs = []
        ctx.log = logs.append
        result = {'status': 'configured', 'links': dict(self.LINKS)}
        with (
            mock.patch.object(gateway_tailnet.gateway_tools, 'selected'),
            mock.patch.object(gateway_tailnet.gateway_tools, 'configure', return_value=True),
            mock.patch.object(gateway_tailnet, 'secure_addresses', return_value=result),
            mock.patch.object(gateway_tailnet.phone_guide, 'write_page', return_value=Path(folder) / 'page.html'),
            mock.patch.object(gateway_tailnet.phone_guide, 'instructions', return_value=['Open the app.']),
            mock.patch.object(gateway_tailnet.phone_guide, 'login_of', side_effect=lambda config, app: app),
            mock.patch.object(guided_setup, 'part', part),
            mock.patch.object(gateway_tailnet, 'atomic_json', wraps=gateway_tailnet.atomic_json) as written,
        ):
            outcome = gateway_tailnet.run(ctx)
        report = json.loads(gateway_tailnet.report_path(ctx.config).read_text())
        return outcome, report, written.call_count

    def test_the_phone_is_asked_once_with_every_step_in_order(self):
        calls = []

        def part(ctx, title, steps, question, done=(), **more):
            calls.append((title, [step['id'] for step in steps], question, list(done), more))
            return {step['id'] for step in steps}

        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'reports').mkdir()
            outcome, report, writes = self.run_step(folder, part)
        [(title, ids, question, done, more)] = calls  # one part, not a question per app
        self.assertEqual(title, 'Set up your phone')
        self.assertEqual(ids, ['tailscale', 'jellyfin', 'immich', 'away_from_home'])
        self.assertEqual(question, 'Is your phone set up, and did an app open on mobile data?')
        self.assertEqual(done, [])
        self.assertIn(('url', gateway_tailnet.phone_guide.TAILSCALE_DOWNLOAD), more['targets'])
        self.assertEqual(report['client_acceptance'], 'user_confirmed')
        self.assertNotIn('phone_steps_done', report)
        self.assertEqual(writes, 1, 'the report is written once')
        self.assertEqual(outcome['status'], 'configured')

    def test_steps_confirmed_before_one_was_left_do_not_come_round_again(self):
        """The setup review (S5): skipping a later app made the confirmed steps repeat."""
        seen = []

        def part(ctx, title, steps, question, done=(), **more):
            seen.append(list(done))
            return {'tailscale', 'jellyfin'} | set(done)  # Immich and the check away are left

        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'reports').mkdir()
            outcome, report, _ = self.run_step(folder, part)
            self.assertEqual(outcome['status'], 'needs_setup')
            self.assertEqual(report['client_acceptance'], 'needs_setup')
            self.assertEqual(report['phone_steps_done'], ['tailscale', 'jellyfin'])
            # The next run is told what is done, and asks only for the rest.
            self.run_step(folder, part)
        self.assertEqual(seen, [[], ['tailscale', 'jellyfin']])

    def test_other_addresses_start_the_phone_over(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'reports').mkdir()
            report = {'links': {'jellyfin': 'https://old.ts.net:8443/'}, 'phone_steps_done': ['tailscale', 'jellyfin']}
            gateway_tailnet.report_path({'data_mount': folder}).write_text(json.dumps(report))
            self.assertEqual(gateway_tailnet.steps_done_before({'data_mount': folder}, self.LINKS), [])
            self.assertEqual(
                gateway_tailnet.steps_done_before({'data_mount': folder}, report['links']), ['tailscale', 'jellyfin']
            )


if __name__ == '__main__':
    unittest.main()
