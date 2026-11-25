"""1.0.1 (coordinator, T1-18's question): a move that puts the backups on the drive that
holds the photos or the music, or the photos or the music on the backups' drive, backs
them up on their own drive. The move window says which, that the weekly USB copy is the
one that survives that drive (music is not in it), and needs its own tick; the job
refuses the move without it, and the Drives page shows "same drive" for that kind after."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402
from tests.test_storage_jobs import Jobs, last_result  # noqa: E402

REAL = storage_jobs.beside_backups


class BesideTests(Jobs):
    def setUp(self):
        super().setUp()
        real = mock.patch.object(storage_jobs, 'beside_backups', REAL)
        real.start()
        self.addCleanup(real.stop)
        # The backups on d3, the photos and the music where setup puts them.
        self.config['locations'] = {'backups': 'd3', 'photos': 'ssd', 'music': 'media'}

    def test_a_kind_moved_onto_the_backups_drive_is_named(self):
        self.assertEqual(storage_jobs.beside_backups(self.config, 'photos', 'd3'), ['photos'])
        self.assertEqual(storage_jobs.beside_backups(self.config, 'music', 'd3'), ['music'])
        self.assertEqual(storage_jobs.beside_backups(self.config, 'photos', 'media'), [])
        self.assertEqual(storage_jobs.beside_backups(self.config, 'movies', 'd3'), [])

    def test_the_backups_moved_onto_a_drive_name_what_is_there(self):
        self.config['locations'] = {'photos': 'd3', 'music': 'd3'}
        self.assertEqual(storage_jobs.beside_backups(self.config, 'backups', 'd3'), ['photos', 'music'])

    def test_a_kind_switched_off_or_not_backed_up_is_not_named(self):
        self.config['media_apps'] = {'photos': False}
        self.config['music'] = {'backup_enabled': False}
        self.assertEqual(storage_jobs.beside_backups(self.config, 'photos', 'd3'), [])
        self.assertEqual(storage_jobs.beside_backups(self.config, 'music', 'd3'), [])

    def test_the_target_says_it_and_names_the_weekly_usb_copy(self):
        found = {t['to']: t for t in storage_jobs.targets(self.config, 'photos', 0)}
        self.assertEqual(found['d3']['beside'], ['photos'])
        words = found['d3']['beside_words']
        self.assertIn('photos would then be backed up on the drive they are on', words)
        self.assertIn('weekly copy on the USB drive', words)
        self.assertEqual(found['media']['beside'], [])
        music = {t['to']: t for t in storage_jobs.targets(self.config, 'music', 0)}['d3']['beside_words']
        self.assertIn('Music is not in the weekly USB copy', music)

    def test_music_alone_is_one_thing_in_the_words(self):
        # "Your music would then be backed up on the drive they are on" (T3 on bd05f7b).
        music = {t['to']: t for t in storage_jobs.targets(self.config, 'music', 0)}['d3']['beside_words']
        self.assertIn('Your music would then be backed up on the drive it is on.', music)
        both = storage_jobs.beside_words(self.config, ['photos', 'music'])
        self.assertIn('Your photos and music would then be backed up on the drive they are on.', both)
        self.assertEqual(self.run_job('move', kind='music', to='d3'), 1)
        self.assertIn('to move it anyway', last_result(self.root)['message'])

    def test_the_move_is_refused_without_the_tick_and_made_with_it(self):
        self.assertEqual(self.run_job('move', kind='photos', to='d3'), 1)
        self.assertIn('Tick', last_result(self.root)['message'])
        self.assertIn('Nothing was changed', last_result(self.root)['message'])
        self.assertEqual(self.saved()['locations'] if self.settings[-1].exists() else {}, {})
        self.assertEqual(self.run_job('move', kind='photos', to='d3', beside_ok=True), 0)
        self.assertEqual(self.saved()['locations']['photos'], 'd3')

    def test_a_move_that_puts_nothing_beside_needs_no_tick(self):
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)

    def test_the_look_names_each_kind_on_its_backups_drive(self):
        self.config['locations'] = {'backups': 'd3', 'photos': 'd3', 'music': 'media'}
        self.assertEqual(storage_jobs.beside_now(self.config), ['photos'])


if __name__ == '__main__':
    unittest.main()
