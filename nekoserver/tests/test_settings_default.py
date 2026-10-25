"""Without --config, every command uses your settings: my-config.json beside it, or
config.json in the copy kept on the SSD. The template config.json (no drives in it) is
refused with a sentence that names --config, instead of a puzzling failure later."""
import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common

CLIS = ('acceptance.py', 'account-setup.py', 'music-setup.py', 'media-health.py', 'media-connect.py', 'onboard.py',
        'recover.py', 'gateway_cert.py', 'source-wizard.py', 'source_import.py')


class DefaultSettingsTests(unittest.TestCase):
    def test_my_config_then_the_kept_copy_then_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'config.json').write_text('{}')
            self.assertEqual(neko_common.default_settings(folder), neko_common.NO_SETTINGS)
            (folder / 'release-manifest.json').write_text('{}')
            self.assertEqual(neko_common.default_settings(folder), str(folder / 'config.json'))
            (folder / 'my-config.json').write_text('{}')
            self.assertEqual(neko_common.default_settings(folder), str(folder / 'my-config.json'))

    def test_the_template_is_refused_with_a_sentence_naming_config(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--config', type=neko_common.checked_settings, default=neko_common.NO_SETTINGS)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            parser.parse_args([])
        self.assertIn('--config', err.getvalue())
        self.assertIn('template', err.getvalue())
        self.assertEqual(parser.parse_args(['--config', '/x/config.json']).config, '/x/config.json')

    def test_every_command_uses_the_shared_default(self):
        for name in CLIS:
            text = (ROOT / name).read_text()
            line = re.search(r"add_argument\(\s*'--config'[^\n]*", text).group(0)
            self.assertIn('default_settings(', line, name)
            self.assertIn('checked_settings', line, name)
            self.assertNotIn("with_name('config.json')", line, name)

    def test_acceptance_in_the_release_folder_uses_my_config(self):
        import acceptance
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'config.json').write_text(json.dumps({'storage': {}}))
            err = io.StringIO()
            with mock.patch.object(acceptance, 'ROOT', folder), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                acceptance.main([])
            self.assertIn('my-config.json', err.getvalue())
            (folder / 'my-config.json').write_text('{}')
            seen = []
            with mock.patch.object(acceptance, 'ROOT', folder), mock.patch.object(acceptance.os, 'geteuid', return_value=0), \
                    mock.patch.object(acceptance, 'load_json', side_effect=lambda p: seen.append(str(p)) or {}), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                acceptance.main([])
            self.assertEqual(seen[0], str(folder / 'my-config.json'))


class InstallerSettingsTests(unittest.TestCase):
    def settings(self, folder, apply=True):
        import installer
        parser = argparse.ArgumentParser()
        with mock.patch.object(installer, 'ROOT', folder):
            return installer.settings_for(argparse.Namespace(config=None, apply=apply), parser)

    def test_the_kept_copy_on_the_ssd_uses_its_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'config.json').write_text('{}')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.settings(folder)
            (folder / 'release-manifest.json').write_text('{}')
            self.assertEqual(self.settings(folder), str(folder / 'config.json'))


if __name__ == '__main__':
    unittest.main()
