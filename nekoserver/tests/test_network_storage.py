"""Drives on a network storage box (a NAS) over iSCSI: the connection step
(modules/network_storage.py) and what the drive step does with such a disk."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host  # noqa: E402
from modules import network_storage as nas  # noqa: E402
from modules import storage  # noqa: E402

IQN = 'iqn.2013-03.com.wdc:ex4100:media'
BACKUP = 'iqn.2013-03.com.wdc:ex4100:backup'
PORTAL = '192.168.1.20:3260'


def done(code=0, out='', err=''):
    return mock.Mock(returncode=code, stdout=out, stderr=err)


class ConnectionTests(unittest.TestCase):
    def test_an_address_is_read_as_the_nas_portal(self):
        self.assertEqual(nas.portal_of('192.168.1.20'), PORTAL)
        self.assertEqual(nas.portal_of(' nas.home:3261 '), 'nas.home:3261')
        for odd in ('', '192.168.1.300', 'nas home', '1.2.3.4:0', '1.2.3.4:70000', 'a;b', '$(x)'):
            self.assertIsNone(nas.portal_of(odd), odd)

    def test_the_saved_choices_are_checked_before_anything_changes(self):
        import config_checks

        good = {'network_storage': {'wanted': True, 'portal': PORTAL, 'targets': [IQN]}}
        config_checks.validate(good, ['network_storage'])
        for bad in ({'wanted': 'yes'}, {'portal': 'nas home'}, {'targets': ['x; reboot']}, {'targets': IQN}):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                config_checks.validate({'network_storage': bad}, ['network_storage'])

    def test_the_targets_the_nas_offers_are_listed(self):
        shown = f'{PORTAL},1 {IQN}\n{PORTAL},1 {BACKUP}\nnot a line\n{PORTAL},1 bad name\n'
        self.assertEqual(nas.discovered(PORTAL, run=lambda *a, **k: done(out=shown)), [(IQN, PORTAL), (BACKUP, PORTAL)])
        with self.assertRaisesRegex(RuntimeError, 'listed no iSCSI disks .*connection refused'):
            nas.discovered(PORTAL, run=lambda *a, **k: done(4, err='iscsiadm: connection refused'))

    def test_the_login_is_for_good_and_a_session_already_there_is_fine(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return done(nas.SESSION_EXISTS if '--login' in argv else 0)

        with mock.patch.object(nas, 'patient'):
            nas.log_in(IQN, PORTAL, run)
        node = ['iscsiadm', '-m', 'node', '-T', IQN, '-p', PORTAL]
        self.assertIn([*node, '-o', 'update', '-n', 'node.startup', '-v', 'automatic'], calls)
        self.assertEqual(calls[-1], [*node, '--login'])
        # Ten minutes before writes fail (the lab: two minutes aborted ext4's journal).
        self.assertIn([*node, '-o', 'update', '-n', 'node.session.timeo.replacement_timeout', '-v', '600'], calls)
        with tempfile.TemporaryDirectory() as d:
            for n, target in ((1, IQN), (2, BACKUP)):
                (Path(d) / f'session{n}').mkdir()
                (Path(d) / f'session{n}' / 'targetname').write_text(target + '\n')
                (Path(d) / f'session{n}' / 'recovery_tmo').write_text('120\n')
            block, links = Path(d) / 'block', Path(d) / 'by-path'
            (block / 'sdc' / 'device').mkdir(parents=True)
            (block / 'sdc' / 'device' / 'timeout').write_text('30\n')
            links.mkdir()
            (links / f'ip-{PORTAL}-iscsi-{IQN}-lun-1').symlink_to(block / 'sdc')
            nas.patient(IQN, d, block, links)
            self.assertEqual((block / 'sdc' / 'device' / 'timeout').read_text(), '120')
            self.assertEqual((Path(d) / 'session1' / 'recovery_tmo').read_text(), '600')
            self.assertEqual((Path(d) / 'session2' / 'recovery_tmo').read_text(), '120\n')
        refused = lambda argv, **k: done(
            24 if '--login' in argv else 0, err='iscsiadm: Login failed: authorization failure'
        )  # noqa: E731
        with self.assertRaisesRegex(RuntimeError, 'allowed on that target .*CHAP'), mock.patch.object(nas, 'patient'):
            nas.log_in(IQN, PORTAL, refused)

    def test_disks_on_a_nas_wait_for_it_at_every_start(self):
        self.assertIn('KERNELS=="session[0-9]*"', nas.RULE)  # iSCSI disks only
        self.assertIn('ATTR{timeout}="120"', nas.RULE)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(nas, 'run_tool') as tool:
            rule = Path(d) / 'rule'
            nas.install_rule(rule)
            self.assertEqual(rule.read_text(), nas.RULE)
            tool.assert_called_once_with(['udevadm', 'control', '--reload'], 30)
            nas.install_rule(rule)
            self.assertEqual(tool.call_count, 1)

    def test_a_target_listed_at_another_address_is_saved_at_the_one_given(self):
        # Found in the lab: the target listed itself at 127.0.0.1, so the login at the
        # address typed found "No records".
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return done(21 if argv[-1] == PORTAL else 0)  # 21: no records

        nas.known_at(IQN, PORTAL, run)
        self.assertEqual(calls[-1], ['iscsiadm', '-m', 'node', '-T', IQN, '-p', PORTAL, '-o', 'new'])
        calls.clear()
        nas.known_at(IQN, PORTAL, lambda argv, **k: calls.append(argv) or done())
        self.assertEqual(len(calls), 1)  # already there

    def test_a_chap_password_is_written_in_the_targets_settings_never_on_a_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / IQN / '192.168.1.20,3260,1'
            folder.mkdir(parents=True)
            (folder / 'default').write_text('node.name = x\nnode.session.auth.authmethod = None\n')
            nas.set_chap(IQN, PORTAL, 'neko', 'a-long-secret-1', roots=(Path(d) / 'missing', Path(d)))
            record = folder / 'default'
            text = record.read_text()
            self.assertIn('node.session.auth.authmethod = CHAP', text)
            self.assertIn('node.session.auth.password = a-long-secret-1', text)
            self.assertNotIn('authmethod = None', text)
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
            # The older layout: the portal entry is the file itself.
            (folder / 'default').unlink()
            folder.rmdir()
            folder.write_text('node.name = x\n')
            self.assertEqual(nas.node_record(IQN, PORTAL, (Path(d),)), folder)
            with self.assertRaisesRegex(RuntimeError, 'were not found'):
                nas.set_chap(BACKUP, PORTAL, 'neko', 'a-long-secret-1', roots=(Path(d),))
        self.assertTrue(nas.chap_ok('neko', 'twelve-chars'))
        for user, password in (
            ('neko', 'short'),
            ('', 'twelve-chars'),
            ('ne ko ', 'twelve-chars'),
            ('n', 'has=equals12'),
        ):
            self.assertFalse(nas.chap_ok(user, password), (user, password))
        source = (ROOT / 'modules/network_storage.py').read_text()
        self.assertNotIn("'-v', password", source)

    def test_the_disks_are_found_by_their_stable_names(self):
        with tempfile.TemporaryDirectory() as d:
            by_path, dev = Path(d) / 'by-path', Path(d) / 'dev'
            by_path.mkdir()
            dev.mkdir()
            for disk in ('sdc', 'sdd', 'sda'):
                (dev / disk).touch()
            (by_path / f'ip-{PORTAL}-iscsi-{IQN}-lun-1').symlink_to(dev / 'sdc')
            (by_path / f'ip-{PORTAL}-iscsi-{BACKUP}-lun-0').symlink_to(dev / 'sdd')
            (by_path / 'pci-0000:00:17.0-ata-1').symlink_to(dev / 'sda')
            found = nas.disks_of(IQN, by_path)
            self.assertEqual([(f['lun'], Path(f['device']).name) for f in found], [(1, 'sdc')])
            self.assertEqual(nas.where('sdd', by_path), {'portal': PORTAL, 'iqn': BACKUP, 'lun': 0})
            self.assertIsNone(nas.where('sda', by_path))
            self.assertEqual(
                nas.format_command(found[0], IQN),
                f'sudo mkfs.ext4 -L neko-media1 {by_path}/ip-{PORTAL}-iscsi-{IQN}-lun-1',
            )
            # Two targets' LUNs get labels of their own (both were "neko-nas-lun1").
            self.assertIn('-L neko-backup1 ', nas.format_command(found[0], BACKUP))
        self.assertEqual(nas.disks_of(IQN, '/nonexistent'), [])

    def test_a_lun_is_described_by_size_filesystem_and_identity(self):
        disk = {'lun': 1, 'path': 'x', 'device': '/dev/sdc'}
        row = {'name': 'sdc', 'size': '1.8T', 'wwn': '0x6001405abc', 'children': [{'fstype': 'ext4', 'uuid': 'u-1'}]}
        self.assertEqual(nas.described(disk, row), 'LUN 1: /dev/sdc, 1.8T, ext4 (UUID u-1); identity 0x6001405abc')
        self.assertIn('empty: no filesystem yet', nas.described(disk, {'name': 'sdc', 'size': '1.8T'}))

    def test_the_initiator_name_is_read_or_made_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'initiatorname.iscsi'
            path.write_text('## comment\nInitiatorName=iqn.2004-10.com.ubuntu:01:abcdef\n')
            self.assertEqual(nas.initiator_name(path), 'iqn.2004-10.com.ubuntu:01:abcdef')
            path.unlink()
            made = nas.initiator_name(path, run=lambda *a, **k: done(out='iqn.2005-03.org.open-iscsi:1234abcd\n'))
            self.assertEqual(made, 'iqn.2005-03.org.open-iscsi:1234abcd')
            self.assertIn('InitiatorName=' + made, path.read_text())

    def test_the_wait_for_the_network_follows_the_network_manager(self):
        enabled = {'systemd-networkd.service'}

        def run(argv, **kwargs):
            return done(0 if argv[-1] in enabled else 1)

        self.assertEqual(nas.wait_online_unit(run), 'systemd-networkd-wait-online.service')
        enabled = {'NetworkManager.service'}
        self.assertEqual(nas.wait_online_unit(run), 'NetworkManager-wait-online.service')
        enabled = set()
        self.assertIsNone(nas.wait_online_unit(run))

    def test_each_linux_names_the_tools_and_the_login_service(self):
        self.assertEqual(host.Debian.service('iscsi-login'), 'open-iscsi.service')
        for family in (host.Arch, host.Fedora, host.Rhel, host.Suse):
            self.assertEqual(family.service('iscsi-login'), 'iscsi.service', family)
            self.assertEqual(family.service('iscsid'), 'iscsid.service')
        fedora = host.Fedora(lambda *a, **k: None, {})
        self.assertEqual(fedora.names('open-iscsi'), ['iscsi-initiator-utils'])
        for family in (host.Arch, host.Debian, host.Suse):
            self.assertEqual(family(lambda *a, **k: None, {}).names('open-iscsi'), ['open-iscsi'], family)


class StepTests(unittest.TestCase):
    """The step as setup runs it, every command stood in for."""

    class Ctx:
        apply = True

        def __init__(self, config):
            self.config, self.lines, self.saved = config, [], 0

        def log(self, line):
            self.lines.append(line)

        def persist_config(self):
            self.saved += 1

    def step(self, config, answers=(), rows=None, chap=None):
        ctx = self.Ctx(config)
        typed = iter(answers)
        logged_in = []
        rows = rows or {'sdc': {'name': 'sdc', 'size': '1.8T', 'fstype': 'ext4', 'uuid': 'u-1', 'wwn': '0x1'}}
        disks = {
            IQN: [{'lun': 1, 'device': '/dev/sdc', 'path': f'/dev/disk/by-path/ip-{PORTAL}-iscsi-{IQN}-lun-1'}],
            BACKUP: [{'lun': 0, 'device': '/dev/sdd', 'path': f'/dev/disk/by-path/ip-{PORTAL}-iscsi-{BACKUP}-lun-0'}],
        }
        with (
            mock.patch.object(nas.os, 'geteuid', return_value=0),
            mock.patch('sys.stdin.isatty', return_value=True),
            mock.patch('builtins.input', side_effect=lambda prompt='': next(typed)),
            mock.patch('getpass.getpass', side_effect=lambda prompt='': chap or ''),
            mock.patch.object(nas, 'install_tools'),
            mock.patch.object(nas, 'initiator_name', return_value='iqn.2004-10.com.ubuntu:01:laptop'),
            mock.patch.object(nas, 'answers', return_value=True),
            mock.patch.object(nas, 'discovered', return_value=[(IQN, PORTAL), (BACKUP, PORTAL)]),
            mock.patch.object(nas, 'log_in', side_effect=lambda iqn, portal: logged_in.append(iqn)),
            mock.patch.object(nas, 'known_at'),
            mock.patch.object(nas, 'install_rule'),
            mock.patch.object(nas, 'set_chap') as chap_saved,
            mock.patch.object(nas, 'wait_for_disks', side_effect=lambda iqn: disks[iqn]),
            mock.patch.object(nas, 'lsblk_row', side_effect=lambda device: rows[Path(device).name]),
            mock.patch.object(nas, 'wait_online_unit', return_value='NetworkManager-wait-online.service'),
            mock.patch.object(nas, 'run_tool') as tool,
        ):
            result = nas.run(ctx)
        return ctx, result, logged_in, chap_saved, tool

    def test_switched_off_it_does_nothing(self):
        ctx = self.Ctx({})
        self.assertIsNone(nas.run(ctx))
        self.assertIn('not used', ctx.lines[0])

    def test_the_first_run_asks_the_address_the_targets_and_chap_then_logs_in(self):
        rows = {
            'sdc': {'name': 'sdc', 'size': '1.8T', 'fstype': 'ext4', 'uuid': 'u-1', 'wwn': '0x1'},
            'sdd': {'name': 'sdd', 'size': '931G', 'wwn': '0x2'},
        }
        config = {'network_storage': {'wanted': True}}
        ctx, result, logged_in, chap_saved, tool = self.step(
            config, ['192.168.1.20', '1,2', 'y', 'neko', 'n'], rows, chap='a-long-secret-1'
        )
        self.assertEqual(config['network_storage'], {'wanted': True, 'portal': PORTAL, 'targets': [IQN, BACKUP]})
        self.assertEqual(logged_in, [IQN, BACKUP])
        chap_saved.assert_called_once_with(IQN, PORTAL, 'neko', 'a-long-secret-1')
        self.assertGreaterEqual(ctx.saved, 1)
        said = '\n'.join(ctx.lines)
        self.assertIn('iqn.2004-10.com.ubuntu:01:laptop', said)  # to allow on the NAS
        self.assertIn('LUN 1: /dev/sdc, 1.8T, ext4 (UUID u-1)', said)
        self.assertIn('one box', said)
        self.assertNotIn('a-long-secret-1', said)
        tool.assert_any_call(['systemctl', 'enable', 'NetworkManager-wait-online.service'], 60)
        # The empty LUN: never formatted, its command given.
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn(
            f'sudo mkfs.ext4 -L neko-backup0 /dev/disk/by-path/ip-{PORTAL}-iscsi-{BACKUP}-lun-0', result['reason']
        )
        self.assertIn('never formats', result['reason'])

    def test_a_later_run_uses_what_was_saved_and_asks_nothing(self):
        config = {'network_storage': {'wanted': True, 'portal': PORTAL, 'targets': [IQN]}}
        ctx, result, logged_in, chap_saved, _ = self.step(config)
        self.assertIsNone(result)
        self.assertEqual(logged_in, [IQN])
        chap_saved.assert_not_called()

    def test_an_address_that_does_not_answer_stops_with_what_to_check(self):
        ctx = self.Ctx({'network_storage': {'wanted': True, 'portal': PORTAL}})
        with (
            mock.patch.object(nas.os, 'geteuid', return_value=0),
            mock.patch.object(nas, 'install_tools'),
            mock.patch.object(nas, 'initiator_name', return_value='iqn.2004-10.com.ubuntu:01:laptop'),
            mock.patch.object(nas, 'answers', return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Nothing answers on 192.168.1.20:3260: check that the NAS is on'):
                nas.run(ctx)


class DriveStepTests(unittest.TestCase):
    """The drive step with a disk on the NAS."""

    INVENTORY = {
        'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'tran': 'sata', 'children': [{'name': 'sda1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'os-1', 'mountpoints': ['/']}]},
            {'name': 'sdb', 'type': 'disk', 'tran': 'usb', 'size': '232.9G', 'children': [{'name': 'sdb1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'ssd-1', 'size': '232.9G'}]},
            {'name': 'sdc', 'type': 'disk', 'tran': 'iscsi', 'wwn': '0x6001405abc', 'size': '1.8T', 'fstype': 'ext4', 'uuid': 'nas-1'},
        ]
    }  # fmt: skip

    def test_a_nas_disk_is_known_as_one_with_its_identity(self):
        found = {v['uuid']: v for v in storage.discover_filesystems(self.INVENTORY, sub_uuid=lambda n: None)}
        self.assertTrue(found['nas-1']['network'])
        self.assertEqual(found['nas-1']['wwn'], '0x6001405abc')
        self.assertFalse(found['ssd-1']['network'])
        self.assertIn('on the network storage box (iSCSI, 0x6001405abc)', storage.describe(found['nas-1']))
        record = {}
        storage.new_drive(record, found['nas-1'])
        self.assertEqual(record['network'], {'transport': 'iscsi', 'wwn': '0x6001405abc'})
        self.assertEqual(
            storage.network_uuids({'storage': {'media_hdd': record, 'data_ssd': {'uuid': 'ssd-1'}}}), {'nas-1'}
        )

    def test_the_apps_ssd_is_never_a_nas_disk(self):
        nas_disk = {'name': 'sdc', 'network': True}
        with self.assertRaisesRegex(RuntimeError, 'SSD for the apps must be connected to this PC'):
            storage.local_ssd('data_ssd', nas_disk)
        self.assertIs(storage.local_ssd('media_hdd', nas_disk), nas_disk)

    def test_the_ssd_question_never_offers_a_nas_disk(self):
        # Found in the lab: the NAS disks were listed for the SSD, then refused once picked.
        items = storage.discover_filesystems(self.INVENTORY, sub_uuid=lambda n: None)
        asked = {}

        def ask(options, key, label, input_fn, out_fn):
            asked[key] = [v['uuid'] for v in options]
            return options[0]

        with mock.patch.object(storage, 'ask_drive', ask):
            storage.choose_new(
                {'data_mount': '/srv/x'}, self.INVENTORY, items, set(), 'data_ssd', 'the SSD', None, None
            )
            storage.choose_new(
                {'media_mount': '/mnt/m'}, self.INVENTORY, items, set(), 'media_hdd', 'the HDD', None, None
            )
            self.assertEqual(asked, {'data_ssd': ['ssd-1'], 'media_hdd': ['ssd-1', 'nas-1']})
            with self.assertRaisesRegex(RuntimeError, 'Only disks on the network storage box were found for the SSD'):
                storage.choose_new(
                    {'data_mount': '/srv/x'}, self.INVENTORY, items, {'ssd-1'}, 'data_ssd', 'the SSD', None, None
                )

    def test_a_saved_nas_disk_must_be_the_same_one(self):
        record = {'uuid': 'nas-1', 'network': {'transport': 'iscsi', 'wwn': '0xaaa'}}
        with self.assertRaisesRegex(RuntimeError, 'another NAS disk than before'):
            storage.same_nas_disk(record, {'network': True, 'wwn': '0xbbb'}, 'the media HDD')
        storage.same_nas_disk(record, {'network': True, 'wwn': '0xaaa'}, 'the media HDD')
        old = {'uuid': 'nas-1'}  # chosen before 0.46.0
        storage.same_nas_disk(old, {'network': True, 'wwn': '0xaaa'}, 'the media HDD')
        self.assertEqual(old['network'], {'transport': 'iscsi', 'wwn': '0xaaa'})
        storage.same_nas_disk(old, {'network': False}, 'the media HDD')
        self.assertNotIn('network', old)

    def test_a_nas_disk_is_mounted_once_the_network_and_the_login_are_up(self):
        with mock.patch.object(storage, 'login_unit', return_value='open-iscsi.service'):
            line = storage.fstab_line('nas-1', 'ext4', '/mnt/nekoserver-media', network=True)
            self.assertEqual(
                line,
                'UUID=nas-1 /mnt/nekoserver-media ext4 defaults,nofail,x-systemd.device-timeout=90s,_netdev,'
                'x-systemd.requires=open-iscsi.service,errors=remount-ro 0 2',
            )
            # Btrfs and XFS have no errors= option: the rest is the same.
            self.assertNotIn('errors=', storage.fstab_line('nas-1', 'xfs', '/mnt/m', network=True))
            self.assertTrue(storage.own_line(line.split()))
            local = storage.fstab_line('nas-1', 'ext4', '/mnt/nekoserver-media', network=False)
            self.assertNotIn('_netdev', local)
            # A drive that moved to the NAS: NekoServer's own line is brought up to date.
            self.assertEqual(storage.existing_line([local], 'nas-1', '/mnt/nekoserver-media', line), 0)
            with tempfile.TemporaryDirectory() as d:
                fstab = Path(d) / 'fstab'
                fstab.write_text(local + '\n')
                spec = [('media_hdd', 'nas-1', 'ext4', '/mnt/nekoserver-media', None)]
                self.assertTrue(storage.install_fstab(spec, fstab, network={'nas-1'}))
                self.assertEqual(fstab.read_text(), line + '\n')
                self.assertFalse(storage.install_fstab(spec, fstab, network={'nas-1'}))
        # Someone else's line for the drive still stops setup.
        theirs = 'UUID=nas-1 /mnt/nekoserver-media ext4 defaults,_netdev 0 0'
        with self.assertRaisesRegex(RuntimeError, 'which NekoServer did not write'):
            storage.existing_line([theirs], 'nas-1', '/mnt/nekoserver-media', local)

    def test_a_drive_is_found_on_the_nas_from_its_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'by-uuid').mkdir()
            block = root / 'class'
            (block / 'sdc' / 'sdc1').mkdir(parents=True)
            (block / 'sdc' / 'sdc1' / 'partition').write_text('1\n')
            links = root / 'links'
            links.mkdir()
            os.symlink(block / 'sdc' / 'sdc1', links / 'sdc1')
            (root / 'by-uuid' / 'u-1').symlink_to(root / 'dev-sdc1')
            (root / 'dev-sdc1').touch()
            with mock.patch.object(storage, 'BY_UUID', str(root / 'by-uuid') + '/'):
                # The name the UUID link leads to, found under /sys/class/block.
                os.symlink(block / 'sdc' / 'sdc1', block / 'dev-sdc1')
                self.assertEqual(storage.disk_of('u-1', block), 'sdc')
                self.assertIsNone(storage.disk_of('missing', block))


class SetupWindowTests(unittest.TestCase):
    def test_the_first_screen_offers_the_nas_switch_off_by_default(self):
        import explanations
        import wizard

        extra = next(e for e in explanations.EXTRAS if e['id'] == 'network_storage')
        self.assertFalse(explanations.extra_state({}, extra))
        self.assertIn('open-iscsi', extra['installs'])
        self.assertIn('never formats', extra['explanation'])
        self.assertIn('network_storage', wizard.STEPS)
        self.assertIn('network_storage', storage.DEPENDENCIES)  # before the drive step
        config = json.loads((ROOT / 'config.json').read_text())
        self.assertIs(config['network_storage']['wanted'], False)


if __name__ == '__main__':
    unittest.main()


class ReadOnlyCheckTests(unittest.TestCase):
    """The recovery menu's "Check a drive that turned read-only", every command stood in."""

    def setUp(self):
        import recovery_wizard

        self.wizard = recovery_wizard
        self.calls, self.said = [], []
        self.config = {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/mnt/nekoserver-media',
            'storage': {'data_ssd': {'uuid': 'ssd-0001'}, 'media_hdd': {'uuid': 'nas-0001', 'fstype': 'ext4'}},
        }
        self.writable = {'/srv/nekoserver': True, '/mnt/nekoserver-media': False}
        self.codes = {}

    def run_(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'systemctl' and argv[1] == 'start' and argv[2].endswith('.mount'):
            self.writable['/mnt/nekoserver-media'] = True  # mounted again, repaired
        out = 'mnt-nekoserver\\x2dmedia.mount\n' if argv[0] == 'systemd-escape' else 'clean\n'
        return mock.Mock(returncode=self.codes.get(argv[0], 0), stdout=out, stderr='')

    def check(self, answers):
        typed = iter(answers)
        with (
            mock.patch.object(self.wizard.os.path, 'ismount', return_value=True),
            mock.patch.object(self.wizard, 'start_apps', side_effect=lambda run, out: self.calls.append(['apps'])),
        ):
            self.wizard.check_read_only(
                self.config, lambda q='': next(typed), self.said.append, self.run_, lambda f: self.writable[str(f)]
            )

    def test_the_drive_is_unmounted_checked_mounted_and_the_apps_started_in_that_order(self):
        self.check(['y', 'y'])
        order = [c[0] if c[0] != 'systemctl' else c[0] + ' ' + c[1] for c in self.calls]
        self.assertEqual(order, ['systemctl stop', 'umount', 'e2fsck', 'systemd-escape', 'systemctl start', 'apps'])
        self.assertEqual(self.calls[1], ['umount', '/mnt/nekoserver-media'])  # never -f or -l
        self.assertEqual(self.calls[2], ['e2fsck', '-f', '-p', '/dev/disk/by-uuid/nas-0001'])
        self.assertIn('takes writes', self.said[-1])

    def test_nothing_is_done_without_a_yes_or_when_every_drive_writes(self):
        self.check(['n'])
        self.assertEqual(self.calls, [])
        self.writable['/mnt/nekoserver-media'] = True
        self.check([])
        self.assertEqual(self.calls, [])
        self.assertIn('nothing to check', self.said[-1])

    def test_a_drive_in_use_is_never_forced_and_bad_damage_is_left_to_you(self):
        self.codes['umount'] = 32
        with self.assertRaisesRegex(RuntimeError, '(?s)could not be unmounted.*apps stay stopped'):
            self.check(['y'])
        self.assertNotIn('e2fsck', [c[0] for c in self.calls])
        self.codes = {'e2fsck': 4}
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'Run  sudo e2fsck -f /dev/disk/by-uuid/nas-0001  yourself'):
            self.check(['y', 'y'])
        self.assertNotIn('apps', [c[0] for c in self.calls])
