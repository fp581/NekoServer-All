"""Deep check 292: a mistyped settings key ("media.servics", "typo_key") was taken
silently, so the setting it was meant to change stayed at its default."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer

TEMPLATE = json.loads((ROOT / 'config.json').read_text())


class MistypedSettingsTests(unittest.TestCase):
    def check(self, settings):
        """installer --check with these settings: what it printed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'my-config.json'
            path.write_text(json.dumps(settings))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = installer.main(
                    ['--check', '--profile', 'foundation', '--config', str(path), '--state-dir', tmp + '/state']
                )
        self.assertEqual(code, 0)
        return printed.getvalue()

    def test_a_mistyped_key_is_named_with_the_one_meant(self):
        settings = json.loads(json.dumps(TEMPLATE))
        settings['media']['servics'] = ['jellyfin']
        settings['dashbord'] = {'port': 8090}
        settings['typo_key'] = 1
        said = self.check(settings)
        lines = said.splitlines()
        knows = 'in my-config.json is not a setting NekoServer knows, so it does nothing.'
        self.assertIn(f'Warning: "media.servics" {knows} Did you mean "media.services"? Check its spelling.', lines)
        self.assertIn(f'Warning: "dashbord" {knows} Did you mean "dashboard"? Check its spelling.', lines)
        self.assertIn(f'Warning: "typo_key" {knows} Check its spelling.', lines)

    def test_the_template_and_setups_own_notes_say_nothing(self):
        settings = json.loads(json.dumps(TEMPLATE))
        settings.update(
            {
                '_comment': 'mine',
                'retention_reviewed': True,
                'existing_media_asked': True,
                'locations': {'photos': 'ssd'},
            }
        )
        settings['tv']['wanted'] = True  # an answer setup saved (guided_setup.wanted)
        settings['storage']['usb'] = {'uuid': 'x'}  # drives are names of yours
        self.assertNotIn('Warning', self.check(settings))


class JobTimeTests(unittest.TestCase):
    def test_a_job_time_is_hours_and_minutes_of_a_day(self):
        import config_checks

        for good in ('00:00', '07:30', '23:59'):
            config_checks._time(good)
        for bad in ('24:00', '7:30', '07:60', '07:30:00', '٠٧:٣٠', 730, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, 'Invalid job time; use HH:MM'):
                config_checks._time(bad)


if __name__ == '__main__':
    unittest.main()
