"""install_state.read_object reads only a .json file, by its real path: a settings file
named on the command line (--config) can never make it read another kind of file, or
read through a link (SonarCloud on PR 84: path traversal)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install_state  # noqa: E402


class ReadObjectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_json_file_is_read(self):
        path = self.folder / 'my-config.json'
        path.write_text(json.dumps({'data_mount': '/srv/nekoserver'}))
        self.assertEqual(install_state.read_object(path), {'data_mount': '/srv/nekoserver'})

    def test_another_kind_of_file_a_link_or_nothing_is_never_read(self):
        other = self.folder / 'shadow'
        other.write_text(json.dumps({'secret': 1}))
        self.assertEqual(install_state.read_object(other), {})
        link = self.folder / 'link.json'
        os.symlink(other, link)
        self.assertEqual(install_state.read_object(link), {})
        self.assertEqual(install_state.read_object(self.folder / 'missing.json'), {})
        (self.folder / 'list.json').write_text('[1, 2]')
        self.assertEqual(install_state.read_object(self.folder / 'list.json'), {})


if __name__ == '__main__':
    unittest.main()
