"""1.0.1 storage from the dashboard: the root jobs (plans/1.0.1-contract.md §3, §4, §6).

A move copies one kind of data while the apps run, stops them, copies what changed,
compares, then switches; the old copy stays until it is deleted. The journal says, before
each step, which side of the commit point (the settings write) the job is on: a power cut
before it is undone at the next start, one after it is finished. The copies, the stops and
setup are migrate.py's, faked here; the folders are real (a temporary folder per drive)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate  # noqa: E402
import storage_jobs  # noqa: E402
from tests.test_storage_refusals import TB4, disk, part, world  # noqa: E402

ID = '0123456789abcdef'
GB = 10**9


def copy(source, target, final=False, out=print, again=False):
    shutil.copytree(source, target, dirs_exist_ok=True)


class Jobs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('ssd', 'media', 'drives/d3', 'state', 'run'):
            (self.root / name).mkdir(parents=True)
        self.config = {
            'data_mount': str(self.root / 'ssd'),
            'media_mount': str(self.root / 'media'),
            'storage': {'data_ssd': {'uuid': 'ssd-1'}, 'media_hdd': {'uuid': 'hdd-1'}, 'd3': {'uuid': 'd3-uuid'}},
            'locations': {},
        }
        self.settings = [self.root / 'config.json', self.root / 'my-config.json']
        self.started, self.setups = [], []
        self.differ = []
        for patch in (
            mock.patch('neko_common.EXTRA_MOUNTS', self.root / 'drives'),
            mock.patch('neko_common.OPERATION_LOCK', self.root / 'operation.lock'),
            mock.patch.object(storage_jobs, 'FACTS', {'min_bytes': 16 * GB}),
            mock.patch.object(storage_jobs, 'same_drive', lambda a, b: Path(a) == Path(b)),
            mock.patch.object(storage_jobs, 'free_bytes', lambda folder: 100 * GB),
            mock.patch.object(storage_jobs, 'du', lambda paths: 0),
            mock.patch.object(storage_jobs, 'steps_working', lambda config: []),
            mock.patch.object(
                storage_jobs, 'setup_again', lambda job: self.setups.append(json.loads(json.dumps(job.config)))
            ),
            mock.patch.object(migrate, 'copy', copy),
            mock.patch.object(migrate, 'differences', lambda a, b: self.differ),
            mock.patch.object(migrate, 'stop_writers', self.stop_writers),
            mock.patch.object(migrate, 'start_again', lambda stopped, out=print: self.started.append(stopped)),
            mock.patch.object(migrate, 'server_check', lambda config, before: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def stop_writers(self, out=print, note=None):
        note(['nekoserver-immich.service'])
        return ['nekoserver-immich.service']

    def files(self, folder, **named):
        for rel, text in named.items():
            (Path(folder) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(folder) / rel).write_text(text)

    def request(self, job, **fields):
        return {'format': storage_jobs.FORMAT, 'id': ID, 'job': job, **fields}

    def run_job(self, job, **fields):
        return storage_jobs.run_request(
            self.request(job, **fields),
            self.config,
            self.settings,
            self.root / 'state',
            self.root / 'run',
            out=lambda *_: None,
        )

    def result(self):
        return storage_jobs.read_state('result.json', self.root / 'state')

    def journal(self):
        return storage_jobs.read_state('journal.json', self.root / 'state')

    def saved(self, index=-1):
        return json.loads(self.settings[index].read_text())


class MoveTests(Jobs):
    def test_photos_moved_to_the_media_drive_keep_an_old_copy_on_the_apps_drive(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'a/x.jpg': 'one', 'y.jpg': 'two'})
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual((self.root / 'media/photos/immich-library/a/x.jpg').read_text(), 'one')
        old = self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/y.jpg'
        self.assertEqual(old.read_text(), 'two')
        self.assertFalse((self.root / 'ssd/photos/immich-library').exists())
        self.assertFalse((self.root / f'media/nekoserver-new-photos-{ID}').exists())
        for index in (0, 1):
            self.assertEqual(self.saved(index)['locations'], {'photos': 'media'})
        self.assertEqual(self.setups[0]['locations'], {'photos': 'media'})
        self.assertEqual(self.started, [['nekoserver-immich.service']])
        self.assertEqual(
            (self.result()['state'], self.result()['kind'], self.result()['to']), ('done', 'photos', 'media')
        )
        self.assertIsNone(self.journal())

    def test_a_copy_that_differs_changes_nothing_and_starts_the_apps_again(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.differ = ['x.jpg']
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertEqual(list((self.root / 'media').iterdir()), [])
        self.assertFalse(self.settings[0].exists())
        self.assertEqual(self.started, [['nekoserver-immich.service']])
        self.assertEqual(self.result()['state'], 'undone')
        self.assertIn('not the same as the original', self.result()['message'])

    def test_a_place_that_is_not_allowed_is_refused_before_anything(self):
        self.assertEqual(self.run_job('move', kind='backups', to='ssd'), 1)
        self.assertEqual((self.result()['state'], self.started), ('failed', []))
        self.assertIn('must be on another drive', self.result()['message'])

    def test_another_job_at_work_is_said(self):
        import fcntl

        with open(self.root / 'operation.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual(self.result()['state'], 'failed')

    def test_backups_moved_to_an_extra_drive_leave_their_old_place_before_setup(self):
        self.files(self.root / 'media', **{'backups/host.tar': 'h', 'photos/archive/2020.zip': 'z'})
        with mock.patch.object(storage_jobs, 'setup_again', self.no_backups_left):
            self.assertEqual(self.run_job('move', kind='backups', to='d3'), 0)
        self.assertEqual((self.root / 'drives/d3/backups/host.tar').read_text(), 'h')
        self.assertTrue((self.root / f'media/nekoserver-old-backups-{ID}/photos/archive/2020.zip').exists())
        self.assertEqual(self.saved()['locations'], {'backups': 'd3'})

    def no_backups_left(self, job):
        self.assertFalse((self.root / 'media/backups').exists(), 'setup would refuse to mount over them')


class BackAndDeleteTests(Jobs):
    def moved(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.run_job('move', kind='photos', to='media')
        self.files(self.root / 'media/photos/immich-library', **{'new.jpg': 'since'})

    def test_photos_go_back_into_their_old_copy_with_what_changed_since(self):
        self.moved()
        self.assertEqual(self.run_job('back', kind='photos', copy=ID), 0)
        self.assertEqual((self.root / 'ssd/photos/immich-library/new.jpg').read_text(), 'since')
        self.assertTrue((self.root / f'media/nekoserver-old-photos-{ID}/photos/immich-library/x.jpg').exists())
        self.assertFalse((self.root / f'ssd/nekoserver-old-photos-{ID}').exists())
        self.assertEqual(self.saved()['locations'], {'photos': 'ssd'})

    def test_a_way_back_that_differs_leaves_the_old_copy_as_it_was(self):
        self.moved()
        self.differ = ['new.jpg']
        self.assertEqual(self.run_job('back', kind='photos', copy=ID), 1)
        self.assertTrue((self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/x.jpg').exists())
        self.assertTrue((self.root / 'media/photos/immich-library/new.jpg').exists())
        self.assertEqual(self.saved()['locations'], {'photos': 'media'})

    def test_an_old_copy_is_deleted_only_with_its_words(self):
        self.moved()
        old = self.root / f'ssd/nekoserver-old-photos-{ID}'
        self.assertEqual(self.run_job('delete_old', kind='photos', copy=ID, confirm='delete it'), 1)
        self.assertTrue(old.exists())
        self.assertEqual(self.run_job('delete_old', kind='photos', copy=ID, confirm='Delete  Photos'), 0)
        self.assertFalse(old.exists())
        self.assertEqual([p.name for p in (self.root / 'ssd').iterdir() if p.name.startswith('nekoserver-')], [])


class PowerCutTests(Jobs):
    def cut(self, phase, committed, **undo):
        job = storage_jobs.Job(
            self.request('move', kind='photos', to='media'),
            self.config,
            self.settings,
            self.root / 'state',
            self.root / 'run',
        )
        staging = self.root / f'media/nekoserver-new-photos-{ID}'
        job.journal(phase, committed, staging=str(staging), here='ssd', to='media', **undo)
        return staging

    def resume(self, run=None):
        with mock.patch.object(storage_jobs, 'run', run or mock.Mock()):
            return storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run')

    def test_a_cut_during_the_copy_removes_the_new_copy(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        staging = self.cut('copy', False)
        self.files(staging / 'photos/immich-library', **{'x.jpg': 'on'})
        self.assertEqual(self.resume(), 0)
        self.assertFalse(staging.exists())
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertEqual((self.result()['state'], self.result()['power_cut']), ('undone', True))
        self.assertIsNone(self.journal())

    def test_a_cut_during_the_switch_puts_the_new_copy_out_of_the_way_again(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.cut('switch', False)
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.assertEqual(self.resume(), 0)
        self.assertFalse((self.root / 'media/photos/immich-library').exists())
        self.assertEqual(self.result()['state'], 'undone')

    def test_a_cut_after_the_settings_were_written_is_finished_after_docker(self):
        self.cut('start', True)
        ran = mock.Mock()
        self.assertEqual(self.resume(ran), 0)
        argv = ran.call_args[0][0]
        self.assertEqual(
            argv[:4],
            ['systemd-run', '--no-block', '--unit=nekoserver-storage-finish', '--property=After=docker.service'],
        )
        self.assertEqual(argv[-3:], ['finish', f'--config={self.settings[0]}', f'--config={self.settings[1]}'])
        self.assertTrue(self.journal()['committed'])

    def test_the_finish_copies_again_what_was_written_meanwhile_and_retires_the_old_folders(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one', 'late.jpg': 'meanwhile'})
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.config['locations'] = {'photos': 'media'}
        self.cut('start', True)
        self.assertEqual(storage_jobs.finish(self.config, self.settings, self.root / 'state', self.root / 'run'), 0)
        self.assertEqual((self.root / 'media/photos/immich-library/late.jpg').read_text(), 'meanwhile')
        self.assertTrue((self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/x.jpg').exists())
        self.assertEqual(self.result()['state'], 'resumed')
        self.assertIsNone(self.journal())

    def test_a_drive_the_job_needs_that_is_missing_keeps_the_journal(self):
        self.config['storage']['d3']['uuid'] = 'd3-uuid'
        job = storage_jobs.Job(
            self.request('move', kind='photos', to='d3'),
            self.config,
            self.settings,
            self.root / 'state',
            self.root / 'run',
        )
        job.journal('copy', False, staging='x', here='ssd', to='d3')
        (self.root / 'drives/d3').rmdir()
        self.assertEqual(self.resume(), 1)
        self.assertIn('is finished when Extra drive d3 is back', self.result()['message'])
        self.assertIsNotNone(self.journal())


class DriveTests(Jobs):
    def setUp(self):
        super().setUp()
        self.disk = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='ext4', uuid='u-new', size=TB4)])
        patch = mock.patch.object(storage_jobs, 'world_now', lambda config: world(self.disk))
        patch.start()
        self.addCleanup(patch.stop)

    def asked(self):
        shown = storage_jobs.describe(dict(world(self.disk), named={}))[0]
        return {k: shown[k] for k in ('by_id', 'serial', 'size_bytes')}

    def test_a_formatted_disk_is_taken_over_as_the_next_extra_drive(self):
        self.assertEqual(self.run_job('use', how='take_over', disk=self.asked()), 0)
        self.assertEqual(self.saved()['storage']['d4'], {'uuid': 'u-new', 'fstype': 'ext4', 'added': True})
        self.assertEqual(len(self.setups), 1)

    def test_an_erase_needs_the_words_of_the_disk(self):
        with mock.patch.object(storage_jobs, 'erase') as erase:
            self.assertEqual(self.run_job('use', how='erase', disk=self.asked(), confirm='4 TB AAAB'), 1)
        erase.assert_not_called()
        self.assertIn('Type exactly: 4 TB AAAA', self.result()['advice'])
        self.assertFalse(self.settings[0].exists())

    def test_another_disk_than_the_page_showed_is_refused(self):
        asked = dict(self.asked(), serial='S-OTHER')
        self.assertEqual(self.run_job('use', how='take_over', disk=asked), 1)
        self.assertIn('not plugged in the way the page showed it', self.result()['message'])

    def test_an_erase_cut_after_the_format_adds_the_drive_only_with_the_ids_it_was_given(self):
        for there, state in ((False, 'undone'), (True, 'resumed')):
            job = storage_jobs.Job(
                self.request('use', how='erase', disk=self.asked()),
                self.config,
                self.settings,
                self.root / 'state',
                self.root / 'run',
            )
            job.journal('format', False, drive='d4', erased={'phase': 'format', 'uuid': 'u-made', 'partuuid': 'p'})
            with (
                mock.patch.object(storage_jobs, 'uuid_there', lambda uuid: there),
                mock.patch.object(storage_jobs, 'run', mock.Mock()),
            ):
                storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run')
            if there:
                self.assertTrue(self.journal()['committed'])
                self.assertEqual(self.saved()['storage']['d4']['uuid'], 'u-made')
            else:
                self.assertEqual(self.result()['state'], state)
                self.assertIsNone(self.journal())

    def test_an_extra_drive_with_nothing_on_it_is_given_up(self):
        fstab = self.root / 'fstab'
        line = f'UUID=d3-uuid {self.root}/drives/d3 ext4 defaults,nofail 0 2\n'
        fstab.write_text('UUID=x / ext4 defaults 0 1\n' + line)
        with (
            mock.patch('modules.storage.FSTAB', fstab),
            mock.patch('modules.storage.own_line', lambda f: 'd3' in f[1]),
            mock.patch.object(storage_jobs, 'run', mock.Mock()),
        ):
            self.assertEqual(self.run_job('release', drive='d3'), 0)
        self.assertEqual(fstab.read_text(), 'UUID=x / ext4 defaults 0 1\n')
        self.assertNotIn('d3', self.saved()['storage'])

    def test_a_drive_with_data_on_it_is_never_given_up(self):
        self.config['locations']['music'] = 'd3'
        self.assertEqual(self.run_job('release', drive='d3'), 1)
        self.assertIn('Move them first', self.result()['message'])


class MirrorTests(Jobs):
    """Job D is mirror_add.py's (T2): this file checks the disk and its words, then hands
    it the one erase and the journal."""

    def setUp(self):
        super().setUp()
        self.disk = disk('sdc', serial='S-AAAA', size=TB4)
        self.handed = []
        fake = mock.Mock()
        fake.Context = lambda **named: named
        fake.run_job = lambda request, ctx: (
            self.handed.append(ctx)
            or {
                'state': 'done',
                'message': 'The new drive is in the mirror.',
                'advice': None,
            }
        )
        for patch in (
            mock.patch.dict(sys.modules, mirror_add=fake),
            mock.patch.object(storage_jobs, 'world_now', lambda config: world(self.disk)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def asked(self):
        shown = storage_jobs.describe(dict(world(self.disk), named={}))[0]
        return {k: shown[k] for k in ('by_id', 'serial', 'size_bytes')}

    def test_the_mirror_job_gets_the_one_erase_after_the_words_of_the_disk(self):
        self.assertEqual(self.run_job('mirror_add', array='md-md0', disk=self.asked(), confirm='4 tb aaaa'), 0)
        self.assertIs(self.handed[0]['refused'], storage_jobs.Refused)
        self.assertEqual(self.result()['state'], 'done')
        self.assertIsNone(self.journal())

    def test_other_words_erase_nothing(self):
        self.assertEqual(self.run_job('mirror_add', array='md-md0', disk=self.asked(), confirm='4 TB'), 1)
        self.assertEqual(self.handed, [])


class UnitTests(unittest.TestCase):
    FACTS = {
        'unit': 'nekoserver-storage.service',
        'resume': 'nekoserver-storage-resume.service',
        'folder': '/run/nekoserver-control/storage',
        'request': 'request.json',
        'state': '/etc/nekoserver-backups/storage',
        'journal': 'journal.json',
    }

    def test_the_job_runs_this_release_with_both_settings_and_the_resume_comes_before_docker(self):
        from modules import dashboard

        with mock.patch.dict(dashboard.SHARED, storage_jobs=self.FACTS):
            units = dashboard.storage_units('/srv/nekoserver', '/home/neko/my-config.json')
        bundle = f'/srv/nekoserver/installer/releases/v{dashboard.VERSION}'
        job = units['nekoserver-storage.service']
        self.assertIn(
            f'ExecStart=/usr/bin/python3 -Es {bundle}/storage_jobs.py run --config {bundle}/config.json '
            '--config "/home/neko/my-config.json" --request /run/nekoserver-control/storage/request.json\n',
            job,
        )
        self.assertIn('TimeoutStartSec=infinity', job)
        self.assertNotIn('[Install]', job)
        resume = units['nekoserver-storage-resume.service']
        self.assertIn('Before=docker.service\n', resume)
        self.assertIn('ConditionPathExists=/etc/nekoserver-backups/storage/journal.json\n', resume)
        self.assertIn('storage_jobs.py resume --config', resume)
        self.assertIn('WantedBy=multi-user.target', resume)


if __name__ == '__main__':
    unittest.main()
