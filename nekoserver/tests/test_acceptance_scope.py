"""The dashboard's acceptance view is outdated once the USB drive, the photo archive
folder or the USB copy's schedule changes (the Rust collector computes the same digest:
collect.rs the_acceptance_scope_names_the_usb_drive_and_archive_like_python)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance_summary import scope_digest  # noqa: E402

SETTINGS = {
    'data_mount': '/srv/nekoserver',
    'media_mount': '/mnt/m',
    'photos': {'deploy_immich': True, 'external_backup_uuid': 'cccc-3333', 'archive_dir': 'photos/archive'},
    'media': {'services': ['jellyfin']},
    'gateway': {'lan_ip': '192.168.1.5'},
}


def changed(section, key, value):
    cfg = {**SETTINGS, section: {**SETTINGS[section], key: value}}
    return scope_digest(cfg)


class ScopeTests(unittest.TestCase):
    def test_same_digest_as_the_rust_collector(self):
        self.assertEqual(scope_digest(SETTINGS), '4a3c4b125516bde8707230fcae0595b16d6db218ffc0d344dcfcc6394cc5828d')

    def test_a_new_usb_drive_archive_or_schedule_makes_it_outdated(self):
        first = scope_digest(SETTINGS)
        for key, value in (
            ('external_backup_uuid', 'dddd-4444'),
            ('external_backup_fstype', 'exfat'),
            ('archive_dir', 'pictures/archive'),
            ('backup_weekday', 'Mon'),
            ('backup_time', '04:00'),
            ('deploy_immich', False),
        ):
            self.assertNotEqual(changed('photos', key, value), first, key)

    def test_unrelated_photo_settings_do_not(self):
        self.assertEqual(changed('photos', 'machine_learning', True), scope_digest(SETTINGS))


if __name__ == '__main__':
    unittest.main()
