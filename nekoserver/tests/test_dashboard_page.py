"""dashboard/app.js run in Node against a stand-in page (skipped without Node): what the
page lists as needing attention, how it reads backup checksums and app states."""
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / 'dashboard_harness.mjs'
NODE = shutil.which('node')


def page(*calls):
    """Answers of the page's own functions, one per (name, argument) call."""
    result = subprocess.run([NODE, str(HARNESS), json.dumps(calls)], capture_output=True, text=True,
                            timeout=60, check=True)
    return json.loads(result.stdout)


def status(**parts):
    return {'format': 'neko-dashboard-v1', 'generated_at': time.time(), 'version': 'x',
            'acceptance': {'status': 'complete'}, **parts}


@unittest.skipUnless(NODE, 'node is not installed')
class DashboardPageTests(unittest.TestCase):
    def test_app_states_come_from_health_checks_never_just_running(self):
        cases = [({'state': 'exited'}, 'exited'), ({}, 'unknown'),
                 ({'state': 'running', 'health': 'unhealthy'}, 'unhealthy'),
                 ({'state': 'running', 'health': 'starting'}, 'starting'),
                 ({'state': 'running', 'responding': False}, 'unhealthy'),
                 ({'state': 'running', 'health': 'healthy'}, 'healthy'),
                 ({'state': 'running', 'responding': True}, 'healthy'),
                 ({'state': 'running'}, 'running')]
        answers = page(*[('appState', app) for app, _ in cases])
        self.assertEqual(answers, [want for _, want in cases])

    def test_checksums_match_only_with_a_recent_full_check_and_no_damage(self):
        now = time.time()
        recent = {'id': 'usb', 'full_at': now - 86400}
        answers = page(('filesIntact', []),
                       ('filesIntact', [recent]),
                       ('filesIntact', [{**recent, 'damaged': 2, 'missing': 1}]),
                       ('filesIntact', [recent, {'id': 'nas'}]),
                       ('filesIntact', [{**recent, 'full_at': now - 40 * 86400}]),
                       ('filesIntact', [{**recent, 'unverified': 4}]),
                       ('filesIntact', [{**recent, 'full_at': now + 86400}]))
        self.assertEqual(answers, [['unknown', 'No check yet'], ['pass', 'Checksums match'], ['fail', '3 files'],
                                   ['unknown', 'No full check yet'], ['stale', 'Check overdue'],
                                   ['unknown', '4 not yet checked'], ['unknown', 'No full check yet']])

    def test_issues_list_urgent_first_and_keep_every_server_alert(self):
        [issues] = page(('collectIssues', status(
            alerts=[{'id': 'backup:weekly', 'title': 'Weekly copy failed', 'level': 'warning'},
                    {'id': 'tailscale:login', 'title': 'Signed out', 'level': 'critical'}],
            storage=[{'id': 'media', 'name': 'Media', 'status': 'missing'},
                     {'id': 'ssd', 'name': 'SSD', 'status': 'mounted', 'used_percent': 95}],
            applications=[{'id': 'jellyfin', 'state': 'running', 'health': 'unhealthy'},
                          {'id': 'immich', 'state': 'running', 'responding': True}],
            backups=[{'id': 'music', 'status': 'disabled', 'timer': {'installed': True}},
                     {'id': 'weekly', 'name': 'Weekly', 'status': 'fail'}],
            integrity=[{'id': 'usb', 'name': 'USB', 'damaged': 1}])))
        ids = [i['id'] for i in issues]
        levels = [i['level'] for i in issues]
        self.assertEqual(levels, sorted(levels, key=lambda level: level != 'critical'), issues)
        for wanted in ('tailscale:login', 'mount:media', 'space:ssd', 'app:jellyfin', 'integrity:usb', 'job:weekly'):
            self.assertIn(wanted, ids)
        # A server alert about a backup and the page's own finding are listed once.
        self.assertEqual(ids.count('job:weekly'), 1)
        self.assertEqual(next(i for i in issues if i['id'] == 'job:weekly')['title'], 'Weekly copy failed')
        # A switched-off backup, a working app and a finished server check need nothing.
        self.assertFalse([i for i in ids if 'music' in i or 'immich' in i or i == 'acceptance'], ids)
        self.assertEqual(next(i for i in issues if i['id'] == 'app:jellyfin')['title'], 'Jellyfin · Not responding')

    def test_an_unfinished_server_check_is_listed(self):
        [issues] = page(('collectIssues', status(acceptance={'status': 'pending'})))
        self.assertEqual(issues, [{'id': 'acceptance', 'level': 'warning', 'title': 'Finish checking your server'}])


    def test_the_fixed_speed_slider_starts_at_the_never_slower_than_floor(self):
        # The control service clamps a fixed speed to "Never slower than" (and 20 %), so the
        # slider never offers a speed the fan would not really get.
        floors = page(['fixedFloor', {'min': 35}], ['fixedFloor', {'min': 5}], ['fixedFloor', {}])
        self.assertEqual(floors, [35, 20, 20])
        raised, kept = page(['raiseFixed', {'min': 40, 'fixed': 30}], ['raiseFixed', {'min': 40, 'fixed': 60}])
        self.assertEqual(raised, {'min': 40, 'value': 40, 'fixed': 40})
        self.assertEqual(kept, {'min': 40, 'value': 60, 'fixed': 60})


if __name__ == '__main__':
    unittest.main()
