"""The setup driver: stopping with Ctrl+C, a settings file missing what it needs, and
the log kept from before the SSD was mounted; the boot-time drive lines."""
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup
import installer
from modules import app_backup, heartbeat, maintenance, notifications, storage

STEP = '''DEPENDENCIES = []
def run(ctx):
    raise KeyboardInterrupt
'''


class InstallerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'modules').mkdir()
        (self.root / 'modules' / 'stopper.py').write_text(STEP)
        config = json.loads((ROOT / 'config.json').read_text())
        for key in ('data_mount', 'media_mount', 'external_backup_mount'):
            config[key] = str(self.root / key)
        config['docker'] = {**config.get('docker', {}), 'persistent_root': config['data_mount']}
        self.config = self.root / 'settings.json'
        self.config.write_text(json.dumps(config))
        self.state = self.root / 'state'

    def args(self, **extra):
        return argparse.Namespace(**{'all': False, 'profile': None, 'module': 'stopper', 'retry_failed': False,
                                     'apply': True, 'check': False, 'dry_run': False,
                                     'config': str(self.config), 'state_dir': str(self.state), **extra})

    def test_ctrl_c_records_the_step_and_ends_without_a_traceback(self):
        with mock.patch('config_checks.validate'), mock.patch('builtins.print'), self.assertRaises(KeyboardInterrupt):
            installer._execute(self.args(), self.root)
        state = json.loads((self.state / 'state.json').read_text())
        self.assertEqual(state['stopper'], {'status': 'failed', 'reason': 'Stopped with Ctrl+C'})
        # The next run (or --retry-failed) starts that step again.
        printed = []
        with mock.patch.object(installer, 'execute', side_effect=KeyboardInterrupt), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: printed.append(' '.join(map(str, a)))):
            code = installer.main(['--module', 'stopper', '--config', str(self.config)])
        self.assertEqual(code, 130)
        self.assertIn('continues where it stopped', printed[-1])
        self.assertNotIn('--recover', printed[-1])
        # Stopped during recovery: the way back in is recovery, not setup.
        recovery = mock.Mock(run=mock.Mock(side_effect=KeyboardInterrupt))
        with mock.patch.dict(sys.modules, recovery_wizard=recovery), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: printed.append(' '.join(map(str, a)))):
            self.assertEqual(installer.main(['--recover', '--config', str(self.config)]), 130)
        self.assertIn('sudo bash install.sh --recover', printed[-1])

    def test_a_settings_file_without_its_drives_says_which_is_missing(self):
        config = json.loads(self.config.read_text())
        del config['media_mount']
        with self.assertRaisesRegex(installer.InstallerError, '"media_mount" .where the media HDD is mounted.'):
            installer.validate_config(config)

    def test_the_first_log_moves_to_the_ssd_once(self):
        STEP_OK = 'DEPENDENCIES = []\ndef run(ctx):\n    ctx.log("hello from the step")\n'
        (self.root / 'modules' / 'storage.py').write_text(STEP_OK)
        default = self.root / 'default-state'
        data = self.root / 'data_mount'
        with mock.patch.object(installer, 'DEFAULT_STATE', default), mock.patch('config_checks.validate'), \
                mock.patch.object(installer.os.path, 'ismount', return_value=False), mock.patch('builtins.print'):
            for _ in range(2):
                installer._execute(self.args(module='storage', state_dir=str(default)), self.root)
        log = (data / 'installer/state/install.log').read_text()
        self.assertEqual(log.count('START storage'), 2, log)
        self.assertFalse((default / 'install.log').exists())


SPECS = [('data_ssd', 'aaa', 'ext4', '/srv/nekoserver'), ('media_hdd', 'bbb', 'ext4', '/srv/media')]
OLD = 'UUID=aaa /srv/nekoserver ext4 defaults,nofail,x-systemd.device-timeout=15s 0 2\n'


class FstabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fstab = Path(self.tmp.name) / 'fstab'
        self.backup = Path(self.tmp.name) / 'fstab.nekoserver.bak'

    def test_drives_wait_long_enough_for_a_slow_hard_drive(self):
        self.fstab.write_text('UUID=root / ext4 defaults 0 1\n')
        self.assertTrue(storage.install_fstab(SPECS, self.fstab))
        lines = self.fstab.read_text().splitlines()
        self.assertEqual(lines[0], 'UUID=root / ext4 defaults 0 1')
        self.assertIn('x-systemd.device-timeout=90s', lines[2])
        self.assertEqual(self.backup.read_text(), 'UUID=root / ext4 defaults 0 1\n')
        self.assertFalse(storage.install_fstab(SPECS, self.fstab))

    def test_a_line_from_an_older_version_is_updated_and_the_first_backup_kept(self):
        self.fstab.write_text('UUID=root / ext4 defaults 0 1\n' + OLD)
        self.backup.write_text('the fstab from before NekoServer\n')
        self.assertTrue(storage.install_fstab(SPECS, self.fstab))
        text = self.fstab.read_text()
        self.assertNotIn('15s', text)
        self.assertEqual(text.count('/srv/nekoserver'), 1)
        self.assertEqual(self.backup.read_text(), 'the fstab from before NekoServer\n')

    def test_a_line_someone_else_wrote_still_stops_setup(self):
        original = 'UUID=aaa /srv/nekoserver ext4 defaults,noatime 0 2\n'
        self.fstab.write_text(original)
        with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
            storage.install_fstab(SPECS, self.fstab)
        self.assertEqual(self.fstab.read_text(), original)



