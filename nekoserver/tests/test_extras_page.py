"""The extras page's own parts (wizard specification P06 C and D, and the alert-topic
change of A): the assistant's models as a person can judge them, what the TV extra
installs and cannot do, the four TV tests, and a topic change shown without the topic.
"""

import contextlib
import io
import json
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extras_page  # noqa: E402
from modules import assistant, notifications, tv  # noqa: E402

SMALL, MEDIUM, LARGE = (model for model, *_ in assistant.MODELS)
GB = 1000**3


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = {'data_mount': folder.name}
        self.folder = Path(folder.name)


class AssistantChoicesTests(Base):
    def choices(self, available=14.0, total=16.0, free_gb=200, downloaded=(), apps_started=True, checks=()):
        with mock.patch.object(assistant, 'downloaded', lambda config, model: model in downloaded):
            return extras_page.assistant_choices(
                self.config,
                memory=(total, available),
                own_use=0,
                apps_started=apps_started,
                usage=lambda path: types.SimpleNamespace(free=free_gb * GB),
                checks=checks,
            )

    def test_each_model_has_a_plain_name_one_line_and_its_technical_name_apart(self):
        found = self.choices()
        self.assertTrue(found['possible'])
        self.assertEqual([one['name'] for one in found['choices']], ['Small', 'Medium', 'Large'])
        self.assertEqual([one['id'] for one in found['choices']], [SMALL, MEDIUM, LARGE])
        small = found['choices'][0]
        self.assertEqual(
            small['line'],
            'Small: short, simple answers, the quickest, but the weakest. '
            'Uses about 3 GB of memory while it answers and 1.9 GB of drive space.',
        )
        for one, (model, download, need, _) in zip(found['choices'], assistant.MODELS):
            self.assertEqual((one['technical'], one['download_gb'], one['memory_gb']), (model, download, need))
            self.assertNotIn(model, one['line'])  # the technical name is small print, not the sentence
            self.assertNotIn('qwen', one['name'].lower())

    def test_what_fits_is_worked_out_as_setup_does_and_the_biggest_that_fits_is_suggested(self):
        # 16 GB, 14 free: the reserve is 3.2 (a fifth), so 10.8 are left: all three fit.
        roomy = self.choices(available=14.0)
        self.assertEqual([one['fits'] for one in roomy['choices']], ['yes', 'yes', 'yes'])
        self.assertEqual((roomy['suggested'], roomy['memory']['free_gb']), (LARGE, 10.8))
        self.assertEqual([one['suggested'] for one in roomy['choices']], [False, False, True])
        # 8 GB, 7.5 free: 5.5 left: the large one does not fit, and says why in numbers.
        tight = self.choices(available=7.5, total=8.0)
        self.assertEqual([one['fits'] for one in tight['choices']], ['yes', 'yes', 'no'])
        self.assertEqual(tight['suggested'], MEDIUM)
        self.assertEqual(
            tight['choices'][2]['why_not'],
            'It needs about 9 GB of free memory. This PC has 7.5 GB free with your apps running, and keeps '
            '2.0 GB for the apps to grow into, which leaves 5.5 GB for the assistant.',
        )
        self.assertEqual(tight['suggested'], assistant.fits(5.5)[-1][0])  # the terminal's own rule

    def test_nothing_fits_is_not_possible_with_the_reason(self):
        found = self.choices(available=3.0, total=4.0)
        self.assertEqual((found['possible'], found['suggested']), (False, None))
        self.assertIn('No model fits on this PC now', found['reason'])
        self.assertIn('needs about 3 GB of free memory', found['reason'])
        self.assertEqual(found['later']['id'], 'later')  # still a valid answer
        unread = self.choices(available=0, total=0)
        self.assertFalse(unread['possible'])
        self.assertIn('could not read how much memory', unread['reason'])

    def test_before_the_apps_run_a_fit_is_not_promised(self):
        found = self.choices(available=7.5, total=8.0, apps_started=False)
        self.assertEqual([one['fits'] for one in found['choices']], ['unknown', 'unknown', 'no'])
        self.assertEqual(found['memory']['measured'], 'before_apps')
        self.assertIn('your apps are not running yet', found['choices'][0]['why_not'])
        self.assertNotIn('with your apps running', found['choices'][2]['why_not'])
        self.assertEqual(found['suggested'], MEDIUM)
        self.assertEqual(self.choices()['memory']['measured'], 'with_apps')

    def test_a_model_on_the_drive_already_is_marked_and_needs_no_room_again(self):
        # 13 GB free: Ollama's image (5) and the spare 10 leave no room for any download.
        full = self.choices(free_gb=13)
        self.assertEqual([one['room_on_drive'] for one in full['choices']], [False, False, False])
        self.assertIn('Not enough drive space', full['choices'][0]['why_not'])
        self.assertFalse(full['possible'])
        (self.folder / 'compose' / 'assistant').mkdir(parents=True)
        (self.folder / 'compose' / 'assistant' / assistant.MANIFEST).write_text('services: {}\n')
        have = self.choices(free_gb=11, downloaded=(MEDIUM,))
        self.assertEqual([one['downloaded'] for one in have['choices']], [False, True, False])
        self.assertEqual([one['room_on_drive'] for one in have['choices']], [False, True, False])
        self.assertEqual(have['suggested'], MEDIUM)

    def test_a_pc_that_cannot_run_it_says_why(self):
        def old_processor():
            raise assistant.NotHere('its processor lacks AVX2')

        found = self.choices(checks=(old_processor,))
        self.assertEqual((found['possible'], found['suggested']), (False, None))
        self.assertEqual(found['reason'], 'The assistant cannot run on this PC: its processor lacks AVX2.')

    def test_the_saved_choice_is_shown_and_a_hand_named_model_is_not_offered_as_one(self):
        self.config['assistant'] = {'wanted': True, 'model': MEDIUM}
        self.assertEqual(self.choices()['chosen'], MEDIUM)
        self.config['assistant'] = {'wanted': True, 'model': 'gemma4:12b'}
        self.assertIsNone(self.choices()['chosen'])
        self.config['assistant'] = {'wanted': False, 'model': MEDIUM}
        self.assertIsNone(self.choices()['chosen'])

    def test_no_promise_that_the_apps_never_slow_down_or_that_nothing_leaves_the_home(self):
        found = self.choices()
        said = json.dumps(found)
        for promise in ('never slow', 'come first', 'no slow', 'never leaves', 'nothing leaves', 'always'):
            self.assertNotIn(promise, said)
        self.assertIn('go slower', found['costs'])
        self.assertIn('downloaded once from the Ollama library on the internet', found['costs'])


