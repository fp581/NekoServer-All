"""Deep check plan 283: gateway_cert.py caught every error and said only "Certificate
export failed; verify mounts, CA and a new destination", hiding its own plain reasons
(a settings file missing a line, a destination that already exists or is in /etc)."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def exported(settings, destination):
    return subprocess.run(
        [sys.executable, str(ROOT / 'gateway_cert.py'), '--config', str(settings), '--export', str(destination)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class GatewayCertTests(unittest.TestCase):
    def test_a_failed_export_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            settings.write_text(json.dumps({'data_mount': f'{tmp}/ssd', 'media_mount': f'{tmp}/hdd'}))
            done = exported(settings, Path(tmp) / 'root.crt')
            self.assertEqual(done.returncode, 1)
            self.assertIn('external_backup_mount', done.stderr)
            self.assertIn('No private key is exported', done.stderr)
            self.assertNotIn('Traceback', done.stderr)

    def test_a_destination_it_refuses_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = exported(Path(tmp) / 'settings.txt', Path(tmp) / 'root.crt')
            self.assertIn('--config names a settings file', done.stderr)
            self.assertNotIn('Traceback', done.stderr)


if __name__ == '__main__':
    unittest.main()
