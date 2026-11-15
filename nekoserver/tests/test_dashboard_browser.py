"""The dashboard in a real browser: the runtime's own web server (runtime/bin) serves
dashboard/ and a stand-in snapshot, and Chromium (Playwright) opens it. Skipped where
Node, Playwright, Chromium or a built runtime is missing, and as root (the web server
refuses to run as root)."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path(__file__).resolve().parent / 'dashboard_browser.mjs'
RUNTIME = ROOT / 'runtime/bin/nekoserver-runtime'
NODE = shutil.which('node')


def playwright_works():
    if not NODE or not RUNTIME.is_file() or os.geteuid() == 0:
        return False
    probe = "require('playwright').chromium.launch().then(b => b.close())"
    try:
        return subprocess.run([NODE, '-e', probe], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def snapshot():
    """A server with ten apps, two drives (one nearly full, with a SMART warning) and an
    app update waiting: enough for every page to have its cards and buttons."""
    now = time.time()
    names = ['Jellyfin', 'Immich', 'Navidrome', 'Nextcloud', 'Seerr', 'Sonarr', 'Radarr', 'Lidarr', 'Prowlarr']
    apps = [{'id': n.lower(), 'name': n, 'state': 'running', 'responding': True} for n in names]
    return {
        'format': 'neko-dashboard-v1',
        'generated_at': now,
        'late_after': 630,
        'version': '0.51.2',
        'acceptance': {'status': 'complete'},
        'applications': apps,
        'storage': [
            {'id': 'data_ssd', 'name': 'Application SSD', 'status': 'mounted', 'total': 1e12, 'used': 4e11},
            {
                'id': 'media_hdd',
                'name': 'Media HDD',
                'status': 'mounted',
                'total': 8e12,
                'used': 7.7e12,
                'used_percent': 96,
                'smart': {'status': 'warning', 'reason': 'Reallocated sectors: 8'},
            },
        ],
        'heartbeat': {'status': 'pass'},
        'notifications': {'status': 'pass'},
        'startup': [{'unit': 'docker.service', 'name': 'Docker', 'enabled': False}],
        'waiting_updates': {
            'settle_days': 7,
            'apps': [
                {
                    'app': 'jellyfin',
                    'installed': '10.11.1',
                    'waiting': [
                        {'version': '10.11.2', 'first_seen': now - 9 * 86400, 'installs_from': now - 2 * 86400},
                        {'version': '10.11.3', 'first_seen': now - 86400, 'installs_from': now + 6 * 86400},
                    ],
                }
            ],
        },
    }


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class DashboardInBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        status = Path(cls.folder.name) / 'status.json'
        status.write_text(json.dumps(snapshot()))
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        command = [str(RUNTIME), 'serve', '--assets', str(ROOT / 'dashboard'), '--status', str(status)]
        command += ['--port', str(port), '--control', str(Path(cls.folder.name) / 'no-control.sock')]
        cls.server = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.address = f'http://127.0.0.1:{port}/'
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.folder.cleanup()

    def browse(self, *checks):
        result = subprocess.run(
            [NODE, str(BROWSER), self.address, json.dumps(checks)],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        return json.loads(result.stdout)

    def test_keyboard_focus_stays_put_when_the_page_refreshes(self):
        # Deep check 156: the page redraws itself every few seconds; a keyboard user who
        # had Tabbed to an app's ⋯ (Your apps, every 3 s) or an update's button (Updates,
        # every 15 s) was sent back to the top of the page each time.
        checks = [
            ('apps', 'Actions for Sonarr', 'control'),
            ('home', 'Actions for Immich', 'control'),
            ('updates', 'Install at the next update night', 'status'),
            ('updates', 'Check for updates', 'status'),
        ]
        answers = self.browse(*[('focusAfterRefresh', {'route': r, 'label': l, 'reload': k}) for r, l, k in checks])
        for (route, label, _), answer in zip(checks, answers):
            with self.subTest(route=route, label=label):
                self.assertEqual(answer, {'before': label, 'after': label})

    def test_a_phone_at_200_percent_zoom_needs_no_sideways_scrolling(self):
        # Deep check 170: a 390 px phone at 200% zoom is 195 CSS pixels wide. The cards'
        # badges ("Won't start after a restart", "Not checked yet") stuck out of their
        # cards, and the page scrolled sideways (Server health 281 px, Backups 246 px).
        routes = ['home', 'apps', 'backups', 'hardware', 'sensors', 'cooling', 'processes', 'health', 'updates']
        routes += ['attention', 'help']
        for width in (390, 320, 195):
            answers = self.browse(*[('pageWidth', {'route': r, 'width': width}) for r in routes])
            for route, (scroll, window, overlap) in zip(routes, answers):
                with self.subTest(route=route, width=width):
                    self.assertLessEqual(scroll, window)
                    self.assertFalse(overlap, "the title of the page is under its buttons")
