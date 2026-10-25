"""Drive identity: the system disk is recognised from every place it is mounted, only a
whole drive (or a Btrfs subvolume) is taken as the SSD or HDD, the Python checks compare
the Btrfs subvolume as the runtime's guard does, Docker's start guard must run on this
Linux, the space check leaves a busy USB drive alone, and a failed restore says why."""
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common
import snapshot_restore
from modules import capacity, docker, photo_backup, storage


def disk(name, *children):
    return {'name': name, 'type': 'disk', 'size': '1T', 'children': list(children)}


def part(name, fstype, uuid, mountpoint=None, mountpoints=None):
    return {'name': name, 'type': 'part', 'fstype': fstype, 'uuid': uuid, 'size': '500G',
            'mountpoint': mountpoint, 'mountpoints': mountpoints if mountpoints is not None else [mountpoint]}


class SystemDiskTests(unittest.TestCase):
    def test_a_btrfs_system_partition_shown_at_home_is_still_the_system_disk(self):
        # lsblk's MOUNTPOINT shows one of the subvolume mounts; MOUNTPOINTS has them all.
        inventory = {'blockdevices': [
            disk('nvme0n1', part('nvme0n1p1', 'vfat', 'EF-01', '/efi'),
                 part('nvme0n1p2', 'btrfs', 'root-uuid-1', '/home', ['/home', '/', '/.snapshots']),
                 part('nvme0n1p3', 'ext4', 'spare-uuid-1')),
            disk('sdb', part('sdb1', 'ext4', 'ssd-uuid-01'))]}
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(inventory)], ['ssd-uuid-01'])

    def test_an_efi_partition_at_efi_marks_the_system_disk(self):
        inventory = {'blockdevices': [
            disk('sda', part('sda1', 'vfat', 'EF-01', '/efi'), part('sda2', 'ext4', 'other-uuid-1')),
            disk('sdb', part('sdb1', 'ext4', 'ssd-uuid-01'))]}
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(inventory)], ['ssd-uuid-01'])
        self.assertTrue(storage.holds_system(inventory['blockdevices'][0]))
        self.assertFalse(storage.holds_system(inventory['blockdevices'][1]))
        # Its FAT boot partition is not named as a drive to reformat.
        message = storage.nothing_eligible(inventory, [], set(), 'the SSD')
        self.assertNotIn('sda1', message)
        self.assertIn('/efi', photo_backup.SYSTEM_MOUNTS)

    def test_lsblk_is_asked_for_every_mountpoint(self):
        with mock.patch.object(storage, 'call', return_value='{"blockdevices": []}') as call:
            storage.inventory_now()
        columns = call.call_args[0][0][3].split(',')
        self.assertIn('MOUNTPOINTS', columns)
        self.assertIn('MOUNTPOINT', columns)


