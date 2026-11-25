"""1.0.1 storage from the dashboard: the root jobs (plans/1.0.1-contract.md §3, §4, §6).

A move copies one kind of data while the apps run, stops them, copies what changed,
compares, then switches; the old copy stays until it is deleted. The journal says, before
each step, which side of the commit point (the settings write) the job is on: a power cut
before it is undone at the next start, one after it is finished. The copies, the stops and
setup are migrate.py's, faked here; the folders are real (a temporary folder per drive)."""

import json
import os
import shutil
import sqlite3
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


def last_result(root):
    """result.json of the jobs a test ran in `root` (the folder its drives are in)."""
    return storage_jobs.read_state('result.json', Path(root) / 'state')


def saved_journal(root):
    """journal.json there, or None once the job is done."""
    return storage_jobs.read_state('journal.json', Path(root) / 'state')


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
        # Each drive is there while its folder is (T1-18); a test unplugs one, or puts
        # another disk in its place, here.
        self.unplugged = {}
        for patch in (
            mock.patch('neko_common.mounted_uuid', self.mounted, create=True),
            mock.patch('neko_common.EXTRA_MOUNTS', self.root / 'drives'),
            mock.patch('neko_common.OPERATION_LOCK', self.root / 'operation.lock'),
            mock.patch.object(storage_jobs, 'FACTS', {'min_bytes': 16 * GB}),
            mock.patch.object(storage_jobs, 'same_drive', lambda a, b: Path(a) == Path(b)),
            mock.patch.object(storage_jobs, 'free_bytes', return_value=100 * GB),
            mock.patch.object(storage_jobs, 'du', return_value=0),
            mock.patch.object(storage_jobs, 'steps_working', lambda config: []),
            mock.patch.object(
                storage_jobs, 'setup_again', lambda job: self.setups.append(json.loads(json.dumps(job.config)))
            ),
            mock.patch.object(migrate, 'copy', copy),
            mock.patch.object(migrate, 'differences', return_value=self.differ),
            mock.patch.object(migrate, 'stop_writers', self.stop_writers),
            mock.patch.object(migrate, 'start_again', lambda stopped, out=print: self.started.append(stopped)),
            mock.patch.object(migrate, 'server_check', return_value=None),
            # The look written after a job reads the computer's disks: never in these tests.
            mock.patch.object(storage_jobs, 'look', lambda config: {'disks': []}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def mounted(self, folder):
        state = self.unplugged.get(Path(folder))
        if state == 'missing' or not Path(folder).is_dir():
            return None
        return 'another-disk' if state == 'other' else ''

    def unplug(self, place, how='missing'):
        self.unplugged[storage_jobs.root_of(self.config, place)] = how

    def plug_in(self, place):
        self.unplugged.pop(storage_jobs.root_of(self.config, place), None)

    def stop_writers(self, out=print, note=None, own=()):
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
            (last_result(self.root)['state'], last_result(self.root)['kind'], last_result(self.root)['to']),
            ('done', 'photos', 'media'),
        )
        self.assertIsNone(saved_journal(self.root))

    def test_a_copy_that_differs_changes_nothing_and_starts_the_apps_again(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.differ.extend(['x.jpg'])
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertEqual(list((self.root / 'media').iterdir()), [])
        self.assertFalse(self.settings[0].exists())
        self.assertEqual(self.started, [['nekoserver-immich.service']])
        self.assertEqual(last_result(self.root)['state'], 'undone')
        self.assertIn('not the same as the original', last_result(self.root)['message'])

    def test_a_place_that_is_not_allowed_is_refused_before_anything(self):
        self.assertEqual(self.run_job('move', kind='backups', to='ssd'), 1)
        self.assertEqual((last_result(self.root)['state'], self.started), ('failed', []))
        self.assertIn('must be on another drive', last_result(self.root)['message'])

    def test_another_job_at_work_is_said(self):
        import fcntl

        with open(self.root / 'operation.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual(last_result(self.root)['state'], 'failed')

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
        self.differ.extend(['new.jpg'])
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
        self.assertEqual((last_result(self.root)['state'], last_result(self.root)['power_cut']), ('undone', True))
        self.assertIsNone(saved_journal(self.root))

    def test_a_cut_during_the_switch_puts_the_new_copy_out_of_the_way_again(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.cut('switch', False)
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        self.assertEqual(self.resume(), 0)
        self.assertFalse((self.root / 'media/photos/immich-library').exists())
        self.assertEqual(last_result(self.root)['state'], 'undone')

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
        self.assertTrue(saved_journal(self.root)['committed'])

    def test_the_finish_copies_again_what_was_written_meanwhile_and_retires_the_old_folders(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one', 'late.jpg': 'meanwhile'})
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one'})
        # Cut after the journal's commit and before the settings were written: the settings
        # on disk still say "ssd"; the finish takes the change from the journal.
        self.cut('start', True, change=[['locations', 'photos', 'media']])
        self.assertEqual(storage_jobs.finish(self.config, self.settings, self.root / 'state', self.root / 'run'), 0)
        self.assertEqual(self.setups[0]['locations'], {'photos': 'media'})
        self.assertEqual(self.saved(0)['locations'], {'photos': 'media'})
        self.assertTrue((self.root / 'media/photos/immich-library/x.jpg').exists())
        self.assertEqual((self.root / 'media/photos/immich-library/late.jpg').read_text(), 'meanwhile')
        self.assertTrue((self.root / f'ssd/nekoserver-old-photos-{ID}/photos/immich-library/x.jpg').exists())
        self.assertEqual(last_result(self.root)['state'], 'resumed')
        self.assertIsNone(saved_journal(self.root))

    def test_a_job_cut_before_its_switch_is_undone_without_a_drive_that_is_missing(self):
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
        # T1-18: never held up by the drive; nothing is written on it, and the job is undone.
        self.assertEqual(self.resume(), 0)
        self.assertEqual(last_result(self.root)['state'], 'undone')
        self.assertIn('Extra drive d3 is not connected', last_result(self.root)['message'])
        self.assertIsNone(saved_journal(self.root))


class DriveTests(Jobs):
    def setUp(self):
        super().setUp()
        self.disk = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='ext4', uuid='u-new', size=TB4)])
        patch = mock.patch.object(storage_jobs, 'world_now', return_value=world(self.disk))
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
        self.assertIn('Type exactly: 4 TB AAAA', last_result(self.root)['advice'])
        self.assertFalse(self.settings[0].exists())

    def test_another_disk_than_the_page_showed_is_refused(self):
        asked = dict(self.asked(), serial='S-OTHER')
        self.assertEqual(self.run_job('use', how='take_over', disk=asked), 1)
        self.assertIn('not plugged in the way the page showed it', last_result(self.root)['message'])

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
                mock.patch.object(storage_jobs, 'uuid_there', return_value=there),
                mock.patch.object(storage_jobs, 'run', mock.Mock()),
            ):
                storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run')
            if there:
                self.assertTrue(saved_journal(self.root)['committed'])
                self.assertEqual(self.saved()['storage']['d4']['uuid'], 'u-made')
            else:
                self.assertEqual(last_result(self.root)['state'], state)
                self.assertIsNone(saved_journal(self.root))

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
        self.assertIn('Move them first', last_result(self.root)['message'])

    def test_a_drive_with_a_media_folder_of_the_person_on_it_is_never_given_up(self):
        self.config['placement'] = {'movies': {'extra': [{'path': str(self.root / 'drives/d3/My films')}]}}
        self.assertEqual(self.run_job('release', drive='d3'), 1)
        self.assertIn('Media folders page', last_result(self.root)['message'])
        self.assertIn('d3', self.config['storage'])

    def test_an_added_drive_cut_before_its_settings_were_written_is_added_at_the_finish(self):
        job = storage_jobs.Job(
            self.request('use', how='take_over', disk=self.asked()),
            self.config,
            self.settings,
            self.root / 'state',
            self.root / 'run',
        )
        entry = {'uuid': 'u-new', 'fstype': 'ext4', 'added': True}
        job.journal('mount', True, drive='d4', change=[['storage', 'd4', entry]])
        with mock.patch.object(storage_jobs, 'run', mock.Mock()):
            storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run')
        self.assertEqual(self.saved(0)['storage']['d4'], entry)
        self.assertEqual(storage_jobs.finish(self.config, self.settings, self.root / 'state', self.root / 'run'), 0)
        self.assertEqual(self.setups[0]['storage']['d4'], entry)
        self.assertEqual(last_result(self.root)['state'], 'resumed')

    def test_a_device_of_a_mounted_pool_of_several_is_never_offered(self):
        first = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='btrfs', uuid='pool', mountpoints=['/x'])])
        second = disk('sdd', serial='S-BBBB', children=[part('sdd1', fstype='btrfs', uuid='pool')])
        second['maj:min'], second['children'][0]['maj:min'] = '8:48', '8:49'
        rows = storage_jobs.describe(dict(world(first, second), named={}))
        self.assertEqual(storage_jobs.refusal(rows[1], 0)['code'], 'mounted')


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
            mock.patch.object(storage_jobs, 'world_now', return_value=world(self.disk)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def asked(self):
        shown = storage_jobs.describe(dict(world(self.disk), named={}))[0]
        return {k: shown[k] for k in ('by_id', 'serial', 'size_bytes')}

    def test_the_mirror_job_gets_the_one_erase_after_the_words_of_the_disk(self):
        self.assertEqual(self.run_job('mirror_add', array='md-md0', disk=self.asked(), confirm='4 tb aaaa'), 0)
        self.assertIs(self.handed[0]['refused'], storage_jobs.Refused)
        self.assertEqual(last_result(self.root)['state'], 'done')
        self.assertIsNone(saved_journal(self.root))

    def test_other_words_erase_nothing(self):
        self.assertEqual(self.run_job('mirror_add', array='md-md0', disk=self.asked(), confirm='4 TB'), 1)
        self.assertEqual(self.handed, [])


class OutOfTheWayTests(unittest.TestCase):
    """Old copies, new copies at work and copies being deleted sit at a drive's top, where
    no backup, snapshot, app or size check looks (plans/1.0.1-findings.md lists each one);
    only a whole-drive move copies a drive's top, and it waits for a Drives job."""

    # What the nightly and monthly snapshots, the backups and the apps read on a drive.
    READ = ('appdata', 'compose', 'installer', 'secrets', 'gateway', 'cards', 'nekoserver')

    def test_every_kind_of_copy_is_outside_every_folder_something_reads(self):
        from neko_common import LOCATIONS

        root = Path('/srv/nekoserver')
        read = [root / rel for rel in self.READ]
        read += [root / rel for _, rels in LOCATIONS.values() for rel in rels]
        read += [root / rel for rel in storage_jobs.kind_folders('backups')]
        for kind in storage_jobs.KINDS:
            for name in (storage_jobs.OLD_NAME, storage_jobs.NEW_COPY, storage_jobs.DELETING):
                copy = root / name.format(kind=kind, id=ID) / storage_jobs.kind_folders(kind)[0]
                self.assertEqual(copy.relative_to(root).parts[0][:11], 'nekoserver-')
                self.assertFalse([r for r in read if r == copy or r in copy.parents], copy)

    def test_a_folder_in_a_copy_is_never_one_of_the_persons_media_folders(self):
        import existing_media

        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': f'{tmp}/ssd', 'media_mount': f'{tmp}/media'}
            mine = Path(tmp) / 'd3/My films'
            old = Path(tmp) / f'd3/nekoserver-old-movies-{ID}/library/movies'
            for folder in (mine, old):
                folder.mkdir(parents=True)
            self.assertEqual(existing_media.checked_folder(config, str(mine)), str(mine))
            with self.assertRaises(existing_media.Refused) as refused:
                existing_media.checked_folder(config, str(old))
            self.assertEqual(refused.exception.code, 'own')
            with self.assertRaises(RuntimeError):
                existing_media.usable_saved_path(config, 'movies', str(old))

    def test_a_whole_drive_move_waits_for_a_drives_job_that_is_not_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'journal.json').write_text('{}')
            with mock.patch.object(storage_jobs, 'STATE', Path(tmp)):
                why = migrate.nothing_at_work(lock=Path(tmp) / 'setup.lock')
        self.assertEqual(why, 'A change on the Drives page is not finished yet (restart the computer)')