class PrivateEntryTests(unittest.TestCase):
    def entry(self, answers, valid):
        typed, shown = iter(answers), []
        value = guided_setup.private_entry('URL: ', valid, 'try again', ask=lambda _: next(typed),
                                           output=shown.append)
        return value, shown

    def test_a_typo_is_asked_again_and_a_copied_slash_dropped(self):
        value, shown = self.entry(['htps://hc-ping.com/abc', ' https://hc-ping.com/abc-123/ '],
                                  heartbeat.endpoint)
        self.assertEqual(value, 'https://hc-ping.com/abc-123')
        self.assertEqual(shown, ['try again'])

    def test_three_typos_leave_the_step_waiting_instead_of_failing(self):
        value, shown = self.entry(['nope'] * 3, notifications.endpoint)
        self.assertEqual((value, len(shown)), ('', 3))

    def test_blank_and_ctrl_d_defer(self):
        self.assertEqual(self.entry([''], notifications.endpoint)[0], '')

        def eof(_):
            raise EOFError
        self.assertEqual(guided_setup.private_entry('URL: ', notifications.endpoint, '', ask=eof), '')

    def test_tokens(self):
        self.assertTrue(notifications.valid_token(''))
        self.assertTrue(notifications.valid_token('tk_abc.123-x'))
        self.assertFalse(notifications.valid_token('tk abc'))
        self.assertFalse(notifications.valid_token('a' * 513))



class SettingsFollowTheAppsTests(unittest.TestCase):
    """An app added later (setup run again with  --module media ) reaches the backups
    that are already installed, not only the dashboard."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.config['media']['services'] = ['jellyfin']

    def test_the_nightly_backup_and_monthly_update_cover_an_added_app(self):
        logs = []
        nightly, monthly = self.root / 'applications.json', self.root / 'maintenance.json'
        nightly.write_text(json.dumps(app_backup.guard_config(self.config)))
        monthly.write_text(json.dumps(self.config))
        self.config['media']['services'] = ['jellyfin', 'sonarr']
        self.assertTrue(app_backup.refresh(self.config, logs.append, nightly))
        self.assertTrue(maintenance.refresh(self.config, logs.append, monthly))
        self.assertEqual(json.loads(nightly.read_text())['media']['services'], ['jellyfin', 'sonarr'])
        self.assertEqual(json.loads(monthly.read_text())['media']['services'], ['jellyfin', 'sonarr'])
        # Nothing changed: nothing written again.
        self.assertFalse(app_backup.refresh(self.config, logs.append, nightly))

    def test_a_backup_that_is_not_installed_stays_not_installed(self):
        self.assertFalse(app_backup.refresh(self.config, lambda m: None, self.root / 'missing.json'))
        self.assertFalse((self.root / 'missing.json').exists())

    def test_the_installer_refreshes_them_after_every_run(self):
        self.assertIn('app_backup', installer.REFRESHED)
        self.assertIn('maintenance', installer.REFRESHED)



class SteamLeftOpenTests(unittest.TestCase):
    """Steam's folders are marked, so a night Steam is open saves everything else."""
    def test_only_steam_folders_are_marked(self):
        from modules import host_backup
        config = json.loads((ROOT / 'config.json').read_text())
        user = mock.Mock(pw_uid=1000, pw_name='neko')
        steam = [Path('/home/neko/.local/share/Steam') / n for n in ('config', 'userdata', 'steamapps/compatdata')]
        with mock.patch.object(host_backup, 'choose_user', return_value=user), \
                mock.patch.object(host_backup, 'owned_home', return_value=Path('/home/neko')), \
                mock.patch.object(host_backup, 'steam_paths', return_value=steam), \
                mock.patch.object(host_backup.native_runtime, 'storage_config', return_value={}):
            rows = host_backup.guard(config)['host_backup']['sources']
        marked = [r['path'] for r in rows if r.get('steam')]
        self.assertEqual(marked, [str(p) for p in steam])
        self.assertTrue(any(r['path'] == '/etc/samba' and not r.get('steam') for r in rows))

    def test_the_runtime_leaves_steam_out_instead_of_failing(self):
        source = (ROOT / 'runtime/src/host_snapshot.rs').read_text()
        self.assertIn('copied_now(all, steam_open(cfg)?)', source)
        self.assertNotIn('fn idle(', source)


if __name__ == '__main__':
    unittest.main()
