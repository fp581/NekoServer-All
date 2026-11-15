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


if __name__ == '__main__':
    unittest.main()
