"""Setup wizard, capacity check and recovery wizard."""

import gzip
import json
import os
import pwd
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import first_checkup
import recovery_wizard  # noqa: E402
import wizard  # noqa: E402
from modules import capacity  # noqa: E402

GIB = 1024**3
REAL_HDD_READY = recovery_wizard.hdd_ready  # the real drive check (most tests replace it)


def chown_refused(folder, uid, gid):
    """Whether this system refuses to give a file to `uid`:`gid` (a container or user
    namespace without those IDs answers EINVAL), so a test of it can't run here."""
    probe = Path(folder) / '.chown-probe'
    probe.write_bytes(b'')
    try:
        os.chown(probe, uid, gid)
        return False
    except OSError:
        return True
    finally:
        probe.unlink()


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class Ctx:
    def __init__(self, config, apply=True):
        self.config, self.apply, self.logs = config, apply, []

    def log(self, message):
        self.logs.append(message)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'config.json', self.root / 'config.json')

    def test_created_once_with_the_sudo_users_ids(self):
        me = pwd.getpwuid(os.getuid())
        path, created = wizard.prepare_settings(self.root, me)
        self.assertTrue(created)
        settings = json.loads(path.read_text())
        self.assertEqual((settings['media']['uid'], settings['media']['gid']), (me.pw_uid, me.pw_gid))
        self.assertEqual(settings['timezone'], 'auto')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Second run keeps the file (and the choices the installer saved in it).
        settings['storage']['data_ssd']['uuid'] = 'kept'
        path.write_text(json.dumps(settings))
        again, created_again = wizard.prepare_settings(self.root, me)
        self.assertFalse(created_again)
        self.assertEqual(json.loads(again.read_text())['storage']['data_ssd']['uuid'], 'kept')

    def test_started_as_root_without_an_account_ids_are_never_guessed(self):
        # Debian with a root password and no sudo: no SUDO_USER. The template's 1000 is
        # kept only when an account has it; otherwise setup stops and says how to name one.
        with (
            mock.patch.object(wizard.pwd, 'getpwuid', side_effect=KeyError(1000)),
            self.assertRaisesRegex(
                RuntimeError, r'no account with user ID 1000.*SUDO_USER=<your account name> bash install.sh'
            ),
        ):
            wizard.prepare_settings(self.root, None)
        self.assertFalse((self.root / wizard.SETTINGS).exists())  # nothing written
        with mock.patch.object(wizard.pwd, 'getpwuid', return_value=pwd.getpwuid(os.getuid())):
            path, created = wizard.prepare_settings(self.root, None)
        self.assertTrue(created)
        self.assertEqual(json.loads(path.read_text())['media']['uid'], 1000)

    def test_template_runs_tailscale_and_capacity_in_full(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        self.assertEqual(full[:4], ['system', 'tailscale', 'storage', 'capacity'])
        self.assertLess(full.index('tailscale'), full.index('gateway_tailnet'))


class WizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A release folder of its own: the folders beside it are this test's too.
        self.root = Path(self.tmp.name) / 'nekoserver-v0.31.0'
        self.root.mkdir()
        shutil.copy(ROOT / 'config.json', self.root / 'config.json')
        geteuid = mock.patch('os.geteuid', return_value=0)
        geteuid.start()
        self.addCleanup(geteuid.stop)
        # Without sudo the template's user ID needs an account; whatever this test PC has.
        account = mock.patch.object(wizard.pwd, 'getpwuid', return_value=pwd.getpwuid(os.getuid()))
        account.start()
        self.addCleanup(account.stop)
        # The template names the real /srv/nekoserver: on a PC running NekoServer its setup
        # state there would be read (or refused to a normal user). This test's own folder.
        folder = mock.patch.object(wizard, 'data_folder', return_value=self.root / 'ssd')
        folder.start()
        self.addCleanup(folder.stop)

    def test_saying_no_changes_nothing(self):
        execute = mock.Mock()
        out = []
        code = wizard.run(execute, self.root / 'state', prompt=answers('no'), output=out.append, root=self.root)
        self.assertEqual(code, 0)
        execute.assert_not_called()
        self.assertTrue(any('No apps were set up' in line for line in out))

    def test_yes_runs_the_full_install_with_the_settings_file(self):
        execute = mock.Mock(return_value=0)
        out = []
        with mock.patch('first_checkup.run') as checkup:
            wizard.run(execute, self.root / 'state', prompt=answers('yes'), output=out.append, root=self.root)
        args = execute.call_args[0][0]
        self.assertEqual((args.profile, args.apply, args.config), ('full', True, str(self.root / 'my-config.json')))
        self.assertTrue(any(line.strip().startswith(('NekoServer is', 'Setup stopped:')) for line in out))
        # Every step finished: the first checkup and backup run straight away.
        checkup.assert_called_once()

    def test_a_failed_first_checkup_fails_the_setup(self):
        with mock.patch('first_checkup.run', return_value=False):
            code = wizard.run(
                mock.Mock(return_value=0),
                self.root / 'state',
                prompt=answers('yes'),
                output=lambda s: None,
                root=self.root,
            )
        self.assertEqual(code, 1)

    def test_no_first_backup_while_steps_are_unfinished(self):
        with mock.patch('first_checkup.run') as checkup:
            wizard.run(
                mock.Mock(return_value=1),
                self.root / 'state',
                prompt=answers('yes'),
                output=lambda s: None,
                root=self.root,
            )
        checkup.assert_not_called()

    def test_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(
                wizard.run(mock.Mock(), self.root, prompt=answers(), output=lambda s: None, root=self.root), 2
            )

    def saved(self, path, uuid):
        settings = json.loads((ROOT / 'config.json').read_text())
        settings['storage']['data_ssd']['uuid'] = uuid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings))

    def setup_until_ready(self, *replies):
        """The wizard run up to "Ready to start?", answered no; the questions it asked."""
        asked, it = [], iter(replies + ('no',))
        out = []

        def prompt(question=''):
            asked.append(question)
            return next(it)

        execute = mock.Mock()
        self.assertEqual(wizard.run(execute, self.root / 'state', prompt=prompt, output=out.append, root=self.root), 0)
        execute.assert_not_called()
        return asked, out

    def settings_uuid(self):
        return json.loads((self.root / 'my-config.json').read_text())['storage']['data_ssd']['uuid']

    def test_the_fixed_folder_finds_the_settings_left_in_a_versioned_one(self):
        # Since 0.38.0 the folder is simply `nekoserver`; a first update from a release
        # before that leaves my-config.json in the old versioned folder beside it.
        root = self.root.parent / 'nekoserver'
        root.mkdir()
        shutil.copy(ROOT / 'config.json', root / 'config.json')
        self.saved(self.root.parent / 'nekoserver-v0.37.3/my-config.json', 'kept')
        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'older')
        self.assertEqual(wizard.earlier_settings(root), self.root.parent / 'nekoserver-v0.37.3/my-config.json')
        # Later updates stay in the same folder: its own settings are simply used.
        self.saved(root / 'my-config.json', 'mine')
        self.assertTrue((root / 'my-config.json').is_file())

    def test_a_newer_settings_file_that_cannot_be_read_is_named_and_the_next_one_used(self):
        # Never a silent choice: a damaged my-config.json in the newest earlier folder is
        # reported, and the next readable one (here the copy setup kept on the SSD) is offered.
        root = self.root.parent / 'nekoserver'
        root.mkdir()
        shutil.copy(ROOT / 'config.json', root / 'config.json')
        damaged = self.root.parent / 'nekoserver-v0.37.3/my-config.json'
        damaged.parent.mkdir()
        damaged.write_text('{"storage": ')
        self.saved(self.root.parent / 'nekoserver-v0.37.2/my-config.json', 'older')
        shown = []
        self.assertEqual(
            wizard.earlier_settings(root, shown.append), self.root.parent / 'nekoserver-v0.37.2/my-config.json'
        )
        self.assertEqual(len(shown), 1)
        self.assertIn(str(damaged) + ' cannot be read', shown[0])
        # The person's own files come before setup's copies on the SSD, whatever the versions.
        (root / 'config.json').write_text(
            json.dumps({**json.loads((ROOT / 'config.json').read_text()), 'data_mount': str(self.root / 'ssd')})
        )
        self.saved(self.root / 'ssd/installer/releases/v0.38.0/config.json', 'ssd')
        self.assertEqual(
            [p.parent.name for p in wizard.earlier_candidates(root)],
            ['nekoserver-v0.37.3', 'nekoserver-v0.37.2', 'v0.38.0'],
        )

    def test_an_update_takes_the_settings_left_in_the_earlier_release_folder(self):
        # git pull moved the release into a new folder; my-config.json stayed in the old one.
        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'kept')
        self.saved(self.root.parent / 'nekoserver-v0.29.0/my-config.json', 'older')
        self.saved(self.root / 'ssd/installer/releases/v0.30.0/config.json', 'ssd')
        asked, out = self.setup_until_ready('yes')
        self.assertIn('Use them for this release', asked[0])
        self.assertTrue(any(str(self.root.parent / 'nekoserver-v0.30.0/my-config.json') in line for line in out))
        self.assertEqual(self.settings_uuid(), 'kept')
        self.assertEqual((self.root / 'my-config.json').stat().st_mode & 0o777, 0o600)

    def test_the_copy_on_the_ssd_is_offered_when_no_folder_has_one(self):
        self.saved(self.root / 'ssd/installer/releases/v0.30.0/config.json', 'ssd')
        self.saved(self.root / 'ssd/installer/releases/v0.29.0/config.json', 'older')
        self.saved(self.root / 'ssd/installer/releases/v0.31.0.old/config.json', 'set aside')
        (self.root.parent / 'nekoserver-v0.30.0').mkdir()  # left without settings
        self.setup_until_ready('yes')
        self.assertEqual(self.settings_uuid(), 'ssd')

    def test_saying_no_starts_from_the_template(self):
        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'kept')
        asked, out = self.setup_until_ready('no')
        self.assertTrue(any('asks for the drives and passwords again' in line for line in out))
        self.assertEqual(self.settings_uuid(), '')

    def test_stopping_at_that_question_keeps_them_for_the_next_run(self):
        # Ctrl+C counted as "no": a new settings file was made, and since it then
        # existed, no later run offered the earlier settings again.
        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'kept')

        def stopped(question=''):
            raise KeyboardInterrupt

        execute, state = mock.Mock(), self.root / 'state'
        with self.assertRaises(KeyboardInterrupt):
            wizard.run(execute, state, prompt=stopped, output=print, root=self.root)
        self.assertFalse((self.root / 'my-config.json').exists())
        self.setup_until_ready('yes')
        self.assertEqual(self.settings_uuid(), 'kept')

    def test_settings_already_here_are_used_without_asking(self):
        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'old folder')
        self.saved(self.root / 'my-config.json', 'here')
        asked, _ = self.setup_until_ready()
        self.assertEqual(len(asked), 1)  # only "Ready to start?"
        self.assertEqual(self.settings_uuid(), 'here')

    def test_a_first_install_asks_nothing_more(self):
        (self.root.parent / 'notes').mkdir()
        asked, _ = self.setup_until_ready()
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.settings_uuid(), '')

    def test_recovery_reads_the_earlier_settings_too(self):
        import recovery_wizard

        self.saved(self.root.parent / 'nekoserver-v0.30.0/my-config.json', 'kept')
        out = []
        with mock.patch.object(recovery_wizard, 'ROOT', self.root):
            config = recovery_wizard.settings(output=out.append)
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'kept')
        self.assertIn('nekoserver-v0.30.0', out[0])


class FirstCheckupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'data_mount': self.tmp.name}
        (Path(self.tmp.name) / 'reports/jobs').mkdir(parents=True)

    def report(self, job, status, **extra):
        (Path(self.tmp.name) / 'reports/jobs' / (job + '.json')).write_text(json.dumps({'status': status, **extra}))

    def systemctl(self, enabled, failing=()):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            unit = argv[-1].rsplit('.', 1)[0]
            if argv[1] == 'is-enabled':
                return mock.Mock(
                    returncode=0 if unit in enabled else 1, stdout='enabled\n' if unit in enabled else 'disabled\n'
                )
            return mock.Mock(returncode=1 if unit in failing else 0)

        return run, calls

    def test_only_switched_on_backups_run_in_order_then_the_checkup(self):
        self.report('applications', 'pass')
        run, calls = self.systemctl({'nekoserver-app-backup', 'nekoserver-media-health'})
        out = []
        ok = first_checkup.run(self.config, out.append, run, checks=lambda config: (12, []))
        started = [argv[-1] for argv in calls if argv[1] == 'start']
        self.assertEqual(started, ['nekoserver-media-health.service', 'nekoserver-app-backup.service'])
        self.assertTrue(ok)
        self.assertIn('  Checkup: 12 checks passed.', out)

    def test_a_failed_backup_and_a_failed_check_are_named(self):
        run, _ = self.systemctl({'nekoserver-host-backup'}, failing={'nekoserver-host-backup'})
        out = []
        ok = first_checkup.run(self.config, out.append, run, checks=lambda config: (3, ['Drive space']))
        self.assertFalse(ok)
        text = '\n'.join(out)
        self.assertIn('journalctl -u nekoserver-host-backup.service', text)
        self.assertIn('Needs a look: Drive space', text)

    def test_a_partial_backup_is_not_called_done(self):
        # systemctl says it finished fine; its own report says part was left out.
        self.report('host', 'partial', missing={'source': 'Steam was open'})
        run, _ = self.systemctl({'nekoserver-host-backup'})
        out = []
        self.assertFalse(first_checkup.run(self.config, out.append, run, checks=lambda config: (3, [])))
        self.assertIn('    done in part: Steam was open', out)

    def test_a_check_still_to_set_up_needs_a_look(self):
        report = {
            'automatic': [
                {'id': 'a', 'name': 'Drives', 'status': 'pass'},
                {'id': 'b', 'name': 'Phone alerts', 'status': 'needs_setup'},
            ]
        }
        with mock.patch('acceptance.inspect', return_value=report), mock.patch('acceptance.save'):
            self.assertEqual(first_checkup.inspect(self.config), (1, ['Phone alerts (still to set up)']))

    def test_a_checkup_that_cannot_run_says_how_to_run_it_later(self):
        run, _ = self.systemctl(set())

        def broken(config):
            raise RuntimeError('SSD not mounted')

        out = []
        self.assertFalse(first_checkup.run(self.config, out.append, run, checks=broken))
        self.assertIn('acceptance.py --save', out[-1])

    def test_a_failed_checkup_is_never_ready(self):
        out = []
        states = {'storage': {'status': 'success'}}
        config = {'data_mount': self.tmp.name}
        self.assertFalse(wizard.finishing_screen(config, states, None, out.append, checkup=False))
        text = '\n'.join(out)
        self.assertIn('almost ready', text)
        self.assertNotIn('NekoServer is ready!', text)
        self.assertIn('First checkup and backup: something needs a look', text)
        self.assertTrue(wizard.finishing_screen(config, states, None, lambda s: None, checkup=True))


class FinishingScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'reports').mkdir()
        (self.data / 'reports/gateway-lan.json').write_text(json.dumps({'portal': 'https://192.168.1.20:8442/'}))
        (self.data / 'reports/gateway-tailnet.json').write_text(
            json.dumps({'links': {'jellyfin': 'https://neko.tail1.ts.net:8443/'}})
        )
        self.config = {'data_mount': str(self.data)}

    def screen(self, states):
        out = []
        complete = wizard.finishing_screen(self.config, states, None, out.append)
        return complete, '\n'.join(out)

    def test_all_done(self):
        complete, text = self.screen({'storage': {'status': 'success'}, 'media': {'status': 'success'}})
        self.assertTrue(complete)
        self.assertIn('NekoServer is ready!', text)
        self.assertIn('https://192.168.1.20:8442/', text)
        self.assertIn('https://neko.tail1.ts.net:8443/', text)
        self.assertIn('account-setup.py show', text)
        self.assertIn('--recover', text)

    def test_the_dashboard_address_uses_its_configured_port(self):
        _, text = self.screen({'storage': {'status': 'success'}})
        self.assertIn('http://127.0.0.1:8787', text)
        self.config['dashboard'] = {'port': 8899}
        _, text = self.screen({'storage': {'status': 'success'}})
        self.assertIn('Dashboard:          http://127.0.0.1:8899', text)
        self.assertNotIn('8787', text)

    def test_the_finishing_screen_says_whether_a_pin_was_chosen(self):
        with tempfile.TemporaryDirectory() as folder:
            pin = Path(folder) / 'pin.json'
            out = []
            wizard.show_dashboard(self.config, out.append, pin)
            self.assertIn('not set', out[-1])
            pin.write_text('{}')
            out = []
            wizard.show_dashboard(self.config, out.append, pin)
            self.assertEqual(out[-1], 'Dashboard PIN:      set')

    def test_missing_or_unreadable_state_is_never_ready(self):
        complete, text = self.screen({})
        self.assertFalse(complete)
        self.assertNotIn('NekoServer is ready!', text)
        self.config['profiles'] = {'full': ['system', 'storage', 'media']}
        complete, text = self.screen({'system': {'status': 'success'}, 'storage': {'status': 'success'}})
        self.assertFalse(complete)
        self.assertIn('Movie, TV and music apps: did not run', text)
        complete, text = self.screen(
            {'system': {'status': 'success'}, 'storage': {'status': 'success'}, 'media': {'status': 'success'}}
        )
        self.assertTrue(complete)

    def test_pending_and_failed_steps_are_named_in_plain_words(self):
        complete, text = self.screen(
            {
                'storage': {'status': 'success'},
                'tailscale': {'status': 'needs_setup', 'reason': 'Tailscale authorization pending'},
                'gateway_tailnet': {'status': 'blocked', 'reason': "Dependencies not successful: ['tailscale']"},
            }
        )
        self.assertFalse(complete)
        self.assertIn('Setup stopped: 1 thing needs you', text)  # 1 of 3 steps done is not "almost ready"
        self.assertIn('Tailscale: Tailscale authorization pending', text)
        self.assertIn('Access from anywhere and phone setup', text)
        self.assertIn('run  sudo bash install.sh  again', text)


