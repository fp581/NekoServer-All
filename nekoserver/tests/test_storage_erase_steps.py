"""1.0.1 storage from the dashboard: the one erase (plans/1.0.1-contract.md §5), with the disk
tools faked and the disks plain files under a by-id folder of their own: no test opens a disk.

Before each write the disk is found again by its by-id name, its serial and size are read
again and the refusals asked again; a disk that changed meanwhile stops the erase before
anything is written. The journal is told each step's new ids before the step writes."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs
from tests.test_storage_refusals import disk, part, world

GB = 10**9
SIZE = 32 * GB
SERIAL = 'S-AAAA'
BY_ID = f'ata-WDC_WD40EFRX_{SERIAL}'

# What `sfdisk --dump` says of a mirror's present member.
DUMP = """label: gpt
label-id: 6D3C1F2A-0000-4000-8000-000000000001
device: /dev/sdb
unit: sectors
first-lba: 2048
last-lba: 62914526
sector-size: 512

/dev/sdb1 : start=2048, size=1048576, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=1111, name="EFI: boot"
/dev/sdb2 : start=1050624, size=61863903, type=A19D880F-05FC-4D3B-A006-743F0F84911E, uuid=2222
"""
# The same table for the new disk: no ids of the member's own, and its end the new disk's.
TABLE = """label: gpt
unit: sectors
first-lba: 2048
sector-size: 512

