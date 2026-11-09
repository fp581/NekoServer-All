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
        self.enabled = True  # the timer, as `systemctl is-enabled` says
        self.calls.side_effect = lambda argv, **k: mock.Mock(
            returncode=0 if argv[1] != 'is-enabled' or self.enabled else 1, stdout='', stderr=''
        )
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

    def test_nextcloud_is_started_again_however_the_weekly_copy_ends(self):
        # The weekly copy pauses Nextcloud for its last files and its database copy: the
        # job's ExecStopPost and a boot-time service start it again after a failure, a
        # stop or a power cut. It needs only the SSD, where Nextcloud's apps live.
        self.config['nextcloud']['enabled'] = True
        self.install()
        self.assertIn(
            'ExecStopPost=' + BIN + ' cloud-resume --config /etc/g.json\nTimeoutStopSec=', self.unit.read_text()
        )
        resume = (self.unit.parent / 'nekoserver-nextcloud-resume.service').read_text()
        self.assertIn('ExecStart=' + BIN + ' cloud-resume --config /etc/g.json\n', resume)
        self.assertIn('WantedBy=multi-user.target', resume)
        self.assertIn('RequiresMountsFor=' + self.config['data_mount'] + '\n', resume)
        self.config['nextcloud']['enabled'] = False
        self.config['photos']['deploy_immich'] = True
        self.assertTrue(self.refresh())
        self.assertNotIn('cloud-resume', self.unit.read_text())

    def test_every_setup_run_seals_the_password_list(self):
        # The USB drive gets the app passwords from the first week on: setup makes an
        # encrypted list on the HDD (seal-passwords) after every run, where passwords change.
        self.install()
        self.calls.side_effect = None
        self.calls.return_value = mock.Mock(returncode=0, stdout='Password list: the one on the HDD is current\n')
        self.refresh()
        self.assertIn(
            [BIN, 'seal-passwords', '--config', '/etc/g.json'], [c.args[0] for c in self.calls.call_args_list]
        )
        self.assertIn('Password list: the one on the HDD is current', self.logged)
        # It failing is said, and setup goes on (the USB copy reports itself incomplete).
        self.calls.return_value = mock.Mock(returncode=1, stdout='', stderr='Drive check failed')
        self.refresh()
        self.assertTrue(any('Password list: not made (Drive check failed)' in line for line in self.logged))

    def test_switched_off_then_an_app_set_up_again_switches_it_on_again(self):
        """The 0.46.0 review: with the copy itself unchanged, refresh never switched the
        timer on again after it was switched off for having nothing to copy."""
        self.install()
        self.config['photos']['deploy_immich'] = False
        self.refresh()  # switched off
        self.enabled = False
        self.config['photos']['deploy_immich'] = True  # Immich set up again: the same copy
        self.calls.reset_mock()
        self.assertTrue(self.refresh())
        self.assertIn(['systemctl', 'enable', '--now', self.timer.name], [c.args[0] for c in self.calls.call_args_list])

    def test_a_new_day_or_time_is_installed(self):
        """The 0.46.0 review: a schedule changed in the settings left the old day and time."""
        self.install()
        self.assertIn('OnCalendar=Sun *-*-* 03:00:00', self.timer.read_text())
        self.config['backup_times']['weekly_day'] = 'Wed'  # the weekly day setup asked for (0.48.0)
        day, when = photo_backup.schedule(self.config)
        self.assertTrue(self.refresh())
        self.assertIn(f'OnCalendar={day} *-*-* {when}:00', self.timer.read_text())
        self.assertFalse(self.refresh())

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
