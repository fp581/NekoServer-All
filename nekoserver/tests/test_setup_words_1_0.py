"""The 1.0.0 wording round (the user: "Another fix to the wording of the installation
please"): every setup screen says the same behaviour in the same words, in the terminal and
in the setup window. Each test fails on the words before this round."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_secrets  # noqa: E402
import drive_table  # noqa: E402
import explanations  # noqa: E402
import finish_states  # noqa: E402
import installer  # noqa: E402
import questions  # noqa: E402
import wizard  # noqa: E402
from modules import storage  # noqa: E402

PREREQUISITES = 'Prerequisite packages are installed. Your selected apps will be set up after you approve.'
WINDOW = (ROOT / 'setup-gui' / 'index.html').read_text(encoding='utf-8')


class UpdatesStepTests(unittest.TestCase):
    def test_the_step_is_named_for_the_weekly_updates_and_the_monthly_check(self):
        # The weekly job installs app updates; the monthly one checks the backups and updates nothing.
        self.assertEqual(wizard.STEPS['maintenance'], 'Weekly app updates and a monthly backup check')
        said = ' '.join(wizard.WELCOME + wizard.NEXT_DONE + wizard.NEXT_CHECKUP + list(explanations.ALWAYS))
        self.assertNotIn('monthly updates', said.lower())
        self.assertNotIn('monthly checkup', said)
        self.assertIn('weekly app updates and a monthly backup check', ' '.join(wizard.WELCOME))
        self.assertIn('weekly app updates and the monthly backup check run on their own', ' '.join(wizard.NEXT_DONE))

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
        self.assertIn(f'<p id="review-intro">{explanations.REVIEW_INTRO}</p>', WINDOW)
        self.assertIn(f'id="choose-hint">{explanations.CHOOSE_NOTE}</span>', WINDOW)
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


if __name__ == '__main__':
    unittest.main()
