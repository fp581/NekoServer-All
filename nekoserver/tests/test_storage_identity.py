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
    return {
        'name': name,
        'type': 'part',
        'fstype': fstype,
        'uuid': uuid,
        'size': '500G',
        'mountpoint': mountpoint,
        'mountpoints': mountpoints if mountpoints is not None else [mountpoint],
    }


class SystemDiskTests(unittest.TestCase):
    def test_a_btrfs_system_partition_shown_at_home_is_still_the_system_disk(self):
        # lsblk's MOUNTPOINT shows one of the subvolume mounts; MOUNTPOINTS has them all.
        inventory = {
            'blockdevices': [
                disk(
                    'nvme0n1',
                    part('nvme0n1p1', 'vfat', 'EF-01', '/efi'),
                    part('nvme0n1p2', 'btrfs', 'root-uuid-1', '/home', ['/home', '/', '/.snapshots']),
                    part('nvme0n1p3', 'ext4', 'spare-uuid-1'),
                ),
                disk('sdb', part('sdb1', 'ext4', 'ssd-uuid-01')),
            ]
        }
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(inventory)], ['ssd-uuid-01'])

    def test_an_efi_partition_at_efi_marks_the_system_disk(self):
        inventory = {
            'blockdevices': [
                disk('sda', part('sda1', 'vfat', 'EF-01', '/efi'), part('sda2', 'ext4', 'other-uuid-1')),
                disk('sdb', part('sdb1', 'ext4', 'ssd-uuid-01')),
            ]
        }
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
        with (
            mock.patch.object(storage, 'findmnt', side_effect=lambda column, target, *a, **k: sources[str(target)]),
            mock.patch.object(
                storage, 'mounted_filesystem', side_effect=[('aaaa-1111', 'ext4'), ('aaaa-1111', 'btrfs')]
            ),
            mock.patch.object(storage.Path, 'mkdir'),
            mock.patch.object(storage.Path, 'exists', return_value=True),
            mock.patch.object(storage.Path, 'resolve', lambda self, strict=False: self),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Only the folder /old-share of the drive is mounted'):
                storage.mount_and_check('aaaa-1111', '/srv/nekoserver', None, 'ext4')
            # A Btrfs subvolume there is what it looks like, and is returned to be saved.
            self.assertEqual(storage.mount_and_check('aaaa-1111', '/srv/nekoserver', None, 'btrfs'), '/old-share')

    def test_mount_all_saves_no_subvolume_for_an_ext4_drive(self):
        config = {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/srv/media',
            'storage': {
                'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'},
            },
        }
        sources = {'/srv/nekoserver': '/dev/sdb1[/old-share]', '/srv/media': '/dev/sdc1'}
        devices = {'/srv/nekoserver': ('aaaa-1111', 'ext4'), '/srv/media': ('bbbb-2222', 'ext4')}
        context = mock.Mock(config=config, log=lambda *_: None)
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(storage, 'findmnt', side_effect=lambda column, target, *a, **k: sources[str(target)]),
            mock.patch.object(storage, 'call', return_value=''),
            mock.patch.object(storage, 'mounted_filesystem', side_effect=devices.get),
            mock.patch.object(storage.Path, 'mkdir'),
            mock.patch.object(storage.Path, 'exists', return_value=True),
            mock.patch.object(storage.Path, 'resolve', lambda self, strict=False: self),
        ):
            (Path(d) / 'fstab').write_text('UUID=root / ext4 defaults 0 1\n')
            specs, fstab = storage.mount_specs(config), Path(d) / 'fstab'
            with self.assertRaisesRegex(RuntimeError, 'not the whole drive'):
                storage.mount_all(context, specs, fstab)
        self.assertNotIn('subvolume', config['storage']['data_ssd'])
        context.persist_config.assert_not_called()


def findmnt_answer(text):
    return lambda argv, **_: mock.Mock(returncode=0, stdout=text)


