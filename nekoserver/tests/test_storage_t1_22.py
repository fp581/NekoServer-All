"""T1-22 (1.0.1): a Drives job and the nightly backups overlapped. An erase ran its setup
re-run while the 04:15 app backup ran: setup's account_credentials, accounts and
gateway_tailnet steps were refused ("A NekoServer backup ... is running right now"), the
backup failed, and the restart that finished the job said "The power went off" with no
power cut. A job is refused up front while a backup, a restore or the USB copy runs; the
nightly jobs give way to a running job (the runtime's common::nightly_lock_wait); setup
inside a job waits for the backups' lock instead of being refused; and a job that stopped
for a reason the journal recorded says that reason, never a power cut."""

import fcntl
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrate  # noqa: E402
import recovery_tools  # noqa: E402
import storage_jobs  # noqa: E402
from tests.test_storage_jobs import ID, Jobs, last_result, saved_journal  # noqa: E402

REAL_SETUP_AGAIN = storage_jobs.setup_again  # the Jobs base fakes it


def held(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, 'a')
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def use_journal(root, drive):
    storage_jobs.write_state(
        'journal.json',
        {
            'id': ID,
            'job': 'use',
            'phase': 'mount',
            'committed': True,
            'boot': 'before',
            'started_at': 1.0,
            'request': {'format': storage_jobs.FORMAT, 'id': ID, 'job': 'use', 'how': 'erase'},
            'undo': {'drive': drive},
        },
        Path(root) / 'state',
    )


class UpFrontTests(Jobs):
    """(1) A job asked for while a NekoServer backup, restore or USB copy runs is refused
    before anything is done, with the reason and "try again when it has finished"."""

    def setUp(self):
        super().setUp()
        self.files(self.root / 'ssd/photos/immich-library', **{'a.jpg': 'one'})

    def refused(self, lock):
        with held(lock):
            code = self.run_job('move', kind='photos', to='media')
        self.assertEqual(code, 1)
        said = last_result(self.root)['message']
        self.assertIn('is running right now', said)
        self.assertIn('Try again when it has finished', said)
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(self.setups, [])
        self.assertTrue((self.root / 'ssd/photos/immich-library/a.jpg').exists())
        self.assertFalse((self.root / 'media/photos').exists())

    def test_a_backup_or_restore_holding_the_backups_lock_refuses_the_job(self):
        self.refused(migrate.recovery_lock(self.config))

    def test_the_usb_copy_refuses_the_job(self):
        self.refused(self.root / 'usb.lock')

    def test_try_again_waits_for_it_too(self):
        use_journal(self.root, 'd3')
        with held(migrate.recovery_lock(self.config)):
            self.assertEqual(self.run_job('retry'), 1)
        self.assertIn('Try again when it has finished', last_result(self.root)['message'])
        self.assertIsNotNone(saved_journal(self.root), 'still waiting to be finished')

    def test_with_nothing_at_work_the_job_runs(self):
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertTrue((self.root / 'media/photos/immich-library/a.jpg').exists())


class SetupInAJobTests(Jobs):
    """(4) Setup re-run inside a job waits for the backups' lock (a nightly job that took
    it a moment before giving way) instead of refusing its steps."""

    def lock_asked(self, env, tty=False):
        asked = []
        config = {'data_mount': str(self.root / 'ssd')}
        (self.root / 'ssd/installer').mkdir(parents=True, exist_ok=True)
        with (
            mock.patch.object(
                recovery_tools, 'wait_for', lambda stream, patient, limit: asked.append((patient, limit))
            ),
            mock.patch.object(recovery_tools.neko_common, 'pending_maintenance', return_value=None),
            mock.patch.object(recovery_tools.sys.stdin, 'isatty', return_value=tty),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            with recovery_tools.lock(config):
                pass
        return asked

    def test_the_setup_of_a_job_is_told_to_wait_for_the_lock(self):
        seen = []

        def setup_again(*_args, **_kwargs):
            seen.append(os.environ.get(storage_jobs.LOCK_WAIT))

        job = storage_jobs.Job(self.request('use'), self.config, self.settings, self.root / 'state', self.root / 'run')
        with mock.patch.object(migrate, 'setup_again', side_effect=setup_again):
            REAL_SETUP_AGAIN(job)
        self.assertEqual(seen, [str(storage_jobs.SETUP_LOCK_WAIT)])
        self.assertNotIn(storage_jobs.LOCK_WAIT, os.environ, 'only for that setup')

    def test_the_lock_waits_when_a_job_says_so(self):
        self.assertEqual(self.lock_asked({storage_jobs.LOCK_WAIT: '600'}), [(True, 600)])

    def test_without_it_a_run_that_is_not_at_a_terminal_does_not_wait(self):
        self.assertEqual(self.lock_asked({}), [(False, 1800)])
        self.assertEqual(self.lock_asked({}, tty=True), [(True, 1800)])
        self.assertEqual(self.lock_asked({storage_jobs.LOCK_WAIT: 'x'}), [(False, 1800)], 'nonsense is ignored')


class StoppedWordsTests(Jobs):
    """(3) A job that stopped after its commit point for a reason other than a power cut
    records it; finishing it says that reason, never "The power went off"."""

    def setUp(self):
        super().setUp()
        self.config['storage']['d5'] = {'uuid': 'new-uuid', 'fstype': 'ext4', 'added': True}
        for path in self.settings:
            path.write_text(json.dumps(self.config))

    def finish(self):
        return storage_jobs.finish(
            self.config,
            self.settings,
            self.root / 'state',
            self.root / 'run',
            out=lambda *_: None,
            sleep=lambda s: None,
        )

    def test_a_refused_step_is_recorded_and_named_when_the_job_is_finished(self):
        def stops(job):
            job.journal('mount', True, drive='d5')
            raise RuntimeError("Setup's step accounts was refused: a NekoServer backup is running right now")

        with mock.patch.dict(storage_jobs.WORK, {'use': stops}):
            self.assertEqual(self.run_job('use', how='erase'), 1)
        self.assertIn('The job stopped after the switch', last_result(self.root)['message'])
        self.assertIn('accounts was refused', saved_journal(self.root)['stopped'])
        self.assertEqual(self.finish(), 0)
        done = last_result(self.root)
        self.assertNotIn('power', done['message'].lower())
        self.assertIn('accounts was refused', done['message'])
        self.assertFalse(done['power_cut'])

    def test_a_real_power_cut_still_says_so(self):
        use_journal(self.root, 'd5')
        self.assertEqual(self.finish(), 0)
        self.assertIn('The power went off', last_result(self.root)['message'])
        self.assertTrue(last_result(self.root)['power_cut'])


if __name__ == '__main__':
    unittest.main()
