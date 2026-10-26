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
                # (The last lines: a thread of another test may print late, and the
                # terminal's last lines are what the context keeps anyway.)
                self.assertEqual(seen[0]['context'][-2:], ['Drives on this PC:', '  1. sda  Samsung SSD'])
                self.assertIsNone(setup_progress.pending_question())
                # A step's own block is the context, without the installer's log lines.
                print('[2026-10-24T12:21:53+00:00] START tailscale')
                print('=== Tailscale: connect this server ===')
                print('1. Open the sign-in page on any device.')
                builtins.input('Have you completed this step successfully? [yes/no]: ')
                self.assertEqual(seen[1]['context'][-2:], ['=== Tailscale: connect this server ===', '1. Open the sign-in page on any device.'])
                self.assertNotIn('Drives on this PC:', seen[1]['context'])   # the earlier question's lines are gone
                self.assertEqual(seen[1]['form'], 'yesno')
                # The installer's time-stamped lines are what a drive question is about:
                # they stay, without their stamps; its step markers do not.
                setup_progress.step('storage', 'running')
                print('[2026-10-24T21:32:16+00:00] Device sde: 86G / Virtual Disk')
                print('[2026-10-24T21:32:16+00:00] Select the SSD (apps, photos and music):')
                print('[2026-10-24T21:32:16+00:00]   1) /dev/sde 86G ext4 UUID=70a9 mounted=-')
                builtins.input('Choose number (or ENTER to abort): ')
                self.assertEqual(seen[2]['context'], ['Device sde: 86G / Virtual Disk', 'Select the SSD (apps, photos and music):',
                                                      '  1) /dev/sde 86G ext4 UUID=70a9 mounted=-'])
                builtins.input('Type sde to confirm it is the SSD: ')
                self.assertEqual(seen[3]['context'], [])   # nothing new was said since the last answer
                import getpass
                self.assertEqual(getpass.getpass('PIN: '), '1234')
                self.assertEqual(setup_progress.question_kind('Ready? [yes/no]: ', False), 'yesno')
        texts = json.dumps(setup_progress.events())
        self.assertNotIn('secret answer', texts)
        self.assertNotIn('1234', texts)
        self.assertEqual([e['kind'] for e in setup_progress.events()], ['question', 'answered'] * 2 + ['step'] + ['question', 'answered'] * 3)
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
            # The window answers: the terminal is told. A line typed in the terminal at the
            # same time, which lost the race, is dropped rather than answering the next question.
            record = setup_progress.question('Select the SSD. Choose number: ')
            os.write(master, b'7\n')
            self.assertFalse(setup_progress.answer_from_window(record['seq'] + 1, '9'))   # not the question waiting
            self.assertTrue(setup_progress.answer_from_window(record['seq'], '2'))
            self.assertFalse(setup_progress.answer_from_window(record['seq'], '3'))        # answered already
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
        packages = mock.patch.object(setup_gui, 'family_packages', return_value={
            'names': lambda name: [name], 'vulkan': ['vulkan-intel', 'lib32-vulkan-intel']})
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
        request = urllib.request.Request(self.window.url + route, data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
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
        for route, token in (('', 'x' * 32), ('api/state', 'y' * 32), ('static/../wizard.py', None), ('wizard.py', None)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(route, token)
            self.assertEqual(caught.exception.code, 404, route)

    def test_only_loading_the_page_claims_the_window(self):
        # Another account that read the address must not take the session with a request
        # of another kind before the window's browser loads the page.
        fresh = setup_gui.Window(self.settings, self.config, wizard.STEPS, self.config['profiles']['full'])
        self.addCleanup(fresh.stop)
        grab = urllib.request.Request(fresh.url, data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.cookie_browser().open(grab, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        browser = self.cookie_browser()
        with browser.open(fresh.url, timeout=5) as response:
            self.assertEqual(response.status, 200)
        # A request of another kind still never claims it, also once it is open.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.cookie_browser().open(grab, timeout=5)
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
        fresh = setup_gui.Window(self.settings, self.config, wizard.STEPS, self.config['profiles']['full'],
                                 owners={0, 1000}, tables=(table, Path(self.tmp.name) / 'missing'))
        self.addCleanup(fresh.stop)
        port = fresh.server.server_address[1]

        def load_as(uid):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            connection.connect()
            here = connection.sock.getsockname()
            table.write_text('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n'
                             + self.table_line(('127.0.0.1', port), here, 4321) + '\n'     # the server's own end
                             + self.table_line(here, ('127.0.0.1', port), uid) + '\n')
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
        status, cookie, body = load_as(2000)            # once it is open, still not another account
        self.assertEqual((status, cookie), (403, ''))
        self.assertIn(b'open somewhere else', body)
        first = fresh.session
        status, cookie, _ = load_as(0)                  # root (setup itself) may open it again
        self.assertEqual(status, 200)
        self.assertNotEqual(fresh.session, first)
        self.assertIn(setup_gui.COOKIE + '=' + fresh.session, cookie)
        table.unlink()                                  # an owner that can't be told is refused too
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
            v4.write_text(head + self.table_line(server, client, 61234) + '\n' + self.table_line(client, server, 1000) + '\n')
            self.assertEqual(setup_gui.peer_uid(client, server, (v4,)), 1000)
            self.assertEqual(setup_gui.peer_uid(server, client, (v4,)), 61234)
            self.assertIsNone(setup_gui.peer_uid(('127.0.0.1', 1), server, (v4,)))
            # IPv6 tables, with 127.0.0.1 written the IPv6 way (::ffff:127.0.0.1).
            raw = bytes(10) + b'\xff\xff' + bytes([127, 0, 0, 1])
            mapped = ''.join(f'{int.from_bytes(raw[i:i + 4], sys.byteorder):08X}' for i in range(0, 16, 4))
            v6.write_text(head + f'   0: {mapped}:D0A4 {mapped}:2253 01 0:0 0:0 0 1001 0 7 1\n   1: garbage\n')
            self.assertEqual(setup_gui.peer_uid(client, server, (Path(d) / 'none', v4.with_name('x'), v6)), 1001)
            self.assertEqual(setup_gui.peer_uid(('::ffff:127.0.0.1', 53412), server, (v6,)), 1001)
        with self.assertRaises(ValueError):
            setup_gui.table_address('0100:1F90')

    def test_the_expected_accounts_are_root_sudo_and_the_desktop_user(self):
        with mock.patch.dict(os.environ, {'SUDO_UID': '1001'}), \
                mock.patch.object(setup_gui.guided_setup, 'desktop_command', return_value=['runuser', '-u', 'root', '--']):
            self.assertEqual(setup_gui.expected_owners({}), {0, os.geteuid(), 1001})
        with mock.patch.dict(os.environ, {'SUDO_UID': ''}), \
                mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=RuntimeError('no desktop')):
            self.assertEqual(setup_gui.expected_owners({}), {0, os.geteuid()})

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

    def test_a_program_asking_in_the_terminal_is_handed_off_there(self):
        self.window.started.set()
        self.assertIsNone(self.state()['terminal'])
        seen = []
        from modules import samba
        samba.samba_password('neko', run=lambda argv, **_: seen.append(self.state()['terminal']) or mock.Mock(returncode=0))
        self.assertIn('account neko', seen[0])
        self.assertIsNone(self.state()['terminal'])     # over once the program ended
        def failing_program():
            with setup_progress.in_terminal('Music listener setup'):
                seen.append(self.state()['terminal'])
                raise RuntimeError('it failed')
        self.assertRaises(RuntimeError, failing_program)
        self.assertEqual(seen[-1], 'Music listener setup')
        self.assertIsNone(self.state()['terminal'])

    def test_onboarding_asks_in_the_terminal_with_the_window_told(self):
        from modules import onboarding
        release = Path(self.tmp.name) / 'data/installer/releases' / ('v' + setup_gui.VERSION)
        release.mkdir(parents=True)
        (release / 'onboard.py').write_text('')
        ctx = mock.Mock(apply=True, plan=(), log=lambda line: None,
                        config={'data_mount': str(Path(self.tmp.name) / 'data'), 'onboarding': {}, 'media': {'services': []}})
        seen = []
        def ran(cmd, **_):
            seen.append(setup_progress.terminal_handoff())
            return mock.Mock(returncode=0)
        with mock.patch.object(onboarding.sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(onboarding.account_store, 'enabled', return_value=False), \
                mock.patch.object(onboarding.subprocess, 'run', side_effect=ran), \
                mock.patch.object(onboarding, 'witness_playback'):
            onboarding.run(ctx)
        self.assertIn('media apps', seen[0])
        self.assertIsNone(setup_progress.terminal_handoff())

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
        # Counted as the terminal and the log count: done of the steps this PC can have.
        self.assertEqual((state['done'], state['total']), (1, 3))
        self.assertTrue(self.window.delivered.is_set())

    def test_a_body_too_large_or_broken_is_refused(self):
        for data, code in ((b'{' * 20, 400), (b'{"tv": false}' + b' ' * setup_gui.LARGEST_BODY, 413)):
            request = urllib.request.Request(self.window.url + 'api/choose', data=data,
                                             headers={'Content-Type': 'application/json'}, method='POST')
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
        request = urllib.request.Request(self.window.url + 'api/answer', data=b'{"seq": 1, "text": "yes"}', method='POST')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            other.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.get('')[0], 200)          # a reload in the window's own browser

    def test_a_closed_window_opens_again_from_the_same_account(self):
        """D2: the auto-opened window was closed (its browser lost the session cookie with
        it); pasting the address again, as the terminal says, opens it again. The older
        window's session ends and the page then says where the window is."""
        reopened = self.cookie_browser()
        status, kind, _ = self.get('', browser=reopened)
        self.assertEqual((status, kind), (200, 'text/html; charset=utf-8'))
        self.assertEqual(self.get('api/state', browser=reopened)[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get('api/state')                       # the first window's cookie
        self.assertEqual(caught.exception.code, 403)
        page = caught.exception.read()
        self.assertTrue(page.startswith(b'<!doctype html>'))    # words that wrap, not a line of code
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
                conn.sendall(f'POST /t/{self.window.token}/api/choose HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                             f'Content-Length: {length}\r\n\r\n'.encode())
                self.assertIn(b' 40', conn.recv(200))   # 400 (or 403 without the cookie), never a hang
        with mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:
                conn.sendall(f'GET /t/{self.window.token}/api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
            import time
            time.sleep(0.3)
        self.assertNotIn('Traceback', err.getvalue())


class WindowPlacementTests(unittest.TestCase):
    """The small window sits low on the screen and never off it; without a known screen
    size the desktop places it."""
    def test_low_on_the_screen_and_always_on_it(self):
        self.assertEqual(setup_gui.window_position((1920, 1080)), '60,540')
        self.assertEqual(setup_gui.window_position((1366, 768)), '60,228')
        self.assertEqual(setup_gui.window_position((1024, 600)), '0,60')      # narrow and short: as far up as needed
        self.assertEqual(setup_gui.window_position((800, 480)), '0,0')

    def test_the_screen_size_comes_from_xrandr_or_is_unknown(self):
        xrandr_says = 'Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 16384 x 16384\n'
        with mock.patch.object(setup_gui, 'find_program', return_value='/usr/bin/xrandr'), \
                mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=lambda config, argv: argv):
            ok = lambda argv, **kwargs: mock.Mock(returncode=0, stdout=xrandr_says)   # noqa: E731
            self.assertEqual(setup_gui.screen_size({}, run=ok), (2560, 1440))
            failed = lambda argv, **kwargs: mock.Mock(returncode=1, stdout='')      # noqa: E731
            self.assertIsNone(setup_gui.screen_size({}, run=failed))
            self.assertIsNone(setup_gui.screen_size({}, run=mock.Mock(side_effect=OSError)))
        with mock.patch.object(setup_gui, 'find_program', return_value=None):
            self.assertIsNone(setup_gui.screen_size({}))
        with mock.patch.object(setup_gui, 'find_program', return_value='/usr/bin/xrandr'), \
                mock.patch.object(setup_gui.guided_setup, 'desktop_command', side_effect=RuntimeError('no desktop')):
            self.assertIsNone(setup_gui.screen_size({}))


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
