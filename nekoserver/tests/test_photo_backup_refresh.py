"""The weekly USB copy follows the apps set up now: Nextcloud added after the USB copy
was installed is copied from the next week on (and a switched-off Immich is not)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer  # noqa: E402
from modules import photo_backup  # noqa: E402

BIN = '/srv/nekoserver/installer/runtime/v0.39.2/nekoserver-runtime'


class RefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        media = self.dir / 'media'
        (media / 'photos/archive').mkdir(parents=True)
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.config.update(
            media_mount=str(media),
            data_mount=str(self.dir / 'data'),
            external_backup_mount=str(self.dir / 'usb'),
            timezone='UTC',
        )
        self.config['photos'].update(
            deploy_immich=True, external_backup_uuid='cccc-3333', external_backup_fstype='exfat'
        )
        self.config.setdefault('nextcloud', {})['enabled'] = False
        self.unit, self.timer = self.dir / 'backup.service', self.dir / 'backup.timer'
        self.logged = []
        self.guard = mock.patch.object(photo_backup.native_runtime, 'backup_guard', return_value=Path('/etc/g.json'))
        self.systemctl = mock.patch.object(photo_backup.subprocess, 'run')
        self.guard.start()
        self.calls = self.systemctl.start()
        self.addCleanup(mock.patch.stopall)

    def install(self):
        source = photo_backup.archive_source(self.config)
        cmd = photo_backup.command_line(
            self.config, BIN, Path('/etc/g.json'), 'cccc-3333', 'exfat', source, photo_backup.destination(self.config)
        )
        photo_backup.install_units(self.config, cmd, True, 'Sun', '03:00', self.unit, self.timer)
        self.calls.reset_mock()

    def refresh(self):
        return photo_backup.refresh(self.config, self.logged.append, self.unit, self.timer)

    def test_nextcloud_added_later_is_copied_too(self):
        self.install()
        self.assertNotIn('--cloud', self.unit.read_text())
        self.config['nextcloud']['enabled'] = True
        self.assertTrue(self.refresh())
        text = self.unit.read_text()
        self.assertIn('--cloud ' + str(Path(self.config['media_mount']) / 'cloud/files'), text)
        self.assertIn('ExecStart=' + BIN + ' weekly ', text)  # the installed program stays
        self.assertIn('Nextcloud files', self.logged[-1])
        self.assertFalse(self.refresh())  # nothing changes a second time

    def test_immich_switched_off_leaves_only_nextcloud(self):
        self.config['nextcloud']['enabled'] = True
        self.install()
        self.config['photos']['deploy_immich'] = False
        self.assertTrue(self.refresh())
        text = self.unit.read_text()
        self.assertNotIn('--source', text)
        self.assertNotIn('After=nekoserver-immich-db-backup.service', text)

    def test_nothing_happens_without_an_installed_copy(self):
        self.assertFalse(self.refresh())
        self.calls.assert_not_called()

    def test_with_nothing_to_copy_any_more_the_weekly_copy_is_switched_off(self):
        self.install()
        before = self.unit.read_text()
        self.config['photos']['deploy_immich'] = False  # and Nextcloud is off too
        self.assertFalse(self.refresh())
        commands = [c.args[0] for c in self.calls.call_args_list]
        self.assertIn(['systemctl', 'disable', '--now', self.timer.name], commands)
        self.assertIn(['systemctl', 'stop', self.unit.name], commands)
        self.assertEqual(self.unit.read_text(), before)  # kept: the dashboard shows it switched off
        self.assertIn('switched off', self.logged[-1])

    def test_the_step_itself_switches_it_off_too(self):
        self.install()
        self.config['photos']['deploy_immich'] = False
        ctx = mock.Mock(config=self.config, apply=True, log=self.logged.append)
        with (
            mock.patch.object(photo_backup.os, 'geteuid', return_value=0),
            mock.patch.object(photo_backup, 'SERVICE_UNIT', self.unit),
            mock.patch.object(photo_backup, 'TIMER_UNIT', self.timer),
        ):
            result = photo_backup.run(ctx)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn(
            ['systemctl', 'disable', '--now', self.timer.name], [c.args[0] for c in self.calls.call_args_list]
        )

    def test_the_installer_refreshes_it_after_every_run(self):
        self.assertIn('photo_backup', installer.REFRESHED)


if __name__ == '__main__':
    unittest.main()
