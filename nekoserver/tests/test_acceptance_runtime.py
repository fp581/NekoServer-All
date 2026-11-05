"""The acceptance check reads what the runtime decides (the review of 0.47.1): the drive
check every job makes, and the backups', the outage monitor's and the phone alerts'
states as the dashboard shows them. It kept copies of both, which already disagreed."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance  # noqa: E402
import status_checks  # noqa: E402

TIMER = {'installed': True, 'active': True}


def config(root):
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg.update(data_mount=str(root / 'data'), media_mount=str(root / 'media'), external_backup_mount=str(root / 'usb'))
    cfg['storage'] = {
        'data_ssd': {'uuid': 'ssd-uuid', 'fstype': 'ext4'},
        'media_hdd': {'uuid': 'hdd-uuid', 'fstype': 'ext4'},
    }
    cfg.setdefault('photos', {})['deploy_immich'] = True
    return cfg


def inspect(states=None, problem=lambda keys: None):
    """The automatic checks of a full inspection, by id, with the runtime's answers."""
    with tempfile.TemporaryDirectory() as tmp:
        with (
            mock.patch.object(acceptance, 'drive_problem', side_effect=lambda cfg, keys: problem(keys)),
            mock.patch.object(acceptance, 'job_states', return_value=(states, None if states else 'no runtime')),
            mock.patch.object(acceptance, 'save'),
        ):
            report = acceptance.inspect(config(Path(tmp)), 'full')
    return {c['id']: c for c in json.loads(json.dumps(report))['automatic']}


class JobStatesTests(unittest.TestCase):
    def test_a_backup_the_runtime_calls_failed_fails_here_too(self):
        """A worker systemd could not start writes no report, so its last report still
        says "pass": the runtime marks it failed, and the old copy here passed it."""
        now = time.time()
        rows = [
            {
                'id': 'applications',
                'name': 'App snapshot',
                'status': 'fail',
                'last_success': now - 3600,
                'timer': TIMER,
            },
            {'id': 'host', 'name': 'Host snapshot', 'status': 'pass', 'last_success': now - 3600, 'timer': TIMER},
        ]
        checks = inspect({'backups': rows})
        self.assertEqual(checks['backup:applications']['status'], 'fail')
        self.assertEqual(checks['backup:host']['status'], 'pass')

    def test_the_outage_monitor_and_phone_alerts_are_the_runtimes_answer(self):
        now = time.time()
        states = {
            'backups': [],
            'heartbeat': {'status': 'stale', 'last_success': now - 9000, 'timer': TIMER},
            'notifications': {'status': 'pass', 'last_success': now - 60, 'timer': TIMER},
        }
        checks = inspect(states)
        self.assertEqual(checks['heartbeat:delivery']['status'], 'fail')
        self.assertEqual(checks['notifications:delivery']['status'], 'pass')
        self.assertEqual(checks['notifications:timer']['status'], 'pass')

    def test_without_the_runtimes_answer_no_backup_passes(self):
        checks = inspect(None)
        self.assertEqual(checks['backup:states']['status'], 'blocked')
        self.assertFalse(any(c['status'] == 'pass' for k, c in checks.items() if k.startswith('backup:')))

    def test_the_runtime_is_asked_and_its_line_of_json_read(self):
        said = []

        def ask(project, cfg, args):
            said.append(args)
            assert Path(args[args.index('--config') + 1]).is_file()
            return 0, 'a warning on stderr\n{"backups": [], "heartbeat": {}}\nanother line'

        states, why = acceptance.job_states({'data_mount': '/srv/x'}, ask=ask)
        self.assertEqual(said[0][0], 'job-states')
        self.assertEqual((states, why), ({'backups': [], 'heartbeat': {}}, None))
        refused = acceptance.job_states({}, ask=lambda *a: (1, 'NekoServer runtime failed: Run it as root'))
        self.assertEqual(refused, (None, 'Run it as root'))

    def test_setup_keeps_no_copy_of_the_job_rules(self):
        for name in ('JOBS', 'backups', 'job_row', 'retired', 'heartbeat_status', 'notification_status'):
            self.assertFalse(hasattr(status_checks, name), name)


