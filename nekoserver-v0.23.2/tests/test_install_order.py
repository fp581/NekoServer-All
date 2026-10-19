"""v0.23.1: install order, system update first, drive picking and weekly catch-up."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer  # noqa: E402
from modules import storage  # noqa: E402


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.full = self.config['profiles']['full']
        self.modules = installer.load_modules(ROOT / 'modules')

    def test_prerequisites_then_tailscale_then_the_rest(self):
        self.assertEqual(self.full[:3], ['system', 'tailscale', 'storage'])
        self.assertLess(self.full.index('gateway_tailnet'), self.full.index('gateway_lan'))

    def test_every_step_runs_after_what_it_needs(self):
        # The profile order is the order users see; nothing may be pulled forward.
        # (Helper steps a module needs, such as music_backup, may be added in between.)
        planned = installer.ordered(self.modules, self.full)
        self.assertEqual([name for name in planned if name in self.full], self.full)

    def test_system_update_and_app_packages_come_before_the_installer(self):
        script = (ROOT / 'install.sh').read_text()
        update = script.index('pacman -Syu --noconfirm')
        packages = script.index('app_packages=(')
        start = script.index('exec python3')
        self.assertLess(update, packages)
        self.assertLess(packages, start)
        self.assertIn('/usr/lib/modules/$(uname -r)', script)  # reboot after a new kernel
        for name in ('docker', 'docker-compose', 'tailscale', 'caddy', 'samba', 'qrencode'):
            self.assertIn(name, script[packages:start])

    def test_missed_weekly_backups_run_at_next_start(self):
        for name in ('photo_backup.py', 'photo_preservation.py'):
            text = (ROOT / 'modules' / name).read_text()
            self.assertNotIn('Persistent=false', text)


class DrivePickingTests(unittest.TestCase):
    INVENTORY = {'blockdevices': [
        {'name': 'nvme0n1', 'type': 'disk', 'children': [
            {'name': 'nvme0n1p2', 'type': 'part', 'uuid': 'root-uuid-0001', 'fstype': 'ext4', 'mountpoint': '/'}]},
        {'name': 'sdb', 'type': 'disk', 'model': 'SSD', 'children': [
            {'name': 'sdb1', 'type': 'part', 'uuid': 'ssd-uuid-0001', 'fstype': 'ext4',
             'mountpoint': '/run/media/neko/fast', 'size': '1T'}]},
        {'name': 'sdc', 'type': 'disk', 'model': 'HDD', 'children': [
            {'name': 'sdc1', 'type': 'part', 'uuid': 'hdd-uuid-0001', 'fstype': 'ext4',
             'mountpoint': None, 'size': '12T'}]},
    ]}

    def test_desktop_mounted_drive_can_be_chosen_by_its_name(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['1', 'sdb1', '1', '/dev/sdc1'])
        out = []
        storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                         input_fn=lambda q='': next(answers), out_fn=out.append)
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-uuid-0001')
        self.assertEqual(config['storage']['media_hdd']['uuid'], 'hdd-uuid-0001')
        self.assertFalse(any('1TB' in line or '12TB' in line for line in out))

    def test_wrong_confirmation_is_refused(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['1', 'sdc1'])
        with self.assertRaisesRegex(RuntimeError, 'confirmation failed'):
            storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                             input_fn=lambda q='': next(answers), out_fn=lambda s: None)


if __name__ == '__main__':
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """v0.23.2: findings from the external review."""

    def test_failed_space_check_blocks_everything_that_writes_to_the_drives(self):
        modules = installer.load_modules(ROOT / 'modules')

        def needs(name, target, seen=()):
            deps = modules[name]['deps']
            return target in deps or any(needs(d, target, seen + (name,)) for d in deps if d not in seen)

        for name in ('docker', 'media', 'photos', 'nextcloud', 'maintenance', 'app_backup',
                     'host_backup', 'photo_backup', 'photo_preservation', 'server_os'):
            self.assertTrue(needs(name, 'capacity'), name)
        # Tailscale does not touch the drives and still runs first.
        self.assertFalse(needs('tailscale', 'capacity'))

    def test_seerr_folder_is_handed_to_the_configured_user(self):
        import tempfile
        from unittest import mock
        from modules import media
        with tempfile.TemporaryDirectory() as folder:
            seerr = Path(folder) / 'seerr'
            (seerr / 'db').mkdir(parents=True)
            (seerr / 'settings.json').write_text('{}')
            owners = {str(seerr): (1000, 1000)}
            calls = []

            class Stat:
                def __init__(self, uid, gid):
                    self.st_uid, self.st_gid = uid, gid

            def lstat(path):
                return Stat(*owners.get(str(path), (1000, 1000)))

            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)), \
                    mock.patch.object(media.os, 'chown', lambda p, u, g, follow_symlinks=True: calls.append((str(p), u, g, follow_symlinks))):
                self.assertTrue(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))
            self.assertIn((str(seerr), 2000, 2000, False), calls)
            self.assertIn((str(seerr / 'settings.json'), 2000, 2000, False), calls)
            self.assertIn((str(seerr / 'db'), 2000, 2000, False), calls)
            # Already right, or owned by someone else entirely: left alone.
            owners[str(seerr)] = (2000, 2000)
            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)):
                self.assertFalse(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))
            owners[str(seerr)] = (3000, 3000)
            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)):
                self.assertFalse(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))

    def test_nightly_backup_settings_check_matches_what_setup_writes(self):
        from modules import app_backup
        config = json.loads((ROOT / 'config.json').read_text())
        guard = app_backup.guard_config(config)
        self.assertIn('keep_snapshots', guard['app_backup'])
        self.assertIn('app_backup.guard_config(config)', (ROOT / 'native_runtime.py').read_text())
