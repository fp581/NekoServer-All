"""0.50.0, the wizard (P02): the disk Linux runs from may hold the apps, the media and
backups, or both (a PC with one drive). The role is then a folder of that disk, mounted at
the role's usual place by a bind mount; its record keeps the folder as findmnt shows it
("subvolume"), which every drive check, here and in the runtime, already compares.

The drive list's side of it (the row can be chosen, with what that means) is in
tests/test_drive_table.py, and the runtime's in common.rs
(two_roles_share_a_filesystem_only_as_folders_of_the_system_disk)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
from modules import capacity, storage  # noqa: E402

APPS, MEDIA = storage.SYSTEM_FOLDERS['data_ssd'], storage.SYSTEM_FOLDERS['media_hdd']
MOUNTS = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}


def role(folder, kept=None, uuid='root-0001'):
    record = {'uuid': uuid, 'fstype': 'ext4', 'folder': folder}
    return {**record, 'subvolume': kept} if kept else record


def settings(data=None, media=None):
    return {
        **MOUNTS,
        'storage': {
            'data_ssd': data or {'uuid': 'ssd-0001', 'fstype': 'ext4'},
            'media_hdd': media or {'uuid': 'hdd-0001', 'fstype': 'ext4'},
        },
    }


class RecordTests(unittest.TestCase):
    def test_the_two_folders_are_fixed_and_neither_is_inside_the_other(self):
        self.assertEqual((APPS, MEDIA), ('/var/lib/nekoserver/apps', '/var/lib/nekoserver/media'))
        self.assertNotIn(Path(APPS), Path(MEDIA).parents)
        self.assertNotIn(Path(MEDIA), Path(APPS).parents)

    def test_a_role_placed_there_is_recorded_with_the_system_filesystem_and_its_folder(self):
        record = {'uuid': 'old-drive', 'fstype': 'xfs', 'subvolume': '/@old', 'network': {'wwn': 'x'}}
        with mock.patch.object(storage, 'system_filesystem', return_value=('root-0001', 'ext4')):
            storage.on_system_disk(record, 'media_hdd')
        self.assertEqual(record, {'uuid': 'root-0001', 'fstype': 'ext4', 'folder': MEDIA})

    def test_only_the_roles_own_folder_is_accepted_from_the_settings(self):
        self.assertIsNone(storage.system_folder({'uuid': 'u', 'fstype': 'ext4'}, 'data_ssd'))
        self.assertEqual(storage.system_folder(role(APPS), 'data_ssd'), APPS)
        for record, key in ((role('/etc'), 'data_ssd'), (role(MEDIA), 'data_ssd'), (role(APPS), 'usb')):
            with self.subTest(folder=record['folder'], key=key):
                with self.assertRaisesRegex(RuntimeError, '"folder" of ".*" under "storage"'):
                    storage.system_folder(record, key)
        both = settings(role(APPS), role(MEDIA))
        self.assertEqual(storage.system_folders(both), {'/srv/nekoserver': APPS, '/mnt/nekoserver-media': MEDIA})
        self.assertEqual(storage.system_folders(settings()), {})

    def test_two_roles_on_the_system_disk_are_one_filesystem_and_two_drives_are_not(self):
        both = settings(role(APPS, APPS), role(MEDIA, MEDIA))
        specs = storage.mount_specs(both)
        self.assertEqual(
            specs,
            [
                ('data_ssd', 'root-0001', 'ext4', '/srv/nekoserver', APPS),
                ('media_hdd', 'root-0001', 'ext4', '/mnt/nekoserver-media', MEDIA),
            ],
        )
        # One of them on the system disk, the other a drive of its own.
        mixed = settings(role(APPS), None)
        self.assertEqual([spec[1] for spec in storage.mount_specs(mixed)], ['root-0001', 'hdd-0001'])
        # Two drives of their own must still be two filesystems.
        same = settings({'uuid': 'same-0001', 'fstype': 'ext4'}, {'uuid': 'same-0001', 'fstype': 'ext4'})
        with self.assertRaisesRegex(RuntimeError, 'different UUIDs'):
            storage.mount_specs(same)

    def test_linux_reinstalled_or_settings_from_another_pc_stop_setup(self):
        with mock.patch.object(storage, 'system_filesystem', return_value=('root-0001', 'ext4')):
            storage.validate_system_folder('root-0001', 'ext4')
            with self.assertRaisesRegex(RuntimeError, 'Linux now runs from another filesystem'):
                storage.validate_system_folder('root-before', 'ext4')


class FstabTests(unittest.TestCase):
    SPECS = [
        ('data_ssd', 'root-0001', 'ext4', '/srv/nekoserver', None),
        ('media_hdd', 'hdd-0001', 'ext4', '/mnt/nekoserver-media', None),
    ]
    FOLDERS = {'/srv/nekoserver': APPS}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fstab = Path(self.tmp.name) / 'fstab'
        self.fstab.write_text('UUID=root-0001 / ext4 defaults 0 1\n')

    def install(self, **more):
        return storage.install_fstab(self.SPECS, self.fstab, folders=self.FOLDERS, network=frozenset(), **more)

    def test_the_folder_gets_a_bind_line_and_the_systems_own_line_is_left_alone(self):
        self.assertTrue(self.install())
        lines = self.fstab.read_text().splitlines()
        self.assertEqual(lines[0], 'UUID=root-0001 / ext4 defaults 0 1')  # same UUID, not ours, untouched
        self.assertEqual(lines[1], f'{APPS} /srv/nekoserver none bind 0 0')
        self.assertTrue(lines[2].startswith('UUID=hdd-0001 /mnt/nekoserver-media ext4 '))
        # A second run changes nothing.
        self.assertFalse(self.install())

    def test_the_line_of_a_drive_given_up_for_the_system_disk_is_replaced(self):
        old = 'UUID=ssd-gone /srv/nekoserver ext4 defaults,nofail,x-systemd.device-timeout=90s 0 2'
        self.fstab.write_text(self.fstab.read_text() + old + '\n')
        said = []
        with mock.patch.object(storage, 'drive_connected', return_value=False):
            self.assertTrue(self.install(log=said.append))
        text = self.fstab.read_text()
        self.assertNotIn('ssd-gone', text)
        self.assertIn(f'{APPS} /srv/nekoserver none bind 0 0', text)
        self.assertTrue(any('the earlier drive (UUID=ssd-gone) is not connected' in line for line in said))

    def test_a_line_someone_else_wrote_for_the_place_stops_setup(self):
        self.fstab.write_text(self.fstab.read_text() + '/dev/sdz1 /srv/nekoserver ext4 defaults 0 2\n')
        with self.assertRaisesRegex(
            RuntimeError, 'already has a line for /srv/nekoserver, which NekoServer did not write'
        ):
            self.install()


class MountTests(unittest.TestCase):
    def mounted(self, shown):
        """mount_and_check with the role already mounted and findmnt showing `shown`."""
        patches = (
            mock.patch.object(storage.Path, 'mkdir'),
            mock.patch.object(storage, 'findmnt', return_value='/srv/nekoserver'),
            mock.patch.object(storage, 'waits_for_first_use', return_value=False),
            mock.patch.object(storage, 'mounted_filesystem', return_value=('root-0001', 'ext4')),
            mock.patch.object(storage, 'mounted_subvolume', return_value=shown),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_folder_findmnt_shows_is_what_the_record_keeps(self):
        self.mounted(APPS)
        self.assertEqual(storage.mount_and_check('root-0001', '/srv/nekoserver', None, 'ext4', folder=APPS), APPS)
        # Linux on a Btrfs subvolume: the folder is shown inside it, and kept that way.
        for patch in (mock.patch.object(storage, 'mounted_subvolume', return_value='/@' + APPS),):
            with patch:
                self.assertEqual(
                    storage.mount_and_check('root-0001', '/srv/nekoserver', None, 'ext4', folder=APPS), '/@' + APPS
                )

    def test_anything_else_mounted_at_the_place_is_refused(self):
        for shown in (None, MEDIA, '/home'):
            with self.subTest(shown=shown):
                self.mounted(shown)
                with self.assertRaisesRegex(RuntimeError, 'should be the folder /var/lib/nekoserver/apps'):
                    storage.mount_and_check('root-0001', '/srv/nekoserver', None, 'ext4', folder=APPS)

    def test_a_drive_of_its_own_is_still_refused_as_a_folder(self):
        self.mounted('/films')
        with self.assertRaisesRegex(RuntimeError, 'Only the folder /films of the drive is mounted'):
            storage.mount_and_check('root-0001', '/srv/nekoserver', None, 'ext4')

    def test_a_later_check_must_find_the_same_folder(self):
        self.mounted(APPS)
        record = role(APPS, APPS)
        self.assertEqual(storage.subvolume_of(record), APPS)
        self.assertEqual(storage.mount_and_check('root-0001', '/srv/nekoserver', APPS, 'ext4', folder=APPS), APPS)
        with self.assertRaisesRegex(RuntimeError, 'a restart would mount a different tree'):
            storage.mount_and_check('root-0001', '/srv/nekoserver', '/@' + APPS, 'ext4', folder=APPS)
        with self.assertRaisesRegex(RuntimeError, 'storage subvolume must be the folder findmnt shows'):
            storage.subvolume_of({**record, 'subvolume': 'apps folder'})


class RoomTests(unittest.TestCase):
    def test_the_room_of_one_filesystem_is_counted_once_for_both_roles(self):
        config = {**MOUNTS}
        gb = capacity.GIB
        with mock.patch.object(capacity, 'same_filesystem', return_value=True):
            stop, _ = capacity.assess(
                config, free=lambda path: 60 * gb, used=lambda path: 0, usb_space=lambda *a, **k: None
            )
            self.assertEqual(len(stop), 1)
            self.assertIn('needs at least 70 GB there for the apps, your files and the backups together', stop[0])
            stop, _ = capacity.assess(
                config, free=lambda path: 80 * gb, used=lambda path: 0, usb_space=lambda *a, **k: None
            )
            self.assertEqual(stop, [])
        # Two drives: each on its own, as before (60 GB is enough for either).
        with mock.patch.object(capacity, 'same_filesystem', return_value=False):
            stop, _ = capacity.assess(
                config, free=lambda path: 60 * gb, used=lambda path: 0, usb_space=lambda *a, **k: None
            )
            self.assertEqual(stop, [])

    def test_the_list_refuses_the_system_disk_when_it_has_no_room_for_what_goes_there(self):
        with (
            mock.patch.object(storage, 'system_filesystem', return_value=('root-0001', 'ext4')),
            mock.patch.object(storage, 'system_free_gb', return_value=60),
        ):
            self.assertEqual(drive_table.system_verdict('data_ssd', {}), '')
            self.assertEqual(drive_table.system_verdict('media_hdd', {}), '')
            # The apps are there already: the media and backups need their room on top.
            apps_there = {'storage': {'data_ssd': role(APPS)}}
            self.assertEqual(
                drive_table.system_verdict('media_hdd', apps_there),
                'it has too little room: 60 GB free, and the media HDD needs at least 70 GB there together with what '
                'is already kept there',
            )
            self.assertEqual(drive_table.system_verdict('usb', {}), drive_table.SYSTEM_NOT_OFFLINE)
        with mock.patch.object(storage, 'system_filesystem', return_value=('root-0001', 'zfs')):
            self.assertIn(
                'zfs filesystem, which the apps cannot keep their files on', drive_table.system_verdict('data_ssd', {})
            )


if __name__ == '__main__':
    unittest.main()
