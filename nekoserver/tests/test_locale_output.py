"""Deep check 271: a PC whose locale is not UTF-8 (LANG=en_US.ISO-8859-1, a legacy
setting) has a terminal that cannot show a dash. "sudo bash install.sh --check" stopped
on its first line with "ERROR: 'latin-1' codec can't encode character '\\u2014'"; what
the terminal cannot show now comes out as "?" instead."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LatinTerminalTests(unittest.TestCase):
    def run_tool(self, *argv):
        # PYTHONIOENCODING stands in for a Latin-1 locale: the same strict latin-1 streams.
        env = {**os.environ, 'PYTHONIOENCODING': 'latin-1', 'LC_ALL': 'C', 'LANG': 'C'}
        return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, capture_output=True, timeout=120, check=False)

    def test_the_setup_plan_is_shown_on_a_latin1_terminal(self):
        # Settings of its own whose app drive is an empty folder, never the real
        # /srv/nekoserver of the PC the tests run on (it may have NekoServer installed).
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads((ROOT / 'config.json').read_text())
            config['data_mount'] = str(Path(folder) / 'ssd')
            settings = Path(folder) / 'my-config.json'
            settings.write_text(json.dumps(config))
            result = self.run_tool('installer.py', '--check', '--config', str(settings))
        self.assertNotIn(b'codec', result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr.decode('latin-1'))
        self.assertIn(b'NekoServer v', result.stdout)
        self.assertIn(b'? read-only plan', result.stdout)


if __name__ == '__main__':
    unittest.main()