class ChooseAssistantTests(Base):
    def setUp(self):
        super().setUp()
        self.saved = []
        self.ctx = types.SimpleNamespace(
            config=self.config, log=lambda line: None, persist_config=lambda: self.saved.append(1)
        )

    def test_a_model_is_saved_and_later_saves_nothing(self):
        self.assertEqual(extras_page.choose_assistant(self.ctx, 'later'), {'status': 'later'})
        self.assertNotIn('assistant', self.config)
        self.assertEqual(self.saved, [])
        self.assertEqual(extras_page.choose_assistant(self.ctx, MEDIUM), {'status': 'chosen', 'model': MEDIUM})
        self.assertEqual(self.config['assistant'], {'wanted': True, 'model': MEDIUM})
        self.assertEqual(self.saved, [1])
        # Later, after a choice was made: the choice stands.
        extras_page.choose_assistant(self.ctx, 'later')
        self.assertEqual(self.config['assistant'], {'wanted': True, 'model': MEDIUM})

    def test_anything_not_offered_is_refused(self):
        for bad in ('gemma4:12b', 'none', '', None, 3, MEDIUM + ' ', ['later']):
            with self.assertRaises(ValueError):
                extras_page.choose_assistant(self.ctx, bad)
        self.assertNotIn('assistant', self.config)


class FakeLinux:
    """A Linux family as the TV step asks it."""

    def __init__(self, name='debian', ubuntu=False, i386=True, offered=True, release=None):
        self.name, self._ubuntu, self._i386, self._offered = name, ubuntu, i386, offered
        self.release = release or {}

    def ubuntu(self):
        return self._ubuntu

    def foreign_architectures(self):
        return ['i386'] if self._i386 else []

    def available(self, package):
        return self._offered


