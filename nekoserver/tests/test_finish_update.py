"""An unfinished monthly update: finished by a later release, and tried again every hour."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer
import neko_common
from modules import maintenance


def config(d):
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg['storage'] = {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                      'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}
    cfg['timezone'] = 'UTC'
    cfg['data_mount'] = str(Path(d) / 'ssd')
    return cfg


class HourlyRetryTests(unittest.TestCase):
    def test_an_unfinished_update_is_tried_again_every_hour_only_while_it_is_there(self):
        units = maintenance.definitions(config('/tmp'), Path('/srv/x/nekoserver-runtime'), Path('/etc/g.json'))
        service = units[maintenance.RETRY_SERVICE]
        self.assertIn(f'ConditionPathExists={neko_common.MAINTENANCE_JOURNAL}\n', service)
        # It never waits for the shared lock (the runtime reads this; maintenance.rs RETRY_ENV).
        self.assertIn('Environment=NEKOSERVER_MAINTENANCE_RETRY=1\n', service)
        self.assertIn('ExecStart=/srv/x/nekoserver-runtime maintenance-resume --config /etc/g.json\n', service)
        self.assertIn('RETRY_ENV: &str = "NEKOSERVER_MAINTENANCE_RETRY"',
                      (ROOT / 'runtime/src/maintenance.rs').read_text())
        timer = units[maintenance.RETRY_TIMER]
        self.assertIn('OnCalendar=*-*-* *:17:00 UTC\n', timer)  # every hour
        self.assertIn('Persistent=false\n', timer)
        source = (ROOT / 'modules/maintenance.py').read_text()
        self.assertIn("['systemctl', 'enable', '--now', RETRY_TIMER]", source)


class FinishUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.journal = self.d / 'maintenance-resume.json'
        self.guard = self.d / 'maintenance.json'
        self.guard.write_text(json.dumps(config(self.d)))
        self.calls = []
        self.written = {}

    def run_finish(self, removes_journal=True, running=False, code=0):
        def run(args, **kwargs):
            self.calls.append(args)
            if args[:2] == ['systemctl', 'start'] and removes_journal:
                self.journal.unlink()
            return mock.Mock(returncode=code if args[:2] == ['systemctl', 'start'] else 0)
        deploy = mock.Mock(return_value=Path('/srv/x/installer/runtime/vNEW/nekoserver-runtime'))
        with mock.patch.object(maintenance, 'verify_storage'), \
                mock.patch.object(maintenance, 'check_units'), \
                mock.patch.object(maintenance, 'write_units', side_effect=self.written.update):
            result = maintenance.finish_update(log=lambda *_: None, run=run, guard=self.guard, journal=self.journal,
                                               deploy=deploy, running=lambda: running)
        return result, deploy

    def test_nothing_to_finish_changes_nothing(self):
        result, deploy = self.run_finish()
        self.assertTrue(result)
        deploy.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_this_release_finishes_an_earlier_releases_update(self):
        self.journal.write_text(json.dumps({'version': '0.39.4', 'phase': 'restoring'}))
        result, deploy = self.run_finish()
        self.assertTrue(result)
        deploy.assert_called_once()
        # Only the services that finish the update are pointed at the new program.
        self.assertEqual(set(self.written), set(maintenance.FINISHING))
        self.assertIn('/srv/x/installer/runtime/vNEW/nekoserver-runtime maintenance-resume',
                      self.written[maintenance.RESUME_SERVICE])
        self.assertNotIn('nekoserver-maintenance.service', self.written)
        self.assertIn(['systemctl', 'start', maintenance.RESUME_SERVICE], self.calls)

    def test_a_failed_finish_says_where_to_look_and_a_running_one_is_left_alone(self):
        self.journal.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'journalctl -u nekoserver-maintenance-resume'):
            self.run_finish(removes_journal=False, code=1)
        with self.assertRaisesRegex(RuntimeError, 'running right now'):
            self.run_finish(running=True)

    def test_installer_runs_only_the_finish(self):
        with mock.patch.object(installer.os, 'geteuid', return_value=0), \
                mock.patch.object(maintenance, 'finish_update') as finish, \
                mock.patch.object(installer, 'settings_for') as settings, \
                mock.patch.object(installer, 'execute') as execute:
            self.assertEqual(installer.main(['--finish-update']), 0)
        finish.assert_called_once()
        settings.assert_not_called()
        execute.assert_not_called()

    def test_setup_names_the_finish_when_an_update_is_unfinished(self):
        self.journal.write_text(json.dumps({'version': '0.39.4'}))
        message = neko_common.pending_maintenance(self.journal, running=lambda: False)
        self.assertIn('sudo bash install.sh --finish-update', message)
        self.assertIn('sudo systemctl start nekoserver-maintenance-resume.service', message)


if __name__ == '__main__':
    unittest.main()
