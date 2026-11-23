"""Odd drive paths in a hand-written settings file, after 0.52.2 moved the path checks to
where each path is used (the 0.52.2 test round, T4-1 and T4-2): each is refused as
v0.52.1 refused it, with the same words, and before anything runs."""

import argparse
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer
import media_libraries
import native_runtime
import onboard

NUL = '/srv/nekoserver\x00x'


def load_connector():
    spec = importlib.util.spec_from_file_location('media_connect_odd_paths', ROOT / 'media-connect.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TwoSlashesTests(unittest.TestCase):
    def test_a_mounted_ssd_written_with_two_slashes_stops_setup_before_any_step(self):
        """ "//srv/nekoserver" is the mounted SSD under another name: v0.52.1 kept the state
        on it and its first state write stopped setup before any step ran. Keeping the
        state in the home folder instead would run the steps (T4-1)."""
        default = Path('/nonexistent/t4-default-state')
        args = argparse.Namespace(state_dir=str(default))
        with (
            mock.patch.object(installer, 'DEFAULT_STATE', default),
            mock.patch.object(installer.os.path, 'ismount', lambda path: path == '/srv/nekoserver'),
        ):
            with self.assertRaises(RuntimeError) as caught:
                installer.state_folders(args, {'data_mount': '//srv/nekoserver'})
        self.assertEqual(str(caught.exception), 'Refusing symlink path: //srv/nekoserver/installer/state/state.json')

    def test_the_ssd_named_as_it_is_still_keeps_the_state(self):
        default = Path('/nonexistent/t4-default-state')
        args = argparse.Namespace(state_dir=str(default))
        with (
            mock.patch.object(installer, 'DEFAULT_STATE', default),
            mock.patch.object(installer.os.path, 'ismount', lambda path: path == '/srv/nekoserver'),
            mock.patch.object(installer.os.path, 'realpath', lambda path: os.path.abspath(path)),
        ):
            given, state_dir, on_the_ssd = installer.state_folders(args, {'data_mount': '/srv/nekoserver'})
        self.assertEqual((state_dir, on_the_ssd), (Path('/srv/nekoserver/installer/state'), True))


class NulByteTests(unittest.TestCase):
    """A NUL byte names no folder: each check says so in its own words, as v0.52.1 did,
    instead of ending on "embedded null byte" (T4-2)."""

    def test_setup_keeps_the_state_in_the_folder_given(self):
        default = Path('/nonexistent/t4-default-state')
        args = argparse.Namespace(state_dir=str(default))
        with mock.patch.object(installer, 'DEFAULT_STATE', default):
            self.assertEqual(installer.state_folders(args, {'data_mount': NUL}), (default, default, False))

    def test_onboarding_says_the_two_drives_are_not_there(self):
        self.assertFalse(onboard.mount_ok(NUL))
        parser = onboard.arguments()
        a = parser.parse_args(['--config', 'settings.json'])
        with mock.patch('sys.stderr') as stderr, self.assertRaises(SystemExit) as caught:
            onboard.the_two_drives(parser, a, {'data_mount': NUL, 'media_mount': '/srv/media'})
        self.assertEqual(caught.exception.code, 2)
        said = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('Expected two distinct mounted data/media filesystems; refusing to proceed', said)

    def test_the_connector_says_the_ssd_is_not_mounted(self):
        connect = load_connector()
        parser = argparse.ArgumentParser(prog='media-connect.py')
        args = argparse.Namespace(data_root=None)
        with mock.patch('sys.stderr') as stderr, self.assertRaises(SystemExit) as caught:
            connect.mounted_ssd(parser, args, {'data_mount': NUL})
        self.assertEqual(caught.exception.code, 2)
        said = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('Data SSD must be mounted at --data-root', said)

    def test_no_release_folders_and_no_library_folder(self):
        self.assertEqual(native_runtime.release_folders(Path(NUL)), [])
        found, library_id, on_share = media_libraries.jellyfin_library(None, NUL, False, 'movies', 'Movies', 'movies')
        self.assertEqual(
            (found, library_id, on_share),
            (
                {
                    'check': 'jellyfin:movies',
                    'status': 'needs_setup',
                    'reason': 'Expected HDD library folder is missing',
                },
                None,
                False,
            ),
        )


if __name__ == '__main__':
    unittest.main()
