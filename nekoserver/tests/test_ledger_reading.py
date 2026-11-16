"""Deep check 302: the recovery menu read the whole checksum ledger as one JSON tree
(neko_common.read_ledger): 829 MB and 6.8 s at a million entries. It is read row by
row now, a piece of the file at a time, as the runtime does (ledger.rs). Its write path
(record_ledger) copies it row by row too (D10)."""

import json
import sys
import tempfile
import tracemalloc
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


class LedgerWritingTests(unittest.TestCase):
    """D10: record_ledger (the recovery menu's write path) still read the whole ledger
    (json.loads of the file) and wrote it whole; it is copied row by row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / neko_common.LEDGER_NAME
        for name in ('a.jpg', 'b.jpg', 'new.jpg'):
            (self.root / name).write_bytes(b'12345')

    def saved(self):
        return json.loads(self.path.read_text())

    def leftovers(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.endswith('.tmp'))

    def test_the_ledger_is_never_held_whole(self):
        rows = {f'photos/{n:06}.jpg': {'sha256': SHA, 'size': n, 'verified_at': 1.0, 'how': 'x'} for n in range(60000)}
        self.path.write_text(json.dumps({'entries': rows, 'format': 'neko-ledger-v1'}, separators=(',', ':')))
        size = self.path.stat().st_size
        del rows
        # Read in small pieces here, so the pieces themselves are not what is measured.
        tracemalloc.start()
        try:
            with mock.patch.object(neko_common._JsonMembers, 'PIECE', 1 << 16):
                added = neko_common.record_ledger(self.root, {'new.jpg': 'b' * 64}, 'chosen')
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(added, ['new.jpg'])
        self.assertLess(peak, size // 8, f'{peak} bytes held for a ledger of {size} bytes')
        saved = self.saved()
        self.assertEqual(len(saved['entries']), 60001)
        self.assertEqual(
            saved['entries']['photos/000007.jpg'], {'sha256': SHA, 'size': 7, 'verified_at': 1.0, 'how': 'x'}
        )
        self.assertEqual(saved['entries']['new.jpg']['size'], 5)
        self.assertEqual(neko_common.read_ledger(self.root)['new.jpg'], 'b' * 64)

    def test_its_rules_are_the_ones_it_had(self):
        record = neko_common.record_ledger
        # A first entry makes the ledger.
        self.assertEqual(record(self.root, {'a.jpg': SHA}, 'copied'), ['a.jpg'])
        first = self.saved()
        self.assertEqual(first['format'], 'neko-ledger-v1')
        self.assertEqual(first['entries']['a.jpg']['how'], 'copied')
        self.assertNotIn('retired', first)
        # An entry that is there is never replaced, and nothing is written for it.
        before = self.path.read_bytes()
        self.assertEqual(record(self.root, {'a.jpg': 'b' * 64}, 'again'), [])
        self.assertEqual(self.path.read_bytes(), before)
        # ... unless you settled it yourself; another one is added beside it.
        added = record(self.root, {'a.jpg': 'b' * 64, 'b.jpg': 'c' * 64}, 'chosen', replace=['a.jpg'])
        self.assertEqual(added, ['a.jpg', 'b.jpg'])
        self.assertEqual(neko_common.read_ledger(self.root), {'a.jpg': 'b' * 64, 'b.jpg': 'c' * 64})
        self.assertEqual(self.saved()['entries']['a.jpg']['how'], 'chosen')
        # Given up as lost: moved to "retired" with why; one that is not there moves nothing.
        moved = record(self.root, {}, 'lost', retire={'b.jpg': 'drive died', 'gone.jpg': 'x'})
        self.assertEqual(moved, ['b.jpg'])
        saved = self.saved()
        self.assertEqual(list(saved['entries']), ['a.jpg'])
        self.assertEqual(saved['retired']['b.jpg']['sha256'], 'c' * 64)
        self.assertEqual(saved['retired']['b.jpg']['why'], 'drive died')
        # What was retired before stays, as do the ledger's other parts, in any order.
        other_order = {
            'format': 'neko-ledger-v1',
            'note': [1],
            'retired': saved['retired'],
            'entries': saved['entries'],
        }
        self.path.write_text(json.dumps(other_order))
        self.assertEqual(record(self.root, {'new.jpg': SHA}, 'copied', retire={'a.jpg': 'lost'}), ['new.jpg', 'a.jpg'])
        saved = self.saved()
        self.assertEqual(list(saved['entries']), ['new.jpg'])
        self.assertEqual(sorted(saved['retired']), ['a.jpg', 'b.jpg'])
        self.assertEqual(saved['note'], [1])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.leftovers(), [])

    def test_an_untrusted_ledger_is_not_extended_and_stays_as_it_is(self):
        good = '{"entries":{"a.jpg":{"sha256":"' + SHA + '"}},"format":"neko-ledger-v1"}'
        for text, why in (
            (good[:-5], 'is unreadable'),
            (good.replace('a.jpg', '../a.jpg'), 'has an invalid entry'),
            (good.replace('neko-ledger-v1', 'other'), 'is unreadable'),
        ):
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(RuntimeError, why):
                neko_common.record_ledger(self.root, {'b.jpg': SHA}, 'copied')
            self.assertEqual(self.path.read_text(), text)
            self.assertEqual(self.leftovers(), [])
        with self.assertRaisesRegex(RuntimeError, 'Refusing an invalid checksum ledger entry'):
            neko_common.record_ledger(self.root, {'../b.jpg': SHA}, 'copied')


if __name__ == '__main__':
    unittest.main()
