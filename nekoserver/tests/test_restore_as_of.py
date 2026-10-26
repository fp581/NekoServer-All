"""Restoring an add-only backup "as of" a backup: only the files you had then."""
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
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


def zone(name):
    """Show dates in the time zone `name` until the test ends (the menu shows local time)."""
    patched = mock.patch.dict(os.environ, {'TZ': name})
    patched.start()
    time.tzset()
    return lambda: (patched.stop(), time.tzset())


class Restoring(unittest.TestCase):
    """A server whose USB drive holds a Nextcloud backup tree (filled by each test class)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The dates said below are this computer's own time: UTC here.
        self.addCleanup(zone('UTC'))
        # The HDD counts as mounted here (the restore folder is suggested on it).
        ready = mock.patch.object(recovery_wizard, 'hdd_ready', return_value=True)
        ready.start()
        self.addCleanup(ready.stop)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        (self.root / 'ssd/installer').mkdir(parents=True)
        self.config = {'media_mount': str(self.media), 'data_mount': str(self.root / 'ssd'),
                       'photos': {'archive_dir': 'photos/archive'}}
        self.tree = self.root / 'usb/nextcloud'

    def write(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def restore(self, *choices):
        """Every restored file (its path under files/) with its content, and what was said."""
        # An earlier restore in the same second would get the same folder name.
        shutil.rmtree(self.media / 'restored', ignore_errors=True)
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / 'usb'), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers(*choices), out.append)
        restored = next((self.media / 'restored').iterdir()) / 'files'
        got = {p.relative_to(restored).as_posix(): p.read_bytes() for p in restored.rglob('*') if p.is_file()}
        return got, '\n'.join(out)


class RestoreAsOfTests(Restoring):
    def setUp(self):
        super().setUp()
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

    def inventory(self, stamp, files):
        """The same format the runtime writes (inventory.rs)."""
        self.write(self.tree / '.neko-inventory' / f'{stamp}.json.gz', gzip.compress(json.dumps(
            {'format': 'neko-inventory-v1', 'made_at': stamp, 'files': files}).encode()))

    def test_as_of_the_last_backup_restores_only_the_live_set(self):
        got, out = self.restore('1', 'yes', '1')
        self.assertEqual(got, {'keep.txt': b'edited', 'new-name.txt': b'renamed'})
        self.assertIn('As of the last backup (2026-11-01 03:00 UTC)', out)
        self.assertIn('Restoring the 2 files you had', out)

    def last_run(self, at, listed, state=None):
        self.write(self.tree / '.neko-inventory/last-run.json', json.dumps(
            {'format': 'neko-last-run-v1', 'at': at, 'state': state or ('complete' if listed else 'pending'),
             'list': listed}).encode())

    def test_a_run_cut_off_after_copying_files_makes_the_menu_warn(self):
        # The run noted itself as pending, copied later.txt and was cut off before any list.
        self.write(self.tree / 'alice/files/later.txt', b'later')
        self.last_run('20261108T030000Z', None, 'pending')
        got, out = self.restore('1', 'yes', '1', 'yes')
        self.assertIn('did not finish: it was cut off part-way', out)
        self.assertNotIn('As of the last backup', out)
        self.assertEqual(got['later.txt'], b'later')
        # A note that says pending is never taken as complete, whatever list it names.
        self.last_run('20261108T030000Z', '20261101T030000Z.json.gz', 'pending')
        _, out = self.restore('1', 'yes', '1', 'yes')
        self.assertIn('did not finish', out)

    def test_a_newest_backup_whose_list_failed_is_not_offered_as_the_last_backup(self):
        # The newest run copied later.txt, then its list could not be written.
        self.write(self.tree / 'alice/files/later.txt', b'later')
        self.last_run('20261108T030000Z', None)
        got, out = self.restore('1', 'yes', '1', 'yes')
        self.assertNotIn('As of the last backup', out)
        self.assertIn('The newest backup (2026-11-08 03:00 UTC) did not finish', out)
        self.assertIn('The newest complete list is from 2026-11-01 03:00 UTC and leaves out', out)
        self.assertIn('1. Everything ever kept, also files you deleted or renamed (recommended', out)
        self.assertIn('As of 2026-11-01 03:00 UTC (an older list', out)
        self.assertEqual(got['later.txt'], b'later')
        # The older list is still there, by its date.
        got, _ = self.restore('1', 'yes', '2')
        self.assertNotIn('later.txt', got)

    def test_a_normal_run_still_recommends_its_own_list(self):
        self.last_run('20261101T030000Z', '20261101T030000Z.json.gz')
        got, out = self.restore('1', 'yes', '1')
        self.assertIn('As of the last backup (2026-11-01 03:00 UTC)', out)
        self.assertEqual(got, {'keep.txt': b'edited', 'new-name.txt': b'renamed'})
        self.assertNotIn('has no list', out)

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
        self.assertIn('NOT restored (2 files)', out)

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

    def test_backup_dates_are_shown_in_this_computer_s_own_time(self):
        # UP-11: a household in Israel was shown UTC (03:00 for its 05:00 backup).
        self.addCleanup(zone('Asia/Jerusalem'))
        self.assertEqual(recovery_wizard.readable('20261101T030000Z'), '2026-11-01 05:00 IST')
        self.assertEqual(recovery_wizard.when(1793502000 * 10**6), '2026-11-01 05:00 IST')
        _, out = self.restore('1', 'yes', '1')
        self.assertIn('As of the last backup (2026-11-01 05:00 IST)', out)

    def test_lists_are_newest_first_also_two_in_one_second(self):
        self.inventory('20261101T030000Z-1', {})
        names = [p.name for p, _ in recovery_wizard.inventories(self.tree)]
        self.assertEqual(names, ['20261101T030000Z-1.json.gz', '20261101T030000Z.json.gz', '20261025T030000Z.json.gz'])



def sha(body):
    return hashlib.sha256(body).hexdigest()


class FileFolderFileTests(Restoring):
    """A file `Projects`, then a folder `Projects`, then a file `Projects` again, across
    three weekly copies, laid out as the backup's move logic leaves it (backup.rs
    move_aside): what stood in the way is moved into alice/neko-replaced/<when>/ with its
    checksum, and the new one takes the old name. An older date must restore the content
    it had then, never today's file under the same name."""
    def setUp(self):
        super().setUp()
        self.saved = {
            'alice/neko-replaced/20261101T030000Z/Projects': b'OLD CONTENT',            # run 1's file
            'alice/neko-replaced/20261108T030000Z/Projects/notes.txt': b'notes',        # run 2's folder
            'alice/files/Projects': b'NEW CONTENT',                                     # run 3's file
        }
        for key, body in self.saved.items():
            self.write(self.tree / key, body)
        self.ledger({key: sha(body) for key, body in self.saved.items()})

    def ledger(self, sums):
        entries = {k: {'sha256': v, 'size': 1, 'verified_at': 1.0, 'how': 'copied'} for k, v in sums.items()}
        self.write(self.tree / '.neko-ledger.json', json.dumps({'format': 'neko-ledger-v1', 'entries': entries}).encode())

    def lists(self, version):
        """The three runs' lists, as the runtime writes them (v2 with checksums, v1 without)."""
        runs = (('20261025T030000Z', {'alice/files/Projects': ('Projects', b'OLD CONTENT')}),
                ('20261101T030000Z', {'alice/files/Projects/notes.txt': ('notes.txt', b'notes')}),
                ('20261108T030000Z', {'alice/files/Projects': ('Projects', b'NEW CONTENT')}))
        for stamp, files in runs:
            listed = {k: ({'copy': c, 'sha256': sha(b)} if version == 2 else c) for k, (c, b) in files.items()}
            self.write(self.tree / '.neko-inventory' / f'{stamp}.json.gz', gzip.compress(json.dumps(
                {'format': f'neko-inventory-v{version}', 'made_at': stamp, 'files': listed}).encode()))

    def test_each_date_restores_the_content_it_had_then(self):
        self.lists(2)
        # Newest first: the last backup, then (choosing a date) the second and the first.
        last, _ = self.restore('1', 'yes', '1')
        self.assertEqual(last, {'Projects': b'NEW CONTENT'})
        second, _ = self.restore('1', 'yes', '2', '1')
        self.assertEqual(second, {'Projects/notes.txt': b'notes'})
        first, out = self.restore('1', 'yes', '2', '2')
        self.assertEqual(first, {'Projects': b'OLD CONTENT'})
        self.assertIn('Checksums matched for 1 file', out)
        self.assertNotIn('NOT restored', out)

    def test_content_no_saved_copy_has_is_missing_never_another_file(self):
        self.lists(2)
        (self.tree / 'alice/neko-replaced/20261101T030000Z/Projects').unlink()
        first, out = self.restore('1', 'yes', '2', '2', 'yes')
        self.assertEqual(first, {})
        self.assertIn('NOT restored (1 file)', out)

    def test_a_saved_copy_whose_content_changed_is_not_taken_for_the_listed_one(self):
        # The ledger and the copy agree with each other but not with the list: the list's
        # checksum decides, so today's file is never handed out as the old one.
        self.lists(2)
        self.ledger({'alice/files/Projects': sha(b'NEW CONTENT')})
        (self.tree / 'alice/neko-replaced').rename(self.root / 'aside')
        first, _ = self.restore('1', 'yes', '2', '2', 'yes')
        self.assertNotIn(b'NEW CONTENT', first.values())

    def test_an_older_list_without_checksums_leaves_such_a_file_out(self):
        self.lists(1)
        first, out = self.restore('1', 'yes', '2', '2', 'yes')
        self.assertEqual(first, {})
        self.assertIn('can\'t be told', out)
        self.assertIn('Left out (1 file): they became a folder', out)
        # A file that was never replaced still restores from such a list.
        self.write(self.tree / 'alice/files/plain.txt', b'plain')
        stamp = self.tree / '.neko-inventory/20261025T030000Z.json.gz'
        stamp.write_bytes(gzip.compress(json.dumps({'format': 'neko-inventory-v1', 'files': {
            'alice/files/Projects': 'Projects', 'alice/files/plain.txt': 'plain.txt'}}).encode()))
        first, _ = self.restore('1', 'yes', '2', '2', 'yes')
        self.assertEqual(first, {'plain.txt': b'plain'})

    def test_lists_mixing_formats_or_with_bad_checksums_are_not_trusted(self):
        bad = self.tree / 'bad.json.gz'
        for kind, files in (('neko-inventory-v2', {'alice/files/a': 'a'}),
                            ('neko-inventory-v1', {'alice/files/a': {'copy': 'a', 'sha256': sha(b'a')}}),
                            ('neko-inventory-v2', {'alice/files/a': {'copy': 'a', 'sha256': 'abc'}}),
                            ('neko-inventory-v2', {'alice/files/a': {'copy': '../a', 'sha256': sha(b'a')}})):
            self.write(bad, gzip.compress(json.dumps({'format': kind, 'files': files}).encode()))
            self.assertIsNone(recovery_wizard.read_inventory(bad), (kind, files))


if __name__ == '__main__':
    unittest.main()
