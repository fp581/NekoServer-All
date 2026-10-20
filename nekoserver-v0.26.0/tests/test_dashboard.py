"""The dashboard: its files, the server that serves them, and how it is installed."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules import dashboard, tv  # noqa: E402

SERVE = (ROOT / 'runtime/src/serve.rs').read_text()
APP = (ROOT / 'dashboard/app.js').read_text()
PAGE = (ROOT / 'dashboard/index.html').read_text()


def served_files():
    block = SERVE[SERVE.index('const ASSETS'):SERVE.index('];', SERVE.index('const ASSETS'))]
    return {m.group(1) for m in re.finditer(r'\(\s*"/[^"]*",\s*"([^"]+)"', block)}


def server_codes():
    block = SERVE[SERVE.index('pub const ERRORS'):SERVE.index('];', SERVE.index('pub const ERRORS'))]
    return set(re.findall(r'\("(E\d\d)"', block))


class DashboardFilesTests(unittest.TestCase):
    def test_the_installer_copies_exactly_what_the_server_serves(self):
        self.assertEqual(set(dashboard.ASSETS), served_files())
        for name in dashboard.ASSETS:
            self.assertTrue((ROOT / 'dashboard' / name).is_file(), name)

    def test_every_element_the_script_uses_is_on_the_page(self):
        ids = set(re.findall(r'\$\(\'([a-z0-9-]+)\'\)', APP)) | set(re.findall(r"put\('([a-z0-9-]+)'", APP))
        ids |= set(re.findall(r"setBadge\('([a-z0-9-]+)'", APP))
        missing = {i for i in ids if f'id="{i}"' not in PAGE}
        self.assertEqual(missing, set())

    def test_the_page_loads_nothing_from_elsewhere(self):
        # The server's Content-Security-Policy allows only its own files.
        for ref in re.findall(r'(?:src|href)="([^"#]+)"', PAGE):
            self.assertFalse(ref.startswith(('http:', 'https:', '//')), ref)
        self.assertNotIn('<script>', PAGE)
        self.assertNotIn(' style="', PAGE)

    def test_error_codes_are_explained_everywhere(self):
        page_codes = set(re.findall(r'^\s+(E\d\d): \[', APP, re.M))
        self.assertLessEqual(server_codes(), page_codes)
        guide = (ROOT / 'DASHBOARD.md').read_text()
        for code in page_codes:
            self.assertIn('| ' + code + ' |', guide, code)
        rows = re.findall(r"^\s+(E\d\d): \['([^']+)', '([^']+)'\]", APP, re.M)
        self.assertEqual({r[0] for r in rows}, page_codes)
        for code, title, fix in rows:
            self.assertLessEqual(len(title.split()), 7, code)  # short enough to read at a glance
            self.assertLessEqual(len(fix), 110, code)

    def test_the_app_manifest_points_at_real_icons(self):
        import json
        manifest = json.loads((ROOT / 'dashboard/manifest.webmanifest').read_text())
        self.assertEqual(manifest['display'], 'standalone')
        for icon in manifest['icons']:
            self.assertIn(icon['src'], dashboard.ASSETS)
        worker = (ROOT / 'dashboard/sw.js').read_text()
        self.assertIn("url.pathname.startsWith('/api/')", worker)  # readings are never cached
        for name in re.findall(r"'/([a-z0-9.-]+)'", worker.split('const SHELL')[1].split(';')[0]):
            self.assertIn(name, dashboard.ASSETS)


class ReadingFreshnessTests(unittest.TestCase):
    def test_late_readings_use_the_configured_interval_everywhere(self):
        import neko_common
        self.assertEqual(neko_common.reading_max_age(60), 150)
        self.assertEqual(neko_common.reading_max_age(300), 630)
        self.assertEqual(neko_common.reading_max_age('x'), 150)
        # The page uses the same rule (statusStale in app.js).
        self.assertIn('return 2 * interval + 30;', APP)
        settings = dashboard.collector_settings(
            {'data_mount': '/srv', 'media_mount': '/m', 'external_backup_mount': '/u', 'storage': {}, 'media': {'services': []}},
            1800, 300)
        self.assertEqual(settings['dashboard']['interval_seconds'], 300)
        self.assertIn('"collect_interval"', (ROOT / 'runtime/src/collect.rs').read_text())

    def test_the_acceptance_check_accepts_a_five_minute_schedule(self):
        import io
        import json
        import time
        from unittest import mock
        import acceptance
        now = time.time()

        def answer(age, interval):
            body = json.dumps({'format': 'neko-dashboard-v1', 'generated_at': now - age, 'collect_interval': interval}).encode()
            response = mock.MagicMock(status=200)
            response.read = io.BytesIO(body).read
            response.__enter__.return_value = response
            return response
        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 300)):
            self.assertTrue(acceptance.dashboard_check(8787, now))
        with mock.patch.object(acceptance.OPENER, 'open', return_value=answer(200, 60)):
            self.assertFalse(acceptance.dashboard_check(8787, now))


class DashboardInstallTests(unittest.TestCase):
    def test_the_web_server_starts_with_the_computer_and_comes_back(self):
        units = dashboard.units(Path('/srv/nekoserver'), Path('/usr/local/lib/x'), Path('/etc/x.json'), 8787, 60)
        service = units[dashboard.SERVICE]
        self.assertIn('Restart=always', service)
        self.assertIn('StartLimitIntervalSec=0', service)
        self.assertIn('WantedBy=multi-user.target', service)
        self.assertIn('DynamicUser=yes', service)  # never root
        self.assertIn('OnBootSec=', units['nekoserver-dashboard-collect.timer'])
        self.assertIn('WantedBy=timers.target', units['nekoserver-dashboard-collect.timer'])

    def test_the_launcher_opens_an_app_window_and_falls_back(self):
        text = dashboard.launcher(8787)
        self.assertIn('--app="$url"', text)
        self.assertIn('url=http://127.0.0.1:8787/', text)
        self.assertTrue(text.rstrip().endswith('exec xdg-open "$url"'))
        entry = dashboard.desktop_entry(Path('/usr/local/lib/nekoserver-dashboard/v1'))
        self.assertIn('Exec=' + str(dashboard.LAUNCHER), entry)
        self.assertIn('icon-512.png', entry)

    def test_a_launcher_someone_else_wrote_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            mine, theirs = Path(d) / 'mine', Path(d) / 'theirs'
            theirs.write_text('#!/bin/sh\necho custom\n')
            logs = []
            self.assertFalse(dashboard.write_managed(theirs, dashboard.launcher(8787), 0o755, logs.append))
            self.assertEqual(theirs.read_text(), '#!/bin/sh\necho custom\n')
            self.assertTrue(dashboard.write_managed(mine, dashboard.launcher(8787), 0o755, logs.append))
            self.assertEqual(os.stat(mine).st_mode & 0o777, 0o755)
            # Ours is replaced on the next release (a new port, say).
            self.assertTrue(dashboard.write_managed(mine, dashboard.launcher(9000), 0o755, logs.append))
            self.assertIn('9000', mine.read_text())

    def test_the_tv_shortcut_uses_the_launcher_once_it_exists(self):
        with tempfile.TemporaryDirectory() as d:
            launcher = Path(d) / 'nekoserver-dashboard'
            from unittest import mock
            with mock.patch.object(dashboard, 'LAUNCHER', launcher):
                self.assertIn('firefox', tv.launchers({})['nekoserver-dashboard.desktop'])
                launcher.write_text(dashboard.launcher(8787))
                self.assertIn('Exec=' + str(launcher), tv.launchers({})['nekoserver-dashboard.desktop'])


if __name__ == '__main__':
    unittest.main()