SUBVOLUME_CONFIG = {
    'data_mount': '/srv/nekoserver',
    'media_mount': '/srv/media',
    'storage': {
        'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'btrfs', 'subvolume': '/@data'},
        'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'},
    },
}
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
        for mounted in (
            {**self.MOUNTED, '/srv/nekoserver': 'aaaa-1111 btrfs /dev/sdb1[/@other]'},
            {**self.MOUNTED, '/srv/nekoserver': 'aaaa-1111 btrfs /dev/sdb1'},
            {**self.MOUNTED, '/srv/media': 'bbbb-2222 ext4 /dev/sdc1[/films]'},
            {**self.MOUNTED, '/srv/media': 'cccc-3333 ext4 /dev/sdd1'},
        ):
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
            with (
                mock.patch.object(docker, 'mount_path', return_value=Path(d) / 'ssd'),
                mock.patch.object(docker, 'DOCKER_STORAGE_DROPIN', dropin),
                mock.patch.object(docker.native_runtime, 'deploy', return_value=Path(d) / 'ssd/guard'),
                mock.patch.object(docker.native_runtime, 'runs_here', return_value=False),
                mock.patch.object(docker, 'call') as call,
                self.assertRaisesRegex(RuntimeError, 'does not start on this Linux.*sudo bash install.sh'),
            ):
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
                with (
                    mock.patch.object(capacity.Path, 'exists', return_value=True),
                    mock.patch.object(capacity, 'measure_usb') as measure,
                ):
                    self.assertIsNone(capacity.usb_room('aaaa-1111', [], 'ext4', lock=lock))
                    measure.assert_not_called()
            finally:
                os.close(held)
            with (
                mock.patch.object(capacity.Path, 'exists', return_value=True),
                mock.patch.object(capacity, 'measure_usb', return_value=(1, 2)) as measure,
            ):
                self.assertEqual(capacity.usb_room('aaaa-1111', [], 'ext4', lock=lock), (1, 2))


class RestoreReasonTests(unittest.TestCase):
    def snapshot(self, root):
        snap = root / 'snap'
        (snap / 'payload/appdata/x').mkdir(parents=True)
        (snap / 'payload/appdata/x/a.txt').write_text('hello')
        rows = [
            {'path': 'appdata', 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0},
            {'path': 'appdata/x', 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0},
            {
                'path': 'appdata/x/a.txt',
                'kind': 'file',
                'uid': 0,
                'gid': 0,
                'mode': 0o644,
                'size': 5,
                'sha256': hashlib.sha256(b'hello').hexdigest(),
            },
        ]
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
            with mock.patch.object(
                snapshot_restore, 'copy_row', side_effect=OSError(28, 'No space left on device', 'a.txt')
            ):
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


POOL_MOUNT = {'TARGET': '/srv/nekoserver', 'SOURCE': '/dev/sdb', 'UUID,FSTYPE,SOURCE': 'pool-uuid btrfs /dev/sdb'}


