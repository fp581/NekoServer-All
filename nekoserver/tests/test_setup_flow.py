"""The install and first-setup flow (review of 0.39.5): which settings file the commands
in the docs use, and the questions and screens on the way."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import installer


class SettingsFileTests(unittest.TestCase):
    """`sudo bash install.sh --module server_os --apply` (as the docs show it) ran on the
    template config.json, with no drives in it, and saved the answers into that file,
    which git tracks: the next `git pull` failed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'config.json').write_text('{}')

    def run_main(self, *argv):
        seen = []
        with mock.patch.object(installer, 'ROOT', self.root), \
                mock.patch.object(installer, 'execute', side_effect=lambda args: seen.append(args.config) or 0):
            code = installer.main(list(argv))
        return code, seen

    def test_your_settings_file_is_used_once_the_wizard_has_made_it(self):
        (self.root / 'my-config.json').write_text('{}')
        code, seen = self.run_main('--module', 'server_os', '--apply')
        self.assertEqual((code, Path(seen[0]).name), (0, 'my-config.json'))

    def test_changes_are_never_saved_into_the_template(self):
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit) as stopped:
            self.run_main('--module', 'server_os', '--apply')
        self.assertEqual(stopped.exception.code, 2)
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit):
            self.run_main('--module', 'server_os', '--apply', '--config', str(self.root / 'config.json'))

    def test_a_template_someone_put_their_drives_in_says_how_to_keep_them(self):
        import io
        (self.root / 'config.json').write_text(json.dumps({'storage': {'data_ssd': {'uuid': 'abcd-1234'}}}))
        err = io.StringIO()
        with mock.patch('sys.stderr', err), self.assertRaises(SystemExit):
            self.run_main('--module', 'server_os', '--apply')
        self.assertIn('cp config.json my-config.json', err.getvalue())

    def test_a_check_may_still_read_the_template(self):
        code, seen = self.run_main('--check')
        self.assertEqual((code, Path(seen[0]).name), (0, 'config.json'))


class PreflightTests(unittest.TestCase):
    """SELinux enforcing (Fedora's default) stopped setup only after minutes of package
    installs and the runtime build; install.sh now asks first, and again after them."""

    def test_what_setup_cannot_run_without_is_said_before_anything_is_installed(self):
        import io

        import capabilities
        rows = [capabilities.row('selinux', 'SELinux', 'enforcing', False, True, problem=capabilities.SELINUX_ENFORCING),
                capabilities.row('docker', 'Docker Engine', 'not installed yet (setup installs it)', True),
                capabilities.row('memory', 'Memory', '4 GB', False)]
        out = io.StringIO()
        self.assertEqual(capabilities.preflight(rows, out), 1)
        self.assertIn('setenforce 0', out.getvalue())
        self.assertNotIn('Docker', out.getvalue())     # installed later by setup itself
        self.assertNotIn('Memory', out.getvalue())     # a note, not a reason to stop
        self.assertEqual(capabilities.preflight(rows[1:], io.StringIO()), 0)

    def test_install_sh_checks_before_the_packages_and_again_before_setup(self):
        script = (Path(installer.ROOT) / 'install.sh').read_text()
        first = script.index('capabilities.py" preflight')
        self.assertLess(first, script.index('pacman -Syu --noconfirm'))      # before the system update
        self.assertLess(first, script.index('zypper_run --quiet refresh'))
        self.assertLess(first, script.index('host.py" bootstrap'))
        second = script.rindex('capabilities.py" preflight')
        self.assertLess(script.index('runtime/build.py'), second)
        self.assertLess(second, script.index('exec python3 "$ROOT/installer.py"'))


class SetupWindowTests(unittest.TestCase):
    """setup.js run in Node against a stand-in page (skipped without Node)."""

    def page(self, states, action=''):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        here = Path(__file__).resolve().parent
        result = subprocess.run([node, str(here / 'setup_window_harness.mjs'), json.dumps(states), action],
                                capture_output=True, text=True, timeout=60, check=True)
        return json.loads(result.stdout)

    def choose(self):
        extras = [{'id': name, 'title': name, 'explanation': '', 'installs': [], 'on': True}
                  for name in ('tv', 'notifications', 'heartbeat', 'nextcloud')]
        return {'version': 'x', 'phase': 'choose', 'extras': extras, 'steps': [], 'current': None, 'done': 0,
                'total': 0, 'question': None, 'terminal': None, 'finish': [], 'complete': False}

    def test_start_without_touching_a_switch_decides_nothing(self):
        # It sent every switch as it was drawn (all on): TV, alerts, heartbeat and
        # Nextcloud were all saved as wanted, never asked about.
        seen = self.page([self.choose()], 'start')
        self.assertEqual(seen['posts'], [['api/start', {'choices': {}}]])

    def test_the_finish_title_is_setups_own_verdict(self):
        done = {**self.choose(), 'phase': 'finished', 'steps': [{'id': 'a', 'title': 'A', 'status': 'success'}],
                'finish': ['  NekoServer is almost ready'], 'complete': False}
        self.assertEqual(self.page([done])['title'], 'NekoServer is almost ready')
        self.assertEqual(self.page([{**done, 'complete': True}])['title'], 'NekoServer is ready')


