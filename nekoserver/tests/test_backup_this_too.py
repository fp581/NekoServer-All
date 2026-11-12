"""G7 (the user's decision after gate run C6): data on a network share gets "Back this up
too?" once on the storage page. Music: yes unless "No", and the nightly music copy then
takes the folder too. Films and shows have no NekoServer backup job: not asked, listed
as not backed up."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_plan  # noqa: E402
import existing_media  # noqa: E402
import existing_page  # noqa: E402
import neko_common  # noqa: E402

ON_SHARE = {'path': '/mnt/tank/music', 'mount': '/mnt/tank', 'share': 'nas:/music', 'fstype': 'nfs4', 'new_files': {}}
LOCAL = {**ON_SHARE, 'path': '/media/neko/disk/music', 'mount': '/media/neko/disk', 'share': '', 'fstype': 'ext4'}


def asked(kind, found, typed_answers):
    answers = iter(typed_answers)
    kept, said = [], []
    with (
        mock.patch.object(existing_page, 'folder_found', return_value=found),
        mock.patch.object(existing_page, 'typed', side_effect=lambda key, prompt, **v: kept.append(key) or next(answers)),
        mock.patch.object(existing_page, 'downloads', return_value=False),
        mock.patch.object(existing_media, 'with_choice', side_effect=lambda c, e: {'placement': {'entry': e}}),
    ):
        config = {}
        existing_page.one_kind(config, kind, kind, (None, said.append))
    return config['placement']['entry'], kept


class AskedTests(unittest.TestCase):
    def test_music_on_a_share_is_asked_and_enter_backs_it_up(self):
        entry, keys = asked('music', ON_SHARE, ['1', ''])
        self.assertEqual(keys, ['existing_use', 'existing_backup'])
        self.assertIs(entry['backup'], True)
        entry, _ = asked('music', ON_SHARE, ['1', '2'])
        self.assertIs(entry['backup'], False)

    def test_a_local_folder_and_films_are_not_asked(self):
        entry, keys = asked('music', LOCAL, ['1'])
        self.assertEqual(keys, ['existing_use'])
        self.assertNotIn('backup', entry)
        entry, keys = asked('movies', {**ON_SHARE, 'path': '/mnt/tank/films'}, ['1'])
        self.assertEqual(keys, ['existing_use'])


class FollowedTests(unittest.TestCase):
    def config(self, backup):
        entry = {'path': '/mnt/tank/music', 'holds': 'music', 'use': 'in_place', 'new_files': False, **ON_SHARE,
                 'backup': backup}  # fmt: skip
        entry.pop('new_files')
        import copy

        import placement

        config = {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/mnt/nekoserver-media',
            'media_apps': {'movies': False, 'music': True, 'photos': False},
            'music': {'backup_enabled': False},
            'storage': {'data_ssd': {'uuid': 'ssd-1', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'hdd-1', 'fstype': 'ext4'}},
        }
        table = copy.deepcopy(placement.table(config))
        table['music']['extra'] = [{**entry, 'new_files': False}]
        config['placement'] = table
        return config

    def test_yes_puts_the_folder_in_the_music_copy_and_on_the_backups_page(self):
        config = self.config(True)
        with mock.patch.object(existing_media, 'extras', return_value=config['placement']['music']['extra']):
            self.assertTrue(neko_common.music_backed_up(config))
            folders = existing_media.backed_up(config)
            self.assertEqual([f['slot'] for f in folders], ['music'])
            self.assertTrue(folders[0]['path'].endswith('library/existing/music'))
            rows = {row['key']: row for row in backup_plan.table(config)}
        self.assertIn('your own music folder', rows['music']['what'])
        self.assertFalse([key for key in rows if key.startswith('not_copied_music')])

    def test_no_keeps_it_listed_as_not_backed_up(self):
        config = self.config(False)
        with mock.patch.object(existing_media, 'extras', return_value=config['placement']['music']['extra']):
            self.assertFalse(neko_common.music_backed_up(config))
            self.assertEqual(existing_media.backed_up(config), [])
            keys = [row['key'] for row in backup_plan.table(config)]
        self.assertIn('not_copied_music', keys)
        self.assertNotIn('music', keys)


if __name__ == '__main__':
    unittest.main()
