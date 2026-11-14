"""A settings file cut short (a full disk, a power cut while it was saved) is said in
plain words that name the file, by every command that reads it: never a Python
traceback, never a hang (deep check 226)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common

# Each command, with what it is asked to do. Run as if by root (os.geteuid faked), so the
# commands that need sudo get as far as reading the settings.
COMMANDS = (
    ('media-connect.py',),
    ('music-setup.py',),
    ('migrate.py', 'list'),
    ('onboard.py',),
    ('source-wizard.py', '--list'),
    ('system_updates.py', 'check'),
    ('upgrade.py', 'resume'),
    ('recover.py', 'check'),
)
AS_ROOT = 'import os, runpy, sys; os.geteuid = lambda: 0; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")'


class CutSettingsTests(unittest.TestCase):
    def run_command(self, command, settings):
        script, *rest = command
        return subprocess.run(
            [sys.executable, '-c', AS_ROOT, str(ROOT / script), *rest, '--config', str(settings)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_every_command_names_a_cut_off_settings_file(self):
        template = (ROOT / 'config.json').read_bytes().rstrip()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            for cut in (0, 1, 300, len(template) - 1):
                settings.write_bytes(template[:cut])
                for command in COMMANDS:
                    with self.subTest(command=command[0], cut=cut):
                        done = self.run_command(command, settings)
                        said = done.stdout + done.stderr
                        self.assertNotIn('Traceback', said)
                        self.assertNotEqual(done.returncode, 0)
                        self.assertIn(str(settings), said)
                        self.assertIn('is not valid JSON', said)

    def test_settings_that_are_not_an_object_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            for text in ('[]', 'null', '"x"', '1e400'):
                settings.write_text(text)
                with self.subTest(text=text), self.assertRaises(RuntimeError) as said:
                    neko_common.read_settings(settings)
                self.assertIn(str(settings), str(said.exception))
            settings.unlink()
            with self.assertRaises(RuntimeError) as said:
                neko_common.read_settings(settings)
            self.assertIn('is not there', str(said.exception))


if __name__ == '__main__':
    unittest.main()
