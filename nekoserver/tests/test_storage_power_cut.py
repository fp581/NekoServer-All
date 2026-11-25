"""T1-14 (1.0.1 drill 5): a power cut drops the writes Linux still keeps in memory.

A "Move back" of music cut just after its commit point came back "finished at the start"
with the music folder empty: the copy and its rename on the new drive were never on the
disk, only the settings (fsynced) were. Here a ledger stands in for the drives: every copy,
rename and delete on a drive leaves it with writes not on the disk until the job syncs it;
a commit point or a result reached with such writes is what the power cut would have lost.
And a resume of a committed move whose new place lost its files undoes it from the old
place instead of finishing it over an empty folder."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate  # noqa: E402
import storage_jobs  # noqa: E402
from tests.test_storage_jobs import ID, Jobs, last_result, saved_journal  # noqa: E402


class Ledger:
    """The drives' writes not on the disk yet: a write on a drive's folder marks it, the
    job's sync of it clears it, and a commit point or a result is checked against it."""

    def __init__(self, test):
        self.test, self.dirty, self.lost = test, set(), []
        self.drives = [test.root / 'ssd', test.root / 'media', test.root / 'drives/d3']

    def drive(self, path):
        path = Path(path)
        return next((d for d in self.drives if d == path or d in path.parents), None)

    def wrote(self, *paths):
        self.dirty |= {d for d in map(self.drive, paths) if d}

    def sync(self, *folders):
        self.dirty -= {d for d in map(self.drive, folders) if d}

    def check(self, at):
        if self.dirty:
            self.lost.append((at, sorted(str(d.relative_to(self.test.root)) for d in self.dirty)))

    def start(self):
        rename, rmtree, copy, commit, write = (
            os.rename,
            shutil.rmtree,
            migrate.copy,
            storage_jobs.Job.commit,
            storage_jobs.write_state,
        )
        ledger = self

        def renamed(source, target, *args, **kwargs):
            ledger.wrote(source, target)
            return rename(source, target, *args, **kwargs)

        def removed(path, *args, **kwargs):
            ledger.wrote(path)
            return rmtree(path, *args, **kwargs)

        def copied(source, target, *args, **kwargs):
            ledger.wrote(target)
            return copy(source, target, *args, **kwargs)

        def committed(job, phase, change, **undo):
            ledger.check(f'commit {phase}')
            return commit(job, phase, change, **undo)

        def written(name, payload, folder=None):
            if name == storage_jobs.RESULT and payload.get('state') != 'working':
                ledger.check(f"result {payload.get('state')}")
            return write(name, payload, folder)

        for patch in (
            mock.patch.object(os, 'rename', renamed),
            mock.patch.object(shutil, 'rmtree', removed),
            mock.patch.object(migrate, 'copy', copied),
            mock.patch.object(storage_jobs, 'sync_drives', self.sync),
            mock.patch.object(storage_jobs.Job, 'commit', committed),
            mock.patch.object(storage_jobs, 'write_state', written),
        ):
            patch.start()
            self.test.addCleanup(patch.stop)
        return self


