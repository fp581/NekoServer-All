"""Deep checks 241-260: whole tasks a beginner does, followed only by the docs and the
product's own messages ("I deleted a movie by mistake", "I forgot the backup password")."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_secrets  # noqa: E402
import outside_request  # noqa: E402
import recovery_wizard  # noqa: E402
from modules import notifications  # noqa: E402
import test_outside_tests  # noqa: E402


def doc(name):
    return (ROOT / name).read_text()


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class DeletedFilmTests(unittest.TestCase):
    """Deep check 253: a film deleted by mistake. The recovery menu listed photos, Nextcloud
    files, passwords and settings, and said nothing about films: the beginner looked for a
    line that is never there, and RECOVERY.md's "Which situation are you in?" had no row."""

    def said(self, config):
        out = []
        recovery_wizard.restore_section(config, answers('0'), out.append)
        return '\n'.join(out)

    def test_the_restore_list_says_films_and_music_are_not_backed_up(self):
        said = self.said({'media_mount': '/nonexistent/hdd', 'data_mount': '/nonexistent/ssd'})
        self.assertIn('Films, shows and music are not backed up, so they are not in this list.', said)
        self.assertIn('Radarr or Sonarr', said)
        # Said before the list, where it is read.
        self.assertLess(said.index('not backed up'), said.index('What would you like to restore?'))

    def test_a_server_without_films_or_music_is_not_told_about_them(self):
        config = {
            'media_mount': '/nonexistent/hdd',
            'data_mount': '/nonexistent/ssd',
            'media_apps': {'photos': True, 'movies': False, 'music': False},
        }
        self.assertNotIn('Films, shows and music', self.said(config))

    def test_recovery_md_has_a_row_for_a_deleted_film(self):
        table = doc('RECOVERY.md').split('## Which situation are you in?')[1].split('\n## ')[0]
        row = next(line for line in table.splitlines() if 'deleted a film' in line)
        self.assertIn('not backed up', row)