class WhatTheToolReadsTests(unittest.TestCase):
    """The unit's arguments and the settings name the files the root tool reads: only the
    control service's own request file, and the cards' catalogue inside the apps drive."""

    def test_only_the_control_services_request_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / 'request.json'
            with mock.patch.object(storage_jobs, 'REQUEST', own):
                self.assertEqual(storage_jobs.request_path(str(own)), os.path.realpath(own))
                for other in ('/etc/shadow', f'{tmp}/../request.json', f'{tmp}/other.json'):
                    with self.assertRaises(SystemExit):
                        storage_jobs.request_path(other)
                (Path(tmp) / 'link.json').symlink_to('/etc/hostname')
                with self.assertRaises(SystemExit):
                    storage_jobs.request_path(f'{tmp}/link.json')

    def test_the_cards_catalogue_is_read_only_inside_the_apps_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'ssd'
            folder = data / storage_jobs.SHARED['cards']['folder']
            folder.mkdir(parents=True)
            outside = Path(tmp) / 'elsewhere.sqlite'
            with sqlite3.connect(outside) as db:
                db.execute('CREATE TABLE volumes (filesystem_uuid TEXT)')
                db.execute("INSERT INTO volumes VALUES ('u-card')")
            catalogue = folder / storage_jobs.SHARED['cards']['catalogue']
            catalogue.symlink_to(outside)
            self.assertEqual(storage_jobs.card_uuids({'data_mount': str(data)}), set())
            catalogue.unlink()
            shutil.copy(outside, catalogue)
            self.assertEqual(storage_jobs.card_uuids({'data_mount': str(data)}), {'u-card'})
            for unsafe in ('', 'relative/ssd', f'{data}/../ssd', '/'):
                self.assertEqual(storage_jobs.card_uuids({'data_mount': unsafe}), set())


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
