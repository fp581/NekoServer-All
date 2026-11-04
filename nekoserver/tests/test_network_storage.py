"""Drives on a network storage box (a NAS) over iSCSI: the connection step
(modules/network_storage.py) and what the drive step does with such a disk."""

import contextlib
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


# A target's saved record as `iscsiadm -m node -T … -p …` shows it, with setup's settings.
RECORD = 'node.name = x\n' + ''.join(f'{k} = {v}\n' for k, v in nas.KEPT.items())


def node_shown(argv):
    """True for the command that shows a target's saved record."""
    return argv[:3] == ['iscsiadm', '-m', 'node'] and '-o' not in argv and '--login' not in argv


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
            return done(nas.SESSION_EXISTS if '--login' in argv else 0, RECORD if node_shown(argv) else '')

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
        refused = lambda argv, **k: done(  # noqa: E731
            24 if '--login' in argv else 0,
            RECORD if node_shown(argv) else '',
            err='iscsiadm: Login failed: authorization failure',
        )
        with self.assertRaisesRegex(RuntimeError, 'allowed on that target .*CHAP'), mock.patch.object(nas, 'patient'):
            nas.log_in(IQN, PORTAL, refused)
        # As iscsiadm really says it (the lab, a wrong CHAP password): exit 24, a vague last line.
        vague = lambda argv, **k: done(  # noqa: E731
            24 if '--login' in argv else 0,
            RECORD if node_shown(argv) else 'Logging in to [iface: default, target: x]',
            err='iscsiadm: Could not log into all portals',
        )
        with self.assertRaisesRegex(RuntimeError, 'NAS refused the login'), mock.patch.object(nas, 'patient'):
            nas.log_in(IQN, PORTAL, vague)

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

    def test_discovery_only_lists_and_never_rewrites_saved_records(self):
        """The 0.46.0 review, H05: open-iscsi's plain discovery rewrote each target's saved
        record from the defaults, taking its CHAP login away at a repeat setup."""
        calls = []
        nas.discovered(PORTAL, run=lambda argv, **k: calls.append(argv) or done(out=f'{PORTAL},1 {IQN}\n'))
        self.assertEqual(calls[0][-2:], ['-o', 'nonpersistent'])

    def test_a_lost_chap_login_is_asked_again_or_stops_setup(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / IQN / '192.168.1.20,3260,1'
            folder.mkdir(parents=True)
            (folder / 'default').write_text('node.session.auth.authmethod = None\n')
            self.assertFalse(nas.chap_kept(IQN, PORTAL, (Path(d),)))
            nas.set_chap(IQN, PORTAL, 'neko', 'a-long-secret-1', roots=(Path(d),))
            self.assertTrue(nas.chap_kept(IQN, PORTAL, (Path(d),)))
        ctx = StepTests.Ctx({'network_storage': {'wanted': True, 'portal': PORTAL, 'targets': [IQN], 'chap': [IQN]}})
        with (
            mock.patch.object(nas, 'answers', return_value=True),
            mock.patch.object(nas, 'discovered', return_value=[(IQN, PORTAL)]),
            mock.patch.object(nas, 'install_rule'),
            mock.patch.object(nas, 'known_at'),
            mock.patch.object(nas, 'chap_kept', return_value=False),
            mock.patch.object(nas, 'log_in') as logged_in,
        ):
            with self.assertRaisesRegex(RuntimeError, 'CHAP login of .* is no longer in its iSCSI settings'):
                nas.connected(ctx, ctx.config['network_storage'], False)
            logged_in.assert_not_called()

    def test_a_target_the_nas_no_longer_offers_is_never_forgotten(self):
        """H02: with one of two saved targets missing, setup saved the shorter list."""
        with self.assertRaisesRegex(RuntimeError, f'does not offer {BACKUP} now.*Nothing was changed'):
            nas.chosen_targets([(IQN, PORTAL)], [IQN, BACKUP], None, print)
        self.assertEqual(
            nas.chosen_targets([(IQN, PORTAL), (BACKUP, PORTAL)], [IQN, BACKUP], None, print), [IQN, BACKUP]
        )

    def test_a_setting_that_was_not_saved_stops_the_login(self):
        """H01: the updates' results were not looked at, and a login that worked hid it."""
        failing = lambda argv, **k: done(13 if '-o' in argv else 0, err='iscsiadm: could not write record')  # noqa: E731
        with self.assertRaisesRegex(RuntimeError, 'could not be saved'):
            nas.log_in(IQN, PORTAL, failing)
        stale = RECORD.replace('= 600', '= 120')
        quiet = lambda argv, **k: done(0, stale if node_shown(argv) else '')  # noqa: E731
        with self.assertRaisesRegex(RuntimeError, 'do not say what setup wrote .*replacement_timeout'):
            nas.log_in(IQN, PORTAL, quiet)

    def test_a_network_wait_that_could_not_be_switched_on_stops_setup(self):
        """H03: a failed enable was reported as the wait being in place."""
        with (
            mock.patch.object(nas, 'wait_online_unit', return_value='systemd-networkd-wait-online.service'),
            mock.patch.object(nas, 'run_tool', side_effect=lambda argv, *a: done(1 if argv[1] == 'enable' else 0)),
        ):
            with self.assertRaisesRegex(RuntimeError, 'could not be switched on'):
                nas.wait_for_network(print)
        said = []
        with (
            mock.patch.object(nas, 'wait_online_unit', return_value='systemd-networkd-wait-online.service'),
            mock.patch.object(nas, 'run_tool', return_value=done()),
        ):
            nas.wait_for_network(said.append)
        self.assertIn('once the network is up', said[0])

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
            # A record made by hand (`-o new`) has no third part in its name (seen in the lab).
            (folder / 'default').unlink()
            folder.rmdir()
            plain = Path(d) / IQN / '192.168.1.20,3260'
            plain.mkdir()
            (plain / 'default').write_text('node.name = x\n')
            self.assertEqual(nas.node_record(IQN, PORTAL, (Path(d),)), plain / 'default')
            self.assertIsNone(nas.node_record(IQN, '192.168.1.2:3260', (Path(d),)))
            (plain / 'default').unlink()
            plain.rmdir()
            folder.mkdir()
            (folder / 'default').write_text('node.name = x\n')
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

    def test_the_name_the_nas_allows_is_kept_after_linux_is_installed_again(self):
        """The 0.46.0 review: a new Linux makes a new iSCSI name, which the NAS refuses; the
        host backups didn't keep the old one either."""
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            return done()

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'initiatorname.iscsi'
            path.write_text('InitiatorName=iqn.2004-10.com.ubuntu:01:new\n')
            options, said = {}, []
            self.assertEqual(nas.kept_initiator(options, said.append, path, run), 'iqn.2004-10.com.ubuntu:01:new')
            self.assertEqual(options['initiator'], 'iqn.2004-10.com.ubuntu:01:new')
            self.assertEqual(ran, [])
            options = {'initiator': 'iqn.2004-10.com.ubuntu:01:laptop'}
            with mock.patch('host.current', return_value=mock.Mock(service=lambda name: name)):
                self.assertEqual(
                    nas.kept_initiator(options, said.append, path, run), 'iqn.2004-10.com.ubuntu:01:laptop'
                )
            self.assertEqual(path.read_text(), 'InitiatorName=iqn.2004-10.com.ubuntu:01:laptop\n')
            self.assertIn(['systemctl', 'restart', 'iscsid'], ran)
            self.assertIn('replaced by the saved one the NAS allows', said[-1])
        from modules import host_backup

        self.assertIn('/etc/iscsi/initiatorname.iscsi', host_backup.HOST_FILES)

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

    def step(self, config, answers=(), rows=None, chap=None, log_in=None):
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
            mock.patch.object(nas, 'log_in', side_effect=log_in or (lambda iqn, portal: logged_in.append(iqn))),
            mock.patch.object(nas, 'known_at'),
            mock.patch.object(nas, 'install_rule'),
            mock.patch.object(nas, 'set_chap') as chap_saved,
            mock.patch.object(nas, 'wait_for_disks', side_effect=lambda iqn: disks[iqn]),
            mock.patch.object(nas, 'lsblk_row', side_effect=lambda device: rows[Path(device).name]),
            mock.patch.object(nas, 'wait_online_unit', return_value='NetworkManager-wait-online.service'),
            mock.patch.object(nas, 'run_tool', return_value=done()) as tool,
            mock.patch.object(nas, 'chap_kept', return_value=True),
        ):
            result = nas.run(ctx)
        return ctx, result, logged_in, chap_saved, tool

    def test_a_refused_login_asks_for_the_chap_login(self):
        """The 0.46.0 review: after Linux was installed again the saved targets skipped the
        CHAP question, and the NAS refused the login with no way to give it."""
        tries = []

        def log_in(iqn, portal):
            tries.append(iqn)
            if tries.count(iqn) == 1:
                raise nas.LoginRefused('Could not log in')

        config = {'network_storage': {'wanted': True, 'portal': PORTAL, 'targets': [IQN]}}
        ctx, _, _, chap_saved, _ = self.step(config, ['y', 'neko'], chap='a-long-password', log_in=log_in)
        self.assertEqual(tries, [IQN, IQN])
        self.assertEqual(config['network_storage']['chap'], [IQN])
        self.assertIn(f'The NAS refused the login to {IQN}.', ctx.lines)

    def test_without_a_network_manager_to_wait_for_it_is_refused_before_connecting(self):
        """0.46.1: it was only a warning, and the drives went missing after each restart."""
        ctx = self.Ctx({'network_storage': {'wanted': True, 'portal': PORTAL}})
        with (
            mock.patch.object(nas.os, 'geteuid', return_value=0),
            mock.patch('sys.stdin.isatty', return_value=False),
            mock.patch.object(nas, 'install_tools'),
            mock.patch.object(nas, 'wait_online_unit', return_value=None),
            mock.patch.object(nas, 'connected') as connected,
        ):
            with self.assertRaisesRegex(RuntimeError, 'cannot wait for the network.*Nothing was mounted'):
                nas.run(ctx)
        connected.assert_not_called()

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
        self.assertEqual(
            config['network_storage'],
            {
                'wanted': True,
                'portal': PORTAL,
                'targets': [IQN, BACKUP],
                'chap': [IQN],
                'initiator': 'iqn.2004-10.com.ubuntu:01:laptop',
            },
        )
        self.assertEqual(logged_in, [IQN, BACKUP])
        chap_saved.assert_called_once_with(IQN, PORTAL, 'neko', 'a-long-secret-1', run=mock.ANY)
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
            mock.patch.object(nas, 'wait_for_network'),
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
            'storage': {
                'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'nas-0001', 'fstype': 'ext4'},
            },
        }
        self.writable = {'/srv/nekoserver': True, '/mnt/nekoserver-media': False}
        self.errors = {}
        self.codes = {}
        self.busy = None
        self.lock = contextlib.nullcontext()

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
            mock.patch.object(self.wizard, 'repair_blocked', side_effect=lambda c, r: self.busy),
        ):
            self.wizard.check_read_only(
                self.config,
                lambda q='': next(typed),
                self.said.append,
                self.run_,
                lambda f: self.writable[str(f)],
                lambda f: self.errors.get(str(f), 0),
                hold=lambda lock: self.lock,
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

    def test_nothing_is_touched_while_another_operation_is_at_work(self):
        """The 0.46.0 review, H07: the repair stopped the apps and unmounted a drive with no
        look at a move, an upgrade, an update or a backup at work."""
        self.lock = None  # a drive move or an upgrade holds the operation lock
        self.check([])
        self.assertEqual(self.calls, [])
        self.assertIn('drive move or a NekoServer upgrade is at work', self.said[-1])
        self.lock = contextlib.nullcontext()
        self.busy = 'nekoserver-photo-backup.service is at work'
        self.check(['y'])
        self.assertEqual(self.calls, [])
        self.assertIn('photo-backup.service is at work: check the drive once it has finished', self.said[-1])

    def test_a_job_at_work_or_an_unfinished_update_blocks_the_repair(self):
        listed = 'nekoserver-photo-backup.service loaded activating start\nnekoserver-nas-watch.service loaded activating x\n'
        run = lambda argv, **k: mock.Mock(returncode=0, stdout=listed)  # noqa: E731
        with mock.patch('upgrade.nothing_at_work', return_value=None):
            self.assertEqual(self.wizard.repair_blocked(self.config, run), 'nekoserver-photo-backup.service is at work')
            quiet = lambda argv, **k: mock.Mock(
                returncode=0, stdout='nekoserver-nas-watch.service loaded activating x\n'
            )  # noqa: E731
            self.assertIsNone(self.wizard.repair_blocked(self.config, quiet))
        with mock.patch('upgrade.nothing_at_work', return_value='An app update is not finished yet'):
            self.assertEqual(self.wizard.repair_blocked(self.config, run), 'An app update is not finished yet')

    def test_a_drive_that_still_writes_but_had_errors_is_checked_too(self):
        """0.46.1: a local drive with filesystem errors (a loose cable) may still take
        writes; Linux's error count says it needs a check."""
        self.writable['/mnt/nekoserver-media'] = True
        self.errors['/srv/nekoserver'] = 3
        self.check(['y', 'y'])
        self.assertEqual(self.calls[1], ['umount', '/srv/nekoserver'])
        self.assertEqual(self.calls[2], ['e2fsck', '-f', '-p', '/dev/disk/by-uuid/ssd-0001'])

    def test_the_error_count_is_read_from_the_mounted_filesystem(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'sdb1').mkdir()
            (Path(d) / 'sdb1' / 'errors_count').write_text('2\n')
            with (
                mock.patch.object(self.wizard, 'findmnt', return_value='/dev/sdb1'),
                mock.patch.object(self.wizard.os.path, 'realpath', return_value='/dev/sdb1'),
            ):
                self.assertEqual(self.wizard.ext4_errors('/srv/x', Path(d)), 2)
            with mock.patch.object(self.wizard, 'findmnt', return_value=None):
                self.assertEqual(self.wizard.ext4_errors('/srv/x', Path(d)), 0)

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


class LocationTests(unittest.TestCase):
    """0.46.0: setup asks where each app keeps your files; everything follows the choice."""

    class Ctx:
        def __init__(self, config):
            self.config, self.lines, self.saved = config, [], 0

        def log(self, line):
            self.lines.append(line)

        def persist_config(self):
            self.saved += 1

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = {'data_mount': str(self.root / 'srv'), 'media_mount': str(self.root / 'mnt')}
        for drive in ('srv', 'mnt'):
            (self.root / drive).mkdir()

    def ask(self, answers, ssd_gb=230, interactive=True):
        ctx = self.Ctx(self.config)
        typed = iter(answers)
        with mock.patch.object(storage, 'free_gb', lambda f: ssd_gb if f.endswith('srv') else 1800):
            storage.choose_locations(ctx, lambda prompt='': next(typed), interactive)
        return ctx

    def test_the_better_drive_is_offered_first_and_enter_takes_it(self):
        ctx = self.ask(['', '2', ''])
        # A 250 GB SSD: photos and music on the media drive; music moved to the SSD by hand.
        self.assertEqual(self.config['locations'], {'photos': 'media', 'music': 'ssd', 'cloud': 'media'})
        said = '\n'.join(ctx.lines)
        self.assertIn('the media drive: 1800 GB free, the most room  (recommended)', said)
        self.assertIn('The backups follow wherever you choose', said)
        self.assertEqual(ctx.saved, 1)
        # A big SSD: photos and music there.
        self.config.pop('locations')
        self.ask([], ssd_gb=900, interactive=False)
        self.assertEqual(self.config['locations'], {'photos': 'ssd', 'music': 'ssd', 'cloud': 'media'})

    def test_files_already_on_a_drive_stay_there_and_a_wrong_setting_stops_setup(self):
        library = self.root / 'srv/photos/immich-library'
        library.mkdir(parents=True)
        (library / 'upload').mkdir()
        self.ask(['', ''])
        self.assertEqual(self.config['locations']['photos'], 'ssd')  # the layout before 0.46.0
        self.config['locations']['photos'] = 'media'
        with self.assertRaisesRegex(RuntimeError, 'would start with an empty folder'):
            self.ask([])

    def test_files_on_both_drives_are_never_guessed(self):
        """The 0.46.0 review: with photos on both drives the SSD won without a word."""
        for drive in ('srv', 'mnt'):
            (self.root / drive / 'photos/immich-library/upload').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'files on both the SSD and the media drive'):
            self.ask(['', ''])
        self.assertNotIn('photos', self.config.get('locations', {}))
        # Once the settings say which one, it is used, and the other is named as left alone.
        self.config['locations'] = {'photos': 'media'}
        ctx = self.ask(['', ''])
        self.assertEqual(self.config['locations']['photos'], 'media')
        self.assertIn('The SSD holds another folder of them too', '\n'.join(ctx.lines))

    def test_every_path_follows_the_choice(self):
        import neko_common

        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        self.assertEqual(str(neko_common.photo_library(config)), '/srv/nekoserver/photos/immich-library')
        self.assertEqual(str(neko_common.cloud_files(config)), '/mnt/nekoserver-media/cloud/files')
        config['locations'] = {'photos': 'media', 'music': 'media', 'cloud': 'ssd'}
        self.assertEqual(str(neko_common.photo_library(config)), '/mnt/nekoserver-media/photos/immich-library')
        self.assertEqual(str(neko_common.music_library(config)), '/mnt/nekoserver-media/library/music')
        self.assertEqual(str(neko_common.music_downloads(config)), '/mnt/nekoserver-media/library/downloads/music')
        self.assertEqual(str(neko_common.cloud_files(config)), '/srv/nekoserver/cloud/files')
        # The runtime's jobs get the choice with the drives.
        import native_runtime
        from modules import photo_backup

        config['storage'] = {}
        self.assertEqual(native_runtime.storage_config(config)['locations'], config['locations'])
        # The folders setup decided are written down for the runtime (common::saved_path), so
        # the USB copy takes Nextcloud's files from where setup put them (the review, H04).
        self.assertEqual(
            native_runtime.storage_config(config)['paths'],
            {
                'photos': '/mnt/nekoserver-media/photos/immich-library',
                'music': '/mnt/nekoserver-media/library/music',
                'downloads': '/mnt/nekoserver-media/library/downloads/music',
                'cloud': '/srv/nekoserver/cloud/files',
            },
        )
        # The NAS check gets what it needs to log in again, never a password.
        config['network_storage'] = {
            'wanted': True,
            'portal': '192.168.1.20',
            'targets': ['iqn.x:a'],
            'chap': ['iqn.x:a'],
        }
        self.assertEqual(
            native_runtime.storage_config(config)['network_storage'],
            {'wanted': True, 'portal': '192.168.1.20', 'targets': ['iqn.x:a']},
        )
        config['network_storage']['wanted'] = False
        self.assertNotIn('network_storage', native_runtime.storage_config(config))
        cmd = photo_backup.command_line(
            {**config, 'nextcloud': {'enabled': True}}, 'bin', 'guard', 'u', 'ext4', None, '/mnt/usb'
        )
        self.assertIn('--cloud /srv/nekoserver/cloud/files', cmd)
        import config_checks

        with self.assertRaises(RuntimeError):
            config_checks.validate({'locations': {'photos': 'usb'}}, [])
        with self.assertRaises(RuntimeError):
            config_checks.validate({'locations': {'films': 'ssd'}}, [])

    def test_no_data_path_is_written_out_by_hand_any_more(self):
        """Every step, backup, restore and check reads the paths from the resolvers."""
        import re

        allowed = {'neko_common.py', 'test_', 'common.rs'}
        for path in [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py'), *ROOT.glob('runtime/src/*.rs')]:
            if any(a in path.name for a in allowed):
                continue
            text = path.read_text()
            if path.suffix == '.rs':
                text = text.split('#[cfg(test)]')[0]
            for pattern in (r"data_mount'\]\) / 'photos/immich-library", r"media_mount'\]\) / 'cloud/files",
                            r'data\.join\("photos/immich-library"\)', r'data\.join\("library/music"\)',
                            r'media\.join\("cloud/files"\)', r"data / 'library/downloads/music'"):  # fmt: skip
                self.assertIsNone(re.search(pattern, text), f'{path.name}: {pattern}')
