"""T1-20 (1.0.1 drill 5): after d4 was dropped from the settings with its mount left, a new
disk was named d4 and its setup failed "Wrong filesystem already mounted at
/mnt/nekoserver-drives/d4" at every start, and every job was refused (E47) for good. A new
drive's name is free of a mount folder, an fstab line and the record; a finish that can
not succeed is shown with "Try again" and "Give it up", and giving up an added drive takes
it out of the settings."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neko_common  # noqa: E402
import storage_jobs  # noqa: E402
from tests.test_storage_jobs import ID, Jobs, last_result, saved_journal  # noqa: E402


class NameTests(Jobs):
    def setUp(self):
        super().setUp()
        self.fstab = self.root / 'fstab'
        self.fstab.write_text('UUID=root / ext4 defaults 0 1\n')
        self.record = self.root / 'state/places.json'

    def name(self):
        return storage_jobs.next_drive(self.config, fstab=self.fstab, record=self.record)

    def test_the_next_name_after_the_drives_of_the_settings(self):
        self.assertEqual(self.name(), 'd4')

    def test_a_name_whose_mount_folder_is_there_is_not_used_again(self):
        (self.root / 'drives/d4').mkdir()
        self.assertEqual(self.name(), 'd5')

    def test_a_name_in_fstab_is_not_used_again(self):
        line = f'UUID=x {self.root}/drives/d4 ext4 nofail 0 2\n'
        self.fstab.write_text(self.fstab.read_text() + line)
        self.assertEqual(self.name(), 'd5')

    def test_a_name_in_the_record_is_not_used_again(self):
        config = json.loads(json.dumps(self.config))
        config['storage']['d4'] = {'uuid': 'old-d4'}
        neko_common.write_places(config, self.record)
        self.assertEqual(self.name(), 'd5')


def use_journal(root, phase='mount'):
    storage_jobs.write_state(
        'journal.json',
        {
            'id': ID,
            'job': 'use',
            'phase': phase,
            'committed': True,
            'boot': 'before',
            'started_at': 1.0,
            'request': {'format': storage_jobs.FORMAT, 'id': ID, 'job': 'use', 'how': 'erase'},
            'undo': {
                'drive': 'd5',
                'change': [['storage', 'd5', {'uuid': 'new-uuid', 'fstype': 'ext4', 'added': True}]],
            },
        },
        Path(root) / 'state',
    )


class StuckAddTests(Jobs):
    def setUp(self):
        super().setUp()
        self.config['storage']['d5'] = {'uuid': 'new-uuid', 'fstype': 'ext4', 'added': True}
        for path in self.settings:
            path.write_text(json.dumps(self.config))
        use_journal(self.root)

    def test_a_drive_whose_setup_fails_at_every_start_is_shown_with_a_way_on(self):
        with mock.patch.object(
            storage_jobs, 'setup_again', side_effect=RuntimeError('Wrong filesystem already mounted')
        ):
            code = storage_jobs.finish(
                self.config, self.settings, self.root / 'state', self.root / 'run', out=print, sleep=lambda s: None
            )
        self.assertEqual(code, 1)
        result = last_result(self.root)
        self.assertIn('Try again', result['advice'])
        self.assertIn('Give it up', result['advice'])

    def test_giving_up_an_added_drive_takes_it_out_of_the_settings(self):
        self.assertEqual(self.run_job('give_up'), 1)
        self.assertIsNone(saved_journal(self.root))
        self.assertNotIn('d5', self.saved()['storage'])
        self.assertIn('not added', last_result(self.root)['message'])

    def test_trying_again_finishes_it(self):
        self.assertEqual(self.run_job('retry'), 0)
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(self.saved()['storage']['d5']['uuid'], 'new-uuid')


class MirrorTests(Jobs):
    """T2's job D (57eb40c): mirror_add.Context asks away(folder) of the mirror's mount
    folder before the erase, the add and a cut-off finish; storage_jobs wires the drive
    check (T1-18) into it."""

    def test_the_mirror_job_is_given_the_drive_check(self):
        import mirror_add

        given = {}

        def context(**named):
            given.update(named)

        with mock.patch.object(mirror_add, 'Context', context):
            job = storage_jobs.Job(
                {'id': ID, 'job': 'mirror_add'}, self.config, self.settings, self.root / 'state', self.root / 'run'
            )
            storage_jobs.mirror_context(job)
        self.assertIsNone(given['away'](self.root / 'drives/d3/pool'))
        self.unplug('d3')
        self.assertEqual(given['away'](self.root / 'drives/d3'), 'missing')
        self.assertIsNone(given['away']('/nowhere'))

    def test_a_mirror_job_that_takes_no_drive_check_fails_loudly(self):
        import mirror_add

        def context(*, run, erase, refused, journal, progress, clear):
            raise AssertionError('never built without the drive check')

        with mock.patch.object(mirror_add, 'Context', context):
            job = storage_jobs.Job(
                {'id': ID, 'job': 'mirror_add'}, self.config, self.settings, self.root / 'state', self.root / 'run'
            )
            with self.assertRaises(TypeError):
                storage_jobs.mirror_context(job)


if __name__ == '__main__':
    unittest.main()