class CapacityTests(unittest.TestCase):
    CONFIG = {'data_mount': '/ssd', 'media_mount': '/hdd', 'photos': {'external_backup_uuid': 'u'}}

    def assess(self, ssd, hdd, usb=None, photos=0, cloud=0, pending=None, dump=0, unarchived=0):
        free = {'/ssd': ssd * GIB, '/hdd': hdd * GIB}
        used = {'/ssd/photos/immich-library': photos * GIB, '/hdd/cloud/files': cloud * GIB}
        pending = photos + cloud if pending is None else pending
        return capacity.assess(
            self.CONFIG,
            free=lambda p: free[str(p)],
            used=lambda p: used.get(str(p), 0),
            usb_space=lambda uuid, pairs, **_: None if usb is None else (usb * GIB, pending * GIB),
            dump=lambda config: dump * GIB,
            unarchived=lambda config: unarchived * GIB,
        )

    def test_usb_counts_free_space_not_drive_size(self):
        # A big but nearly full drive warns; files the drive already has need no new room.
        _, warn = self.assess(500, 4000, usb=10, photos=100)
        self.assertTrue(any('USB drive has 10 GB free' in w for w in warn))
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=0)
        self.assertEqual(warn, [])

    def test_old_versions_on_the_usb_are_not_counted_as_room(self):
        # 100 GB of new files, 30 GB free, 150 GB of earlier versions: the copy cannot fit.
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=100)
        self.assertTrue(any('still has to add 100 GB' in w for w in warn))
        # This week's database copy counts too.
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=5, dump=20)
        self.assertTrue(any('still has to add 25 GB' in w for w in warn))

    def test_pending_counts_only_new_and_changed_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ssd, hdd, usb = root / 'ssd', root / 'hdd', root / 'usb'
            files = {
                hdd / 'photos/archive/upload/u/same.jpg': (b'a' * 10, b'a' * 10),
                hdd / 'photos/archive/upload/u/new.jpg': (b'b' * 20, None),
                hdd / 'photos/archive/library/changed.xmp': (b'c' * 30, b'c' * 5),
                hdd / 'cloud/files/alice/files/doc.txt': (b'd' * 40, None),
                hdd / 'cloud/files/appdata_oc1a2b3c4d5e/files/skip.bin': (b'e' * 50, None),
                hdd / 'photos/archive/upload/u/live.jpg.neko-part': (b'f' * 60, None),
                hdd / 'cloud/files/alice/files/v.mp4.ocTransferId42.part': (b'g' * 70, None),
            }
            for path, (source, copy) in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source)
            mapping = {pair[0]: pair[1] for pair in capacity.usb_sources({'data_mount': ssd, 'media_mount': hdd})}
            for path, (source, copy) in files.items():
                base = next((s for s in mapping if s in path.parents), None)
                if copy is not None and base is not None:
                    target = usb / mapping[base] / path.relative_to(base)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(copy)
            # An old dated version on the drive is not room for anything new.
            (usb / 'upload/u/same.neko-20250101T000000Z.jpg').write_bytes(b'x' * 999)
            self.assertEqual(capacity.pending_bytes(usb, list(mapping.items())), 20 + 30 + 40)

    def test_photos_kept_only_in_the_hdd_archive_need_room_on_the_usb(self):
        # A photo deleted from Immich stays in the archive and the USB job copies it; a
        # photo not archived yet goes to the archive first and then to the USB drive too.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = {'data_mount': root / 'ssd', 'media_mount': root / 'hdd'}
            usb = root / 'usb'
            (root / 'hdd/photos/archive/upload/u').mkdir(parents=True)
            (root / 'hdd/photos/archive/upload/u/deleted-from-immich.jpg').write_bytes(b'k' * 70)
            (root / 'hdd/photos/archive/upload/u/old.neko-20261001T000000Z.jpg').write_bytes(b'v' * 5)
            (root / 'ssd/photos/immich-library/upload/u').mkdir(parents=True)
            (root / 'ssd/photos/immich-library/upload/u/brand-new.jpg').write_bytes(b'n' * 9)
            usb.mkdir()
            self.assertEqual(capacity.pending_bytes(usb, capacity.usb_sources(config)), 75)
            self.assertEqual(capacity.awaiting_archive(config), 9)
        _, warn = self.assess(500, 4000, usb=30, pending=20, unarchived=15)
        self.assertTrue(any('still has to add 35 GB' in w for w in warn), warn)

    def test_a_future_dated_database_copy_is_not_the_newest(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'backups/immich-database'
            folder.mkdir(parents=True)
            (folder / 'immich-20261025T021500Z.sql').write_bytes(b'x' * 10)
            (folder / 'immich-20300101T021500Z.sql').write_bytes(b'x' * 99)
            self.assertEqual(capacity.newest_dump_bytes({'media_mount': d}, now=1_792_900_000.0), 10)

    def test_a_user_named_appdata_something_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = {'data_mount': root / 'ssd', 'media_mount': root / 'hdd'}
            for user in ('appdata_alice', 'appdata_ocq1w2e3r4t5', 'appdata_ocabcdefghij'):
                (root / 'hdd/cloud/files' / user / 'files').mkdir(parents=True)
            users = lambda: sorted(n for _, n in capacity.usb_sources(config) if n.startswith('nextcloud/'))  # noqa: E731
            # Without config.php: Nextcloud's own name form only.
            self.assertEqual(users(), ['nextcloud/appdata_alice/files'])
            # With it: exactly its instance's folder.
            php = root / 'ssd/appdata/nextcloud/html/config/config.php'
            php.parent.mkdir(parents=True)
            php.write_text("<?php\n$CONFIG = array (\n  'instanceid' => 'ocq1w2e3r4t5',\n);\n")
            self.assertEqual(users(), ['nextcloud/appdata_alice/files', 'nextcloud/appdata_ocabcdefghij/files'])

    def test_a_damaged_usb_photo_counts_its_full_replacement(self):
        # The weekly mirror keeps a damaged USB copy aside and writes the archive file
        # again; the estimate must count that, following the mirror's own checks.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive, usb = root / 'hdd/photos/archive', root / 'usb'
            (archive / 'upload').mkdir(parents=True)
            (usb / 'upload').mkdir(parents=True)
            (archive / 'upload/current.mp4').write_bytes(b'A' * 40)
            (archive / 'upload/.current.mp4.neko-latest').write_text('current.mp4')
            (archive / 'upload/other.mp4').write_bytes(b'B' * 30)
            (usb / 'upload/.current.mp4.neko-latest').write_text('current.mp4')
            (usb / 'upload/current.mp4').write_bytes(b'Z' * 40)  # damaged, same size, newer
            (usb / 'upload/other.mp4').write_bytes(b'Y' * 30)  # damaged, same size, newer
            pairs = [(archive / 'upload', 'upload')]
            # Weekly: the noted file is checked by checksum, the other one is not (the
            # monthly checksum check compares everything).
            self.assertEqual(capacity.pending_bytes(usb, pairs, mirror=('upload',)), 40)

    def test_a_same_size_edit_needs_room_again(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src, usb = root / 'src', root / 'usb'
            usb.mkdir()
            (usb / 'a.txt').write_bytes(b'AAAA')
            (usb / 'b.txt').write_bytes(b'BBBB')
            (usb / 'c.txt').write_bytes(b'CCCC')
            (usb / 'c.neko-20261001T000000Z.txt').write_bytes(b'DDDD')
            src.mkdir()
            (src / 'a.txt').write_bytes(b'XXXX')  # edited, same size: a new version on the USB
            (src / 'b.txt').write_bytes(b'BBBB')  # rewritten with the same content
            (src / 'c.txt').write_bytes(b'DDDD')  # back to a version the drive already has
            for name in ('a.txt', 'b.txt', 'c.txt', 'c.neko-20261001T000000Z.txt'):
                os.utime(usb / name, (0, 0))  # copies older than the source change
            self.assertEqual(capacity.pending_bytes(usb, [(src, '.')]), 4)

    def test_enough_space(self):
        self.assertEqual(self.assess(500, 4000, usb=1000, photos=200), ([], []))

    def test_too_little_stops_with_a_clear_reason(self):
        stop, _ = self.assess(10, 4000)
        self.assertTrue(stop and 'SSD has only 10 GB free' in stop[0])
        stop, _ = self.assess(500, 30)
        self.assertTrue(stop and 'HDD' in stop[0])

    def test_below_recommended_warns(self):
        stop, warn = self.assess(60, 300)
        self.assertEqual(stop, [])
        self.assertEqual(len(warn), 2)

    def test_usb_must_fit_photos_and_cloud_with_room(self):
        _, warn = self.assess(500, 4000, usb=128, photos=100, cloud=20)
        self.assertTrue(any('USB drive has 128 GB free' in w for w in warn))
        _, warn = self.assess(500, 4000, usb=256, photos=100, cloud=20)
        self.assertEqual(warn, [])

    def test_stop_raises_and_warning_asks(self):
        ctx = Ctx({})
        new_install = mock.patch.object(capacity, 'installed', return_value=False)
        with new_install, mock.patch.object(capacity, 'assess', return_value=(['too small'], [])):
            with self.assertRaisesRegex(RuntimeError, 'too small'):
                capacity.run(ctx)
        with (
            new_install,
            mock.patch.object(capacity, 'assess', return_value=([], ['low'])),
            mock.patch('guided_setup.interactive', return_value=True),
        ):
            self.assertIsNone(capacity.run(Ctx({}), prompt=answers('yes'), output=lambda s: None))
            result = capacity.run(Ctx({}), prompt=answers('no'), output=lambda s: None)
            self.assertEqual(result['status'], 'needs_setup')


def then_stopped(*values):
    """These answers, then Ctrl+D."""
    it = iter(values)

    def prompt(question=''):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return prompt


class RecoveryWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        (self.root / 'ssd/installer').mkdir(parents=True)  # as on a set-up SSD
        self.config = {
            'media_mount': str(self.media),
            'data_mount': str(self.root / 'ssd'),
            'photos': {'archive_dir': 'photos/archive'},
        }
        # The HDD counts as mounted here (the restore folder is suggested on it), and so do
        # the SSD and the USB drive (the repair checks each drive it uses).
        for name in ('hdd_ready', 'ssd_ready', 'usb_ready'):
            ready = mock.patch.object(recovery_wizard, name, return_value=True)
            ready.start()
            self.addCleanup(ready.stop)
        # Dates are said in this computer's own time: UTC for these tests.
        utc = mock.patch.dict(os.environ, {'TZ': 'UTC'})
        utc.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(utc.stop)

    def write(self, rel, body=b'x'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def test_backups_are_listed_newest_first(self):
        for name in ('snapshot-1000000-1', 'snapshot-3000000-1', 'snapshot-2000000-1'):
            self.write(f'hdd/backups/media-applications/{name}/manifest.json')
        self.write('hdd/backups/media-applications/.snapshot-9-1.partial/manifest.json')
        names = [p.name for p, _ in recovery_wizard.snapshots(self.media / 'backups/media-applications')]
        self.assertEqual(names, ['snapshot-3000000-1', 'snapshot-2000000-1', 'snapshot-1000000-1'])
        self.write('hdd/backups/monthly/100-1/manifest.json')
        self.write('hdd/backups/monthly/200-1/manifest.json')
        self.write('hdd/backups/monthly/200-1/secrets.tar.cms')
        runs = recovery_wizard.monthly_snapshots(self.media / 'backups/monthly', need='secrets.tar.cms')
        self.assertEqual([p.name for p, _ in runs], ['200-1'])

    def test_restore_folder_must_be_new(self):
        existing = self.root / 'exists'
        existing.mkdir()
        out = []
        # Suggested folder exists -> refused; typed relative path -> refused; then a good one.
        chosen = recovery_wizard.new_folder(
            existing, answers('yes', 'relative', str(self.root / 'fresh'), 'yes'), out.append
        )
        self.assertEqual(chosen, self.root / 'fresh')
        self.assertTrue(any('already exists' in line for line in out))
        self.assertIsNone(recovery_wizard.new_folder(existing, answers('no', ''), out.append))

    def test_photos_restore_copies_into_a_new_folder_only(self):
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        self.write('hdd/photos/archive/library/b.heic', b'photo2')
        self.write('ssd/photos/immich-library/upload/live.jpg', b'live')
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_photos(self.config, answers('1', 'yes', 'no'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'upload/u1/a.jpg').read_bytes(), b'photo')
        self.assertEqual((restored / 'library/b.heic').read_bytes(), b'photo2')
        self.assertEqual((self.root / 'ssd/photos/immich-library/upload/live.jpg').read_bytes(), b'live')
        self.assertTrue(any('2 files copied' in line for line in out))
        # Other accounts never get in (the restore itself is given to the person's own).
        self.assertEqual((self.media / 'restored').stat().st_mode & 0o007, 0)

    def test_a_restore_never_goes_inside_a_backup(self):
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        inside = self.media / 'photos/archive/upload/restored-here'
        elsewhere = self.root / 'restore-elsewhere'
        out = []
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            # Offered folder declined; one inside the archive refused; then a separate one.
            recovery_wizard.restore_photos(
                self.config, answers('1', 'no', str(inside), 'yes', str(elsewhere), 'yes', 'no'), out.append
            )
        self.assertIn('inside (or around) a backup', '\n'.join(out))
        self.assertFalse(inside.exists())
        self.assertEqual((elsewhere / 'upload/u1/a.jpg').read_bytes(), b'photo')
        # The archive still holds its one file only.
        self.assertEqual(len([p for p in (self.media / 'photos/archive').rglob('*') if p.is_file()]), 1)
        # A folder around a backup is refused too.
        self.assertIsNotNone(
            recovery_wizard.folder_problem(self.media / 'photos', recovery_wizard.backup_trees(self.config))
        )

    def test_nextcloud_restore_lists_users_from_the_usb(self):
        usb = self.root / 'usb'
        self.write('usb/nextcloud/alice/files/Docs/plan.odt', b'plan')
        self.write('usb/nextcloud/bob/files/todo.txt', b'todo')
        self.assertEqual(recovery_wizard.cloud_users(usb), ['alice', 'bob'])
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('2', 'yes', 'no'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertIn('nextcloud-bob', restored.name)
        self.assertEqual((restored / 'files/todo.txt').read_bytes(), b'todo')

    def test_what_a_change_of_shape_set_aside_is_restored_too(self):
        # A file "Projects" that became a folder: the backup kept the file aside, dated.
        usb = self.root / 'usb'
        self.write('usb/nextcloud/alice/files/Projects/notes.txt', b'notes')
        self.write('usb/nextcloud/alice/neko-replaced/20261025T010000Z/Projects', b'the old file')
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'files/Projects/notes.txt').read_bytes(), b'notes')
        self.assertEqual((restored / 'replaced earlier/20261025T010000Z/Projects').read_bytes(), b'the old file')
        self.assertIn('replaced earlier', '\n'.join(out))

    def test_files_encrypted_by_nextcloud_are_not_called_ready_to_upload(self):
        usb = self.root / 'usb'
        self.write(
            'usb/nextcloud/alice/files/report.pdf',
            b'HBEGIN:oc_encryption_module:OC_DEFAULT_MODULE:cipher:AES-256-CTR:HEND',
        )
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'no'), out.append)
        text = '\n'.join(out)
        self.assertIn('ENCRYPTED', text)
        self.assertNotIn('upload the', text)

    def test_newest_version_is_restored_under_its_own_name(self):
        usb = self.root / 'usb'
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.docx', b'first')
        self.write(files + 'report.neko-20261001T090000Z.docx', b'second')
        self.write(files + 'report.neko-20261012T140300Z.docx', b'newest')
        self.write(files + 'report.neko-20261012T140300Z-1.docx', b'newest, same second')
        self.write(files + 'notes', b'only one')
        self.write(files + 'IMG_1.HEIC.neko-20261005T000000Z.xmp', b'sidecar edit')
        self.write(files + 'IMG_1.HEIC.xmp', b'sidecar')
        # What the backup records (since 0.28.7) about whose versions these are.
        self.write(
            files + '.neko-names.json',
            json.dumps(
                {
                    'format': 'neko-names-v1',
                    'names': {},
                    'versions': {
                        'report.neko-20261001T090000Z.docx': 'report.docx',
                        'report.neko-20261012T140300Z.docx': 'report.docx',
                        'report.neko-20261012T140300Z-1.docx': 'report.docx',
                        'IMG_1.HEIC.neko-20261005T000000Z.xmp': 'IMG_1.HEIC.xmp',
                    },
                }
            ).encode(),
        )
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), out.append)
        restored = next((self.media / 'restored').iterdir()) / 'files'
        self.assertEqual(sorted(p.name for p in restored.iterdir()), ['IMG_1.HEIC.xmp', 'notes', 'report.docx'])
        self.assertEqual((restored / 'report.docx').read_bytes(), b'newest, same second')
        self.assertEqual((restored / 'IMG_1.HEIC.xmp').read_bytes(), b'sidecar edit')
        text = '\n'.join(out)
        self.assertIn('report.docx: version from 2026-10-12 14:03 UTC (4 versions', text)
        self.assertNotIn('older versions', text)

    def restore_newest(self):
        usb = self.root / 'usb'
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), lambda s: None)
        return next((self.media / 'restored').iterdir()) / 'files'

    def test_a_file_that_only_looks_like_a_version_is_restored_as_itself(self):
        # Two separate originals, both backed up in their own right: never merged.
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'one document')
        self.write(files + 'report.neko-20261022T120000Z.txt', b'another document')
        restored = self.restore_newest()
        self.assertEqual(sorted(p.name for p in restored.iterdir()), ['report.neko-20261022T120000Z.txt', 'report.txt'])
        self.assertEqual((restored / 'report.txt').read_bytes(), b'one document')

    def test_an_older_looking_file_of_its_own_is_never_taken_for_a_version(self):
        # Two separate originals; report.txt was then edited (a recorded version and a
        # note naming it). The older-looking file stays a file of its own.
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'first')
        self.write(files + 'report.neko-20250101T120000Z.txt', b'another document')
        self.write(files + 'report.neko-20261022T130000Z.txt', b'edited')
        self.write(files + '.report.txt.neko-latest', b'report.neko-20261022T130000Z.txt')
        self.write(
            files + '.neko-names.json',
            json.dumps(
                {'format': 'neko-names-v1', 'names': {}, 'versions': {'report.neko-20261022T130000Z.txt': 'report.txt'}}
            ).encode(),
        )
        restored = self.restore_newest()
        self.assertEqual((restored / 'report.txt').read_bytes(), b'edited')
        self.assertEqual((restored / 'report.neko-20250101T120000Z.txt').read_bytes(), b'another document')

    def test_a_recorded_version_list_decides_over_the_name(self):
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'first')
        self.write(files + 'report.neko-20261001T090000Z.txt', b'edit')
        self.write(files + '.report.txt.neko-latest', b'report.neko-20261001T090000Z.txt')
        self.write(files + 'report.neko-20261022T120000Z.txt', b'a file of its own')
        self.write(
            files + '.neko-names.json',
            json.dumps(
                {'format': 'neko-names-v1', 'names': {}, 'versions': {'report.neko-20261001T090000Z.txt': 'report.txt'}}
            ).encode(),
        )
        restored = self.restore_newest()
        self.assertEqual((restored / 'report.txt').read_bytes(), b'edit')
        self.assertEqual((restored / 'report.neko-20261022T120000Z.txt').read_bytes(), b'a file of its own')

    def test_an_undone_edit_restores_the_content_the_backup_last_saw(self):
        # AAAA -> BBBB -> AAAA: the backup kept BBBB as a dated version and noted that
        # the original is current again; "newest only" must restore AAAA.
        usb = self.root / 'usb'
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'a.txt', b'AAAA')
        self.write(files + 'a.neko-20261001T090000Z.txt', b'BBBB')
        self.write(files + '.a.txt.neko-latest', b'a.txt')
        self.write(files + 'b.txt', b'one')
        self.write(files + 'b.neko-20261001T090000Z.txt', b'two')
        self.write(files + 'b.neko-20261002T090000Z.txt', b'three')
        self.write(files + '.b.txt.neko-latest', b'b.neko-20261001T090000Z.txt')
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
            mock.patch.object(recovery_wizard, 'stamp', side_effect=['1', '2']),
        ):
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), lambda s: None)
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'no'), lambda s: None)
        latest, every = sorted((self.media / 'restored').iterdir())
        self.assertEqual((latest / 'files/a.txt').read_bytes(), b'AAAA')
        self.assertEqual((latest / 'files/b.txt').read_bytes(), b'two')
        # Versions from before 0.28.7 that no note or record ties to their file come back
        # as files of their own: an extra file, never a lost one.
        self.assertEqual(
            sorted(p.name for p in (latest / 'files').iterdir()),
            ['a.neko-20261001T090000Z.txt', 'a.txt', 'b.neko-20261002T090000Z.txt', 'b.txt'],
        )
        # Copying every version leaves the backup's notes out.
        self.assertFalse(any(p.name.endswith('.neko-latest') for p in (every / 'files').iterdir()))

    def ledger(self, rel_root, files):
        import hashlib as h

        entries = {
            key: {'sha256': h.sha256(body).hexdigest(), 'size': len(body), 'verified_at': 1.0, 'how': 'copied'}
            for key, body in files.items()
        }
        self.write(
            rel_root + '/.neko-ledger.json', json.dumps({'format': 'neko-ledger-v1', 'entries': entries}).encode()
        )

    def restore_cloud(self, *choices):
        usb = self.root / 'usb'
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_cloud(self.config, answers(*choices), out.append)
        return out

    def test_restored_files_are_checked_against_their_recorded_checksums(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept safe')
        self.write('usb/nextcloud/alice/files/old.txt', b'from before checksums')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept safe'})
        out = self.restore_cloud('1', 'yes', 'yes')
        restored = next((self.media / 'restored').iterdir()) / 'files'
        self.assertEqual((restored / 'a.txt').read_bytes(), b'kept safe')
        self.assertIn('Checksums matched for 1 file; 1 file had no recorded checksum yet', '\n'.join(out))

    def test_a_damaged_backup_file_is_left_out_and_named_and_the_rest_restored(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept safe')
        self.write('usb/nextcloud/alice/files/b.txt', b'also safe')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept safe', 'alice/files/b.txt': b'also safe'})
        self.write('usb/nextcloud/alice/files/a.txt', b'kept s?fe')  # bit rot on the USB drive
        out = '\n'.join(self.restore_cloud('1', 'yes', 'yes'))
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'files/b.txt').read_bytes(), b'also safe')
        self.assertFalse((restored / 'files/a.txt').exists(), 'a damaged copy is never left in the restore')
        self.assertIn('NOT restored (1 file)', out)
        self.assertIn('Done: 1 file copied', out)
        self.assertIn('damaged: alice/files/a.txt', (restored / 'NOT-RESTORED.txt').read_text())

    def test_a_file_missing_from_the_backup_is_asked_about_then_named(self):
        self.write('usb/nextcloud/alice/files/b.txt', b'also safe')
        self.ledger('usb/nextcloud', {'alice/files/b.txt': b'also safe', 'alice/files/gone.txt': b'x'})
        # "no": nothing is restored.
        with self.assertRaisesRegex(RuntimeError, 'Nothing was restored'):
            self.restore_cloud('1', 'yes', 'no')
        # "yes": the rest comes back and the missing one is listed.
        out = '\n'.join(self.restore_cloud('1', 'yes', 'yes', 'yes'))
        restored = next(p for p in (self.media / 'restored').iterdir() if (p / 'files').is_dir())
        self.assertEqual((restored / 'files/b.txt').read_bytes(), b'also safe')
        self.assertIn('missing: alice/files/gone.txt', (restored / 'NOT-RESTORED.txt').read_text())
        self.assertIn('NOT restored (1 file)', out)

    def test_an_unreadable_checksum_list_does_not_stop_the_restore(self):
        self.write('usb/nextcloud/alice/files/b.txt', b'also safe')
        self.write('usb/nextcloud/.neko-ledger.json', b'{"format": "neko-ledg')  # cut off mid-write
        out = '\n'.join(self.restore_cloud('1', 'yes', 'yes'))
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'files/b.txt').read_bytes(), b'also safe')
        self.assertIn('checksum list could not be used', out)

    def test_an_unreadable_or_odd_backup_entry_stops_the_restore(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'a')
        os.symlink('/etc/passwd', self.root / 'usb/nextcloud/alice/files/escape')
        with self.assertRaisesRegex(RuntimeError, 'could not be read'):
            self.restore_cloud('1', 'yes', 'no')
        self.assertFalse((self.media / 'restored').exists() and any((self.media / 'restored').iterdir()))

    def test_a_ledger_with_an_unsafe_path_is_not_trusted(self):
        from neko_common import read_ledger

        self.write(
            'usb/nextcloud/.neko-ledger.json',
            json.dumps(
                {'format': 'neko-ledger-v1', 'entries': {'../../etc/shadow': {'sha256': 'a' * 64, 'size': 1}}}
            ).encode(),
        )
        with self.assertRaisesRegex(RuntimeError, 'not trusted'):
            read_ledger(self.root / 'usb/nextcloud')

    def test_repair_fixes_each_case_from_a_copy_that_matches_its_checksum(self):
        import hashlib as h

        good = {k: f'good {k}'.encode() for k in 'abcdef'}
        archive, usb = 'hdd/photos/archive', 'usb'
        for k in 'abcf':  # recorded checksums
            self.write(f'{archive}/upload/{k}.jpg', good[k])
            self.write(f'{usb}/upload/{k}.jpg', good[k])
        self.ledger(archive, {f'upload/{k}.jpg': good[k] for k in 'abcf'})
        self.ledger(usb, {f'upload/{k}.jpg': good[k] for k in 'abcf'})
        self.write(f'{usb}/upload/a.jpg', b'rotten a')  # USB damaged
        self.write(f'{archive}/upload/b.jpg', b'rotten b')  # HDD damaged
        self.write(f'{archive}/upload/c.jpg', b'rot1 c')  # both damaged...
        self.write(f'{usb}/upload/c.jpg', b'rot2 c')
        self.write('ssd/photos/immich-library/upload/c.jpg', good['c'])  # ...Immich still has it
        self.write(f'{archive}/upload/d.jpg', b'hdd d')  # no checksum, copies differ,
        self.write(f'{usb}/upload/d.jpg', good['d'])  # the original decides
        self.write('ssd/photos/immich-library/upload/d.jpg', good['d'])
        self.write(f'{archive}/upload/e.jpg', b'hdd e')  # no checksum, no original:
        self.write(f'{usb}/upload/e.jpg', good['e'])  # you choose (USB)
        self.write(f'{archive}/upload/f.jpg', b'rot1 f')  # nothing good left
        self.write(f'{usb}/upload/f.jpg', b'rot2 f')
        keys = {
            'damaged': ['upload/a.jpg', 'upload/b.jpg'],
            'both_damaged': ['upload/c.jpg', 'upload/f.jpg'],
            'disagree': ['upload/d.jpg', 'upload/e.jpg'],
        }
        self.write(
            'ssd/reports/integrity.json',
            json.dumps(
                {
                    'format': 'neko-integrity-v1',
                    'sections': {'usb_photos': {**keys, 'counts': {k: len(v) for k, v in keys.items()}}},
                }
            ).encode(),
        )
        out = []
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            recovery_wizard.repair_backups(self.config, answers('2', '1'), out.append)  # f: keep it as it is
        for k in 'abcde':
            for where in (archive, usb):
                self.assertEqual((self.root / where / f'upload/{k}.jpg').read_bytes(), good[k], (k, where))
        self.assertEqual((self.root / archive / 'upload/f.jpg').read_bytes(), b'rot1 f')
        self.assertEqual((self.root / usb / 'upload/f.jpg').read_bytes(), b'rot2 f')
        text = '\n'.join(out)
        self.assertIn('upload/f.jpg: no copy matches its recorded checksum', text)
        # Damaged copies are kept aside, never deleted.
        aside = next((self.root / archive / 'neko-damaged').iterdir())
        self.assertEqual((aside / 'upload/b.jpg').read_bytes(), b'rotten b')
        # The chosen checksums were recorded; the recorded ones were never changed.
        from neko_common import read_ledger

        ledger = read_ledger(self.root / archive)
        self.assertEqual(ledger['upload/e.jpg'], h.sha256(good['e']).hexdigest())
        self.assertEqual(ledger['upload/b.jpg'], h.sha256(good['b']).hexdigest())
        # Only the file that could not be repaired is still listed for the dashboard.
        left = json.loads((self.root / 'ssd/reports/integrity.json').read_text())['sections']['usb_photos']
        self.assertEqual([left[k] for k in ('damaged', 'both_damaged', 'disagree')], [[], ['upload/f.jpg'], []])
        self.assertEqual(left['counts'], {'damaged': 0, 'both_damaged': 1, 'disagree': 0, 'missing': 0})

    def report(self, sections):
        self.write(
            'ssd/reports/integrity.json',
            json.dumps(
                {
                    'format': 'neko-integrity-v1',
                    'sections': {
                        name: {**lists, 'counts': {k: len(v) for k, v in lists.items()}}
                        for name, lists in sections.items()
                    },
                }
            ).encode(),
        )

    def repair(self, usb, *choices):
        out = []
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            recovery_wizard.repair_backups(self.config, answers(*choices), out.append)
        return out, json.loads((self.root / 'ssd/reports/integrity.json').read_text())['sections']

    def test_a_usb_warning_stays_until_the_usb_copy_was_really_checked(self):
        archive = 'hdd/photos/archive'
        self.write(f'{archive}/upload/a.jpg', b'good a')
        self.ledger(archive, {'upload/a.jpg': b'good a'})
        self.report({'archive': {'damaged': ['upload/a.jpg']}, 'usb_photos': {'damaged': ['upload/a.jpg']}})
        out, left = self.repair(None)  # USB drive not plugged in
        self.assertIn('not plugged in', '\n'.join(out))
        self.assertEqual(left['archive']['damaged'], [])  # HDD copy checked: intact
        self.assertEqual(left['usb_photos']['damaged'], ['upload/a.jpg'])  # USB never checked

    def test_a_missing_archive_file_is_restored_from_the_usb_copy(self):
        archive, usb = 'hdd/photos/archive', 'usb'
        self.write(f'{usb}/upload/a.jpg', b'good a')
        self.ledger(archive, {'upload/a.jpg': b'good a'})
        self.ledger(usb, {'upload/a.jpg': b'good a'})
        self.report({'usb_photos': {'missing': ['upload/a.jpg']}})
        out, left = self.repair(self.root / usb)
        self.assertEqual((self.root / archive / 'upload/a.jpg').read_bytes(), b'good a')
        self.assertEqual(left['usb_photos']['missing'], [])

    # Owner review of 0.40.2, finding 1: with the HDD unplugged, "Repair damaged backup
    # copies" wrote the HDD's copies into its empty folder on the system disk, said
    # "repaired the HDD copy" and cleared the warning.
    HDD = MappingProxyType({'uuid': 'hdd-uuid', 'fstype': 'ext4'})  # read-only: shared by every test

    def hdd_copies_missing(self, *names, hdd=None):
        """The HDD archive lacks these photos; the USB drive has them, checked."""
        files = {f'upload/{name}': f'good {name}'.encode() for name in names}
        for key, body in files.items():
            self.write(f'usb/{key}', body)
        self.ledger('usb', files)
        self.report({'archive': {'missing': sorted(files)}, 'usb_photos': {'missing': sorted(files)}})
        self.config['storage'] = {'media_hdd': dict(hdd or self.HDD)}
        return sorted(files)

    def repair_with_hdd(self, mounted):
        """Repair with the real HDD check, findmnt saying `mounted` (UUID FSTYPE SOURCE, or
        None for nothing) is at the HDD's folder."""
        with (
            mock.patch.object(recovery_wizard, 'hdd_ready', REAL_HDD_READY),
            mock.patch('neko_common.findmnt', return_value=mounted),
        ):
            return self.repair(self.root / 'usb')

    def files_under(self, folder):
        return sorted((str(p.relative_to(folder)), p.read_bytes()) for p in Path(folder).rglob('*') if p.is_file())

    def assert_hdd_copy_not_repaired(self, out, left, keys, why):
        said = '\n'.join(out)
        for key in keys:
            self.assertIn(f'{key}: already intact; the HDD copy was NOT repaired: {why}', said)
        self.assertNotIn('repaired the HDD', said)
        self.assertEqual(left['archive']['missing'], keys)  # the HDD warnings stay
        self.assertEqual(left['usb_photos']['missing'], keys)

    def test_repair_never_writes_under_a_missing_hdd(self):
        keys = self.hdd_copies_missing('a.jpg', 'b.jpg')
        out, left = self.repair_with_hdd(None)  # nothing is mounted at the HDD's folder
        self.assertEqual(self.files_under(self.media), [], 'nothing may land under the HDD mount folder')
        self.assert_hdd_copy_not_repaired(out, left, keys, f'the HDD is not mounted at {self.media}')
        self.assertIn('Its copies were NOT repaired, and their warnings stay listed', '\n'.join(out))

    def test_repair_never_writes_onto_another_drive_at_the_hdd_folder(self):
        keys = self.hdd_copies_missing('a.jpg', hdd={'uuid': 'hdd-uuid', 'fstype': 'btrfs', 'subvolume': '/@media'})
        self.write('hdd/photos/archive/upload/a.jpg', b'what the other drive holds')
        before = self.files_under(self.media)
        for mounted in (
            'other-uuid btrfs /dev/sdz1[/@media]',  # another drive
            'hdd-uuid ext4 /dev/sdb1',  # the right UUID, another filesystem
            'hdd-uuid btrfs /dev/sdb1[/@snapshots]',
        ):  # the right drive, another subvolume
            out, left = self.repair_with_hdd(mounted)
            self.assertEqual(self.files_under(self.media), before, mounted)
            self.assert_hdd_copy_not_repaired(out, left, keys, 'the HDD is not mounted at')

    def unplugged_during(self, *, keep_identity):
        """Repair two missing HDD copies; the HDD is unplugged while the first is written:
        its mount folder (now on the system disk) is empty again. With `keep_identity`,
        the drive check still says yes (it can't tell in time); otherwise it says no."""
        keys = self.hdd_copies_missing('a.jpg', 'b.jpg')
        unplugged = self.root / 'hdd-unplugged'
        real, calls = recovery_wizard.copy_checked_into, []

        def writing(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 1 + keep_identity and not unplugged.exists():
                self.media.rename(unplugged)
                self.media.mkdir()
            return real(*args, **kwargs)

        ready = (lambda config: True) if keep_identity else (lambda config: not unplugged.exists())
        with (
            mock.patch.object(recovery_wizard, 'copy_checked_into', side_effect=writing),
            mock.patch.object(recovery_wizard, 'hdd_ready', side_effect=ready),
        ):
            out, left = self.repair(self.root / 'usb')
        self.assertEqual(self.files_under(self.media), [], 'nothing may land under the HDD mount folder')
        return keys, unplugged, '\n'.join(out), left

    def test_an_hdd_unplugged_mid_repair_gets_nothing_more(self):
        keys, unplugged, said, left = self.unplugged_during(keep_identity=False)
        # a.jpg reached the HDD before it went; b.jpg was never written anywhere.
        self.assertEqual(self.files_under(unplugged / 'photos/archive/upload'), [('a.jpg', b'good a.jpg')])
        self.assertIn(
            'upload/a.jpg: the HDD copy was written, but its checksum was NOT recorded (the HDD was '
            'disconnected or changed during the repair), so it stays listed',
            said,
        )
        self.assertIn(
            'upload/b.jpg: the HDD copy was NOT repaired: the HDD was disconnected or changed during the repair', said
        )
        self.assertEqual(left['archive']['missing'], keys)

    def test_a_write_racing_an_unplug_lands_on_the_drive_it_was_opened_on(self):
        # The drive goes between the last check and the write: the file is written through
        # the HDD's open folder, so it lands on the HDD, never in the empty folder.
        _keys, unplugged, said, left = self.unplugged_during(keep_identity=True)
        self.assertEqual(
            self.files_under(unplugged / 'photos/archive/upload'), [('a.jpg', b'good a.jpg'), ('b.jpg', b'good b.jpg')]
        )
        self.assertIn('upload/b.jpg: the HDD copy was written, but its checksum was NOT recorded', said)
        self.assertEqual(left['archive']['missing'], ['upload/b.jpg'])

    def test_a_folder_of_another_drive_inside_the_archive_is_never_written_through(self):
        keys = self.hdd_copies_missing('a.jpg')
        (self.media / 'photos/archive').mkdir(parents=True)
        real = os.fstat
        inside = (self.media / 'photos/archive').stat().st_ino

        def fstat(fd):  # the archive folder is another filesystem mounted inside the HDD
            found = real(fd)
            return (
                os.stat_result((found.st_mode, found.st_ino, found.st_dev + 1, *found[3:]))
                if found.st_ino == inside
                else found
            )

        with mock.patch.object(recovery_wizard.os, 'fstat', side_effect=fstat):
            out, left = self.repair(self.root / 'usb')
        self.assertEqual(self.files_under(self.media), [])
        self.assertIn('the HDD copy was NOT repaired: "archive" in the HDD is on another drive', '\n'.join(out))
        self.assertEqual(left['archive']['missing'], keys)

    # Owner review of 0.40.2, finding 2: a copy a restore found missing never reached
    # the repair option ("The last checksum checks found nothing to repair").
    def test_a_copy_missing_at_a_restore_is_offered_by_the_repair(self):
        import hashlib

        self.write('hdd/photos/archive/upload/u/kept.jpg', b'kept')
        self.ledger('hdd/photos/archive', {'upload/u/kept.jpg': b'kept', 'upload/u/missing.jpg': b'gone from the HDD'})
        self.write('usb/upload/u/missing.jpg', b'gone from the HDD')
        self.ledger('usb', {'upload/u/missing.jpg': b'gone from the HDD'})
        (self.root / 'ssd/reports').mkdir()
        out = []
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / 'usb'),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            recovery_wizard.restore_photos(self.config, answers('1', 'yes', 'yes', 'yes'), out.append)
        self.assertIn('now listed for this menu\'s "Repair damaged backup copies"', '\n'.join(out))
        archive = recovery_wizard.integrity_sections(self.config)['archive']
        self.assertEqual((archive['missing'], archive['counts']['missing']), (['upload/u/missing.jpg'], 1))
        self.assertEqual(
            archive['checksums'], {'upload/u/missing.jpg': hashlib.sha256(b'gone from the HDD').hexdigest()}
        )
        repaired, left = self.repair(self.root / 'usb')
        self.assertIn('upload/u/missing.jpg: repaired the HDD copy', '\n'.join(repaired))
        self.assertEqual((self.media / 'photos/archive/upload/u/missing.jpg').read_bytes(), b'gone from the HDD')
        self.assertEqual(left['archive']['missing'], [])

    def test_a_missing_copy_is_listed_for_repair_even_when_you_stop_the_restore(self):
        import hashlib

        self.write('usb/nextcloud/alice/files/b.txt', b'b')
        self.ledger('usb/nextcloud', {'alice/files/b.txt': b'b', 'alice/files/gone.txt': b'was here'})
        (self.root / 'ssd/reports').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'Nothing was restored'):
            self.restore_cloud('1', 'yes', 'no')
        cloud = recovery_wizard.integrity_sections(self.config)['usb_nextcloud']
        self.assertEqual(cloud['missing'], ['alice/files/gone.txt'])
        self.assertEqual(cloud['checksums']['alice/files/gone.txt'], hashlib.sha256(b'was here').hexdigest())

    def test_a_restore_as_of_a_backup_lists_the_saved_copy_that_is_missing(self):
        # The list names the file; what is missing (and what the repair fixes) is its
        # saved copy: here a dated version, which the live file still matches.
        import hashlib

        dated = 'doc.neko-20261020T010000Z.txt'
        sha = hashlib.sha256(b'version two').hexdigest()
        self.write('usb/nextcloud/alice/files/doc.txt', b'version one')
        self.write('usb/nextcloud/alice/files/.doc.txt.neko-latest', dated.encode())
        self.ledger('usb/nextcloud', {'alice/files/doc.txt': b'version one', f'alice/files/{dated}': b'version two'})
        self.write(
            'usb/nextcloud/.neko-inventory/20261101T030000Z.json.gz',
            gzip.compress(
                json.dumps(
                    {'format': 'neko-inventory-v2', 'files': {'alice/files/doc.txt': {'copy': dated, 'sha256': sha}}}
                ).encode()
            ),
        )
        self.write('hdd/cloud/files/alice/files/doc.txt', b'version two')  # Nextcloud still has it
        (self.root / 'ssd/reports').mkdir()
        self.restore_cloud('1', 'yes', '1', 'yes')
        cloud = recovery_wizard.integrity_sections(self.config)['usb_nextcloud']
        self.assertEqual(
            (cloud['missing'], cloud['checksums']), ([f'alice/files/{dated}'], {f'alice/files/{dated}': sha})
        )
        out, left = self.repair(self.root / 'usb')
        self.assertIn(f'alice/files/{dated}: repaired the USB copy', '\n'.join(out))
        self.assertEqual((self.root / 'usb/nextcloud/alice/files' / dated).read_bytes(), b'version two')
        self.assertEqual(left['usb_nextcloud']['missing'], [])

    def disagreeing(self):
        archive, usb = 'hdd/photos/archive', 'usb'
        self.write(f'{archive}/upload/a.jpg', b'hdd version')
        self.write(f'{usb}/upload/a.jpg', b'usb version')
        # Each copy matches its own ledger, and the ledgers disagree.
        self.ledger(archive, {'upload/a.jpg': b'hdd version'})
        self.ledger(usb, {'upload/a.jpg': b'usb version'})
        self.report({'usb_photos': {'disagree': ['upload/a.jpg']}})
        return self.root / archive, self.root / usb

    def test_ledgers_that_disagree_change_nothing_until_you_choose(self):
        hdd, usb = self.disagreeing()
        out, left = self.repair(usb, '3')  # Keep both as they are
        self.assertEqual((hdd / 'upload/a.jpg').read_bytes(), b'hdd version')
        self.assertEqual((usb / 'upload/a.jpg').read_bytes(), b'usb version')
        self.assertIn('both copies were kept', '\n'.join(out))
        self.assertEqual(left['usb_photos']['disagree'], ['upload/a.jpg'])
        self.assertNotEqual(recovery_wizard.read_ledger(hdd), recovery_wizard.read_ledger(usb))

    def test_the_copy_you_choose_wins_in_both_ledgers(self):
        import hashlib

        hdd, usb = self.disagreeing()
        out, left = self.repair(usb, '2')  # the USB copy
        self.assertIn('matches the USB list', '\n'.join(out))
        self.assertEqual((hdd / 'upload/a.jpg').read_bytes(), b'usb version')
        good = hashlib.sha256(b'usb version').hexdigest()
        # Both ledgers now agree with the files: neither copy fails its own check.
        self.assertEqual(recovery_wizard.read_ledger(hdd)['upload/a.jpg'], good)
        self.assertEqual(recovery_wizard.read_ledger(usb)['upload/a.jpg'], good)
        self.assertEqual(left['usb_photos']['disagree'], [])
        kept = list((hdd / 'neko-damaged').rglob('a.jpg'))
        self.assertEqual([k.read_bytes() for k in kept], [b'hdd version'])  # set aside, not deleted

    def test_nightly_restores_keep_owners_and_say_when_steam_is_missing(self):
        import json as json_module

        nights = self.media / 'backups/host-user'
        for stamp, left_out in (('1792900000', ['steam']), ('1792800000', [])):
            run = nights / f'snapshot-{stamp}-1'
            run.mkdir(parents=True)
            (run / 'manifest.json').write_text(
                json_module.dumps({'format': 'neko-host-snapshot-v1', 'left_out': left_out})
            )
        out, calls = [], []
        with mock.patch.object(recovery_wizard, 'tool', side_effect=lambda *a: calls.append(a) or True):
            recovery_wizard.restore_nightly(self.config, 'host', answers('1', 'yes'), out.append)
        said = '\n'.join(out)
        self.assertIn('(no Steam files: Steam was open)', said.split('2.')[0])  # the newest night is marked
        self.assertNotIn('(no Steam files', said.split('2.')[1].split('0. Back')[0])
        self.assertIn('--preserve-owners', calls[0])
        self.assertIn('choose another night for those', said)
        self.assertIn('sudo cp -a', said)
        # IT-16: what the sourceN folders are, and where that is written down.
        self.assertIn('named source0, source1 and so on', said)
        self.assertIn('restore-report.json says which original folder each one is', said)

    def test_the_top_menu_quits_and_ends_with_the_repair_choice(self):
        out = []
        with (
            mock.patch.object(os, 'geteuid', return_value=0),
            mock.patch.object(recovery_wizard, 'settings', return_value=self.config),
        ):
            self.assertEqual(recovery_wizard.run(answers('0'), out.append), 0)
        said = '\n'.join(out)
        self.assertIn('  0. Quit', said)
        self.assertNotIn('0. Back', said)
        self.assertEqual(recovery_wizard.MENU[-1][0], 'repair')
        self.assertNotIn('v0.21.1', said)  # the old USB snapshots' review is gone

    def test_the_monthly_restore_says_where_the_password_files_are(self):
        import json as json_module

        for kept, expect in (('unencrypted_in_data_tar', 'are in it, unencrypted'), ('sealed', 'App password list')):
            month = self.media / 'backups/monthly' / f'1792900000-{len(kept)}'
            month.mkdir(parents=True)
            (month / 'manifest.json').write_text(json_module.dumps({'password_files': kept}))
            (month / 'restore-tests.json').write_text('{}')
            out = []
            with mock.patch.object(recovery_wizard, 'tool', return_value=True):
                recovery_wizard.restore_monthly(self.config, answers('1', 'yes', 'no'), out.append)
            self.assertIn(expect, '\n'.join(out))
            for path in sorted(month.parent.iterdir()):
                (path / 'manifest.json').unlink()
                (path / 'restore-tests.json').unlink()
                path.rmdir()

    def test_a_restore_stops_when_a_verified_file_is_missing(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept')
        self.ledger(
            'usb/nextcloud',
            {'alice/files/a.txt': b'kept', 'alice/files/gone.txt': b'was here', 'bob/files/other.txt': b'not restored'},
        )
        shown = []
        usb = self.root / 'usb'
        restore = mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb)
        done = mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None)
        replies = answers('1', 'yes', 'no')
        with restore, done, self.assertRaisesRegex(RuntimeError, 'Nothing was restored'):
            recovery_wizard.restore_cloud(self.config, replies, shown.append)
        said = '\n'.join(shown)
        self.assertIn(
            'Missing from this backup, though backed up and checked before: 1 file', said
        )  # bob's file is not counted
        self.assertIn('alice/files/gone.txt', said)
        restored = self.media / 'restored'
        self.assertEqual(list(restored.iterdir()) if restored.exists() else [], [])

    def test_a_dated_version_is_repaired_from_its_live_original(self):
        archive = 'hdd/photos/archive'
        dated = 'upload/photo.neko-20261020T010000Z.jpg'
        self.write(f'{archive}/{dated}', b'rotten')
        self.write(f'{archive}/upload/.photo.jpg.neko-latest', dated.split('/')[1].encode())
        self.ledger(archive, {dated: b'photo as it is now'})
        self.write('ssd/photos/immich-library/upload/photo.jpg', b'photo as it is now')
        self.report({'archive': {'damaged': [dated]}})
        out, left = self.repair(None)
        self.assertEqual((self.root / archive / dated).read_bytes(), b'photo as it is now')
        self.assertEqual(left['archive']['damaged'], [])
        self.assertEqual(recovery_wizard.live_name('a/b.neko-20261020T010000Z-2.jpg'), 'a/b.jpg')
        self.assertEqual(recovery_wizard.live_name('a/plain.jpg'), 'a/plain.jpg')

    def test_a_dated_version_never_takes_a_later_live_edit(self):
        archive = 'hdd/photos/archive'
        dated = 'upload/photo.neko-20261020T010000Z.jpg'
        self.write(f'{archive}/{dated}', b'old version')
        self.ledger(archive, {dated: b'old version'})
        self.write('ssd/photos/immich-library/upload/photo.jpg', b'edited since')
        self.write(f'{archive}/{dated}', b'rotten')
        self.report({'archive': {'damaged': [dated]}})
        out, left = self.repair(None)
        self.assertEqual((self.root / archive / dated).read_bytes(), b'rotten')  # kept untouched
        self.assertIn('no copy matches its recorded checksum', '\n'.join(out))
        self.assertEqual(left['archive']['damaged'], [dated])

    def unrepairable_cloud_copy(self):
        """The USB copy (the only one) of report.txt rotted, and the file was edited since;
        gone.txt's copy disappeared after the file was deleted."""
        tree = 'usb/nextcloud'
        self.write(f'{tree}/alice/files/report.txt', b'versiXn one')
        self.ledger(tree, {'alice/files/report.txt': b'version one', 'alice/files/gone.txt': b'deleted'})
        self.write('hdd/cloud/files/alice/files/report.txt', b'version two')
        self.report({'usb_nextcloud': {'damaged': ['alice/files/report.txt'], 'missing': ['alice/files/gone.txt']}})
        return self.root / tree

    def test_an_unrepairable_copy_is_kept_until_you_accept_it_as_lost(self):
        tree = self.unrepairable_cloud_copy()
        out, left = self.repair(self.root / 'usb', '1', '1')  # keep both as they are
        said = '\n'.join(out)
        self.assertIn('the monthly snapshot and updates wait', said)
        self.assertIn('Accept that this saved copy is lost', said)
        self.assertEqual((tree / 'alice/files/report.txt').read_bytes(), b'versiXn one')
        self.assertEqual(left['usb_nextcloud']['damaged'], ['alice/files/report.txt'])
        self.assertIn('alice/files/report.txt', recovery_wizard.read_ledger(tree))

    def test_an_unrepairable_copy_accepted_as_lost_is_kept_aside_and_retired(self):
        tree = self.unrepairable_cloud_copy()
        out, left = self.repair(self.root / 'usb', '2', '2')
        said = '\n'.join(out)
        self.assertIn('alice/files/gone.txt: the saved copy is missing', said)
        self.assertIn('alice/files/report.txt: accepted as lost (the damaged copy is kept in neko-damaged/', said)
        aside = next((tree / 'neko-damaged').iterdir())
        self.assertEqual((aside / 'alice/files/report.txt').read_bytes(), b'versiXn one')
        self.assertFalse((tree / 'alice/files/report.txt').exists())
        # Out of the checksum list, with a record of why; the dashboard warning is cleared.
        self.assertEqual(recovery_wizard.read_ledger(tree), {})
        saved = json.loads((tree / '.neko-ledger.json').read_text())
        self.assertIn('accepted as lost', saved['retired']['alice/files/report.txt']['why'])
        self.assertTrue(saved['retired']['alice/files/gone.txt']['why'].startswith('missing'))
        self.assertEqual((left['usb_nextcloud']['damaged'], left['usb_nextcloud']['missing']), ([], []))

    def test_a_photo_copy_is_never_given_up_while_the_usb_drive_is_away(self):
        archive = 'hdd/photos/archive'
        self.write(f'{archive}/upload/a.jpg', b'rotten')
        self.ledger(archive, {'upload/a.jpg': b'good a'})
        self.write('ssd/photos/immich-library/upload/a.jpg', b'edited since')
        self.report({'archive': {'damaged': ['upload/a.jpg']}})
        out, _ = self.repair(None)  # no answer is asked for
        self.assertNotIn('Accept that this saved copy is lost', '\n'.join(out))
        self.assertEqual((self.root / archive / 'upload/a.jpg').read_bytes(), b'rotten')

    def test_a_restore_never_writes_onto_the_system_disk_when_the_hdd_is_missing(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept')
        elsewhere = self.root / 'other-drive'
        elsewhere.mkdir()
        shown = []
        with (
            mock.patch.object(recovery_wizard, 'hdd_ready', return_value=False),
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / 'usb'),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            # A folder typed under the HDD's empty folder is refused too; then one elsewhere.
            recovery_wizard.restore_cloud(
                self.config,
                answers(
                    '1', str(self.media / 'restored-here'), 'yes', str(elsewhere / 'files-back'), 'yes', '1', 'yes'
                ),
                shown.append,
            )
        said = '\n'.join(shown)
        self.assertIn('The HDD is not mounted at', said)
        self.assertIn('that folder would be on the system disk', said)
        self.assertFalse((self.media / 'restored').exists())
        self.assertFalse((self.media / 'restored-here').exists())
        self.assertEqual((elsewhere / 'files-back/files/a.txt').read_bytes(), b'kept')

    def damaged_on_the_usb(self):
        """Nextcloud's USB copy (its only copy) of a.txt rotted; b.txt is fine."""
        self.write('usb/nextcloud/alice/files/a.txt', b'kept safe')
        self.write('usb/nextcloud/alice/files/b.txt', b'also safe')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept safe', 'alice/files/b.txt': b'also safe'})
        self.write('usb/nextcloud/alice/files/a.txt', b'kept s?fe')

    def test_damage_a_restore_finds_is_what_the_repair_option_then_works_on(self):
        # UP-08: the restore said "try the repair option", and repair found nothing.
        self.damaged_on_the_usb()
        (self.root / 'ssd/reports').mkdir()
        with mock.patch.object(recovery_wizard, 'ssd_ready', return_value=True):
            out = '\n'.join(self.restore_cloud('1', 'yes', 'yes'))
        self.assertIn('now listed for this menu\'s "Repair damaged backup copies"', out)
        # Nextcloud's USB copy is its only backup: no "other backup" is promised.
        self.assertNotIn('other backup', out)
        sections = recovery_wizard.integrity_sections(self.config)
        self.assertEqual(recovery_wizard.listed(sections['usb_nextcloud']), ['alice/files/a.txt'])
        self.assertEqual(sections['usb_nextcloud']['counts']['damaged'], 1)
        repaired, _ = self.repair(self.root / 'usb', '1')
        self.assertNotIn('nothing to repair', '\n'.join(repaired))
        self.assertIn('alice/files/a.txt', '\n'.join(repaired))

    def test_a_damaged_copy_is_kept_as_it_is_beside_the_restore(self):
        # UP-09: a copy with a few bad bytes may still open in part; it is kept, apart.
        self.damaged_on_the_usb()
        out = '\n'.join(self.restore_cloud('1', 'yes', 'yes'))
        restored = next((self.media / 'restored').iterdir())
        self.assertFalse((restored / 'files/a.txt').exists())
        self.assertEqual((restored / 'damaged-as-is/files/a.txt').read_bytes(), b'kept s?fe')
        self.assertIn(f'kept as they are in {restored / "damaged-as-is"}', out)

    def test_a_copy_accepted_as_lost_is_named_not_asked_about_at_every_restore(self):
        # UP-09: after "accept that this saved copy is lost", a restore "as of" a backup
        # that listed it asked a scary question every time, as if it were new damage.
        tree = self.unrepairable_cloud_copy()
        self.write('usb/nextcloud/alice/files/b.txt', b'b')
        self.repair(self.root / 'usb', '2', '2')
        self.write(
            'usb/nextcloud/.neko-inventory/20261101T030000Z.json.gz',
            gzip.compress(
                json.dumps(
                    {
                        'format': 'neko-inventory-v1',
                        'files': {'alice/files/report.txt': 'report.txt', 'alice/files/b.txt': 'b.txt'},
                    }
                ).encode()
            ),
        )
        out = '\n'.join(self.restore_cloud('1', 'yes', '1'))  # no "Restore everything else?" asked
        self.assertNotIn('Restore everything else', out)
        aside = next((tree / 'neko-damaged').iterdir()).name
        self.assertIn(
            'Not restored, as you accepted them as lost in the repair menu: 1 file '
            '(for example alice/files/report.txt: damaged; kept in neko-damaged/',
            out,
        )
        self.assertIn(
            f'its damaged copy is still on the backup, as it is: {tree}/neko-damaged/{aside}/alice/files/report.txt',
            out,
        )
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'files/b.txt').read_bytes(), b'b')
        self.assertIn('accepted as lost: alice/files/report.txt', (restored / 'NOT-RESTORED.txt').read_text())
        # Given up on purpose: never listed for the repair option again as new damage.
        cloud = recovery_wizard.integrity_sections(self.config)['usb_nextcloud']
        self.assertEqual(recovery_wizard.listed(cloud), [])

    def test_restored_files_belong_to_the_person_who_restores_them(self):
        # UP-02: they were root's only, so "upload them in the browser" could not work.
        if os.geteuid() == 0 and chown_refused(self.root, 4321, 4322):
            self.skipTest('this system refuses to give files to the IDs 4321:4322 (no such IDs in its namespace)')
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        out = []
        with (
            mock.patch.dict(os.environ, {'SUDO_UID': '4321', 'SUDO_GID': '4322'}),
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
            mock.patch.object(recovery_wizard, 'reachable', return_value=True),
        ):
            recovery_wizard.restore_photos(self.config, answers('1', 'yes', 'yes'), out.append)
        said = '\n'.join(out)
        restored = next((self.media / 'restored').iterdir())
        if os.geteuid() != 0:
            # Not root (a test run as an ordinary account): it says how instead.
            self.assertIn(f'sudo chown -R 4321: "{restored}"', said)
            return
        for path in (restored, restored / 'upload', restored / 'upload/u1/a.jpg'):
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (4321, 4322), path)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o700)  # still private to that account
        parent = (self.media / 'restored').stat()
        self.assertEqual((parent.st_uid, parent.st_gid, parent.st_mode & 0o777), (0, 4322, 0o750))
        self.assertIn('now belong to 4321, so you can open them from your desktop', said)
        self.assertLess(said.index('belong to'), said.index('click "Upload"'))

    def test_a_restored_folder_out_of_the_person_s_reach_is_said_with_how_to_move_it(self):
        target = self.root / 'private/back'
        target.mkdir(parents=True)
        (self.root / 'private').chmod(0o700)
        with (
            mock.patch.object(recovery_wizard, 'give_to'),
            mock.patch.object(recovery_wizard, 'owner', return_value=(4321, 4322)),
        ):
            lines = recovery_wizard.handed_over({'media_mount': str(self.media)}, target)
        self.assertIn('can\'t get to where it is', lines[0])
        self.assertEqual(lines[1], f'  sudo mv "{target}" "/home/"')

    def test_restore_texts_say_what_happened(self):
        # UP-10, UP-12: wording that did not fit what happened.
        self.write('usb/nextcloud/alice/files/a.txt', b'a')
        self.write(
            'usb/nextcloud/.neko-inventory/20261101T030000Z.json.gz',
            gzip.compress(
                json.dumps({'format': 'neko-inventory-v1', 'files': {'alice/files/a.txt': 'a.txt'}}).encode()
            ),
        )
        out = '\n'.join(self.restore_cloud('1', 'yes', '1'))
        self.assertIn(
            'Checksums matched for 0 files; 1 file had no recorded checksum yet (the backup that '
            'copied it did not finish, or it was backed up before checksums were kept)',
            out,
        )
        self.assertIn('Done: 1 file copied', out)
        self.assertNotIn('replaced earlier', out)  # there is no such folder in this restore
        self.assertLess(out.index('Which files?'), out.index('Copying...'))
        stopped = []
        with (
            mock.patch.object(os, 'geteuid', return_value=0),
            mock.patch.object(recovery_wizard, 'settings', return_value=self.config),
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / 'usb'),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            recovery_wizard.run(then_stopped('2', '1', 'yes', '1'), stopped.append)
        self.assertIn('Stopped. Nothing more was changed; what was already restored', stopped[-1])

    def test_a_restore_made_again_at_once_gets_a_folder_of_its_own(self):
        # UP-13: the suggested folder was named to the minute, so a second restore within
        # that minute was refused ("That folder already exists").
        self.assertRegex(recovery_wizard.stamp(), r'^\d{8}-\d{6}$')
        first = self.media / 'restored/photos-20261026-0945'
        first.mkdir(parents=True)
        self.assertEqual(recovery_wizard.unused(first), first.with_name('photos-20261026-0945-2'))
        first.with_name('photos-20261026-0945-2').mkdir()
        self.assertEqual(recovery_wizard.unused(first), first.with_name('photos-20261026-0945-3'))

    def test_repair_with_nothing_reported(self):
        out = []
        recovery_wizard.repair_backups(self.config, answers(), out.append)
        self.assertIn('nothing to repair', out[0])

    def test_the_password_list_comes_from_the_usb_drive_when_the_hdd_has_none(self):
        # The SSD and the HDD both broke: the weekly copy's password list on the USB drive
        # (the newest sealed snapshot's files) is offered, and opened the usual way.
        usb = self.root / 'usb'
        self.write('usb/passwords/100-1/manifest.json', b'{"format":"neko-maintenance-v1"}')
        self.write('usb/passwords/100-1/secrets.tar.cms', b'sealed')
        restored = []
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
            mock.patch.object(recovery_wizard, 'tool', side_effect=lambda *a: restored.append(a) or True),
            mock.patch.object(recovery_wizard, 'new_folder', return_value=self.root / 'out'),
        ):
            out = []
            recovery_wizard.restore_passwords(self.config, answers('1'), out.append)
        said = '\n'.join(out)
        self.assertIn('looking on the USB drive', said)
        self.assertIn('(copy on the USB drive)', said)
        self.assertEqual(restored[0][:3], ('backup_secrets.py', '--restore', str(usb / 'passwords/100-1')))

    def test_no_password_list_anywhere_is_said(self):
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
        ):
            out = []
            recovery_wizard.restore_passwords(self.config, answers(), out.append)
        self.assertIn('No password list on the USB drive either', out[-1])

    def test_a_drive_that_fills_up_mid_restore_says_what_is_there(self):
        import errno

        self.write('src/a.txt', b'a')
        self.write('src/b.txt', b'b')
        full = OSError(errno.ENOSPC, 'No space left on device')
        checked = recovery_wizard.Checked(None)
        with mock.patch.object(recovery_wizard, 'copy_checked', side_effect=[None, full]):
            with self.assertRaisesRegex(RuntimeError, 'filled up after 1 of 2 files') as caught:
                recovery_wizard.copy_new([('src', self.root / 'src')], self.root / 'restored', checked)
        self.assertIn('run the restore again', str(caught.exception))

    def test_a_password_list_from_a_snapshot_without_a_backup_key(self):
        import tarfile

        source = self.root / 'ssd'
        self.write('ssd/secrets/accounts/APP-CREDENTIALS.txt', b'jellyfin: admin / pw')
        self.write('ssd/appdata/jellyfin/config.xml', b'<x/>')
        run = self.media / 'backups/monthly/100-1'
        run.mkdir(parents=True)
        with tarfile.open(run / 'data.tar', 'w') as archive:
            for rel in ('appdata/jellyfin', 'secrets/accounts'):
                archive.add(source / rel, arcname=rel)
        import hashlib

        digest = hashlib.sha256((run / 'data.tar').read_bytes()).hexdigest()
        (run / 'manifest.json').write_text(
            json.dumps(
                {
                    'format': 'neko-maintenance-v1',
                    'password_files': 'unencrypted_in_data_tar',
                    'paths': {'data': ['appdata/jellyfin', 'secrets/accounts'], 'media': []},
                    'sha256': {'data': digest},
                }
            )
        )
        # A changed archive is refused before anything is taken out of it.
        good = (run / 'data.tar').read_bytes()
        (run / 'data.tar').write_bytes(good.replace(b'admin / pw', b'admin / XX'))
        out = []
        recovery_wizard.restore_passwords(self.config, answers('1', 'no', str(self.root / 'bad'), 'yes'), out.append)
        self.assertIn('damaged (checksum mismatch)', '\n'.join(out))
        self.assertFalse((self.root / 'bad').exists())
        (run / 'data.tar').write_bytes(good)
        target = self.root / 'passwords'
        out = []
        recovery_wizard.restore_passwords(self.config, answers('1', 'no', str(target), 'yes'), out.append)
        self.assertIn('not encrypted', '\n'.join(out))
        self.assertEqual((target / 'secrets/accounts/APP-CREDENTIALS.txt').read_bytes(), b'jellyfin: admin / pw')
        self.assertFalse((target / 'appdata').exists())  # only the password list

    def test_menu_back_and_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(recovery_wizard.run(answers(), lambda s: None), 2)
        with (
            mock.patch('os.geteuid', return_value=0),
            mock.patch.object(recovery_wizard, 'settings', return_value=self.config),
        ):
            out = []
            self.assertEqual(recovery_wizard.run(answers('0'), out.append), 0)
            self.assertTrue(any('NEW folder' in line for line in out))


if __name__ == '__main__':
    unittest.main()
