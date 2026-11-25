"""T1-18 (1.0.1 drill 7): with Extra drive d3 gone (a blank disk in its place), "Move
back" of films from their old copy on the media drive was taken, ended "The job stopped
after the switch: [Errno 1] Operation not permitted: '/mnt/nekoserver-drives/d3/library'",
and the start failed the same way every time, holding up every other job. Now every drive a
job uses is checked before it starts and before each write (there, and the same drive); the
"Move back" from a lost drive is the way out; the start finishes or undoes a cut-off job
without a drive that is away; and a job the start can not finish can be tried again or
given up."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate  # noqa: E402
import neko_common  # noqa: E402
import storage_jobs  # noqa: E402
from tests.test_storage_jobs import ID, Jobs, copy, last_result, saved_journal  # noqa: E402

OLD = 'f' * 16


def listing(folder):
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob('*'))


def journal(root, action, phase, request, **undo):
    storage_jobs.write_state(
        'journal.json',
        {
            'id': ID,
            'job': action,
            'phase': phase,
            'committed': True,
            'boot': 'before',
            'started_at': 1.0,
            'request': {'format': storage_jobs.FORMAT, 'id': ID, 'job': action, **request},
            'undo': undo,
        },
        Path(root) / 'state',
    )


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.config = {'data_mount': '/srv/x', 'media_mount': '/mnt/y', 'storage': {'d3': {'uuid': 'd3-uuid'}}}

    def state(self, now):
        with mock.patch.object(neko_common, 'mounted_uuid', return_value=now, create=True):
            return neko_common.drive_away(self.config, 'd3')

    def test_a_drive_is_there_only_mounted_and_the_same(self):
        self.assertEqual(self.state(None), 'missing')
        self.assertEqual(self.state('another-uuid'), 'other')
        self.assertIsNone(self.state('d3-uuid'))
        self.assertIsNone(self.state(''))  # a mount whose UUID can not be read

    def test_a_drive_not_in_the_settings_is_missing(self):
        self.assertEqual(neko_common.drive_away(self.config, 'd9'), 'missing')

    def test_nothing_is_mounted_at_a_plain_folder(self):
        self.assertIsNone(neko_common.mounted_uuid(Path(__file__).parent))

    def findmnt(self, code, out):
        found = mock.Mock(returncode=code, stdout=out)
        return mock.patch.object(neko_common.subprocess, 'run', return_value=found)

    def test_a_bind_of_a_folder_is_a_mount_with_its_filesystems_uuid(self):
        """Review of cdcbc14: os.path.ismount is False for a bind of a folder on the same
        filesystem (a role kept on the disk Linux runs from); findmnt shows it."""
        with self.findmnt(0, 'root-uuid\n'), mock.patch('os.path.ismount', return_value=False):
            self.assertEqual(neko_common.mounted_uuid('/srv/x'), 'root-uuid')
        with self.findmnt(1, ''):
            self.assertIsNone(neko_common.mounted_uuid('/srv/x'))
        with self.findmnt(0, '\n'):
            self.assertEqual(neko_common.mounted_uuid('/srv/x'), '')

    def test_of_two_mounts_on_one_folder_the_top_one_counts(self):
        """Review of f2a8bb7: findmnt lists a hidden lower mount first; paths reach the last."""
        with self.findmnt(0, 'd3-uuid\nanother-uuid\n'):
            self.assertEqual(neko_common.mounted_uuid('/srv/x'), 'another-uuid')

    def test_the_system_disk_and_one_drive_layouts_are_there(self):
        modules = str(Path(__file__).resolve().parents[1] / 'modules')
        sys.path.insert(0, modules)
        self.addCleanup(sys.path.remove, modules)
        import storage

        system = {
            'data_mount': '/srv/x',
            'media_mount': '/mnt/y',
            'storage': {
                key: {'uuid': 'root-uuid', 'fstype': 'ext4', 'folder': folder}
                for key, folder in storage.SYSTEM_FOLDERS.items()
            },
        }
        one = {
            'data_mount': '/srv/x',
            'media_mount': '/mnt/y',
            'storage': {
                key: {'uuid': 'one-uuid', 'fstype': 'ext4', 'folder': folder}
                for key, folder in storage.SHARED_FOLDERS.items()
            },
        }
        with mock.patch('os.path.ismount', return_value=False):
            with self.findmnt(0, 'root-uuid\n'):
                self.assertEqual([neko_common.drive_away(system, p) for p in ('ssd', 'media')], [None, None])
            with self.findmnt(0, 'one-uuid\n'):
                self.assertEqual([neko_common.drive_away(one, p) for p in ('ssd', 'media')], [None, None])
            with self.findmnt(1, ''):
                self.assertEqual(neko_common.drive_away(one, 'ssd'), 'missing')


class BeforeTheStartTests(Jobs):
    def test_a_move_to_a_drive_that_is_away_is_refused_before_anything(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.unplug('d3')
        self.assertEqual(self.run_job('move', kind='photos', to='d3'), 1)
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('Extra drive d3 is not connected', result['message'])
        self.assertIn('Nothing was changed', result['message'])
        self.assertIn('Plug Extra drive d3 in', result['advice'])
        self.assertEqual(listing(self.root / 'drives/d3'), [])
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(self.started, [])

    def test_a_move_from_a_drive_with_another_disk_in_its_place_is_refused(self):
        self.config['locations'] = {'photos': 'd3'}
        self.files(self.root / 'drives/d3/photos/immich-library', **{'x.jpg': 'one'})
        self.unplug('d3', 'other')
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertIn('another disk is mounted', last_result(self.root)['message'])
        self.assertEqual(listing(self.root / 'media'), [])

    def test_a_drive_that_is_away_is_not_offered_as_a_place(self):
        self.unplug('d3')
        self.assertIn('is not connected', storage_jobs.target_problem(self.config, 'photos', 'd3', 0))

    def test_a_move_back_to_a_drive_that_is_away_is_refused(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.run_job('move', kind='photos', to='media')
        self.unplug('ssd')
        self.assertEqual(self.run_job('back', kind='photos', copy=ID), 1)
        self.assertIn('Apps drive is not connected', last_result(self.root)['message'])
        self.assertEqual(self.saved()['locations'], {'photos': 'media'})

    def test_an_old_copy_on_a_drive_that_is_away_is_not_deleted(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.run_job('move', kind='photos', to='media')
        self.unplug('ssd')
        self.assertEqual(self.run_job('delete_old', kind='photos', copy=ID, confirm='Delete Photos'), 1)
        self.assertIn('not connected', last_result(self.root)['message'])
        self.assertTrue((self.root / f'ssd/nekoserver-old-photos-{ID}').exists())


class MidJobTests(Jobs):
    def test_a_drive_that_goes_away_during_the_copy_undoes_the_move_and_is_not_written_again(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})

        def copy_then_unplug(source, target, final=False, out=print, again=False):
            copy(source, target)
            self.unplug('d3')

        with mock.patch.object(migrate, 'copy', copy_then_unplug):
            self.assertEqual(self.run_job('move', kind='photos', to='d3'), 1)
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('Extra drive d3 is not connected', result['message'])
        self.assertIn('Nothing was changed', result['message'])
        self.assertFalse(self.settings[0].exists())
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertIsNone(saved_journal(self.root))

    def test_a_new_place_that_goes_away_after_the_switch_is_undone_from_the_old_one(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        real = storage_jobs.sync_drives

        def sync_then_unplug(*folders):
            real(*folders)
            if self.root / 'drives/d3' in map(Path, folders):
                self.unplug('d3')

        with mock.patch.object(storage_jobs, 'sync_drives', sync_then_unplug):
            self.assertEqual(self.run_job('move', kind='photos', to='d3'), 1)
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('stay on Apps drive', result['message'])
        self.assertNotIn('photos', self.saved().get('locations', {}))
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertIsNone(saved_journal(self.root))


class LostDriveBackTests(Jobs):
    """The person's way out: the films' old copy on the media drive taken back while d3,
    the drive they were moved to, is lost."""

    def moved_to_d3(self):
        films = storage_jobs.paths(self.config, 'movies', 'media')[0]
        self.files(films, **{'old.mkv': 'from before'})
        self.assertEqual(self.run_job('move', kind='movies', to='d3'), 0)
        self.files(storage_jobs.paths(self.config, 'movies', 'd3')[0], **{'new.mkv': 'since'})
        self.config = self.saved()
        self.unplug('d3')
        return films

    def test_t1_18_move_back_from_a_lost_drive_takes_the_old_copy_and_never_touches_the_drive(self):
        films = self.moved_to_d3()
        before = listing(self.root / 'drives/d3')
        self.assertEqual(self.run_job('back', kind='movies', copy=ID), 0)
        result = last_result(self.root)
        self.assertEqual(result['state'], 'done')
        self.assertIn('as the old copy had them', result['message'])
        self.assertIn('is not in it', result['message'])
        self.assertEqual((films / 'old.mkv').read_text(), 'from before')
        self.assertFalse((films / 'new.mkv').exists())
        self.assertEqual(self.saved()['locations']['movies'], 'media')
        self.assertEqual(listing(self.root / 'drives/d3'), before)
        self.assertIsNone(saved_journal(self.root))
        waiting = storage_jobs.waiting(self.root / 'state')
        self.assertEqual(waiting, [{'job': 'retire', 'kind': 'movies', 'here': 'd3', 'id': ID}])

    def test_its_old_folders_are_retired_once_the_drive_is_back(self):
        self.moved_to_d3()
        self.run_job('back', kind='movies', copy=ID)
        self.config = self.saved()
        self.plug_in('d3')
        storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        retired = self.root / f'drives/d3/nekoserver-old-movies-{ID}'
        self.assertTrue(any(p.name == 'new.mkv' for p in retired.rglob('*')))
        self.assertEqual(storage_jobs.waiting(self.root / 'state'), [])

    def test_setup_failing_after_it_never_undoes_onto_the_lost_drive(self):
        self.moved_to_d3()
        with mock.patch.object(storage_jobs, 'setup_again', side_effect=RuntimeError('d3 is not connected')):
            self.assertEqual(self.run_job('back', kind='movies', copy=ID), 0)  # done, with what setup said
        result = last_result(self.root)
        self.assertEqual(result['state'], 'done')
        self.assertIn('are on Media and backups drive now', result['message'])
        self.assertEqual(self.saved()['locations']['movies'], 'media')
        self.assertIsNone(saved_journal(self.root))


class AtTheStartTests(Jobs):
    def resume_and_finish(self):
        with mock.patch.object(storage_jobs, 'run'):
            storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        config = json.loads(self.settings[0].read_text())
        storage_jobs.finish(
            config, self.settings, self.root / 'state', self.root / 'run', out=print, sleep=lambda s: None
        )

    def write_settings(self):
        for path in self.settings:
            path.write_text(json.dumps(self.config))

    def test_t1_18_an_undo_onto_a_lost_drive_is_turned_round_and_finished(self):
        """The drill's state: "back" of films d3 -> media, setup refused, the undo (unswitch)
        cut off copying onto d3, which is gone; the films already in media's old copy."""
        films = storage_jobs.paths(self.config, 'movies', 'media')[0]
        kept = self.root / f'media/nekoserver-old-movies-{OLD}'
        self.files(kept / films.relative_to(self.root / 'media'), **{'a.mkv': 'film'})
        self.config['locations'] = {'movies': 'd3'}
        self.write_settings()
        staging = self.root / f'media/nekoserver-new-movies-{ID}'
        journal(
            self.root,
            'back',
            'unswitch',
            {'kind': 'movies', 'copy': OLD},
            staging=str(staging),
            here='d3',
            to='media',
            change=[['locations', 'movies', 'd3']],
            was='d3',
        )
        self.unplug('d3')
        self.resume_and_finish()
        self.assertEqual(self.saved()['locations']['movies'], 'media')
        self.assertEqual((films / 'a.mkv').read_text(), 'film')
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(last_result(self.root)['state'], 'resumed')
        self.assertEqual(listing(self.root / 'drives/d3'), [])

    def test_a_move_whose_new_drive_is_away_at_the_start_goes_back_to_the_old_one(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.config['locations'] = {'photos': 'd3'}
        self.write_settings()
        journal(
            self.root,
            'move',
            'start',
            {'kind': 'photos', 'to': 'd3'},
            staging=str(self.root / f'drives/d3/nekoserver-new-photos-{ID}'),
            here='ssd',
            to='d3',
            change=[['locations', 'photos', 'd3']],
            was=None,
        )
        self.unplug('d3')
        self.resume_and_finish()
        self.assertNotIn('photos', self.saved().get('locations', {}))
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('Extra drive d3 is not connected', result['message'])
        self.assertIn('stay on Apps drive', result['message'])
        self.assertIsNone(saved_journal(self.root))

    def test_a_delete_whose_drive_is_away_waits_for_it_and_holds_up_nothing(self):
        gone = self.root / f'drives/d3/nekoserver-deleting-photos-{ID}'
        self.files(gone, **{'x.jpg': 'one'})
        journal(self.root, 'delete_old', 'delete', {'kind': 'photos', 'copy': ID}, deleting=str(gone))
        self.unplug('d3')
        storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        self.assertIsNone(saved_journal(self.root))
        self.assertIn('deleted when it is back', last_result(self.root)['message'])
        self.assertTrue(gone.exists())
        self.plug_in('d3')
        storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        self.assertFalse(gone.exists())

    def test_a_job_that_waits_for_two_drives_can_be_given_up(self):
        self.config['locations'] = {'photos': 'd3'}
        self.write_settings()
        journal(
            self.root,
            'move',
            'start',
            {'kind': 'photos', 'to': 'd3'},
            here='media',
            to='d3',
            change=[['locations', 'photos', 'd3']],
            was='media',
        )
        self.unplug('d3')
        self.unplug('media')
        self.assertEqual(
            storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print), 1
        )
        self.assertIsNotNone(saved_journal(self.root))
        self.assertIn('Give it up', last_result(self.root)['advice'])
        self.assertEqual(self.run_job('move', kind='music', to='ssd'), 1)
        self.assertIn('Try again', last_result(self.root)['advice'])
        self.run_job('give_up')
        self.assertIsNone(saved_journal(self.root))
        self.assertIn('given up', last_result(self.root)['message'])
        self.assertEqual(
            storage_jobs.waiting(self.root / 'state'),
            [{'job': 'retire', 'kind': 'photos', 'here': 'media', 'id': ID}],
        )

    def test_a_job_the_start_could_not_finish_is_tried_again_once_its_drive_is_back(self):
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.config['locations'] = {'photos': 'media'}
        self.write_settings()
        journal(
            self.root,
            'move',
            'start',
            {'kind': 'photos', 'to': 'media'},
            here='ssd',
            to='media',
            change=[['locations', 'photos', 'media']],
            was=None,
        )
        self.unplug('ssd')
        self.unplug('media')
        storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        self.assertIsNotNone(saved_journal(self.root))
        self.plug_in('ssd')
        self.plug_in('media')
        self.assertEqual(self.run_job('retry'), 0)
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(last_result(self.root)['state'], 'resumed')

    def test_try_again_resumes_under_the_lock_with_the_apps_stopped(self):
        """Review of cdcbc14: the start's resume assumes no app runs (it copies the new
        place back and deletes the staging); "Try again" runs it as the start would."""
        self.config['locations'] = {'photos': 'media'}
        self.write_settings()
        journal(
            self.root,
            'move',
            'start',
            {'kind': 'photos', 'to': 'media'},
            here='ssd',
            to='media',
            change=[['locations', 'photos', 'media']],
            was=None,
        )
        seen, real = [], storage_jobs.resume

        def resume(*args, **kwargs):
            held = neko_common.hold_operation(self.root / 'operation.lock')
            seen.append((held is None, list(self.started)))
            if held is not None:
                held.close()
            return real(*args, **kwargs)

        stops = []
        writers = self.stop_writers

        def stop_writers(**kwargs):
            stops.append(True)
            return writers(**kwargs)

        with (
            mock.patch.object(storage_jobs, 'resume', resume),
            mock.patch.object(migrate, 'stop_writers', stop_writers),
        ):
            self.assertEqual(self.run_job('retry'), 0)
        self.assertTrue(seen[0][0])  # the lock was held
        self.assertTrue(stops)
        self.assertEqual(seen[0][1], [])  # nothing started again before it
        self.assertIn(['nekoserver-immich.service'], self.started)

    def test_give_up_is_refused_while_every_drive_of_the_job_is_there(self):
        self.config['locations'] = {'photos': 'd3'}
        self.write_settings()
        journal(
            self.root,
            'move',
            'unswitch',
            {'kind': 'photos', 'to': 'd3'},
            here='media',
            to='d3',
            change=[['locations', 'photos', 'd3']],
            was='media',
        )
        self.assertEqual(self.run_job('give_up'), 1)
        self.assertIn('use "Try again"', last_result(self.root)['message'])
        self.assertIsNotNone(saved_journal(self.root))
        self.assertEqual(self.saved()['locations']['photos'], 'd3')

    def test_a_given_up_delete_waits_for_its_drive_and_is_done_once_it_is_back(self):
        gone = self.root / f'drives/d3/nekoserver-deleting-photos-{ID}'
        self.files(gone, **{'x.jpg': 'one'})
        journal(self.root, 'delete_old', 'delete', {'kind': 'photos', 'copy': ID}, deleting=str(gone))
        self.unplug('d3')
        self.run_job('give_up')
        self.assertEqual(
            storage_jobs.waiting(self.root / 'state'),
            [{'job': 'delete_old', 'deleting': str(gone), 'place': 'd3'}],
        )
        self.plug_in('d3')
        storage_jobs.waiting_work(self.config, self.root / 'state', out=print)
        self.assertFalse(gone.exists())
        self.assertEqual(storage_jobs.waiting(self.root / 'state'), [])

    def test_a_waiting_delete_is_done_only_on_the_drive_it_was_on(self):
        gone = self.root / f'media/nekoserver-deleting-photos-{ID}'
        self.files(gone, **{'x.jpg': 'one'})
        storage_jobs.wait_for(self.root / 'state', {'job': 'delete_old', 'deleting': str(gone), 'place': 'd3'})
        storage_jobs.waiting_work(self.config, self.root / 'state', out=print)
        self.assertTrue(gone.exists())


class MediaFoldersTests(Jobs):
    def test_a_folder_on_an_extra_drive_that_is_away_is_refused(self):
        import media_locations

        folder = self.root / 'drives/d3/media/movies'
        folder.mkdir(parents=True)
        media_locations.drive_there(self.config, str(folder))  # there: no refusal
        self.unplug('d3')
        with self.assertRaises(media_locations.Refusal) as refused:
            media_locations.drive_there(self.config, str(folder))
        self.assertIn('Extra drive d3, which is not connected', str(refused.exception))


if __name__ == '__main__':
    unittest.main()
