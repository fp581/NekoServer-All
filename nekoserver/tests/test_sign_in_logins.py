"""0.50.0, item 9: "If the user asks you to make the password, then you ask him to log in
afterwards, how will the user know the passwords?" Every step that asks the person to sign
in to an app offers "Show login": in the terminal on the screen alone, in the setup window
as a button that fetches the login when pressed. A login shown this way is in no log, no
event and no report."""

import http.cookiejar
import json
import secrets
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_store  # noqa: E402
import guided_setup  # noqa: E402
import setup_gui  # noqa: E402
import setup_progress  # noqa: E402
import wizard  # noqa: E402

# Made when the tests run: nothing that looks like a real password is written here.
PLANTED = 'planted-' + secrets.token_hex(10)
STORE = {
    'jellyfin': {'username': 'neko_ab12', 'password': PLANTED, 'state': 'verified'},
    'immich': {'username': 'me@example.org', 'password': 'other-' + secrets.token_hex(10), 'state': 'verified'},
}


def saved(config, app, verified=False):
    return STORE.get(app)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(account_store, 'get', saved)
        patcher.start()
        self.addCleanup(patcher.stop)
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)

    def shown(self, apps, typed):
        """show_logins with these answers: (what went to the log's side, what went to the
        screen alone, the pages the window was given)."""
        answers, said, screen, pages = iter(typed), [], [], []

        def prompt(text):
            said.append(text)
            pages.append(setup_progress.page_shown())
            return next(answers)

        guided_setup.show_logins(
            {}, apps, prompt, said.append, screen.append, lambda text: screen.append(text), lambda text: 'QR'
        )
        return said, screen, pages

    def test_the_login_is_offered_and_shown_on_the_screen_alone(self):
        said, screen, pages = self.shown(['jellyfin', 'immich', 'dashboard'], ['9', '1', ''])
        self.assertEqual(said[0], 'Your login for this step is saved on this server (never in a log):')
        self.assertEqual(
            said[1:3], ['  1) Jellyfin administrator: neko_ab12', '  2) Immich administrator: me@example.org']
        )
        self.assertIn('Show a login now? Type its number (Enter alone: go on): ', said)
        self.assertIn('Type a number from 1 to 2, or press Enter alone to go on.', said)
        self.assertIn('The login is hidden again.', said)
        # The password and its QR code went to the screen, then the screen was cleared.
        self.assertIn(f'Password: {PLANTED}', screen[0])
        self.assertIn('Username: neko_ab12', screen[0])
        self.assertIn('QR', screen[0])
        self.assertEqual(screen[-1], guided_setup.CLEAR_SCREEN)
        # Nothing of it on the side that is logged, nor in what the window is given.
        self.assertNotIn(PLANTED, '\n'.join(said))
        self.assertNotIn(PLANTED, json.dumps(pages))
        self.assertEqual(pages[0]['form'], 'logins')
        self.assertEqual(
            pages[0]['apps'],
            [
                {'id': 'jellyfin', 'title': 'Jellyfin administrator', 'username': 'neko_ab12'},
                {'id': 'immich', 'title': 'Immich administrator', 'username': 'me@example.org'},
            ],
        )

    def test_the_screen_is_cleared_even_when_the_wait_is_cut_off(self):
        screen = []

        def cut(text):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            guided_setup.show_logins(
                {}, ['jellyfin'], lambda text: '1', lambda line: None, screen.append, cut, lambda text: ''
            )
        self.assertEqual(screen[-1], guided_setup.CLEAR_SCREEN)

    def test_no_saved_login_means_no_question(self):
        asked = mock.Mock(side_effect=AssertionError('nothing to ask'))
        guided_setup.show_logins({}, ['sonarr', 'dashboard'], asked, asked)
        guided_setup.show_logins({}, [], asked, asked)
        # The store cannot be read (no SSD yet): no question, and no error either.
        with mock.patch.object(
            account_store, 'get', side_effect=RuntimeError('Account storage must lie on the app SSD')
        ):
            self.assertEqual(guided_setup.saved_logins({}, ['jellyfin']), [])

    def test_a_sign_in_step_offers_its_logins_before_asking_whether_it_is_done(self):
        order = []
        ctx = SimpleNamespace(config={}, apply=True)
        with (
            mock.patch.object(guided_setup, 'interactive', return_value=True),
            mock.patch.object(guided_setup, 'open_targets'),
            mock.patch.object(guided_setup, 'show_logins', side_effect=lambda *a, **k: order.append(('logins', a[1]))),
            mock.patch.object(guided_setup, 'yes_no', side_effect=lambda *a, **k: order.append('done?') or True),
        ):
            self.assertTrue(
                guided_setup.checkpoint(
                    ctx, 'Finish Jellyfin', ['Sign in.'], output=lambda line: None, logins=['jellyfin']
                )
            )
        self.assertEqual(order, [('logins', ['jellyfin']), 'done?'])

    def test_the_words_for_the_terminal_alone_are_in_no_event(self):
        import io

        real = io.StringIO()
        with mock.patch('sys.stdout', real), setup_progress.watching_prompts():
            print('a line the window may show')
            setup_progress.screen_only(f'  Password: {PLANTED}\n')
            self.assertIn(PLANTED, real.getvalue())  # the person saw it
            self.assertNotIn(PLANTED, json.dumps(setup_progress.recent_lines(40)))
            self.assertIn('a line the window may show', setup_progress.recent_lines(40))
            record = setup_progress.question('Have you completed this step successfully? [yes/no]: ')
            self.assertNotIn(PLANTED, json.dumps(record))
            setup_progress.answered()
            # "Press Enter to hide it" is the terminal's alone: the window is not asked.
            with mock.patch.dict(setup_progress._plain, {'input': lambda prompt: ''}):
                setup_progress.ask_terminal('  Press Enter to hide it: ')
            self.assertIsNone(setup_progress.pending_question())
        self.assertNotIn(
            PLANTED, json.dumps([{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()])
        )

    def test_the_qr_code_is_made_from_qrencodes_input_never_its_command_line(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs.get('input')))
            return mock.Mock(returncode=0, stdout='<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>')

        with (
            mock.patch.object(guided_setup.shutil, 'which', return_value='/usr/bin/qrencode'),
            mock.patch.object(guided_setup.subprocess, 'run', side_effect=run),
        ):
            picture = guided_setup.qr_image(PLANTED)
            guided_setup.terminal_qr(PLANTED)
        self.assertTrue(picture.startswith('data:image/svg+xml;base64,'))
        for argv, given in calls:
            self.assertEqual(given, PLANTED)
            self.assertNotIn(PLANTED, ' '.join(argv))
        with mock.patch.object(guided_setup.shutil, 'which', return_value=None):
            self.assertEqual((guided_setup.qr_image(PLANTED), guided_setup.terminal_qr(PLANTED)), ('', ''))


