"""Setup wizard, capacity check and recovery wizard."""
import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard  # noqa: E402
import wizard  # noqa: E402
from modules import capacity  # noqa: E402

GIB = 1024 ** 3


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

    def test_template_runs_tailscale_and_capacity_in_full(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        self.assertEqual(full[:4], ['system', 'tailscale', 'storage', 'capacity'])
        self.assertLess(full.index('tailscale'), full.index('gateway_tailnet'))


class WizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'config.json', self.root / 'config.json')
        geteuid = mock.patch('os.geteuid', return_value=0)
        geteuid.start()
        self.addCleanup(geteuid.stop)

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
        wizard.run(execute, self.root / 'state', prompt=answers('yes'), output=out.append, root=self.root)
        args = execute.call_args[0][0]
        self.assertEqual((args.profile, args.apply, args.config), ('full', True, str(self.root / 'my-config.json')))
        self.assertTrue(any('NekoServer is' in line for line in out))

    def test_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(wizard.run(mock.Mock(), self.root, prompt=answers(), output=lambda s: None,
                                        root=self.root), 2)


class FinishingScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'reports').mkdir()
        (self.data / 'reports/gateway-lan.json').write_text(json.dumps({'portal': 'https://192.168.1.20:8442/'}))
        (self.data / 'reports/gateway-tailnet.json').write_text(json.dumps(
            {'links': {'jellyfin': 'https://neko.tail1.ts.net:8443/'}}))
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
        self.assertIn('sudo cat', text)
        self.assertIn('--recover', text)

    def test_missing_or_unreadable_state_is_never_ready(self):
        complete, text = self.screen({})
        self.assertFalse(complete)
        self.assertNotIn('NekoServer is ready!', text)
        self.config['profiles'] = {'full': ['system', 'storage', 'media']}
        complete, text = self.screen({'system': {'status': 'success'}, 'storage': {'status': 'success'}})
        self.assertFalse(complete)
        self.assertIn('Movie, TV and music apps: did not run', text)
        complete, text = self.screen({'system': {'status': 'success'}, 'storage': {'status': 'success'},
                                      'media': {'status': 'success'}})
        self.assertTrue(complete)

    def test_pending_and_failed_steps_are_named_in_plain_words(self):
        complete, text = self.screen({
            'storage': {'status': 'success'},
            'tailscale': {'status': 'needs_setup', 'reason': 'Tailscale authorization pending'},
            'gateway_tailnet': {'status': 'blocked', 'reason': "Dependencies not successful: ['tailscale']"},
        })
        self.assertFalse(complete)
        self.assertIn('almost ready', text)
        self.assertIn('Tailscale: Tailscale authorization pending', text)
        self.assertIn('Access from anywhere and phone setup', text)
        self.assertIn('run  sudo bash install.sh  again', text)


class CapacityTests(unittest.TestCase):
    CONFIG = {'data_mount': '/ssd', 'media_mount': '/hdd', 'photos': {'external_backup_uuid': 'u'}}

    def assess(self, ssd, hdd, usb=None, photos=0, cloud=0, copied=0):
        free = {'/ssd': ssd * GIB, '/hdd': hdd * GIB}
        used = {'/ssd/photos/immich-library': photos * GIB, '/hdd/cloud/files': cloud * GIB}
        return capacity.assess(self.CONFIG, free=lambda p: free[str(p)], used=lambda p: used[str(p)],
                               usb_space=lambda uuid: None if usb is None else (usb * GIB, copied * GIB))

    def test_usb_counts_free_space_not_drive_size(self):
        # A big but nearly full drive warns; space already holding earlier copies counts.
        _, warn = self.assess(500, 4000, usb=10, photos=100)
        self.assertTrue(any('USB drive has 10 GB free' in w for w in warn))
        _, warn = self.assess(500, 4000, usb=30, photos=100, copied=100)
        self.assertEqual(warn, [])

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
        with mock.patch.object(capacity, 'assess', return_value=(['too small'], [])):
            with self.assertRaisesRegex(RuntimeError, 'too small'):
                capacity.run(ctx)
        with mock.patch.object(capacity, 'assess', return_value=([], ['low'])), \
                mock.patch('guided_setup.interactive', return_value=True):
            self.assertIsNone(capacity.run(Ctx({}), prompt=answers('yes'), output=lambda s: None))
            result = capacity.run(Ctx({}), prompt=answers('no'), output=lambda s: None)
            self.assertEqual(result['status'], 'needs_setup')


class RecoveryWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        self.config = {'media_mount': str(self.media), 'data_mount': str(self.root / 'ssd'),
                       'photos': {'archive_dir': 'photos/archive'}}

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
        chosen = recovery_wizard.new_folder(existing, answers('yes', 'relative', str(self.root / 'fresh'), 'yes'),
                                            out.append)
        self.assertEqual(chosen, self.root / 'fresh')
        self.assertTrue(any('already exists' in line for line in out))
        self.assertIsNone(recovery_wizard.new_folder(existing, answers('no', ''), out.append))

    def test_photos_restore_copies_into_a_new_folder_only(self):
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        self.write('hdd/photos/archive/library/b.heic', b'photo2')
        self.write('ssd/photos/immich-library/upload/live.jpg', b'live')
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_photos(self.config, answers('1', 'yes'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'upload/u1/a.jpg').read_bytes(), b'photo')
        self.assertEqual((restored / 'library/b.heic').read_bytes(), b'photo2')
        self.assertEqual((self.root / 'ssd/photos/immich-library/upload/live.jpg').read_bytes(), b'live')
        self.assertTrue(any('2 files copied' in line for line in out))
        self.assertEqual((self.media / 'restored').stat().st_mode & 0o777, 0o700)

    def test_nextcloud_restore_lists_users_from_the_usb(self):
        usb = self.root / 'usb'
        self.write('usb/nextcloud/alice/files/Docs/plan.odt', b'plan')
        self.write('usb/nextcloud/bob/files/todo.txt', b'todo')
        self.assertEqual(recovery_wizard.cloud_users(usb), ['alice', 'bob'])
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('2', 'yes'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertIn('nextcloud-bob', restored.name)
        self.assertEqual((restored / 'files/todo.txt').read_bytes(), b'todo')

    def test_nothing_found_is_explained(self):
        out = []
        recovery_wizard.restore_passwords(self.config, answers(), out.append)
        self.assertIn('No monthly snapshot', out[-1])

    def test_menu_back_and_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(recovery_wizard.run(answers(), lambda s: None), 2)
        with mock.patch('os.geteuid', return_value=0), \
                mock.patch.object(recovery_wizard, 'settings', return_value=self.config):
            out = []
            self.assertEqual(recovery_wizard.run(answers('0'), out.append), 0)
            self.assertTrue(any('NEW folder' in line for line in out))


if __name__ == '__main__':
    unittest.main()
