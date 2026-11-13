"""0.50.0, the wizard (P06): alerts and the outage warning are a form each, followed by a
test that is really sent. No "did you set it up?" gate comes first, and nothing is called
set up on the strength of an address that was saved: the alert server, or the monitor,
has to take the test; whether a message arrived on a phone is the person's own word.

The sending itself is the helper's (tests/test_outside_tests.py); here: the forms and
what each result leads to (T28, T29)."""

import secrets
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import questions  # noqa: E402
import setup_progress  # noqa: E402
from modules import heartbeat, notifications  # noqa: E402

# Made when the tests run: no address or token that looks real is written here.
TOPIC = 'https://ntfy.example.org/neko-' + secrets.token_hex(8)
PING = 'https://hc-ping.example.org/' + secrets.token_hex(12)


class Typed:
    """What is typed (hidden) at each prompt, and the page shown with it."""

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.pages = []
        self.prompts = []

    def __call__(self, prompt=''):
        self.pages.append(setup_progress.page_shown())
        self.prompts.append(prompt)
        answer = next(self.answers)
        if answer is EOFError:
            raise EOFError
        return answer


def context(interactive=True):
    logs = []
    return SimpleNamespace(config={}, apply=True, log=logs.append, logs=logs), logs


class AlertsFormTests(unittest.TestCase):
    def form(self, *typed):
        person = Typed(*typed)
        ctx, _ = context()
        with mock.patch('builtins.print'):
            return notifications.alerts_form(ctx, ask=person), person

    def test_the_address_and_the_token_are_one_page_with_the_steps_above_them(self):
        (address, token), person = self.form(TOPIC + '/', '')
        self.assertEqual((address, token), (TOPIC, ''))  # a "/" from copying is dropped
        page = person.pages[0]
        self.assertEqual((page['form'], page['what']), ('fields', 'Send alerts to my phone'))
        self.assertEqual([row['key'] for row in page['rows']], ['address', 'token'])
        self.assertTrue(all(row['secret'] for row in page['rows']))  # typed hidden, in no log
        # The steps name the app and offer a topic name nobody could guess.
        self.assertIn('install the free app "ntfy"', page['lines'][0])
        self.assertTrue(page['lines'][2].strip().startswith('https://ntfy.sh/neko-'))
        self.assertIn('set this up later', page['note'])
        # Nothing of what was typed is in the page the window is sent.
        self.assertEqual(page['fields'], {'address': '', 'token': ''})
        self.assertIsNone(setup_progress.page_shown())

    def test_left_empty_is_later_and_a_wrong_address_is_asked_again(self):
        self.assertEqual(self.form('')[0], ('', ''))
        self.assertEqual(self.form(EOFError)[0], ('', ''))  # Ctrl+D, the window's Skip
        (address, _), person = self.form('ntfy.example.org/topic', '', TOPIC, '')
        self.assertEqual(address, TOPIC)
        self.assertEqual(len(person.prompts), 4)
        # Three entries that are no address: left for later, never guessed.
        self.assertEqual(self.form('x', '', 'y', '', 'z', '')[0], ('', ''))

    def test_there_is_no_gate_question_before_the_form(self):
        for gone in ('alerts_phone_ready', 'alerts_address', 'monitor_ready', 'monitor_address'):
            self.assertNotIn(gone, questions.Q)
        source = (ROOT / 'modules/notifications.py').read_text() + (ROOT / 'modules/heartbeat.py').read_text()
        self.assertNotIn('guided_setup.checkpoint(', source)


