"""1.0.1 storage from the dashboard, job D: a mirror's lost drive replaced, on loop devices
(root tier).

Run as root through tests/mirror-loop-tests.sh (it sets NEKOSERVER_STORAGE_TEST=1, under
which loop devices are the only disks and the only ones erase may write to). Never part of
the ordinary unit run: it makes loop devices, a Btrfs pool and an mdadm mirror. Each test
builds a two-drive mirror on partitioned loop devices (as setup lays them out), writes
files, takes one drive away, then runs mirror_add.run_job with a third loop device, as the
Drives page does, and checks the mirror is whole again with the same files."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mirror_add  # noqa: E402
import storage_jobs  # noqa: E402

MIB = 1024 * 1024
ROOT = os.name == 'posix' and os.geteuid() == 0 and os.environ.get(storage_jobs.TEST_FLAG) == '1'
# WSL's one kernel, shared by every distro, panics in md (md_handle_request, 2026-11-25):
# an mdadm array made there restarts every distro. Real md runs only on the VM.
WSL = 'microsoft' in os.uname().release.lower() if hasattr(os, 'uname') else False
MD_SKIP = 'a WSL kernel panics in md and takes every distro down: run on the VM'


def sh(*argv, stdin=None):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=True).stdout.strip()


def run(argv, timeout=None):
    """mirror_add's runner: (exit code, what it said), never a shell."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or 600, check=False)
    return done.returncode, done.stdout + done.stderr


