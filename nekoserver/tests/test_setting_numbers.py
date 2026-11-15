"""Deep check plan 277: a number in my-config.json out of its range stopped setup with
"ERROR: Invalid maintenance.keep_snapshots" (or "Invalid journal bound", "Invalid
notification timing", "Invalid photo archive interval/age"): it did not say which setting,
where, or what to write instead. Each now names the setting, the file and the range."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config_checks
from modules import host_backup, maintenance, notifications, server_os

# (check, settings group, setting, a value it refuses, the range it takes)
CASES = (
    (maintenance.options, 'maintenance', 'keep_snapshots', 200, (0, 120)),
    (maintenance.options, 'maintenance', 'night_end_hour', 9, (1, 7)),
    (maintenance.options, 'maintenance', 'settle_days', '7', (1, 30)),
    (server_os.settings, 'server_os', 'journal_retention_days', 365, (1, 90)),
    (notifications.options, 'notifications', 'repeat_seconds', 60, (3600, 604800)),
    (host_backup.settings, 'host_backup', 'minimum_free_gb', 0, (1, 1024)),
    (lambda c: config_checks.validate(c, ['media_monitor']), 'monitoring', 'interval_minutes', 5, (15, 1440)),
    (lambda c: config_checks.validate(c, ['onboarding']), 'onboarding', 'wait_seconds', 900, (0, 600)),
    (
        lambda c: config_checks.validate(c, ['photo_preservation']),
        'photos',
        'archive_min_age_seconds',
        1,
        (60, 31536000),
    ),
)


class SettingNumberTests(unittest.TestCase):
    def test_a_number_out_of_range_names_the_setting_and_its_range(self):
        for check, group, key, value, (low, high) in CASES:
            with self.subTest(key=key), mock.patch('neko_common.host_timezone', return_value='UTC'):
                config = {group: {key: value}, 'timezone': 'UTC'}
                with (
                    mock.patch.object(maintenance, 'host_timezone', return_value='UTC'),
                    mock.patch.object(maintenance, 'job_times', return_value={'monthly': '02:00'}),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    check(config)
                said = str(caught.exception)
                self.assertIn(f'"{key}" under "{group}" in my-config.json', said)
                self.assertIn(f'from {low} to {high}', said)
                self.assertIn(repr(value), said)


if __name__ == '__main__':
    unittest.main()
