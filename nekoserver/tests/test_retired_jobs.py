"""A backup whose app is switched off (Immich, say) is shown as switched off, never as
overdue; a schedule that stops while its app is still set up is still flagged
(collect.rs has the same rules: jobs_of_a_switched_off_app_are_retired_not_overdue)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status_checks  # noqa: E402

NOW = 2_000_000_000.0


class RetiredJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        jobs = self.data / 'reports/jobs'
        jobs.mkdir(parents=True)
        old = {'status': 'pass', 'last_success': NOW - 30 * 86400}
        for name in ('database', 'archive', 'external', 'host'):
            (jobs / (name + '.json')).write_text(json.dumps(old))
        self.timers = {}

    def rows(self, config):
        def systemctl(args, timeout=8):
            state = self.timers.get(args[2], 'not-found')
            return 0, ('LoadState=loaded\nActiveState=' + state if state != 'not-found' else 'LoadState=not-found\nActiveState=inactive')
        with mock.patch.object(status_checks, 'run', side_effect=systemctl):
            return {r['id']: r for r in status_checks.backups({'data_mount': str(self.data), **config}, NOW, True)}

    def test_immich_switched_off_shows_its_jobs_as_switched_off(self):
        rows = self.rows({'photos': {'deploy_immich': False}, 'nextcloud': {'enabled': True}})
        for key in ('archive', 'database'):
            self.assertEqual(rows[key]['status'], 'disabled', key)
            self.assertIn('Immich is not set up', rows[key]['reason'])
        self.assertEqual(rows['external']['status'], 'stale')       # Nextcloud still wants it
        self.assertEqual(rows['host']['status'], 'stale')

    def test_music_off_is_switched_off_not_left_out(self):
        rows = self.rows({'photos': {'deploy_immich': True}, 'music': {'backup_enabled': False}})
        self.assertEqual(rows['music']['status'], 'disabled')

    def test_a_wanted_schedule_switched_off_or_gone_is_flagged(self):
        jobs = self.data / 'reports/jobs'
        (jobs / 'host.json').write_text(json.dumps({'status': 'pass', 'last_success': NOW - 3600}))
        (jobs / 'external.json').write_text(json.dumps({'status': 'pass', 'last_success': NOW - 3600}))
        self.timers['nekoserver-host-backup.timer'] = 'inactive'
        rows = self.rows({'photos': {'deploy_immich': True}})
        self.assertEqual(rows['host']['status'], 'stale')
        self.assertIn('switch its schedule back on', rows['host']['reason'])
        self.assertEqual(rows['external']['status'], 'stale')      # ran before, timer gone
        self.assertIn('schedule is gone', rows['external']['reason'])
        self.assertEqual(rows['applications']['status'], 'unknown')   # never set up: no evidence

    def test_without_systemd_there_is_no_evidence(self):
        (self.data / 'reports/jobs/host.json').write_text(json.dumps({'status': 'pass', 'last_success': NOW - 3600}))
        with mock.patch.object(status_checks, 'run', return_value=(None, '')):
            rows = {r['id']: r for r in status_checks.backups({'data_mount': str(self.data)}, NOW, True)}
        self.assertEqual(rows['host']['status'], 'pass')


class ResumeTimeoutTests(unittest.TestCase):
    """A snapshot's recovery may wait 3 hours for the shared recovery lock: systemd gives
    its resume service, and its ExecStopPost, at least that long."""
    def test_resume_services_allow_for_the_recovery_lock_wait(self):
        from modules import app_backup, host_backup
        from neko_common import RESUME_TIMEOUT
        cfg = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/m', 'timezone': 'UTC'}
        with mock.patch.object(app_backup, 'host_timezone', return_value='UTC'), \
                mock.patch.object(host_backup, 'host_timezone', return_value='UTC'), \
                mock.patch.object(app_backup, 'keep_snapshots'):
            units = {**app_backup.definitions(cfg, '/bin/r', '/etc/g.json'), **host_backup.definitions(cfg, '/bin/r', '/etc/g.json')}
        self.assertEqual(RESUME_TIMEOUT, '3h30min')
        for name in ('nekoserver-app-resume.service', host_backup.RESUME_UNIT):
            self.assertIn('TimeoutStartSec=' + RESUME_TIMEOUT, units[name], name)
        for name in ('nekoserver-app-backup.service', host_backup.BACKUP_UNIT):
            self.assertIn('TimeoutStopSec=' + RESUME_TIMEOUT, units[name], name)


if __name__ == '__main__':
    unittest.main()
