"""0.50.0, the wizard (P09 and P08 in the setup window): the finishing page is a page, not
the terminal's text in a box, and no page of the window tells a person to type a command
where it has a button. The finishing page has a card for each app (address, how it
stands, Open, Show login, a test that can be left for later); the phone page's app steps
have the address with its QR code and Show login."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import finish_page  # noqa: E402
import finish_states  # noqa: E402
import guided_setup  # noqa: E402
import phone_guide  # noqa: E402
import setup_gui  # noqa: E402
import setup_progress  # noqa: E402
import wizard  # noqa: E402

DONE = {'state': 'done', 'reason': None, 'action': None}
SUMMARY = {
    'outcome': 'pending',
    'apps': [
        {
            'id': 'jellyfin', 'name': 'Jellyfin', 'url': 'https://neko.example.ts.net:8442', 'local_url': '',
            'test': 'play_video',
            'states': {
                'installed': DONE, 'login': DONE, 'address': DONE,
                'tried': {'state': 'not_tested', 'reason': 'nobody_tried_yet', 'action': None},
            },
        },
        {
            'id': 'prowlarr', 'name': 'Prowlarr', 'url': '', 'local_url': 'http://127.0.0.1:9696', 'test': None,
            'states': {
                'installed': DONE, 'address': DONE,
                'login': {'state': 'failed', 'reason': 'saved_login_refused', 'action': None},
            },
        },
    ],
    'server': {'backup': DONE, 'restore': {'state': 'not_tested', 'reason': 'nobody_tried_yet', 'action': None}},
    'tests': [
        {'id': 'play_video', 'app': 'jellyfin', 'state': 'not_tested', 'reason': 'nobody_tried_yet'},
        {'id': 'tv_picture', 'state': 'waiting', 'reason': 'you_chose_later'},
    ],
    'attention': [],
    'pending': [
        {'id': 'step:gateway_tailnet', 'state': 'waiting', 'reason': 'step_waits_for_you'},
        {'id': 'login:prowlarr', 'state': 'failed', 'reason': 'saved_login_refused'},
    ],
}  # fmt: skip
CONFIG = {'dashboard': {'port': 8787}, 'data_mount': '/srv/nekoserver'}


class PageTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            self.page = finish_page.page(CONFIG, SUMMARY, pin_set=True)

    def test_the_heading_says_the_real_outcome(self):
        self.assertEqual(self.page['heading'], 'Your apps are installed. Some setup is still waiting for you.')
        ready = {**SUMMARY, 'outcome': 'ready', 'pending': []}
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            self.assertEqual(finish_page.page(CONFIG, ready)['heading'], 'NekoServer is ready.')
        self.assertIsNone(finish_page.page(CONFIG, None))

    def test_each_app_has_its_address_its_states_its_login_and_its_test(self):
        jellyfin, prowlarr = self.page['apps']
        self.assertEqual(jellyfin['url'], 'https://neko.example.ts.net:8442')
        self.assertEqual(
            [(s['label'], s['state'], s['words']) for s in jellyfin['states']],
            [('Installed', 'done', 'done'), ('Signed in', 'done', 'done'), ('Opens', 'done', 'done')],
        )
        self.assertEqual(jellyfin['login'], 'jellyfin')  # what Show login fetches
        self.assertEqual(jellyfin['test']['title'], 'Play a video in Jellyfin')
        self.assertEqual(jellyfin['test']['words'], 'not tried yet (nobody has tried this yet)')
        # An app without a saved login or a test has no such button; its address at home is shown.
        self.assertEqual((prowlarr['url'], prowlarr['login'], prowlarr['test']), ('http://127.0.0.1:9696', '', None))
        self.assertEqual(prowlarr['states'][1]['words'], 'needs fixing (the app did not take the saved login)')

    def test_what_waits_is_named_and_the_rest_is_in_plain_words(self):
        self.assertEqual(
            self.page['waiting'],
            [
                wizard.title('gateway_tailnet') + ': waiting (waiting for something only you can do)',
                'Prowlarr: needs fixing (the app did not take the saved login)',
            ],
        )
        self.assertEqual([test['id'] for test in self.page['tests']], ['tv_picture'])  # no app's card has it
        self.assertEqual([state['label'] for state in self.page['backups']], ['Backup made', 'Restore tried'])
        self.assertEqual(self.page['dashboard']['url'], 'http://127.0.0.1:8787')
        self.assertIn('A dashboard PIN is set', self.page['dashboard']['pin'])

    def test_no_command_is_on_the_page(self):
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            both = [self.page, finish_page.page(CONFIG, SUMMARY, pin_set=False)]
        text = json.dumps(both)
        for command in ('sudo ', 'account-setup', 'install.sh', '/tmp/', '--recover'):
            self.assertNotIn(command, text, command)
        self.assertIn('Run setup again to choose one', text)

    def test_the_buttons_it_has_are_the_only_ones_answered(self):
        self.assertEqual(finish_page.offered(self.page), ({'jellyfin'}, {'play_video', 'tv_picture'}))
        self.assertEqual(finish_page.offered(None), (set(), set()))


class WindowTests(unittest.TestCase):
    """The window's server answers the finishing page's buttons, and nothing else."""

    def setUp(self):
        self.window = setup_gui.Window.__new__(setup_gui.Window)
        self.window.config = dict(CONFIG)
        self.window._saved = lambda: dict(CONFIG)
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            page = finish_page.page(CONFIG, SUMMARY)
        finished = mock.patch.object(setup_progress, 'last', return_value={'page': page, 'lines': []})
        finished.start()
        self.addCleanup(finished.stop)
        nothing_asked = mock.patch.object(setup_progress, 'pending_question', return_value=None)
        nothing_asked.start()
        self.addCleanup(nothing_asked.stop)

    def test_show_login_works_for_an_app_of_the_page_only(self):
        import account_store

        saved = {'username': 'neko', 'password': 'p' * 12}
        with (
            mock.patch.object(account_store, 'get', return_value=saved) as get,
            mock.patch.object(guided_setup, 'qr_image', return_value=''),
        ):
            status, body, _ = self.window.login('jellyfin')
            self.assertEqual((status, json.loads(body)['username']), (200, 'neko'))
            get.assert_called_once()
            self.assertEqual(self.window.login('nextcloud')[0], 403)  # not on the page

    def test_a_tests_answer_is_kept_and_anything_else_is_refused(self):
        with mock.patch.object(finish_states, 'record_test') as kept:
            self.assertEqual(self.window.tried('play_video', 'later')[0], 200)
            kept.assert_called_once_with(CONFIG, 'play_video', 'later')
            self.assertEqual(self.window.tried('cloud_file', 'seen')[0], 403)  # not a test of this page
        with mock.patch.object(finish_states, 'record_test', side_effect=ValueError('Not an answer')):
            status, body, _ = self.window.tried('play_video', 'whatever')
        self.assertEqual((status, json.loads(body)['ok']), (409, False))


class StayOpenTests(unittest.TestCase):
    def test_setup_stays_for_the_pages_buttons_until_enter_and_never_waits_unattended(self):
        said = []
        terminal = mock.Mock(isatty=lambda: True, readline=mock.Mock(return_value='\n'))
        wizard.keep_window_open(said.append, terminal)
        terminal.readline.assert_called_once()
        self.assertIn('Press Enter here when you are done with it.', said[-1])
        nobody = io.StringIO('')
        wizard.keep_window_open(said.append, nobody)  # not a terminal: returns at once
        self.assertEqual(len(said), 2)


class PhoneStepTests(unittest.TestCase):
    """The phone page in the window: each app's step is short, with the address, its QR
    code and Show login; the terminal keeps its lines, command included."""

    def test_the_windows_step_has_buttons_where_the_terminal_has_a_command(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'data_mount': folder}
            window = phone_guide.window_step(config, 'jellyfin', 'https://neko.example.ts.net:8442')
            terminal = phone_guide.instructions(config, 'jellyfin', 'https://neko.example.ts.net:8442', asks=False)
        self.assertEqual(window['address'], 'https://neko.example.ts.net:8442')
        self.assertEqual(window['login'], 'jellyfin')
        self.assertEqual(len(window['lines']), 2)
        self.assertNotIn('account-setup', json.dumps(window))
        self.assertNotIn('terminal', json.dumps(window))
        self.assertIn('account-setup.py show jellyfin', ' '.join(terminal))  # the terminal's own way stays

    def test_the_part_page_sends_the_windows_version_of_a_step(self):
        step = {
            'id': 'jellyfin', 'title': 'Jellyfin', 'lines': ['a', 'run  sudo python3 x/account-setup.py show jellyfin'],
            'window': {'lines': ['Install it.'], 'address': 'https://x:8442', 'login': 'jellyfin'},
        }  # fmt: skip
        with mock.patch.object(guided_setup, 'qr_image', return_value='data:image/svg+xml;base64,AAAA') as code:
            shown = guided_setup.window_step(step, False)
        code.assert_called_once_with('https://x:8442')
        self.assertEqual(
            shown,
            {
                'id': 'jellyfin', 'title': 'Jellyfin', 'lines': ['Install it.'], 'done': False,
                'address': 'https://x:8442', 'qr': 'data:image/svg+xml;base64,AAAA', 'login': 'jellyfin',
            },
        )  # fmt: skip
        # A step with nothing of its own for the window is listed as the terminal lists it.
        plain = guided_setup.window_step({'id': 'away', 'title': 'Away', 'lines': ['x']}, True)
        self.assertEqual(plain, {'id': 'away', 'title': 'Away', 'lines': ['x'], 'done': True})

    def test_show_login_is_answered_for_a_step_of_the_part(self):
        import account_store

        window = setup_gui.Window.__new__(setup_gui.Window)
        window.config, window._saved = {}, lambda: {}
        asked = {'page': {'form': 'part', 'steps': [{'id': 'jellyfin', 'login': 'jellyfin'}, {'id': 'away'}]}}
        with (
            mock.patch.object(setup_progress, 'pending_question', return_value=asked),
            mock.patch.object(setup_progress, 'last', return_value=None),
            mock.patch.object(account_store, 'get', return_value={'username': 'neko', 'password': 'p' * 12}),
            mock.patch.object(guided_setup, 'qr_image', return_value=''),
        ):
            self.assertEqual(window.login('jellyfin')[0], 200)
            self.assertEqual(window.login('immich')[0], 403)


if __name__ == '__main__':
    unittest.main()
