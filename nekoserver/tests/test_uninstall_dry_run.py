"""Gate run B8 (VM2): `bash uninstall.sh --dry-run` as a normal user, which the README says
needs no password, stopped with a PermissionError traceback on /run/nekoserver-control (a
folder of setup's that only root may read). The dry run now lists what this account can
read and says in plain words what it could not, without sudo."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uninstaller  # noqa: E402
from test_uninstaller import FakeSystem  # noqa: E402

CONTROL = 'run/nekoserver-control'


class UnprivilegedDryRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = FakeSystem(tmp.name).populate()
        self.control = self.system.root / CONTROL
        self.shown = []

    def root_only(self):
        """Path.iterdir as this account sees it: /run/nekoserver-control is root's own (a
        chmod would not stop a test run as root)."""
        real = Path.iterdir
        control = self.control

        def iterdir(path):
            if path == control:
                raise PermissionError(13, 'Permission denied', str(path))
            return real(path)

        return mock.patch.object(Path, 'iterdir', iterdir)

    def test_the_folder_it_may_not_read_is_said_and_the_rest_is_listed(self):
        with self.root_only():
            plan = self.system.plan()
            said = plan.describe({key for key, *_ in uninstaller.CHOICES})
        self.assertIn(
            '!! Could not look in ' + str(self.control) + ' without sudo, so what is in it is not listed', said
        )
        self.assertIn('run with sudo for the whole plan', said)
        # Said once, however many choices look at /run.
        self.assertEqual(said.count('Could not look in ' + str(self.control)), 1)
        # Not removed whole (its lock files stay), and the rest of the plan is there.
        self.assertNotIn('remove ' + str(self.control) + ' ', said)
        self.assertIn('== Your photos and videos', said)
        self.assertIn('nekoserver-dashboard.service', said)

    def test_the_dry_run_ends_well_without_sudo(self):
        with (
            self.root_only(),
            mock.patch.object(uninstaller.os, 'geteuid', return_value=1000),
            mock.patch.object(uninstaller, 'find_settings', return_value=({}, '/srv/nekoserver/my-config.json')),
            mock.patch.object(uninstaller, 'Plan', side_effect=lambda settings: self.system.plan()),
            mock.patch.object(uninstaller, 'busy') as locks,
            mock.patch('builtins.print', self.shown.append),
        ):
            self.assertEqual(uninstaller.main(['--dry-run']), 0)
        locks.assert_not_called()  # a dry run takes no lock
        text = '\n'.join(str(line) for line in self.shown)
        self.assertIn('Dry run: what each choice would remove. Nothing is changed.', text)
        self.assertIn('Could not look in ' + str(self.control) + ' without sudo', text)
        self.assertNotIn('Traceback', text)

    def test_anything_else_out_of_reach_says_run_it_with_sudo(self):
        def private(settings):
            raise PermissionError(13, 'Permission denied', '/srv/nekoserver/installer')

        code = uninstaller.dry_run({}, 'the settings', plan_of=private, out=self.shown.append)
        self.assertEqual(code, 1)
        self.assertEqual(self.shown[0], 'Settings: the settings')
        self.assertIn('This account may not look at /srv/nekoserver/installer', self.shown[1])
        self.assertIn('Run it with sudo:  sudo bash uninstall.sh --dry-run', self.shown[1])

    def test_the_readme_s_no_password_look_is_still_what_the_script_offers(self):
        readme = (ROOT / 'README.md').read_text()
        self.assertIn('no password needed): `bash uninstall.sh --dry-run`', readme)
        self.assertIn('bash uninstall.sh --dry-run  to only look', (ROOT / 'uninstall.sh').read_text())


if __name__ == '__main__':
    unittest.main()
