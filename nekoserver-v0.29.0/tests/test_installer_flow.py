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
        args = self.args()
        with mock.patch('config_checks.validate'), mock.patch('builtins.print'), self.assertRaises(KeyboardInterrupt):
            installer._execute(args, self.root)
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

    def test_a_drive_under_home_is_refused_before_anything_is_installed(self):
        import neko_common
        # ProtectHome=yes in the services' units hides /home, /root and /run/user.
        for bad in ('/home/neko/app-ssd', '/run/user/1000/ssd'):
            with self.assertRaisesRegex(RuntimeError, 'cannot see those folders'):
                neko_common.mount_path(bad)
        self.assertEqual(str(neko_common.mount_path('/srv/nekoserver')), '/srv/nekoserver')
        self.assertEqual(str(neko_common.mount_path('/mnt/homeserver')), '/mnt/homeserver')  # only a similar name
        config = json.loads(self.config.read_text())
        config['data_mount'] = '/home/neko/app-ssd'
        with self.assertRaisesRegex(RuntimeError, 'cannot see those folders'):
            installer.validate_config(config)

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


SPECS = [('data_ssd', 'aaa', 'ext4', '/srv/nekoserver', None), ('media_hdd', 'bbb', 'ext4', '/srv/media', None)]
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

    def test_a_line_you_wrote_yourself_is_shown_with_what_to_do(self):
        self.fstab.write_text('UUID=root / ext4 defaults 0 1\nUUID=bbb /mnt/films ext4 defaults 0 2\n')
        with self.assertRaises(RuntimeError) as caught:
            storage.install_fstab(SPECS, self.fstab)
        message = str(caught.exception)
        self.assertIn('    UUID=bbb /mnt/films ext4 defaults 0 2', message)
        self.assertIn('Take that line out of /etc/fstab', message)
        self.assertEqual(self.fstab.read_text().count('\n'), 2)   # nothing written

    def test_a_btrfs_subvolume_is_kept_so_a_restart_mounts_the_same_tree(self):
        self.fstab.write_text('UUID=root / ext4 defaults 0 1\n')
        specs = [('data_ssd', 'aaa', 'btrfs', '/srv/nekoserver', '/@data'), ('media_hdd', 'bbb', 'btrfs', '/srv/media', None)]
        storage.install_fstab(specs, self.fstab)
        lines = self.fstab.read_text().splitlines()
        self.assertEqual(lines[1], 'UUID=aaa /srv/nekoserver btrfs defaults,nofail,x-systemd.device-timeout=90s,subvol=/@data 0 2')
        self.assertNotIn('subvol', lines[2])   # the filesystem's default tree
        # Unchanged on the next run; a line from before 0.28.7 for that drive is upgraded.
        self.assertFalse(storage.install_fstab(specs, self.fstab))
        self.fstab.write_text('UUID=aaa /srv/nekoserver btrfs defaults,nofail,x-systemd.device-timeout=15s,subvol=/@data 0 2\n')
        self.assertTrue(storage.install_fstab(specs[:1], self.fstab))
        self.assertIn('90s,subvol=/@data', self.fstab.read_text())
        # A line for the same drive with another subvolume is someone else's choice.
        self.fstab.write_text('UUID=aaa /srv/nekoserver btrfs defaults,nofail,x-systemd.device-timeout=90s,subvol=/@other 0 2\n')
        with self.assertRaisesRegex(RuntimeError, 'did not write'):
            storage.install_fstab(specs[:1], self.fstab)

    def test_an_already_mounted_subvolume_is_remembered(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/srv/media',
                  'storage': {'data_ssd': {'uuid': 'ssd-uuid-0001', 'fstype': 'btrfs'}, 'media_hdd': {'uuid': 'hdd-uuid-0001', 'fstype': 'ext4'}}}
        answers = {'/srv/nekoserver': '/dev/sdb1[/@data]\n', '/srv/media': '/dev/sdc1\n'}
        storage.remember_subvolumes(config, run=lambda argv, **_: mock.Mock(returncode=0, stdout=answers[argv[-1]]))
        self.assertEqual(config['storage']['data_ssd']['subvolume'], '/@data')
        self.assertNotIn('subvolume', config['storage']['media_hdd'])
        self.assertEqual([s[4] for s in storage.mount_specs(config)], ['/@data', None])
        # The top level of the filesystem is its default: nothing to remember.
        config['storage']['data_ssd'].pop('subvolume')
        storage.remember_subvolumes(config, run=lambda argv, **_: mock.Mock(returncode=0, stdout='/dev/sdb1[/]\n'))
        self.assertNotIn('subvolume', config['storage']['data_ssd'])
        config['storage']['data_ssd']['subvolume'] = 'bad name'
        with self.assertRaisesRegex(RuntimeError, 'subvolume'):
            storage.mount_specs(config)

    def test_a_line_someone_else_wrote_still_stops_setup(self):
        original = 'UUID=aaa /srv/nekoserver ext4 defaults,noatime 0 2\n'
        self.fstab.write_text(original)
        with self.assertRaisesRegex(RuntimeError, 'did not write'):
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


