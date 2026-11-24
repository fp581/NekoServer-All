"""1.0.1 storage from the dashboard: the one erase entry point on loop devices (root tier).

Run as root through tests/storage-loop-tests.sh (it sets NEKOSERVER_STORAGE_TEST=1, under
which loop devices are the only disks and the only ones erase may write to). Never part of
the ordinary unit run: it makes loop devices."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402

MIB = 1024 * 1024
ROOT = os.name == 'posix' and os.geteuid() == 0 and os.environ.get(storage_jobs.TEST_FLAG) == '1'


def sh(*argv, stdin=None):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(ROOT, 'makes loop devices: root, through tests/storage-loop-tests.sh')
class EraseOnLoopTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.by_id = self.work / 'by-id'
        self.by_id.mkdir()

    def loop(self, name, mib=64):
        image = self.work / name
        sh('truncate', '-s', f'{mib}M', str(image))
        device = sh('losetup', '-f', '-P', '--show', str(image))
        self.addCleanup(lambda: subprocess.run(['losetup', '-d', device], check=False))
        (self.by_id / f'test-{name}').symlink_to(device)
        return f'test-{name}', name, mib * MIB, device

    def erase(self, by_id, serial, size, **extra):
        return storage_jobs.erase(by_id, serial, size, folder=self.by_id, minimum_bytes=16 * MIB, **extra)

    def test_a_blank_disk_gets_one_ext4_partition_with_the_ids_told_before_each_write(self):
        by_id, serial, size, device = self.loop('blank.img')
        told = []
        part = self.erase(by_id, serial, size, label='neko-d3', journal=told.append)
        self.assertEqual(part, f'{device}p1')
        self.assertEqual([t['phase'] for t in told], ['erase', 'partition', 'format'])
        self.assertEqual(sh('blkid', '-s', 'TYPE', '-o', 'value', part), 'ext4')
        self.assertEqual(sh('blkid', '-s', 'UUID', '-o', 'value', part), told[0]['uuid'])
        self.assertEqual(sh('blkid', '-s', 'PARTUUID', '-o', 'value', part), told[0]['partuuid'])
        self.assertEqual(sh('blkid', '-s', 'LABEL', '-o', 'value', part), 'neko-d3')

    def test_a_disk_with_a_filesystem_and_a_partition_table_is_erased_whole(self):
        by_id, serial, size, device = self.loop('ntfs-like.img')
        sh('sfdisk', device, stdin='label: dos\n,32M,7\n,,83\n')
        sh('udevadm', 'settle')
        sh('mkfs.ext4', '-q', '-L', 'old-photos', f'{device}p2')
        part = self.erase(by_id, serial, size)
        self.assertEqual(sh('lsblk', '-n', '-o', 'NAME', device).count('loop'), 2)  # the disk and one partition
        self.assertNotIn('old-photos', sh('blkid', '-o', 'value', part))

    def test_a_mounted_disk_is_refused_and_left_as_it_was(self):
        by_id, serial, size, device = self.loop('mounted.img')
        sh('mkfs.ext4', '-q', '-L', 'in-use', device)
        mounted = self.work / 'mnt'
        mounted.mkdir()
        sh('mount', device, str(mounted))
        self.addCleanup(lambda: subprocess.run(['umount', str(mounted)], check=False))
        with self.assertRaises(storage_jobs.Refused) as refused:
            self.erase(by_id, serial, size)
        self.assertEqual(refused.exception.code, 'mounted')
        self.assertEqual(sh('blkid', '-s', 'LABEL', '-o', 'value', device), 'in-use')

    def test_another_disk_where_the_person_chose_one_is_refused(self):
        by_id, serial, size, device = self.loop('chosen.img')
        sh('mkfs.ext4', '-q', '-L', 'keep', device)
        for wrong in ((by_id, 'other.img', size), (by_id, serial, size + MIB)):
            with self.assertRaises(storage_jobs.Refused) as refused:
                self.erase(*wrong)
            self.assertEqual(refused.exception.code, 'unknown')
        self.assertEqual(sh('blkid', '-s', 'LABEL', '-o', 'value', device), 'keep')

    def test_a_disk_too_small_is_refused(self):
        by_id, serial, size, _ = self.loop('small.img', mib=8)
        with self.assertRaises(storage_jobs.Refused) as refused:
            self.erase(by_id, serial, size)
        self.assertEqual(refused.exception.code, 'small')

    def test_a_mirror_member_is_laid_out_like_the_present_one(self):
        _, _, _, present = self.loop('member.img')
        sh('sfdisk', present, stdin='label: gpt\n,1M\n,,R\n')
        sh('udevadm', 'settle')
        by_id, serial, size, device = self.loop('replacement.img', mib=80)
        part = self.erase(by_id, serial, size, layout='mirror_member', like=f'{present}p2')
        self.assertEqual(part, f'{device}p2')
        sizes = [sh('blockdev', '--getsize64', p) for p in (f'{present}p1', f'{device}p1')]
        self.assertEqual(sizes[0], sizes[1])
        present_ids = sh('blkid', '-s', 'PARTUUID', '-o', 'value', f'{present}p2')
        self.assertNotEqual(sh('blkid', '-s', 'PARTUUID', '-o', 'value', part), present_ids)


class NeverARealDiskTests(unittest.TestCase):
    def test_while_testing_anything_but_a_loop_device_is_refused_before_a_look(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'ata-REAL_DISK_1234').symlink_to('/dev/null')
            looked = []
            os.environ[storage_jobs.TEST_FLAG] = '1'
            try:
                with self.assertRaises(storage_jobs.Refused):
                    storage_jobs.erase(
                        'ata-REAL_DISK_1234', '1234', 1, folder=Path(folder), minimum_bytes=0, look=looked.append
                    )
            finally:
                os.environ.pop(storage_jobs.TEST_FLAG)
        self.assertEqual(looked, [])


if __name__ == '__main__':
    unittest.main()