class DurabilityTests(Jobs):
    def ledger(self):
        return Ledger(self).start()

    def test_a_move_has_its_copy_on_the_new_drive_before_its_commit_point(self):
        for kind, folder in (('photos', 'ssd/photos/immich-library'), ('backups', 'media/backups')):
            with self.subTest(kind=kind):
                self.files(self.root / folder, **{'a/x.bin': 'one'})
                ledger = self.ledger()
                with (
                    mock.patch.object(storage_jobs, 'unbind_backups'),
                    mock.patch.object(storage_jobs, 'rebind_backups'),
                ):
                    self.assertEqual(self.run_job('move', kind=kind, to='d3'), 0)
                self.assertEqual([at for at in ledger.lost if at[0].startswith('commit')], [])

    def test_a_move_back_has_its_copy_in_place_before_its_commit_point(self):
        self.config['locations'] = {'music': 'd3'}
        self.files(self.root / 'drives/d3/library/music', **{'song.flac': 'now'})
        self.files(self.root / f'media/nekoserver-old-music-{"f" * 16}/library/music', **{'song.flac': 'before'})
        ledger = self.ledger()
        self.assertEqual(self.run_job('back', kind='music', copy='f' * 16), 0)
        self.assertEqual((self.root / 'media/library/music/song.flac').read_text(), 'now')
        self.assertEqual([at for at in ledger.lost if at[0].startswith('commit')], [])

    def test_no_result_is_written_before_the_drives_have_what_it_says(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        ledger = self.ledger()
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        copy = 'nekoserver-old-photos-' + ID
        found = storage_jobs.old_copies(self.config, measure=lambda _paths: 0)
        words = next(c['delete_words'] for c in found if c['path'].endswith(copy))
        request = dict(self.request('delete_old', kind='photos', copy=ID, confirm=words), id='1' * 16)
        storage_jobs.run_request(request, self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        self.assertEqual(last_result(self.root)['state'], 'done')
        self.assertFalse((self.root / f'ssd/{copy}').exists())
        self.assertEqual([at for at in ledger.lost if at[0].startswith('result')], [])

    def test_the_drives_are_synced_for_real_once_per_filesystem(self):
        with mock.patch.object(storage_jobs.os, 'sync', return_value=None) as sync:
            storage_jobs.sync_drives(self.root / 'ssd', self.root / 'media', self.root / 'nowhere', None)
        self.assertLessEqual(sync.call_count, 1)  # syncfs once per filesystem; sync() only without it


class LostSwitchTests(Jobs):
    """The state a power cut leaves when the new drive lost the copy but the settings (and
    the journal, past its commit point) say the switch: the resume undoes it."""

    def cut(self, kind, here, to, phase='switch', copied=None, action='move', copy=None):
        request = self.request(action, kind=kind, **({'to': to} if action == 'move' else {'copy': copy}))
        staging = storage_jobs.root_of(self.config, to) / storage_jobs.NEW_COPY.format(kind=kind, id=ID)
        was = (self.config.get('locations') or {}).get(kind)
        undo = {
            'staging': str(staging),
            'here': here,
            'to': to,
            'copied': copied or [],
            'change': [['locations', kind, to]],
            'was': was,
            'stopped': ['nekoserver-navidrome.service'],
        }
        storage_jobs.write_state(
            'journal.json',
            {
                'id': ID,
                'job': action,
                'phase': phase,
                'committed': True,
                'boot': 'before',
                'started_at': 1.0,
                'request': request,
                'undo': undo,
            },
            self.root / 'state',
        )
        self.config.setdefault('locations', {})[kind] = to
        for path in self.settings:
            path.write_text(json.dumps(self.config))

    def resume_and_finish(self):
        with mock.patch.object(storage_jobs, 'run') as ran:
            storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run', out=print)
        self.assertTrue(any('finish' in call.args[0] for call in ran.call_args_list))
        after_resume = self.saved()['locations']
        config = json.loads(self.settings[0].read_text())
        storage_jobs.finish(
            config, self.settings, self.root / 'state', self.root / 'run', out=print, sleep=lambda s: None
        )
        return after_resume

    def test_a_move_back_whose_copy_the_power_cut_lost_stays_where_it_was(self):
        # T1-14 (6): music d3 -> media ("Move back"); the copy into the old copy and its
        # rename into place never reached the media drive.
        self.config['locations'] = {'music': 'd3'}
        self.files(self.root / 'drives/d3/library/music', **{'a.flac': 'one', 'b.flac': 'two'})
        old = self.root / f'media/nekoserver-new-music-{ID}/library/music'
        self.files(old, **{'a.flac': 'one (the old copy)'})
        self.cut('music', 'd3', 'media', copied=[2, 0], action='back', copy='f' * 16)
        after_resume = self.resume_and_finish()
        self.assertEqual(after_resume, {'music': 'd3'})  # undone before Docker
        self.assertEqual(self.saved()['locations'], {'music': 'd3'})
        self.assertEqual((self.root / 'drives/d3/library/music/b.flac').read_text(), 'two')
        self.assertFalse((self.root / f'drives/d3/nekoserver-old-music-{ID}').exists())
        self.assertEqual(
            (self.root / f'media/nekoserver-old-music-{"f" * 16}/library/music/a.flac').read_text(),
            'one (the old copy)',
        )
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('did not survive', result['message'])
        self.assertIn('It stays on Extra drive d3', result['message'])
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(self.setups[-1]['locations'], {'music': 'd3'})

    def test_a_move_whose_new_folder_has_fewer_files_than_were_copied_is_undone(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'a.jpg': '1', 'b.jpg': '2', 'c.jpg': '3'})
        self.files(self.root / 'drives/d3/photos/immich-library', **{'a.jpg': '1'})  # the rest never written
        self.cut('photos', 'ssd', 'd3', copied=[3])
        self.resume_and_finish()
        self.assertEqual(self.saved()['locations'], {})
        self.assertEqual(
            sorted(p.name for p in (self.root / 'ssd/photos/immich-library').iterdir()), ['a.jpg', 'b.jpg', 'c.jpg']
        )
        self.assertFalse((self.root / 'drives/d3/photos/immich-library').exists())
        self.assertEqual(last_result(self.root)['state'], 'failed')

    def test_what_an_app_wrote_at_the_new_place_is_kept_when_a_lost_switch_is_undone(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'a.jpg': '1', 'b.jpg': '2', 'c.jpg': '3'})
        self.files(self.root / 'drives/d3/photos/immich-library', **{'a.jpg': '1', 'from-the-phone.jpg': 'new'})
        self.cut('photos', 'ssd', 'd3', copied=[3])
        self.resume_and_finish()
        staging = self.root / f'drives/d3/nekoserver-new-photos-{ID}'
        self.assertEqual((staging / 'photos/immich-library/from-the-phone.jpg').read_text(), 'new')
        self.assertEqual(self.saved()['locations'], {})
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn(str(staging), result['advice'])

    def test_an_empty_new_folder_after_setup_started_is_undone_from_the_retired_old_one(self):
        # Backups retired before setup ran, then the cut: the old place's files are in the
        # old copy's folder, the new one is empty.
        retired = self.root / f'media/nekoserver-old-backups-{ID}'
        self.files(retired / 'backups', **{'host.tar': 'h'})
        (self.root / 'drives/d3/backups').mkdir(parents=True)
        self.cut('backups', 'media', 'd3', phase='start')
        with mock.patch.object(storage_jobs, 'unbind_backups'), mock.patch.object(storage_jobs, 'rebind_backups'):
            self.resume_and_finish()
        self.assertEqual((self.root / 'media/backups/host.tar').read_text(), 'h')
        self.assertNotIn('backups', self.saved()['locations'])
        self.assertEqual(last_result(self.root)['state'], 'failed')

    def test_a_switch_that_survived_is_finished_as_before(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'a.jpg': '1'})
        self.files(self.root / 'drives/d3/photos/immich-library', **{'a.jpg': '1'})
        self.cut('photos', 'ssd', 'd3', copied=[1])
        self.resume_and_finish()
        self.assertEqual(self.saved()['locations'], {'photos': 'd3'})
        self.assertEqual(last_result(self.root)['state'], 'resumed')
        self.assertTrue((self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/a.jpg').exists())


class RetireTests(Jobs):
    def test_a_staging_folder_that_still_holds_files_is_kept(self):
        staging = self.root / f'drives/d3/nekoserver-new-photos-{ID}'
        self.files(staging / 'photos/immich-library', **{'only.jpg': 'the only copy'})
        (staging / 'empty/below').mkdir(parents=True)
        job = storage_jobs.Job(
            self.request('move', kind='photos', to='d3'), self.config, self.settings, self.root / 'state'
        )
        job.journal('start', True, staging=str(staging), here='ssd', to='d3')
        storage_jobs.retire_old(job, 'photos', 'ssd')
        self.assertEqual((staging / 'photos/immich-library/only.jpg').read_text(), 'the only copy')
        self.assertFalse((staging / 'empty').exists())

    def test_an_empty_staging_folder_goes(self):
        staging = self.root / f'drives/d3/nekoserver-new-photos-{ID}'
        (staging / 'photos').mkdir(parents=True)
        job = storage_jobs.Job(
            self.request('move', kind='photos', to='d3'), self.config, self.settings, self.root / 'state'
        )
        job.journal('start', True, staging=str(staging), here='ssd', to='d3')
        storage_jobs.retire_old(job, 'photos', 'ssd')
        self.assertFalse(staging.exists())


if __name__ == '__main__':
    unittest.main()
