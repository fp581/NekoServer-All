"""music-setup.py, the music listener's check and set-up."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('music_setup', ROOT / 'music-setup.py')
music_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(music_setup)


class DriveCheckTests(unittest.TestCase):
    def test_a_drive_that_is_not_there_stops_it_with_the_reason_not_a_traceback(self):
        """Seen on a fresh Ubuntu: `music-setup.py` ended in a Python traceback when the drive
        check refused (a drive not connected, or the runtime not built yet)."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            settings.write_text(json.dumps({'data_mount': tmp}))
            refused = RuntimeError('The HDD is not mounted at /mnt/nekoserver-media')
            said = io.StringIO()
            with (
                mock.patch.object(music_setup.os, 'geteuid', return_value=0),
                mock.patch.object(music_setup, 'checked_settings', lambda value: value),
                mock.patch.object(music_setup, 'verify_storage', side_effect=refused),
                contextlib.redirect_stderr(said),
            ):
                code = music_setup.main(['--config', str(settings)])
        self.assertEqual(code, 2)
        self.assertIn('Music setup stopped: The HDD is not mounted at /mnt/nekoserver-media', said.getvalue())
        self.assertIn('Nothing was changed', said.getvalue())


if __name__ == '__main__':
    unittest.main()