@unittest.skipUnless(shutil.which('openssl'), 'the openssl command is not installed')
class ForgottenBackupPasswordTests(unittest.TestCase):
    """Deep check 243: the backup password forgotten. A wrong one got "Wrong password for this
    backup" and nothing else, no doc said what to do, and no command could choose a new one
    (--setup says "already set up")."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = Path(self.tmp.name) / 'keys'
        self.out = []
        backup_secrets.setup(ask=answers('first password', 'first password'), out=self.out.append, key_dir=self.keys)

    def test_a_wrong_password_says_what_to_do_when_it_is_forgotten(self):
        with self.assertRaises(RuntimeError) as caught:
            backup_secrets.opening_key(
                self.keys / backup_secrets.KEY_NAME, self.keys / backup_secrets.CERT_NAME, 'guess', False, self.tmp.name
            )
        said = str(caught.exception)
        self.assertIn('nothing was restored', said)
        self.assertIn('account-setup.py show', said)
        self.assertIn('--new-password', said)

    def test_a_new_backup_password_keeps_the_old_key_beside_it(self):
        old = (self.keys / backup_secrets.KEY_NAME).read_bytes()
        backup_secrets.new_password(answers('second password', 'second password'), self.out.append, self.keys, 'T')
        self.assertTrue(backup_secrets.unlocks('second password', self.keys / backup_secrets.KEY_NAME))
        self.assertEqual((self.keys / (backup_secrets.KEY_NAME + '.before-T')).read_bytes(), old)
        self.assertTrue((self.keys / (backup_secrets.CERT_NAME + '.before-T')).is_file())
        self.assertIn('sudo bash install.sh', self.out[-1])

    def test_no_new_password_confirmed_leaves_the_old_one(self):
        old = (self.keys / backup_secrets.KEY_NAME).read_bytes()
        typed = answers('new password', 'typo', 'short', 'x')
        with self.assertRaisesRegex(RuntimeError, 'nothing changed'):
            backup_secrets.new_password(typed, self.out.append, self.keys, 'T')
        self.assertEqual((self.keys / backup_secrets.KEY_NAME).read_bytes(), old)
        self.assertEqual(
            sorted(p.name for p in self.keys.iterdir()), [backup_secrets.KEY_NAME, backup_secrets.CERT_NAME]
        )

    def test_the_guide_says_what_to_do(self):
        step = doc('GUIDED-SETUP.md').split('### 14. The backup password')[1].split('\n### ')[0]
        self.assertIn('Forgot it?', step)
        self.assertIn('sudo python3 backup_secrets.py --new-password', step)


class WhereTheDocsSayItTests(unittest.TestCase):
    """Tasks the docs had no word for: a new PC (244: RECOVERY.md spoke only of replacing
    Linux on the same PC), a friend watching your films (245) and a new name for the server
    (257: its name is in every app address)."""

    def situations(self):
        return doc('RECOVERY.md').split('## Which situation are you in?')[1].split('\n## ')[0]

    def test_moving_to_a_new_pc_has_its_row(self):
        row = next(line for line in self.situations().splitlines() if 'new PC' in line)
        self.assertIn('#replace-the-os-and-reattach-the-existing-store', row)
        self.assertIn('Tailscale', row)

    def test_a_friend_and_a_new_name_are_in_the_readme(self):
        use = doc('README.md').split('## Using your server')[1].split('\n## ')[0]
        friend = use.split('**A friend should see your films.**')[1].split('\n\n')[0]
        for words in ('Users', 'administrator', 'Share', ':8443'):
            self.assertIn(words, friend)
        rename = use.split('**Renaming the server.**')[1].split('\n\n')[0]
        self.assertIn('sudo bash install.sh', rename)


class AlertsStoppedTests(unittest.TestCase):
    """Deep check 250: the phone stopped getting alerts (the access token was taken back in
    ntfy). The dashboard said "check its address and access token (setup can change them)",
    but setup again kept both without a word and called the step done: the alerts kept
    failing. Now a rerun tests them, and asks again when the alert server refuses them."""

    # The faked system parts of the finishing screen's retry tests, borrowed as they are.
    save = test_outside_tests.FinishRetryTests.save
    retry = test_outside_tests.FinishRetryTests.retry

    def setUp(self):
        test_outside_tests.FinishRetryTests.setUp(self)
        self.delivery = self.folder / 'delivery.json'
        patch = mock.patch.object(notifications, 'DELIVERY', str(self.delivery))
        patch.start()
        self.addCleanup(patch.stop)
        notifications.guided_setup.interactive.return_value = True
        # Set up, tested and seen on the phone, weeks ago.
        self.save(notifications.PRIVATE_CONFIG, {'endpoint': self.topic, 'token': self.token})
        alerts = self.files[notifications.PRIVATE_CONFIG]
        outside_request.mark_tested(alerts, 'accepted')
        outside_request.mark_seen(alerts, True)

    def failing(self):
        self.delivery.write_text(json.dumps({'format': 'neko-delivery-v1', 'status': 'fail'}))

    def test_delivering_alerts_ask_nothing_and_send_nothing(self):
        self.delivery.write_text(json.dumps({'format': 'neko-delivery-v1', 'status': 'pass'}))
        with mock.patch.object(notifications, 'alerts_form') as form:
            state, server = self.retry(notifications, 401)
        form.assert_not_called()
        self.assertEqual((server.requests, state['status']), ([], 'complete'))

    def test_a_refused_token_is_asked_again_and_the_new_one_tested(self):
        self.failing()
        _, new_topic, new_token = test_outside_tests.a_topic()
        with (
            mock.patch.object(notifications, 'alerts_form', return_value=(new_topic, new_token)) as form,
            mock.patch.object(
                notifications, 'send_test', side_effect=[{'server': 'wrong_token'}, {'server': 'accepted'}]
            ),
            mock.patch.object(notifications.questions, 'yes_no', return_value=True),
        ):
            state, _ = self.retry(notifications, 200)
        form.assert_called_once()
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(json.loads(self.files[notifications.PRIVATE_CONFIG].read_text())['token'], new_token)

    def test_a_network_problem_is_said_and_the_step_waits(self):
        self.failing()
        with mock.patch.object(notifications, 'alerts_form') as form:
            state, server = self.retry(notifications, ConnectionRefusedError)
        form.assert_not_called()
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(state['status'], 'needs_setup')
        self.assertIn('network problem', state['reason'])


class NextcloudOffTests(unittest.TestCase):
    """Deep check 246: "Turn off Nextcloud, I don't use it". Setup said "your files and
    database are kept" but not where, nor that the weekly USB copy stops taking them, and
    the recovery menu then dropped "Nextcloud files": the copies on the USB drive could no
    longer be restored without switching Nextcloud on again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {
            'data_mount': str(self.root / 'ssd'),
            'media_mount': str(self.root / 'hdd'),
            'nextcloud': {'enabled': False},
        }

    def set_up_before(self):
        manifest = self.root / 'ssd/compose/nextcloud/compose.yaml'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('services: {}')

    def offered(self):
        return [key for key, _ in recovery_wizard.menu(self.config)]

    def test_a_nextcloud_switched_off_after_it_was_set_up_can_still_be_restored(self):
        self.assertNotIn('cloud', self.offered())  # never set up: nothing to restore
        self.set_up_before()
        self.assertIn('cloud', self.offered())

    def test_turning_it_off_says_what_is_kept_and_what_stops(self):
        from modules import nextcloud

        self.set_up_before()
        logs = []
        ctx = mock.Mock(config=self.config, apply=True, log=logs.append)
        nextcloud.stop_installed(ctx, run_command=mock.Mock(), hold=mock.MagicMock(), journal=self.root / 'none')
        said = logs[-1]
        self.assertIn(str(self.root / 'hdd/cloud/files'), said)
        self.assertIn('USB', said)
        self.assertIn('first page', said)


if __name__ == '__main__':
    unittest.main()
