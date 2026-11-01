"""Phone alerts or the outage monitor switched off (in the setup window, or "wanted":
false) are switched off for real: the installed timer is stopped and disabled before
the saved destination is read, and no timer is enabled again."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status_checks  # noqa: E402
from modules import _schedules, heartbeat, notifications  # noqa: E402


class SwitchedOffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.units = Path(tmp.name)
        for name in ('nekoserver-notifications', 'nekoserver-heartbeat'):
            for suffix in ('timer', 'service'):
                (self.units / f'{name}.{suffix}').write_text('[Unit]\nDescription=NekoServer something\n')
        self.calls = []
        for patch in (
            mock.patch.object(_schedules, 'SYSTEMD_UNITS', self.units),
            mock.patch.object(_schedules, 'subprocess', mock.Mock(run=lambda argv, **_: self.calls.append(argv))),
        ):
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def ran(self, module, section):
        logged = []
        ctx = mock.Mock(apply=True, config={section: {'wanted': False}}, log=logged.append)
        with (
            mock.patch.object(module.os, 'geteuid', return_value=0),
            mock.patch.object(module, 'safe_path', side_effect=AssertionError('the saved destination was read')),
            mock.patch.object(
                module, 'subprocess', mock.Mock(run=mock.Mock(side_effect=AssertionError('a timer was enabled again')))
            ),
        ):
            self.assertIsNone(module.run(ctx))
        return '\n'.join(logged)

    def test_phone_alerts_off_stop_their_timer(self):
        self.ran(notifications, 'notifications')
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-notifications.timer'], self.calls)
        self.assertIn(['systemctl', 'stop', 'nekoserver-notifications.service'], self.calls)

    def test_the_outage_monitor_off_stops_its_timer_and_says_to_pause_the_monitor(self):
        said = self.ran(heartbeat, 'heartbeat')
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-heartbeat.timer'], self.calls)
        self.assertIn('pause (or delete) the check at your outside monitor', said)

    def test_never_set_up_and_off_does_nothing(self):
        for path in self.units.iterdir():
            path.unlink()
        self.assertTrue(
            _schedules.switched_off({'heartbeat': {'wanted': False}}, 'heartbeat', 'nekoserver-heartbeat', print)
        )
        self.assertEqual(self.calls, [])
        self.assertFalse(_schedules.switched_off({'heartbeat': {}}, 'heartbeat', 'nekoserver-heartbeat', print))


class SwitchedOffStatusTests(unittest.TestCase):
    def test_a_switched_off_timer_is_not_a_missed_check_in(self):
        for active, expected in (('inactive', 'unknown'), ('active', 'stale')):
            answer = (0, 'LoadState=loaded\nActiveState=' + active + '\n')
            with (
                mock.patch.object(status_checks, 'run', return_value=answer),
                mock.patch.object(status_checks, 'read_json', return_value={}),
            ):
                self.assertEqual(status_checks.heartbeat_status({}, 2_000_000_000.0)['status'], expected)
                self.assertEqual(status_checks.notification_status(2_000_000_000.0)['status'], expected)


if __name__ == '__main__':
    unittest.main()