class PoolMountTests(unittest.TestCase):
    """A Btrfs pool over two drives: findmnt names the member with the lowest device ID
    as the mount's source, while /dev/disk/by-uuid may point at the other one. Setup
    compared those two paths and refused the right pool; it now reads the UUID."""

    def findmnt(self, answers):
        return mock.patch.object(storage, 'findmnt', side_effect=lambda column, target, *a, **k: answers.get(column))

    def test_the_pool_mounted_from_its_other_member_passes(self):
        # by-uuid names the second member (/dev/sdc): it is not read at all.
        by_uuid = {'pool-uuid': Path('/dev/sdc')}
        with (
            self.findmnt(POOL_MOUNT),
            mock.patch.object(storage.Path, 'mkdir'),
            mock.patch.object(storage.Path, 'resolve', lambda self, strict=False: by_uuid.get(self.name, self)),
        ):
            self.assertIsNone(storage.mount_and_check('pool-uuid', '/srv/nekoserver', None, 'btrfs'))
            with self.assertRaisesRegex(RuntimeError, 'Post-mount validation failed'):
                storage.mount_and_check('other-uuid', '/srv/nekoserver', None, 'btrfs')
            with self.assertRaisesRegex(RuntimeError, 'Post-mount validation failed'):
                storage.mount_and_check('pool-uuid', '/srv/nekoserver', None, 'ext4')

    def test_an_empty_uuid_column_is_read_from_the_mounted_device(self):
        answers = {**POOL_MOUNT, 'UUID,FSTYPE,SOURCE': 'btrfs /dev/sdb[/@data]'}
        with self.findmnt(answers), mock.patch.object(storage, 'call', return_value='pool-uuid') as call:
            self.assertEqual(storage.mounted_filesystem('/srv/nekoserver'), ('pool-uuid', 'btrfs'))
        self.assertEqual(call.call_args[0][0][-1], '/dev/sdb')

    def test_a_rerun_with_the_pool_mounted_goes_on(self):
        config = {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/srv/media',
            'storage': {
                'data_ssd': {'uuid': 'pool-uuid', 'fstype': 'btrfs'},
                'media_hdd': {'uuid': 'hdd-uuid-1', 'fstype': 'ext4'},
            },
        }
        context = mock.Mock(config=config, apply=True, log=lambda *_: None)
        mounts = {
            '/srv/nekoserver': POOL_MOUNT,
            '/srv/media': {'SOURCE': '/dev/sdd1', 'UUID,FSTYPE,SOURCE': 'hdd-uuid-1 ext4 /dev/sdd1'},
        }
        with (
            mock.patch.object(
                storage, 'findmnt', side_effect=lambda column, target, *a, **k: mounts[target].get(column)
            ),
            mock.patch.object(storage, 'inventory_now', return_value={'blockdevices': []}),
            mock.patch.object(storage, 'resolve_storage_identity'),
            mock.patch.object(storage, 'remember_subvolumes'),
            mock.patch.object(storage, 'validate_device'),
            mock.patch.object(storage, 'save_storage'),
            mock.patch.object(storage, 'mount_all') as mount_all,
            mock.patch.object(storage, 'choose_usb'),
            mock.patch.object(storage.os, 'geteuid', return_value=0),
            mock.patch.object(storage.shutil, 'which', return_value='/usr/bin/x'),
        ):
            storage.run(context)
            mount_all.assert_called_once()
            mounts['/srv/nekoserver'] = {'SOURCE': '/dev/sde', 'UUID,FSTYPE,SOURCE': 'else-uuid btrfs /dev/sde'}
            with self.assertRaisesRegex(RuntimeError, 'Wrong filesystem already mounted at /srv/nekoserver: /dev/sde'):
                storage.run(context)

    def test_the_second_device_of_a_btrfs_system_pool_is_the_system_disk(self):
        inventory = {
            'blockdevices': [
                disk('sda', part('sda1', 'btrfs', 'root-uuid-1', '/')),
                disk('sdb', part('sdb1', 'btrfs', 'root-uuid-1'), part('sdb2', 'ext4', 'spare-uuid-1')),
                disk('sdc', part('sdc1', 'ext4', 'ssd-uuid-01')),
            ]
        }
        self.assertEqual(
            [v['uuid'] for v in storage.discover_filesystems(inventory, lambda name: None)], ['ssd-uuid-01']
        )

    def test_a_root_pool_named_by_another_device_is_still_the_os_filesystem(self):
        answers = {'SOURCE': '/dev/sda1', 'UUID': 'root-uuid-1'}
        with (
            self.findmnt(answers),
            mock.patch.object(storage, 'call', return_value='btrfs'),
            mock.patch.object(storage.Path, 'exists', return_value=True),
            self.assertRaisesRegex(RuntimeError, 'belongs to the OS filesystem'),
        ):
            storage.validate_device('root-uuid-1', 'btrfs')


ENCRYPTED_INVENTORY = {
    'blockdevices': [
        disk('sda', part('sda1', 'ext4', 'os-uuid-001', '/')),
        disk(
            'sdb',
            {
                'name': 'sdb1',
                'type': 'part',
                'fstype': 'crypto_LUKS',
                'uuid': 'luks-outer-1',
                'children': [
                    {'name': 'secret', 'type': 'crypt', 'fstype': 'ext4', 'uuid': 'ssd-uuid-01', 'size': '1T'}
                ],
            },
        ),
        disk('sdc', part('sdc1', 'ext4', 'hdd-uuid-01')),
    ]
}


