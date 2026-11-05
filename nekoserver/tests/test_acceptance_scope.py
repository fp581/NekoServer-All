"""The dashboard's acceptance view is outdated once the USB drive, the photo archive
folder or the USB copy's schedule changes. Setup works the fingerprint out and writes it
into the dashboard's settings; the collector only compares it (collect.rs
the_profiles_and_the_scope_are_the_ones_setup_wrote)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from acceptance_summary import scope_digest  # noqa: E402
from neko_common import LOCATIONS  # noqa: E402

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
    def test_the_digest_is_stable(self):
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


class MovedDataTests(unittest.TestCase):
    def test_moving_an_apps_files_to_the_other_drive_makes_earlier_runs_outdated(self):
        """The review of 0.47.2: moving the photos from the SSD to the HDD changed neither
        fingerprint, so a restore witnessed before still passed."""
        import acceptance

        cfg = json.loads((ROOT / 'config.json').read_text())
        before = (scope_digest(cfg), acceptance.fingerprint(cfg, 'full'))
        for what in ('photos', 'music', 'cloud'):
            moved = {**cfg, 'locations': {what: 'media' if LOCATIONS[what][0] == 'ssd' else 'ssd'}}
            after = (scope_digest(moved), acceptance.fingerprint(moved, 'full'))
            self.assertNotEqual(after[0], before[0], what)
            self.assertNotEqual(after[1], before[1], what)
        # Naming the default changes nothing: a server that never moved keeps its results.
        same = {**cfg, 'locations': {what: LOCATIONS[what][0] for what in LOCATIONS}}
        self.assertEqual((scope_digest(same), acceptance.fingerprint(same, 'full')), before)

    def test_the_usb_copy_on_another_nas_disk_makes_earlier_runs_outdated(self):
        cfg = json.loads((ROOT / 'config.json').read_text())
        nas = {**cfg, 'photos': {**cfg.get('photos', {}), 'external_backup_network': {'wwn': '0x600140512345'}}}
        self.assertNotEqual(scope_digest(nas), scope_digest(cfg))


if __name__ == '__main__':
    unittest.main()
