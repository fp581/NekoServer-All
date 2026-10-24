"""The setup window: the words it shows, the events setup feeds it, its little server,
and the wizard's "Ready to start?" that the window can answer."""
import builtins
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import explanations  # noqa: E402
import host  # noqa: E402
import setup_gui  # noqa: E402
import setup_progress  # noqa: E402
import wizard  # noqa: E402
from modules import tv  # noqa: E402


class ExplanationTests(unittest.TestCase):
    def test_every_step_and_package_has_its_words(self):
        self.assertEqual(set(explanations.STEP_EXPLANATIONS), set(wizard.STEPS))
        for name in host.APP_PACKAGES:
            self.assertIn(name, explanations.PACKAGES)
        for name in ('steam', 'steam-devices', 'vulkan-intel', 'lib32-vulkan-intel', 'vulkan-radeon', 'lib32-vulkan-radeon',
                     'firefox', 'bluez', 'bluez-utils', 'openrgb', 'smartmontools', *tv.vulkan_packages(['intel', 'amd'])):
            self.assertIn(name, explanations.PACKAGES)
        for extra in explanations.EXTRAS:
            self.assertIn(extra['step'], wizard.STEPS)
            for name in extra['installs']:
                self.assertIn(name, explanations.PACKAGES)
        for text in (*explanations.STEP_EXPLANATIONS.values(), *explanations.PACKAGES.values()):
            self.assertLess(len(text), 160, text)   # one line, not an essay

    def test_extras_read_and_write_the_settings(self):
        config = {}
        extra = next(e for e in explanations.EXTRAS if e['id'] == 'tv')
        self.assertTrue(explanations.extra_state(config, extra))        # assumed on until chosen
        explanations.set_extra(config, extra, False)
        self.assertEqual(config, {'tv': {'wanted': False}})
        self.assertFalse(explanations.extra_state(config, extra))
        nextcloud = next(e for e in explanations.EXTRAS if e['id'] == 'nextcloud')
        self.assertFalse(explanations.extra_state({'nextcloud': {'enabled': False}}, nextcloud))


class ProgressTests(unittest.TestCase):
    def tearDown(self):
        setup_progress.disable()

    def test_events_are_kept_and_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'progress.jsonl'
            setup_progress.enable(path)
            setup_progress.step('docker', 'running')
            setup_progress.packages(['docker', 'docker-compose'], ['docker-ce', 'docker-compose-plugin'])
            setup_progress.note('Docker is running')
            setup_progress.step('docker', 'success')
            kinds = [e['kind'] for e in setup_progress.events()]
            self.assertEqual(kinds, ['step', 'packages', 'note', 'step'])
            self.assertEqual(setup_progress.events()[1]['step'], 'docker')   # recorded while docker ran
            self.assertIsNone(setup_progress.events()[3]['step'])
            self.assertEqual(setup_progress.events(since=3)[0]['kind'], 'step')
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e['seq'] for e in lines], [1, 2, 3, 4])
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_questions_are_seen_while_they_wait_and_never_their_answers(self):
        # stdin is not a terminal here: the plain input() answers, as in any script.
        setup_progress.enable()
        seen = []
        with mock.patch('builtins.input', side_effect=lambda prompt='': seen.append(setup_progress.pending_question()) or 'secret answer'), \
                mock.patch('getpass.getpass', return_value='1234'), mock.patch('sys.stdin', mock.Mock(fileno=mock.Mock(side_effect=OSError))), \
                mock.patch('sys.stdout'):
            with setup_progress.watching_prompts():
                print('Drives on this PC:')
                print('  1. sda  Samsung SSD')
                self.assertEqual(builtins.input('Choose number: '), 'secret answer')
                self.assertEqual(seen[0]['text'], 'Choose number:')
                self.assertEqual(seen[0]['form'], 'text')
                self.assertEqual(seen[0]['context'], ['Drives on this PC:', '  1. sda  Samsung SSD'])
                self.assertIsNone(setup_progress.pending_question())
                # A step's own block is the context, without the installer's log lines.
                print('[2026-10-24T12:21:53+00:00] START tailscale')
                print('=== Tailscale: connect this server ===')
                print('1. Open the sign-in page on any device.')
                builtins.input('Have you completed this step successfully? [yes/no]: ')
                self.assertEqual(seen[1]['context'], ['=== Tailscale: connect this server ===', '1. Open the sign-in page on any device.'])
                self.assertEqual(seen[1]['form'], 'yesno')
                import getpass
                self.assertEqual(getpass.getpass('PIN: '), '1234')
                self.assertEqual(setup_progress.question_kind('Ready? [yes/no]: ', False), 'yesno')
        texts = json.dumps(setup_progress.events())
        self.assertNotIn('secret answer', texts)
        self.assertNotIn('1234', texts)
        self.assertEqual([e['kind'] for e in setup_progress.events()], ['question', 'answered'] * 3)
        hidden = next(e for e in setup_progress.events() if e['kind'] == 'question' and e['secret'])
        self.assertEqual((hidden['text'], hidden['form']), ('a hidden entry', 'secret'))
        self.assertIs(builtins.input, builtins.__dict__['input'])   # restored afterwards

    def test_the_window_or_the_terminal_answers_whichever_is_first(self):
        import pty
        setup_progress.enable()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        terminal = os.fdopen(slave, 'r')
        self.addCleanup(terminal.close)
        with mock.patch('sys.stdout'):
            # The window answers: the terminal is told, nothing was typed.
            record = setup_progress.question('Select the SSD. Choose number: ')
            self.assertFalse(setup_progress.answer_from_window(record['seq'] + 1, '9'))   # not the question waiting
            self.assertTrue(setup_progress.answer_from_window(record['seq'], '2'))
            self.assertFalse(setup_progress.answer_from_window(record['seq'], '3'))        # answered already
            self.assertEqual(setup_progress._wait_for_answer(terminal, False), '2')
            setup_progress.answered()
            # The terminal answers.
            setup_progress.question('Same PIN again: ', secret=True)
            os.write(master, b'1234\n')
            self.assertEqual(setup_progress._wait_for_answer(terminal, True), '1234')
            setup_progress.answered()
            self.assertIsNone(setup_progress.pending_question())
            # Not a terminal at all: nothing to watch.
            with tempfile.TemporaryFile('r+') as plain:
                setup_progress.question('x')
                self.assertIsNone(setup_progress._wait_for_answer(plain, False))
                setup_progress.answered()