class SavedEncryptedDriveTests(unittest.TestCase):
    """A drive already in the settings (reused after a reinstall) goes through the same
    boot-unlock check as a newly chosen one."""

    def resolve(self, crypttab_text):
        config = {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/srv/media',
            'storage': {'data_ssd': {'uuid': 'ssd-uuid-01'}, 'media_hdd': {'uuid': 'hdd-uuid-01'}},
        }
        real = storage.boot_unlock_problem
        with tempfile.TemporaryDirectory() as d:
            crypttab = Path(d) / 'crypttab'
            crypttab.write_text(crypttab_text)
            with mock.patch.object(
                storage, 'boot_unlock_problem', side_effect=lambda dev: real(dev, crypttab)
            ) as check:
                try:
                    return storage.resolve_storage_identity(config, ENCRYPTED_INVENTORY, apply=True)
                finally:
                    self.assertIn('secret', [c.args[0]['name'] for c in check.call_args_list])

    def test_a_saved_encrypted_drive_opened_by_hand_is_refused(self):
        for text in ('other UUID=1 none\n', 'secret UUID=2 /root/key luks,noauto\n'):
            with self.assertRaisesRegex(RuntimeError, '/dev/mapper/secret.*/etc/crypttab'):
                self.resolve(text)

    def test_a_saved_encrypted_drive_unlocked_at_start_passes(self):
        self.assertEqual(self.resolve('secret UUID=2 /root/key luks\n')['data_ssd']['fstype'], 'ext4')


def block_devices(rows, sub_ids, blkid_code=0, lsblk_code=0):
    """subprocess.run replaced with fake_run while the checks run."""
    return mock.patch.object(neko_common.subprocess, 'run', side_effect=fake_run(rows, sub_ids, blkid_code, lsblk_code))


def fake_run(rows, sub_ids, blkid_code=0, lsblk_code=0):
    """A subprocess.run answering lsblk's device list and blkid's UUID_SUB."""

    def run(argv, **_):
        if argv[0] == 'lsblk':
            return mock.Mock(returncode=lsblk_code, stdout=json.dumps({'blockdevices': rows}))
        return mock.Mock(returncode=blkid_code, stdout=sub_ids.get(argv[-1], '') + '\n')

    return run


DEVICES = [
    {'path': '/dev/sda', 'uuid': None},
    {'path': '/dev/sdb1', 'uuid': 'aaaa-1111'},
    {'path': '/dev/sdc1', 'uuid': 'bbbb-2222'},
]


def lvm_row(name, kname, parent, uuid=None):
    """One row of  lsblk -J -l -o PATH,UUID,TYPE,NAME,KNAME,PKNAME  for an LVM device."""
    return {'path': '/dev/mapper/' + name, 'uuid': uuid, 'type': 'lvm', 'name': name, 'kname': kname, 'pkname': parent}


# The HDD as LVM volume vg0/media on /dev/sdc1, with a snapshot (lvcreate -s -n media-snap),
# as lsblk lists it: the volume and the snapshot both sit on the hidden vg0-media-real, the
# snapshot on its own vg0-media--snap-cow too, so it is listed once under each. LVM's udev
# rules leave the hidden layers unprobed (no UUID); the snapshot carries the volume's UUID.
HDD = 'bbbb-2222'
LVM_SNAPSHOT = [
    {'path': '/dev/sda', 'uuid': None, 'type': 'disk', 'name': 'sda', 'kname': 'sda', 'pkname': None},
    {'path': '/dev/sdb1', 'uuid': 'aaaa-1111', 'type': 'part', 'name': 'sdb1', 'kname': 'sdb1', 'pkname': 'sdb'},
    {'path': '/dev/sdc1', 'uuid': 'Xc4Qz1-pv01', 'type': 'part', 'name': 'sdc1', 'kname': 'sdc1', 'pkname': 'sdc'},
    lvm_row('vg0-media-real', 'dm-1', 'sdc1'),
    lvm_row('vg0-media', 'dm-0', 'dm-1', HDD),
    lvm_row('vg0-media--snap', 'dm-3', 'dm-1', HDD),
    lvm_row('vg0-media--snap-cow', 'dm-2', 'sdc1'),
    lvm_row('vg0-media--snap', 'dm-3', 'dm-2', HDD),
]


