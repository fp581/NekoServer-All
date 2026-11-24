"""1.0.1 storage from the dashboard: setup run again accepts what the Drives page did.

- "locations": {"backups": "d3"} is a valid setting (never "ssd"), and setup mounts the
  backups' folders there at their usual places on the media drive (bind lines with
  nofail), so every backup job keeps its paths;
- an extra drive added from the dashboard is mounted before anything is put on it;
- a kind moved to another drive passes setup's check that the files are where the
  settings say (same_place): the old copy was renamed out of the apps' way."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_checks  # noqa: E402
import placement  # noqa: E402
from modules import storage  # noqa: E402

UUID = '0d6c1f6e-1111-4c4c-8a8a-123456789abc'


def settings(**locations):
    return {
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
        'storage': {
            'data_ssd': {'uuid': 'a' * 8},
            'media_hdd': {'uuid': 'b' * 8},
            'd3': {'uuid': UUID, 'fstype': 'ext4'},
        },
        'locations': locations,
    }


class BackupsPlaceTests(unittest.TestCase):
    def test_the_backups_may_be_on_the_media_drive_or_an_extra_drive_never_the_apps_drive(self):
        config_checks.check_locations(settings(backups='d3'))
        config_checks.check_locations(settings(backups='media'))
        on_the_apps_drive, on_no_drive = settings(backups='ssd'), settings(backups='d9')
        with self.assertRaisesRegex(RuntimeError, 'backups never "ssd"'):
            config_checks.check_locations(on_the_apps_drive)
        with self.assertRaises(RuntimeError):
            config_checks.check_locations(on_no_drive)

    def test_the_placement_table_follows_the_backups(self):
        self.assertEqual(placement.derived(settings(backups='d3'))[placement.BACKUP][placement.DATA], 'd3')
        self.assertEqual(placement.derived(settings())[placement.BACKUP][placement.DATA], 'media')

    def test_the_backups_folders_on_an_extra_drive_are_mounted_at_their_usual_places(self):
        binds = storage.backup_binds(settings(backups='d3'))
        self.assertEqual(
            [(str(s), str(t)) for _, s, t, _ in binds],
            [
                ('/mnt/nekoserver-drives/d3/backups', '/mnt/nekoserver-media/backups'),
                ('/mnt/nekoserver-drives/d3/photos/archive', '/mnt/nekoserver-media/photos/archive'),
            ],
        )
        self.assertEqual(storage.backup_binds(settings()), [])
        line = storage.installed_line(*[str(x) for x in binds[0][1:]], storage.bind_parent(settings(), binds[0][2]))
        self.assertEqual(
            line,
            '/mnt/nekoserver-drives/d3/backups /mnt/nekoserver-media/backups none bind,nofail,'
            'x-systemd.requires-mounts-for=/mnt/nekoserver-drives/d3,'
            'x-systemd.requires-mounts-for=/mnt/nekoserver-media 0 0',
        )

    def test_setup_never_mounts_the_backups_drive_over_backups_still_on_the_media_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(backups='d3')
            config['media_mount'] = tmp
            (Path(tmp) / 'backups').mkdir()
            (Path(tmp) / 'backups' / 'host.tar').write_text('x')
            context = mock.Mock(config=config)
            with self.assertRaisesRegex(RuntimeError, 'Your backups are still in'):
                storage.mount_installed(context, fstab=Path(tmp) / 'fstab')


class AddedDriveTests(unittest.TestCase):
    def test_an_extra_drive_added_from_the_dashboard_is_mounted_with_nothing_on_it_yet(self):
        config = settings()
        self.assertEqual(storage.extra_specs(config), [])
        config['storage']['d3']['added'] = True
        specs = storage.extra_specs(config)
        self.assertEqual([(s[0], s[3]) for s in specs], [('d3', '/mnt/nekoserver-drives/d3')])


class MovedKindTests(unittest.TestCase):
    def test_a_kind_moved_to_the_media_drive_passes_setups_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(photos='media')
            config['data_mount'], config['media_mount'] = f'{tmp}/ssd', f'{tmp}/media'
            (Path(tmp) / 'media/photos/immich-library/x.jpg').mkdir(parents=True)
            # The move renamed the folder on the apps drive to its old copy.
            (Path(tmp) / 'ssd/nekoserver-old-photos-0123456789abcdef/photos/immich-library/x.jpg').mkdir(parents=True)
            kept = storage.kept_location(config, 'photos')
            self.assertEqual(kept, ['media'])
            storage.same_place(mock.Mock(), 'photos', 'your photos', 'media', kept)


if __name__ == '__main__':
    unittest.main()
