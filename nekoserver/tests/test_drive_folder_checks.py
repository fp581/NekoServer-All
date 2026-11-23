"""Two checks of the drives' folders that 0.52.2 split out (mutation test T6): onboarding
takes a drive only by its real, mounted folder, and the recovery tools run Immich only
from the Compose files setup left on the app SSD."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import onboard  # noqa: E402
import recovery_tools  # noqa: E402


class OnboardDriveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.drive = Path(tmp.name) / 'drive'
        self.drive.mkdir()
        self.link = Path(tmp.name) / 'ssd'
        self.link.symlink_to(self.drive)

    def test_a_mounted_drive_named_by_its_real_folder_is_taken(self):
        with mock.patch.object(onboard.os.path, 'ismount', return_value=True):
            self.assertTrue(onboard.mount_ok(str(self.drive)))

    def test_a_folder_that_is_not_a_mounted_drive_is_not(self):
        with mock.patch.object(onboard.os.path, 'ismount', return_value=False):
            self.assertFalse(onboard.mount_ok(str(self.drive)))

    def test_a_link_to_a_mounted_drive_is_not_the_drive(self):
        with mock.patch.object(onboard.os.path, 'ismount', return_value=True):
            self.assertFalse(onboard.mount_ok(str(self.link)))


class ImmichComposeTests(unittest.TestCase):
    """compose_args: what the recovery tools run Immich's containers with."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ssd = Path(tmp.name)
        self.immich = self.ssd / 'compose' / 'immich'
        self.immich.mkdir(parents=True)
        self.config = {'data_mount': str(self.ssd)}

    def test_immich_runs_from_its_compose_file_and_settings_on_the_ssd(self):
        (self.immich / 'compose.yaml').write_text('services: {}\n')
        (self.immich / '.env').write_text('DB_PASSWORD=x\n')
        args = recovery_tools.compose_args(self.config)
        real = os.path.realpath(self.immich)
        self.assertEqual(
            args[-6:],
            ['-p', 'neko-immich', '-f', f'{real}/compose.yaml', '--env-file', f'{real}/.env'],
        )

    def test_without_either_file_nothing_is_run(self):
        for present in ('compose.yaml', '.env'):
            with self.subTest(only=present):
                for name in ('compose.yaml', '.env'):
                    (self.immich / name).unlink(missing_ok=True)
                (self.immich / present).write_text('x\n')
                with self.assertRaises(RuntimeError) as caught:
                    recovery_tools.compose_args(self.config)
                self.assertEqual(str(caught.exception), 'Existing Immich manifest and environment required')


if __name__ == '__main__':
    unittest.main()
