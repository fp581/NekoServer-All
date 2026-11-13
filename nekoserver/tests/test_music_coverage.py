"""Gate run C7: music on a drive of its own was in no backup job (the nightly music copy is
off by default since it was retired). Every place that holds data is in a backup or
listed as not backed up: music on a drive of its own is copied when Music is on."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_plan  # noqa: E402
import neko_common  # noqa: E402
from modules import music_backup  # noqa: E402

OWN = {
    'data_mount': '/srv/nekoserver',
    'media_mount': '/mnt/nekoserver-media',
    'media_apps': {'movies': False, 'music': True, 'photos': False},
    'music': {'backup_enabled': False},
    'storage': {'d3': {'uuid': 'hdd2-0001', 'fstype': 'ext4'}},
    'locations': {'music': 'd3'},
}


class MusicCoverageTests(unittest.TestCase):
    def test_music_on_a_drive_of_its_own_is_copied_whatever_the_old_switch(self):
        self.assertTrue(neko_common.music_backed_up(OWN))
        beside = {**OWN, 'locations': {'music': 'media'}}
        self.assertFalse(neko_common.music_backed_up(beside))
        self.assertFalse(neko_common.music_backed_up({**OWN, 'media_apps': {'music': False}}))
        self.assertTrue(neko_common.music_backed_up({**beside, 'music': {'backup_enabled': True}}))

    def test_the_copy_waits_for_that_drive(self):
        unit = music_backup.definitions(OWN, '/bin/x', '/etc/g.json')['nekoserver-music-backup.service']
        self.assertIn('RequiresMountsFor=/srv/nekoserver /mnt/nekoserver-media /mnt/nekoserver-drives/d3\n', unit)

    def test_the_backups_page_lists_it_once_either_way(self):
        keys = [row['key'] for row in backup_plan.table(OWN)]
        self.assertNotIn('not_copied_music', keys)
        beside = [row['key'] for row in backup_plan.table({**OWN, 'locations': {'music': 'media'}})]
        self.assertIn('not_copied_music', beside)


if __name__ == '__main__':
    unittest.main()