class WindowTests(unittest.TestCase):
    def setUp(self):
        setup_progress.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / 'my-config.json'
        self.config = {'profiles': {'full': ['system', 'docker', 'tv', 'maintenance']}, 'tv': {'user': 'auto'}}
        self.settings.write_text(json.dumps(self.config))
        self.window = setup_gui.Window(self.settings, self.config, wizard.STEPS, self.config['profiles']['full'])
        self.addCleanup(self.window.stop)
        self.addCleanup(setup_progress.disable)

    def get(self, route, token=None):
        url = self.window.url.replace(self.window.token, token or self.window.token) + route
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers.get('Content-Type', ''), response.read()

    def post(self, route, payload):
        request = urllib.request.Request(self.window.url + route, data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def state(self):
        return json.loads(self.get('api/state')[2])

    def test_the_page_its_files_and_the_token(self):
        status, kind, body = self.get('')
        self.assertEqual((status, kind), (200, 'text/html; charset=utf-8'))
        self.assertIn(b'NekoServer setup', body)
        self.assertIn(b'static/setup.js', body)
        self.assertEqual(self.get('static/setup.css')[1], 'text/css; charset=utf-8')
        self.assertEqual(self.get('static/logo.svg')[1], 'image/svg+xml')
        for route, token in (('', 'x' * 32), ('api/state', 'y' * 32), ('static/../wizard.py', None), ('wizard.py', None)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(route, token)
            self.assertEqual(caught.exception.code, 404, route)

    def test_choosing_before_the_start_saves_the_settings_and_start_is_seen(self):
        state = self.state()
        self.assertEqual(state['phase'], 'choose')
        self.assertEqual([s['id'] for s in state['steps']], ['system', 'docker', 'tv', 'maintenance'])
        self.assertEqual(state['steps'][1]['title'], 'Docker')
        self.assertTrue(next(e for e in state['extras'] if e['id'] == 'tv')['on'])
        self.assertEqual(next(e for e in state['extras'] if e['id'] == 'tv')['installs'][0]['name'], 'steam')
        self.assertEqual(self.post('api/choose', {'tv': False, 'nonsense': True, 'heartbeat': 'yes'}), {'ok': True})
        saved = json.loads(self.settings.read_text())
        self.assertEqual(saved['tv'], {'user': 'auto', 'wanted': False})
        self.assertNotIn('nonsense', saved)
        self.assertNotIn('wanted', saved.get('heartbeat', {}))
        self.assertFalse(next(e for e in self.state()['extras'] if e['id'] == 'tv')['on'])
        self.assertFalse(self.window.started.is_set())
        self.post('api/start', {'choices': {'notifications': False}})
        self.assertTrue(self.window.started.is_set())
        self.assertFalse(json.loads(self.settings.read_text())['notifications']['wanted'])
        self.assertEqual(self.state()['phase'], 'running')

    def test_the_run_as_the_window_sees_it(self):
        self.window.started.set()
        setup_progress.step('system', 'running')
        setup_progress.note('ok   Docker Engine: 29.1.3')
        setup_progress.step('system', 'success')
        setup_progress.step('docker', 'running')
        setup_progress.packages(['docker', 'docker-compose'], ['docker-ce'])
        state = self.state()
        self.assertEqual(state['current'], 'docker')
        self.assertEqual(state['done'], 1)
        system, docker = state['steps'][0], state['steps'][1]
        self.assertEqual(system['status'], 'success')
        self.assertEqual(system['notes'], ['ok   Docker Engine: 29.1.3'])
        self.assertEqual([p['name'] for p in docker['packages']], ['docker', 'docker-compose'])
        self.assertIn('container', docker['packages'][0]['explanation'])
        self.assertIsNone(state['question'])
        record = setup_progress.question('Select the SSD. Choose number: ')
        question = self.state()['question']
        self.assertEqual((question['seq'], question['text'], question['secret'], question['kind']),
                         (record['seq'], 'Select the SSD. Choose number:', False, 'text'))
        self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '2'}), {'ok': True})
        self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '3'}), {'ok': False})   # taken already
        self.assertEqual(setup_progress._pending['answer'], '2')
        setup_progress.answered()
        setup_progress.step('docker', 'needs_setup', 'Docker is installed as a snap')
        setup_progress.step('tv', 'unavailable', 'no Steam package')
        setup_progress.event('finished', lines=['  NekoServer is almost ready', 'Done: 1 of 3 steps.'], complete=False)
        state = self.state()
        self.assertEqual(state['phase'], 'finished')
        self.assertEqual(state['finish'][1], 'Done: 1 of 3 steps.')
        self.assertEqual(state['steps'][1]['reason'], 'Docker is installed as a snap')
        self.assertEqual(state['done'], 2)    # unavailable counts as settled
        self.assertTrue(self.window.delivered.is_set())

    def test_a_body_too_large_or_broken_is_refused(self):
        for data, code in ((b'{' * 20, 400), (b'{"tv": false}' + b' ' * setup_gui.LARGEST_BODY, 413)):
            request = urllib.request.Request(self.window.url + 'api/choose', data=data,
                                             headers={'Content-Type': 'application/json'}, method='POST')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, code)


