"""The setup window: the words it shows, the events setup feeds it, its little server,
and the wizard's approval that the window can answer."""

import builtins
import contextlib
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
import neko_common  # noqa: E402
import setup_gui  # noqa: E402
import setup_progress  # noqa: E402
import wizard  # noqa: E402
from modules import tv  # noqa: E402


class ExplanationTests(unittest.TestCase):
    def test_every_step_and_package_has_its_words(self):
        self.assertEqual(set(explanations.STEP_EXPLANATIONS), set(wizard.STEPS))
        for name in host.APP_PACKAGES:
            self.assertIn(name, explanations.PACKAGES)
        for name in (
            'steam',
            'steam-devices',
            'vulkan-intel',
            'lib32-vulkan-intel',
            'vulkan-radeon',
            'lib32-vulkan-radeon',
            'firefox',
            'bluez',
            'bluez-utils',
            'openrgb',
            'smartmontools',
            *tv.vulkan_packages(['intel', 'amd']),
        ):
            self.assertIn(name, explanations.PACKAGES)
        for extra in explanations.EXTRAS:
            self.assertIn(extra['step'], wizard.STEPS)
            for name in extra['installs']:
                self.assertIn(name, explanations.PACKAGES)
        for text in (*explanations.STEP_EXPLANATIONS.values(), *explanations.PACKAGES.values()):
            self.assertLess(len(text), 160, text)  # one line, not an essay

    def test_extras_read_and_write_the_settings(self):
        config = {}
        extra = next(e for e in explanations.EXTRAS if e['id'] == 'tv')
        # Nothing saved: a server set up before has it, a new one is offered it off (an opt-in).
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            self.assertTrue(explanations.extra_state(config, extra))
        with mock.patch.object(neko_common, 'server_exists', return_value=False):
            self.assertFalse(explanations.extra_state(config, extra))
            self.assertTrue(explanations.extra_state({'tv': {'wanted': True}}, extra))
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
            self.assertEqual(setup_progress.events()[1]['step'], 'docker')  # recorded while docker ran
            self.assertIsNone(setup_progress.events()[3]['step'])
            self.assertEqual(setup_progress.events(since=3)[0]['kind'], 'step')
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e['seq'] for e in lines], [1, 2, 3, 4])
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')

    def test_questions_are_seen_while_they_wait_and_never_their_answers(self):
        # stdin is not a terminal here: the plain input() answers, as in any script.
        setup_progress.enable()
        seen = []
        with (
            mock.patch(
                'builtins.input',
                side_effect=lambda prompt='': seen.append(setup_progress.pending_question()) or 'secret answer',
            ),
            mock.patch('getpass.getpass', return_value='1234'),
            mock.patch('sys.stdin', mock.Mock(fileno=mock.Mock(side_effect=OSError))),
            mock.patch('sys.stdout'),
        ):
            with setup_progress.watching_prompts():
                print('Drives on this PC:')
                print('  1. sda  Samsung SSD')
                self.assertEqual(builtins.input('Choose number: '), 'secret answer')
                self.assertEqual(seen[0]['text'], 'Choose number:')
                self.assertEqual(seen[0]['form'], 'text')
                # (The last lines: a thread of another test may print late, and the
                # terminal's last lines are what the context keeps anyway.)
                self.assertEqual(seen[0]['context'][-2:], ['Drives on this PC:', '  1. sda  Samsung SSD'])
                self.assertIsNone(setup_progress.pending_question())
                # A step's own block is the context, without the installer's log lines.
                print('[2026-10-24T12:21:53+00:00] START tailscale')
                print('=== Tailscale: connect this server ===')
                print('1. Open the sign-in page on any device.')
                builtins.input('Have you completed this step successfully? [yes/no]: ')
                self.assertEqual(
                    seen[1]['context'][-2:],
                    ['=== Tailscale: connect this server ===', '1. Open the sign-in page on any device.'],
                )
                self.assertNotIn('Drives on this PC:', seen[1]['context'])  # the earlier question's lines are gone
                self.assertEqual(seen[1]['form'], 'yesno')
                # The installer's time-stamped lines are what a drive question is about:
                # they stay, without their stamps; its step markers do not.
                setup_progress.step('storage', 'running')
                print('[2026-10-24T21:32:16+00:00] Device sde: 86G / Virtual Disk')
                print('[2026-10-24T21:32:16+00:00] Select the SSD (apps, photos and music):')
                print('[2026-10-24T21:32:16+00:00]   1) /dev/sde 86G ext4 UUID=70a9 mounted=-')
                builtins.input('Choose number (or ENTER to abort): ')
                self.assertEqual(
                    seen[2]['context'],
                    [
                        'Device sde: 86G / Virtual Disk',
                        'Select the SSD (apps, photos and music):',
                        '  1) /dev/sde 86G ext4 UUID=70a9 mounted=-',
                    ],
                )
                builtins.input('Type sde to confirm it is the SSD: ')
                self.assertEqual(seen[3]['context'], [])  # nothing new was said since the last answer
                import getpass

                self.assertEqual(getpass.getpass('PIN: '), '1234')
                self.assertEqual(setup_progress.question_kind('Ready? [yes/no]: ', False), 'yesno')
        # Without the times: a timestamp's digits can hold "1234" (seen on CI).
        texts = json.dumps([{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()])
        self.assertNotIn('secret answer', texts)
        self.assertNotIn('1234', texts)
        self.assertEqual(
            [e['kind'] for e in setup_progress.events()],
            ['question', 'answered'] * 2 + ['step'] + ['question', 'answered'] * 3,
        )
        hidden = next(e for e in setup_progress.events() if e['kind'] == 'question' and e['secret'])
        # Its prompt is shown (seen on a VM: "PIN:" and "Same PIN again:" both read "a hidden
        # entry" in the window, so the repeat looked like the first ask); its answer never is.
        self.assertEqual((hidden['text'], hidden['form']), ('PIN:', 'secret'))
        self.assertIs(builtins.input, builtins.__dict__['input'])  # restored afterwards

    def test_a_question_asked_again_keeps_its_list(self):
        """Seen in the window on a VM: after a wrong number, the window showed only "Choose
        another one from the list", without the list. The same words asking a new question
        (the three "where should ... be kept" questions) keep only their own list."""
        setup_progress.enable()
        seen = []
        with (
            mock.patch(
                'builtins.input', side_effect=lambda prompt='': seen.append(setup_progress.pending_question()) or '1'
            ),
            mock.patch('sys.stdin', mock.Mock(fileno=mock.Mock(side_effect=OSError))),
            mock.patch('sys.stdout'),
        ):
            with setup_progress.watching_prompts():
                print('Now choose the HDD (movies, shows, Nextcloud files and backups).')
                print('  1) sdc: 32 GB  - too small, needs 54 GB')
                print('  2) sdd: 256 GB')
                builtins.input('Number of the HDD (Enter alone stops setup here): ')
                print('sdc is 32 GB, too small for the HDD. Choose another one from the list.')
                builtins.input('Number of the HDD (Enter alone stops setup here): ')
                self.assertEqual(
                    seen[1]['context'][-4:],
                    [
                        'Now choose the HDD (movies, shows, Nextcloud files and backups).',
                        '  1) sdc: 32 GB  - too small, needs 54 GB',
                        '  2) sdd: 256 GB',
                        'sdc is 32 GB, too small for the HDD. Choose another one from the list.',
                    ],
                )
                for what in ('photos', 'music'):
                    print(f'Where should the {what} be kept?')
                    print('  1) the media drive: 255 GB free (recommended)')
                    print('  2) the SSD: 63 GB free, faster')
                    builtins.input('Number (Enter for the recommended one): ')
                self.assertEqual(seen[3]['context'][0], 'Where should the music be kept?')
                self.assertNotIn('Where should the photos be kept?', seen[3]['context'])

    def test_the_window_or_the_terminal_answers_whichever_is_first(self):
        import pty

        setup_progress.enable()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        terminal = os.fdopen(slave, 'r')
        self.addCleanup(terminal.close)
        with mock.patch('sys.stdout'):
            # The window answers: the terminal is told. A line typed in the terminal at the
            # same time, which lost the race, is dropped rather than answering the next question.
            record = setup_progress.question('Select the SSD. Choose number: ')
            os.write(master, b'7\n')
            self.assertFalse(setup_progress.answer_from_window(record['seq'] + 1, '9'))  # not the question waiting
            self.assertTrue(setup_progress.answer_from_window(record['seq'], '2'))
            self.assertFalse(setup_progress.answer_from_window(record['seq'], '3'))  # answered already
            self.assertEqual(setup_progress._wait_for_answer(terminal, False), '2')
            setup_progress.answered()
            # The terminal answers (the typed 7 is gone).
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

    def test_a_page_field_is_asked_once_and_the_windows_copy_is_never_reused(self):
        """0.50.0: a password chosen now is one page in the window (two hidden fields). The
        window sends both with the first question; a second try must ask again, not find
        the first try's entry still there."""
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        asked = []

        def ask(prompt):
            asked.append(prompt)
            record = setup_progress.question(prompt, secret=True)
            # The window answers this question with both fields, as its page posts them.
            sent = {'password': 'first-try', 'again': 'other'} if len(asked) == 1 else {'password': 'b', 'again': 'b'}
            setup_progress.answer_from_window(record['seq'], sent['password'], fields=sent)
            setup_progress.answered()
            return sent['password']

        with setup_progress.password_page('Backup password', least=8):
            shown = setup_progress.page_shown()
            self.assertEqual(
                shown,
                {
                    'form': 'new_password',
                    'fields': {'password': '', 'again': ''},
                    'asking': None,
                    'least': 8,
                    'what': 'Backup password',
                },
            )
            first = setup_progress.page_field('password', ask, 'Backup password: ')
            again = setup_progress.page_field('again', ask, 'Same backup password again: ')
            self.assertEqual((first, again), ('first-try', 'other'))
            self.assertEqual(asked, ['Backup password: '])  # the second entry came with the first
            # The second try: both asked for anew.
            self.assertEqual(setup_progress.page_field('password', ask, 'Backup password: '), 'b')
            self.assertEqual(setup_progress.page_field('again', ask, 'Same backup password again: '), 'b')
            self.assertEqual(len(asked), 2)
        # Neither entry is in any event.
        recorded = json.dumps([{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()])
        self.assertNotIn('first-try', recorded)
        self.assertNotIn('other', recorded)
        # Skip for now on the page leaves it as Ctrl+D does.
        with setup_progress.password_page('Dashboard PIN', optional=True):
            record = setup_progress.question('PIN: ', secret=True)
            setup_progress.answer_from_window(record['seq'], '', skip=True)
            setup_progress.answered()
            with self.assertRaises(EOFError):
                setup_progress.page_field('again', ask, 'Same PIN again: ')
        # Off a page, a field is simply asked.
        self.assertEqual(setup_progress.page_field('password', lambda prompt: 'typed', 'PIN: '), 'typed')

    def test_the_short_yes_or_no_gets_buttons_too(self):
        for text in ('Ready to start? [yes/no]: ', 'Does the NAS ask for a CHAP login? [y/N]: ', 'Go on? [Y/n] '):
            self.assertEqual(setup_progress.question_kind(text, False), 'yesno', text)
        for text in ('qBittorrent username [admin]: ', 'Which one? [1/2] (Enter alone: 1): ', 'Number: '):
            self.assertEqual(setup_progress.question_kind(text, False), 'text', text)
        self.assertEqual(setup_progress.question_kind('Delete it? [y/N]: ', True), 'secret')

    def test_the_typed_twice_prompts_are_pages_with_their_own_rules(self):
        import backup_secrets
        from modules import dashboard

        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        pages = []

        def typing(*answers):
            typed = iter(answers)

            def ask(prompt):
                pages.append(setup_progress.page_shown())
                return next(typed)

            return ask

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(backup_secrets, 'create_key') as made:
            backup_secrets.setup(typing('long-enough', 'long-enough'), lambda line: None, Path(folder))
        made.assert_called_once()
        self.assertEqual((pages[0]['what'], pages[0]['least'], pages[0]['asking']), ('Backup password', 8, 'password'))
        self.assertEqual(pages[1]['asking'], 'again')
        pages.clear()
        saved = mock.Mock(returncode=0)
        with (
            mock.patch.object(dashboard, 'CONTROL_CONFIG', Path('/nonexistent-control')),
            mock.patch.object(dashboard.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(dashboard.subprocess, 'run', return_value=saved) as run,
            mock.patch('builtins.print'),
        ):
            self.assertTrue(dashboard.choose_pin('/bin/runtime', lambda line: None, typing('123456', '123456')))
        rules = {k: pages[0][k] for k in ('what', 'least', 'most', 'digits', 'optional')}
        self.assertEqual(rules, {'what': 'Dashboard PIN', 'least': 6, 'most': 8, 'digits': True, 'optional': True})
        # The PIN goes to the control service on its input, never on its command line.
        self.assertEqual(run.call_args.kwargs['input'], '123456\n')
        self.assertNotIn('123456', json.dumps(run.call_args.args))

    def test_a_long_list_is_shown_whole_to_the_window(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location('media_connect_pick', ROOT / 'media-connect.py')
        connect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(connect)
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        items = [{'name': f'Indexer {n}'} for n in range(1, 41)]
        pages = []

        def ask(prompt):
            pages.append(setup_progress.page_shown())
            return '37'

        with mock.patch('builtins.print'):
            self.assertEqual(connect.choose(items, 'Select an indexer', ask), items[36])
        page = pages[0]
        self.assertEqual((page['form'], page['asking'], page['what']), ('pick', 'choice', 'Select an indexer'))
        self.assertEqual(len(page['items']), 40)  # every row, not the terminal's last lines
        self.assertEqual(page['items'][36], {'label': 'Indexer 37'})
        self.assertIsNone(setup_progress.page_shown())  # over with the question


class WindowTests(unittest.TestCase):
    def setUp(self):
        setup_progress.enable()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / 'my-config.json'
        self.config = {'profiles': {'full': ['system', 'docker', 'tv', 'maintenance']}, 'tv': {'user': 'auto'}}
        self.settings.write_text(json.dumps(self.config))
        # NekoServer's own package names, whatever Linux runs the tests (WindowPackageTests
        # checks the names of each family).
        packages = mock.patch.object(
            setup_gui,
            'family_packages',
            return_value={'names': lambda name: [name], 'vulkan': ['vulkan-intel', 'lib32-vulkan-intel']},
        )
        packages.start()
        self.addCleanup(packages.stop)
        self.window = setup_gui.Window(self.settings, self.config, wizard.STEPS, self.config['profiles']['full'])
        self.addCleanup(self.window.stop)
        self.addCleanup(setup_progress.disable)
        # The window's browser: it keeps the cookie the first page load gives it.
        self.browser = self.cookie_browser()
        self.browser.open(self.window.url, timeout=5).read()

    @staticmethod
    def cookie_browser():
        import http.cookiejar

        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, route, token=None, browser=None):
        url = self.window.url.replace(self.window.token, token or self.window.token) + route
        with (browser or self.browser).open(url, timeout=5) as response:
            return response.status, response.headers.get('Content-Type', ''), response.read()

    def post(self, route, payload):
        request = urllib.request.Request(
            self.window.url + route,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with self.browser.open(request, timeout=5) as response:
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
        for route, token in (
            ('', 'x' * 32),
            ('api/state', 'y' * 32),
            ('static/../wizard.py', None),
            ('wizard.py', None),
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(route, token)
            self.assertEqual(caught.exception.code, 404, route)

    def test_only_loading_the_page_claims_the_window(self):
        # Another account that read the address must not take the session with a request
        # of another kind before the window's browser loads the page.
        fresh = setup_gui.Window(self.settings, self.config, wizard.STEPS, self.config['profiles']['full'])
        self.addCleanup(fresh.stop)
        grab = urllib.request.Request(
            fresh.url, data=b'{}', headers={'Content-Type': 'application/json'}, method='POST'
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.cookie_browser().open(grab, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        browser = self.cookie_browser()
        with browser.open(fresh.url, timeout=5) as response:
            self.assertEqual(response.status, 200)
        # A request of another kind still never claims it, also once it is open.
        other = self.cookie_browser()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            other.open(grab, timeout=5)
        self.assertEqual(caught.exception.code, 403)

    @staticmethod
    def table_line(client, server, uid):
        """A line of /proc/net/tcp as the kernel writes it for IPv4."""

        def hex_of(address):
            host, port = address
            return f"{int.from_bytes(bytes(map(int, host.split('.'))), sys.byteorder):08X}:{port:04X}"

        return f'   1: {hex_of(client)} {hex_of(server)} 01 00000000:00000000 00:00000000 00000000 {uid:5d}        0 4242 1'

    def test_another_account_cannot_claim_the_window_first(self):
        """Another account that reads the address from the browser's command line and
        loads the page first is refused (its connection is not the desktop user's); the
        desktop user's browser, later, still gets the window."""
        import http.client

        table = Path(self.tmp.name) / 'tcp'
        fresh = setup_gui.Window(
            self.settings,
            self.config,
            wizard.STEPS,
            self.config['profiles']['full'],
            owners={0, 1000},
            tables=(table, Path(self.tmp.name) / 'missing'),
        )
        self.addCleanup(fresh.stop)
        port = fresh.server.server_address[1]

        def load_as(uid):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.connect()
            here = connection.sock.getsockname()
            table.write_text(
                '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n'
                + self.table_line(('127.0.0.1', port), here, 4321)
                + '\n'  # the server's own end
                + self.table_line(here, ('127.0.0.1', port), uid)
                + '\n'
            )
            connection.request('GET', '/t/' + fresh.token + '/')
            response = connection.getresponse()
            answer = response.status, response.getheader('Set-Cookie') or '', response.read()
            connection.close()
            return answer

        status, cookie, body = load_as(2000)
        self.assertEqual((status, cookie), (403, ''))
        self.assertIn(b'Only the account that runs setup', body)
        self.assertFalse(fresh.opened)
        status, cookie, _ = load_as(1000)
        self.assertEqual(status, 200)
        self.assertIn(setup_gui.COOKIE + '=' + fresh.session, cookie)
        status, cookie, body = load_as(2000)  # once it is open, still not another account
        self.assertEqual((status, cookie), (403, ''))
        self.assertIn(b'open somewhere else', body)
        first = fresh.session
        status, cookie, _ = load_as(0)  # root (setup itself) may open it again
        self.assertEqual(status, 200)
        self.assertNotEqual(fresh.session, first)
        self.assertIn(setup_gui.COOKIE + '=' + fresh.session, cookie)
        table.unlink()  # an owner that can't be told is refused too
        refused = setup_gui.Window(self.settings, self.config, wizard.STEPS, [], owners={0}, tables=(table,))
        self.addCleanup(refused.stop)
        browser, url = self.cookie_browser(), refused.url
        with self.assertRaises(urllib.error.HTTPError) as caught:
            browser.open(url, timeout=5)
        self.assertEqual(caught.exception.code, 403)

    def test_the_owner_of_a_connection_is_read_from_the_kernels_tables(self):
        client, server = ('127.0.0.1', 53412), ('127.0.0.1', 8787)
        if sys.byteorder == 'little':
            self.assertIn(' 0100007F:D0A4 0100007F:2253 ', self.table_line(client, server, 7))
        head = 'sl local rem st\n'
        with tempfile.TemporaryDirectory() as d:
            v4, v6 = Path(d) / 'tcp', Path(d) / 'tcp6'
            v4.write_text(
                head + self.table_line(server, client, 61234) + '\n' + self.table_line(client, server, 1000) + '\n'
            )
            self.assertEqual(setup_gui.peer_uid(client, server, (v4,)), 1000)
            self.assertEqual(setup_gui.peer_uid(server, client, (v4,)), 61234)
            self.assertIsNone(setup_gui.peer_uid(('127.0.0.1', 1), server, (v4,)))
            # IPv6 tables, with 127.0.0.1 written the IPv6 way (::ffff:127.0.0.1).
            raw = bytes(10) + b'\xff\xff' + bytes([127, 0, 0, 1])
            mapped = ''.join(f'{int.from_bytes(raw[i : i + 4], sys.byteorder):08X}' for i in range(0, 16, 4))
            v6.write_text(head + f'   0: {mapped}:D0A4 {mapped}:2253 01 0:0 0:0 0 1001 0 7 1\n   1: garbage\n')
            self.assertEqual(setup_gui.peer_uid(client, server, (Path(d) / 'none', v4.with_name('x'), v6)), 1001)
            self.assertEqual(setup_gui.peer_uid(('::ffff:127.0.0.1', 53412), server, (v6,)), 1001)
        with self.assertRaises(ValueError):
            setup_gui.table_address('0100:1F90')

    def test_the_expected_accounts_are_root_sudo_and_the_desktop_user(self):
        with (
            mock.patch.dict(os.environ, {'SUDO_UID': '1001'}),
            mock.patch.object(setup_gui.guided_setup, 'desktop_command', return_value=['runuser', '-u', 'root', '--']),
        ):
            self.assertEqual(setup_gui.expected_owners({}), {0, os.geteuid(), 1001})
        with (
            mock.patch.dict(os.environ, {'SUDO_UID': ''}),
            mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=RuntimeError('no desktop')),
        ):
            self.assertEqual(setup_gui.expected_owners({}), {0, os.geteuid()})

    def test_choosing_before_the_start_saves_the_settings_and_start_is_seen(self):
        state = self.state()
        self.assertEqual(state['phase'], 'choose')
        self.assertEqual([s['id'] for s in state['steps']], ['system', 'docker', 'tv', 'maintenance'])
        self.assertEqual(state['steps'][1]['title'], 'Docker')
        self.assertEqual(self.post('api/choose', {'tv': True}), {'ok': True})
        state = self.state()
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

    def refused(self, route, payload):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(route, payload)
        self.assertEqual(caught.exception.code, 409)
        return json.loads(caught.exception.read())

    def test_a_late_choice_after_the_start_cannot_change_the_saved_settings(self):
        self.post('api/start', {'choices': {'tv': False}})
        # The installer saves what it found (the SSD) while setup runs...
        saved = json.loads(self.settings.read_text())
        saved['storage'] = {'data_ssd': {'uuid': 'aaaa-1111'}}
        self.settings.write_text(json.dumps(saved))
        # ...then a stale switch or a second Start arrives from the window.
        self.assertFalse(self.refused('api/choose', {'tv': True})['ok'])
        self.assertIn('fixed', self.refused('api/start', {'choices': {'notifications': False}})['reason'])
        after = json.loads(self.settings.read_text())
        self.assertEqual(after, saved)
        self.assertFalse(after['tv']['wanted'])

    def test_a_settings_file_that_cannot_be_read_is_never_written_over(self):
        # The second audit: a mistake typed into my-config.json while the window was open
        # made a switch write the window's old copy over it, losing the person's edits.
        edited = self.settings.read_text().rstrip().rstrip('}') + ', "mine": 1,}'
        self.settings.write_text(edited)
        answer = self.refused('api/choose', {'tv': False})
        self.assertIn("my-config.json can't be read right now", answer['reason'])
        self.assertEqual(self.settings.read_text(), edited)  # left exactly as typed
        self.assertIn('id="choose-note"', (ROOT / 'setup-gui/index.html').read_text())

    def test_a_terminal_start_also_fixes_the_choices(self):
        self.window.freeze()
        self.assertFalse(self.window.started.is_set())
        self.assertEqual(self.state()['phase'], 'running')
        self.refused('api/choose', {'tv': False})
        self.refused('api/start', {})
        self.assertFalse(self.window.started.is_set())
        self.assertEqual(json.loads(self.settings.read_text()), self.config)

    def test_a_choice_keeps_what_the_file_gained_since_the_window_opened(self):
        saved = json.loads(self.settings.read_text())
        saved['storage'] = {'data_ssd': {'uuid': 'bbbb-2222'}}
        self.settings.write_text(json.dumps(saved))
        self.post('api/choose', {'tv': False})
        after = json.loads(self.settings.read_text())
        self.assertEqual(after['storage'], {'data_ssd': {'uuid': 'bbbb-2222'}})
        self.assertFalse(after['tv']['wanted'])

    def answered_from_the_window(self, work, answers):
        """`work` run while setup watches its prompts, on a terminal nobody types in: each
        question it asks is answered from the window with the next of `answers` (what the
        page posts). Returns (what work returned, the questions as the window showed them)."""
        import pty
        import time

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        terminal = os.fdopen(slave, 'r')
        self.addCleanup(terminal.close)
        result, shown, pending = [], [], list(answers)

        def run():
            with mock.patch('sys.stdin', terminal), mock.patch('sys.stdout'), setup_progress.watching_prompts():
                result.append(work())

        thread = threading.Thread(target=run)
        thread.start()
        last, deadline = 0, time.time() + 30
        while thread.is_alive() and time.time() < deadline:
            question = self.state()['question']
            if question and question['seq'] != last and pending:
                last = question['seq']
                shown.append(question)
                self.assertEqual(self.post('api/answer', {'seq': last, **pending.pop(0)}), {'ok': True})
            time.sleep(0.05)
        thread.join(5)
        self.assertFalse(thread.is_alive(), 'a question was left waiting')
        return result[0], shown

    def recorded(self):
        """Everything setup recorded for the window, and what the window shows now, as text."""
        events = [{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()]
        return json.dumps(events) + json.dumps(self.state())

    def test_a_program_of_setups_asks_its_questions_in_the_window_too(self):
        """0.50.0, item 8: connecting the media apps, the music setup and the shared-folder
        password ran as programs of their own, whose questions reached only the terminal
        ("Answer this step in the terminal"). They now run inside setup's process."""
        import secrets

        self.window.started.set()
        hidden = 'typed-' + secrets.token_hex(8)
        program = Path(self.tmp.name) / 'music-setup.py'
        program.write_text(
            'import getpass, sys\n'
            'print("Open http://127.0.0.1:4533 on the server.")\n'
            'user = input("Listener username: ").strip()\n'
            'password = getpass.getpass("Listener password (never saved as plaintext): ")\n'
            'sys.exit(0 if (user, password) == ("listener", sys.argv[2]) else 2)\n'
        )
        argv = list(sys.argv)
        code, shown = self.answered_from_the_window(
            lambda: setup_progress.run_program(program, ['--expect', hidden]), [{'text': 'listener'}, {'text': hidden}]
        )
        self.assertEqual(code, 0)  # both answers reached the program
        self.assertEqual(sys.argv, argv)
        self.assertEqual(
            [q['text'] for q in shown], ['Listener username:', 'Listener password (never saved as plaintext):']
        )
        self.assertIn('Open http://127.0.0.1:4533 on the server.', shown[0]['context'])  # what it printed
        self.assertEqual((shown[1]['secret'], shown[1]['kind']), (True, 'secret'))
        # A password typed in the window is in no event and not in what the window shows.
        self.assertNotIn(hidden, self.recorded())
        self.assertNotIn('terminal', self.state())  # nothing is handed off to the terminal any more
        self.assertFalse(hasattr(setup_progress, 'in_terminal'))

    def test_a_program_ends_as_it_would_on_its_own(self):
        program = Path(self.tmp.name) / 'program.py'
        for body, expected in (
            ('pass\n', 0),
            ('import sys\nsys.exit(3)\n', 3),
            ('import sys\nsys.exit()\n', 0),
            ('import sys\nsys.exit("Onboarding failed")\n', 1),
            ('raise RuntimeError("an error it did not handle")\n', 1),
            ('import argparse\nargparse.ArgumentParser().error("a wrong option")\n', 2),
        ):
            program.write_text(body)
            with self.subTest(body=body), mock.patch('sys.stderr'):
                self.assertEqual(setup_progress.run_program(program), expected)
        # Ctrl+C is setup's to handle, as when the program was one of its own.
        program.write_text('raise KeyboardInterrupt\n')
        with self.assertRaises(KeyboardInterrupt):
            setup_progress.run_program(program)

    def test_the_steps_run_their_programs_inside_setup(self):
        from modules import music, onboarding

        release = Path(self.tmp.name) / 'data/installer/releases' / ('v' + setup_gui.VERSION)
        release.mkdir(parents=True)
        (release / 'onboard.py').write_text('')
        data = str(Path(self.tmp.name) / 'data')
        config = {'data_mount': data, 'onboarding': {}, 'media': {'services': []}}
        ctx = mock.Mock(apply=True, plan=(), log=lambda line: None, config=config, config_path='/c.json')
        import recovery_tools

        with (
            mock.patch.object(onboarding.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(onboarding.account_store, 'enabled', return_value=False),
            mock.patch.object(onboarding.setup_progress, 'run_program', return_value=0) as connected,
            mock.patch.object(onboarding.subprocess, 'run', side_effect=AssertionError('a program of its own')),
            mock.patch.object(onboarding, 'witness_playback'),
            mock.patch.object(onboarding, 'remove_stale'),
            # The lock asks about this PC's own monthly update (its journal, its service): not this test's business.
            mock.patch.object(recovery_tools.neko_common, 'pending_maintenance', return_value=None),
        ):
            onboarding.run(ctx)
        script, options = connected.call_args.args
        self.assertEqual(Path(script), release / 'onboard.py')
        self.assertIn('--configure', options)
        with (
            mock.patch.object(music.os, 'geteuid', return_value=0),
            mock.patch('neko_common.verify_storage'),
            mock.patch.object(music.music_client, 'check', return_value={'status': 'needs_setup'}),
            mock.patch.object(music.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(music.setup_progress, 'run_program', return_value=1) as asked,
            mock.patch.object(music, 'atomic_json'),
        ):
            ctx.config = {'data_mount': data, 'media_mount': data + '-media', 'media': {'services': ['navidrome']}}
            music.run(ctx)
        asked.assert_called_once_with(release / 'music-setup.py', ['--config', '/c.json', '--configure'])

    def test_the_shared_folders_password_is_one_hidden_page_and_goes_to_samba_on_its_input(self):
        import secrets

        from modules import samba

        self.window.started.set()
        chosen = 'chosen-' + secrets.token_hex(8)
        calls = []

        def smbpasswd(argv, **kwargs):
            calls.append((argv, kwargs.get('input')))
            return mock.Mock(returncode=0)

        # The first try differs (the page checks that too, before it sends); the second matches.
        done, shown = self.answered_from_the_window(
            lambda: samba.samba_password('neko', run=smbpasswd),
            [{'text': chosen, 'fields': {'again': chosen + 'x'}}, {'text': chosen, 'fields': {'again': chosen}}],
        )
        self.assertTrue(done)
        self.assertEqual(
            shown[0]['page'],
            {
                'form': 'new_password',
                'fields': {'password': '', 'again': ''},  # never the values
                'asking': 'password',
                'what': 'Password for the shared folders (account neko)',
            },
        )
        self.assertTrue(shown[0]['secret'])
        self.assertEqual(len(shown), 2)  # "the same again" was never asked: the page sent both
        # Samba gets it on its input (-s), never on its command line.
        self.assertEqual(calls, [(['smbpasswd', '-s', '-a', 'neko'], f'{chosen}\n{chosen}\n')])
        self.assertNotIn(chosen, json.dumps([c[0] for c in calls]))
        self.assertNotIn(chosen, self.recorded())

    def test_every_question_goes_through_setup_from_its_first_line(self):
        """A function that took `input` as its default when its file was loaded
        (`def collect(prompt=input)`) asked in the terminal only, unless its file happened
        to be loaded after the window started watching. installer.py now makes input() and
        getpass() setup's own before any other file is loaded."""
        import subprocess

        check = (
            'import installer, builtins, getpass, setup_progress, account_store, guided_setup, backup_secrets\n'
            'assert builtins.input is setup_progress.asking_input\n'
            'assert getpass.getpass is setup_progress.asking_getpass\n'
            'assert account_store.collect.__defaults__[0] is setup_progress.asking_input\n'
            'assert account_store.collect.__defaults__[1] is setup_progress.asking_getpass\n'
            'assert guided_setup.ask_choice.__defaults__[0] is setup_progress.asking_input\n'
            'assert backup_secrets.setup.__defaults__[0] is setup_progress.asking_getpass\n'
            'print("ok")\n'
        )
        ran = subprocess.run([sys.executable, '-c', check], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((ran.returncode, ran.stdout.strip()), (0, 'ok'), ran.stderr)
        # Without a window watching they ask the plain way.
        with mock.patch.object(setup_progress, '_real_input', return_value='typed') as plain:
            self.assertEqual(setup_progress.asking_input('Name: '), 'typed')
        plain.assert_called_once_with('Name: ')

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
        self.assertEqual(
            (question['seq'], question['text'], question['secret'], question['kind']),
            (record['seq'], 'Select the SSD. Choose number:', False, 'text'),
        )
        self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '2'}), {'ok': True})
        self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '3'}), {'ok': False})  # taken already
        self.assertEqual(setup_progress._pending['answer'], '2')
        setup_progress.answered()
        # "Skip for now" in the window: the step is left pending, as Ctrl+D leaves it.
        record = setup_progress.question('Have you completed this step successfully? [yes/no]: ')
        self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '', 'skip': True}), {'ok': True})
        self.assertTrue(setup_progress._pending['skip'])
        with mock.patch('sys.stdout') as stdout:
            with self.assertRaises(EOFError):
                setup_progress._from_window(setup_progress._pending, False)
        self.assertIn('left for now, from the setup window', stdout.write.call_args[0][0])
        setup_progress.answered()
        # The page offers the button for that question alone.
        page = (ROOT / 'setup-gui/setup.js').read_text()
        self.assertIn("q.page?.form === 'confirm_step'", page)
        self.assertNotIn('Have you completed this step successfully?', page)  # each step asks its own (0.50.0)
        self.assertIn('Skip for now', (ROOT / 'setup-gui/index.html').read_text())
        setup_progress.step('docker', 'needs_setup', 'Docker is installed as a snap')
        setup_progress.step('tv', 'unavailable', 'no Steam package')
        setup_progress.event('finished', lines=['  NekoServer is almost ready', 'Done: 1 of 3 steps.'], complete=False)
        state = self.state()
        self.assertEqual(state['phase'], 'finished')
        self.assertEqual(state['finish'][1], 'Done: 1 of 3 steps.')
        self.assertEqual(state['steps'][1]['reason'], 'Docker is installed as a snap')
        # Counted as the terminal and the log count: done of the steps this PC can have.
        self.assertEqual((state['done'], state['total']), (1, 3))
        self.assertTrue(self.window.delivered.is_set())

    def test_a_page_of_questions_is_answered_whole(self):
        self.window.started.set()
        setup_progress.step('storage', 'running')
        shown = {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30'}
        with setup_progress.page('backup_times', shown, weekdays=['Mon', 'Sun'], night_end=6):
            setup_progress.page_asks('daily')
            record = setup_progress.question('When should the daily backups run? (HH:MM, Enter for 04:15): ')
            question = self.state()['question']
            self.assertEqual(
                question['page'],
                {
                    'form': 'backup_times',
                    'fields': shown,
                    'asking': 'daily',
                    'weekdays': ['Mon', 'Sun'],
                    'night_end': 6,
                },
            )
            fields = {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00', 'other': 'x'}
            self.assertEqual(
                self.post('api/answer', {'seq': record['seq'], 'text': '23:30', 'fields': fields}), {'ok': True}
            )
            self.assertEqual(setup_progress._pending['answer'], '23:30')
            setup_progress.answered()
            # The other fields answer their questions before they are asked; unknown ones are dropped.
            self.assertEqual(setup_progress.page_answer('weekly_day'), ('answer', 'Sat'))
            self.assertEqual(setup_progress.page_answer('weekly'), ('answer', '21:00'))
            self.assertEqual(setup_progress.page_answer('monthly'), ('answer', '01:00'))
            self.assertIsNone(setup_progress.page_answer('other'))
            self.assertIsNone(setup_progress.page_answer('monthly'), 'each answer is used once')
        # Off the page a question carries none.
        record = setup_progress.question('Select the SSD. Choose number: ')
        self.assertIsNone(self.state()['question']['page'])
        setup_progress.answered()
        # "Skip for now" on the page leaves every field at the time shown.
        with setup_progress.page('backup_times', shown):
            record = setup_progress.question('When should the daily backups run? (HH:MM, Enter for 04:15): ')
            self.assertEqual(self.post('api/answer', {'seq': record['seq'], 'text': '', 'skip': True}), {'ok': True})
            self.assertEqual(setup_progress.page_answer('weekly'), ('skip', None))
            setup_progress.answered()

    def test_a_body_too_large_or_broken_is_refused(self):
        for data, code in ((b'{' * 20, 400), (b'{"tv": false}' + b' ' * setup_gui.LARGEST_BODY, 413)):
            request = urllib.request.Request(
                self.window.url + 'api/choose', data=data, headers={'Content-Type': 'application/json'}, method='POST'
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.browser.open(request, timeout=5)
            self.assertEqual(caught.exception.code, code)

    def test_without_the_cookie_only_the_page_itself_opens_and_only_for_its_account(self):
        # Another account on this PC can read the address from the browser's command
        # line; once the window has opened it, the address alone lets nobody in but the
        # same account (or root), and then only by loading the page itself.
        other = self.cookie_browser()
        for route in ('api/state', 'static/setup.js'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(route, browser=other)
            self.assertEqual(caught.exception.code, 403, route)
        request = urllib.request.Request(
            self.window.url + 'api/answer', data=b'{"seq": 1, "text": "yes"}', method='POST'
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            other.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.get('')[0], 200)  # a reload in the window's own browser

    def test_a_closed_window_opens_again_from_the_same_account(self):
        """D2: the auto-opened window was closed (its browser lost the session cookie with
        it); pasting the address again, as the terminal says, opens it again. The older
        window's session ends and the page then says where the window is."""
        reopened = self.cookie_browser()
        status, kind, _ = self.get('', browser=reopened)
        self.assertEqual((status, kind), (200, 'text/html; charset=utf-8'))
        self.assertEqual(self.get('api/state', browser=reopened)[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get('api/state')  # the first window's cookie
        self.assertEqual(caught.exception.code, 403)
        page = caught.exception.read()
        self.assertTrue(page.startswith(b'<!doctype html>'))  # words that wrap, not a line of code
        self.assertIn(b'open somewhere else', page)

    def test_only_this_pcs_own_name_is_answered(self):
        request = urllib.request.Request(self.window.url + 'api/state', headers={'Host': 'evil.example:80'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.browser.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_a_broken_length_is_refused_quietly(self):
        import io
        import socket

        port = self.window.server.server_address[1]
        for length in ('abc', '-5'):
            with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:
                conn.sendall(
                    f'POST /t/{self.window.token}/api/choose HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                    f'Content-Length: {length}\r\n\r\n'.encode()
                )
                self.assertIn(b' 40', conn.recv(200))  # 400 (or 403 without the cookie), never a hang
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:
                conn.sendall(
                    f'GET /t/{self.window.token}/api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode()
                )
            import time

            time.sleep(0.3)
        self.assertNotIn('Traceback', err.getvalue())


class MenuWindowTests(unittest.TestCase):
    """0.50.0, item 8: the recovery menu and the uninstaller have the setup window too, when
    there is a desktop; the terminal keeps working, and is all there is without one."""

    def setUp(self):
        packages = mock.patch.object(setup_gui, 'family_packages', return_value={'names': lambda n: [n], 'vulkan': []})
        packages.start()
        self.addCleanup(packages.stop)
        self.addCleanup(setup_progress.disable)

    def browser_of(self, window):
        browser = WindowTests.cookie_browser()
        browser.open(window.url, timeout=5).read()
        return browser

    def state(self, window, browser):
        with browser.open(window.url + 'api/state', timeout=5) as response:
            return json.loads(response.read())

    def answer(self, window, browser, payload):
        request = urllib.request.Request(
            window.url + 'api/answer',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with browser.open(request, timeout=5) as response:
            return json.loads(response.read())

    def test_a_menu_is_followed_and_answered_in_the_window(self):
        import pty
        import time

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        terminal = os.fdopen(slave, 'r')
        self.addCleanup(terminal.close)
        said, picked, opened = [], [], []

        def menu():
            with mock.patch('sys.stdin', terminal), mock.patch('sys.stdout'):
                with setup_gui.session('NekoServer recovery', {}, said.append, lambda config, url: True) as window:
                    opened.append(window)
                    print('What do you want to get back?')
                    print('  1. Photos and videos (Immich)')
                    print('  2. Nextcloud files')
                    print('  0. Quit')
                    picked.append(builtins.input('Choose a number: '))

        thread = threading.Thread(target=menu)
        thread.start()
        deadline = time.time() + 20
        while not opened and time.time() < deadline:
            time.sleep(0.05)
        window = opened[0]
        browser = self.browser_of(window)
        shown = None
        while time.time() < deadline:
            shown = self.state(window, browser)
            if shown['question']:
                break
            time.sleep(0.05)
        # The window follows the menu: its name, the terminal's words, no steps or switches.
        self.assertEqual(shown['phase'], 'running')
        self.assertEqual(shown['session']['title'], 'NekoServer recovery')
        self.assertEqual(shown['steps'], [])
        self.assertEqual(shown['question']['text'], 'Choose a number:')
        self.assertEqual(
            shown['question']['context'][-3:], ['  1. Photos and videos (Immich)', '  2. Nextcloud files', '  0. Quit']
        )
        self.assertEqual(self.answer(window, browser, {'seq': shown['question']['seq'], 'text': '2'}), {'ok': True})
        thread.join(10)
        self.assertEqual(picked, ['2'])  # the menu got the window's answer
        self.assertIn('A window opened in your browser with the same questions', said[0])
        # Its switches are never there to press: a menu changes no settings file.
        self.assertTrue(window.frozen)

    def test_without_a_desktop_the_menu_is_the_terminal_alone(self):
        said = []
        with setup_gui.session('NekoServer recovery', {}, said.append, lambda config, url: False) as window:
            self.assertIsNone(window)
            # Asked the plain way: nothing watches the prompts.
            self.assertFalse(setup_progress._watching)
        self.assertEqual(said, [])
        # A window that cannot even be served changes nothing either.
        with mock.patch.object(setup_gui, 'Window', side_effect=OSError('no port')):
            with setup_gui.session('Removing NekoServer', {}, said.append) as window:
                self.assertIsNone(window)

    def test_the_recovery_menu_opens_it_only_when_a_person_runs_it(self):
        import recovery_wizard

        sessions = []

        @contextlib.contextmanager
        def session(title, config, output):
            sessions.append(title)
            yield None

        with (
            mock.patch.object(recovery_wizard.os, 'geteuid', return_value=0),
            mock.patch.object(recovery_wizard, 'stop_on_sigterm'),
            mock.patch.object(recovery_wizard, 'settings', return_value={}),
            mock.patch.object(recovery_wizard, 'menu_loop', return_value=0) as loop,
            mock.patch.object(setup_gui, 'session', session),
        ):
            # A prompt of someone's own (a test, another program): the menu alone.
            self.assertEqual(recovery_wizard.run(lambda text: '0', lambda line: None), 0)
            self.assertEqual(sessions, [])
            # The person's own terminal: with the window.
            self.assertEqual(recovery_wizard.run(builtins.input, lambda line: None), 0)
            self.assertEqual(sessions, ['NekoServer recovery'])
        self.assertEqual(loop.call_count, 2)

    def test_the_uninstaller_never_depends_on_the_window(self):
        import uninstaller

        with mock.patch.dict(sys.modules, {'setup_gui': None}):  # its file cannot be loaded
            with uninstaller.window_session({'data_mount': '/srv/nekoserver'}) as window:
                self.assertIsNone(window)
        seen = []

        @contextlib.contextmanager
        def session(title, config):
            seen.append((title, config))
            yield None

        with mock.patch.object(setup_gui, 'session', session):
            with uninstaller.window_session('not a settings object'):
                pass
        self.assertEqual(seen, [('Removing NekoServer', {})])


class WindowPlacementTests(unittest.TestCase):
    """The small window sits low on the screen and never off it; without a known screen
    size the desktop places it."""

    def test_low_on_the_screen_and_always_on_it(self):
        self.assertEqual(setup_gui.window_position((1920, 1080)), '60,540')
        self.assertEqual(setup_gui.window_position((1366, 768)), '60,228')
        self.assertEqual(setup_gui.window_position((1024, 600)), '0,60')  # narrow and short: as far up as needed
        self.assertEqual(setup_gui.window_position((800, 480)), '0,0')

    def test_the_screen_size_comes_from_xrandr_or_is_unknown(self):
        xrandr_says = 'Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 16384 x 16384\n'
        with (
            mock.patch.object(setup_gui, 'find_program', return_value='/usr/bin/xrandr'),
            mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=lambda config, argv: argv),
        ):
            ok = lambda argv, **kwargs: mock.Mock(returncode=0, stdout=xrandr_says)  # noqa: E731
            self.assertEqual(setup_gui.screen_size({}, run=ok), (2560, 1440))
            failed = lambda argv, **kwargs: mock.Mock(returncode=1, stdout='')  # noqa: E731
            self.assertIsNone(setup_gui.screen_size({}, run=failed))
            self.assertIsNone(setup_gui.screen_size({}, run=mock.Mock(side_effect=OSError)))
        with mock.patch.object(setup_gui, 'find_program', return_value=None):
            self.assertIsNone(setup_gui.screen_size({}))
        with (
            mock.patch.object(setup_gui, 'find_program', return_value='/usr/bin/xrandr'),
            mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=RuntimeError('no desktop')),
        ):
            self.assertIsNone(setup_gui.screen_size({}))


class ReadyToStartTests(unittest.TestCase):
    """The terminal's one approval and the window's Install button answer the same question."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'my-config.json'
        self.path.write_text('{}')
        new = mock.patch.object(neko_common, 'server_exists', return_value=False)
        new.start()
        self.addCleanup(new.stop)

    def pipe(self, text=''):
        read_end, write_end = os.pipe()
        if text:
            os.write(write_end, text.encode())
        os.close(write_end)
        self.addCleanup(lambda: None)
        return os.fdopen(read_end)

    def window(self):
        window = mock.Mock(started=threading.Event())
        window.choose.return_value = True
        return window

    def test_install_in_the_window_counts_as_yes(self):
        window = self.window()
        window.started.set()
        said = []
        with self.pipe() as stdin, mock.patch('sys.stdout'):
            self.assertTrue(wizard.wait_to_start(self.path, builtins.input, said.append, window, stdin))
        self.assertIn('pressed in the setup window', said[-1])
        window.choose.assert_not_called()  # the window saved the switches itself

    def test_the_terminal_still_answers(self):
        window = self.window()
        said = []
        with self.pipe('maybe\nno\n') as stdin, mock.patch('sys.stdout'):
            self.assertFalse(wizard.wait_to_start(self.path, builtins.input, said.append, window, stdin))
        self.assertIn('Type the number of a line (1 to 11), press Enter to go on, or type no to stop.', said)
        window.choose.assert_not_called()
        with self.pipe('\ny\n') as stdin, mock.patch('sys.stdout'):
            self.assertTrue(wizard.wait_to_start(self.path, builtins.input, lambda line: None, window, stdin))
        # Yes saves every switch as it stands and fixes them, through the window.
        window.choose.assert_called_once_with({}, start=True)
        with self.pipe('') as stdin, mock.patch('sys.stdout'):  # Ctrl+D
            self.assertFalse(wizard.wait_to_start(self.path, builtins.input, lambda line: None, window, stdin))
        # Enter alone approves nothing.
        with self.pipe('\n\nno\n') as stdin, mock.patch('sys.stdout'):
            self.assertFalse(wizard.wait_to_start(self.path, builtins.input, said.append, window, stdin))
        self.assertIn('Enter alone does nothing here.', said[-1])

    def test_a_number_switches_that_line_through_the_window(self):
        window = self.window()
        with self.pipe('2 7\nno\n') as stdin, mock.patch('sys.stdout'):
            self.assertFalse(wizard.wait_to_start(self.path, builtins.input, lambda line: None, window, stdin))
        # Line 2 (Movies and shows) was on, line 7 (a TV) off on a new server.
        window.choose.assert_called_once_with({'media_movies': False, 'tv': True}, start=False)

    def test_without_a_window_or_in_tests_the_plain_question_is_asked(self):
        answers = iter(['', 'yes'])
        asked = []
        self.assertTrue(
            wizard.wait_to_start(self.path, lambda question: asked.append(question) or next(answers), print, None)
        )
        self.assertIn('Install selected apps? Type yes to go on, back to change your choices, or no to stop', asked[1])
        self.assertNotIn('setup window', asked[0] + asked[1])
        answers = iter(['no'])
        self.assertFalse(
            wizard.wait_to_start(self.path, lambda question: next(answers), lambda line: None, mock.Mock())
        )


# The connecting step asks Docker first (modules/onboarding.docker_running); these tests
# run it on a PC that may have no Docker, so Docker answers here.
_DOCKER = None


def setUpModule():
    global _DOCKER
    from modules import onboarding as _onboarding

    _DOCKER = mock.patch.object(_onboarding, 'docker_running')
    _DOCKER.start()


def tearDownModule():
    _DOCKER.stop()


if __name__ == '__main__':
    unittest.main()


class WindowAddressTests(unittest.TestCase):
    """The terminal always shows the window's address, so a closed window can be opened again."""

    def test_the_address_is_shown_whether_or_not_the_window_opened(self):
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / 'my-config.json'
            settings.write_text(json.dumps({'profiles': {'full': ['system']}}))
            for opened in (True, False):
                shown = []
                with mock.patch.object(setup_gui, 'open_window', return_value=opened):
                    window = wizard.setup_window(settings, shown.append)
                try:
                    self.assertIn(window.url, '\n'.join(shown), opened)
                finally:
                    window.stop()
