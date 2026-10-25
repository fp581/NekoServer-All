"""A later acceptance check, such as the one setup runs at the end of every run, keeps the
tests you witnessed yourself (a test restore) for the same profile and settings; and
Nextcloud switched off is not something to accept."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance


def config(root):
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg.update(data_mount=str(root / 'data'), media_mount=str(root / 'media'), external_backup_mount=str(root / 'usb'))
    cfg['storage'] = {'data_ssd': {'uuid': 'ssd-uuid', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'hdd-uuid', 'fstype': 'ext4'}}
    return cfg


class CarryTests(unittest.TestCase):
    def test_witnessed_results_are_kept_only_for_the_same_settings(self):
        now = time.time()
        manual = lambda: [{'id': 'app_restore', 'status': 'needs_setup'}, {'id': 'host_restore', 'status': 'needs_setup'}]
        previous = {'manual': [{'id': 'app_restore', 'status': 'pass', 'evidence': 'user_reported', 'confirmed_at': now - 60},
                               {'id': 'host_restore', 'status': 'pass'}]}  # no witness record: not kept
        kept = acceptance.carried(manual(), previous, True, now)
        self.assertEqual([c['status'] for c in kept], ['pass', 'needs_setup'])
        self.assertEqual(kept[0]['evidence'], 'user_reported')
        self.assertEqual([c['status'] for c in acceptance.carried(manual(), previous, False, now)], ['needs_setup'] * 2)

    def test_the_checkup_after_setup_keeps_a_witnessed_restore(self):
        import first_checkup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = config(root)
            latest = root / 'data/reports/acceptance/latest.json'
            latest.parent.mkdir(parents=True)
            saved = []
            with mock.patch.object(acceptance.health, 'mount_info', side_effect=lambda path, uuid, fs: {'status': 'mounted'}), \
                    mock.patch.object(acceptance, 'save', side_effect=lambda c, r: (latest.write_text(json.dumps(r)), saved.append(r))):
                first = acceptance.inspect(cfg, 'full')
                for check in first['manual']:
                    if check['id'] == 'app_restore':
                        check.update(status='pass', evidence='user_reported', confirmed_at=time.time())
                latest.write_text(json.dumps(first))
                first_checkup.inspect(cfg)
            after = {c['id']: c['status'] for c in saved[-1]['manual']}
            self.assertEqual(after['app_restore'], 'pass')


class StaleTests(unittest.TestCase):
    def test_stale_is_a_complete_check_over_a_day_old_never_a_failed_one(self):
        import acceptance_summary
        from neko_common import VERSION
        now = time.time()
        report = {'format': 'neko-acceptance-v1', 'profile': 'full', 'version': VERSION, 'generated_at': now - 2 * 86400,
                  'dashboard_scope': acceptance_summary.scope_digest({}), 'acceptance_complete': True,
                  'automatic': [{'status': 'pass'}],
                  'manual': [{'id': 'host_restore', 'status': 'pass', 'evidence': 'user_reported', 'confirmed_at': now - 2 * 86400}]}
        self.assertEqual(acceptance_summary.summarize(report, {}, now)['status'], 'stale')
        failed = {**report, 'acceptance_complete': False, 'automatic': [{'status': 'fail'}]}
        self.assertEqual(acceptance_summary.summarize(failed, {}, now)['status'], 'failed')
        pending = {**report, 'acceptance_complete': False, 'automatic': [{'status': 'needs_setup'}]}
        self.assertEqual(acceptance_summary.summarize(pending, {}, now)['status'], 'pending')


class NextcloudOffTests(unittest.TestCase):
    def test_nextcloud_switched_off_is_not_accepted(self):
        with mock.patch.object(acceptance, 'unavailable_steps', return_value=set()):
            on = acceptance.accepted_modules({'nextcloud': {'enabled': True}}, ['docker', 'nextcloud'])
            off = acceptance.accepted_modules({'nextcloud': {'enabled': False}}, ['docker', 'nextcloud'])
        self.assertEqual((on, off), (['docker', 'nextcloud'], ['docker']))
        ids = {row['id'] for row in acceptance.manual_tests(off, {'nextcloud': {'enabled': False}})}
        self.assertNotIn('cloud_client', ids)


if __name__ == '__main__':
    unittest.main()