class TvPlanTests(Base):
    def plan(self, family=None, has_desktop=True, vendors=('intel',), has_browser=True):
        return extras_page.tv_plan(self.config, family or FakeLinux(), has_desktop, list(vendors), has_browser)

    def test_it_lists_what_is_installed_in_plain_words_and_the_packages_apart(self):
        found = self.plan()
        self.assertTrue(found['possible'])
        self.assertEqual(found['packages'], tv.wanted_packages({}, ['intel'], True))
        self.assertEqual(found['packages'][:2], ['steam', 'steam-devices'])
        said = ' '.join(found['installs'])
        for part in ('Steam', 'Big Picture', 'Vulkan', 'Bluetooth', 'Two shortcuts', 'does not go to sleep'):
            self.assertIn(part, said)
        self.assertNotIn('Firefox', said)
        self.assertEqual(found['needs_first'], '')

    def test_the_settings_and_the_pc_change_the_list(self):
        self.assertIn('The Firefox browser (this PC has no browser yet).', self.plan(has_browser=False)['installs'])
        self.assertIn('firefox', self.plan(has_browser=False)['packages'])
        self.config['tv'] = {'bluetooth': False, 'keep_awake': False}
        quiet = self.plan()
        self.assertNotIn('bluez', quiet['packages'])
        self.assertFalse([line for line in quiet['installs'] if 'Bluetooth' in line or 'sleep' in line])
        nvidia = self.plan(vendors=('nvidia',))
        self.assertFalse([line for line in nvidia['installs'] if 'Vulkan' in line])
        self.assertIn("NVIDIA graphics: its maker's driver, installed your Linux's own way.", nvidia['not_automatic'])

    def test_it_never_says_steam_the_account_or_the_controller_are_set_up_for_you(self):
        found = self.plan()
        told = ' '.join(found['not_automatic'])
        for part in (
            'Signing in to Steam',
            'Steam Guard',
            'Remote Play',
            'Pairing a controller',
            'resolution',
            'sound output',
        ):
            self.assertIn(part, told)
        for promise in ('automatic', 'signs you in', 'pairs your', 'sets up your account'):
            self.assertNotIn(promise, ' '.join(found['installs']).lower())

    def test_a_pc_that_cannot_have_it_says_why_and_installs_nothing(self):
        for found, why in (
            (self.plan(has_desktop=False), 'No desktop on this PC'),
            (self.plan(family=FakeLinux('rhel')), 'no Steam package'),
            (
                self.plan(family=FakeLinux('suse', release={'ID': 'opensuse-leap'})),
                'openSUSE Leap has no Steam package',
            ),
        ):
            self.assertEqual((found['possible'], found['installs'], found['packages']), (False, [], []))
            self.assertIn(why, found['reason'])
        self.config['media_apps'] = {'movies': False}
        off = self.plan()
        self.assertFalse(off['possible'])
        self.assertIn('Movies and shows is switched off', off['reason'])

    def test_steam_not_offered_yet_says_what_the_person_switches_on(self):
        for family in (FakeLinux(i386=False), FakeLinux(offered=False)):
            found = self.plan(family=family)
            self.assertTrue(found['possible'])
            self.assertIn('dpkg --add-architecture i386', found['needs_first'])
            self.assertIn('NekoServer changes no software source itself', found['needs_first'])


class TvTestsTests(Base):
    def statuses(self):
        return {row['id']: row['status'] for row in extras_page.tv_tests(self.config)}

    def test_four_separate_rows_all_not_tested_at_first(self):
        rows = extras_page.tv_tests(self.config)
        self.assertEqual([row['id'] for row in rows], ['tv:picture', 'tv:sound', 'tv:controller', 'tv:steam'])
        for row in rows:
            self.assertEqual((row['status'], row['by']), ('not_tested', 'person'))
            self.assertTrue(row['title'] and row['look_for'])

    def test_each_result_is_kept_on_its_own(self):
        extras_page.record_tv_test(self.config, 'picture', 'worked', now=10.0)
        after = extras_page.record_tv_test(self.config, 'sound', 'did_not_work', now=20.0)
        self.assertEqual([row['status'] for row in after], ['worked', 'did_not_work', 'not_tested', 'not_tested'])
        self.assertEqual(
            self.statuses(),
            {
                'tv:picture': 'worked',
                'tv:sound': 'did_not_work',
                'tv:controller': 'not_tested',
                'tv:steam': 'not_tested',
            },
        )
        extras_page.record_tv_test(self.config, 'sound', 'not_tested')  # taken back
        self.assertEqual(self.statuses()['tv:sound'], 'not_tested')
        self.assertEqual(self.statuses()['tv:picture'], 'worked')

    def test_only_the_four_tests_and_three_results_are_taken(self):
        for test, result in (
            ('wifi', 'worked'),
            ('picture', 'yes'),
            ('picture', True),
            (None, 'worked'),
            ('tv:picture', 'worked'),
        ):
            with self.assertRaises(ValueError):
                extras_page.record_tv_test(self.config, test, result)
        self.assertEqual(set(self.statuses().values()), {'not_tested'})

    def test_what_the_tv_step_was_told_before_counts_and_a_newer_answer_wins(self):
        report = self.folder / tv.REPORT
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    'checks': [
                        {'check': 'tv:display_audio_playback', 'status': 'user_confirmed'},
                        {'check': 'tv:controller', 'status': 'needs_setup'},
                    ]
                }
            )
        )
        self.assertEqual(
            self.statuses(),
            {'tv:picture': 'worked', 'tv:sound': 'worked', 'tv:controller': 'not_tested', 'tv:steam': 'not_tested'},
        )
        extras_page.record_tv_test(self.config, 'sound', 'did_not_work')
        self.assertEqual(self.statuses()['tv:sound'], 'did_not_work')

    def test_a_damaged_file_is_no_result(self):
        path = self.folder / 'reports' / 'tv-tests.json'
        path.parent.mkdir(parents=True)
        for content in (
            'nonsense',
            '[]',
            '{"format": "other", "tests": {"picture": {"status": "worked"}}}',
            '{"format": "neko-tv-tests-v1", "tests": {"picture": {"status": "great"}}}',
        ):
            path.write_text(content)
            self.assertEqual(set(self.statuses().values()), {'not_tested'}, content)