start=2048, size=1048576, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI: boot"
start=1050624, size=61863903, type=A19D880F-05FC-4D3B-A006-743F0F84911E
"""
SETTLE = (('udevadm', 'settle', '--timeout=60'), None)


class Erase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.dev, self.by_id = self.root / 'dev', self.root / 'by-id'
        self.dev.mkdir()
        self.by_id.mkdir()
        self.device = self.plug('loop7', BY_ID)
        self.part1 = self.plug('loop7p1', f'{BY_ID}-part1')
        self.disk = self.loop('loop7')
        self.events, self.shown = [], None
        for patch in (
            mock.patch.dict(os.environ, {storage_jobs.TEST_FLAG: '1'}),
            mock.patch.object(storage_jobs, 'run', self.tool),
            mock.patch.object(storage_jobs, 'FACTS', {'min_bytes': 16 * GB}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def plug(self, name, link):
        device = self.dev / name
        device.touch()
        (self.by_id / link).symlink_to(device)
        return str(device)

    def loop(self, kname, serial=SERIAL, size=SIZE, **extra):
        return disk(kname, serial=serial, size=size, type='loop', path=str(self.dev / kname), **extra)

    def look(self, config):
        self.events.append(('look', config))
        # No by-id names here: the erase reads them from its own folder.
        return world(self.disk, by_id={})

    def tool(self, argv, stdin=None, timeout=600):
        self.events.append((tuple(argv), stdin))
        if argv[0] == 'lsblk':
            return json.dumps({'blockdevices': [self.shown]})
        if argv[:2] == ['sfdisk', '--dump']:
            return DUMP
        return ''

    def tell(self, fields):
        self.events.append(('tell', fields))

    def erase(self, by_id=BY_ID, serial=SERIAL, size=SIZE, **named):
        named.setdefault('look', self.look)
        return storage_jobs.erase(by_id, serial, size, folder=self.by_id, **named)

    def writes(self):
        return [e for e in self.events if isinstance(e[0], tuple)]

    def refused(self, **named):
        with self.assertRaises(storage_jobs.Refused) as raised:
            self.erase(**named)
        self.assertEqual(self.writes(), [], 'nothing is written to a refused disk')
        return raised.exception.code, str(raised.exception)


class DataLayoutTests(Erase):
    def test_each_write_comes_after_a_check_and_the_journal_hears_the_ids_first(self):
        self.disk = self.loop(
            'loop7',
            children=[
                part('loop7p1', fstype='ext4', uuid='old-1'),
                part('loop7p2', fstype='vfat', uuid='old-2'),
            ],
        )
        made = self.erase(label='neko-a-very-long-name', journal=self.tell)
        self.assertEqual(made, self.part1)
        told = [e[1] for e in self.events if e[0] == 'tell']
        partuuid, fsuuid = told[0]['partuuid'], told[0]['uuid']
        self.assertNotEqual(partuuid, fsuuid)
        look = ('look', {})
        self.assertEqual(
            self.events,
            [
                look,
                ('tell', {'phase': 'erase', 'partuuid': partuuid, 'uuid': fsuuid}),
                look,
                (('wipefs', '--all', '/dev/loop7p2'), None),
                look,
                (('wipefs', '--all', '/dev/loop7p1'), None),
                look,
                (('wipefs', '--all', self.device), None),
                ('tell', {'phase': 'partition', 'partuuid': partuuid, 'uuid': fsuuid}),
                look,
                (
                    ('sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', self.device),
                    f'label: gpt\ntype={storage_jobs.LINUX_DATA}, uuid={partuuid}\n',
                ),
                SETTLE,
                ('tell', {'phase': 'format', 'partuuid': partuuid, 'uuid': fsuuid, 'partition': self.part1}),
                look,
                (('mkfs.ext4', '-q', '-L', 'neko-a-very-long', '-U', fsuuid, self.part1), None),
                SETTLE,
            ],
        )

    def test_a_filesystem_on_the_whole_disk_is_wiped_once(self):
        self.disk = self.loop('loop7', fstype='ext4', uuid='old')
        self.erase()
        wiped = [argv for argv, _ in self.writes() if argv[0] == 'wipefs']
        self.assertEqual(wiped, [('wipefs', '--all', self.device)])

    def test_the_label_is_the_drives_own_when_none_is_given_and_no_journal_is_needed(self):
        self.erase()
        made = [argv for argv, _ in self.writes() if argv[0] == 'mkfs.ext4']
        self.assertEqual(made[0][:4], ('mkfs.ext4', '-q', '-L', 'neko-drive'))

    def test_the_smallest_size_is_the_releases_unless_one_is_given(self):
        self.disk = self.loop('loop7', size=8 * GB)
        self.assertEqual(self.refused(size=8 * GB), ('small', 'It is too small: at least 16 GB is needed.'))
        self.erase(size=8 * GB, minimum_bytes=0)
        self.assertTrue(self.writes())

    def test_an_unknown_layout_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.erase(layout='raid0')
        self.assertEqual(self.writes(), [])


class CheckAgainTests(Erase):
    def test_a_disk_not_plugged_in(self):
        for asked in ('ata-WDC_WD40EFRX_S-ZZZZ', '..', f'x/{BY_ID}', ''):
            with self.subTest(asked=asked):
                self.assertEqual(self.refused(by_id=asked), ('unknown', 'The disk is not plugged in any more.'))
                self.assertEqual(self.events, [], 'not even looked for')

    def test_only_loop_devices_while_testing_and_never_one_otherwise(self):
        words = 'Only loop devices are written to while testing, and never one otherwise.'
        with mock.patch.dict(os.environ, {storage_jobs.TEST_FLAG: '0'}):
            self.assertEqual(self.refused(), ('unknown', words))
        self.plug('sdc', 'ata-WDC_WD40EFRX_S-SDC')
        self.assertEqual(self.refused(by_id='ata-WDC_WD40EFRX_S-SDC'), ('unknown', words))

    def test_a_disk_that_is_not_there_when_looked_at_again(self):
        self.disk = self.loop('loop8')
        self.assertEqual(self.refused(), ('unknown', 'The disk could not be found again.'))

    def test_another_disk_where_this_one_was(self):
        words = 'Another disk is plugged in where this one was: nothing was erased.'
        self.assertEqual(self.refused(serial='S-BBBB'), ('unknown', words))
        self.assertEqual(self.refused(size=SIZE + 1), ('unknown', words))
        # A second name of the same device: the disk is told by the name it is shown by.
        (self.by_id / 'usb-WDC_S-AAAA').symlink_to(self.device)
        self.assertEqual(self.refused(by_id='usb-WDC_S-AAAA'), ('unknown', words))

    def test_the_refusals_are_asked_again(self):
        self.disk = self.loop('loop7', children=[part('loop7p1', mountpoints=['/media/x'])])
        self.assertEqual(self.refused(), ('mounted', 'It is in use: something on it is mounted.'))

    def test_a_disk_linux_holds_is_never_written(self):
        flags = []

        def held(path, flag, *rest):
            flags.append((path, flag))
            raise OSError(16, 'Device or resource busy')

        with mock.patch.object(storage_jobs.os, 'open', held):
            self.assertEqual(self.refused(), ('busy', 'Linux is using the disk right now: nothing was erased.'))
        self.assertEqual(flags, [(self.device, os.O_RDONLY | os.O_EXCL)])

    def test_the_disk_found_again_is_returned(self):
        row = storage_jobs.recheck(BY_ID, SERIAL, SIZE, 0, {}, self.by_id, self.look)
        self.assertEqual((row['by_id'], row['path'], row['dev']), (BY_ID, self.device, 'loop7'))


class MirrorLayoutTests(Erase):
    def test_a_member_named_by_nothing_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.erase(layout='mirror_member')
        self.assertEqual(self.writes(), [])

    def test_a_whole_disk_member_wipes_the_new_disk_only(self):
        self.shown = {'path': '/dev/sdb', 'type': 'disk', 'pkname': None, 'partn': None}
        made = self.erase(layout='mirror_member', like='/dev/sdb', journal=self.tell)
        self.assertEqual(made, self.device)
        look = ('look', {})
        self.assertEqual(
            self.events,
            [
                look,
                (('lsblk', '-J', '-o', 'PATH,TYPE,PKNAME,PARTN', '/dev/sdb'), None),
                ('tell', {'phase': 'erase', 'like': '/dev/sdb'}),
                look,
                (('wipefs', '--all', self.device), None),
            ],
        )

    def test_a_partition_member_gets_the_same_table_and_its_partition_is_returned(self):
        part2 = self.plug('loop7p2', f'{BY_ID}-part2')
        self.shown = {'path': '/dev/sdb2', 'type': 'part', 'pkname': 'sdb', 'partn': 2}
        made = self.erase(layout='mirror_member', like='/dev/sdb2', journal=self.tell)
        self.assertEqual(made, part2)
        look = ('look', {})
        self.assertEqual(
            self.events,
            [
                look,
                (('lsblk', '-J', '-o', 'PATH,TYPE,PKNAME,PARTN', '/dev/sdb2'), None),
                ('tell', {'phase': 'erase', 'like': '/dev/sdb2'}),
                look,
                (('wipefs', '--all', self.device), None),
                ('tell', {'phase': 'partition', 'like': '/dev/sdb2'}),
                look,
                (('sfdisk', '--dump', '/dev/sdb'), None),
                (('sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', self.device), TABLE),
                SETTLE,
            ],
        )

    def test_the_members_table_without_its_ids(self):
        self.assertEqual(storage_jobs.member_table('/dev/sdb'), TABLE)


class PartitionPathTests(Erase):
    def test_a_partition_udev_has_no_name_for_yet_is_found_by_its_device(self):
        (self.by_id / f'{BY_ID}-part1').unlink()
        self.assertEqual(storage_jobs.partition_path(BY_ID, 1, self.by_id), self.part1)
        self.assertEqual(self.events, [SETTLE])
        self.plug('sdc', 'ata-X_S-SDC')
        (self.dev / 'sdc3').touch()
        self.assertEqual(storage_jobs.partition_path('ata-X_S-SDC', 3, self.by_id), str(self.dev / 'sdc3'))

    def test_a_partition_that_does_not_appear(self):
        with self.assertRaises(RuntimeError) as raised:
            storage_jobs.partition_path(BY_ID, 4, self.by_id)
        self.assertEqual(str(raised.exception), f'The new partition 4 of {BY_ID} did not appear')

    def test_udevs_name_for_the_partition_comes_first(self):
        # A device-mapper or other odd name: only udev's link leads to it.
        (self.by_id / f'{BY_ID}-part1').unlink()
        (self.dev / 'loop7p1').unlink()
        odd = self.plug('dm-5', f'{BY_ID}-part1')
        self.assertEqual(storage_jobs.partition_path(BY_ID, 1, self.by_id), odd)


class DescribeTests(unittest.TestCase):
    """What a disk row says of a disk: the refusals and the erase read nothing else."""

    def test_a_disk_as_lsblk_shows_it(self):
        sdc = disk(
            'sdc',
            model=' WDC WD40EFRX  ',
            children=[part('sdc1', fstype='ext4', label='x', uuid='u', partuuid='p', size=7, fsused=3)],
        )
        row = storage_jobs.describe(world(sdc))[0]
        self.assertEqual(
            (row['model'], row['tran'], row['dev'], row['path']), ('WDC WD40EFRX', 'sata', 'sdc', '/dev/sdc')
        )
        self.assertEqual(
            row['filesystems'],
            [
                {
                    'path': '/dev/sdc1',
                    'fstype': 'ext4',
                    'label': 'x',
                    'uuid': 'u',
                    'partuuid': 'p',
                    'size': 7,
                    'fsused': 3,
                }
            ],
        )

    def test_a_row_without_a_kernel_name_or_path_or_model_takes_its_name(self):
        sdc = disk('sdc', model=None)
        del sdc['kname'], sdc['path']
        row = storage_jobs.describe(world(sdc, by_id={'sdc': ['ata-X_S']}))[0]
        self.assertEqual((row['dev'], row['path'], row['by_id'], row['model']), ('sdc', '/dev/sdc', 'ata-X_S', ''))

    def test_what_linux_built_on_a_device_is_found_by_its_kernel_name(self):
        # An open encrypted partition: lsblk names it by its mapper name, its kernel name is dm-0.
        crypt = {'name': 'luks-1', 'kname': 'dm-0', 'path': '/dev/mapper/luks-1', 'type': 'crypt', 'maj:min': '254:0'}
        sdc = disk('sdc', children=[part('sdc1', children=[crypt])])
        self.assertEqual(storage_jobs.describe(world(sdc, holders={'dm-0': ['dm-1']}))[0]['holders'], ['dm-1'])
        named = dict(part('sdc1'), kname=None)
        sdc = disk('sdc', children=[named])
        self.assertEqual(storage_jobs.describe(world(sdc, holders={'sdc1': ['md0']}))[0]['holders'], ['md0'])

    def test_sizes_in_the_words_a_box_uses(self):
        for size, words in (
            (10**12, '1 TB'),
            (10 * 10**9, '10 GB'),
            (10.5 * 10**9, '10 GB'),
            (5 * 10**6, '5 MB'),
            (999_999, '999999 bytes'),
            (None, '0 bytes'),
        ):
            with self.subTest(size=size):
                self.assertEqual(storage_jobs.size_words(size), words)


if __name__ == '__main__':
    unittest.main()
