"""1.0 round T1-R10: after a new HDD, setup run before the photos are restored said only
"Immich administrator: not verified (it did not answer within 180 seconds; it may still be
starting ...)". Immich was in fact restarting over and over: its database on the SSD knows
its folders, none of them is on the new drive (setup makes the missing ones again only once
the restored photos are there, T1-R6), and its log says so at every start. Waiting never
helps, so setup says to restore the photos first and that Immich starts once setup is run
again after that. A new Immich that is slow or stopped is never told to restore anything."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_setup  # noqa: E402
import account_store  # noqa: E402
import neko_common  # noqa: E402

# Immich's own words when a folder its database knows has lost its marker (T1-R6's log).
LOOP = (
    '[Nest] 7  - 11/22/2026, 9:14:03 PM   ERROR [Microservices:StorageService] Failed to read '
    '(/data/encoded-video/.immich): Error: ENOENT: no such file or directory, open '
    "'/data/encoded-video/.immich'\n"
    'Please see https://docs.immich.app/administration/system-integrity#folder-checks\n'
)
STARTING = '[Nest] 7  - 11/22/2026, 9:14:03 PM     LOG [Api:EventRepository] Running migrations\n'


class PhotosFirstTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        top = Path(tmp.name)
        self.config = {'data_mount': str(top / 'ssd'), 'media_mount': str(top / 'media'), 'accounts': {'enabled': True}}
        self.library = neko_common.photo_library(self.config)
        self.library.mkdir(parents=True)  # setup made the photo folder on the drive
        db = top / 'ssd' / 'appdata' / 'immich' / 'postgres'
        db.mkdir(parents=True)
        (db / 'PG_VERSION').write_text('16')  # PostgreSQL made its files at its first start, also when new
        update = mock.patch.object(account_store, 'update')
        update.start()
        self.addCleanup(update.stop)
        self.log = LOOP
        self.asked = []
        docker = mock.patch.object(account_setup, 'docker', side_effect=self.docker)
        docker.start()
        self.addCleanup(docker.stop)

    def docker(self, args, input_text=None):
        self.asked.append(args)
        if args[0] == 'ps':
            return 'c0ffee12\n' if self.log is not None else ''
        if args[0] == 'logs':
            return self.log
        raise AssertionError(args)

    def said(self, exc, app='immich'):
        lines, pending = [], []
        entry = {'username': 'admin@example.invalid', 'allow_create': True}
        account_setup.not_verified(self.config, app, entry, exc, lines.append, pending)
        self.assertEqual(pending, [app])
        return ' '.join(lines)

    def test_a_new_hdd_before_the_photo_restore_says_to_restore_the_photos_first(self):
        for exc in (account_setup.NotAnswering(0, 180), account_setup.NotRunning('Immich is not running')):
            said = self.said(exc)
            self.assertIn('Immich administrator: not verified', said)
            self.assertIn('its photos are not on this drive', said)
            self.assertIn('Restore the photos first', said)
            self.assertIn('sudo bash install.sh --recover', said)
            self.assertIn('"Photos and videos (Immich)"', said)
            self.assertIn('Immich starts once you run  sudo bash install.sh  again after that', said)
            self.assertNotIn('may still be starting', said)
            self.assertIn('the saved login is kept', said)
        self.assertIn(['logs', '--tail', '200', 'c0ffee12'], self.asked)

    def test_a_fresh_install_that_is_slow_or_stopped_is_never_told_to_restore_photos(self):
        # A new Immich: its database has files, none of its folders is made yet (it makes
        # them once it is up), and it is not answering, still migrating or gone.
        for log in (STARTING, '', None):
            self.log = log
            said = self.said(account_setup.NotAnswering(0, 180))
            self.assertIn('may still be starting', said)
            self.assertNotIn('Restore the photos first', said)

    def test_with_the_photos_back_it_says_it_may_be_starting(self):
        (self.library / 'upload').mkdir()  # the restored photos are there
        said = self.said(account_setup.NotAnswering(0, 180))
        self.assertIn('may still be starting', said)
        self.assertNotIn('Restore the photos first', said)
        self.assertEqual(self.asked, [])  # nothing asked of Docker then

    def test_docker_that_cannot_say_leaves_the_words_as_before(self):
        self.docker = mock.Mock(side_effect=RuntimeError('Local account setup command failed'))
        account_setup.docker.side_effect = self.docker
        said = self.said(account_setup.NotAnswering(0, 180))
        self.assertIn('may still be starting', said)

    def test_another_reason_or_another_app_is_said_as_before(self):
        refused = self.said(RuntimeError('Authenticated account identity/administrator check did not match'))
        self.assertIn('check did not match', refused)
        self.assertNotIn('Restore the photos first', refused)
        other = self.said(account_setup.NotAnswering(0, 180), app='jellyfin')
        self.assertNotIn('Restore the photos first', other)
        self.assertEqual(self.asked, [])


if __name__ == '__main__':
    unittest.main()