@unittest.skipUnless(ROOT, 'makes loop devices: root, through tests/mirror-loop-tests.sh')
class MirrorOnLoopTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.dev = self.work / 'dev'
        self.by_id = self.dev / 'disk' / 'by-id'
        self.by_id.mkdir(parents=True)
        self.journal, self.cleared = [], []

    def loop(self, name, mib):
        """A loop device of `mib` MiB, named in by-id as test-<name> with its two partitions."""
        image = self.work / name
        sh('truncate', '-s', f'{mib}M', str(image))
        device = sh('losetup', '-f', '-P', '--show', str(image))
        self.addCleanup(lambda: subprocess.run(['losetup', '-d', device], capture_output=True, check=False))
        by_id = f'test-{name}'
        (self.by_id / by_id).symlink_to(device)
        for n in (1, 2):
            (self.by_id / f'{by_id}-part{n}').symlink_to(f'{device}p{n}')
        return {'by_id': by_id, 'serial': name, 'size_bytes': mib * MIB}, device

    def lose(self, name, device):
        """The drive is pulled: its loop device goes, and (as udev does) its by-id links."""
        sh('losetup', '-d', device)
        for link in (f'test-{name}', f'test-{name}-part1', f'test-{name}-part2'):
            (self.by_id / link).unlink()

    def partitioned(self, name, mib):
        disk, device = self.loop(name, mib)
        sh('sfdisk', '-q', device, stdin='label: gpt\n,1M\n,,\n')
        sh('udevadm', 'settle')
        return disk, device

    def context(self):
        return mirror_add.Context(
            run=run,
            erase=lambda *a, **k: storage_jobs.erase(*a, folder=self.by_id, **k),
            refused=storage_jobs.Refused,
            journal=lambda phase, committed, undo: self.journal.append(phase),
            progress=lambda *a, **k: None,
            clear=lambda: self.cleared.append(True),
            dev=str(self.dev),
            sleep=lambda seconds: time.sleep(1),
        )

    def files(self, folder, count=12):
        """Files of random data written in `folder`: {name: sha256}."""
        written = {}
        for n in range(count):
            data = os.urandom(512 * 1024 + n)
            (folder / f'file-{n}.bin').write_bytes(data)
            written[f'file-{n}.bin'] = hashlib.sha256(data).hexdigest()
        sh('sync')
        return written

    def same_files(self, folder, written):
        found = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('file-*.bin')}
        self.assertEqual(found, written)

    @unittest.skipUnless(shutil.which('mkfs.btrfs'), 'btrfs-progs is not installed')
    def test_a_btrfs_pool_that_lost_a_drive_is_whole_again_with_the_same_files(self):
        _, a = self.partitioned('a.img', 320)
        _, b = self.partitioned('b.img', 320)
        sh('mkfs.btrfs', '-q', '-f', '-d', 'raid1', '-m', 'raid1', f'{a}p2', f'{b}p2')
        uuid = sh('blkid', '-s', 'UUID', '-o', 'value', f'{a}p2')
        pool = self.work / 'pool'
        pool.mkdir()
        sh('mount', f'{a}p2', str(pool))
        written = self.files(pool)
        sh('umount', str(pool))
        # The drive is lost: its loop device goes, and the pool opens degraded on the one left.
        self.lose('b.img', b)
        subprocess.run(['btrfs', 'device', 'scan', '--forget'], check=False)
        sh('mount', '-o', 'degraded', f'{a}p2', str(pool))
        self.addCleanup(lambda: subprocess.run(['umount', str(pool)], check=False))
        written |= self.files(pool, 3)  # written while the drive was missing: balanced after
        array = mirror_add.find(f'btrfs-{uuid}', run, '/sys/block')
        self.assertTrue(array['degraded'])
        self.assertIn(array['id'], [x['id'] for x in mirror_add.degraded(run, '/sys/block')])
        disk, c = self.loop('c.img', 340)
        result = mirror_add.run_job({'array': array['id'], 'disk': disk}, self.context())
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('your data is on both drives again', result['message'])
        self.assertEqual(self.journal, ['erase', 'add', 'rebuild'])
        self.assertTrue(self.cleared, 'the journal goes: the page is never held')
        shown = sh('btrfs', 'filesystem', 'show', '--raw', uuid)
        self.assertNotIn('missing', shown.lower())
        self.assertIn(f'{c}p2', shown)
        self.assertFalse(mirror_add.find(array['id'], run, '/sys/block')['degraded'])
        self.same_files(pool, written)
        self.assertEqual(run(['btrfs', 'scrub', 'start', '-B', str(pool)])[0], 0)

    @unittest.skipIf(WSL, MD_SKIP)
    @unittest.skipUnless(shutil.which('mdadm'), 'mdadm is not installed')
    def test_an_mdadm_mirror_that_lost_a_drive_takes_the_new_one_and_resyncs(self):
        _, a = self.partitioned('a.img', 96)
        _, b = self.partitioned('b.img', 96)
        name = 'nekotest'
        sh(
            'mdadm',
            '--create',
            f'/dev/md/{name}',
            '--level=1',
            '--raid-devices=2',
            '--metadata=1.2',
            '--run',
            '--assume-clean',
            '--homehost=any',
            f'{a}p2',
            f'{b}p2',
        )
        md = Path(os.path.realpath(f'/dev/md/{name}')).name
        self.addCleanup(lambda: subprocess.run(['mdadm', '--stop', f'/dev/{md}'], check=False))
        sh('mkfs.ext4', '-q', f'/dev/{md}')
        mounted = self.work / 'md'
        mounted.mkdir()
        sh('mount', f'/dev/{md}', str(mounted))
        self.addCleanup(lambda: subprocess.run(['umount', str(mounted)], check=False))
        written = self.files(mounted)
        # The drive is lost.
        sh('mdadm', f'/dev/{md}', '--fail', f'{b}p2')
        sh('mdadm', f'/dev/{md}', '--remove', f'{b}p2')
        self.lose('b.img', b)
        array = mirror_add.find(f'md-{md}', run, '/sys/block')
        self.assertTrue(array and array['degraded'], array)
        disk, c = self.loop('c.img', 112)
        self.addCleanup(lambda: subprocess.run(['mdadm', '--stop', f'/dev/{md}'], capture_output=True, check=False))
        result = mirror_add.run_job({'array': array['id'], 'disk': disk}, self.context())
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(self.journal, ['erase', 'add'])
        for _ in range(120):  # the resync of a small mirror: seconds
            state = Path(f'/sys/block/{md}/md/sync_action').read_text().strip()
            missing = Path(f'/sys/block/{md}/md/degraded').read_text().strip()
            if state in mirror_add.MD_QUIET and missing == '0':
                break
            time.sleep(1)
        self.assertEqual(Path(f'/sys/block/{md}/md/degraded').read_text().strip(), '0')
        self.assertIn(f'{Path(c).name}p2', sh('mdadm', '--detail', f'/dev/{md}'))
        sh('sync')
        sh('sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches')
        self.same_files(mounted, written)

    @unittest.skipIf(WSL, MD_SKIP)
    @unittest.skipUnless(shutil.which('mdadm'), 'mdadm is not installed')
    def test_a_replacement_too_small_for_the_mirror_is_refused_before_anything_is_written(self):
        _, a = self.partitioned('a.img', 96)
        _, b = self.partitioned('b.img', 96)
        name = 'nekosmall'
        sh(
            'mdadm',
            '--create',
            f'/dev/md/{name}',
            '--level=1',
            '--raid-devices=2',
            '--metadata=1.2',
            '--run',
            '--assume-clean',
            '--homehost=any',
            f'{a}p2',
            f'{b}p2',
        )
        md = Path(os.path.realpath(f'/dev/md/{name}')).name
        self.addCleanup(lambda: subprocess.run(['mdadm', '--stop', f'/dev/{md}'], check=False))
        sh('mdadm', f'/dev/{md}', '--fail', f'{b}p2')
        sh('mdadm', f'/dev/{md}', '--remove', f'{b}p2')
        array = mirror_add.find(f'md-{md}', run, '/sys/block')
        disk, c = self.loop('c.img', 48)
        sh('mkfs.ext4', '-q', '-L', 'keep-me', c)
        result = mirror_add.run_job({'array': array['id'], 'disk': disk}, self.context())
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(sh('blkid', '-s', 'LABEL', '-o', 'value', c), 'keep-me')
        self.assertEqual(Path(f'/sys/block/{md}/md/degraded').read_text().strip(), '1')


if __name__ == '__main__':
    unittest.main()
