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


def fresh(report):
    """The report as a check that ran just now writes it."""
    import datetime

    return {**report, 'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}


class AcceptanceMediaTests(unittest.TestCase):
    def run_with(self, report, installed=True, start=0, said=''):
        ran = []

        def run(argv, **kw):
            ran.append(argv)
            if argv[1] == 'start':
                return mock.Mock(returncode=start, stderr=said, stdout='')
            return mock.Mock(returncode=0 if installed else 1)

        with tempfile.TemporaryDirectory() as d:
            if report is not None:
                (Path(d) / 'reports/media').mkdir(parents=True)
                (Path(d) / 'reports/media/latest.json').write_text(json.dumps(report))
            with mock.patch.object(acceptance, 'mount_path', return_value=Path(d)):
                return acceptance.media_checks({'data_mount': d}, run=run), ran

    def test_the_runtimes_check_is_run_and_its_report_read(self):
        report = {'services': {'sonarr': {'result': 'pass'}}, 'integrations': {'prowlarr': {'result': 'needs_setup'}}}
        checks, ran = self.run_with(fresh(report))
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

    def test_an_old_report_is_never_shown_as_this_checks_result(self):
        """The review of 0.47.10: acceptance started the check, ignored how it ended and
        read the report there; a check that stopped before writing left the last PASS."""
        old = {'generated_at': '2026-11-01T03:00:00+00:00', 'services': {'sonarr': {'result': 'pass'}}}
        # The start failed (the check refused its drive) and the old PASS report is there.
        checks, _ = self.run_with(old, start=1, said='Job for nekoserver-media-health.service failed.')
        self.assertEqual([(c['id'], c['status']) for c in checks], [('media:audit', 'fail')])
        self.assertIn('did not write a new report', checks[0]['detail'])
        self.assertIn('Job for nekoserver-media-health.service failed.', checks[0]['detail'])
        # The start went well but left the old report (nothing new written): not current either.
        checks, _ = self.run_with(old)
        self.assertEqual([(c['id'], c['status']) for c in checks], [('media:audit', 'fail')])
        # The runtime's own way of writing the time (nanoseconds) is read.
        now = acceptance.time.time()
        self.assertTrue(acceptance.written_since({'generated_at': '2099-01-01T00:00:00.123456789+00:00'}, now))
        self.assertFalse(acceptance.written_since({'generated_at': '2020-01-01T00:00:00.123456789+00:00'}, now))
        # A report without its time is no proof of a run.
        checks, _ = self.run_with({'services': {'sonarr': {'result': 'pass'}}})
        self.assertEqual([c['status'] for c in checks], ['fail'])
        # A check that found a failing row ends with an error after writing: its rows count.
        checks, _ = self.run_with(fresh({'services': {'sonarr': {'result': 'fail'}}}), start=1)
        self.assertEqual([(c['id'], c['status']) for c in checks], [('media:services:sonarr', 'fail')])


if __name__ == '__main__':
    unittest.main()
