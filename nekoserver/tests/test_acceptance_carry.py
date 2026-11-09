"""A later acceptance check, such as the one setup runs at the end of every run, keeps the
tests you witnessed yourself (a test restore) for the same profile and settings; and
Nextcloud switched off is not something to accept."""

import json
import subprocess
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


def config(root):
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg.update(data_mount=str(root / 'data'), media_mount=str(root / 'media'), external_backup_mount=str(root / 'usb'))
    cfg['storage'] = {
        'data_ssd': {'uuid': 'ssd-uuid', 'fstype': 'ext4'},
        'media_hdd': {'uuid': 'hdd-uuid', 'fstype': 'ext4'},
    }
    return cfg


def setUpModule():
    """Seen on the rig: a full inspection asked the real Docker, systemd and Tailscale
    (some 70 calls per module). On a server whose Docker could not start each call waited
    out its time limit and the suite took over half an hour; on a running server the
    results read its live state, and the media check's own service was started on it.
    Every command answers as on a PC without them; a test that needs an answer patches
    it itself."""
    absent = subprocess.CompletedProcess([], 1, '', 'not on this test machine')
    for patcher in (
        mock.patch.object(status_checks, 'run', return_value=(None, '')),
        # The media check's own service (it would be started for real) and Tailscale.
        mock.patch.object(subprocess, 'run', return_value=absent),
    ):
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


class CarryTests(unittest.TestCase):
    def test_witnessed_results_are_kept_only_for_the_same_settings(self):
        now = time.time()
        manual = lambda: [
            {'id': 'app_restore', 'status': 'needs_setup'},
            {'id': 'host_restore', 'status': 'needs_setup'},
        ]
        previous = {
            'manual': [
                {'id': 'app_restore', 'status': 'pass', 'evidence': 'user_reported', 'confirmed_at': now - 60},
                {'id': 'host_restore', 'status': 'pass'},
            ]
        }  # no witness record: not kept
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
            with (
                mock.patch.object(acceptance, 'drive_problem', return_value=None),
                mock.patch.object(acceptance, 'job_states', return_value=(None, 'not asked in this test')),
                mock.patch.object(
                    acceptance, 'save', side_effect=lambda c, r: (latest.write_text(json.dumps(r)), saved.append(r))
                ),
            ):
                first = acceptance.inspect(cfg, 'full')
                for check in first['manual']:
                    if check['id'] == 'app_restore':
                        check.update(status='pass', evidence='user_reported', confirmed_at=time.time())
                latest.write_text(json.dumps(first))
                # The real checks, but a wait that never ends (the 0.48.2 mutation pass
                # turned inspect's `break` into `continue`) fails at once instead of
                # hanging the suite; nothing here is starting, so nothing is waited for.
                real_inspect, looks = acceptance.inspect, []

                def look(config, mode):
                    looks.append(mode)
                    if len(looks) > 20:
                        raise AssertionError('first_checkup.inspect kept waiting: more than 20 looks')
                    return real_inspect(config, mode)

                def sleep(seconds):
                    raise AssertionError('first_checkup.inspect waited though nothing was starting')

                with mock.patch.object(acceptance, 'inspect', side_effect=look):
                    first_checkup.inspect(cfg, sleep=sleep)
            after = {c['id']: c['status'] for c in saved[-1]['manual']}
            self.assertEqual(after['app_restore'], 'pass')

    def test_photo_jobs_are_asked_for_only_with_immich(self):
        """0.45.8: with Immich switched off its archive and database jobs are off too, yet
        the check asked for both, so it could never pass."""
        rows = [
            {'id': job, 'name': job, 'status': 'disabled', 'last_success': None,
             'timer': {'installed': False, 'active': False}}
            for job in ('archive', 'database', 'external')
        ]  # fmt: skip

        def asked(immich):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = config(Path(tmp))
                cfg.setdefault('photos', {})['deploy_immich'] = immich
                with (
                    mock.patch.object(acceptance, 'drive_problem', return_value=None),
                    mock.patch.object(acceptance, 'job_states', return_value=({'backups': rows}, None)),
                    mock.patch.object(acceptance, 'save'),
                ):
                    report = acceptance.inspect(cfg, 'full')
            return {c['id'] for c in json.loads(json.dumps(report))['automatic'] if c['id'].startswith('backup:')}

        self.assertNotIn('backup:archive', asked(False))
        self.assertNotIn('backup:database', asked(False))
        self.assertLessEqual({'backup:archive', 'backup:database'}, asked(True))


class ProfileTests(unittest.TestCase):
    def test_the_dashboard_is_told_every_profile_a_run_may_name(self):
        """How a run reads on the dashboard (stale, failed, pending) is tested in the
        collector (collect.rs acceptance_tests). The profiles are setup's: the review of
        0.47.1 found the collector's own list without "accounts"."""
        from modules import dashboard

        cfg = json.loads((ROOT / 'config.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            cfg.update(data_mount=tmp, media_mount=tmp, external_backup_mount=tmp)
            written = dashboard.collector_settings(cfg, 1800)
        self.assertEqual(written['acceptance_profiles'], sorted(cfg['profiles']))
        self.assertIn('accounts', written['acceptance_profiles'])
        # The USB drive now, so a new one's checksum results are its own (collect.rs integrity).
        self.assertIn('external_backup_uuid', written['photos'])


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
