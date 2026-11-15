"""Deep checks 611-630: the dashboard in the states a real server is in, as the real
collector describes them (each case below is the shape it wrote in the lab: loop drives,
stand-in systemctl/docker/smartctl), read by the page's own code in Node."""

import shutil
import unittest

from test_dashboard_page import ROOT, page, status

NODE = shutil.which('node')
APPS = [('jellyfin', 'Jellyfin'), ('immich', 'Immich'), ('seerr', 'Seerr')]


@unittest.skipUnless(NODE, 'node is not installed')
class RealStatesTests(unittest.TestCase):
    def test_apps_down_with_docker_read_docker_stopped(self):
        # Deep check 615: with Docker stopped every app card read "Not checked yet".
        app = {'id': 'jellyfin', 'name': 'Jellyfin', 'state': 'docker_down', 'health': 'unknown', 'held': True}
        self.assertEqual(page(('appMenu', app)), ['Docker stopped'])
        apps = [{'id': i, 'name': n, 'state': 'docker_down', 'health': 'unknown', 'held': True} for i, n in APPS]
        alert = {'id': 'startup:docker.service:failed', 'level': 'critical', 'title': 'Apps (Docker) has stopped'}
        issues = page(('collectIssues', status(applications=apps, alerts=[alert])))[0]
        self.assertEqual([i['id'] for i in issues], ['startup:docker.service:failed'])

    def test_a_failing_drive_opens_the_drive_health_guide(self):
        # Deep check 618: a failing drive opened "Check a drive or free up space".
        smart = {'status': 'fail', 'reason': 'Pending or uncorrectable sectors'}
        drive = {'id': 'media_hdd', 'name': 'Media HDD', 'status': 'mounted', 'smart': smart}
        alert = {'id': 'smart:media_hdd', 'level': 'critical', 'title': 'Media HDD is failing', 'detail': 'x'}
        for alerts in ([alert], []):  # the collector's alert, and the page's own
            with self.subTest(alerts=bool(alerts)):
                issues = page(('issueDetails', status(storage=[drive], alerts=alerts)))[0]
                self.assertEqual([(i['id'], i['guide']) for i in issues], [('smart:media_hdd', 'health')])
        source = (ROOT / 'dashboard/app.js').read_text()
        guide = source.split('  health: {')[1].split('\n  },')[0]
        for words in ('migrate.py list', 'Replace the OS and reattach the existing store', 'Backups'):
            self.assertIn(words, guide)


if __name__ == '__main__':
    unittest.main()