class FolderMountTests(unittest.TestCase):
    def test_a_folder_of_an_ext4_drive_mounted_there_is_refused_not_saved_as_a_subvolume(self):
        sources = {'/srv/nekoserver': '/dev/sdb1[/old-share]'}
        with mock.patch.object(storage, 'findmnt', side_effect=lambda column, target, *a, **k: sources[str(target)]), \
                mock.patch.object(storage, 'source_path', return_value=Path('/dev/sdb1')), \
                mock.patch.object(storage.Path, 'mkdir'), mock.patch.object(storage.Path, 'exists', return_value=True), \
                mock.patch.object(storage.Path, 'resolve', lambda self, strict=False: self):
            with self.assertRaisesRegex(RuntimeError, 'Only the folder /old-share of the drive is mounted'):
                storage.mount_and_check('aaaa-1111', '/srv/nekoserver', None, 'ext4')
            # A Btrfs subvolume there is what it looks like, and is returned to be saved.
            self.assertEqual(storage.mount_and_check('aaaa-1111', '/srv/nekoserver', None, 'btrfs'), '/old-share')

    def test_mount_all_saves_no_subvolume_for_an_ext4_drive(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/srv/media',
                  'storage': {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                              'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}}
        sources = {'/srv/nekoserver': '/dev/sdb1[/old-share]', '/srv/media': '/dev/sdc1'}
        devices = {'aaaa-1111': Path('/dev/sdb1'), 'bbbb-2222': Path('/dev/sdc1')}
        context = mock.Mock(config=config, log=lambda *_: None)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(storage, 'findmnt', side_effect=lambda column, target, *a, **k: sources[str(target)]), \
                mock.patch.object(storage, 'call', return_value=''), \
                mock.patch.object(storage, 'source_path', side_effect=devices.get), \
                mock.patch.object(storage.Path, 'mkdir'), mock.patch.object(storage.Path, 'exists', return_value=True), \
                mock.patch.object(storage.Path, 'resolve', lambda self, strict=False: self):
            (Path(d) / 'fstab').write_text('UUID=root / ext4 defaults 0 1\n')
            with self.assertRaisesRegex(RuntimeError, 'not the whole drive'):
                storage.mount_all(context, storage.mount_specs(config), Path(d) / 'fstab')
        self.assertNotIn('subvolume', config['storage']['data_ssd'])
        context.persist_config.assert_not_called()


def findmnt_answer(text):
    return lambda argv, **_: mock.Mock(returncode=0, stdout=text)


SUBVOLUME_CONFIG = {'data_mount': '/srv/nekoserver', 'media_mount': '/srv/media',
                    'storage': {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'btrfs', 'subvolume': '/@data'},
                                'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}}
MOUNTED = {'/srv/nekoserver': 'aaaa-1111 btrfs /dev/sdb1[/@data]', '/srv/media': 'bbbb-2222 ext4 /dev/sdc1'}


class SubvolumeCheckTests(unittest.TestCase):
    CONFIG = SUBVOLUME_CONFIG
    MOUNTED = MOUNTED

    def guard(self):
        spec = importlib.util.spec_from_file_location('storage_guard_test', ROOT / 'storage-guard.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def checks(self, mounted):
        def fake(columns, target, *a, **k):
            self.assertEqual(columns, 'UUID,FSTYPE,SOURCE')
            return mounted.get(str(target))
        with mock.patch.object(neko_common, 'findmnt', side_effect=fake):
            yield from (neko_common.verify_storage, self.guard().verify)

    def test_the_right_subvolume_passes(self):
        for check in self.checks(self.MOUNTED):
            check(self.CONFIG)

    def test_another_subvolume_of_the_right_drive_is_refused(self):
        for mounted in ({**self.MOUNTED, '/srv/nekoserver': 'aaaa-1111 btrfs /dev/sdb1[/@other]'},
                        {**self.MOUNTED, '/srv/nekoserver': 'aaaa-1111 btrfs /dev/sdb1'},
                        {**self.MOUNTED, '/srv/media': 'bbbb-2222 ext4 /dev/sdc1[/films]'},
                        {**self.MOUNTED, '/srv/media': 'cccc-3333 ext4 /dev/sdd1'}):
            for check in self.checks(mounted):
                with self.assertRaises(RuntimeError):
                    check(self.CONFIG)

    def test_the_tree_is_read_as_the_runtime_reads_it(self):
        with mock.patch.object(neko_common, 'findmnt', return_value='u btrfs /dev/sdb1[/]'):
            self.assertEqual(neko_common.mounted_identity('/x'), ('u', 'btrfs', ''))
        with mock.patch.object(neko_common, 'findmnt', return_value=None):
            self.assertIsNone(neko_common.mounted_identity('/x'))


class GuardRunsHereTests(unittest.TestCase):
    def test_a_start_guard_that_does_not_run_here_is_never_installed(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/srv/media', 'storage': {}}
        with tempfile.TemporaryDirectory() as d:
            dropin = Path(d) / 'docker.service.d/10-nekoserver-storage.conf'
            with mock.patch.object(docker, 'mount_path', return_value=Path(d) / 'ssd'), \
                    mock.patch.object(docker, 'DOCKER_STORAGE_DROPIN', dropin), \
                    mock.patch.object(docker.native_runtime, 'deploy', return_value=Path(d) / 'ssd/guard'), \
                    mock.patch.object(docker.native_runtime, 'runs_here', return_value=False), \
                    mock.patch.object(docker, 'call') as call, \
                    self.assertRaisesRegex(RuntimeError, 'does not start on this Linux.*sudo bash install.sh'):
                docker.install_storage_guard(config)
            self.assertFalse(dropin.exists())
            call.assert_not_called()


class BusyUsbTests(unittest.TestCase):
    def test_a_usb_drive_the_weekly_copy_holds_is_not_measured(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / 'photo-backup.lock'
            held = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(held, fcntl.LOCK_EX)
            try:
                with mock.patch.object(capacity.Path, 'exists', return_value=True), \
                        mock.patch.object(capacity, 'measure_usb') as measure:
                    self.assertIsNone(capacity.usb_room('aaaa-1111', [], 'ext4', lock=lock))
                    measure.assert_not_called()
            finally:
                os.close(held)
            with mock.patch.object(capacity.Path, 'exists', return_value=True), \
                    mock.patch.object(capacity, 'measure_usb', return_value=(1, 2)) as measure:
                self.assertEqual(capacity.usb_room('aaaa-1111', [], 'ext4', lock=lock), (1, 2))


class RestoreReasonTests(unittest.TestCase):
    def snapshot(self, root):
        snap = root / 'snap'
        (snap / 'payload/appdata/x').mkdir(parents=True)
        (snap / 'payload/appdata/x/a.txt').write_text('hello')
        rows = [{'path': 'appdata', 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0},
                {'path': 'appdata/x', 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0},
                {'path': 'appdata/x/a.txt', 'kind': 'file', 'uid': 0, 'gid': 0, 'mode': 0o644, 'size': 5,
                 'sha256': hashlib.sha256(b'hello').hexdigest()}]
        (snap / 'manifest.json').write_text(json.dumps({'format': 'neko-media-snapshot-v1', 'files': rows}))
        return snap

    def run_main(self, argv):
        said = io.StringIO()
        with redirect_stderr(said):
            code = snapshot_restore.main(snapshot_restore.APPS, '', argv)
        return code, said.getvalue()

    def test_a_full_drive_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.snapshot(Path(d))
            full = RuntimeError('not enough free space: the restore needs 900.0 GB and 3.0 GB is free on that drive')
            with mock.patch.object(snapshot_restore, 'ensure_room', side_effect=full):
                code, said = self.run_main(['restore', '--snapshot', str(snap), '--destination', str(Path(d) / 'new')])
        self.assertEqual(code, 1)
        self.assertIn('Live app data was not changed. Reason: not enough free space', said)
        self.assertNotIn('kept in', said)

    def test_a_cut_off_restore_names_the_folder_it_leaves(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.snapshot(Path(d))
            target = Path(d) / 'new'
            with mock.patch.object(snapshot_restore, 'copy_row', side_effect=OSError(28, 'No space left on device', 'a.txt')):
                code, said = self.run_main(['restore', '--snapshot', str(snap), '--destination', str(target)])
            self.assertTrue(target.is_dir())
        self.assertEqual(code, 1)
        self.assertIn('Reason: No space left on device (a.txt)', said)
        self.assertIn('kept in ' + str(target) + ' (incomplete)', said)

    def test_an_existing_folder_is_said_and_not_called_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            snap = self.snapshot(Path(d))
            _code, said = self.run_main(['restore', '--snapshot', str(snap), '--destination', d])
        self.assertIn('Reason: Restore needs a new absolute directory', said)
        self.assertNotIn('kept in', said)


if __name__ == '__main__':
    unittest.main()