class AlertsChangeTests(Base):
    def setUp(self):
        super().setUp()
        self.private = self.folder / 'alerts.json'
        self.topic = notifications.new_topic_name()
        self.address = notifications.topic_address(self.topic, 'https://alerts.example')
        self.token = 'tk' + '_' + secrets.token_hex(12)

    def save(self, address, token=''):
        self.private.write_text(json.dumps({'endpoint': address, 'token': token}))

    def change(self, address, token=''):
        return extras_page.alerts_change(address, token, private=self.private)

    def test_a_topic_is_shown_by_its_server_and_four_of_its_characters(self):
        shown = extras_page.redacted(self.address)
        self.assertEqual(shown, f'https://alerts.example/{self.topic[:2]}…{self.topic[-2:]} (29 characters)')
        self.assertNotIn(self.topic, shown)
        # A short topic is somebody's own word: nothing of it is shown.
        self.assertEqual(
            extras_page.redacted('https://alerts.example/my-server'), 'https://alerts.example/… (9 characters)'
        )
        for bad in ('http://alerts.example/' + self.topic, self.address + '?x=1', '', None, 'nonsense'):
            self.assertEqual(extras_page.redacted(bad), '')

    def test_current_and_new_are_both_redacted_for_the_one_save(self):
        self.save(self.address, self.token)
        other = notifications.topic_address(notifications.new_topic_name(), 'https://ntfy.example')
        found = self.change(other, self.token)
        self.assertEqual(
            found,
            {
                'has_current': True,
                'current': extras_page.redacted(self.address),
                'new': extras_page.redacted(other),
                'new_ok': True,
                'changes': 'topic',
                'token': 'kept',
            },
        )
        for secret in (self.topic, self.address, other, other.rsplit('/', 1)[1], self.token):
            self.assertNotIn(secret, json.dumps(found))

    def test_what_changes_is_said_exactly(self):
        self.save(self.address, self.token)
        new_token = 'tk' + '_' + secrets.token_hex(12)
        self.assertEqual(self.change(self.address, self.token)['changes'], 'nothing')
        same_topic = self.change(self.address, new_token)
        self.assertEqual((same_topic['changes'], same_topic['token']), ('token', 'new'))
        dropped = self.change(self.address)
        self.assertEqual((dropped['changes'], dropped['token']), ('token', 'removed'))
        self.save(self.address)
        self.assertEqual(self.change(self.address)['token'], 'none')

    def test_a_server_without_alerts_has_no_current_one(self):
        first = self.change(self.address, self.token)
        self.assertEqual((first['has_current'], first['current'], first['changes']), (False, '', 'first'))
        self.save('')
        self.assertEqual(self.change(self.address)['changes'], 'first')

    def test_a_new_address_or_token_that_is_not_acceptable_is_not_shown_or_compared(self):
        self.save(self.address, self.token)
        for address, token in ((self.address + '?x=1', self.token), (self.address, self.token + ' x'), ('', '')):
            found = self.change(address, token)
            self.assertEqual((found['new_ok'], found['new'], found['changes']), (False, '', 'unknown'))
            self.assertEqual(found['current'], extras_page.redacted(self.address))

    def test_a_saved_file_that_cannot_be_read_is_unknown_not_none(self):
        for content in ('nonsense', '[]'):
            self.private.write_text(content)
            found = self.change(self.address)
            self.assertEqual((found['has_current'], found['current'], found['changes']), (None, '', 'unknown'))

    def test_nothing_is_saved_logged_or_printed(self):
        self.save(self.address, self.token)
        before = self.private.read_text()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed), self.assertNoLogs(level='DEBUG'):
            self.change(notifications.topic_address(notifications.new_topic_name()), self.token)
            extras_page.redacted(self.address)
        self.assertEqual((self.private.read_text(), printed.getvalue()), (before, ''))


if __name__ == '__main__':
    unittest.main()
