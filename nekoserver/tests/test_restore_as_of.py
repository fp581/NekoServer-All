"""Restoring an add-only backup "as of" a backup: only the files you had then."""
import gzip
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard

EDIT = 'keep.neko-20261101T030000Z.txt'


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class RestoreAsOfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        (self.root / 'ssd/installer').mkdir(parents=True)
        self.config = {'media_mount': str(self.media), 'data_mount': str(self.root / 'ssd'),
                       'photos': {'archive_dir': 'photos/archive'}}
        self.tree = self.root / 'usb/nextcloud'
        # What the USB drive holds after two weekly runs. Run 1: keep.txt, gone.txt and
        # old-name.txt. Run 2: gone.txt deleted, old-name.txt renamed to new-name.txt,
        # keep.txt edited (its new content is kept as a dated version, as the copy does).
        files = self.tree / 'alice/files'
        for name, body in (('keep.txt', b'first'), (EDIT, b'edited'), ('gone.txt', b'gone'),
                           ('old-name.txt', b'renamed'), ('new-name.txt', b'renamed')):
            self.write(files / name, body)
        self.write(files / '.keep.txt.neko-latest', EDIT.encode())
        self.write(files / '.neko-names.json', json.dumps(
            {'format': 'neko-names-v1', 'names': {}, 'versions': {EDIT: 'keep.txt'}}).encode())
        self.inventory('20261025T030000Z', {'alice/files/keep.txt': 'keep.txt', 'alice/files/gone.txt': 'gone.txt',
                                            'alice/files/old-name.txt': 'old-name.txt'})
        self.inventory('20261101T030000Z', {'alice/files/keep.txt': EDIT, 'alice/files/new-name.txt': 'new-name.txt'})

    def write(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def inventory(self, stamp, files):
        """The same format the runtime writes (inventory.rs)."""
        self.write(self.tree / '.neko-inventory' / f'{stamp}.json.gz', gzip.compress(json.dumps(
            {'format': 'neko-inventory-v1', 'made_at': stamp, 'files': files}).encode()))

    def restore(self, *choices):
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / 'usb'), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers(*choices), out.append)
        restored = next((self.media / 'restored').iterdir()) / 'files'
        return {p.name: p.read_bytes() for p in restored.iterdir()}, '\n'.join(out)

    def test_as_of_the_last_backup_restores_only_the_live_set(self):
        got, out = self.restore('1', 'yes', '1')
        self.assertEqual(got, {'keep.txt': b'edited', 'new-name.txt': b'renamed'})
        self.assertIn('As of the last backup (2026-11-01 03:00 UTC)', out)
        self.assertIn('Restoring the 2 files you had', out)

    def test_as_of_a_chosen_date(self):
        got, _ = self.restore('1', 'yes', '2', '1')
        self.assertEqual(got, {'keep.txt': b'first', 'gone.txt': b'gone', 'old-name.txt': b'renamed'})

    def test_everything_ever_kept_is_still_there(self):
        got, _ = self.restore('1', 'yes', '3', 'yes')
        self.assertEqual(got, {'keep.txt': b'edited', 'gone.txt': b'gone', 'old-name.txt': b'renamed',
                               'new-name.txt': b'renamed'})

    def test_checked_against_the_ledger_and_a_lost_file_is_named(self):
        entries = {'alice/files/' + EDIT: {'sha256': hashlib.sha256(b'damaged?').hexdigest(), 'size': 6,
                                           'verified_at': 1.0, 'how': 'copied'}}
        self.write(self.tree / '.neko-ledger.json', json.dumps({'format': 'neko-ledger-v1', 'entries': entries}).encode())
        (self.tree / 'alice/files/new-name.txt').unlink()
        got, out = self.restore('1', 'yes', '1', 'yes')
        self.assertEqual(got, {})
        listed = next((self.media / 'restored').iterdir()) / 'NOT-RESTORED.txt'
        self.assertEqual(listed.read_bytes(), b'damaged: alice/files/' + EDIT.encode()
                         + b'\nmissing: alice/files/new-name.txt\n')
        self.assertIn('2 files were NOT restored', out)

    def test_a_list_that_cannot_be_read_offers_everything(self):
        for path in (self.tree / '.neko-inventory').iterdir():
            path.write_bytes(b'not gzip')
        got, out = self.restore('1', 'yes', '1', 'yes')
        self.assertIn('could not be read, so everything ever kept is offered', out)
        self.assertEqual(len(got), 4)

    def test_a_backup_without_lists_behaves_as_before_and_says_so(self):
        for path in list((self.tree / '.neko-inventory').iterdir()):
            path.unlink()
        got, out = self.restore('1', 'yes', 'yes')
        self.assertIn(recovery_wizard.NO_INVENTORY, out)
        self.assertEqual(len(got), 4)

    def test_a_list_with_unsafe_paths_is_not_trusted(self):
        bad = self.tree / 'bad.json.gz'
        for files in ({'../etc/passwd': 'passwd'}, {'alice/files/a.txt': '../b'}, {'a': 1}):
            bad.write_bytes(gzip.compress(json.dumps({'format': 'neko-inventory-v1', 'files': files}).encode()))
            self.assertIsNone(recovery_wizard.read_inventory(bad))
        bad.write_bytes(gzip.compress(json.dumps({'format': 'other', 'files': {}}).encode()))
        self.assertIsNone(recovery_wizard.read_inventory(bad))
        self.assertIsNone(recovery_wizard.read_inventory(self.tree / 'missing.json.gz'))

    def test_lists_are_newest_first_also_two_in_one_second(self):
        self.inventory('20261101T030000Z-1', {})
        names = [p.name for p, _ in recovery_wizard.inventories(self.tree)]
        self.assertEqual(names, ['20261101T030000Z-1.json.gz', '20261101T030000Z.json.gz', '20261025T030000Z.json.gz'])


if __name__ == '__main__':
    unittest.main()