class TimezoneTests(unittest.TestCase):
    def test_a_copied_localtime_falls_back_to_etc_timezone(self):
        import neko_common
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'zoneinfo/Asia').mkdir(parents=True)
            (root / 'zoneinfo/Asia/Jerusalem').write_bytes(b'TZif2')
            (root / 'localtime').write_bytes(b'TZif2')   # a copy, not a link
            (root / 'timezone').write_text('Asia/Jerusalem\n')
            self.assertEqual(neko_common.local_timezone(root / 'localtime', root / 'timezone', root / 'zoneinfo'), 'Asia/Jerusalem')
            (root / 'timezone').write_text('Mars/Olympus\n')   # not installed: not trusted
            self.assertIsNone(neko_common.local_timezone(root / 'localtime', root / 'timezone', root / 'zoneinfo'))
            (root / 'link').symlink_to(root / 'zoneinfo/Asia/Jerusalem')
            self.assertEqual(neko_common.local_timezone(root / 'link', root / 'missing', root / 'zoneinfo'), 'Asia/Jerusalem')

    def test_one_word_zones_are_accepted(self):
        import neko_common
        for zone in ('Japan', 'CET', 'UTC', 'Europe/London', 'America/Argentina/Buenos_Aires'):
            self.assertTrue(neko_common.TIMEZONE_PATTERN.fullmatch(zone), zone)
        for zone in ('', '../etc', 'Europe/Lon don'):
            self.assertFalse(neko_common.TIMEZONE_PATTERN.fullmatch(zone), zone)


class UsbDriveSkipTests(unittest.TestCase):
    def test_enter_skips_for_now_as_offered(self):
        from modules import photo_backup
        inventory = {'blockdevices': [{'name': 'sdd', 'type': 'disk', 'children': [
            {'name': 'sdd1', 'type': 'part', 'uuid': 'usb-uuid-0001', 'fstype': 'ext4', 'mountpoint': None}]}]}
        config = {'external_backup_mount': '/mnt/nekoserver-photos', 'storage': {}}
        self.assertIsNone(photo_backup.select_external(config, inventory, input_fn=lambda q: '', out_fn=lambda s: None))
        self.assertNotIn('external_backup_uuid', config.get('photos', {}))

        def eof(q):
            raise EOFError
        self.assertIsNone(photo_backup.select_external(config, inventory, input_fn=eof, out_fn=lambda s: None))


class ReasonsReachTheLogTests(unittest.TestCase):
    def test_a_failed_command_says_why(self):
        import neko_common
        with self.assertRaisesRegex(RuntimeError, r'Immich \(docker compose up\) failed: port is already allocated'):
            neko_common.checked_run(['sh', '-c', 'echo "port is already allocated" >&2; exit 1'], 10, 'Immich (docker compose up)')
        self.assertEqual(neko_common.checked_run(['true'], 10).returncode, 0)

    def test_a_command_that_failed_with_check_true_says_its_own_words(self):
        import subprocess
        failed = subprocess.CalledProcessError(1, ['systemctl', 'enable', '--now', 'smbd.service'],
                                               stderr='Failed to enable unit: Unit file smbd.service does not exist.\n')
        self.assertEqual(installer.failure_reason(failed),
                         'systemctl enable --now smbd.service failed: Failed to enable unit: Unit file smbd.service does not exist.')
        quiet = subprocess.CalledProcessError(2, ['docker', 'info'])   # output not captured
        self.assertEqual(installer.failure_reason(quiet), 'docker info failed (exit status 2, no details)')
        slow = subprocess.TimeoutExpired(['docker', 'compose', 'up'], 900)
        self.assertEqual(installer.failure_reason(slow), 'docker compose up did not finish within 900 seconds')

    def test_the_settings_file_keeps_its_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'my-config.json'
            path.write_text('{}')
            ctx = installer.Context({'a': 1}, lambda m: None, apply=True, config_path=path)
            chowned = []
            with mock.patch('os.geteuid', return_value=0), mock.patch('os.chown', side_effect=lambda p, u, g: chowned.append((u, g))):
                ctx.persist_config()
            self.assertEqual(chowned, [(path.stat().st_uid, path.stat().st_gid)])
            self.assertEqual(json.loads(path.read_text()), {'a': 1})


if __name__ == '__main__':
    unittest.main()