class DriveCheckTests(unittest.TestCase):
    def test_no_backup_report_is_read_past_a_refused_drive(self):
        """The review of 0.47.2: the SSD's identity was refused, yet its backup reports
        were read and passed."""
        now = time.time()
        rows = [{'id': 'host', 'name': 'Host snapshot', 'status': 'pass', 'last_success': now - 60, 'timer': TIMER}]
        refused = 'Drive check failed: /srv/nekoserver is mounted with another Btrfs subvolume'
        checks = inspect({'backups': rows}, problem=lambda keys: refused if 'data_ssd' in keys else None)
        self.assertEqual(checks['mount:data_ssd']['status'], 'fail')
        self.assertNotEqual(checks['backup:host']['status'] if 'backup:host' in checks else None, 'pass')
        self.assertEqual(checks['backup:states']['status'], 'blocked')
        self.assertIn('drive verification', checks['backup:states']['detail'])
        # The runtime itself reads them only from drives that pass the check every job makes.
        source = (ROOT / 'runtime/src/collect.rs').read_text()
        states = source[source.index('pub fn job_states(') : source.index('pub fn run(a: &Args)')]
        self.assertIn('crate::backup::checked_drive(', states)
        self.assertNotIn('mount_info(', states)

    def test_a_drive_the_runtime_refuses_is_not_passed(self):
        """A copied LUN has the right UUID and filesystem: the old check here passed it,
        and every backup refused it."""
        nas = 'Drive check failed: /mnt/m has the right UUID but is on another NAS disk (0x2, not 0x1)'
        checks = inspect({'backups': []}, problem=lambda keys: nas if keys == ('media_hdd',) else None)
        self.assertEqual(checks['mount:data_ssd']['status'], 'pass')
        self.assertEqual(checks['mount:media_hdd']['status'], 'fail')
        self.assertIn('another NAS disk', checks['mount:media_hdd']['detail'])
        # Nothing private is read past a drive that is not the right one.
        self.assertEqual(checks['bundle']['status'], 'blocked')

    def test_two_drives_that_are_one_filesystem_fail_together(self):
        both = 'The SSD and the HDD must be two different filesystems'
        checks = inspect({'backups': []}, problem=lambda keys: both if len(keys) == 2 else None)
        self.assertEqual(checks['mount:distinct']['status'], 'fail')
        self.assertEqual(checks['mount:distinct']['detail'], both)

    def test_the_usb_drive_is_the_nas_disk_setup_chose(self):
        """The review of 0.47.2's sweep: the USB backup drive passed on its UUID alone, so a
        restored or copied NAS LUN mounted in its place passed too."""
        other = 'Drive check failed: /mnt/usb has the right UUID but is on another NAS disk (0x2, not 0x1)'
        with mock.patch.object(acceptance.health, 'mount_info', return_value={'status': 'mounted'}):
            checks = inspect({'backups': []}, problem=lambda keys: other if keys == ('usb',) else None)
        self.assertEqual(checks['external:identity']['status'], 'fail')
        self.assertIn('another NAS disk', checks['external:identity']['detail'])
        with mock.patch.object(acceptance.health, 'mount_info', return_value={'status': 'mounted'}):
            self.assertEqual(inspect({'backups': []})['external:identity']['status'], 'pass')

    def test_the_drive_check_is_given_the_usb_drives_settings(self):
        import neko_common

        asked = []
        cfg = {'data_mount': '/srv/x', 'media_mount': '/mnt/m', 'external_backup_mount': '/mnt/usb', 'storage': {},
               'photos': {'external_backup_uuid': 'u1', 'external_backup_fstype': 'exfat',
                          'external_backup_network': {'wwn': '0x1'}, 'deploy_immich': True}}  # fmt: skip

        def ask(project, config, args):
            asked.append(json.loads(Path(args[args.index('--config') + 1]).read_text()))
            return 0, 'ok'

        neko_common.verify_storage(cfg, ('usb',), ask=ask)
        self.assertEqual(asked[0]['external_backup_mount'], '/mnt/usb')
        self.assertEqual(asked[0]['photos']['external_backup_network'], {'wwn': '0x1'})
        self.assertNotIn('deploy_immich', asked[0]['photos'])

    def test_the_runtimes_drive_check_is_the_one_asked(self):
        with mock.patch.object(acceptance, 'verify_storage', side_effect=RuntimeError('wrong subvolume')) as asked:
            self.assertEqual(acceptance.drive_problem({}, ('data_ssd',)), 'wrong subvolume')
        asked.assert_called_once_with({}, ('data_ssd',))


if __name__ == '__main__':
    unittest.main()
