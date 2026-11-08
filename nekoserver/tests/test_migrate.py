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
    def test_a_drive_setup_would_refuse_is_never_offered(self):
        """The 0.46.1 audit: an encrypted drive opened by hand (not in /etc/crypttab) or a
        loop device was offered; after the switch setup refused it, and the move could go
        neither on nor back."""
        rows = [*ROWS, row('/dev/mapper/newcrypt', 'crypt-0001', 'ext4', 8000 * GB, '/dev/sdh'),
                row('/dev/loop3', 'loop-0001', 'ext4', 8000 * GB, None)]  # fmt: skip
        inventory = {'blockdevices': [
            {'name': 'sdc', 'type': 'disk', 'children': [{'name': 'sdc1', 'type': 'part', 'uuid': 'newbig-0001', 'fstype': 'ext4', 'size': '8T'}]},
            {'name': 'sdh', 'type': 'disk', 'children': [{'name': 'sdh1', 'type': 'part', 'fstype': 'crypto_LUKS', 'children': [
                {'name': 'newcrypt', 'type': 'crypt', 'uuid': 'crypt-0001', 'fstype': 'ext4', 'size': '8T'}]}]},
            {'name': 'loop3', 'type': 'loop', 'uuid': 'loop-0001', 'fstype': 'ext4', 'size': '8T'},
        ]}  # fmt: skip
        with mock.patch('modules.storage.unlocked_at_boot', return_value=False):
            found, left_out = migrate.targets(CONFIG, 'media_hdd', rows, used=1000 * GB, inventory=inventory)
        self.assertEqual([t[0] for t in found], ['newbig-0001'])
        said = ' '.join(left_out)
        self.assertIn('/dev/mapper/newcrypt', said)
        self.assertIn('/etc/crypttab', said)
        self.assertIn('/dev/loop3', said)

    def test_the_backup_drive_and_a_nas_disk_for_the_ssd_are_never_offered(self):
        """The 0.46.0 review: the move offered the USB backup drive, and a NAS disk for
        the SSD, both of which setup refuses, so the trouble came after the copy."""
        rows = [*ROWS, {**row('/dev/sdg', None, None, 8000 * GB, None), 'type': 'disk', 'tran': 'iscsi'}]
        rows.append(row('/dev/sdg1', 'lun-0001', 'ext4', 8000 * GB, '/dev/sdg'))
        config = {**CONFIG, 'photos': {'external_backup_uuid': 'newbig-0001'}}
        found, left_out = migrate.targets(config, 'data_ssd', rows, used=500 * GB)
        self.assertNotIn('newbig-0001', [t[0] for t in found])
        self.assertNotIn('lun-0001', [t[0] for t in found])
        said = ' '.join(left_out)
        self.assertIn('/dev/sdc1: the USB backup drive', said)
        self.assertIn('/dev/sdg1: a disk on the network storage box', said)
        # The media drive may move to the NAS.
        found, _ = migrate.targets(config, 'media_hdd', rows, used=500 * GB)
        self.assertIn('lun-0001', [t[0] for t in found])

    def test_only_an_empty_big_enough_filesystem_on_a_disk_of_its_own_is_offered(self):
        found, left_out = migrate.targets(CONFIG, 'media_hdd', ROWS, used=3000 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001'])
        said = ' '.join(left_out)
        self.assertIn('/dev/sdd1: 2000 GB is less than the 3150 GB needed', said)
        self.assertIn('/dev/sde1: mounted at /media/usb', said)
        # Never the system disk, the old drive's own disk, the other drive's, nor vfat.
        for name in ('nvme0n1p2', 'sdb2', 'sda1', 'sdf1'):
            self.assertNotIn(name, said)
        # Found in the lab: the drive list asks systemctl for "nekoserver-*", which the
        # argument check must let through; an unmounted drive's folder is said, not a crash.
        self.assertTrue(migrate.ARGUMENT.fullmatch('nekoserver-*'))
        self.assertFalse(migrate.ARGUMENT.fullmatch('a;b'))
        said = []
        with (
            mock.patch.object(migrate, 'load_settings', return_value=(Path('x'), CONFIG)),
            mock.patch.object(migrate, 'lsblk', return_value=ROWS),
            mock.patch.object(migrate, 'used_bytes', side_effect=FileNotFoundError(2, 'No such file or directory')),
        ):
            self.assertEqual(migrate.list_targets('x', said.append), 0)
        self.assertIn('is it mounted?', said[0])
        found, _ = migrate.targets(CONFIG, 'media_hdd', ROWS, used=1500 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001', 'newsmall-0001'])


class StopTests(unittest.TestCase):
    """What a move stops, with systemctl answering as it does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal = Path(tmp.name) / 'migrate.json'
        self.units = {'active': ['nekoserver-app-backup.timer', 'nekoserver-dashboard.service'], 'activating': []}
        self.calls = []

        def run(argv, *args, **kwargs):
            self.calls.append(argv)
            if argv[:2] == ['systemctl', 'list-units']:
                states = argv[4].removeprefix('--state=').split(',')
                found = [u for state in states for u in self.units.get(state, [])]
                return mock.Mock(returncode=0, stdout=''.join(u + ' loaded x y\n' for u in found))
            if argv[:2] == ['systemctl', 'stop']:
                for state in self.units.values():
                    if argv[2] in state:
                        state.remove(argv[2])
            return mock.Mock(returncode=1, stdout='')  # is-active: Docker and Samba are not running

        patcher = mock.patch.object(migrate, 'run', run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_backup_at_work_stops_the_move_instead_of_being_cut_off(self):
        self.units['activating'] = ['nekoserver-app-backup.service']  # a oneshot job running
        with self.assertRaisesRegex(RuntimeError, 'nekoserver-app-backup.service is at work'):
            migrate.stop_writers(self.journal)
        self.assertNotIn(['systemctl', 'stop', 'nekoserver-app-backup.service'], self.calls)
        # The timers it stopped are noted, so they start again.
        self.assertEqual(migrate.read(self.journal)['stopped'], ['nekoserver-app-backup.timer'])

    def test_a_timer_that_does_not_stop_stops_the_move(self):
        """The review of 0.47.9: a timer whose stop failed was passed over (the final check
        left timers out), so a backup could start during the quiet copy and the switch."""
        self.units['active'].append('nekoserver-photo-backup.timer')
        real = migrate.run

        def run(argv, *args, **kwargs):
            if argv[:3] == ['systemctl', 'stop', 'nekoserver-photo-backup.timer']:
                return mock.Mock(returncode=1, stdout='')  # it stays active
            return real(argv, *args, **kwargs)

        with mock.patch.object(migrate, 'run', run):
            with self.assertRaisesRegex(RuntimeError, 'These did not stop: nekoserver-photo-backup.timer'):
                migrate.stop_writers(self.journal)
        self.assertNotIn(['systemctl', 'stop', 'nekoserver-dashboard.service'], self.calls)
        self.assertEqual(
            migrate.read(self.journal)['stopped'], ['nekoserver-app-backup.timer', 'nekoserver-photo-backup.timer']
        )

    def test_with_no_job_at_work_everything_stops_and_is_noted(self):
        self.assertEqual(
            migrate.stop_writers(self.journal), ['nekoserver-app-backup.timer', 'nekoserver-dashboard.service']
        )
        self.assertEqual(
            migrate.read(self.journal)['stopped'], ['nekoserver-app-backup.timer', 'nekoserver-dashboard.service']
        )


class LayeredDiskTests(unittest.TestCase):
    def test_a_partition_beside_an_encrypted_system_is_never_offered(self):
        # Ubuntu's "encrypt the disk": / is /dev/mapper/root, on the LUKS partition
        # nvme0n1p3; nvme0n1p4 is a spare partition of that same disk.
        def part(name, parent, kind, uuid=None, fstype=None, mounts=(), size=8000 * GB):
            return {
                'name': name,
                'pkname': parent,
                'type': kind,
                'uuid': uuid,
                'fstype': fstype,
                'size': size,
                'mountpoints': list(mounts),
            }

        rows = [
            part('/dev/nvme0n1', None, 'disk'),
            part('/dev/nvme0n1p3', '/dev/nvme0n1', 'part', 'luks-1', 'crypto_LUKS'),
            part('/dev/mapper/root', '/dev/nvme0n1p3', 'crypt', 'os-0001', 'ext4', ['/']),
            part('/dev/nvme0n1p4', '/dev/nvme0n1', 'part', 'spare-0001', 'ext4'),
            *ROWS[1:4],
            part('/dev/sdc', None, 'disk'),
            part('/dev/sdc1', '/dev/sdc', 'part', 'luks-2', 'crypto_LUKS'),
            part('/dev/mapper/big', '/dev/sdc1', 'crypt', 'newbig-0001', 'ext4'),
        ]
        found, left_out = migrate.targets(CONFIG, 'media_hdd', rows, used=1000 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001'])  # an encrypted new drive is fine
        self.assertNotIn('nvme0n1p4', ' '.join(left_out))
        self.assertEqual(migrate.physical_disks(rows, '/dev/mapper/root'), {'/dev/nvme0n1'})

    def test_a_boot_disk_and_every_member_of_a_btrfs_system_are_never_offered(self):
        def part(name, parent, kind, uuid=None, fstype=None, mounts=(), size=8000 * GB):
            return {
                'name': name,
                'pkname': parent,
                'type': kind,
                'uuid': uuid,
                'fstype': fstype,
                'size': size,
                'mountpoints': list(mounts),
            }

        rows = [
            # / is a Btrfs pool over two disks; lsblk shows its folder on the first only.
            part('/dev/sdx', None, 'disk'),
            part('/dev/sdx1', '/dev/sdx', 'part', 'pool-0001', 'btrfs', ['/']),
            part('/dev/sdy', None, 'disk'),
            part('/dev/sdy1', '/dev/sdy', 'part', 'pool-0001', 'btrfs'),
            part('/dev/sdy2', '/dev/sdy', 'part', 'spare-on-pool', 'ext4'),
            # /boot/efi on a disk of its own, with a spare partition beside it.
            part('/dev/sdz', None, 'disk'),
            part('/dev/sdz1', '/dev/sdz', 'part', 'efi-0001', 'vfat', ['/boot/efi']),
            part('/dev/sdz2', '/dev/sdz', 'part', 'spare-on-boot', 'ext4'),
            *ROWS[1:4],
            part('/dev/sdc', None, 'disk'),
            part('/dev/sdc1', '/dev/sdc', 'part', 'newbig-0001', 'ext4'),
        ]
        found, left_out = migrate.targets(CONFIG, 'media_hdd', rows, used=1000 * GB)
        self.assertEqual([t[0] for t in found], ['newbig-0001'])
        self.assertFalse({'/dev/sdy2', '/dev/sdz2'} & {t[3] for t in found})
        self.assertEqual(migrate.system_disks(rows), {'/dev/sdx', '/dev/sdy', '/dev/sdz'})


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
        # What findmnt says is mounted where (the tests change it to unplug a drive).
        self.mounted = {
            '/mnt/nekoserver-media': ('hdd-0001', 'ext4', ''),
            str(self.staging): ('newbig-0001', 'ext4', ''),
        }
        self.start_fails = set()
        self.busy = None
        self.problem = None  # what the check after setup finds
        self.checked = []
        for target, value in (
            ('OPERATION_LOCK', self.dir / 'operation.lock'),
            ('steps_working', lambda config: ['storage']),
            ('server_check', lambda config, before: self.checked.append(before) or self.problem),
            ('mounted_identity', lambda folder: self.mounted.get(str(folder))),
            ('settings_path', lambda p: Path(p)),
            ('mount_path', lambda p: Path(p)),
            ('nothing_at_work', lambda **kwargs: self.busy),
            ('targets', lambda config, drive: ([('newbig-0001', 'ext4', 8000 * GB, '/dev/sdc1')], [])),
            ('mount_staging', lambda uuid, fstype, staging: staging),
            ('copy', lambda source, target, final=False, out=print: self.calls.append(('copy', final))),
            ('stop_writers', self.stop),
            ('start_again', self.start),
            ('run', self.command),
            ('setup_again', lambda *a, **k: self.calls.append(('setup',))),
        ):
            patcher = mock.patch.object(migrate, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ismount = mock.patch.object(migrate.os.path, 'ismount', return_value=False)
        ismount.start()
        self.addCleanup(ismount.stop)

    def command(self, argv, *args, **kwargs):
        """A stand-in for commands: `mount <folder>` mounts what /etc/fstab's active line
        names, as Linux would; `umount` unmounts."""
        self.calls.append(tuple(argv))
        if argv[0] == 'umount':
            self.mounted.pop(argv[1], None)
        if argv[0] == 'mount' and len(argv) == 2:
            lines = self.fstab.read_text().splitlines()
            found = migrate.mount_lines(lines, argv[1])
            if found:
                fields = lines[found[0]].split()
                subvolume = next((o[7:] for o in fields[3].split(',') if o.startswith('subvol=')), '')
                self.mounted[argv[1]] = (fields[0].removeprefix('UUID='), fields[2], subvolume)
        return mock.Mock(returncode=0, stdout='')

    def start(self, stopped, out=print):
        self.calls.append(('start', tuple(stopped or [])))
        return [u for u in stopped or [] if u in self.start_fails]

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

    def test_a_subvolume_the_settings_name_is_never_swapped_for_the_mounted_one(self):
        """The review of 0.47.2: the move took the subvolume that was mounted, even when the
        settings named another one, so another tree could be copied."""
        config = json.loads(json.dumps(CONFIG))
        config['storage']['media_hdd']['subvolume'] = '/@media'
        self.settings.write_text(json.dumps(config))
        self.mounted['/mnt/nekoserver-media'] = ('hdd-0001', 'ext4', '/@other')
        self.assertEqual(self.move(), 2)
        self.assertFalse([c for c in self.calls if c[0] in ('copy', 'stop')])
        self.assertIn('settings name /@media', self.said[-1])
        # The one the settings name: the move goes ahead.
        self.mounted['/mnt/nekoserver-media'] = ('hdd-0001', 'ext4', '/@media')
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)

    def test_a_staging_folder_in_use_already_keeps_what_is_mounted_there(self):
        """The review of 0.47.2: the move refused a staging folder already in use, then its
        clean-up unmounted it anyway."""
        self.mounted[str(self.staging)] = ('someone-else', 'ext4', '')

        def in_use(uuid, fstype, staging):
            raise RuntimeError(f'{staging} is in use already; unmount it first')

        with mock.patch.object(migrate, 'mount_staging', side_effect=in_use):
            self.assertEqual(self.move(), 1)
        self.assertNotIn(('umount', str(self.staging)), self.calls)
        self.assertEqual(self.mounted[str(self.staging)][0], 'someone-else')

    def test_a_record_that_can_not_be_read_is_never_called_done(self):
        """The review of 0.47.2: resume said all was well and left the record, which still
        keeps upgrades and setup waiting."""
        self.journal.write_text('{not json')
        code = migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab)
        self.assertEqual(code, 2)
        self.assertTrue(self.journal.exists())
        self.assertIn('can not be read', self.said[-1])
        self.journal.unlink()
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)

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
        migrate.record(self.journal, phase='switching', boot='this-boot', since=5, **cut_off)
        with mock.patch.object(migrate, 'this_boot', return_value='this-boot'):
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

    def test_a_drive_that_drops_out_mid_move_stops_it_before_the_last_copy(self):
        # Unplugged while the apps were being stopped: its folder is an empty one on the
        # system disk now. The last copy would delete everything copied so far, and an
        # empty-to-empty comparison would pass.
        def stop(journal):
            migrate.record(journal, phase='stopped', stopped=['docker.service'])
            self.mounted.pop('/mnt/nekoserver-media')
            return ['docker.service']

        with (
            mock.patch.object(migrate, 'stop_writers', stop),
            mock.patch.object(migrate, 'differences', return_value=[]),
        ):
            self.assertEqual(self.move(), 1)
        self.assertNotIn(('copy', True), self.calls)
        self.assertIn('no longer the one mounted', self.said[-1])
        self.assertIn(('start', ('docker.service',)), self.calls)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        # Another drive mounted there instead: the same.
        self.calls.clear()
        self.mounted['/mnt/nekoserver-media'] = ('other-0001', 'ext4', '')
        self.assertEqual(self.move(), 2)  # it is not the drive the settings name: refused before anything
        self.assertEqual(self.calls, [])

    def test_a_drive_on_another_nas_disk_than_the_settings_name_is_refused(self):
        """The review of 0.47.9: the source was known by its UUID, filesystem and subvolume
        alone, so a copied LUN with the same UUID on another NAS disk (WWN) was accepted and
        copied; the runtime refuses such a drive at every backup."""
        settings = json.loads(self.settings.read_text())
        settings['storage']['media_hdd']['network'] = {'transport': 'iscsi', 'wwn': '0x6001405aaaa'}
        self.settings.write_text(json.dumps(settings))
        disks = {'/mnt/nekoserver-media': '0x6001405bbbb'}
        with mock.patch.object(migrate, 'nas_disk_of', lambda folder: disks.get(str(folder), '')):
            self.assertEqual(self.move(), 2)  # refused before anything
            self.assertEqual(self.calls, [])
            self.assertIn('is on another NAS disk (0x6001405bbbb, not 0x6001405aaaa)', self.said[-1])
            # The right disk at the start, swapped while the apps were being stopped: the
            # move stops before the last copy, like a drive that dropped out.
            disks['/mnt/nekoserver-media'] = '0x6001405aaaa'

            def stop(journal):
                migrate.record(journal, phase='stopped', stopped=['docker.service'])
                disks['/mnt/nekoserver-media'] = '0x6001405bbbb'
                return ['docker.service']

            with (
                mock.patch.object(migrate, 'stop_writers', stop),
                mock.patch.object(migrate, 'differences', return_value=[]),
            ):
                self.assertEqual(self.move(), 1)
            self.assertNotIn(('copy', True), self.calls)
            self.assertIn('another NAS disk', self.said[-1])
            self.assertIn(('start', ('docker.service',)), self.calls)
            self.assertIn('UUID=hdd-0001', self.fstab.read_text())

    def test_the_nas_disk_is_read_from_findmnt_and_lsblk_as_the_runtime_reads_it(self):
        answers = {
            ('findmnt', '-n', '-o', 'SOURCE', '--mountpoint', '/mnt/nekoserver-media'): '/dev/sdc1\n',
            ('lsblk', '-n', '-s', '-o', 'TYPE,WWN', '/dev/sdc1'): 'part 0x6001405aaaa\ndisk 0x6001405aaaa\n',
            ('findmnt', '-n', '-o', 'SOURCE', '--mountpoint', '/mnt/plain'): '/dev/sdb1[/@data]\n',
            ('lsblk', '-n', '-s', '-o', 'TYPE,WWN', '/dev/sdb1'): 'part\ndisk\n',
        }

        def run(argv, *args, **kwargs):
            if tuple(argv) in answers:
                return mock.Mock(returncode=0, stdout=answers[tuple(argv)])
            return mock.Mock(returncode=1, stdout='')

        with mock.patch.object(migrate, 'run', run):
            self.assertEqual(migrate.nas_disk_of('/mnt/nekoserver-media'), '0x6001405aaaa')
            self.assertEqual(migrate.nas_disk_of('/mnt/plain'), '')  # a disk with no WWN
            self.assertEqual(migrate.nas_disk_of('/mnt/nothing'), '')  # nothing mounted
        old = {'uuid': 'hdd-0001', 'fstype': 'ext4', 'network': {'transport': 'iscsi', 'wwn': '0x6001405aaaa'}}
        with mock.patch.object(migrate, 'run', run):
            self.assertIsNone(migrate.other_nas_disk('/mnt/nekoserver-media', old))
            self.assertIn("can't be told apart", migrate.other_nas_disk('/mnt/plain', old))
            self.assertIsNone(migrate.other_nas_disk('/mnt/plain', {'uuid': 'x', 'fstype': 'ext4'}))  # not on a NAS

    def test_stopping_that_fails_part_way_starts_again_what_it_stopped(self):
        def stop(journal):
            migrate.record(journal, phase='stopped', stopped=['nekoserver-dashboard.service', 'docker.service'])
            raise RuntimeError('These did not stop: docker.service')

        self.start_fails = {'docker.service'}
        with mock.patch.object(migrate, 'stop_writers', stop):
            self.assertEqual(self.move(), 2)
        # The list comes from the record, not from what the failed call would have returned.
        self.assertIn(('start', ('nekoserver-dashboard.service', 'docker.service')), self.calls)
        # Not everything runs again: the record stays, naming what is left, and it says so.
        self.assertTrue(self.journal.exists())
        self.assertEqual(migrate.read(self.journal)['stopped'], ['docker.service'])
        self.assertIn('did not start again: docker.service', self.said[-1])
        # Once it runs again, resume finishes the clean-up.
        self.start_fails = set()
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertFalse(self.journal.exists())

    def test_going_back_to_a_btrfs_drive_mounts_its_own_subvolume_again(self):
        self.mounted['/mnt/nekoserver-media'] = ('hdd-0001', 'ext4', '/@media')
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)
        previous = json.loads(self.settings.read_text())['storage']['media_hdd']['previous_drive']
        self.assertEqual(previous, {'uuid': 'hdd-0001', 'fstype': 'ext4', 'subvolume': '/@media'})
        with (
            mock.patch.object(
                migrate, 'connected_uuids', return_value={'hdd-0001', 'newbig-0001', 'oldhdd-0001', 'oldssd-0001'}
            ),
            mock.patch.object(migrate, 'stop_writers', return_value=[]),
        ):
            self.assertEqual(migrate.back(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertIn(
            'UUID=hdd-0001 /mnt/nekoserver-media ext4 defaults,nofail,x-systemd.device-timeout=90s,subvol=/@media 0 2',
            self.fstab.read_text(),
        )
        self.assertEqual(
            json.loads(self.settings.read_text())['storage']['media_hdd'],
            # The newer drive is remembered in turn: `back` again goes to it.
            {
                'uuid': 'hdd-0001',
                'fstype': 'ext4',
                'subvolume': '/@media',
                'previous_drive': {'uuid': 'newbig-0001', 'fstype': 'ext4'},
            },
        )

    def test_going_back_needs_the_old_drive_connected(self):
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)
        with mock.patch.object(migrate, 'connected_uuids', return_value={'newbig-0001'}):
            self.assertEqual(migrate.back(self.settings, self.said.append, self.journal, self.staging, self.fstab), 2)
        self.assertIn('The old media HDD (hdd-0001) is not connected', self.said[-1])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'abcd-1234').touch()
            self.assertEqual(migrate.connected_uuids(tmp), {'abcd-1234'})
        self.assertEqual(migrate.connected_uuids('/nonexistent'), set())

    def test_a_move_cut_off_before_its_switch_whose_apps_ran_again_starts_over(self):
        # The audit of 0.45.6: the switch failed (the old drive was busy), the computer
        # restarted, and the apps ran on the old drive. Switching to the new drive's copy
        # would lose what they wrote since.
        cut_off = {
            'drive': 'media_hdd',
            'mount': '/mnt/nekoserver-media',
            'old': {'uuid': 'hdd-0001', 'fstype': 'ext4'},
            'new': {'uuid': 'newbig-0001', 'fstype': 'ext4'},
            'stopped': ['docker.service', 'nekoserver-app-backup.timer'],
        }
        migrate.record(self.journal, phase='switching', **cut_off)
        with mock.patch.object(migrate, 'active', lambda units: [u for u in units if u == 'docker.service']):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())  # never switched
        self.assertIn("copy may not be up to date. Start the move again", self.said[-1])
        self.assertFalse(self.journal.exists())
        # The 0.46.0 review: a record without the boot and the time (one written before
        # 0.45.8) can't tell whether the apps ran again since, so it starts over too.
        migrate.record(self.journal, phase='switching', **cut_off)
        with mock.patch.object(migrate, 'active', return_value=[]):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        # Nothing ran again, as the record and systemd show: the switch is finished as before.
        migrate.record(self.journal, phase='switching', boot='this-boot', since=5, **cut_off)
        with (
            mock.patch.object(migrate, 'active', return_value=[]),
            mock.patch.object(migrate, 'this_boot', return_value='this-boot'),
            mock.patch.object(migrate, 'started_since', return_value=[]),
        ):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertIn('UUID=newbig-0001', self.fstab.read_text())

    def test_apps_that_ran_and_were_stopped_again_still_mean_starting_over(self):
        """0.45.8: after a restart the apps ran on the old drive, then were stopped (for a
        recovery, say); nothing ran when resume looked, so it switched to the new drive's
        older copy, and what was saved in between was missing."""
        cut_off = {
            'drive': 'media_hdd',
            'mount': '/mnt/nekoserver-media',
            'old': {'uuid': 'hdd-0001', 'fstype': 'ext4'},
            'new': {'uuid': 'newbig-0001', 'fstype': 'ext4'},
            'stopped': ['docker.service'],
        }
        quiet = mock.patch.object(migrate, 'active', return_value=[])
        # Another boot: Docker started with the computer.
        migrate.record(self.journal, phase='switching', boot='an-earlier-boot', since=5, **cut_off)
        with quiet, mock.patch.object(migrate, 'this_boot', return_value='this-boot'):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        self.assertIn('Start the move again', self.said[-1])
        # The same boot, but Docker was started (and stopped) after the record.
        migrate.record(self.journal, phase='switching', boot='this-boot', since=5, **cut_off)
        with (
            quiet,
            mock.patch.object(migrate, 'this_boot', return_value='this-boot'),
            mock.patch.object(migrate, 'started_since', lambda units, since: list(units)),
        ):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1)
        self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        # The 0.46.0 review: systemd could not say when Docker last started, or this boot's
        # ID could not be read: nobody knows, so it starts over.
        for unknown in (
            mock.patch.object(migrate, 'started_since', return_value=None),
            mock.patch.object(migrate, 'this_boot', return_value=''),
        ):
            migrate.record(self.journal, phase='switching', boot='this-boot', since=5, **cut_off)
            with quiet, mock.patch.object(migrate, 'this_boot', return_value='this-boot'), unknown:
                self.assertEqual(
                    migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 1
                )
            self.assertIn('UUID=hdd-0001', self.fstab.read_text())
        # Nothing ran since: the switch is finished.
        migrate.record(self.journal, phase='switching', boot='this-boot', since=5, **cut_off)
        with (
            quiet,
            mock.patch.object(migrate, 'this_boot', return_value='this-boot'),
            mock.patch.object(migrate, 'started_since', return_value=[]),
        ):
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertIn('UUID=newbig-0001', self.fstab.read_text())

    def test_systemd_start_times_are_read_as_it_prints_them(self):
        shown = {
            'docker.service': 'InactiveExitTimestampMonotonic=900\n',
            'smbd.service': 'InactiveExitTimestampMonotonic=0\n',
        }
        with mock.patch.object(migrate, 'run', lambda argv, *a, **k: mock.Mock(returncode=0, stdout=shown[argv[-1]])):
            self.assertEqual(migrate.started_since(['docker.service', 'smbd.service'], 500), ['docker.service'])
        # systemd could not say: None, never "not started".
        with mock.patch.object(migrate, 'run', return_value=mock.Mock(returncode=1, stdout='')):
            self.assertIsNone(migrate.started_since(['docker.service'], 500))
        self.assertTrue(migrate.this_boot())

    def test_a_move_started_again_goes_on_with_its_own_copy(self):
        """0.45.8: "Start the move again" was refused at once: the copy the first move made
        is on the new drive, so it was not empty. Its own copy is reused; any other
        content still means no."""
        self.staging.mkdir(parents=True, exist_ok=True)
        (self.staging / 'from-the-first-move').write_text('x')
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 1)
        self.assertIn('is not empty', self.said[-1])
        migrate.copy_note(self.journal).write_text(
            json.dumps({'drive': 'media_hdd', 'old': 'hdd-0001', 'new': 'newbig-0001'})
        )
        self.mounted[str(self.staging)] = ('newbig-0001', 'ext4', '')  # mounted again
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0, self.said)
        self.assertIn('UUID=newbig-0001', self.fstab.read_text())
        self.assertFalse(migrate.copy_note(self.journal).exists())  # done: the note goes

    def test_a_copy_made_by_a_move_is_noted_for_a_retry(self):
        def fails(*args, **kwargs):
            raise RuntimeError('the copy stopped')

        with mock.patch.object(migrate, 'copy', fails):
            self.assertEqual(self.move(), 1)
        self.assertTrue(migrate.our_copy(self.journal, 'media_hdd', 'hdd-0001', 'newbig-0001'))
        self.assertFalse(migrate.our_copy(self.journal, 'media_hdd', 'hdd-0001', 'other-0001'))

    def test_back_takes_back_the_drive_asked_for(self):
        storage = json.loads(self.settings.read_text())
        storage['storage']['data_ssd']['previous_drive'] = {'uuid': 'oldssd-0001', 'fstype': 'ext4'}
        storage['storage']['media_hdd']['previous_drive'] = {'uuid': 'oldhdd-0001', 'fstype': 'ext4'}
        self.settings.write_text(json.dumps(storage))

        def go_back(drive=None):
            with (
                mock.patch.object(
                    migrate, 'connected_uuids', return_value={'hdd-0001', 'newbig-0001', 'oldhdd-0001', 'oldssd-0001'}
                ),
                mock.patch.object(migrate, 'stop_writers', return_value=[]),
            ):
                return migrate.back(
                    self.settings, self.said.append, self.journal, self.staging, self.fstab, drive=drive
                )

        # Both moved: it asks which, and changes nothing.
        self.assertEqual(go_back(), 2)
        self.assertIn('Both drives were moved: say which one goes back', self.said[-1])
        self.assertFalse(self.journal.exists())
        # --drive media_hdd: the HDD, never the SSD (the first in the settings).
        go_back('media_hdd')
        self.assertEqual(json.loads(self.settings.read_text())['storage']['media_hdd']['uuid'], 'oldhdd-0001')
        self.assertEqual(json.loads(self.settings.read_text())['storage']['data_ssd']['uuid'], 'ssd-0001')
        self.assertEqual(migrate.which_back(storage, 'data_ssd'), ('data_ssd', None))
        self.assertIsNone(migrate.which_back({'storage': {}}, 'media_hdd')[0])

    def test_resume_after_the_check_failed_does_not_unmount_the_drive_in_use(self):
        # The final check failed after the apps had started again on the new drive:
        # resume must not try to unmount it (they use it), but run setup and the check again.
        self.problem = 'these steps worked before and do not now: immich'
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 2)
        self.assertEqual(migrate.read(self.journal)['phase'], 'setup')
        self.problem = None
        self.calls.clear()
        with mock.patch.object(migrate.os.path, 'ismount', return_value=True):  # in use: can't be unmounted
            self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertNotIn(('umount', '/mnt/nekoserver-media'), self.calls)
        self.assertIn(('setup',), self.calls)
        self.assertFalse(self.journal.exists())

    def test_going_back_has_the_moves_checks_and_its_record(self):
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)
        self.mounted['/mnt/nekoserver-media'] = ('newbig-0001', 'ext4', '')

        def go_back():
            return migrate.back(self.settings, self.said.append, self.journal, self.staging, self.fstab)

        # Setup, an upgrade, an unfinished update or a backup at work: nothing is done.
        self.busy = 'A backup or an app update is running'
        self.calls.clear()
        self.assertEqual(go_back(), 2)
        self.assertEqual(self.calls, [])
        self.assertIn('A backup or an app update is running', self.said[-1])
        self.busy = None

        # Cut off right after it switched /etc/fstab (a power cut): its record is on the OS
        # disk (not in /run, which a restart empties), and resume finishes it.
        def cut_off(*args, **kwargs):
            raise SystemExit('power cut')

        with (
            mock.patch.object(
                migrate, 'connected_uuids', return_value={'hdd-0001', 'newbig-0001', 'oldhdd-0001', 'oldssd-0001'}
            ),
            mock.patch.object(migrate, 'setup_again', cut_off),
            self.assertRaises(SystemExit),
        ):
            go_back()
        saved = migrate.read(self.journal)
        self.assertEqual((saved['direction'], saved['phase']), ('back', 'setup'))
        self.assertEqual(saved['new']['uuid'], 'hdd-0001')
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertFalse(self.journal.exists())
        self.assertIn('UUID=hdd-0001 /mnt/nekoserver-media', self.fstab.read_text())
        self.assertEqual(json.loads(self.settings.read_text())['storage']['media_hdd']['uuid'], 'hdd-0001')
        self.assertIn('is the old drive again', self.said[-1])
        # A setup started by hand meanwhile stops (the move's own runs pass the flag).
        script = (ROOT / 'install.sh').read_text()
        self.assertIn('[[ -e /etc/nekoserver-backups/migrate.json && -z "${NEKOSERVER_KEEP_SYSTEM:-}" ]]', script)
        self.assertIn("'NEKOSERVER_KEEP_SYSTEM': '1'", (ROOT / 'upgrade.py').read_text())
        self.assertIn('migrate.py resume', (ROOT / 'install.sh').read_text())
        # Nothing here writes to /run any more.
        self.assertNotIn("'/run/neko-migrate", (ROOT / 'migrate.py').read_text())

    def test_the_end_of_the_move_is_checked_like_an_upgrade(self):
        # Setup ending with exit code 1 can mean a step failed: the check finds it, and
        # the move is not called done.
        self.problem = 'these steps worked before and do not now: immich'
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 2)
        self.assertEqual(self.checked, [['storage']])  # the steps that worked before the move
        self.assertIn('does not pass its check: these steps worked before', self.said[-1])
        self.assertEqual(migrate.read(self.journal)['phase'], 'setup')
        # Put right: resume runs setup and the check again, then it is done.
        self.problem = None
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertFalse(self.journal.exists())
        self.assertIn('the new drive', self.said[-1])
        # The real check is the upgrade's own (its steps, dashboard and services).
        self.assertIn("upgrade.verify(config, 'v' + VERSION, before)", (ROOT / 'migrate.py').read_text())

    def test_a_comment_or_another_line_naming_the_new_drive_does_not_stop_the_switch(self):
        self.fstab.write_text(
            self.fstab.read_text() + '# UUID=newbig-0001 /mnt/nekoserver-media ext4 defaults 0 2 (an old try)\n'
        )
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 0)
        lines = self.fstab.read_text().splitlines()
        active = [lines[i] for i in migrate.mount_lines(lines, '/mnt/nekoserver-media')]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].startswith('UUID=newbig-0001 '))
        self.assertEqual(self.mounted['/mnt/nekoserver-media'][0], 'newbig-0001')

    def test_a_mount_that_is_not_the_new_drive_is_not_called_done(self):
        def wrong_mount(argv, *args, **kwargs):
            self.calls.append(tuple(argv))
            if argv[0] == 'mount':
                self.mounted[argv[1]] = ('hdd-0001', 'ext4', '')
            return mock.Mock(returncode=0, stdout='')

        with (
            mock.patch.object(migrate, 'differences', return_value=[]),
            mock.patch.object(migrate, 'run', wrong_mount),
        ):
            self.assertEqual(self.move(), 2)
        self.assertIn('did not mount the new drive newbig-0001 (it shows hdd-0001)', self.said[-1])
        self.assertNotIn(('setup',), self.calls)
        self.assertEqual(migrate.read(self.journal)['phase'], 'switching')

    def test_apps_that_do_not_start_after_the_switch_are_finished_on_the_new_drive(self):
        self.start_fails = {'docker.service'}
        with mock.patch.object(migrate, 'differences', return_value=[]):
            self.assertEqual(self.move(), 2)
        saved = migrate.read(self.journal)
        self.assertEqual((saved['phase'], saved['stopped']), ('started_partly', ['docker.service']))
        # Resume carries on on the new drive: it never says the old one is in use.
        self.start_fails = set()
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertNotIn('the old drive, unchanged', ' '.join(self.said))
        self.assertIn('the new drive', self.said[-1])
        self.assertFalse(self.journal.exists())
        # A 0.45.2 record ("restart_failed" after the switch) is told apart by the drive mounted.
        migrate.record(
            self.journal,
            phase='restart_failed',
            drive='media_hdd',
            mount='/mnt/nekoserver-media',
            old={'uuid': 'hdd-0001', 'fstype': 'ext4'},
            new={'uuid': 'newbig-0001', 'fstype': 'ext4'},
            stopped=['docker.service'],
        )
        self.assertEqual(migrate.resume(self.settings, self.said.append, self.journal, self.staging, self.fstab), 0)
        self.assertIn('the new drive', self.said[-1])

    def test_a_move_and_an_upgrade_never_overlap(self):
        import fcntl

        import upgrade

        self.assertEqual(upgrade.MIGRATE_JOURNAL, migrate.JOURNAL)
        with open(self.dir / 'operation.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.move(), 2)
            self.assertIn('A NekoServer upgrade or another drive move is at work', self.said[-1])
            self.assertEqual(self.calls, [])
        # The upgrade holds the same lock, and refuses while a move's record is there.
        with (
            mock.patch.object(upgrade, 'OPERATION_LOCK', self.dir / 'operation.lock'),
            mock.patch.object(upgrade, 'MIGRATE_JOURNAL', self.journal),
        ):
            migrate.record(self.journal, phase='copying')
            said = []
            self.assertEqual(upgrade.upgrade('v99.0.0', self.settings, said.append, journal=self.dir / 'u.json'), 2)
            self.assertIn('A move to another drive is not finished', said[-1])
            with open(self.dir / 'operation.lock', 'a') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                self.assertEqual(upgrade.upgrade('v99.0.0', self.settings, said.append), 2)
                self.assertIn('A drive move (migrate.py) or another upgrade is at work', said[-1])

    def test_the_fstab_line_is_switched_only_when_it_is_the_old_drives(self):
        self.fstab.write_text(self.fstab.read_text() + 'UUID=other /mnt/nekoserver-media ext4 defaults 0 2\n')
        with self.assertRaisesRegex(RuntimeError, 'exactly one line'):
            migrate.switch_fstab('/mnt/nekoserver-media', 'hdd-0001', 'newbig-0001', 'ext4', self.fstab)


if __name__ == '__main__':
    unittest.main()
