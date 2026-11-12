"""The two tests the setup window's extras page offers (wizard specification P06, checks
T28, T29, T36): one test message to the phone-alert topic and one test check-in to the
outage monitor. Every answer a server can give is told apart, a person's own observation
is never claimed, and the topic address, its token and the ping address reach no log,
no progress event, no command line and no result.

No request leaves this computer: the server is a fake with open(), and the one real
socket is a local one that proves a redirect is not followed.
"""

import contextlib
import http.server
import io
import json
import logging
import secrets
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import installer  # noqa: E402
import outside_request  # noqa: E402
import setup_progress  # noqa: E402
from modules import heartbeat, notifications  # noqa: E402
from neko_common import NoRedirect  # noqa: E402


class FakeServer:
    """Answers as a server would: a number (the HTTP answer) or an error to raise. Its
    errors carry the address in their words, as real ones do, to prove they go nowhere."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.answer, type) and issubclass(self.answer, BaseException):
            raise self.answer('no answer from ' + request.full_url)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if self.answer >= 300:  # the careful opener follows no redirect: it is an error too
            raise urllib.error.HTTPError(request.full_url, self.answer, 'said ' + request.full_url, {}, io.BytesIO())
        return contextlib.closing(mock.Mock(status=self.answer))

    @property
    def only(self):
        ((request, _),) = self.requests
        return request


def a_topic():
    """A topic address and its token, made now (no secret is written in this file)."""
    name = notifications.new_topic_name()
    return name, notifications.topic_address(name, 'https://alerts.example'), 'tk' + '_' + secrets.token_hex(12)


def a_ping_address():
    return 'https://ping.example/' + str(uuid.uuid4())


NO_ANSWER = (
    (urllib.error.URLError(TimeoutError('timed out')), 'timed_out'),
    (TimeoutError, 'timed_out'),
    (urllib.error.URLError(socket.gaierror(-2, 'Name or service not known')), 'unreachable'),
    (ConnectionRefusedError, 'unreachable'),
    (urllib.error.URLError(ssl.SSLCertVerificationError('self-signed certificate')), 'untrusted'),
)


class TopicNameTests(unittest.TestCase):
    def test_a_made_name_is_long_random_and_fits_a_topic_address(self):
        names = {notifications.new_topic_name() for _ in range(50)}
        self.assertEqual(len(names), 50)
        for name in names:
            self.assertRegex(name, r'^neko-[a-z0-9]{24}$')  # ntfy allows up to 64 of these
            self.assertTrue(notifications.endpoint(notifications.topic_address(name)))
        self.assertTrue(notifications.topic_address(name).startswith(notifications.PUBLIC_SERVER + '/neko-'))

    def test_a_name_or_server_that_makes_no_acceptable_address_gives_none(self):
        name = notifications.new_topic_name()
        self.assertEqual(notifications.topic_address(name, 'https://alerts.example/'), 'https://alerts.example/' + name)
        for bad_name, server in (
            ('two words', 'https://alerts.example'),
            ('a/b', 'https://alerts.example'),
            ('', 'https://alerts.example'),
            (name, 'http://alerts.example'),
            (name, 'https://someone@alerts.example'),
            (name, 'https://alerts.example/path'),
            (None, 'https://alerts.example'),
            (name, None),
        ):
            self.assertEqual(notifications.topic_address(bad_name, server), '', (bad_name, server))


class AlertTestTests(unittest.TestCase):
    def test_accepted_means_the_server_took_it_and_never_that_the_phone_got_it(self):
        _, address, token = a_topic()
        server = FakeServer(200)
        result = notifications.send_test(address, token, opener=server)
        self.assertEqual(
            result, {'address': 'ok', 'token': 'ok', 'server': 'accepted', 'http_status': 200, 'phone': 'not_tested'}
        )
        request = server.only
        self.assertEqual((request.get_method(), request.full_url), ('POST', address))
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + token)
        self.assertEqual(request.data, notifications.TEST_MESSAGE.encode())
        self.assertEqual(request.get_header('Title'), notifications.TEST_TITLE)
        self.assertEqual(server.requests[0][1], outside_request.TIME_LIMIT_SECONDS)

    def test_no_token_sends_no_sign_in(self):
        _, address, _ = a_topic()
        server = FakeServer(200)
        result = notifications.send_test(address, opener=server)
        self.assertEqual((result['token'], result['server']), ('none', 'accepted'))
        self.assertIsNone(server.only.get_header('Authorization'))

    def test_every_no_from_the_server_has_its_own_word(self):
        _, address, token = a_topic()
        for status, word in (
            (401, 'wrong_token'),
            (403, 'not_allowed'),
            (404, 'wrong_address'),
            (410, 'wrong_address'),
            (400, 'refused'),
            (503, 'busy'),
            (502, 'server_error'),
            (429, 'busy'),
            (500, 'server_error'),
            (301, 'redirected'),
            (307, 'redirected'),
        ):
            result = notifications.send_test(address, token, opener=FakeServer(status))
            self.assertEqual((result['server'], result['http_status']), (word, status))
            self.assertEqual(result['phone'], 'not_tested')

    def test_no_answer_at_all_has_its_own_words(self):
        _, address, token = a_topic()
        for error, word in NO_ANSWER:
            result = notifications.send_test(address, token, opener=FakeServer(error))
            self.assertEqual((result['server'], result['http_status']), (word, None), error)

    def test_an_address_or_token_that_is_not_acceptable_is_never_sent(self):
        name, address, token = a_topic()
        for bad in (
            'http://alerts.example/' + name,
            'https://alerts.example/' + name + '?auth=' + token,
            'https://someone:' + token + '@alerts.example/' + name,
            'https://alerts.example/' + name + '\nheader = x',
            'file:///etc/passwd',
            '',
            None,
        ):
            server = FakeServer(200)
            result = notifications.send_test(bad, token, opener=server)
            self.assertEqual((result['address'], result['server']), ('invalid', 'not_sent'), bad)
            self.assertEqual(server.requests, [])
        for bad in (token + '\r\nX-Other: 1', token + ' x', None):
            server = FakeServer(200)
            result = notifications.send_test(address, bad, opener=server)
            self.assertEqual((result['address'], result['token'], result['server']), ('ok', 'invalid', 'not_sent'))
            self.assertEqual(server.requests, [])

    def test_the_persons_answer_is_recorded_apart_and_only_yes_is_observed(self):
        self.assertEqual(outside_request.observed(True), 'observed')
        for answer in (False, None, 'yes', 1):
            self.assertEqual(outside_request.observed(answer), 'not_tested')


class ReadingTestTests(unittest.TestCase):
    def test_a_topic_anyone_may_read_is_called_open(self):
        _, address, _ = a_topic()
        server = FakeServer(200)
        result = notifications.reading_test(address, opener=server)
        self.assertEqual(result, {'address': 'ok', 'token': 'none', 'server': 'open', 'http_status': 200})
        request = server.only
        self.assertEqual((request.get_method(), request.full_url, request.data), ('GET', address + '/auth', None))
        self.assertIsNone(request.get_header('Authorization'))

    def test_a_token_that_may_read_is_told_from_one_that_may_only_send(self):
        _, address, token = a_topic()
        server = FakeServer(200)
        self.assertEqual(notifications.reading_test(address, token, opener=server)['server'], 'allowed')
        self.assertEqual(server.only.get_header('Authorization'), 'Bearer ' + token)
        for status, word in (
            (403, 'not_allowed'),
            (401, 'wrong_token'),
            (404, 'wrong_address'),
            (400, 'refused'),
            (302, 'redirected'),
        ):
            result = notifications.reading_test(address, token, opener=FakeServer(status))
            self.assertEqual((result['server'], result['http_status']), (word, status))
        # A protected topic asked without a token: its server wants a sign-in.
        self.assertEqual(notifications.reading_test(address, opener=FakeServer(403))['server'], 'not_allowed')

    def test_no_answer_and_bad_entries(self):
        name, address, token = a_topic()
        for error, word in NO_ANSWER:
            self.assertEqual(notifications.reading_test(address, token, opener=FakeServer(error))['server'], word)
        server = FakeServer(200)
        self.assertEqual(
            notifications.reading_test('http://alerts.example/' + name, opener=server)['server'], 'not_sent'
        )
        self.assertEqual(notifications.reading_test(address, 'a b', opener=server)['server'], 'not_sent')
        self.assertEqual(server.requests, [])


class CheckInTestTests(unittest.TestCase):
    def test_accepted_is_one_check_in_taken_and_nothing_more_is_claimed(self):
        address = a_ping_address()
        server = FakeServer(200)
        result = heartbeat.send_test_checkin(address, opener=server)
        self.assertEqual(
            result,
            {
                'address': 'ok',
                'server': 'accepted',
                'http_status': 200,
                'timing': 'not_tested',
                'warning': 'not_tested',
            },
        )
        request = server.only
        self.assertEqual((request.get_method(), request.full_url, request.data), ('GET', address, None))
        self.assertEqual(server.requests[0][1], outside_request.TIME_LIMIT_SECONDS)

    def test_rejected_timed_out_and_the_rest_are_told_apart(self):
        address = a_ping_address()
        for status, word in (
            (404, 'wrong_address'),
            (400, 'rejected'),
            (429, 'busy'),
            (503, 'busy'),
            (500, 'server_error'),
            (302, 'redirected'),
        ):
            result = heartbeat.send_test_checkin(address, opener=FakeServer(status))
            self.assertEqual((result['server'], result['http_status']), (word, status))
        for error, word in NO_ANSWER:
            result = heartbeat.send_test_checkin(address, opener=FakeServer(error))
            self.assertEqual((result['server'], result['http_status']), (word, None), error)
            self.assertEqual((result['timing'], result['warning']), ('not_tested', 'not_tested'))

    def test_an_address_that_is_not_a_ping_address_is_never_sent(self):
        address = a_ping_address()
        for bad in (
            address.replace('https://', 'http://'),
            address + '/fail',
            address + '/start',
            address + '?x=1',
            'https://someone@ping.example/x',
            'https://healthchecks.io',
            '',
            None,
        ):
            server = FakeServer(200)
            result = heartbeat.send_test_checkin(bad, opener=server)
            self.assertEqual(
                (result['address'], result['server'], result['http_status']), ('invalid', 'not_sent', None)
            )
            self.assertEqual(server.requests, [])


class ScheduleTests(unittest.TestCase):
    def test_the_period_is_the_timers_own_and_the_grace_time_twice_it(self):
        told = heartbeat.schedule({})
        self.assertEqual(
            told,
            {
                'interval_seconds': 300,
                'grace_seconds': 600,
                'interval_words': '5 minutes',
                'grace_words': '10 minutes',
                'warns_after_seconds': 900,
                'warns_after_words': '15 minutes',
            },
        )
        for seconds in (60, 300, 450, 3600):
            config = {'heartbeat': {'interval_seconds': seconds}}
            timer = heartbeat.definitions(config, '/bin/x', '/etc/x')['nekoserver-heartbeat.timer']
            told = heartbeat.schedule(config)
            self.assertIn(f"OnUnitInactiveSec={told['interval_seconds']}s\n", timer)
            self.assertEqual(told['grace_seconds'], 2 * seconds)
            # A lost check-in is followed by the next before the grace time is over
            # (the job may take 40 seconds: TimeoutStartSec in the service).
            self.assertGreater(told['grace_seconds'], seconds + 40)

    def test_times_are_said_as_a_person_sets_them(self):
        for seconds, words in (
            (60, '1 minute'),
            (90, '90 seconds'),
            (120, '2 minutes'),
            (3600, '1 hour'),
            (7200, '2 hours'),
        ):
            self.assertEqual(heartbeat.plain_time(seconds), words)

    def test_a_period_setup_would_refuse_is_refused_here_too(self):
        for bad in (30, 4000, '300', True):
            with self.assertRaises(RuntimeError):
                heartbeat.schedule({'heartbeat': {'interval_seconds': bad}})


class NoRedirectFollowedTests(unittest.TestCase):
    """The opener itself, against a local socket: a redirect is an answer, not a journey."""

    def test_the_careful_opener_uses_no_proxy_and_follows_no_redirect(self):
        asked = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/elsewhere')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Elsewhere)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        with mock.patch.dict('os.environ', {'http_proxy': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9'}):
            opener = outside_request.careful_opener()
            with self.assertRaises(urllib.error.HTTPError) as raised:
                opener.open(f'http://127.0.0.1:{server.server_port}/first', timeout=5)
        raised.exception.close()
        thread.join(5)
        self.assertEqual((raised.exception.code, asked), (302, ['/first']))
        self.assertTrue(any(isinstance(handler, NoRedirect) for handler in opener.handlers))

    def test_only_https_is_ever_sent(self):
        for url in ('http://alerts.example/x', 'ftp://alerts.example/x', 'file:///etc/passwd', '', None):
            server = FakeServer(200)
            self.assertEqual(outside_request.send(url, opener=server), ('not_sent', None))
            self.assertEqual(server.requests, [])


class SecretsStayOutTests(unittest.TestCase):
    """T36: the topic address, its token and the ping address are in no log, no progress
    event, nothing printed, no command line and no result, whatever the server answers."""

    ANSWERS = (200, 401, 403, 404, 500, 302) + tuple(error for error, _ in NO_ANSWER)

    def setUp(self):
        self.name, self.topic, self.token = a_topic()
        self.ping = a_ping_address()
        self.secrets = (self.topic, self.name, self.token, self.ping, self.ping.rsplit('/', 1)[1])
        self.results = []

    def run_everything(self):
        for answer in self.ANSWERS:
            self.results += [
                notifications.send_test(self.topic, self.token, opener=FakeServer(answer)),
                notifications.reading_test(self.topic, self.token, opener=FakeServer(answer)),
                heartbeat.send_test_checkin(self.ping, opener=FakeServer(answer)),
            ]
        # Entries that are refused before anything is sent hold the secrets too.
        self.results += [
            notifications.send_test(self.topic + '?auth=' + self.token, self.token),
            notifications.send_test(self.topic, self.token + ' ' + self.token),
            notifications.reading_test(self.topic + '?x', self.token),
            heartbeat.send_test_checkin(self.ping + '/fail'),
        ]

    def test_nothing_secret_is_logged_printed_recorded_or_returned(self):
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(level)))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events = Path(folder.name) / 'events.jsonl'
        setup_progress.enable(events)
        self.addCleanup(setup_progress.disable)
        printed, errors = io.StringIO(), io.StringIO()
        no_program = mock.Mock(side_effect=AssertionError('a program was started: its command line is public'))
        with (
            contextlib.redirect_stdout(printed),
            contextlib.redirect_stderr(errors),
            mock.patch('subprocess.run', no_program),
            mock.patch('subprocess.Popen', no_program),
            mock.patch('os.system', no_program),
        ):
            self.run_everything()
            setup_progress.note('the tests are over')
            recorded = repr(setup_progress.events())
        self.assertEqual(len(self.results), 3 * len(self.ANSWERS) + 4)
        places = {
            'the log': logged.getvalue(),
            'the screen': printed.getvalue() + errors.getvalue(),
            'the progress events': recorded,
            'the progress file': events.read_text(),
            'the results': repr(self.results),
        }
        self.assertIn('the tests are over', places['the progress file'])  # the recording was on
        for place, text in places.items():
            for secret in self.secrets:
                self.assertNotIn(secret, text, place)
            self.assertNotIn('alerts.example', text, place)
            self.assertNotIn('ping.example', text, place)

    def test_a_result_holds_only_known_words_and_the_answers_number(self):
        self.run_everything()
        words = {
            'ok', 'invalid', 'none', 'accepted', 'open', 'allowed', 'wrong_token', 'not_allowed', 'refused',
            'rejected', 'redirected', 'timed_out', 'unreachable', 'untrusted', 'not_sent', 'not_tested', 'busy', 'server_error', 'wrong_address',
        }  # fmt: skip
        for result in self.results:
            for key, value in result.items():
                if key == 'http_status':
                    self.assertTrue(value is None or type(value) is int, result)
                else:
                    self.assertIn(value, words, result)


class NoGateTests(unittest.TestCase):
    """Both forms work with what the person entered: nothing asks "did you...?" first, and
    "set up later" is a step still to do, not a failure and not a success."""

    def setUp(self):
        asked = AssertionError('a question was asked')
        for target in ('builtins.input', 'getpass.getpass'):
            patch = mock.patch(target, side_effect=asked)
            patch.start()
            self.addCleanup(patch.stop)

    def installed(self, module, *values):
        """module.install(...) with the system parts faked: what it saved, ran and said."""
        saved, ran, said = {}, [], []
        context = mock.Mock(apply=True, config={}, log=said.append)
        with (
            mock.patch.object(module, 'safe_path', side_effect=Path),
            mock.patch.object(module.native_runtime, 'deploy', return_value='/usr/local/lib/neko/nekoserver'),
            mock.patch.object(module.shutil, 'which', return_value='/usr/bin/curl'),
            mock.patch.object(module, 'check_units'),
            mock.patch.object(module, 'write_units'),
            mock.patch.object(module, 'atomic_json', side_effect=lambda path, value: saved.update(value)),
            mock.patch.object(module, 'subprocess', mock.Mock(run=lambda argv, **_: ran.append(argv))),
        ):
            result = module.install(context, *values)
        return result, saved, ran, said

    def test_the_tests_ask_nothing(self):
        _, address, token = a_topic()
        notifications.send_test(address, token, opener=FakeServer(200))
        notifications.reading_test(address, token, opener=FakeServer(200))
        heartbeat.send_test_checkin(a_ping_address(), opener=FakeServer(200))
        heartbeat.schedule({})

    def test_alerts_are_installed_from_the_forms_values_without_a_question(self):
        _, address, token = a_topic()
        result, saved, ran, said = self.installed(notifications, address, token)
        self.assertIsNone(result)
        self.assertEqual((saved['endpoint'], saved['token']), (address, token))
        self.assertIn(['systemctl', 'enable', '--now', 'nekoserver-notifications.timer'], ran)
        for secret in (address, token):
            self.assertNotIn(secret, repr(ran) + repr(said))

    def test_outage_warnings_are_installed_from_the_forms_value_without_a_question(self):
        address = a_ping_address()
        result, saved, ran, said = self.installed(heartbeat, address)
        self.assertIsNone(result)
        self.assertEqual(saved, {'endpoint': address, 'tested': False})
        self.assertIn(['systemctl', 'enable', '--now', 'nekoserver-heartbeat.timer'], ran)
        self.assertNotIn(address, repr(ran) + repr(said))

    def test_install_refuses_a_value_that_is_not_acceptable(self):
        _, address, token = a_topic()
        for values in ((address + '?x=1', token), (address, token + ' x')):
            with self.assertRaises(RuntimeError) as refused:
                self.installed(notifications, *values)
            self.assertNotIn(token, str(refused.exception))
        with self.assertRaises(RuntimeError):
            self.installed(heartbeat, a_ping_address() + '/fail')

    def test_set_up_later_is_a_step_still_to_do(self):
        for module in (notifications, heartbeat):
            later = module.set_up_later()
            state = installer.outcome(later, 'complete')
            self.assertEqual(state, {'status': 'needs_setup', 'reason': later['reason']})
            self.assertIn('not set up yet', later['reason'])


class GateRunCTests(unittest.TestCase):
    """Gate run C12 (VM3): a busy server (429) was told as a wrong topic, a server error
    (500) as a wrong address, the finishing screen's retry of "Phone alerts" said PASS with
    no request sent, and the retry of "Server heartbeat" sent its check-in to the alerts
    address and said PASS on a 429."""

    KINDS = ('wrong_address', 'busy', 'server_error', 'timed_out', 'unreachable')

    def test_each_kind_of_answer_has_its_own_word(self):
        for status, word in (
            (404, 'wrong_address'),
            (410, 'wrong_address'),
            (429, 'busy'),
            (503, 'busy'),
            (500, 'server_error'),
            (502, 'server_error'),
            (400, 'refused'),
        ):
            self.assertEqual(outside_request.kind('refused', status), word, status)
        self.assertEqual(outside_request.kind('accepted', 200), 'accepted')
        for word in ('timed_out', 'unreachable', 'untrusted'):
            self.assertEqual(outside_request.kind(word, None), word)

    def test_each_kind_of_answer_has_its_own_plain_words(self):
        for module in (notifications, heartbeat):
            said = {kind: module.TEST_SAID[kind] for kind in self.KINDS}
            self.assertEqual(len(set(said.values())), len(said), module.__name__)
            self.assertIn('Try again in a few minutes', said['busy'])
            self.assertIn('Their server has a problem right now', said['server_error'])
            for kind in ('timed_out', 'unreachable'):
                self.assertIn('network problem', said[kind])
            # Only a wrong address asks for the address again.
            self.assertIn('enter it again', said['wrong_address'])
            for kind, words in module.TEST_SAID.items():
                if kind != 'wrong_address':
                    self.assertNotIn('enter it again', words, (module.__name__, kind))
                    self.assertNotIn('spelling of the address', words, (module.__name__, kind))

    def test_the_test_record_follows_the_saved_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / 'config.json'
            missing = Path(folder) / 'missing.json'
            address = 'https://alerts.example/' + notifications.new_topic_name()
            private.write_text(json.dumps({'endpoint': address}))
            self.assertFalse(outside_request.untested(private))  # saved before 0.50.0: it stands
            self.assertEqual(outside_request.kept_test(private, endpoint=address), {})
            outside_request.mark_tested(private, 'busy')
            self.assertTrue(outside_request.untested(private))
            self.assertFalse(outside_request.wrong_address(private))
            outside_request.mark_tested(private, 'wrong_address')
            self.assertTrue(outside_request.wrong_address(private))
            outside_request.mark_tested(private, 'accepted')
            self.assertFalse(outside_request.untested(private))
            self.assertEqual(json.loads(private.read_text())['endpoint'], address)
            # the same destination keeps its record; another one is not tested yet
            self.assertTrue(outside_request.kept_test(private, endpoint=address)['tested'])
            self.assertEqual(outside_request.kept_test(private, endpoint=address + 'x'), {'tested': False})
            self.assertEqual(outside_request.kept_test(missing, endpoint=address), {'tested': False})
            self.assertFalse(outside_request.untested(missing))


class FinishRetryTests(unittest.TestCase):
    """The finishing screen's "Retry this step" runs the step's module again
    (wizard.retry_unfinished, then installer with retry_step): here that run, with the
    system parts faked and the two private files in a folder of the test's own."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.files = {
            notifications.PRIVATE_CONFIG: self.folder / 'alerts.json',
            heartbeat.PRIVATE: self.folder / 'heartbeat.json',
        }
        _, self.topic, self.token = a_topic()
        self.ping = a_ping_address()
        self.context = mock.Mock(apply=True, config={}, log=lambda line: None)
        patches = [
            mock.patch('modules._schedules.switched_off', return_value=False),
            mock.patch('modules._schedules.installed', return_value=True),
            mock.patch('os.geteuid', return_value=0),
            mock.patch.object(notifications.shutil, 'which', return_value='/usr/bin/curl'),
            mock.patch.object(notifications.guided_setup, 'interactive', return_value=False),
            mock.patch.object(heartbeat.sys, 'stdin', mock.Mock(isatty=mock.Mock(return_value=False))),
        ]
        for module in (notifications, heartbeat):
            patches += [
                mock.patch.object(module, 'safe_path', side_effect=lambda p: self.files[str(p)]),
                mock.patch.object(module.native_runtime, 'deploy', return_value='/usr/local/lib/neko/runtime'),
                mock.patch.object(module, 'check_units'),
                mock.patch.object(module, 'write_units'),
                mock.patch.object(module, 'subprocess'),
            ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def save(self, key, record):
        self.files[key].write_text(json.dumps(record))

    def retry(self, module, answer):
        server = FakeServer(answer)
        with (
            mock.patch.object(outside_request, 'careful_opener', return_value=server),
            mock.patch.object(heartbeat, 'saved_endpoint', return_value=self.ping),
            mock.patch.object(notifications, 'reading_test', return_value={'server': 'allowed'}),
        ):
            result = module.run(self.context)
        return installer.outcome(result, 'complete'), server

    def test_an_alerts_retry_sends_the_test_and_passes_only_on_a_good_answer(self):
        for answer in (429, 500, 404, 400, ConnectionRefusedError, TimeoutError):
            with self.subTest(answer=answer):
                self.save(notifications.PRIVATE_CONFIG, {'endpoint': self.topic, 'token': self.token, 'tested': False})
                state, server = self.retry(notifications, answer)
                self.assertEqual(server.only.full_url, self.topic)
                self.assertEqual(state['status'], 'needs_setup')
                self.assertTrue(outside_request.untested(self.files[notifications.PRIVATE_CONFIG]))
        state, server = self.retry(notifications, 200)
        self.assertEqual((server.only.get_method(), server.only.full_url), ('POST', self.topic))
        self.assertEqual(state['status'], 'complete')
        self.assertFalse(outside_request.untested(self.files[notifications.PRIVATE_CONFIG]))

    def test_a_run_cut_off_after_saving_is_tested_at_the_retry(self):
        # install saved the address (not tested yet), then the run stopped: no pass unseen.
        notifications.install(self.context, self.topic, self.token)
        self.assertTrue(outside_request.untested(self.files[notifications.PRIVATE_CONFIG]))
        state, server = self.retry(notifications, 503)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(state['status'], 'needs_setup')
        self.assertIn('Try again in a few minutes', state['reason'])

    def test_the_heartbeat_retry_goes_to_the_heartbeat_address(self):
        self.save(notifications.PRIVATE_CONFIG, {'endpoint': self.topic, 'token': self.token, 'tested': True})
        self.save(heartbeat.PRIVATE, {'endpoint': self.ping, 'tested': False, 'answer': 'busy'})
        state, server = self.retry(heartbeat, 429)
        self.assertEqual((server.only.get_method(), server.only.full_url), ('GET', self.ping))
        self.assertNotEqual(server.only.full_url, self.topic)
        self.assertEqual(state['status'], 'needs_setup')
        self.assertIn('Try again in a few minutes', state['reason'])
        state, server = self.retry(heartbeat, 200)
        self.assertEqual(server.only.full_url, self.ping)
        self.assertEqual(state['status'], 'complete')
        # passed once and unchanged: a later run sends nothing and stays passed
        state, server = self.retry(heartbeat, 500)
        self.assertEqual((server.requests, state['status']), ([], 'complete'))

    def test_a_server_error_or_no_answer_on_the_heartbeat_retry_is_not_a_pass(self):
        for answer in (500, 503, ConnectionRefusedError, 404, 400):
            with self.subTest(answer=answer):
                self.save(heartbeat.PRIVATE, {'endpoint': self.ping, 'tested': False})
                state, server = self.retry(heartbeat, answer)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(state['status'], 'needs_setup')

    def test_only_a_wrong_address_asks_for_it_again(self):
        alerts = self.files[notifications.PRIVATE_CONFIG]
        self.save(notifications.PRIVATE_CONFIG, {'endpoint': self.topic, 'token': self.token})
        outside_request.mark_tested(alerts, 'busy')
        notifications.guided_setup.interactive.return_value = True
        with mock.patch.object(notifications, 'alerts_form', return_value=('', '')) as form:
            self.retry(notifications, 429)
            form.assert_not_called()
            outside_request.mark_tested(alerts, 'wrong_address')
            state, server = self.retry(notifications, 200)
            form.assert_called_once()
        self.assertEqual(server.requests, [])
        self.assertEqual(state['status'], 'needs_setup')
        # the outage monitor the same way: asked again, then tested at the new address
        self.save(heartbeat.PRIVATE, {'endpoint': self.ping, 'tested': False, 'answer': 'wrong_address'})
        heartbeat.sys.stdin.isatty.return_value = True
        new_ping = a_ping_address()
        with mock.patch.object(heartbeat, 'monitor_form', return_value=new_ping) as form:
            state, server = self.retry(heartbeat, 200)
        form.assert_called_once()
        self.assertEqual(server.only.full_url, new_ping)
        self.assertEqual(state['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