class SavedSettingsTests(unittest.TestCase):
    """After Linux is reinstalled, setup made new settings before it could see the SSD
    and then wrote them over the copy kept there, the one recovery needs."""

    def test_the_earlier_settings_are_kept_beside_the_new_ones(self):
        from modules import recovery
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'config.json'
            saved.write_text(json.dumps({'storage': 'the real drives'}))
            logged = []
            for run in range(7):
                recovery.keep_previous(saved, {'storage': 'new', 'run': run}, logged.append, now=1_800_000_000 + run)
                saved.write_text(json.dumps({'storage': 'new', 'run': run}))
            kept = sorted(Path(d).glob('config.previous-*.json'))
            self.assertEqual(len(kept), recovery.PREVIOUS_KEPT)
            # The settings from before (the first ones kept) are never rotated out.
            self.assertIn({'storage': 'the real drives'}, [json.loads(p.read_text()) for p in kept])
            recovery.keep_previous(saved, {'storage': 'new', 'run': 6}, logged.append, now=1_900_000_000)
            self.assertEqual(len(sorted(Path(d).glob('config.previous-*.json'))), recovery.PREVIOUS_KEPT)  # same: nothing new
            self.assertIn('config.previous-', logged[0])

    def test_the_first_copy_is_kept_from_the_first_change(self):
        from modules import recovery
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'config.json'
            saved.write_text(json.dumps({'storage': 'the real drives'}))
            recovery.keep_previous(saved, {'storage': 'new'}, lambda *_: None, now=1_800_000_000)
            [kept] = Path(d).glob('config.previous-*.json')
            self.assertEqual(json.loads(kept.read_text()), {'storage': 'the real drives'})
            # A second change in the same second keeps its own copy.
            saved.write_text(json.dumps({'storage': 'new'}))
            recovery.keep_previous(saved, {'storage': 'newer'}, lambda *_: None, now=1_800_000_000)
            self.assertEqual(len(list(Path(d).glob('config.previous-*.json'))), 2)


class BackupDriveMessageTests(unittest.TestCase):
    def test_a_fat32_stick_is_named_instead_of_connect_it_first(self):
        from modules import photo_backup
        config = {'storage': {'data_ssd': {'uuid': 'ssd'}, 'media_hdd': {'uuid': 'hdd'}}, 'external_backup_mount': '/mnt/usb'}
        inventory = {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'children': [{'name': 'sda1', 'type': 'part', 'mountpoint': '/', 'fstype': 'ext4', 'uuid': 'os'}]},
            {'name': 'sdc', 'type': 'disk', 'tran': 'usb', 'rm': False,
             'children': [{'name': 'sdc1', 'type': 'part', 'fstype': 'vfat', 'uuid': 'AB-CD'}]}]}
        with self.assertRaisesRegex(RuntimeError, r'/dev/sdc1 is formatted FAT \(FAT32\).*exFAT or ext4'):
            photo_backup.select_external(config, inventory)
        # The system disk's own FAT boot partition is never named, and neither is an
        # internal disk (Windows on a second disk): never told to format those.
        inventory['blockdevices'][0]['children'].append({'name': 'sda2', 'type': 'part', 'fstype': 'vfat', 'uuid': 'EF'})
        inventory['blockdevices'][1] = {'name': 'nvme1n1', 'type': 'disk', 'tran': 'nvme', 'rm': False, 'children': [
            {'name': 'nvme1n1p1', 'type': 'part', 'fstype': 'vfat', 'uuid': 'EF2'},
            {'name': 'nvme1n1p3', 'type': 'part', 'fstype': 'ntfs', 'uuid': 'WIN'}]}
        with self.assertRaises(RuntimeError) as caught:
            photo_backup.select_external(config, inventory)
        self.assertIn('connect it first', str(caught.exception))
        self.assertNotIn('nvme1n1', str(caught.exception))
        self.assertNotIn('this erases it', str(caught.exception))



class DockerOrderTests(unittest.TestCase):
    def test_the_ssd_data_root_is_never_set_without_the_drive_guard(self):
        # daemon.json pointed Docker at the SSD folder before the guard existed; a run
        # stopped in between let Docker start at boot before the SSD and fill that
        # folder on the system disk, which then refused the SSD's mount.
        from modules import docker
        written = []
        config = {'data_mount': '/srv/nekoserver', 'docker': {'install_engine': True}}
        context = mock.Mock(config=config, apply=True, log=lambda *_: None)
        # Not this PC's own daemon.json: on a PC running NekoServer it names the SSD already.
        daemon = Path(tempfile.mkdtemp()) / 'daemon.json'
        self.addCleanup(shutil.rmtree, daemon.parent, True)
        with mock.patch.object(docker, 'DAEMON_FILE', daemon),                 mock.patch.object(docker, 'mounted_data_root', return_value=('/srv/nekoserver', '/dev/sdb1')), \
                mock.patch.object(docker.os, 'geteuid', return_value=0), \
                mock.patch.object(docker.shutil, 'which', return_value='/usr/bin/x'), \
                mock.patch.object(docker.host, 'snap_docker', return_value=False), \
                mock.patch.object(docker, 'prepare_daemon_config', return_value={'data-root': '/srv/nekoserver/docker-engine'}), \
                mock.patch.object(docker, 'check_existing_docker'), \
                mock.patch.object(docker.Path, 'mkdir'), \
                mock.patch.object(docker, 'write_atomic', side_effect=lambda path, text: written.append(str(path))), \
                mock.patch.object(docker, 'validate_daemon'), \
                mock.patch.object(docker, 'install_storage_guard', side_effect=RuntimeError('runtime build failed')), \
                mock.patch.object(docker.subprocess, 'run',   # Docker not running; its Compose plugin there
                                  side_effect=lambda argv, **kw: mock.Mock(returncode=3 if 'is-active' in argv else 0)), \
                self.assertRaisesRegex(RuntimeError, 'runtime build failed'):
            docker.run(context)
        self.assertNotIn(str(daemon), written)


