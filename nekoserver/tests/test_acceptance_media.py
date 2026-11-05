"""Acceptance reads the media apps' configuration from the runtime's own check (media.rs,
the one the dashboard shows), not from a check of its own (the review of 0.47.0)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance  # noqa: E402


class AcceptanceMediaTests(unittest.TestCase):
    def run_with(self, report, installed=True):
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            return mock.Mock(returncode=0 if installed or argv[1] != 'cat' else 1)

        with tempfile.TemporaryDirectory() as d:
            if report is not None:
                (Path(d) / 'reports/media').mkdir(parents=True)
                (Path(d) / 'reports/media/latest.json').write_text(json.dumps(report))
            with mock.patch.object(acceptance, 'mount_path', return_value=Path(d)):
                return acceptance.media_checks({'data_mount': d}, run=run), ran

    def test_the_runtimes_check_is_run_and_its_report_read(self):
        report = {'services': {'sonarr': {'result': 'pass'}}, 'integrations': {'prowlarr': {'result': 'needs_setup'}}}
        checks, ran = self.run_with(report)
        self.assertIn(['systemctl', 'start', acceptance.MEDIA_CHECK], ran)
        self.assertEqual(
            sorted((c['id'], c['status']) for c in checks),
            [('media:integrations:prowlarr', 'needs_setup'), ('media:services:sonarr', 'pass')],
        )

    def test_no_check_or_no_report_is_said(self):
        checks, ran = self.run_with(None, installed=False)
        self.assertEqual([c['status'] for c in checks], ['needs_setup'])
        self.assertNotIn(['systemctl', 'start', acceptance.MEDIA_CHECK], ran)
        checks, _ = self.run_with(None)
        self.assertEqual([c['status'] for c in checks], ['fail'])


if __name__ == '__main__':
    unittest.main()
