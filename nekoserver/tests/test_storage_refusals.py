"""1.0.1 storage from the dashboard: the one list of reasons a disk is never offered
(plans/1.0.1-contract.md §5). Every code has a test; no test reads a real disk."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402

TB4 = 4000787030016


def disk(kname='sdc', serial='WD-WCC4E1234567', size=TB4, children=(), **extra):
    return {
        'name': kname,
        'kname': kname,
        'path': f'/dev/{kname}',
        'type': 'disk',
        'size': size,
        'serial': serial,
        'model': 'WDC WD40EFRX',
        'tran': 'sata',
        'maj:min': '8:32',
        'children': list(children),
        **extra,
    }


def part(kname, **extra):
    return {'name': kname, 'kname': kname, 'path': f'/dev/{kname}', 'type': 'part', 'maj:min': '8:33', **extra}


def world(*disks, by_id=None, holders=None, mounted=(), swaps=(), named=None):
    return {
        'lsblk': list(disks),
        'by_id': by_id if by_id is not None else {d['kname']: [f'ata-{d["model"]}_{d["serial"]}'] for d in disks},
        'holders': holders or {},
        'mounted': set(mounted),
        'swaps': set(swaps),
        'named': named or {},
    }


def code(w, minimum=10**9, index=0):
    refused = storage_jobs.refusal(storage_jobs.describe(w)[index], minimum)
    return refused and refused['code']


class RefusalTests(unittest.TestCase):
    def test_a_blank_disk_of_its_own_is_offered(self):
        self.assertIsNone(code(world(disk())))

    def test_a_disk_without_a_by_id_name_or_serial_is_unknown(self):
        self.assertEqual(code(world(disk(), by_id={})), 'unknown')
        self.assertEqual(code(world(disk(serial=''))), 'unknown')

    def test_two_disks_with_one_serial_are_neither_offered(self):
        twins = world(disk('sdc'), disk('sdd'))
        self.assertEqual(code(twins, index=0), 'unknown')
        self.assertEqual(code(twins, index=1), 'unknown')

    def test_the_system_disk(self):
        w = world(disk(children=[part('sdc1', mountpoints=['/boot/efi']), part('sdc2', mountpoints=['/'])]))
        self.assertEqual(code(w), 'system')

    def test_every_drive_the_settings_name_even_one_not_mounted_now(self):
        for named, expected in (
            ('role', 'role'),
            ('settings', 'settings'),
            ('offline_backup', 'offline_backup'),
            ('cards', 'cards'),
            ('migrate_old', 'migrate_old'),
        ):
            w = world(disk(children=[part('sdc1', fstype='ext4', uuid='u-1')]), named={'u-1': named})
            self.assertEqual(code(w), expected)

    def test_a_running_mirror_member_and_other_holders(self):
        member = disk(children=[part('sdc1', fstype='linux_raid_member')])
        self.assertEqual(code(world(member, holders={'sdc1': ['md0']})), 'mirror_member')
        luks = disk(children=[part('sdc1', fstype='crypto_LUKS')])
        self.assertEqual(code(world(luks, holders={'sdc1': ['dm-0']})), 'holders')

    def test_a_set_signature_not_assembled(self):
        for signature in ('linux_raid_member', 'LVM2_member', 'crypto_LUKS', 'zfs_member'):
            self.assertEqual(code(world(disk(fstype=signature))), 'signature', signature)

    def test_swap_and_mounts_anywhere_binds_and_containers_included(self):
        self.assertEqual(code(world(disk(children=[part('sdc1')]), swaps={'/dev/sdc1'})), 'swap')
        self.assertEqual(code(world(disk(children=[part('sdc1', mountpoints=['/media/x'])]))), 'mounted')
        # Not in lsblk's mountpoints, but mounted in a container's namespace (its mountinfo).
        self.assertEqual(code(world(disk(children=[part('sdc1')]), mounted={'8:33'})), 'mounted')

    def test_a_disk_smaller_than_asked(self):
        self.assertEqual(code(world(disk(size=10**9)), minimum=TB4), 'small')
        self.assertIsNone(code(world(disk(size=TB4)), minimum=TB4))

    def test_each_refusal_says_why_in_words(self):
        refused = storage_jobs.refusal(storage_jobs.describe(world(disk(size=10**9)))[0], TB4)
        self.assertEqual(refused['words'], 'It is too small: at least 4 TB is needed.')


class SettingsTests(unittest.TestCase):
    def test_the_settings_name_roles_extra_drives_the_old_drive_the_usb_backup_and_cards(self):
        with tempfile.TemporaryDirectory() as data:
            cards = Path(data) / 'cards'
            cards.mkdir()
            with sqlite3.connect(cards / 'catalogue.sqlite') as db:
                db.execute('CREATE TABLE volumes (volume_id TEXT, filesystem_uuid TEXT)')
                db.execute("INSERT INTO volumes VALUES ('v1', 'card-1')")
            config = {
                'data_mount': data,
                'storage': {
                    'data_ssd': {'uuid': 'ssd-1', 'previous_drive': {'uuid': 'old-1'}},
                    'media_hdd': {'uuid': 'hdd-1'},
                    'd3': {'uuid': 'd3-1'},
                },
                'photos': {'external_backup_uuid': 'usb-1'},
            }
            self.assertEqual(
                storage_jobs.settings_uuids(config),
                {
                    'ssd-1': 'role',
                    'old-1': 'migrate_old',
                    'hdd-1': 'role',
                    'd3-1': 'settings',
                    'usb-1': 'offline_backup',
                    'card-1': 'cards',
                },
            )


class WordsTests(unittest.TestCase):
    def test_sizes_and_the_confirm_words(self):
        self.assertEqual(storage_jobs.size_words(TB4), '4 TB')
        self.assertEqual(storage_jobs.size_words(256060514304), '256 GB')
        self.assertEqual(storage_jobs.size_words(1.5 * 10**12), '1.5 TB')
        row = storage_jobs.describe(world(disk()))[0]
        self.assertEqual(storage_jobs.confirm_words(row), '4 TB 4567')
        self.assertTrue(storage_jobs.same_words('4  tb 4567 ', '4 TB 4567'))
        self.assertFalse(storage_jobs.same_words('4 TB 4568', '4 TB 4567'))


if __name__ == '__main__':
    unittest.main()