class WindowTests(unittest.TestCase):
    """The window's "Show login": fetched on request, by the window's own page only."""

    def setUp(self):
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in (
            ((setup_gui, 'family_packages'), {'names': lambda n: [n], 'vulkan': []}),
            ((guided_setup, 'qr_image'), 'data:image/svg+xml;base64,QR'),
        ):
            patcher = mock.patch.object(*target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(account_store, 'get', saved)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = Path(self.tmp.name) / 'my-config.json'
        settings.write_text('{}')
        self.window = setup_gui.Window(settings, {}, wizard.STEPS, [])
        self.addCleanup(self.window.stop)
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.browser.open(self.window.url, timeout=5).read()

    def post(self, route, payload, browser=None):
        request = urllib.request.Request(
            self.window.url + route,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with (browser or self.browser).open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as refused:
            body = refused.read()
            refused.close()
            return refused.code, body

    def sign_in_step(self):
        page = setup_progress.page('logins', {'answer': ''}, apps=guided_setup.saved_logins({}, ['jellyfin']))
        page.__enter__()
        self.addCleanup(page.__exit__, None, None, None)
        setup_progress.page_asks('answer')
        return setup_progress.question('Show a login now? Type its number (Enter alone: go on): ')

    def test_show_login_gives_the_login_of_the_step_under_way(self):
        self.sign_in_step()
        status, login = self.post('api/login', {'app': 'jellyfin'})
        self.assertEqual(status, 200)
        self.assertEqual(
            login, {'ok': True, 'username': 'neko_ab12', 'password': PLANTED, 'qr': 'data:image/svg+xml;base64,QR'}
        )
        # Asked for and gone: it is in no event, and not in what the window polls.
        events = json.dumps([{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()])
        with self.browser.open(self.window.url + 'api/state', timeout=5) as response:
            state = response.read().decode()
        self.assertNotIn(PLANTED, events + state)
        self.assertIn('neko_ab12', state)  # the username is on the page, as in the phone guide

    def test_only_a_login_the_question_waiting_offers(self):
        # No sign-in step is asking: nothing is given, whatever is asked for.
        self.assertEqual(self.post('api/login', {'app': 'jellyfin'})[0], 403)
        self.sign_in_step()
        for app in ('immich', 'nextcloud', '', None, ['jellyfin'], '../jellyfin'):
            self.assertEqual(self.post('api/login', {'app': app})[0], 403, app)
        # The step is over: its login is no longer given.
        setup_progress.answered()
        self.assertEqual(self.post('api/login', {'app': 'jellyfin'})[0], 403)

    def test_only_the_windows_own_page_may_ask(self):
        self.sign_in_step()
        stranger = urllib.request.build_opener()  # no cookie of this window
        status, body = self.post('api/login', {'app': 'jellyfin'}, stranger)
        self.assertEqual(status, 403)
        self.assertNotIn(PLANTED.encode(), body)
        # And a GET never gives one.
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.browser.open(self.window.url + 'api/login?app=jellyfin', timeout=5)
        self.assertEqual(refused.exception.code, 404)
        refused.exception.close()

    def test_a_login_that_is_not_saved_says_so(self):
        offered = [{'id': 'navidrome', 'title': 'Navidrome administrator', 'username': 'x'}]
        with setup_progress.page('logins', {'answer': ''}, apps=offered):
            setup_progress.question('Show a login now? ')
            self.assertEqual(self.post('api/login', {'app': 'navidrome'})[0], 404)


class StepTests(unittest.TestCase):
    """Each step that asks the person to sign in names the logins it means."""

    def test_the_sign_in_steps_name_their_logins(self):
        from modules import accounts, gateway_tailnet

        seen = []

        def checkpoint(ctx, title, instructions, targets=(), **more):
            seen.append((title, list(more.get('logins', ())), ' '.join(instructions)))
            return True

        ctx = SimpleNamespace(config={'data_mount': '/srv/nekoserver'}, apply=True, log=lambda line: None)
        parts = []

        def part(ctx, title, steps, question, **more):
            parts.append((title, steps))
            return set()

        with mock.patch.object(guided_setup, 'checkpoint', checkpoint), mock.patch.object(guided_setup, 'part', part):
            accounts.finish_pending(ctx, ['jellyfin'])
            links = {'jellyfin': 'https://neko.ts.net:8443/', 'navidrome': 'https://neko.ts.net:8446/'}
            with (
                mock.patch.object(gateway_tailnet.phone_guide, 'write_page', return_value='/tmp/page.html'),
                mock.patch.object(account_store, 'get', saved),
            ):
                gateway_tailnet.phone_steps(ctx, {'links': links})
        by_title = {title: (logins, words) for title, logins, words in seen}
        self.assertEqual(by_title['Finish Jellyfin administrator'][0], ['jellyfin'])
        self.assertIn('"Show login" in the setup window', by_title['Finish Jellyfin administrator'][1])
        self.assertNotIn('APP-CREDENTIALS.txt', by_title['Finish Jellyfin administrator'][1])
        # The phone is one part (0.50.0): each of its steps names the login it means.
        [(title, steps)] = parts
        self.assertEqual(title, 'Set up your phone')
        logins = {step['title']: step.get('logins', []) for step in steps}
        self.assertEqual(logins['Jellyfin: movies and shows'], ['jellyfin'])
        # Navidrome's login on the phone is the one this server gives the person (item 11).
        self.assertEqual(logins['Navidrome: music'], ['navidrome_listener'])
        self.assertEqual(logins['Tailscale on the phone'], [])

    def test_a_login_setup_makes_is_said_with_how_to_see_it(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'data_mount': folder, 'accounts': {'enabled': True}}
            said = []
            with (
                mock.patch.object(account_store, 'load', return_value={'apps': {}, 'mode': None}),
                mock.patch.object(account_store, 'save'),
            ):
                account_store.collect(config, ['jellyfin'], prompt=lambda text: '1', output=said.append)
        made = next(line for line in said if 'Made and saved' in line)
        self.assertIn('"Show login" shows it', made)
        self.assertRegex(made, r'username \S+, with a strong password')
        self.assertEqual(
            len(
                [
                    line
                    for line in said
                    if 'password' in line.lower() and 'strong' not in line and 'not printed' not in line
                ]
            ),
            0,
        )


if __name__ == '__main__':
    unittest.main()
