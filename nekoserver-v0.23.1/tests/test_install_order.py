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
