"""Backups of files with the longest names Linux allows (255 bytes): the restore finds
the shortened version names and notes the backup program writes (backup.rs)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common as nc  # noqa: E402
import recovery_wizard as rw  # noqa: E402


class LongNameTests(unittest.TestCase):
    def test_the_same_names_as_the_backup_program(self):
        # backup.rs long_names_are_shortened_the_same_way_as_the_restore_expects
        name = 'n' * 250 + '.pdf'
        stem, ext = nc.version_parts(name)
        self.assertEqual((len(stem.encode()), stem[-12:], ext), (224, 'nnn~b93fc58d', '.pdf'))
        self.assertTrue(nc.latest_note(name).endswith('nnn~b93fc58d.neko-latest'))
        self.assertEqual(len(nc.latest_note(name).encode()), 255)
        self.assertEqual(nc.partial_name(name), '.neko-b93fc58dc2275fe3.neko-part')
        stem, ext = nc.version_parts('é' * 120 + '.txt')
        self.assertEqual((len(stem.encode()), stem[-12:], ext), (223, 'ééé~ead92854', '.txt'))
        self.assertEqual(nc.version_parts('photo.jpg'), ('photo', '.jpg'))
        self.assertEqual(nc.latest_note('photo.jpg'), '.photo.jpg.neko-latest')

    def test_newest_only_restores_a_long_named_file_under_its_own_name(self):
        name = 'Quarterly report ' + 'x' * 228 + '.docx'
        self.assertEqual(len(name.encode()), 250)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'usb/alice/files'
            folder.mkdir(parents=True)
            (folder / name).write_bytes(b'first')
            stem, ext = nc.version_parts(name)
            version = stem + '.neko-20261022T104800Z' + ext
            (folder / version).write_bytes(b'edited')
            (folder / nc.NAMES_FILE).write_text(json.dumps({'format': 'neko-names-v1', 'names': {stem: name}}))
            (folder / nc.latest_note(name)).write_text(version)
            chosen = rw.latest_versions([('alice', Path(tmp) / 'usb/alice')])
            self.assertEqual(len(chosen), 1)
            path, target, _, count = chosen[0]
            self.assertEqual((path.name, target, count), (version, Path('alice/files') / name, 2))
            self.assertEqual(rw.live_name('alice/files/' + version, Path(tmp) / 'usb'), 'alice/files/' + name)


if __name__ == '__main__':
    unittest.main()
