"""The 1.0.0 wording round (the user: "Another fix to the wording of the installation
please"): every setup screen says the same behaviour in the same words, in the terminal and
in the setup window. Each test fails on the words before this round."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import json  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from types import MappingProxyType  # noqa: E402
from unittest import mock  # noqa: E402

import backup_secrets  # noqa: E402
import drive_table  # noqa: E402
import explanations  # noqa: E402
import finish_page  # noqa: E402
import finish_states  # noqa: E402
import guided_setup  # noqa: E402
import installer  # noqa: E402
import questions  # noqa: E402
import wizard  # noqa: E402
from modules import capacity, notifications, storage  # noqa: E402

GIB = 1024**3

PREREQUISITES = 'Prerequisite packages are installed. Your selected apps will be set up after you approve.'
WINDOW = (ROOT / 'setup-gui' / 'index.html').read_text(encoding='utf-8')


def said_as_one(lines):
    """Terminal lines read as one sentence: their indents and line breaks as single spaces."""
    return ' '.join(' '.join(lines).split())


class UpdatesStepTests(unittest.TestCase):
    def test_the_step_is_named_for_the_weekly_updates_and_the_monthly_check(self):
        # The weekly job installs app updates; the monthly one checks the backups and updates nothing.
        self.assertEqual(wizard.STEPS['maintenance'], 'Weekly app updates and a monthly backup check')
        said = ' '.join(wizard.WELCOME + wizard.NEXT_DONE + wizard.NEXT_CHECKUP + list(explanations.ALWAYS))
        self.assertNotIn('monthly updates', said.lower())
        self.assertNotIn('monthly checkup', said)
        self.assertIn('weekly app updates and a monthly backup check', ' '.join(wizard.WELCOME))
        self.assertIn(
            'the weekly app updates and the monthly backup check run on their own', said_as_one(wizard.NEXT_DONE)
        )

    def test_the_steps_explanation_says_what_each_job_does(self):
        said = explanations.STEP_EXPLANATIONS['maintenance']
        self.assertIn('weekly app updates', said)
        self.assertIn('monthly backup check', said)
        self.assertNotIn('monthly update', said)


class NothingInstalledYetTests(unittest.TestCase):
    def test_a_first_install_says_the_packages_are_in_and_the_apps_wait_for_the_approval(self):
        for words in (explanations.REVIEW_INTRO, explanations.CHOOSE_NOTE, wizard.what_found({'kind': 'saved'})[0]):
            with self.subTest(words=words):
                self.assertIn(PREREQUISITES, words)
                self.assertNotIn('Nothing', words.split(PREREQUISITES)[0])
        # The window's own first words, before setup's answer replaces them, are the same.
        # (Prettier wraps the window's long lines: read them as one.)
        window = ' '.join(WINDOW.split())
        self.assertRegex(window, r'id="review-intro">\s*' + re.escape(explanations.REVIEW_INTRO) + r'\s*</p>')
        self.assertRegex(window, r'id="choose-hint"\s*>\s*' + re.escape(explanations.CHOOSE_NOTE) + r'\s*</span')
        self.assertNotIn('Nothing is installed yet', WINDOW)
        self.assertNotIn('Nothing has been installed yet', WINDOW)

    def test_a_server_set_up_already_still_says_nothing_changes_before_the_button(self):
        self.assertNotIn('Prerequisite', explanations.REVIEW_INTRO_AGAIN + explanations.CHOOSE_NOTE_AGAIN)


class BackupPasswordTests(unittest.TestCase):
    def test_the_question_says_what_the_password_encrypts(self):
        self.assertEqual(
            explanations.BACKUP_PASSWORD_ASKED, 'choose a password to encrypt your saved app logins. Enter it twice'
        )
        said = []
        # No try at all: only the words before the password are said, and no key is made.
        with self.assertRaises(RuntimeError):
            backup_secrets.setup(ask=lambda question='': '', out=said.append, key_dir=ROOT / 'no-such-dir', tries=0)
        self.assertIn('Choose a password to encrypt your saved app logins. Enter it twice', ' '.join(said))
        self.assertNotIn('typed twice', ' '.join(said))


class DriveRoleTests(unittest.TestCase):
    def test_the_roles_are_named_by_what_they_are_for(self):
        self.assertEqual(
            [drive_table.role(key) for key in ('data_ssd', 'media_hdd', 'usb')],
            ['the apps drive', 'the media and backups drive', 'the offline backup drive'],
        )
        self.assertEqual(
            list(storage.PAGE_ROLES.values()),
            ['Apps drive', 'Media and backups drive', 'Offline backup drive (optional)'],
        )
        self.assertEqual(storage.PAGE_PLACES, {'ssd': 'Apps drive', 'media': 'Media and backups drive'})

    def test_ssd_and_hdd_are_left_only_as_a_drives_kind(self):
        roles = [
            *[words for (words,) in drive_table.ROLES.values()],
            *storage.PAGE_ROLES.values(),
            *storage.PAGE_PLACES.values(),
            *storage.DRIVE_WORDS.values(),
            *storage.PLACE_SAID.values(),
            storage.INSTALL_HERE,
            storage.PHOTOS_ON_MEDIA,
            storage.MUSIC_ON_MEDIA,
            *[label for _, label, _ in storage.DRIVES],
            *installer.REQUIRED.values(),
            explanations.STEP_EXPLANATIONS['recovery'],
            explanations.STEP_EXPLANATIONS['docker'],
            explanations.STEP_EXPLANATIONS['photo_preservation'],
            explanations.STEP_EXPLANATIONS['photo_backup'],
            *wizard.STEPS.values(),
        ]
        for words in roles:
            with self.subTest(words=words):
                self.assertNotRegex(words, r'\b(SSD|HDD)\b|USB (backup|drive)')


class FinishStateTests(unittest.TestCase):
    def test_the_login_state_is_login_verified_and_says_what_that_means(self):
        self.assertIn(('login', 'Login verified'), wizard.APP_STATES)
        verified = finish_states.login({'logins': {'jellyfin': 'verified'}}, 'jellyfin', True)
        self.assertEqual(wizard.state_said(verified), 'done (NekoServer used the saved login successfully)')
        refused = finish_states.login({'logins': {'jellyfin': 'needs_setup'}}, 'jellyfin', True)
        self.assertEqual(
            wizard.state_said(refused), "needs fixing (NekoServer couldn't sign in with the saved credentials)"
        )


class QuestionWordsTests(unittest.TestCase):
    def test_the_nas_button_and_the_other_folder_question(self):
        self.assertEqual(questions.words('nas_address')['send'], 'Find my NAS')
        for what in ('films', 'shows', 'music'):
            with self.subTest(what=what):
                self.assertEqual(
                    questions.words('existing_more_folder', what=what, cap=8)['title'],
                    f'Do you have {what} in another folder?',
                )

    def test_a_refused_login_says_nekoserver_could_not_sign_in(self):
        self.assertEqual(
            questions.words('login_retry', app='Jellyfin')['title'],
            "NekoServer couldn't sign in to Jellyfin with the saved credentials. What now?",
        )


class CardAndAccessWordsTests(unittest.TestCase):
    def test_the_movies_and_music_cards(self):
        cards = {entry['id']: entry['explanation'] for entry in explanations.EXTRAS}
        self.assertEqual(cards['media_movies'], 'Watch your movies and shows.')
        self.assertEqual(cards['media_music'], 'Stream your music collection.')

    def test_tailscale_is_access_over_the_internet_through_the_private_network(self):
        line = 'access your server over the internet through your private Tailscale network'
        self.assertIn(f'Tailscale: {line}. The server is not opened to the public internet', explanations.ALWAYS)
        self.assertIn('access through your private Tailscale network are always set up', explanations.PAGE_HINT)
        self.assertIn('access through your private Tailscale network are always set up', ' '.join(WINDOW.split()))

    def test_the_alerts_address_is_the_ntfy_address_the_phone_subscribes_to(self):
        said = 'Your ntfy notification address: the address your phone subscribes to for server alerts'
        self.assertIn(said, Path(notifications.__file__).read_text(encoding='utf-8'))
        self.assertEqual(explanations.ASKED_DURING['notifications'], said[0].lower() + said[1:])


class QuestionWordsSecondRoundTests(unittest.TestCase):
    def test_a_lost_drive_is_asked_as_permanently_unavailable(self):
        asked = questions.words('install_drive_lost', apps='Jellyfin')
        self.assertEqual(asked['title'], 'Is this drive permanently unavailable?')
        self.assertIn('If it is temporarily disconnected, reconnect it instead', asked['hint'])
        self.assertIn('The drive that Jellyfin are installed on is not connected', asked['hint'])

    def test_the_model_hint_says_more_detail_more_memory_slower(self):
        self.assertTrue(
            questions.words('assistant_model')['hint'].startswith(
                'Larger models may give more detailed answers, but use more memory and can respond more slowly.'
            )
        )

    def test_a_small_drive_asks_to_continue_with_it(self):
        self.assertEqual(questions.words('drive_space', which='this drive')['title'], 'Continue with this drive?')
        self.assertNotIn('Go on anyway', json.dumps(questions.Q))

    def test_a_nas_without_a_login(self):
        self.assertEqual(questions.words('nas_login_needed')['no'], 'This disk does not require a login.')


class StepInstructionsTests(unittest.TestCase):
    def test_no_is_said_as_reopening_the_instructions_with_the_windows_label(self):
        said = []
        guided_setup.show_step(said.append, 'Phone', ['Open the app'], 'Enter: done', guided_setup.no_label('played'))
        self.assertIn(
            'Return to the terminal afterward. Answer No (in the setup window: Not yet) to reopen the instructions '
            'and try again; Enter: done',
            said,
        )
        said = []
        guided_setup.show_step(said.append, 'Phone', ['Open the app'], 'Enter: done', guided_setup.no_label(None))
        self.assertIn(
            'Return to the terminal afterward. Answer No to reopen the instructions and try again; Enter: done', said
        )


class DriveSpaceTests(unittest.TestCase):
    CONFIG = MappingProxyType({'data_mount': '/ssd', 'media_mount': '/hdd'})

    def warnings(self, ssd, hdd):
        free = {'/ssd': ssd * GIB, '/hdd': hdd * GIB}
        return capacity.assess(
            self.CONFIG,
            free=lambda p: free[str(p)],
            used=lambda p: 0,
            usb_space=lambda uuid, pairs, **_: None,
            dump=lambda config: 0,
            unarchived=lambda config: 0,
        )[1]

    def test_the_warning_names_the_drive_its_space_the_minimum_and_the_recommended_size(self):
        (said,) = self.warnings(60, 4000)
        self.assertTrue(
            said.startswith(
                'The apps drive has 60 GB free. That meets the minimum of 20 GB free, but a '
                'drive of 100 GB or more is recommended for'
            ),
            said,
        )

    def test_the_question_names_this_drive_or_these_drives(self):
        for warn, which in ((['low'], 'this drive'), (['low', 'lower'], 'these drives')):
            asked = []
            with (
                mock.patch.object(capacity, 'installed', return_value=False),
                mock.patch.object(capacity, 'assess', return_value=([], warn)),
                mock.patch('guided_setup.interactive', return_value=True),
                mock.patch.object(
                    questions, 'yes_no', side_effect=lambda key, *a, asked=asked, **values: asked.append(values) or True
                ),
            ):
                capacity.run(
                    type('Ctx', (), {'config': {}, 'apply': True, 'log': lambda self, m: None})(), output=lambda s: None
                )
            self.assertEqual(asked, [{'which': which}])


def a_summary(outcome, backups, steps):
    return {
        'outcome': outcome,
        'apps': [],
        'tests': [],
        'attention': [],
        'pending': [],
        'server': {'backup': {'state': 'done', 'reason': None}, 'restore': {'state': 'not_tested', 'reason': None}},
        'backups': backups,
        'scheduled': steps.get('maintenance') == 'success'
        and bool(backups)
        and all(steps.get(found['step']) == 'success' for found in backups),
    }


DONE = {'state': 'done', 'reason': None}


class FinishHeadlineTests(unittest.TestCase):
    """The three finish headlines (and the plain one where nothing is scheduled), each for its state."""

    def heading(self, summary):
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            page = finish_page.page({'dashboard': {'port': 8787}, 'data_mount': '/srv'}, summary)
        lines = wizard.states_lines(summary)
        return page['heading'], page['subheading'], lines

    def test_ready_with_the_backups_and_updates_scheduled(self):
        backups = [{'id': 'applications', 'step': 'app_backup', **DONE}]
        heading, more, lines = self.heading(
            a_summary('ready', backups, {'maintenance': 'success', 'app_backup': 'success'})
        )
        self.assertEqual(heading, 'Installation complete. Scheduled backups and updates are enabled.')
        self.assertEqual(more, 'Review the checks below to see what has been verified and what you can still try.')
        self.assertEqual(lines[1:3], ['How things stand: ' + heading, more])

    def test_ready_without_them_keeps_the_plain_headline(self):
        heading, more, _ = self.heading(a_summary('ready', [], {}))
        self.assertEqual((heading, more), ('NekoServer is ready.', ''))

    def test_something_waiting_is_setup_is_incomplete(self):
        heading, more, _ = self.heading(a_summary('pending', [], {}))
        self.assertEqual((heading, more), ('Setup is incomplete. Review the remaining steps below.', ''))
        self.assertNotIn('Your apps are installed', json.dumps(wizard.OUTCOMES))

    def test_something_failed_is_setup_needs_attention(self):
        heading, more, _ = self.heading(a_summary('attention', [], {}))
        self.assertEqual((heading, more), ('Setup needs attention.', ''))

    def test_a_failed_maintenance_step_is_not_scheduled(self):
        backups = [{'id': 'applications', 'step': 'app_backup', **DONE}]
        summary = a_summary('ready', backups, {'maintenance': 'failed', 'app_backup': 'success'})
        self.assertEqual(wizard.finish_heading(summary), ('NekoServer is ready.', ''))


class InitialBackupsTests(unittest.TestCase):
    def test_every_first_backup_passed_is_one_line(self):
        backups = [
            {'id': 'applications', 'step': 'app_backup', **DONE},
            {'id': 'host', 'step': 'host_backup', **DONE},
        ]
        self.assertEqual(wizard.backup_states({'backups': backups}), [('Initial backups completed', DONE)])

    def test_otherwise_each_backup_says_its_own_result(self):
        failed = {'state': 'failed', 'reason': 'step_failed'}
        backups = [
            {'id': 'applications', 'step': 'app_backup', **DONE},
            {'id': 'host', 'step': 'host_backup', **failed},
        ]
        found = wizard.backup_states({'backups': backups})
        self.assertEqual([label for label, _ in found], [wizard.title('app_backup'), wizard.title('host_backup')])
        self.assertEqual([state['state'] for _, state in found], ['done', 'failed'])
        self.assertNotIn(
            'Initial backups completed',
            ' '.join(wizard.states_lines({**a_summary('attention', backups, {}), 'apps': []})),
        )


class LastLinesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'data_mount': self.tmp.name}

    def screen(self, states):
        out = []
        wizard.finishing_screen(self.config, states, None, out.append)
        return said_as_one(out)

    def test_scheduled_says_installation_complete(self):
        text = self.screen({'maintenance': {'status': 'success'}, 'app_backup': {'status': 'success'}})
        self.assertIn(said_as_one(wizard.NEXT_DONE), text)
        self.assertIn('Installation complete. Scheduled backups and updates are enabled', text)

    def test_done_without_the_schedules_does_not_claim_them(self):
        text = self.screen({'storage': {'status': 'success'}})
        self.assertNotIn('Scheduled backups and updates are enabled', text)
        self.assertIn(said_as_one(wizard.NEXT_DONE_UNSCHEDULED), text)


if __name__ == '__main__':
    unittest.main()