class ConnectedCopyTests(unittest.TestCase):
    """A copy of the SSD or HDD (dd, Clonezilla) connected after setup carries its UUID
    too, and fstab mounts by UUID: the start checks refuse, unless the devices are the
    distinct members of one Btrfs pool, or the check can't be made."""

    CONFIG, MOUNTED = SUBVOLUME_CONFIG, MOUNTED
    guard, checks = SubvolumeCheckTests.guard, SubvolumeCheckTests.checks

    def run_checks(self, rows, sub_ids=None, blkid_code=0, lsblk_code=0, devices=DEVICES):
        failures = []
        with block_devices(devices + rows, sub_ids or {}, blkid_code, lsblk_code):
            for check in self.checks(self.MOUNTED):
                try:
                    check(self.CONFIG)
                except RuntimeError as exc:
                    failures.append(str(exc))
        return failures

    def test_a_copy_of_the_hdd_is_refused(self):
        failures = self.run_checks([{'path': '/dev/sdd1', 'uuid': 'bbbb-2222'}])
        self.assertEqual(len(failures), 2)
        for text in failures:
            self.assertIn('two drives look the same (one is probably a copy); unplug the copy', text.lower())
            self.assertIn('/dev/sdc1 and /dev/sdd1', text)

    def test_a_copy_of_one_device_of_a_btrfs_pool_is_refused(self):
        rows = [{'path': '/dev/sde1', 'uuid': 'aaaa-1111'}]
        self.assertEqual(len(self.run_checks(rows, {'/dev/sdb1': 'dev-1', '/dev/sde1': 'dev-1'})), 2)

    def test_an_lvm_snapshot_of_the_drive_is_not_a_copy(self):
        """QA-06: a snapshot (lvcreate -s) of the HDD's volume carries its UUID; it is not a
        copy left connected, so Docker and the apps still start."""
        self.assertEqual(self.run_checks([], devices=LVM_SNAPSHOT), [])
        # A Btrfs volume: the snapshot has the volume's device ID (UUID_SUB) too.
        same_id = {'/dev/mapper/vg0-media': 'dev-1', '/dev/mapper/vg0-media--snap': 'dev-1'}
        self.assertEqual(neko_common.look_alikes(HDD, 'btrfs', run=fake_run(LVM_SNAPSHOT, same_id)), [])
        self.assertEqual(neko_common.devices_with_uuid(HDD, run=fake_run(LVM_SNAPSHOT, {})), ['/dev/mapper/vg0-media'])

    def test_a_real_copy_beside_an_lvm_snapshot_is_still_refused(self):
        failures = self.run_checks(
            [{'path': '/dev/sdd1', 'uuid': HDD, 'type': 'part', 'name': 'sdd1', 'kname': 'sdd1', 'pkname': 'sdd'}],
            devices=LVM_SNAPSHOT,
        )
        self.assertEqual(len(failures), 2)
        for text in failures:
            self.assertIn('/dev/mapper/vg0-media and /dev/sdd1 carry', text)

    def test_only_lvms_own_layers_and_snapshots_are_left_out(self):
        self.assertEqual(
            neko_common.lvm_snapshots(LVM_SNAPSHOT),
            {'/dev/mapper/vg0-media-real', '/dev/mapper/vg0-media--snap', '/dev/mapper/vg0-media--snap-cow'},
        )
        # LVM doubles a '-' inside a name: a volume named "media-real" is no hidden layer.
        self.assertFalse(neko_common.lvm_layer('vg0-media--real', 'real'))
        self.assertTrue(neko_common.lvm_layer('vg0-media---real', 'real'))  # the layer of a volume named "media-"
        self.assertTrue(neko_common.lvm_layer('vg0-media--snap-cow', 'cow'))
        # Two plain volumes with one UUID (a dd copy from one volume to another) stay a copy.
        copied = [lvm_row('vg0-media', 'dm-0', 'sdc1', HDD), lvm_row('vg0-copy', 'dm-4', 'sdc1', HDD)]
        self.assertEqual(neko_common.lvm_snapshots(copied), set())
        self.assertEqual(
            neko_common.look_alikes(HDD, 'ext4', run=fake_run(copied, {})),
            ['/dev/mapper/vg0-copy', '/dev/mapper/vg0-media'],
        )

    def test_a_pool_an_unreadable_device_id_or_no_lsblk_passes(self):
        pool = [{'path': '/dev/sde1', 'uuid': 'aaaa-1111'}]
        self.assertEqual(self.run_checks(pool, {'/dev/sdb1': 'dev-1', '/dev/sde1': 'dev-2'}), [])
        self.assertEqual(self.run_checks(pool, blkid_code=2), [])
        self.assertEqual(self.run_checks([{'path': '/dev/sdd1', 'uuid': 'bbbb-2222'}], lsblk_code=1), [])
        # An LVM volume lsblk lists under each disk it spans is one device.
        twice = [{'path': '/dev/sdc1', 'uuid': 'bbbb-2222'}]
        self.assertEqual(self.run_checks(twice), [])
