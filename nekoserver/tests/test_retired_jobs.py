"""The resume services of the snapshots wait long enough for the recovery lock. (Which
backups are switched off or overdue is the runtime's to say, tested there: collect.rs
jobs_of_a_switched_off_app_are_retired_not_overdue; setup's copy went in 0.47.2.)"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))



class ResumeTimeoutTests(unittest.TestCase):
    """A snapshot's recovery may wait 3 hours for the shared recovery lock: systemd gives
    its resume service, and its ExecStopPost, at least that long."""

    def test_resume_services_allow_for_the_recovery_lock_wait(self):
        from modules import app_backup, host_backup
        from neko_common import RESUME_TIMEOUT

        cfg = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/m', 'timezone': 'UTC'}
        with (
            mock.patch.object(app_backup, 'host_timezone', return_value='UTC'),
            mock.patch.object(host_backup, 'host_timezone', return_value='UTC'),
            mock.patch.object(app_backup, 'keep_snapshots'),
        ):
            units = {
                **app_backup.definitions(cfg, '/bin/r', '/etc/g.json'),
                **host_backup.definitions(cfg, '/bin/r', '/etc/g.json'),
            }
        self.assertEqual(RESUME_TIMEOUT, '3h30min')
        for name in ('nekoserver-app-resume.service', host_backup.RESUME_UNIT):
            self.assertIn('TimeoutStartSec=' + RESUME_TIMEOUT, units[name], name)
        for name in ('nekoserver-app-backup.service', host_backup.BACKUP_UNIT):
            self.assertIn('TimeoutStopSec=' + RESUME_TIMEOUT, units[name], name)


if __name__ == '__main__':
    unittest.main()