class DriveLayoutTests(unittest.TestCase):
    """Btrfs pools over several drives stopped setup ("Duplicate filesystem UUID"), and
    encrypted, LVM and RAID drives were never offered."""

    def setUp(self):
        from modules import storage
        self.real_profiles = storage.data_profiles
        # A pool's data profile is never read from this computer's own drives here.
        profiles = mock.patch.object(storage, 'data_profiles', return_value=None)
        profiles.start()
        self.addCleanup(profiles.stop)

    # Owner review of 0.40.2: a Btrfs pool's size was its first drive's, so the same
    # 32 GB + 4 TB pool listed the other way round went from "too small" to "big enough".
    def pool_both_ways(self, profiles=None, statvfs=None, mountpoint=None):
        """The pool of a 32 GB and a 4 TB drive, as discovered with the drives listed in
        each order."""
        from modules import storage
        found = []
        for sizes in (('32G', '4T'), ('4T', '32G')):
            members = [self.fs(name, 'pool', fstype='btrfs', mountpoint=mountpoint) for name in ('sdb1', 'sdc1')]
            for row, size in zip(members, sizes):
                row['size'] = size
            inventory = {'blockdevices': [self.disk('sdb', members[0]), self.disk('sdc', members[1])]}
            with mock.patch.object(storage, 'data_profiles', return_value=profiles), \
                    mock.patch.object(storage.os, 'statvfs', side_effect=statvfs or OSError(2, 'not mounted')):
                [pool] = storage.discover_filesystems(inventory, {'sdb1': 'dev-1', 'sdc1': 'dev-2'}.get)
            found.append(pool)
        self.assertEqual(found[0]['size'], found[1]['size'], 'the order the drives are listed in changes nothing')
        return found[0]

    def test_a_pool_whose_layout_cannot_be_read_counts_only_its_smallest_drive(self):
        from modules import storage
        pool = self.pool_both_ways()
        self.assertEqual(storage.size_bytes(pool['size']), 32 * 1024 ** 3)
        self.assertIn('only its smallest drive is counted, to be safe', storage.describe(pool))
        with self.assertRaisesRegex(RuntimeError, 'No drive here is big enough.*32 GB.*could not be read'):
            storage.ask_drive([pool], 'media_hdd', 'the HDD', lambda q: '', lambda s: None)

    def test_a_raid1_pool_holds_what_its_smallest_mirror_holds_and_a_single_one_the_sum(self):
        from modules import storage
        mirrored = self.pool_both_ways(profiles={'RAID1'})
        self.assertEqual(storage.size_bytes(mirrored['size']), 32 * 1024 ** 3)
        self.assertIn('keeping its data as RAID1', storage.describe(mirrored))
        self.assertFalse(storage.big_enough(mirrored, storage.sizes('media_hdd')[0]))
        single = self.pool_both_ways(profiles={'SINGLE'})
        self.assertEqual(storage.size_bytes(single['size']), (4 * 1024 + 32) * 1024 ** 3)
        self.assertTrue(storage.big_enough(single, storage.sizes('media_hdd')[0]))

    def test_a_drive_of_exactly_the_minimum_size_is_too_small_once_formatted(self):
        # The drive space step wants the minimum FREE; a drive of that size in all never
        # has it, so the picker says so, instead of accepting it and failing a step later.
        from modules import storage
        exactly = {'name': 'sdb1', 'size': '50G', 'fstype': 'ext4', 'uuid': 'x'}
        enough = {'name': 'sdc1', 'size': '54G', 'fstype': 'ext4', 'uuid': 'y'}
        self.assertFalse(storage.big_enough(exactly, 50))
        self.assertTrue(storage.big_enough(enough, 50))
        said = []
        with self.assertRaisesRegex(RuntimeError, 'No drive here is big enough'):
            storage.ask_drive([exactly], 'media_hdd', 'the HDD', lambda q: '', said.append)

    def test_a_mounted_pool_counts_what_the_pool_itself_reports(self):
        from modules import storage
        reported = mock.Mock(f_blocks=1000, f_bfree=900, f_bavail=700, f_frsize=1024 ** 3 // 25)
        pool = self.pool_both_ways(statvfs=lambda folder: reported, mountpoint='/run/media/neko/pool')
        self.assertEqual(storage.size_bytes(pool['size']), 800 * (1024 ** 3 // 25))
        self.assertIn('usable space as the pool itself reports it', storage.describe(pool))

    def test_usable_space_by_data_profile(self):
        from modules import storage
        gib = 1024 ** 3
        cases = {('RAID1', (32, 4096)): 32, ('RAID1', (1000, 1000, 1000)): 1500, ('RAID1C3', (1000, 1000, 1000)): 1000,
                 ('RAID1C4', (1000, 1000, 1000)): 0, ('DUP', (100, 300)): 200, ('RAID0', (32, 4096)): 64,
                 ('RAID10', (500, 500, 500, 500)): 1000, ('RAID6', (32, 4096, 4096)): 32}
        for (profile, sizes), usable in cases.items():
            self.assertEqual(storage.profile_capacity(profile, [s * gib for s in sizes]), usable * gib, (profile, sizes))
        chunks = ('item 2 key (FIRST_CHUNK_TREE CHUNK_ITEM 13631488) itemoff 15975 itemsize 112\n'
                  '\t\tlength 8388608 owner 2 stripe_len 65536 type SYSTEM|RAID1\n'
                  '\t\tlength 1073741824 owner 2 stripe_len 65536 type METADATA|RAID1\n'
                  '\t\tlength 1073741824 owner 2 stripe_len 65536 type DATA|RAID1\n'
                  '\t\tlength 1073741824 owner 2 stripe_len 65536 type DATA\n')   # a conversion from single, unfinished
        with mock.patch.object(storage.os.path, 'exists', return_value=True), \
                mock.patch.object(storage, 'call', return_value=chunks) as called:
            self.assertEqual(self.real_profiles(['sdb1']), {'RAID1', 'SINGLE'})
        self.assertEqual(called.call_args.args[0], ['btrfs', 'inspect-internal', 'dump-tree', '-t', 'chunk', '/dev/sdb1'])

    def disk(self, name, *children, mountpoint=None):
        return {'name': name, 'type': 'disk', 'size': '1T', 'children': list(children), 'mountpoint': mountpoint}

    def fs(self, name, uuid, kind='part', fstype='ext4', mountpoint=None, children=()):
        return {'name': name, 'type': kind, 'fstype': fstype, 'uuid': uuid, 'size': '1T', 'mountpoint': mountpoint,
                'children': list(children)}

    def test_a_btrfs_pool_over_two_drives_is_one_choice_on_both(self):
        from modules import storage
        inventory = {'blockdevices': [
            self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
            self.disk('sdb', self.fs('sdb1', 'pool', fstype='btrfs')),
            self.disk('sdc', self.fs('sdc1', 'pool', fstype='btrfs'))]}
        devices = {'sdb1': 'dev-1', 'sdc1': 'dev-2'}   # each pool member has a UUID_SUB of its own
        [pool] = storage.discover_filesystems(inventory, devices.get)
        self.assertEqual((pool['uuid'], pool['physical_disks']), ('pool', ['sdb', 'sdc']))

    def test_a_cloned_btrfs_drive_is_not_taken_for_a_pool(self):
        # dd or a disk cloner copies the device's own UUID_SUB too: mounted by UUID, the
        # kernel could write to either drive.
        from modules import storage
        inventory = {'blockdevices': [
            self.disk('sdb', self.fs('sdb1', 'pool', fstype='btrfs')),
            self.disk('sdc', self.fs('sdc1', 'pool', fstype='btrfs'))]}
        devices = {'sdb1': 'dev-1', 'sdc1': 'dev-1'}
        with self.assertRaisesRegex(RuntimeError, 'cloned drives'):
            storage.discover_filesystems(inventory, devices.get)
        # A device ID blkid could not read (no sudo) still stops setup, but the drives are
        # not called copies: it says what could not be read, and to use sudo.
        with self.assertRaisesRegex(RuntimeError, r'Could not read the Btrfs device ID of /dev/sdc1.*sudo') as caught:
            storage.discover_filesystems(inventory, {'sdb1': 'dev-1'}.get)
        self.assertNotIn('clone', str(caught.exception))

    def test_lvm_raid_and_encrypted_volumes_are_offered(self):
        from modules import storage
        lv = self.fs('data-media', 'lv', kind='lvm')
        inventory = {'blockdevices': [
            self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
            self.disk('sdb', self.fs('sdb1', None, fstype='LVM2_member', children=[lv])),
            self.disk('sdc', self.fs('sdc1', None, fstype='LVM2_member', children=[lv])),   # the LV spans both
            self.disk('sdd', self.fs('sdd1', None, fstype='crypto_LUKS', children=[self.fs('secret', 'luks', kind='crypt')])),
            self.disk('sde', self.fs('sde1', None, fstype='linux_raid_member', children=[self.fs('md0', 'md', kind='raid1')])),
            self.disk('sdf', self.fs('sdf1', None, fstype='linux_raid_member', children=[self.fs('md0', 'md', kind='raid1')]))]}
        found = {v['uuid']: v['physical_disks'] for v in storage.discover_filesystems(inventory)}
        self.assertEqual(found, {'lv': ['sdb', 'sdc'], 'luks': ['sdd'], 'md': ['sde', 'sdf']})

    def test_two_clones_of_one_drive_still_stop_setup(self):
        from modules import storage
        inventory = {'blockdevices': [self.disk('sdb', self.fs('sdb1', 'same')), self.disk('sdc', self.fs('sdc1', 'same'))]}
        with self.assertRaisesRegex(RuntimeError, 'cloned drives'):
            storage.discover_filesystems(inventory)

    def test_the_systems_own_btrfs_raid_is_never_offered(self):
        from modules import storage
        inventory = {'blockdevices': [
            self.disk('sda', self.fs('sda1', 'root', fstype='btrfs', mountpoint='/')),
            self.disk('sdb', self.fs('sdb1', 'root', fstype='btrfs')),
            self.disk('sdc', self.fs('sdc1', 'ssd'))]}
        # Never offered, so never checked for clones either (even where blkid can't say).
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(inventory, lambda name: None)], ['ssd'])

    def test_a_raid_array_that_lost_a_drive_is_refused_at_setup(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / 'md0' / 'md'
            for member, state in (('dev-sda1', 'in_sync'), ('dev-sdb1', 'in_sync')):
                (md / member).mkdir(parents=True)
                (md / member / 'state').write_text(state + '\n')
            (md / 'degraded').write_text('0\n')
            device = {'name': 'md0', 'kind': 'raid1', 'uuid': 'x'}
            self.assertIs(storage.whole(device, sys_block=d), device)
            (md / 'degraded').write_text('1\n')
            (md / 'dev-sdb1' / 'state').write_text('faulty\n')
            with self.assertRaisesRegex(RuntimeError, 'md0 has lost a drive .*sdb1'):
                storage.whole(device, sys_block=d)
            # Not an array: nothing in /sys is read for it.
            self.assertEqual(storage.whole({'name': 'sdc1', 'kind': 'part'}, sys_block=d)['name'], 'sdc1')

    def test_a_rerun_warns_about_a_degraded_array_instead_of_stopping(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / 'md0' / 'md'
            md.mkdir(parents=True)
            (md / 'degraded').write_text('1\n')
            with mock.patch.object(storage, 'findmnt', return_value='/dev/md0[/@data]'):
                note = storage.raid_note('/srv/nekoserver', sys_block=d)
            self.assertIn('one copy fewer', note)
            self.assertIn('md0', note)
            with mock.patch.object(storage, 'findmnt', return_value='/dev/sdb1'):
                self.assertIsNone(storage.raid_note('/srv/nekoserver', sys_block=d))

    def test_an_encrypted_drive_must_be_unlocked_at_every_start(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as d:
            crypttab = Path(d) / 'crypttab'
            crypttab.write_text('# <name> <device> <key>\nother UUID=1 none\n')
            device = {'name': 'secret', 'kind': 'crypt'}
            self.assertIn('/etc/crypttab', storage.boot_unlock_problem(device, crypttab))
            crypttab.write_text('secret UUID=2 /root/key luks\n')
            self.assertIsNone(storage.boot_unlock_problem(device, crypttab))
            self.assertIsNone(storage.boot_unlock_problem({'name': 'sdb1', 'kind': 'part'}, crypttab))

    def test_a_drive_unlocked_only_by_hand_or_under_an_lvm_volume_is_named(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as d:
            crypttab = Path(d) / 'crypttab'
            # `noauto`: listed, but Linux does not open it at start-up.
            crypttab.write_text('secret UUID=2 /root/key luks,noauto\n')
            self.assertIn('/dev/mapper/secret', storage.boot_unlock_problem({'name': 'secret', 'kind': 'crypt'}, crypttab))
            # An LVM volume on an encrypted drive opened by hand: the drive under it is named.
            lv = self.fs('vg-data', 'lv', kind='lvm')
            inventory = {'blockdevices': [
                self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
                self.disk('sdb', self.fs('sdb1', None, fstype='crypto_LUKS', children=[
                    self.fs('cryptdata', None, kind='crypt', fstype='LVM2_member', children=[lv])]))]}
            [volume] = storage.discover_filesystems(inventory)
            self.assertEqual(volume['unlocks'], ['cryptdata'])
            crypttab.write_text('other UUID=1 none\n')
            self.assertIn('/dev/mapper/cryptdata', storage.boot_unlock_problem(volume, crypttab))
            crypttab.write_text('cryptdata UUID=3 none luks\n')
            self.assertIsNone(storage.boot_unlock_problem(volume, crypttab))


class SharedLockTests(unittest.TestCase):
    def test_the_lock_never_makes_folders_on_the_system_disk(self):
        import recovery_tools
        with tempfile.TemporaryDirectory() as d:
            config = {'data_mount': d}          # an empty mount point: the SSD is not mounted
            with self.assertRaisesRegex(RuntimeError, 'not mounted'), recovery_tools.lock(config, changes_apps=False):
                pass
            self.assertFalse((Path(d) / 'installer').exists())


class FstabTests(unittest.TestCase):
    def test_a_line_for_the_drive_or_folder_written_another_way_is_found(self):
        # Only "UUID=<uuid>" and the exact folder text were recognised, so a second line
        # was added: the first one wins at boot and mounts something else there.
        from modules import storage
        entry = storage.fstab_line('abcd-1234', 'ext4', '/srv/nekoserver')
        devices = {'/dev/sdb1': 'abcd-1234', 'LABEL=ssd': 'abcd-1234', 'PARTUUID=9-1': 'abcd-1234', '/dev/sdc1': 'other'}
        for theirs in ('/dev/disk/by-uuid/abcd-1234 /data ext4 defaults 0 2', 'UUID="abcd-1234" /data ext4 defaults 0 2',
                       '/dev/sdb1 /data ext4 defaults 0 2', 'LABEL=ssd /data ext4 defaults 0 2',
                       'PARTUUID=9-1 /data ext4 defaults 0 2', '/dev/sdc1 /srv/nekoserver/ ext4 defaults 0 2',
                       'UUID=zz /srv/nek\\040oserver ext4 defaults 0 2'.replace('nek\\040oserver', 'nekoserver')):
            with self.assertRaisesRegex(RuntimeError, 'NekoServer did not write', msg=theirs):
                storage.existing_line([theirs], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get)
        # Another drive at another folder: nothing to do with it.
        self.assertIsNone(storage.existing_line(['/dev/sdc1 /mnt/other ext4 defaults 0 2'], 'abcd-1234',
                                                '/srv/nekoserver', entry, resolve=devices.get))
        # A drive that isn't connected can't be told apart: not taken for this one.
        self.assertIsNone(storage.existing_line(['LABEL=gone /mnt/gone ext4 defaults 0 2'], 'abcd-1234',
                                                '/srv/nekoserver', entry, resolve=devices.get))
        # NekoServer's own line is still its own.
        self.assertEqual(storage.existing_line([entry], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get), 0)

    def test_a_replaced_drive_takes_over_nekoservers_own_fstab_line(self):
        # The SSD broke and a new one was put in (RECOVERY.md): the line NekoServer wrote
        # for the old drive is rewritten for the new one, and setup says so.
        from modules import storage
        entry = storage.fstab_line('new-1111', 'ext4', '/srv/nekoserver')
        old = storage.fstab_line('old-9999', 'ext4', '/srv/nekoserver')
        notes = []
        self.assertEqual(storage.existing_line([old], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get,
                                               connected=lambda u: False, notes=notes), 0)
        self.assertIn('old-9999', notes[0])
        self.assertIn('not connected', notes[0])
        # The old drive is still connected: that is someone's drive, so setup stops as before.
        with self.assertRaisesRegex(RuntimeError, 'did not write'):
            storage.existing_line([old], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get, connected=lambda u: True)
        # A line with the folder but not NekoServer's options is never taken over.
        theirs = 'UUID=old-9999 /srv/nekoserver ext4 defaults 0 2'
        with self.assertRaisesRegex(RuntimeError, 'already has a line'):
            storage.existing_line([theirs], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get, connected=lambda u: False)
        # The whole line is rewritten in the file, and the change is said.
        with tempfile.TemporaryDirectory() as d:
            fstab = Path(d) / 'fstab'
            fstab.write_text('# keep\n' + old + '\n')
            said = []
            self.assertTrue(storage.install_fstab([('data_ssd', 'new-1111', 'ext4', '/srv/nekoserver', None)], fstab, said.append))
            self.assertEqual(fstab.read_text(), '# keep\n' + entry + '\n')
            self.assertEqual(len(said), 1)

    def test_the_first_fstab_copy_is_never_left_cut_off(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as d:
            fstab = Path(d) / 'fstab'
            fstab.write_text('UUID=os / ext4 defaults 0 1\n')
            spec = [('data_ssd', 'abcd-1234', 'ext4', str(Path(d) / 'ssd'), None)]
            self.assertTrue(storage.install_fstab(spec, fstab))
            backup = Path(d) / 'fstab.nekoserver.bak'
            self.assertEqual(backup.read_text(), 'UUID=os / ext4 defaults 0 1\n')
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ['fstab', 'fstab.nekoserver.bak'])

    def test_a_slow_mount_is_waited_for(self):
        from modules import storage
        self.assertGreaterEqual(storage.MOUNT_WAIT, 300)
        seen = []
        with mock.patch.object(storage, 'findmnt', side_effect=[None, '/dev/sdb1', '/dev/sdb1']), \
                mock.patch.object(storage, 'call', side_effect=lambda argv, timeout=30: seen.append(timeout) or ''), \
                mock.patch.object(storage, 'mounted_filesystem', return_value=('abcd-1234', 'btrfs')), \
                mock.patch.object(storage, 'mounted_subvolume', return_value=None), \
                mock.patch.object(storage.Path, 'mkdir'), mock.patch.object(storage.Path, 'exists', return_value=True):
            storage.mount_and_check('abcd-1234', '/srv/nekoserver', None)
        self.assertEqual(seen, [storage.MOUNT_WAIT])


class DockerSettingsTests(unittest.TestCase):
    def test_a_setting_dockers_service_already_sets_stops_before_anything_changes(self):
        from modules import docker
        content = {'data-root': '/srv/nekoserver/docker-engine', 'log-opts': {'max-size': '10m'}}
        shown = mock.Mock(returncode=0, stdout='{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// --data-root=/data/docker ; }')
        with self.assertRaisesRegex(RuntimeError, 'data-root on its command line'):
            docker.validate_daemon(content, run=lambda *a, **k: shown)
        self.assertEqual(docker.flag_conflicts(content, '/usr/bin/dockerd --log-opt max-size=5m'), ['log-opts'])
        self.assertEqual(docker.flag_conflicts(content, '/usr/bin/dockerd -H fd://'), [])

    def test_a_daemon_json_docker_refuses_is_never_written(self):
        from modules import docker

        def run(argv, **kw):
            if '--validate' in argv:
                return mock.Mock(returncode=1, stdout='', stderr='invalid configuration: unknown key "nope"')
            return mock.Mock(returncode=0, stdout='/usr/bin/dockerd -H fd://')
        with mock.patch.object(docker.shutil, 'which', return_value='/usr/bin/dockerd'):
            with self.assertRaisesRegex(RuntimeError, 'refuses the new'):
                docker.validate_daemon({'nope': 1}, run=run)
            # Docker before 23 has no --validate: not taken for a refusal.
            def old(argv, **kw):
                return mock.Mock(returncode=125, stdout='', stderr='unknown flag: --validate')
            docker.validate_daemon({}, run=old)


class ReadBackTests(unittest.TestCase):
    def test_a_restored_file_is_read_back_from_the_drive_not_from_memory(self):
        import neko_common
        advised = []
        real = neko_common.os.posix_fadvise
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a').write_bytes(b'photo')
            with mock.patch.object(neko_common.os, 'posix_fadvise',
                                   side_effect=lambda fd, o, n, how: advised.append(how) or real(fd, o, n, how)):
                neko_common.copy_checked(Path(d) / 'a', Path(d) / 'b')
        self.assertIn(neko_common.os.POSIX_FADV_DONTNEED, advised)


class SambaShareTests(unittest.TestCase):
    def test_a_share_path_cannot_add_settings_of_its_own(self):
        from modules import samba
        self.assertEqual(samba.validate_share('Media', 'library/My Movies'), Path('library/My Movies'))
        for bad in ('library\n   root preexec = /bin/sh -c x', 'library/%U', 'a\\b'):
            with self.assertRaisesRegex(RuntimeError, 'may use only'):
                samba.validate_share('Media', bad)


class SnapshotErrorTests(unittest.TestCase):
    def test_a_failed_restart_never_hides_why_the_snapshot_failed(self):
        import io

        import recovery_tools
        err = io.StringIO()
        with mock.patch.object(recovery_tools, 'restart_owed', side_effect=RuntimeError('docker is down')), \
                mock.patch('sys.stderr', err):
            recovery_tools.restart_again({}, [], failed=True)        # said, not raised
            self.assertIn('docker is down', err.getvalue())
            with self.assertRaisesRegex(RuntimeError, 'docker is down'):
                recovery_tools.restart_again({}, [], failed=False)   # the snapshot passed: this is the error


class AnswerRaceTests(unittest.TestCase):
    def test_only_one_side_is_told_its_answer_was_used(self):
        import threading

        import setup_progress
        pending = {'seq': 7, 'answer': None, 'event': threading.Event()}
        with mock.patch.object(setup_progress, '_pending', pending):
            self.assertTrue(setup_progress._claimed_by_terminal(pending, 'yes'))
            self.assertFalse(setup_progress.answer_from_window(7, 'no'))   # too late: told so
            self.assertEqual(pending['answer'], 'yes')
        # The window first: the terminal's line gives way.
        other = {'seq': 8, 'answer': None, 'event': threading.Event()}
        with mock.patch.object(setup_progress, '_pending', other):
            self.assertTrue(setup_progress.answer_from_window(8, 'no'))
            self.assertFalse(setup_progress._claimed_by_terminal(other, 'yes'))
            self.assertEqual(other['answer'], 'no')


class StateMoveTests(unittest.TestCase):
    def test_an_old_record_on_the_system_disk_never_undoes_newer_results(self):
        here = {'system': {'status': 'success'}, 'storage': {'status': 'success'}, 'docker': {'status': 'blocked'}}
        on_ssd = {'docker': {'status': 'success'}, 'media': {'status': 'success'}}
        merged = installer.moved_states(on_ssd, here, ['system', 'storage'])
        self.assertEqual(merged['docker'], {'status': 'success'})        # the SSD's newer result
        self.assertEqual(merged['storage'], {'status': 'success'})       # what this run did
        self.assertEqual(merged['media'], {'status': 'success'})
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / 'state.json'
            old.write_text('{}')
            installer.retire(old)
            self.assertFalse(old.exists())
            self.assertTrue((Path(d) / 'state.json.moved-to-ssd').exists())


class AccountStoreTests(unittest.TestCase):
    def test_a_first_save_cut_off_does_not_lock_the_store_for_good(self):
        import account_store
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'secrets/accounts'
            root.mkdir(parents=True, mode=0o700)
            (root / '.credentials.json-abc123').write_text('{"half": ')   # left by a power cut
            with mock.patch.object(account_store, 'check_parent'), mock.patch.object(account_store, 'private'):
                value = account_store.load({'data_mount': d})
            self.assertEqual(value['apps'], {})
            self.assertEqual(list(root.iterdir()), [])
            (root / 'something-else').write_text('x')       # anything else is still left for a person
            with mock.patch.object(account_store, 'check_parent'), mock.patch.object(account_store, 'private'), \
                    self.assertRaisesRegex(RuntimeError, 'Incomplete account store'):
                account_store.load({'data_mount': d})


class UsbPickerSystemDiskTests(unittest.TestCase):
    def test_a_btrfs_system_disk_showing_home_is_still_the_system_disk(self):
        from modules import photo_backup
        config = {'storage': {}, 'external_backup_mount': '/mnt/usb'}
        inventory = {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'children': [
                {'name': 'sda2', 'type': 'part', 'fstype': 'btrfs', 'uuid': 'root', 'mountpoint': '/home',
                 'mountpoints': ['/home', '/', '/.snapshots']},
                {'name': 'sda3', 'type': 'part', 'fstype': 'ext4', 'uuid': 'spare'}]}]}
        self.assertEqual(photo_backup.eligible(config, inventory), [])

    def test_every_disk_of_a_btrfs_system_pool_is_the_system_disk(self):
        # A root pool over sda2 and sdb1: lsblk shows the mount on one member only.
        from modules import photo_backup
        config = {'storage': {}, 'external_backup_mount': '/mnt/usb'}
        inventory = {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'children': [
                {'name': 'sda2', 'type': 'part', 'fstype': 'btrfs', 'uuid': 'pool', 'mountpoints': ['/']}]},
            {'name': 'sdb', 'type': 'disk', 'children': [
                {'name': 'sdb1', 'type': 'part', 'fstype': 'btrfs', 'uuid': 'pool'},
                {'name': 'sdb2', 'type': 'part', 'fstype': 'ext4', 'uuid': 'spare'}]},
            {'name': 'sdc', 'type': 'disk', 'children': [
                {'name': 'sdc1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'usb'}]}]}
        self.assertEqual([r['uuid'] for r in photo_backup.eligible(config, inventory)], ['usb'])
        self.assertIn('sdb', photo_backup.shared_disk(config, inventory, 'spare'))

if __name__ == '__main__':
    unittest.main()


class StaleProfileTests(unittest.TestCase):
    """A settings file keeps the profiles it was made with: on a server set up before
    0.28.7 the desktop step (and before 0.24.1 the graphics-chip video step) never ran,
    and the finishing screen still said "ready"."""

    def setUp(self):
        self.template = json.loads((installer.ROOT / 'config.json').read_text())
        self.old = json.loads(json.dumps(self.template))
        self.old['project_name'] = 'NekoServer v0.23.1'
        full = self.old['profiles']['full']
        for step in ('desktop', 'hardware_video', 'samba'):   # samba: taken out by its owner
            full.remove(step)

    def test_steps_added_since_are_merged_and_steps_taken_out_stay_out(self):
        import config_checks
        lines, changed = config_checks.merge_profiles(self.old, self.template)
        full = self.old['profiles']['full']
        self.assertTrue(changed)
        self.assertIn('desktop', full)
        self.assertIn('hardware_video', full)
        self.assertNotIn('samba', full)            # in the 0.23.1 template already: taken out on purpose
        self.assertLess(full.index('dashboard'), full.index('desktop'))   # in the template's order
        self.assertEqual(len(lines), 2)
        # Taken out again after the merge: the record now knows it, so it stays out.
        full.remove('desktop')
        lines, _ = config_checks.merge_profiles(self.old, self.template)
        self.assertEqual(lines, [])
        self.assertNotIn('desktop', self.old['profiles']['full'])
        # A step a later release adds is found through the record.
        newer = json.loads(json.dumps(self.template))
        newer['profiles']['full'].append('future_step')
        lines, _ = config_checks.merge_profiles(self.old, newer)
        self.assertIn('future_step', self.old['profiles']['full'])

    def test_your_own_additions_stay(self):
        import config_checks
        self.old['profiles']['full'].append('my_step')
        config_checks.merge_profiles(self.old, self.template)
        self.assertIn('my_step', self.old['profiles']['full'])

    def test_the_finishing_screen_judges_against_the_templates_steps(self):
        import wizard
        states = {name: {'status': 'success'} for name in self.old['profiles']['full']}
        self.assertFalse(wizard.finishing_screen(self.old, states, None, lambda *_: None, checkup=True))
        states.update(desktop={'status': 'success'}, hardware_video={'status': 'success'})
        self.assertTrue(wizard.finishing_screen(self.old, states, None, lambda *_: None, checkup=True))

    def test_setup_plans_the_new_steps_and_saves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            settings.write_text(json.dumps(self.old))
            args = installer.argparse.Namespace(config=str(settings), state_dir=tmp + '/state', profile='full', module=None,
                                                retry_failed=False, check=False, dry_run=True, apply=False)
            shown = []
            with mock.patch('builtins.print', side_effect=lambda *a, **k: shown.append(' '.join(map(str, a)))):
                installer._execute(args)
            self.assertTrue(any(line.startswith('  desktop:') for line in shown), shown)
            self.assertTrue(any('"hardware_video"' in line for line in shown), shown)
            self.assertNotIn('desktop', json.loads(settings.read_text())['profiles']['full'])   # read-only: not saved


class SavedProfileTests(unittest.TestCase):
    def test_the_added_steps_are_logged_and_saved_only_with_apply(self):
        class Ctx:
            def __init__(self):
                self.lines, self.saved = [], 0

            def log(self, line):
                self.lines.append(line)

            def persist_config(self):
                self.saved += 1
        ctx = Ctx()
        installer.save_profiles(ctx, ['Settings: the step "desktop" ...'], True, False)
        self.assertEqual((ctx.lines, ctx.saved), (['Settings: the step "desktop" ...'], 0))
        installer.save_profiles(ctx, [], True, True)   # only the record of known steps changed
        self.assertEqual(ctx.saved, 1)