class ReadyToStartTests(unittest.TestCase):
    """The terminal's "Ready to start?" and the window's Start answer the same question."""
    def pipe(self, text=''):
        read_end, write_end = os.pipe()
        if text:
            os.write(write_end, text.encode())
        os.close(write_end)
        self.addCleanup(lambda: None)
        return os.fdopen(read_end)

    def test_start_in_the_window_counts_as_yes(self):
        window = mock.Mock(started=threading.Event())
        window.started.set()
        said = []
        with self.pipe() as stdin, mock.patch('sys.stdout'):
            self.assertTrue(wizard.wait_to_start(builtins.input, said.append, window, stdin))
        self.assertIn('Start pressed', said[0])

    def test_the_terminal_still_answers(self):
        window = mock.Mock(started=threading.Event())
        with self.pipe('maybe\nno\n') as stdin, mock.patch('sys.stdout'):
            self.assertFalse(wizard.wait_to_start(builtins.input, lambda line: None, window, stdin))
        with self.pipe('y\n') as stdin, mock.patch('sys.stdout'):
            self.assertTrue(wizard.wait_to_start(builtins.input, lambda line: None, window, stdin))
        with self.pipe('') as stdin, mock.patch('sys.stdout'):      # Ctrl+D
            self.assertFalse(wizard.wait_to_start(builtins.input, lambda line: None, window, stdin))

    def test_without_a_window_or_in_tests_the_plain_question_is_asked(self):
        answers = iter(['yes'])
        self.assertTrue(wizard.wait_to_start(lambda question: next(answers), lambda line: None, None))
        answers = iter(['no'])
        self.assertFalse(wizard.wait_to_start(lambda question: next(answers), lambda line: None, mock.Mock()))


if __name__ == '__main__':
    unittest.main()
