"""Any number of drives works, one drive included (the user, after gate run B): one drive
of its own may hold the apps, the media and the backups, as the disk Linux runs from
may. The drive is mounted once (storage.ONE_DRIVE) and each role is a folder of it, bound
at the role's usual place; the backups on the same drive are said once. Two drives work
in either order."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import drive_table  # noqa: E402
from modules import storage  # noqa: E402
from test_drive_table import FOLDERS, INVENTORY, Conversation, DriveListCase  # noqa: E402

SYSTEM = INVENTORY['blockdevices'][2]  # the disk Linux runs from
SSD = INVENTORY['blockdevices'][3]  # sda: 466 GB SSD
HDD = INVENTORY['blockdevices'][4]  # sdb: 1.8 TB HDD
APPS, MEDIA = storage.SHARED_FOLDERS['data_ssd'], storage.SHARED_FOLDERS['media_hdd']


def inventory(*disks):
    return {'blockdevices': list(disks)}


class OneDriveTests(DriveListCase):
    def resolve(self, disks, answers):
        talk = Conversation(answers)
        config = dict(FOLDERS)
        with mock.patch('builtins.input', talk.ask):
            storage.resolve_storage_identity(config, inventory(*disks), True, True, talk.ask, talk.out)
        return config, talk

    def test_one_drive_of_its_own_holds_both_and_is_recommended_before_the_system_disk(self):
        table = self.table(dict(FOLDERS), inventory(SYSTEM, HDD))
        best = drive_table.recommended(table, dict(FOLDERS))
        self.assertEqual((best['data_ssd']['name'], best['media_hdd']['name']), ('sdb1', 'sdb1'))
        # Enter at both lines: both are folders of that drive.
        config, talk = self.resolve((SYSTEM, HDD), ['', ''])
        self.assertEqual(
            config['storage'],
            {
                'data_ssd': {'uuid': 'hdd-0001', 'fstype': 'ext4', 'folder': APPS},
                'media_hdd': {'uuid': 'hdd-0001', 'fstype': 'ext4', 'folder': MEDIA},
            },
        )
        self.assertEqual(sum(drive_table.SAME_DISK_BACKUPS in line for line in talk.lines), 1)  # said once
        self.assertEqual(storage.shared_with(config), ('hdd-0001', 'ext4'))

    def test_the_drive_must_have_room_for_both(self):
        small = dict(HDD, size='60G', children=[dict(HDD['children'][0], size='60G')])
        table = self.table(dict(FOLDERS), inventory(SYSTEM, small))
        row = next(r for r in table if r['name'] == 'sdb1')
        config = {**FOLDERS, 'storage': {'data_ssd': {'uuid': 'hdd-0001', 'fstype': 'ext4'}}}
        self.assertIn('too small for both', drive_table.verdict(row, 'media_hdd', config))
        roomy = next(r for r in self.table(dict(FOLDERS), inventory(SYSTEM, HDD)) if r['name'] == 'sdb1')
        self.assertEqual(drive_table.verdict(roomy, 'media_hdd', config), '')
        # A drive that has the apps from an earlier run (set up, mounted) is not shared afterwards.
        kept = {**FOLDERS, 'storage': {'data_ssd': {'uuid': 'hdd-0001', 'fstype': 'ext4', 'subvolume': '/'}}}
        self.assertIn('already', drive_table.verdict(roomy, 'media_hdd', kept))

    def test_the_system_disk_alone_still_holds_both(self):
        config, _ = self.resolve((SYSTEM,), ['', ''])
        self.assertEqual(
            [config['storage'][key]['folder'] for key in ('data_ssd', 'media_hdd')],
            list(storage.SYSTEM_FOLDERS.values()),
        )
        self.assertIsNone(storage.shared_with(config))

    def test_two_drives_in_either_order(self):
        for apps, media in (('2', '3'), ('3', '2')):
            with self.subTest(apps=apps, media=media):
                config, _ = self.resolve((SYSTEM, SSD, HDD), [apps, media])
                uuids = {'2': 'ssd-0001', '3': 'hdd-0001'}
                self.assertEqual(config['storage']['data_ssd'], {'uuid': uuids[apps], 'fstype': 'ext4'})
                self.assertEqual(config['storage']['media_hdd'], {'uuid': uuids[media], 'fstype': 'ext4'})


class MountTests(unittest.TestCase):
    CONFIG = {
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
        'storage': {
            'data_ssd': {'uuid': 'hdd-00001', 'fstype': 'ext4', 'folder': APPS},
            'media_hdd': {'uuid': 'hdd-00001', 'fstype': 'ext4', 'folder': MEDIA},
        },
    }

    def test_the_drive_is_mounted_first_then_the_two_folders(self):
        specs = storage.mount_specs(self.CONFIG)
        self.assertEqual(
            [(s[0], s[3]) for s in specs],
            [('one_drive', storage.ONE_DRIVE), ('data_ssd', '/srv/nekoserver'), ('media_hdd', '/mnt/nekoserver-media')],
        )
        self.assertEqual(storage.system_folders(self.CONFIG), {'/srv/nekoserver': APPS, '/mnt/nekoserver-media': MEDIA})

    def test_the_folders_lines_wait_for_the_drive_and_are_known_as_setups_own(self):
        line = storage.bind_line(APPS, '/srv/nekoserver')
        self.assertEqual(
            line, f'{APPS} /srv/nekoserver none bind,x-systemd.requires-mounts-for={storage.ONE_DRIVE} 0 0'
        )
        self.assertTrue(storage.own_bind_line(line.split()))
        system = storage.bind_line(storage.SYSTEM_FOLDERS['data_ssd'], '/srv/nekoserver')
        self.assertTrue(storage.own_bind_line(system.split()))
        self.assertFalse(storage.own_bind_line(['/home/x', '/srv/nekoserver', 'none', 'bind', '0', '0']))

    def test_a_folder_is_checked_as_findmnt_shows_it_inside_the_drive(self):
        self.assertEqual(storage.inside_filesystem(APPS), '/nekoserver/apps')
        self.assertEqual(storage.inside_filesystem(storage.SYSTEM_FOLDERS['data_ssd']), '/var/lib/nekoserver/apps')
        with (
            mock.patch.object(storage, 'findmnt', return_value='/srv/nekoserver'),
            mock.patch.object(storage, 'waits_for_first_use', return_value=False),
            mock.patch.object(storage, 'mounted_filesystem', return_value=('hdd-00001', 'ext4')),
            mock.patch.object(storage, 'mounted_subvolume', return_value='/nekoserver/apps'),
            mock.patch.object(storage.Path, 'mkdir'),
        ):
            self.assertEqual(
                storage.mount_and_check('hdd-00001', '/srv/nekoserver', None, 'ext4', folder=APPS), '/nekoserver/apps'
            )

    def test_the_drive_itself_is_checked_by_its_uuid_not_as_the_system_disk(self):
        specs = storage.mount_specs(self.CONFIG)
        with (
            mock.patch.object(storage, 'validate_device') as device,
            mock.patch.object(storage, 'validate_system_folder', side_effect=AssertionError('system')),
            mock.patch.object(storage, 'findmnt', return_value=None),
            mock.patch.object(storage, 'waits_for_first_use', return_value=False),
            mock.patch.object(storage.Path, 'exists', return_value=False),
        ):
            storage.checked_before_mount(self.CONFIG, specs)
        self.assertEqual(device.call_count, 3)

    def test_the_uninstaller_takes_out_the_drives_line_too(self):
        import uninstaller

        self.assertEqual(uninstaller.ONE_DRIVE, storage.ONE_DRIVE)


if __name__ == '__main__':
    unittest.main()


class OneDriveWordsTests(unittest.TestCase):
    """Gate run B3: on one drive the photos question offered "the media drive: 105 GB free"
    and "the SSD: 105 GB free", the same drive twice, and the log said "your only copy on
    another drive is the weekly USB backup" with no offline drive."""

    def one(self, **photos):
        folders = storage.SHARED_FOLDERS
        return {
            'storage': {key: {'uuid': 'one-1', 'fstype': 'ext4', 'folder': folders[key]} for key in folders},
            'photos': photos,
        }

    def test_the_one_drive_is_not_offered_twice(self):
        from types import SimpleNamespace

        said = []
        ctx = SimpleNamespace(config=self.one(), log=said.append)
        room = {'ssd': 105, 'media': 105}
        place = storage.asked_location(
            ctx, "Immich's photos", 'media', room, False, mock.Mock(side_effect=AssertionError)
        )
        self.assertEqual(place, 'media')
        self.assertEqual(said, ["Immich's photos: kept on the one drive of this server."])

    def test_the_photos_line_says_what_copies_there_are(self):
        none = storage.photos_on_media(self.one(**{storage.OFFLINE_WANTED: False}))
        self.assertIn('the one drive of this server', none)
        self.assertNotIn('only copy on another drive', none)
        self.assertIn('run  sudo bash install.sh  again', none)
        usb = storage.photos_on_media(self.one(external_backup_uuid='usb-1'))
        self.assertIn('Your only copy on another drive is the weekly USB backup.', usb)
        self.assertEqual(storage.photos_on_media({'photos': {}}), storage.PHOTOS_ON_MEDIA)