class AlertTestTests(unittest.TestCase):
    def outcome(self, server, reading='allowed', seen=True):
        ctx, logs = context()
        with (
            mock.patch.object(notifications, 'send_test', return_value={'server': server, 'phone': 'not_tested'}),
            mock.patch.object(notifications, 'reading_test', return_value={'server': reading}),
            mock.patch.object(notifications.guided_setup, 'interactive', return_value=True),
            mock.patch.object(notifications.questions, 'yes_no', return_value=seen) as asked,
        ):
            return notifications.test_alert(ctx, TOPIC, ''), logs, asked

    def test_accepted_is_not_arrived_and_the_person_is_asked(self):
        result, logs, asked = self.outcome('accepted')
        self.assertIsNone(result)
        asked.assert_called_once()
        self.assertEqual(asked.call_args.args[0], 'alert_arrived')
        self.assertIn('Phone alerts test: The alert server accepted the test message.', logs)
        self.assertIn('Phone alerts test: you saw it arrive on your phone', logs)
        # Not seen: said as not seen, with what to look at; the server's yes still stands.
        result, logs, _ = self.outcome('accepted', seen=False)
        self.assertIsNone(result)
        self.assertTrue(any('not seen on a phone yet' in line for line in logs))
        self.assertFalse(any('you saw it arrive' in line for line in logs))

    def test_a_topic_anyone_can_read_is_said_plainly(self):
        _, logs, _ = self.outcome('accepted', reading='open')
        self.assertIn('Phone alerts: ' + notifications.OPEN_TOPIC, logs)
        self.assertIn('it is not a lock', notifications.OPEN_TOPIC)
        _, logs, _ = self.outcome('accepted', reading='allowed')
        self.assertFalse(any('Anyone who knows this address' in line for line in logs))

    def test_a_test_the_server_did_not_take_leaves_the_step_waiting_with_the_reason(self):
        for server in (
            'wrong_token',
            'not_allowed',
            'refused',
            'wrong_address',
            'redirected',
            'timed_out',
            'unreachable',
            'untrusted',
            'busy',
            'server_error',
        ):
            with self.subTest(server=server):
                result, _, asked = self.outcome(server)
                self.assertEqual(result['status'], 'needs_setup')
                self.assertIn(notifications.TEST_SAID[server], result['reason'])
                asked.assert_not_called()  # nothing arrived that could be asked about
        # Every answer the sender can give has its words.
        self.assertEqual(
            set(notifications.TEST_SAID),
            {
                'accepted',
                'wrong_token',
                'not_allowed',
                'refused',
                'wrong_address',
                'redirected',
                'timed_out',
                'unreachable',
                'untrusted',
                'busy',
                'server_error',
                'not_sent',
            },
        )

    def test_the_address_and_the_token_are_in_no_log_line(self):
        _, logs, _ = self.outcome('accepted', reading='open')
        self.assertFalse([line for line in logs if TOPIC in line or TOPIC.rsplit('/', 1)[1] in line])


class OutageFormTests(unittest.TestCase):
    def test_the_steps_say_the_period_the_grace_and_who_tells_you(self):
        person = Typed(PING)
        ctx, _ = context()
        with mock.patch('builtins.print'):
            self.assertEqual(heartbeat.monitor_form(ctx, ask=person), PING)
        page = person.pages[0]
        self.assertEqual((page['form'], page['what']), ('fields', 'Warn me if the server stops checking in'))
        self.assertEqual(
            page['rows'], [{'key': 'address', 'label': 'Check-in address (the Ping URL of your check)', 'secret': True}]
        )
        said = ' '.join(page['lines'])
        self.assertIn('Period: 5 minutes. Grace time: 10 minutes.', said)
        self.assertIn('warns you when no check-in came for about 15 minutes', said)
        self.assertIn('Tell the service how to reach you', said)
        # Left empty, or three entries that are no address: later.
        with mock.patch('builtins.print'):
            self.assertEqual(heartbeat.monitor_form(ctx, ask=Typed('')), '')
            self.assertEqual(heartbeat.monitor_form(ctx, ask=Typed('a', 'b', 'c')), '')

    def test_a_check_in_the_monitor_took_says_what_that_does_not_show(self):
        ctx, logs = context()
        with mock.patch.object(heartbeat, 'send_test_checkin', return_value={'server': 'accepted'}):
            self.assertIsNone(heartbeat.test_checkin(ctx, PING))
        self.assertTrue(any('The monitor accepted a check-in.' in line for line in logs))
        self.assertTrue(any('Whether its warning reaches you is not tested yet' in line for line in logs))
        self.assertFalse([line for line in logs if PING in line])

    def test_a_check_in_the_monitor_refused_leaves_the_step_waiting(self):
        ctx, _ = context()
        for server in (
            'rejected',
            'wrong_address',
            'busy',
            'server_error',
            'redirected',
            'timed_out',
            'unreachable',
            'untrusted',
        ):
            with self.subTest(server=server):
                with mock.patch.object(heartbeat, 'send_test_checkin', return_value={'server': server}):
                    result = heartbeat.test_checkin(ctx, PING)
                self.assertEqual(result['status'], 'needs_setup')
                self.assertIn(heartbeat.TEST_SAID[server], result['reason'])


if __name__ == '__main__':
    unittest.main()
