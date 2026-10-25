"""Only NekoServer's own backup metadata is left out of a restore, by exact name or
pattern: a user's file called `.neko-notes.txt` is data and is restored like any other
(backup.rs only_named_metadata_is_left_out_not_every_neko_name says the same)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from neko_common import backup_files, not_backup_data  # noqa: E402


class MetadataNameTests(unittest.TestCase):
    def test_only_named_metadata_is_left_out(self):
        root = Path('/usb')
        for here, name in ((root, '.neko-ledger.json'), (root, '.neko-ledger.lock'), (root, '.neko-ledger.json.1.tmp'),
                           (root, '.neko-ledger.json.unreadable-1'), (root / 'a', '.neko-names.json'),
                           (root / 'a', '.IMG.jpg.neko-latest'), (root / 'a', 'IMG.jpg.neko-part'),
                           (root / 'a', '.neko-123-456-7')):
            self.assertTrue(not_backup_data(here, root, name), name)
        for here, name in ((root, '.neko-notes.txt'), (root / 'a', '.neko-notes.txt'),
                           (root / 'a', '.neko-ledger.json'), (root / 'a', 'notes.neko-20261022T120000Z.txt')):
            self.assertFalse(not_backup_data(here, root, name), name)

    def test_a_users_neko_named_file_is_restored(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'alice/files').mkdir(parents=True)
            for name in ('alice/files/.neko-notes.txt', 'alice/files/a.txt', '.neko-ledger.json', 'alice/files/.neko-names.json'):
                (root / name).write_text('x')
            found = sorted(p.relative_to(root).as_posix() for p, _ in backup_files(root, root))
            self.assertEqual(found, ['alice/files/.neko-notes.txt', 'alice/files/a.txt'])


    def test_below_the_tree_root_nekoservers_root_names_are_ordinary_files(self):
        # A user's own folder named neko-damaged, at the top of their Nextcloud files.
        with tempfile.TemporaryDirectory() as d:
            files = Path(d) / 'alice/files'
            (files / 'neko-damaged').mkdir(parents=True)
            (files / 'neko-damaged/plan.txt').write_text('mine')
            (files / '.neko-ledger.json').write_text('mine too')
            found = sorted(p.relative_to(files).as_posix() for p, _ in backup_files(files))
            self.assertEqual(found, ['.neko-ledger.json', 'neko-damaged/plan.txt'])
            # At the tree's own root they are the backup's.
            self.assertEqual(backup_files(files, files), [])


class TrailingDotTests(unittest.TestCase):
    def test_a_name_ending_in_a_dot_gets_its_newest_version_back(self):
        import json

        import recovery_wizard
        with tempfile.TemporaryDirectory() as d:
            files = Path(d) / 'files'
            files.mkdir()
            (files / 'notes.').write_text('old')
            (files / 'notes.neko-20261020T010000Z.').write_text('new')
            (files / '.neko-names.json').write_text(json.dumps(
                {'format': 'neko-names-v1', 'names': {}, 'versions': {'notes.neko-20261020T010000Z.': 'notes.'}}))
            chosen = recovery_wizard.latest_versions([('files', files)])
            self.assertEqual([(p.name, str(t)) for p, t, _, _ in chosen], [('notes.neko-20261020T010000Z.', 'files/notes.')])


if __name__ == '__main__':
    unittest.main()
