"""1.0 round T1-R10: after a new HDD, setup run before the photos are restored said only
"Immich administrator: not verified (it did not answer within 180 seconds; it may still be
starting ...)". Immich was in fact restarting over and over: its database on the SSD knows
its folders, and none of them is on the new drive (setup makes the missing ones again only
once the restored photos are there, T1-R6). Waiting never helps, so setup says to restore
the photos first and that Immich starts once setup is run again after that."""

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


class PhotosFirstTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        top = Path(tmp.name)
        self.config = {'data_mount': str(top / 'ssd'), 'media_mount': str(top / 'media'), 'accounts': {'enabled': True}}
        self.library = neko_common.photo_library(self.config)
        self.library.mkdir(parents=True)  # setup made the photo folder on the new drive
        self.db = top / 'ssd' / 'appdata' / 'immich' / 'postgres'
        self.db.mkdir(parents=True)
        update = mock.patch.object(account_store, 'update')
        update.start()
        self.addCleanup(update.stop)

    def said(self, exc):
        lines, pending = [], []
        entry = {'username': 'admin@example.invalid', 'allow_create': True}
        account_setup.not_verified(self.config, 'immich', entry, exc, lines.append, pending)
        self.assertEqual(pending, ['immich'])
        return ' '.join(lines)

    def test_a_new_hdd_before_the_photo_restore_says_to_restore_the_photos_first(self):
        (self.db / 'PG_VERSION').write_text('16')  # Immich's database knows its photos
        said = self.said(account_setup.NotAnswering(0, 180))
        self.assertIn('Immich administrator: not verified', said)
        self.assertIn('its photos are not on this drive', said)
        self.assertIn('Restore the photos first', said)
        self.assertIn('sudo bash install.sh --recover', said)
        self.assertIn('"Photos and videos (Immich)"', said)
        self.assertIn('Immich starts once you run  sudo bash install.sh  again after that', said)
        self.assertNotIn('may still be starting', said)
        self.assertIn('the saved login is kept', said)

    def test_with_the_photos_back_or_a_new_immich_it_still_says_it_may_be_starting(self):
        new_immich = self.said(account_setup.NotAnswering(0, 180))  # empty database: a new Immich
        self.assertIn('may still be starting', new_immich)
        (self.db / 'PG_VERSION').write_text('16')
        (self.library / 'upload').mkdir()  # the restored photos are there
        restored = self.said(account_setup.NotAnswering(0, 180))
        self.assertIn('may still be starting', restored)
        self.assertNotIn('Restore the photos first', restored)

    def test_another_reason_or_another_app_is_said_as_before(self):
        (self.db / 'PG_VERSION').write_text('16')
        refused = self.said(RuntimeError('Authenticated account identity/administrator check did not match'))
        self.assertIn('check did not match', refused)
        self.assertNotIn('Restore the photos first', refused)
        lines = []
        account_setup.not_verified(self.config, 'jellyfin', {}, account_setup.NotAnswering(0, 180), lines.append, [])
        self.assertNotIn('Restore the photos first', ' '.join(lines))


if __name__ == '__main__':
    unittest.main()
