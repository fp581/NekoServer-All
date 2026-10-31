"""LAN folder sharing (Samba) and the LAN HTTPS gateway share one home-network choice,
so a fresh setup with automatic LAN settings finishes Samba on the first pass."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import network_access  # noqa: E402
from modules import samba  # noqa: E402

ROWS = [
    {'interface': 'enp3s0', 'ip': '192.168.1.20', 'cidr': '192.168.1.0/24'},
    {'interface': 'wlan0', 'ip': '10.0.0.5', 'cidr': '10.0.0.0/24'},
]


class HomeBridgeTests(unittest.TestCase):
    """A bridge of your own carrying the home network (br0, bridge0) is offered like a
    network card; Docker's, libvirt's and the tunnels never are (startup.rs agrees)."""

    def test_a_home_bridge_is_a_candidate_and_dockers_bridges_are_not(self):
        import json

        rows = [
            {
                'ifname': name,
                'addr_info': [{'family': 'inet', 'scope': 'global', 'local': '192.168.1.20', 'prefixlen': 24}],
            }
            for name in (
                'enp2s0',
                'br0',
                'bridge0',
                'br-lan',
                'docker0',
                'br-0123456789ab',
                'virbr0',
                'veth1',
                'tailscale0',
            )
        ]
        with mock.patch.object(network_access, 'command', return_value=json.dumps(rows)):
            found = [row['interface'] for row in network_access.candidates()]
        self.assertEqual(found, ['enp2s0', 'br0', 'bridge0', 'br-lan'])


class LanChoiceTests(unittest.TestCase):
    def ctx(self, **gateway):
        saved = []
        ctx = SimpleNamespace(config={'gateway': dict(gateway)}, log=print, apply=True)
        ctx.persist_config = lambda: saved.append(dict(ctx.config['gateway']))
        return ctx, saved

    def test_the_choice_is_asked_once_and_saved(self):
        ctx, saved = self.ctx(lan_ip='auto', lan_cidr='auto')
        with mock.patch.object(network_access, 'candidates', return_value=ROWS):
            self.assertTrue(network_access.choose_lan(ctx, output=lambda *_: None, ask=lambda _: '1', interactive=True))
            self.assertEqual(ctx.config['gateway'], {'lan_ip': '192.168.1.20', 'lan_cidr': '192.168.1.0/24'})
            self.assertEqual(saved, [ctx.config['gateway']])
            # The next step (the gateway after Samba, say) uses the answer without asking.
            never = mock.Mock(side_effect=AssertionError('asked twice'))
            self.assertTrue(network_access.choose_lan(ctx, output=lambda *_: None, ask=never, interactive=True))

    def test_an_address_the_router_changed_is_asked_again(self):
        ctx, _ = self.ctx(lan_ip='192.168.1.77', lan_cidr='192.168.1.0/24')
        shown = []
        with mock.patch.object(network_access, 'candidates', return_value=ROWS):
            self.assertTrue(network_access.choose_lan(ctx, output=shown.append, ask=lambda _: '1', interactive=True))
        self.assertEqual(ctx.config['gateway']['lan_ip'], '192.168.1.20')
        self.assertIn('no longer 192.168.1.77', ' '.join(shown))
        # Nobody to choose: a clear message instead of "not on an active physical LAN interface".
        ctx, _ = self.ctx(lan_ip='192.168.1.77', lan_cidr='192.168.1.0/24')
        with (
            mock.patch.object(network_access, 'candidates', return_value=ROWS),
            self.assertRaisesRegex(RuntimeError, 'router gave it another one.*in a terminal'),
        ):
            network_access.choose_lan(ctx, output=lambda *_: None, interactive=False)

    def test_without_anyone_to_choose_nothing_is_guessed(self):
        ctx, saved = self.ctx(lan_ip='auto', lan_cidr='auto')
        self.assertFalse(network_access.choose_lan(ctx, output=lambda *_: None, interactive=False))
        self.assertEqual((ctx.config['gateway']['lan_ip'], saved), ('auto', []))
        # A wrong number is asked again (not a failed step); three wrong ones, or
        # Ctrl+D, leave the choice for next time.
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        answers = iter(['7', 'x', '1'])
        with mock.patch.object(network_access, 'candidates', return_value=ROWS), mock.patch('builtins.print'):
            self.assertTrue(
                network_access.choose_lan(ctx, output=lambda *_: None, ask=lambda _: next(answers), interactive=True)
            )
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        with mock.patch.object(network_access, 'candidates', return_value=ROWS), mock.patch('builtins.print'):
            self.assertFalse(
                network_access.choose_lan(ctx, output=lambda *_: None, ask=lambda _: '7', interactive=True)
            )
        self.assertEqual(ctx.config['gateway']['lan_ip'], 'auto')

    def test_samba_chooses_the_lan_itself_instead_of_waiting_for_the_gateway(self):
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        ctx.config.update(networking={'share_mode': 'lan', 'shares': {'Media': '.'}}, media_mount='/nonexistent')
        asked = []

        def choose(context):
            asked.append(context)
            context.config['gateway'].update(lan_ip='192.168.1.20', lan_cidr='192.168.1.0/24')
            return True

        with (
            mock.patch.object(samba.os, 'geteuid', return_value=0),
            mock.patch.object(samba.host, 'current'),
            mock.patch.object(samba, 'previous_account', return_value=None),
            mock.patch.object(samba.network_access, 'choose_lan', side_effect=choose),
            mock.patch.object(samba.network_access, 'verify_lan') as verified,
            mock.patch.dict(samba.os.environ, {'SUDO_USER': 'root'}),
            self.assertRaisesRegex(RuntimeError, 'real desktop account'),
        ):
            samba.run(ctx)  # gets past the LAN step, to the next check
        self.assertEqual(asked, [ctx])
        verified.assert_called_once_with(
            {
                'gateway': {
                    'share_mode': 'lan',
                    'shares': {'Media': '.'},
                    'lan_ip': '192.168.1.20',
                    'lan_cidr': '192.168.1.0/24',
                }
            }
        )
        # With nobody to choose, Samba says how to finish instead of pointing at the gateway.
        ctx, _ = self.ctx(lan_ip='auto', lan_cidr='auto')
        ctx.config.update(networking={'share_mode': 'lan', 'shares': {'Media': '.'}})
        with (
            mock.patch.object(samba.os, 'geteuid', return_value=0),
            mock.patch.object(samba.host, 'current'),
            mock.patch.object(samba.network_access, 'choose_lan', return_value=False),
        ):
            result = samba.run(ctx)
        self.assertEqual(result['status'], 'needs_setup')
        self.assertNotIn('gateway_lan', result['reason'])


class SambaAccountRecordTests(unittest.TestCase):
    """Setup writes down the Samba accounts it creates, so the uninstaller removes only those."""

    def test_a_created_account_is_recorded_once(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d) / 'nekoserver-accounts'
            self.assertEqual(samba.created_accounts(record), [])
            samba.remember_account('neko', record)
            samba.remember_account('neko', record)
            samba.remember_account('dana', record)
            self.assertEqual(samba.created_accounts(record), ['neko', 'dana'])
            self.assertTrue(record.read_text().startswith('# Managed by NekoServer'))
            self.assertEqual(record.stat().st_mode & 0o777, 0o600)


class SambaWaitsForItsFoldersTests(unittest.TestCase):
    """The library and photo archive are made by the media and photos steps: until those
    have run (Docker failed, say), the shares wait instead of failing. A share folder you
    named yourself must exist."""

    def run_with(self, shares, made=()):
        with tempfile.TemporaryDirectory() as media:
            for rel in made:
                (Path(media) / rel).mkdir(parents=True)
            ctx = SimpleNamespace(
                config={
                    'media_mount': media,
                    'gateway': {'lan_ip': '192.168.1.20', 'lan_cidr': '192.168.1.0/24'},
                    'networking': {'share_mode': 'lan', 'shares': shares},
                    'photos': {'deploy_immich': True},
                },
                log=print,
                apply=True,
            )
            with (
                mock.patch.object(samba.os, 'geteuid', return_value=0),
                mock.patch.object(samba.host, 'current'),
                mock.patch.object(samba.network_access, 'choose_lan', return_value=True),
                mock.patch.object(samba.network_access, 'verify_lan'),
                mock.patch.object(samba, 'samba_account', return_value='neko'),
                mock.patch.object(samba.pwd, 'getpwnam'),
                mock.patch.object(samba, 'findmnt', return_value=media),
                mock.patch.object(samba.shutil, 'which', return_value=None),
            ):
                return samba.run(ctx)

    def test_default_shares_wait_for_the_steps_that_make_them(self):
        result = self.run_with({'Media': 'library', 'Photos': 'photos/archive'})
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('made by the media apps step', result['reason'])
        self.assertIn('made by the photos (Immich) step', result['reason'])

    def test_a_folder_you_named_must_exist(self):
        with self.assertRaisesRegex(RuntimeError, 'Share directory missing: .*Scans'):
            self.run_with({'Media': 'library', 'Scans': 'scans'}, made=['library'])


class SambaCommandMessageTests(unittest.TestCase):
    """A failed systemctl call shows its reason, not its "Created symlink" notice."""

    def test_the_reason_is_shown_without_the_link_notice(self):
        stderr = (
            "Created symlink '/etc/systemd/system/multi-user.target.wants/smb.service' → '/usr/lib/systemd/system/smb.service'.\n"
            "Job for smb.service failed because the control process exited with error code.\n"
        )
        with mock.patch.object(samba.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr=stderr, stdout='')):
            with self.assertRaises(RuntimeError) as caught:
                samba.call(['systemctl', 'enable', '--now', 'smb.service'])
        self.assertIn('systemctl enable --now failed: Job for smb.service failed', str(caught.exception))
        self.assertNotIn('Created symlink', str(caught.exception))


class SambaUnusedProgramsTests(unittest.TestCase):
    """winbind (and nmbd) are switched off on a server already set up: the settings backup
    refuses to run beside them, and a standalone Samba never uses them."""

    def fake(self, states):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            if argv[1] == 'show':
                return mock.Mock(stdout=states.get(argv[2], 'LoadState=not-found\n'), returncode=0)
            return mock.Mock(stdout='', stderr='', returncode=0)

        return run, calls

    def test_running_winbind_is_switched_off_and_absent_units_are_left(self):
        run, calls = self.fake(
            {
                'winbind.service': 'LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n',
                'nmbd.service': 'LoadState=loaded\nUnitFileState=disabled\nActiveState=inactive\n',
            }
        )
        self.assertEqual(samba.switch_off_unused(run), ['winbind.service'])
        self.assertIn(['systemctl', 'disable', '--now', 'winbind.service'], calls)
        self.assertNotIn(['systemctl', 'disable', '--now', 'nmbd.service'], calls)

    def test_a_masked_or_missing_unit_is_not_touched(self):
        run, calls = self.fake({'winbind.service': 'LoadState=masked\nUnitFileState=masked\nActiveState=inactive\n'})
        self.assertEqual(samba.switch_off_unused(run), [])
        self.assertFalse(any(c[1] == 'disable' for c in calls))


class SambaOwnConfigTests(unittest.TestCase):
    """Your own smb.conf stays yours: setup joins it through an include line you add."""

    def test_an_include_line_in_your_own_file_counts(self):
        self.assertTrue(samba.includes_ours('[global]\n   include = /etc/samba/nekoserver.conf\n'))
        self.assertTrue(
            samba.includes_ours(
                '[global]\n  workgroup = HOME\n  include=/etc/samba/nekoserver.conf\n[Scans]\n  path = /srv/scans\n'
            )
        )
        self.assertFalse(
            samba.includes_ours('[global]\n  workgroup = HOME\n  # include = /etc/samba/nekoserver.conf\n')
        )
        self.assertFalse(samba.includes_ours('[global]\n  workgroup = HOME\n'))


class SambaAccountTests(unittest.TestCase):
    """The shares keep their account across runs by different administrators."""

    def test_the_first_account_stays(self):
        # No share file yet (the machine running the tests may have a real one).
        with (
            mock.patch.dict(samba.os.environ, {'SUDO_USER': 'dana'}),
            mock.patch.object(samba, 'previous_account', return_value=None),
        ):
            self.assertEqual(samba.samba_account({}, previous=None), 'dana')  # first setup
            self.assertEqual(samba.samba_account({}, previous='neko'), 'neko')  # dana runs setup later
            self.assertEqual(samba.samba_account({'samba_user': 'dana'}, previous='neko'), 'dana')  # on purpose
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / 'nekoserver.conf'
            conf.write_text(
                samba.share_config({'shares': {'Media': 'library'}, 'share_mode': 'tailnet'}, Path('/mnt/m'), 'neko')
            )
            self.assertEqual(samba.previous_account(conf), 'neko')
            conf.write_text('[global]\n   valid users = someone\n')  # not NekoServer's file
            self.assertIsNone(samba.previous_account(conf))
            self.assertIsNone(samba.previous_account(Path(d) / 'missing'))


class SambaPasswordTests(unittest.TestCase):
    def test_a_mistyped_password_is_asked_again(self):
        codes = iter([1, 0])
        with mock.patch('builtins.print'):
            self.assertTrue(samba.samba_password('neko', run=lambda argv, **_: mock.Mock(returncode=next(codes))))
            self.assertFalse(samba.samba_password('neko', run=lambda argv, **_: mock.Mock(returncode=1)))


class SambaRewriteTests(unittest.TestCase):
    """NekoServer's own share file follows the settings; a foreign file, or widening
    tailnet-only sharing to the home network, stops setup."""

    def test_own_file_is_rewritten_and_the_two_guards_stay(self):
        own = samba.MANAGED_HEADER + '; never expose\n[global]\n   interfaces = lo 192.168.1.20\n[Media]\n'
        samba.check_rewrite(own, 'lan', {})  # a share added, say: fine
        tailnet = samba.MANAGED_HEADER + '; never expose\n[global]\n   interfaces = lo tailscale0\n'
        samba.check_rewrite(tailnet, 'tailnet', {})
        with self.assertRaisesRegex(RuntimeError, 'allow_tailnet_to_lan'):
            samba.check_rewrite(tailnet, 'lan', {})
        samba.check_rewrite(tailnet, 'lan', {'allow_tailnet_to_lan': True})
        with self.assertRaisesRegex(RuntimeError, 'not written by NekoServer'):
            samba.check_rewrite('[global]\n   workgroup = HOME\n', 'lan', {})


class SambaExampleTests(unittest.TestCase):
    """A smb.conf that is not NekoServer's: the package's untouched example is set aside
    (nobody's settings); a file someone wrote stops setup."""

    def test_the_packages_example_is_set_aside_and_a_written_one_stays(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / 'smb.conf'
            main.write_text('# Sample configuration file\n[global]\n')
            logged = []
            samba.unmanaged_config(main, mock.Mock(packaged_example=lambda path: True), logged.append)
            self.assertFalse(main.exists())
            self.assertEqual((Path(d) / 'smb.conf.packaged').read_text(), '# Sample configuration file\n[global]\n')
            self.assertIn("the package's unchanged example", logged[0])
            main.write_text('[global]\n   workgroup = HOME\n')
            with self.assertRaisesRegex(RuntimeError, 'not written by NekoServer'):
                samba.unmanaged_config(main, mock.Mock(packaged_example=lambda path: False), logged.append)
            self.assertTrue(main.exists())


class SambaSharesTests(unittest.TestCase):
    """A media-only server (Immich off) has no photo archive: no default Photos share."""

    def test_the_default_photos_share_follows_immich(self):
        config = {
            'networking': {'shares': {'Media': 'library', 'Photos': 'photos/archive'}},
            'photos': {'deploy_immich': False},
        }
        self.assertEqual(samba.wanted_shares(config), {'Media': 'library'})
        config['photos']['deploy_immich'] = True
        self.assertEqual(samba.wanted_shares(config), {'Media': 'library', 'Photos': 'photos/archive'})

    def test_a_share_you_set_yourself_stays(self):
        config = {
            'networking': {'shares': {'Media': 'library', 'Photos': 'my-pictures'}},
            'photos': {'deploy_immich': False},
        }
        self.assertEqual(samba.wanted_shares(config), {'Media': 'library', 'Photos': 'my-pictures'})


if __name__ == '__main__':
    unittest.main()


class SambaTailnetStartTests(unittest.TestCase):
    """With "bind interfaces only", smbd started before tailscale0 had its address never
    listened on the tailnet until it was restarted (Samba after a reboot)."""

    def test_tailnet_mode_waits_for_the_address_and_never_fails_the_start(self):
        from modules import samba

        text = samba.storage_guard('/mnt/m', 'mnt-m.mount', 'tailnet', 'tailscaled.service')
        start = [line for line in text.splitlines() if line.startswith('ExecStartPre=')]
        self.assertEqual(len(start), 1)
        self.assertTrue(start[0].startswith('ExecStartPre=-/bin/sh -c '))  # "-": a failure is ignored too
        self.assertIn('tailscale0', start[0])
        self.assertTrue(start[0].rstrip("'").endswith('exit 0'))
        self.assertIn('$$i', start[0])  # systemd's own $ escaped

    def test_home_network_mode_does_not_wait_for_tailscale(self):
        from modules import samba

        self.assertNotIn('ExecStartPre', samba.storage_guard('/mnt/m', 'mnt-m.mount', 'lan', 'tailscaled.service'))
