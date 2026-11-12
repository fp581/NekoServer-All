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
            (404, 'refused'),
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
        for status, word in ((403, 'not_allowed'), (401, 'wrong_token'), (404, 'refused'), (302, 'redirected')):
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
            (404, 'rejected'),
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
            'rejected', 'redirected', 'timed_out', 'unreachable', 'untrusted', 'not_sent', 'not_tested', 'busy', 'server_error',
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
        self.assertEqual(saved, {'endpoint': address})
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


if __name__ == '__main__':
    unittest.main()


class GateRunCTests(unittest.TestCase):
    """Gate run C (VM3): a busy server (429) was told as a wrong topic, a server error (500)
    as a wrong address, and a retry after a failed test called the step done with no
    request sent."""

    def test_busy_and_a_server_error_are_told_apart_from_a_wrong_address(self):
        from modules import heartbeat, notifications

        self.assertEqual(notifications._answer_word('refused', 429, 'accepted'), 'busy')
        self.assertEqual(notifications._answer_word('refused', 503, 'accepted'), 'busy')
        self.assertEqual(notifications._answer_word('refused', 500, 'accepted'), 'server_error')
        self.assertEqual(notifications._answer_word('refused', 404, 'accepted'), 'refused')
        for module in (notifications, heartbeat):
            self.assertIn('try later', module.TEST_SAID['busy'])
            self.assertIn('problem of its own', module.TEST_SAID['server_error'])

    def test_a_saved_address_whose_test_was_not_taken_is_tested_again(self):
        from modules import notifications

        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / 'config.json'
            private.write_text(json.dumps({'endpoint': 'https://ntfy.example/topic'}))
            self.assertFalse(notifications.untested(private))  # saved before 0.50.0: it stands
            notifications.mark_tested(private, False)
            self.assertTrue(notifications.untested(private))
            notifications.mark_tested(private, True)
            self.assertFalse(notifications.untested(private))
            self.assertEqual(json.loads(private.read_text())['endpoint'], 'https://ntfy.example/topic')
            self.assertFalse(notifications.untested(Path(folder) / 'missing.json'))
