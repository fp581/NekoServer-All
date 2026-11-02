"""migrate.py: the SSD or the HDD moved to a new, bigger drive while both are connected.
The old drive is only ever read; a copy that does not match, file for file by checksum,
stops the move with the apps back on the old drive."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import migrate  # noqa: E402

GB = 10**9


def row(name, uuid, fstype, size, disk, mounts=()):
    return {'name': name, 'uuid': uuid, 'fstype': fstype, 'size': size, 'pkname': disk, 'mountpoints': list(mounts)}


ROWS = [
    row('/dev/nvme0n1p2', 'os-0001', 'ext4', 500 * GB, '/dev/nvme0n1', ['/']),
    row('/dev/sda1', 'ssd-0001', 'ext4', 1000 * GB, '/dev/sda', ['/srv/nekoserver']),
    row('/dev/sdb1', 'hdd-0001', 'ext4', 4000 * GB, '/dev/sdb', ['/mnt/nekoserver-media']),
    row('/dev/sdb2', 'hdd-spare', 'ext4', 4000 * GB, '/dev/sdb'),  # the old HDD's own disk
    row('/dev/sdc1', 'newbig-0001', 'ext4', 8000 * GB, '/dev/sdc'),
    row('/dev/sdd1', 'newsmall-0001', 'xfs', 2000 * GB, '/dev/sdd'),
    row('/dev/sde1', 'newbusy-0001', 'btrfs', 8000 * GB, '/dev/sde', ['/media/usb']),
    row('/dev/sdf1', 'newfat-0001', 'vfat', 8000 * GB, '/dev/sdf'),
]
CONFIG = {
    'data_mount': '/srv/nekoserver',
    'media_mount': '/mnt/nekoserver-media',
    'storage': {
        'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'},
        'media_hdd': {'uuid': 'hdd-0001', 'fstype': 'ext4'},
    },
}


class TargetTests(unittest.TestCase):
    def test_only_an_empty_big_enough_filesystem_on_a_disk_of_its_own_is_offered(self):
        found, left_out = migrate.targets(CONFIG, 'media_hdd', ROWS, used=3000 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001'])
        said = ' '.join(left_out)
        self.assertIn('/dev/sdd1: 2000 GB is less than the 3150 GB needed', said)
        self.assertIn('/dev/sde1: mounted at /media/usb', said)
        # Never the system disk, the old drive's own disk, the other drive's, nor vfat.
        for name in ('nvme0n1p2', 'sdb2', 'sda1', 'sdf1'):
            self.assertNotIn(name, said)
        found, _ = migrate.targets(CONFIG, 'media_hdd', ROWS, used=1500 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001', 'newsmall-0001'])


@unittest.skipUnless(shutil.which('rsync'), 'rsync is not installed')
class CopyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.old, self.new = Path(tmp.name) / 'old', Path(tmp.name) / 'new'
        (self.old / 'photos').mkdir(parents=True)
        self.new.mkdir()
        (self.old / 'photos/a.jpg').write_bytes(b'a' * 1000)
        os.link(self.old / 'photos/a.jpg', self.old / 'photos/a-again.jpg')
        (self.old / 'secret').write_text('x')
        os.chmod(self.old / 'secret', 0o600)

    def test_the_copy_keeps_links_and_permissions_and_is_checked_by_checksum(self):
        migrate.copy(self.old, self.new, out=lambda _: None)
        migrate.copy(self.old, self.new, final=True, out=lambda _: None)
        self.assertEqual(migrate.differences(self.old, self.new), [])
        self.assertEqual(os.stat(self.new / 'photos/a.jpg').st_ino, os.stat(self.new / 'photos/a-again.jpg').st_ino)
        self.assertEqual(os.stat(self.new / 'secret').st_mode & 0o777, 0o600)
        # A file that differs in content alone (same size and time) is found.
        stamp = os.stat(self.new / 'secret')
        (self.new / 'secret').write_text('y')
        os.utime(self.new / 'secret', ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertTrue(migrate.differences(self.old, self.new))
        # The last pass removes what was deleted on the old drive meanwhile.
        (self.new / 'stale').write_text('gone on the old drive')
        migrate.copy(self.old, self.new, final=True, out=lambda _: None)
        self.assertFalse((self.new / 'stale').exists())


class MoveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.journal = self.dir / 'migrate.json'
        self.fstab = self.dir / 'fstab'
        self.fstab.write_text(
            'UUID=os-0001 / ext4 defaults 0 1\n'
            'UUID=hdd-0001 /mnt/nekoserver-media ext4 defaults,nofail,x-systemd.device-timeout=90s 0 2\n'
        )
        self.settings = self.dir / 'my-config.json'
        self.settings.write_text(json.dumps(CONFIG))
        self.staging = self.dir / 'staging'
        self.staging.mkdir()
        self.calls, self.said = [], []
        for target, value in (
            ('settings_path', lambda p: Path(p)),
            ('mount_path', lambda p: Path(p)),
            ('nothing_at_work', lambda: None),
            ('targets', lambda config, drive: ([('newbig-0001', 'ext4', 8000 * GB, '/dev/sdc1')], [])),
            ('mount_staging', lambda uuid, fstype, staging: staging),
            ('copy', lambda source, target, final=False, out=print: self.calls.append(('copy', final))),
            ('stop_writers', self.stop),
            ('start_again', lambda stopped, out=print: self.calls.append(('start', tuple(stopped)))),
            ('run', lambda argv, *a, **k: self.calls.append(tuple(argv)) or mock.Mock(returncode=0, stdout='')),
            ('setup_again', lambda *a, **k: self.calls.append(('setup',))),
        ):
            patcher = mock.patch.object(migrate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ismount = mock.patch.object(migrate.os.path, 'ismount', return_value=False)
        ismount.start()
        self.addCleanup(ismount.stop)

    def stop(self, journal):
        migrate.record(journal, phase='stopped', stopped=['docker.service', 'nekoserver-app-backup.timer'])
        self.calls.append(('stop',))
        return ['docker.service', 'nekoserver-app-backup.timer']

    def move(self):
        return migrate.move(
            self.settings, 'media_hdd', 'newbig-0001', self.said.append, self.journal, self.staging, self.fstab
        )

    def test_a_good_copy_switches_to_the_new_drive_and_keeps_the_old_one(self):
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds.index('copy'), 0)  # the first pass while the apps run
        self.assertLess(kinds.index('stop'), self.calls.index(('copy', True)))
        self.assertLess(self.calls.index(('copy', True)), kinds.index('setup'))
        fstab = self.fstab.read_text()
        self.assertIn('UUID=newbig-0001 /mnt/nekoserver-media ext4 defaults,nofail', fstab)
        self.assertNotIn('UUID=hdd-0001', fstab)
        kept = list(self.dir.glob('fstab.neko-before-move-*'))
        self.assertEqual(len(kept), 1)
        self.assertIn('UUID=hdd-0001', kept[0].read_text())
        saved = json.loads(self.settings.read_text())['storage']['media_hdd']
        self.assertEqual(
            saved, {'uuid': 'newbig-0001', 'fstype': 'ext4', 'previous_drive': {'uuid': 'hdd-0001', 'fstype': 'ext4'}}
        )
        self.assertIn(('start', ('docker.service', 'nekoserver-app-backup.timer')), self.calls)
        self.assertFalse(self.journal.exists())
        # The old drive is never written to: rsync only ever reads it.
        self.assertNotIn('--remove-source-files', ' '.join(migrate.RSYNC))

    def test_a_copy_that_does_not_match_changes_nothing(self):
        with mock.patch.object(migrate, 'differences', return_value=['>fcsT...... photos/a.jpg']):
            self.assertEqual(self.move(), 1)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        self.assertEqual(json.loads(self.settings.read_text()), CONFIG)
        self.assertIn(('start', ('docker.service', 'nekoserver-app-backup.timer')), self.calls)
        self.assertNotIn(('setup',), self.calls)
        self.assertFalse(self.journal.exists())
        self.assertIn('the old drive, unchanged', self.said[-1])

    def test_a_restart_before_the_switch_changes_nothing_and_after_it_finishes_it(self):
        migrate.record(self.journal, phase='copying', stopped=['docker.service'], drive='media_hdd')
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertIn(('start', ('docker.service',)), self.calls)
        self.assertFalse(self.journal.exists())
        cut_off = {
            'drive': 'media_hdd',
            'mount': '/mnt/nekoserver-media',
            'old': {'uuid': 'hdd-0001', 'fstype': 'ext4'},
            'new': {'uuid': 'newbig-0001', 'fstype': 'ext4'},
            'stopped': [],
        }
        migrate.record(self.journal, phase='switching', **cut_off)
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertIn('UUID=newbig-0001', self.fstab.read_text())
        # Cut off again after the fstab was switched: finishing it gives the same result.
        migrate.record(self.journal, phase='setup', **cut_off)
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertEqual(self.fstab.read_text().count('UUID=newbig-0001'), 1)

    def test_the_settings_or_the_tool_on_the_drive_being_moved_are_refused(self):
        settings = Path(CONFIG['media_mount']) / 'my-config.json'
        with mock.patch.object(migrate, 'load_settings', return_value=(settings, json.loads(json.dumps(CONFIG)))):
            self.assertEqual(self.move(), 2)
        self.assertIn('Run this from the NekoServer folder you downloaded', self.said[-1])
        self.assertEqual(self.calls, [])

    def test_the_fstab_line_is_switched_only_when_it_is_the_old_drives(self):
        self.fstab.write_text(self.fstab.read_text() + 'UUID=other /mnt/nekoserver-media ext4 defaults 0 2\n')
        with self.assertRaisesRegex(RuntimeError, 'exactly one line'):
            migrate.switch_fstab('/mnt/nekoserver-media', 'hdd-0001', 'newbig-0001', 'ext4', self.fstab)


if __name__ == '__main__':
    unittest.main()
