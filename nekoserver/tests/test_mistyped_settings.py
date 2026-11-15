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
        self.assertIn('"media.servics" in my-config.json is not a setting NekoServer knows', said)
        self.assertIn('Did you mean "media.services"?', said)
        self.assertIn('Did you mean "dashboard"?', said)
        self.assertIn('"typo_key" in my-config.json is not a setting', said)

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


if __name__ == '__main__':
    unittest.main()
