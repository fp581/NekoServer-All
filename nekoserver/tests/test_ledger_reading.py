"""Deep check 302: the recovery menu read the whole checksum ledger as one JSON tree
(neko_common.read_ledger): 829 MB and 6.8 s at a million entries. It is read row by
row now, a piece of the file at a time, as the runtime does (ledger.rs)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common

SHA = 'a' * 64


class LedgerReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def ledger(self, text):
        (self.root / neko_common.LEDGER_NAME).write_text(text)
        return neko_common.read_ledger(self.root)

    def test_the_file_is_never_read_whole(self):
        rows = {f'photos/{n}.jpg': {'sha256': SHA, 'size': n, 'verified_at': 1.0, 'how': 'x'} for n in range(50)}
        text = json.dumps({'entries': rows, 'format': 'neko-ledger-v1', 'retired': {}})
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('read whole')):
            self.assertEqual(self.ledger(text), {rel: SHA for rel in rows})

    def test_every_cut_between_pieces_reads_the_same(self):
        """Pieces of 1 to 9 characters: every key, row and number is cut somewhere."""
        rows = {'a "quoted" é/b.jpg': {'sha256': SHA, 'size': 12345}, 'c.jpg': {'sha256': 'b' * 64, 'size': 0}}
        for text in (
            json.dumps({'entries': rows, 'format': 'neko-ledger-v1'}),  # Python's ", " and ": "
            json.dumps({'format': 'neko-ledger-v1', 'retired': {'x': [1, 2]}, 'entries': rows}, indent=2),
            json.dumps({'entries': rows, 'format': 'neko-ledger-v1'}, separators=(',', ':')),  # the runtime's
        ):
            for piece in range(1, 10):
                with self.subTest(piece=piece), mock.patch.object(neko_common._JsonMembers, 'PIECE', piece):
                    self.assertEqual(self.ledger(text), {'a "quoted" é/b.jpg': SHA, 'c.jpg': 'b' * 64})
        self.assertEqual(self.ledger('{"entries": {}, "format": "neko-ledger-v1"}'), {})

    def test_a_damaged_or_untrusted_ledger_is_refused_as_before(self):
        good = '{"entries":{"a.jpg":{"sha256":"' + SHA + '"}},"format":"neko-ledger-v1"}'
        for text in (
            good[:-5],  # cut short
            good.replace('"format":"neko-ledger-v1"', '"format":"other"'),
            good.replace('}},', '}} ,,'),
            '[]',
            '{"format":"neko-ledger-v1"}',  # no entries
            '{"entries":[],"format":"neko-ledger-v1"}',
            good + 'x',
        ):
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'is unreadable'):
                self.ledger(text)
        for text in (good.replace('a.jpg', '../a.jpg'), good.replace(SHA, 'zz')):
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, 'has an invalid entry'):
                self.ledger(text)


if __name__ == '__main__':
    unittest.main()
