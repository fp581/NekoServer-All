"""music-setup.py, the music listener's check and set-up."""

import contextlib
import importlib.util
import io
import json
import os
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


class ListenerReasonTests(unittest.TestCase):
    """0.52.2 test round, T2c-02: an administrator typed in, no music library, a wrong
    password or Navidrome down all ended in one line; the check's own reason is kept."""

    def configure_said(self, refused):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            settings.write_text(json.dumps({'data_mount': tmp}))
            said = io.StringIO()
            with (
                mock.patch.object(music_setup.os, 'geteuid', return_value=0),
                mock.patch.object(music_setup, 'checked_settings', lambda value: value),
                mock.patch.object(music_setup, 'verify_storage'),
                mock.patch.object(music_setup, 'media_services', return_value=['navidrome']),
                mock.patch.object(music_setup.sys.stdin, 'isatty', return_value=True),
                mock.patch('builtins.input', return_value='listener'),
                mock.patch.object(music_setup.getpass, 'getpass', return_value='a password'),
                mock.patch.object(music_setup.music_client, 'save', side_effect=refused),
                contextlib.redirect_stdout(said),
            ):
                code = music_setup.main(['--config', str(settings), '--configure'])
        return code, said.getvalue().splitlines()[-1]

    def test_the_setup_says_why_the_listener_was_not_taken(self):
        refused = RuntimeError('Create a non-administrator listener account for clients and checks')
        self.assertEqual(
            self.configure_said(refused),
            (
                2,
                'Listener verification failed: Create a non-administrator listener account for clients and '
                'checks. No credential was saved.',
            ),
        )

    def test_the_nightly_check_keeps_the_reason(self):
        import music_client

        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': os.path.realpath(tmp)}
            path = Path(config['data_mount']) / 'appdata/navidrome/nekoserver-client.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(music_client.credential('listener', 'a password')))
            path.chmod(0o600)
            seen = os.stat(path)
            owned = os.stat_result((seen.st_mode, *seen[1:4], 0, *seen[5:]))
            with (
                mock.patch.object(music_client.os, 'stat', return_value=owned),
                mock.patch.object(
                    music_client, 'inspect', side_effect=RuntimeError('Navidrome has no configured music library')
                ),
            ):
                result = music_client.check(config)
        self.assertEqual(
            result,
            {
                'status': 'fail',
                'reason': 'Authenticated music catalogue check failed: Navidrome has no configured music library',
            },
        )
