"""1.0.1 storage from the dashboard: which side of the commit point each step of a job says
it is on, and what the start of the computer does with a job a power cut stopped
(plans/1.0.1-contract.md §4, §6). A journal that said "committed" too early would make the
start finish a move whose data never reached its new place; one that said it too late
would undo a move whose settings already name the new place. The copies, the stops and
setup are migrate.py's, faked as in test_storage_jobs."""

import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate
import storage_jobs
from tests.test_storage_jobs import ID, Jobs, copy
from tests.test_storage_refusals import disk, part, world

UNIT = 'nekoserver-immich.service'
# setup_again as the release has it (the tests of test_storage_jobs fake it).
SETUP_AGAIN = storage_jobs.setup_again


class Marked(Jobs):
    """The journal's, the progress's and the result's writes, in order."""

    def setUp(self):
        super().setUp()
        self.marks, self.copies, self.checks = [], [], []
        job = storage_jobs.Job
        journal, progress, result = job.journal, job.progress, job.result

        def journaled(job, phase, committed, **undo):
            self.marks.append(('journal', phase, committed))
            return journal(job, phase, committed, **undo)

        def progressed(job, step, **counts):
            self.marks.append(('progress', step, counts.get('bytes_total')))
            return progress(job, step, **counts)

        def resulted(job, state, message, advice=None, power_cut=False):
            self.marks.append(('result', state))
            return result(job, state, message, advice, power_cut)

        def copied(source, target, final=False, out=print, again=False):
            self.copies.append((str(Path(source).relative_to(self.root)), final, again))
            copy(source, target, final, out, again)

        def checked(config, before):
            self.checks.append(before)
            return self.problem

        self.problem = None
        self.tools = mock.Mock()
        for patch in (
            mock.patch.object(job, 'journal', journaled),
            mock.patch.object(job, 'progress', progressed),
            mock.patch.object(job, 'result', resulted),
            mock.patch.object(migrate, 'copy', copied),
            mock.patch.object(migrate, 'server_check', checked),
            mock.patch.object(storage_jobs, 'du', lambda paths: 7),
            mock.patch.object(storage_jobs, 'run', self.tools),
            mock.patch.object(
                storage_jobs, 'shutil_which', lambda name: '/usr/bin/chattr' if name == 'chattr' else None
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def journals(self):
        return [m[1:] for m in self.marks if m[0] == 'journal']

    def job(self, job, **fields):
        return storage_jobs.Job(
            self.request(job, **fields), self.config, self.settings, self.root / 'state', self.root / 'run'
        )

    def resume(self):
        return storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run')

    def finish(self):
        return storage_jobs.finish(self.config, self.settings, self.root / 'state', self.root / 'run')


class MoveStepsTests(Marked):
    def test_a_move_is_committed_only_at_the_switch(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual(
            self.marks,
            [
                ('result', 'working'),
                ('journal', 'copy', False),
                ('progress', 'copy', 7),
                ('journal', 'stop', False),
                ('progress', 'stop', None),
                ('journal', 'stop', False),
                ('progress', 'last_copy', 7),
                ('journal', 'compare', False),
                ('progress', 'compare', None),
                ('journal', 'switch', False),
                ('progress', 'switch', None),
                ('journal', 'switch', True),
                ('journal', 'start', True),
                ('progress', 'start', None),
                ('progress', 'check', None),
                ('result', 'done'),
            ],
        )
        # The first copy while the apps run, the last one after they stopped.
        source = 'ssd/photos/immich-library'
        self.assertEqual(self.copies, [(source, False, False), (source, True, False)])
        self.assertEqual(self.checks, [], 'no steps were working before: nothing to check after')

    def test_a_second_copy_into_a_copy_already_begun_says_so(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.files(self.root / f'media/nekoserver-new-photos-{ID}/photos/immich-library', **{'x.jpg': 'on'})
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual([c[1:] for c in self.copies], [(False, True), (True, False)])

    def test_a_kind_of_several_folders_moves_the_ones_there(self):
        # Films and shows: library/movies is not there, library/tv and library/downloads are.
        self.files(self.root / 'media/library', **{'tv/s01.mkv': 'tv', 'downloads/a.part': 'dl'})
        self.assertEqual(self.run_job('move', kind='movies', to='d3'), 0)
        self.assertEqual((self.root / 'drives/d3/library/tv/s01.mkv').read_text(), 'tv')
        self.assertEqual((self.root / 'drives/d3/library/downloads/a.part').read_text(), 'dl')
        old = self.root / f'media/nekoserver-old-movies-{ID}/library'
        self.assertEqual(sorted(p.name for p in old.iterdir()), ['downloads', 'tv'])

    def test_the_way_back_is_committed_only_at_the_switch(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.run_job('move', kind='photos', to='media')
        self.marks.clear()
        self.assertEqual(self.run_job('back', kind='photos', copy=ID), 0)
        self.assertEqual(self.journals()[0], ('copy', False))
        self.assertIn(('progress', 'copy', 7), self.marks)
        self.assertEqual([j for j in self.journals() if j[1]], [('switch', True), ('start', True)])

    def test_an_old_copy_is_marked_for_deletion_before_it_is_touched(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.run_job('move', kind='photos', to='media')
        self.marks.clear()
        self.assertEqual(self.run_job('delete_old', kind='photos', copy=ID), 1)
        self.assertEqual(self.last_result()['advice'], 'Type exactly: delete photos')
        self.assertEqual(self.run_job('delete_old', kind='photos', copy=ID, confirm='delete photos'), 0)
        self.assertIn(('journal', 'delete', True), self.marks)
        self.assertLess(self.marks.index(('journal', 'delete', True)), self.marks.index(('progress', 'delete', None)))


class InTheWayTests(Marked):
    def test_an_empty_folder_at_the_new_place_is_taken_away_first(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        closed = self.root / 'media/photos/immich-library'
        closed.mkdir(parents=True)
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.tools.assert_any_call(['chattr', '-i', str(closed)])
        self.assertEqual((closed / 'x.jpg').read_text(), 'one')

    def test_a_link_at_the_new_place_is_in_the_way(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        (self.root / 'elsewhere').mkdir()
        (self.root / 'media/photos').mkdir()
        (self.root / 'media/photos/immich-library').symlink_to(self.root / 'elsewhere')
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertIn('is in the way: it is not empty', self.last_result()['message'])
        self.assertEqual(self.last_result()['state'], 'undone')
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')

    def test_files_at_the_new_place_stop_the_move_before_any_copy(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.files(self.root / 'media/photos/immich-library', **{'mine.jpg': 'theirs'})
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertIn('holds files already. Nothing was changed.', self.last_result()['message'])
        self.assertEqual((self.copies, self.journals()), ([], []))


class BackupsTests(Marked):
    def setUp(self):
        super().setUp()
        self.unbound, self.rebound = [], []
        for patch in (
            mock.patch.object(storage_jobs, 'unbind_backups', lambda config: self.unbound.append(config)),
            mock.patch.object(storage_jobs, 'rebind_backups', lambda config, out=print: self.rebound.append(1)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_backups_back_on_the_media_drive_leave_their_binds_and_their_setting(self):
        self.config['locations'] = {'backups': 'd3'}
        self.files(self.root / 'drives/d3', **{'backups/host.tar': 'h'})
        self.assertEqual(self.run_job('move', kind='backups', to='media'), 0)
        self.assertEqual(len(self.unbound), 1)
        self.assertEqual((self.root / 'media/backups/host.tar').read_text(), 'h')
        self.assertNotIn('backups', self.saved()['locations'])

    def test_only_backups_away_from_the_media_drive_have_binds(self):
        self.files(self.root / 'media', **{'backups/host.tar': 'h'})
        self.assertEqual(self.run_job('move', kind='backups', to='d3'), 0)
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual(self.unbound, [])

    def test_a_cut_switch_of_backups_from_an_extra_drive_mounts_them_there_again(self):
        for kind, here, again in (('backups', 'd3', 1), ('backups', 'media', 0), ('photos', 'd3', 0)):
            with self.subTest(kind=kind, here=here):
                self.rebound.clear()
                job = self.job('move', kind=kind, to='ssd')
                staging = self.root / f'ssd/nekoserver-new-{kind}-{ID}'
                job.journal('switch', False, staging=str(staging), here=here, to='ssd')
                self.assertEqual(self.resume(), 0)
                self.assertEqual(len(self.rebound), again)


class StoppedTests(Marked):
    def test_an_error_before_the_switch_changes_nothing(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        with mock.patch.object(migrate, 'copy', side_effect=OSError('No space left on device')):
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual(self.last_result()['state'], 'failed')
        self.assertEqual(
            self.last_result()['message'], 'The job stopped: No space left on device. Nothing was changed.'
        )
        self.assertIsNone(self.saved_journal())
        self.assertEqual(self.started, [None])

    def test_an_error_after_the_commit_point_is_left_for_the_start_to_finish(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        with mock.patch.object(storage_jobs.Job, 'save_settings', side_effect=OSError('Read-only file system')):
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual(self.last_result()['state'], 'failed')
        self.assertEqual(self.last_result()['message'], 'The job stopped after the switch: Read-only file system')
        self.assertEqual(self.last_result()['advice'], 'It is finished at the next start.')
        self.assertTrue(self.saved_journal()['committed'])
        # Never undone: the settings may name the new place already.
        self.assertEqual((self.root / 'media/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertFalse((self.root / f'media/nekoserver-new-photos-{ID}/photos/immich-library').exists())
        self.assertEqual(self.started, [])

    def test_a_server_check_that_fails_after_the_move_still_says_it_moved(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.problem = 'Immich does not answer'
        with mock.patch.object(storage_jobs, 'steps_working', lambda config: [UNIT]):
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual(self.checks, [[UNIT]])
        self.assertEqual(self.last_result()['state'], 'done')
        self.assertEqual(
            self.last_result()['message'],
            'Your photos were moved, but the server check after it says: Immich does not answer',
        )
        self.assertIsNone(self.saved_journal())

    def test_a_disk_refused_at_the_erase_is_said_and_nothing_changes(self):
        self.disk = disk('sdc', serial='S-AAAA')
        refused = storage_jobs.Refused('busy', 'Linux is using the disk right now: nothing was erased.')
        with (
            mock.patch.object(storage_jobs, 'world_now', lambda config: world(self.disk)),
            mock.patch.object(storage_jobs, 'erase', side_effect=refused),
        ):
            shown = storage_jobs.describe(dict(world(self.disk), named={}))[0]
            asked = {k: shown[k] for k in ('by_id', 'serial', 'size_bytes')}
            self.assertEqual(self.run_job('use', how='erase', disk=asked, confirm='4 TB AAAA'), 1)
        self.assertEqual(self.last_result()['state'], 'failed')
        self.assertEqual(
            self.last_result()['message'], 'Linux is using the disk right now: nothing was erased. Nothing was changed.'
        )
        self.assertIsNone(self.saved_journal())
        self.assertFalse(self.settings[0].exists())


class CutMoveTests(Marked):
    def cut(self, phase, kind='movies', to='d3', here='media', job='move', **fields):
        staging = self.root / ('drives/d3' if to == 'd3' else to) / f'nekoserver-new-{kind}-{ID}'
        saved = self.job(job, kind=kind, to=to, **fields)
        saved.started = 123.0
        saved.journal(phase, False, staging=str(staging), here=here, to=to)
        return staging

    def test_without_a_journal_the_start_does_nothing(self):
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.marks, [])

    def test_a_folder_already_at_the_new_place_stays_when_a_copy_is_cut(self):
        mount_point = self.root / 'drives/d3/library/tv'
        mount_point.mkdir(parents=True)
        staging = self.cut('copy')
        self.files(staging / 'library/downloads', **{'a.part': 'half'})
        self.assertEqual(self.resume(), 0)
        self.assertTrue(mount_point.is_dir())
        self.assertFalse(staging.exists())
        self.assertEqual(self.last_result()['started_at'], 123.0)

    def test_a_cut_switch_takes_back_only_the_folders_that_were_switched(self):
        # library/tv and library/downloads were switched; library/movies never was.
        self.files(self.root / 'drives/d3/library', **{'tv/s01.mkv': 'tv', 'downloads/a.part': 'dl'})
        self.files(self.root / 'media/library', **{'tv/s01.mkv': 'tv', 'downloads/a.part': 'dl'})
        staging = self.cut('switch')
        self.assertEqual(self.resume(), 0)
        self.assertFalse((self.root / 'drives/d3/library/tv').exists())
        self.assertFalse((self.root / 'drives/d3/library/downloads').exists())
        self.assertFalse(staging.exists())
        self.assertEqual((self.root / 'media/library/tv/s01.mkv').read_text(), 'tv')
        self.assertEqual(self.last_result()['state'], 'undone')

    def test_a_drive_the_job_came_from_that_is_missing_keeps_the_journal(self):
        self.cut('copy', kind='photos', to='media', here='d3')
        (self.root / 'drives/d3').rmdir()
        self.assertEqual(self.resume(), 1)
        self.assertEqual(self.last_result()['state'], 'failed')
        self.assertEqual(self.last_result()['advice'], 'Plug it in and restart.')
        self.assertIsNotNone(self.saved_journal())

    def test_a_cut_way_back_gives_the_old_copy_its_name_again(self):
        staging = self.cut('copy', kind='photos', to='ssd', job='back', copy=ID)
        self.files(staging / 'photos/immich-library', **{'x.jpg': 'one'})
        self.assertEqual(self.resume(), 0)
        old = self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/x.jpg'
        self.assertEqual(old.read_text(), 'one')
        self.assertFalse(staging.exists())


class FinishedAtTheStartTests(Marked):
    def committed(self, job, phase='start', **fields):
        saved = self.job(job, **fields)
        saved.journal(phase, True, **{k: v for k, v in fields.items() if k in ('drive', 'deleting', 'change')})
        return saved

    def test_committed_moves_ways_back_added_drives_and_mirrors_finish_after_docker(self):
        for job in ('move', 'back', 'use', 'mirror_add'):
            with self.subTest(job=job):
                self.tools.reset_mock()
                self.committed(job, kind='photos', to='media', copy=ID)
                self.assertEqual(self.resume(), 0)
                self.tools.assert_called_once_with(
                    [
                        'systemd-run',
                        '--no-block',
                        '--unit=nekoserver-storage-finish',
                        '--property=After=docker.service',
                        '--property=TimeoutStartSec=infinity',
                        sys.executable,
                        '-Es',
                        str(Path(storage_jobs.__file__).resolve()),
                        'finish',
                        f'--config={self.settings[0]}',
                        f'--config={self.settings[1]}',
                    ]
                )

    def test_a_cut_deletion_is_finished_whichever_name_the_copy_has(self):
        for renamed in (True, False):
            with self.subTest(renamed=renamed):
                old = self.root / f'ssd/nekoserver-old-photos-{ID}'
                gone = self.root / f'ssd/nekoserver-deleting-photos-{ID}'
                self.files(gone if renamed else old, **{'photos/immich-library/x.jpg': 'one'})
                self.committed('delete_old', phase='delete', kind='photos', copy=ID, deleting=str(gone))
                self.assertEqual(self.resume(), 0)
                self.assertFalse(old.exists() or gone.exists())
                self.assertEqual(self.last_result()['state'], 'resumed')
                self.assertTrue(self.last_result()['power_cut'])
                self.assertIsNone(self.saved_journal())
                self.tools.assert_not_called()

    def test_a_deletion_journal_naming_another_folder_deletes_nothing(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.committed('delete_old', phase='delete', kind='photos', copy=ID, deleting=str(self.root / 'ssd/photos'))
        self.assertEqual(self.resume(), 0)
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')

    def test_a_cut_release_takes_the_drives_line_out_at_the_start(self):
        dropped = []
        change = [['storage', 'd3', None]]
        self.committed('release', phase='release', drive='d3', change=change)
        with mock.patch.object(storage_jobs, 'drop_fstab_line', dropped.append):
            self.assertEqual(self.resume(), 0)
        self.assertEqual(dropped, [self.root / 'drives/d3'])
        self.assertNotIn('d3', self.saved(0)['storage'])
        self.assertEqual((self.last_result()['state'], self.last_result()['power_cut']), ('resumed', True))
        self.tools.assert_not_called()

    def test_an_erase_cut_after_the_format_adds_an_ext4_drive_with_the_erases_id(self):
        asked = []
        job = self.job('use', how='erase', disk={})
        job.journal('format', False, drive='d4', erased={'phase': 'format', 'uuid': 'u-made', 'partuuid': 'p'})
        with mock.patch.object(storage_jobs, 'uuid_there', lambda uuid: asked.append(uuid) or True):
            self.resume()
        self.assertEqual(asked, ['u-made'])
        self.assertEqual(self.saved()['storage']['d4'], {'uuid': 'u-made', 'fstype': 'ext4', 'added': True})

    def test_an_erase_cut_before_its_filesystem_says_the_disk_was_erased(self):
        job = self.job('use', how='erase', disk={})
        job.journal('partition', False, drive='d4', erased={'phase': 'partition', 'uuid': 'u-made', 'partuuid': 'p'})
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.last_result()['message'], storage_jobs.UNDONE_WORDS['use'])
        self.assertIn('erase it again from the Drives page', self.last_result()['message'])

    def test_a_filesystem_is_there_only_when_blkid_finds_its_id(self):
        calls = []

        def ran(argv, **named):
            calls.append((argv, named))
            if argv[0] == 'blkid':
                return mock.Mock(returncode=self.found[0], stdout=self.found[1])
            return mock.Mock(returncode=0, stdout='')

        with mock.patch('subprocess.run', ran):
            self.found = (0, '')
            self.assertFalse(storage_jobs.uuid_there(''))
            self.assertEqual(calls, [], 'no id: nothing asked')
            for found, there in (((0, '/dev/sdc1\n'), True), ((0, '\n'), False), ((2, '/dev/sdc1\n'), False)):
                with self.subTest(found=found):
                    calls.clear()
                    self.found = found
                    self.assertIs(storage_jobs.uuid_there('u-made'), there)
                    self.assertEqual(
                        calls,
                        [
                            (['udevadm', 'settle'], {'capture_output': True, 'timeout': 60, 'check': False}),
                            (
                                ['blkid', '-U', 'u-made'],
                                {'capture_output': True, 'text': True, 'timeout': 60, 'check': False},
                            ),
                        ],
                    )

    def test_the_finish_does_nothing_for_a_job_that_was_not_committed(self):
        self.job('move', kind='photos', to='media').journal('copy', False, here='ssd', to='media')
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.setups, [])
        self.assertIsNotNone(self.saved_journal())

    def test_the_finish_stops_the_apps_copies_what_is_left_and_starts_them_again(self):
        # The copy at the new place is complete; the old folder was retired already.
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.committed('move', kind='photos', to='media')
        saved = storage_jobs.read_state('journal.json', self.root / 'state')
        saved['undo'].update(here='ssd', to='media', change=[['locations', 'photos', 'media']])
        storage_jobs.write_state('journal.json', saved, self.root / 'state')
        seen = []

        def stop_writers(out=print, note=None):
            note([UNIT])
            seen.append(self.saved_journal())
            return [UNIT]

        with mock.patch.object(migrate, 'stop_writers', stop_writers):
            self.assertEqual(self.finish(), 0)
        self.assertEqual(self.copies, [], 'nothing left at the old place to copy')
        # The apps it stopped are in the journal at once, still on the far side of the commit.
        self.assertEqual((seen[0]['phase'], seen[0]['committed'], seen[0]['undo']['stopped']), ('start', True, [UNIT]))
        self.assertEqual(self.started, [[UNIT]])
        self.assertEqual((self.last_result()['state'], self.last_result()['power_cut']), ('resumed', True))

    def test_the_finish_copies_with_the_last_copys_care(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'late.jpg': 'meanwhile'})
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.committed('move', kind='photos', to='media')
        saved = storage_jobs.read_state('journal.json', self.root / 'state')
        saved['undo'].update(here='ssd', to='media')
        storage_jobs.write_state('journal.json', saved, self.root / 'state')
        self.assertEqual(self.finish(), 0)
        self.assertEqual(self.copies, [('ssd/photos/immich-library', True, False)])

    def test_a_finish_that_fails_is_tried_again_at_the_next_start(self):
        self.committed('use', phase='mount', drive='d4', change=[['storage', 'd4', {'uuid': 'u'}]])
        with mock.patch.object(storage_jobs, 'setup_again', side_effect=RuntimeError('setup failed')):
            self.assertEqual(self.finish(), 1)
        self.assertEqual(self.last_result()['state'], 'failed')
        self.assertEqual(self.last_result()['message'], 'Finishing the job at the start stopped: setup failed')
        self.assertEqual(self.last_result()['advice'], 'It is tried again at the next start.')
        self.assertIsNotNone(self.saved_journal())


class OldCopiesTests(Marked):
    def test_only_real_folders_with_an_old_copys_name_are_offered_or_deleted(self):
        ssd = self.root / 'ssd'
        (self.root / 'elsewhere').mkdir()
        (ssd / 'aaa-not-a-copy').mkdir()
        (ssd / f'nekoserver-old-movies-{ID}').symlink_to(self.root / 'elsewhere')
        (ssd / f'nekoserver-old-music-{ID}').write_text('a file, not a copy')
        self.files(ssd / f'nekoserver-old-photos-{ID}', **{'photos/immich-library/x.jpg': 'one'})
        copies = storage_jobs.old_copies(self.config, measure=lambda paths: 5)
        self.assertEqual([(c['kind'], c['place'], c['bytes']) for c in copies], [('photos', 'ssd', 5)])
        self.assertEqual(copies[0]['at'], (ssd / f'nekoserver-old-photos-{ID}').stat().st_mtime)
        self.assertEqual(self.run_job('delete_old', kind='movies', copy=ID, confirm='delete films and shows'), 1)
        self.assertIn('not there any more', self.last_result()['message'])
        self.assertTrue((self.root / 'elsewhere').is_dir())

    def test_a_drive_that_is_away_hides_no_other_drives_copies(self):
        shutil.rmtree(self.root / 'media')
        self.files(self.root / f'drives/d3/nekoserver-old-music-{ID}', **{'library/music/a.flac': 'a'})
        copies = storage_jobs.old_copies(self.config, measure=lambda paths: 0)
        self.assertEqual([(c['kind'], c['place']) for c in copies], [('music', 'd3')])


class UseAndReleaseTests(Marked):
    def setUp(self):
        super().setUp()
        self.disk = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='ext4', uuid='u-new')])
        patch = mock.patch.object(storage_jobs, 'world_now', lambda config: world(self.disk))
        patch.start()
        self.addCleanup(patch.stop)

    def asked(self):
        shown = storage_jobs.describe(dict(world(self.disk), named={}))[0]
        return {k: shown[k] for k in ('by_id', 'serial', 'size_bytes')}

    def test_a_drive_the_settings_name_is_never_taken_even_when_not_mounted(self):
        self.disk = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='ext4', uuid='ssd-1')])
        self.assertEqual(self.run_job('use', how='take_over', disk=self.asked()), 1)
        self.assertEqual(
            self.last_result()['message'],
            'NekoServer does not use that disk: NekoServer keeps your apps or your media and backups on it. '
            'Nothing was changed.',
        )
        self.assertFalse(self.settings[0].exists())

    def test_an_erased_disk_is_added_by_the_id_its_erase_gave_it(self):
        asked = []

        def erase(by_id, serial, size, label=None, config=None, journal=None):
            asked.append((by_id, serial, size, label))
            journal({'phase': 'format', 'partuuid': 'p-1', 'uuid': 'u-erased', 'partition': '/dev/sdc1'})
            return '/dev/sdc1'

        with mock.patch.object(storage_jobs, 'erase', erase):
            self.assertEqual(self.run_job('use', how='erase', disk=self.asked(), confirm='4 TB AAAA'), 0)
        self.assertEqual(asked, [(self.asked()['by_id'], 'S-AAAA', self.asked()['size_bytes'], 'neko-d4')])
        self.assertEqual(self.saved()['storage']['d4'], {'uuid': 'u-erased', 'fstype': 'ext4', 'added': True})
        self.assertIn(('progress', 'erase', None), self.marks)
        self.assertIn(('progress', 'mount', None), self.marks)

    def test_a_drive_given_up_is_unmounted_by_its_place(self):
        mounted = self.root / 'drives/d3'
        with (
            mock.patch.object(storage_jobs.os.path, 'ismount', lambda path: Path(path) == mounted),
            mock.patch.object(storage_jobs, 'drop_fstab_line', lambda mountpoint: None),
        ):
            self.assertEqual(self.run_job('release', drive='d3'), 0)
        self.tools.assert_called_once_with(['umount', str(mounted)])

    def test_only_the_drives_own_line_leaves_fstab(self):
        fstab = self.root / 'fstab'
        line = f'UUID=d3-uuid {self.root}/drives/d3 ext4 defaults,nofail 0 2\n'
        kept = 'UUID=x / ext4 defaults 0 1\nUUID=y /home ext4 defaults 0 2\n'
        fstab.write_text(kept + line)
        with (
            mock.patch('modules.storage.FSTAB', fstab),
            mock.patch('modules.storage.own_line', lambda f: 'd3' in f[1]),
        ):
            storage_jobs.drop_fstab_line(self.root / 'drives/d3')
        self.assertEqual(fstab.read_text(), kept)
        self.assertEqual(fstab.stat().st_mode & 0o777, 0o644)
        self.tools.assert_called_once_with(['systemctl', 'daemon-reload'])

    def test_setup_runs_again_with_the_persons_settings_and_a_log_of_its_own(self):
        called = []
        job = self.job('move', kind='photos', to='media')
        with mock.patch.object(migrate, 'setup_again', lambda *given, **named: called.append(given)):
            SETUP_AGAIN(job)
        self.assertEqual(called, [(self.config, self.settings[-1], self.root / f'state/setup-{ID}.log')])


class MirrorJournalTests(Marked):
    """Job D's resume and finish are mirror_add.py's (T2): this file turns their answers
    into the result the page shows."""

    def setUp(self):
        super().setUp()
        self.answer = {}
        fake = mock.Mock()
        fake.Context = lambda **named: named
        fake.resume = lambda saved, ctx: self.answer
        fake.finish = lambda saved, ctx: self.answer
        fake.run_job = lambda request, ctx: self.answer
        patch = mock.patch.dict(sys.modules, mirror_add=fake)
        patch.start()
        self.addCleanup(patch.stop)

    def cut(self, committed):
        self.job('mirror_add', array='md-md0').journal('add', committed, member='/dev/sdc1')

    def test_a_cut_mirror_job_is_mirror_adds_to_resume(self):
        self.answer = {'state': 'resumed', 'message': 'The rebuild goes on.', 'advice': 'Leave it on.'}
        self.cut(False)
        self.assertEqual(self.resume(), 0)
        shown = self.last_result()
        self.assertEqual(
            (shown['state'], shown['message'], shown['advice'], shown['power_cut']),
            ('resumed', 'The rebuild goes on.', 'Leave it on.', True),
        )
        self.assertIsNone(self.saved_journal())
        self.answer = {'state': 'failed', 'message': 'The new drive is gone.', 'power_cut': False}
        self.cut(False)
        self.assertEqual(self.resume(), 1)
        self.assertFalse(self.last_result()['power_cut'])

    def test_a_mirror_job_run_now_is_no_power_cut(self):
        self.answer = {'state': 'done', 'message': 'The new drive is in the mirror.'}
        job = self.job('mirror_add', array='md-md0')
        self.assertEqual(storage_jobs.mirror_result(job, self.answer), 0)
        self.assertFalse(self.last_result()['power_cut'])

    def test_a_committed_mirror_job_is_waited_for_after_the_start(self):
        for answer, state, code in (('done', 'resumed', 0), ('failed', 'failed', 1)):
            with self.subTest(answer=answer):
                self.answer = {'state': answer, 'message': f'It is {answer}.', 'advice': 'Look at the Drives page.'}
                self.cut(True)
                self.assertEqual(self.finish(), code)
                shown = self.last_result()
                self.assertEqual(
                    (shown['state'], shown['message'], shown['advice'], shown['power_cut']),
                    (state, f'It is {answer}.', 'Look at the Drives page.', True),
                )


if __name__ == '__main__':
    unittest.main()
