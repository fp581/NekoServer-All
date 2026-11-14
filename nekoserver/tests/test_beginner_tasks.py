"""Deep checks 241-260: whole tasks a beginner does, followed only by the docs and the
product's own messages ("I deleted a movie by mistake", "I forgot the backup password")."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_secrets  # noqa: E402
import recovery_wizard  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
