"""The install and first-setup flow (review of 0.39.5): which settings file the commands
in the docs use, and the questions and screens on the way."""
import json
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
        self.assertLess(first, script.index('host.py" bootstrap'))
        second = script.index('capabilities.py" preflight', first + 1)
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
        result = subprocess.run([node, str(here / 'setup_window_harness.js'), str(here.parent / 'setup-gui/setup.js'),
                                 json.dumps(states), action], capture_output=True, text=True, timeout=60, check=True)
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


class BackupDriveMessageTests(unittest.TestCase):
    def test_a_fat32_stick_is_named_instead_of_connect_it_first(self):
        from modules import photo_backup
        config = {'storage': {'data_ssd': {'uuid': 'ssd'}, 'media_hdd': {'uuid': 'hdd'}}, 'external_backup_mount': '/mnt/usb'}
        inventory = {'blockdevices': [
            {'name': 'sda', 'type': 'disk', 'children': [{'name': 'sda1', 'type': 'part', 'mountpoint': '/', 'fstype': 'ext4', 'uuid': 'os'}]},
            {'name': 'sdc', 'type': 'disk', 'children': [{'name': 'sdc1', 'type': 'part', 'fstype': 'vfat', 'uuid': 'AB-CD'}]}]}
        with self.assertRaisesRegex(RuntimeError, r'/dev/sdc1 is formatted FAT \(FAT32\).*exFAT or ext4'):
            photo_backup.select_external(config, inventory)
        # The system disk's own FAT boot partition is never named.
        inventory['blockdevices'][0]['children'].append({'name': 'sda2', 'type': 'part', 'fstype': 'vfat', 'uuid': 'EF'})
        del inventory['blockdevices'][1]
        with self.assertRaisesRegex(RuntimeError, 'connect it first'):
            photo_backup.select_external(config, inventory)


if __name__ == '__main__':
    unittest.main()
