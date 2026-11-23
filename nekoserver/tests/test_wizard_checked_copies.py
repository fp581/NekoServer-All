"""The guided restore's checksum checking (recovery_wizard.Checked and its helpers): which
saved copies count as checked, left out or given up on, where a listed file's content is
found again, what the restore's closing summary says, and the NOT-RESTORED.txt list it
writes into the restore folder."""

import gzip
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard as wizard


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def ledger(self, root, entries, **rest):
        root.mkdir(parents=True, exist_ok=True)
        value = {'entries': {k: {'sha256': v} for k, v in entries.items()}, 'format': 'neko-ledger-v1', **rest}
        (root / wizard.LEDGER_NAME).write_text(json.dumps(value))


class SmallHelperTests(TempCase):
    def test_a_snapshot_made_while_steam_was_open_says_so(self):
        run = self.tmp / 'run'
        run.mkdir()
        self.assertIs(wizard.steam_left_out(run), False)  # no manifest at all
        (run / wizard.MANIFEST).write_text('{}')
        self.assertIs(wizard.steam_left_out(run), False)
        (run / wizard.MANIFEST).write_text('{"left_out": true}')
        self.assertIs(wizard.steam_left_out(run), True)

    def test_password_lists_named_in_seconds_are_read_as_microseconds(self):
        self.assertEqual(wizard.run_time('1700000000'), 1_700_000_000_000_000)
        self.assertEqual(wizard.run_time(10**12 - 1), (10**12 - 1) * 1_000_000)
        # From 10**12 on the name is already in microseconds.
        self.assertEqual(wizard.run_time(10**12), 10**12)
        self.assertEqual(wizard.run_time(2 * 10**12), 2 * 10**12)
        self.assertEqual(wizard.run_time(5 * 10**12), 5 * 10**12)

    def test_sizes_are_said_as_people_say_them(self):
        mib, gib, tib = 1 << 20, 1 << 30, 1 << 40
        self.assertEqual(wizard.size_words(3 * tib + tib // 2), '3.5 TB')
        self.assertEqual(wizard.size_words(tib), '1.0 TB')
        self.assertEqual(wizard.size_words(tib - 1), '1024.0 GB')
        self.assertEqual(wizard.size_words(3 * gib), '3.0 GB')
        self.assertEqual(wizard.size_words(gib), '1.0 GB')
        self.assertEqual(wizard.size_words(gib - mib), '1023 MB')
        self.assertEqual(wizard.size_words(5 * mib), '5 MB')
        self.assertEqual(wizard.size_words(mib), '1 MB')
        self.assertEqual(wizard.size_words(mib - 1), 'less than 1 MB')
        self.assertEqual(wizard.size_words(0), 'less than 1 MB')

    def test_a_new_folder_is_on_the_drive_of_the_nearest_folder_that_exists(self):
        self.assertEqual(wizard.standing_folder(self.tmp / 'restore' / 'photos'), self.tmp)
        self.assertEqual(wizard.standing_folder(self.tmp), self.tmp)

    def test_only_plain_names_count_as_left_off_the_usb_drive(self):
        (self.tmp / wizard.LEFT_OUT['file']).write_text(
            json.dumps({'format': wizard.LEFT_OUT['format'], 'files': ['photos/a:b.jpg', '../etc/passwd', '/abs', 5]})
        )
        self.assertEqual(wizard.left_out(self.tmp), {'photos/a:b.jpg'})

    def test_only_plain_names_with_a_reason_count_as_accepted_as_lost(self):
        self.ledger(
            self.tmp,
            {},
            retired={
                'photos/a.jpg': {'why': 'missing'},
                '../outside': {'why': 'missing'},
                'photos/b.jpg': 'not a row',
                'photos/c.jpg': {'why': 3},
            },
        )
        self.assertEqual(wizard.retired_copies(self.tmp), {'photos/a.jpg': 'missing'})


class RecordedCopyTests(unittest.TestCase):
    def test_without_a_checksum_the_copy_beside_the_file_and_its_recorded_checksum(self):
        sums = {'p/a.jpg.v1': 's1'}
        self.assertEqual(wizard.recorded_copy('p/a.jpg', 'a.jpg.v1', None, sums), ('p/a.jpg.v1', 's1'))

    def test_with_a_checksum_nothing_recorded_yet_keeps_the_copy_beside_the_file(self):
        self.assertEqual(wizard.recorded_copy('p/a.jpg', 'a.jpg.v1', 'new', {}), ('p/a.jpg.v1', 'new'))
        sums = {'p/a.jpg.v1': 'new', 'q/other': 'new'}
        self.assertEqual(wizard.recorded_copy('p/a.jpg', 'a.jpg.v1', 'new', sums), ('p/a.jpg.v1', 'new'))

    def test_a_name_that_holds_other_content_now_points_to_the_copy_with_that_checksum(self):
        sums = {'p/a.jpg.v1': 'old', 'z/moved': 'new', 'q/moved': 'new'}
        self.assertEqual(wizard.recorded_copy('p/a.jpg', 'a.jpg.v1', 'new', sums), ('q/moved', 'new'))

    def test_no_copy_ever_held_it(self):
        sums = {'p/a.jpg.v1': 'old'}
        self.assertEqual(wizard.recorded_copy('p/a.jpg', 'a.jpg.v1', 'new', sums), (None, None))


class LocatedTests(TempCase):
    def put(self, key, data):
        path = self.tmp / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_a_recorded_checksum_is_trusted_without_reading_the_file(self):
        self.put('p/a.jpg', b'what is there now')
        located = wizard.Located(self.tmp, {'p/a.jpg': 'recorded'})
        self.assertIs(located.holds('p/a.jpg', 'recorded'), True)
        self.assertIs(located.holds('p/a.jpg', sha(b'what is there now')), False)

    def test_an_unrecorded_copy_is_read_and_compared(self):
        self.put('p/a.jpg', b'photo')
        located = wizard.Located(self.tmp, {})
        self.assertIs(located.holds('p/a.jpg', sha(b'photo')), True)
        self.assertIs(located.holds('p/a.jpg', sha(b'other')), False)
        self.assertIs(located.holds('p/gone.jpg', sha(b'photo')), False)

    def test_a_copy_that_cannot_be_read_does_not_hold_it(self):
        self.put('p/a.jpg', b'photo')
        located = wizard.Located(self.tmp, {})
        with mock.patch.object(wizard, 'sha256_file', side_effect=OSError('read error')):
            self.assertIs(located.holds('p/a.jpg', sha(b'photo')), False)

    def test_the_copy_beside_the_file_is_found_by_its_content(self):
        self.put('u/files/p/a.jpg.v2', b'photo')
        located = wizard.Located(self.tmp, {})
        self.assertEqual(located.find('u/files/p/a.jpg', 'a.jpg.v2', sha(b'photo')), 'u/files/p/a.jpg.v2')
        self.assertIsNone(located.find('u/files/p/a.jpg', 'a.jpg.v2', sha(b'other')))

    def test_a_copy_moved_aside_under_the_same_name_is_preferred(self):
        """The copy beside the file is gone; of the saved copies with that checksum, the one
        that kept the name comes first, even when another sorts before it."""
        content = sha(b'photo')
        sums = {'a/x.other': content, 'top.jpg': content, 'z/a.jpg.v2': content}
        for key in sums:
            self.put(key, b'photo')
        located = wizard.Located(self.tmp, sums)
        self.assertEqual(located.find('u/files/p/a.jpg', 'a.jpg.v2', content), 'z/a.jpg.v2')

    def test_without_one_of_the_same_name_the_first_by_name(self):
        content = sha(b'photo')
        sums = {'b/x.other': content, 'a/y.other': content}
        for key in sums:
            self.put(key, b'photo')
        located = wizard.Located(self.tmp, sums)
        self.assertEqual(located.find('u/files/p/a.jpg', 'a.jpg.v2', content), 'a/y.other')


class CheckedCountingTests(TempCase):
    def test_every_checked_copy_is_counted(self):
        checked = wizard.Checked(None)
        for n in range(3):
            source = self.tmp / f'src{n}'
            source.write_bytes(b'photo %d' % n)
            checked.copy(source, self.tmp / f'out{n}', sha(b'photo %d' % n))
        self.assertEqual((checked.verified, checked.unverified), (3, 0))

    def test_every_unchecked_copy_is_counted(self):
        checked = wizard.Checked(None)
        for n in range(3):
            source = self.tmp / f'src{n}'
            source.write_bytes(b'photo')
            checked.copy(source, self.tmp / f'out{n}')
        self.assertEqual((checked.verified, checked.unverified), (0, 3))

    def test_damaged_copies_count_as_restored_too(self):
        """They are written (into damaged-as-is): the stop message counts them."""
        checked = wizard.Checked(None)
        checked.verified, checked.unverified, checked.damaged = 2, 1, ['p/a.jpg']
        self.assertEqual(checked.restored(), 4)

    def test_the_restore_folder_is_made_for_its_owner_only(self):
        checked = wizard.Checked(None)
        destination = self.tmp / 'restore'
        old = os.umask(0o022)
        try:
            checked.begin(destination)
        finally:
            os.umask(old)
        self.addCleanup(os.close, checked.target.fd)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual(checked.destination, destination)

    def test_with_nowhere_to_publish_nothing_counts_as_handed_to_the_repair(self):
        checked = wizard.Checked(None)
        checked.publish('missing', {'p/a.jpg': None})
        self.assertIs(checked.reported, False)
        checked.report = lambda kind, copies: True
        checked.publish('missing', {'p/a.jpg': None})
        self.assertIs(checked.reported, True)


class AcceptedAndPresentTests(TempCase):
    def test_given_up_by_the_files_name_or_by_its_saved_copys_name(self):
        checked = wizard.Checked(None)
        checked.lost = {'photos/2026/a.jpg.v1': 'by copy', 'photos/2026/b.jpg': 'by name', 'photos/2026/b.jpg.v1': 'x'}
        self.assertEqual(checked.accepted_as_lost('photos/2026/a.jpg', 'a.jpg.v1'), 'by copy')
        self.assertEqual(checked.accepted_as_lost('photos/2026/b.jpg', 'b.jpg.v1'), 'by name')
        self.assertIsNone(checked.accepted_as_lost('photos/2026/c.jpg', 'c.jpg.v1'))

    def test_files_gone_from_a_folder_are_found_after_a_folder_from_elsewhere(self):
        usb = self.tmp / 'usb'
        self.ledger(usb, {'photos/a.jpg': '0' * 64, 'photos/b.jpg': '1' * 64, 'music/c.mp3': '2' * 64})
        (usb / 'photos').mkdir()
        (usb / 'photos' / 'a.jpg').write_bytes(b'a')
        checked = wizard.Checked(usb)
        sources = [('elsewhere', self.tmp / 'other'), ('photos', usb / 'photos')]
        self.assertEqual(checked.require_present(sources), ['photos/b.jpg'])

    def test_photos_the_usb_drive_cannot_hold_are_named_apart(self):
        checked = wizard.Checked(None)
        checked.kept_off = ['p/a.jpg', 'p/b.jpg', 'p/c.jpg', 'p/d.jpg']
        self.assertEqual(
            checked.kept_off_note(),
            (
                "4 files (for example p/a.jpg, p/b.jpg, p/c.jpg) are not on this USB drive: its filesystem (exFAT) "
                "can't store their names. They are safe on the HDD: restore them from the HDD's weekly copy."
            ),
        )
        checked.kept_off = ['p/a.jpg']
        self.assertEqual(
            checked.kept_off_note(),
            (
                "1 file (for example p/a.jpg) is not on this USB drive: its filesystem (exFAT) can't store their "
                "names. They are safe on the HDD: restore them from the HDD's weekly copy."
            ),
        )


class SummaryTests(TempCase):
    def begun(self):
        checked = wizard.Checked(None, 'the HDD')
        destination = self.tmp / 'restore'
        checked.begin(destination)
        self.addCleanup(os.close, checked.target.fd)
        return checked, destination

    def test_all_checked(self):
        checked = wizard.Checked(None)
        checked.verified = 3
        self.assertEqual(checked.summary(), 'Checksums matched for 3 files.')

    def test_what_did_not_come_back_is_said_and_listed(self):
        checked, destination = self.begun()
        checked.damaged, checked.missing = ['p/a.jpg'], ['p/b.jpg']
        checked.accepted = [('p/c.jpg', 'missing; it was already missing')]
        self.assertEqual(
            checked.summary(destination),
            (
                'Checksums matched for 0 files. Not restored (1 file): you accepted them as lost when fixing '
                'damaged backup copies. NOT restored (2 files), because this backup has them damaged (1) or no '
                'longer has them (1). Get them from the HDD, if it has them. '
                f'{wizard.FIX_COPIES} fixes damaged or missing ones from a copy that still matches. The damaged '
                f'copies are kept as they are in {destination}/damaged-as-is (they may still open in part; '
                f'nothing checked them). Their names are in {destination}/NOT-RESTORED.txt.'
            ),
        )
        self.assertEqual(
            (destination / 'NOT-RESTORED.txt').read_text(),
            'damaged: p/a.jpg\nmissing: p/b.jpg\naccepted as lost: p/c.jpg (missing; it was already missing)\n',
        )

    def test_missing_ones_handed_to_the_repair_and_no_list_without_a_begun_restore(self):
        checked = wizard.Checked(None)
        checked.report = lambda kind, copies: True
        checked.publish('missing', {'p/b.jpg': None})
        checked.verified, checked.missing = 1, ['p/b.jpg']
        destination = self.tmp / 'restore'
        self.assertEqual(
            checked.summary(destination),
            (
                'Checksums matched for 1 file. NOT restored (1 file), because this backup has them damaged (0) or '
                f'no longer has them (1). They are now listed for {wizard.FIX_COPIES}, which fixes them from a copy '
                'that still matches.'
            ),
        )
        self.assertFalse(destination.exists())

    def test_an_unusable_checksum_list_and_files_left_out_are_said(self):
        usb = self.tmp / 'usb'
        usb.mkdir()
        (usb / wizard.LEDGER_NAME).write_text('{"entries": {')
        checked = wizard.Checked(usb)
        checked.uncertain = ['u/files/x']
        self.assertEqual(
            checked.summary(),
            (
                "Checksums matched for 0 files. This backup's checksum list could not be used (The checksum ledger "
                f'{usb}/.neko-ledger.json is unreadable), so no file was checked. Left out (1 file): '
                f'{wizard.UNCERTAIN_WHY}'
            ),
        )


class WriteListTests(TempCase):
    def begun(self):
        checked = wizard.Checked(None)
        destination = self.tmp / 'restore'
        checked.begin(destination)
        self.addCleanup(os.close, checked.target.fd)
        checked.missing = ['p/b.jpg']
        return checked, destination

    def test_no_list_without_a_begun_restore(self):
        checked = wizard.Checked(None)
        checked.missing = ['p/b.jpg']
        self.assertIs(checked.write_list(), False)

    def test_no_list_once_the_restore_folder_is_not_the_one_it_began_in(self):
        checked, destination = self.begun()
        destination.rename(self.tmp / 'moved')
        destination.mkdir()
        self.assertIs(checked.write_list(), False)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list((self.tmp / 'moved').iterdir()), [])

    def test_the_list_is_written_for_its_owner_only(self):
        checked, destination = self.begun()
        old = os.umask(0o022)
        try:
            self.assertIs(checked.write_list(), True)
        finally:
            os.umask(old)
        listed = destination / 'NOT-RESTORED.txt'
        self.assertEqual(stat.S_IMODE(listed.stat().st_mode), 0o600)
        self.assertEqual(listed.read_text(), 'missing: p/b.jpg\n')

    def test_a_list_already_there_is_written_over_whole(self):
        checked, destination = self.begun()
        (destination / 'NOT-RESTORED.txt').write_text('x' * 200)
        self.assertIs(checked.write_list(), True)
        self.assertEqual((destination / 'NOT-RESTORED.txt').read_text(), 'missing: p/b.jpg\n')

    def test_a_link_put_in_the_lists_place_is_never_followed(self):
        checked, destination = self.begun()
        outside = self.tmp / 'outside.txt'
        outside.write_text('not part of the restore')
        (destination / 'NOT-RESTORED.txt').symlink_to(outside)
        with self.assertRaises(OSError):
            checked.write_list()
        self.assertEqual(outside.read_text(), 'not part of the restore')


class InventoryTests(TempCase):
    STAMP = '20261101T020000Z'

    def inventory(self, name, value):
        folder = self.tmp / wizard.INVENTORY_DIR
        folder.mkdir(exist_ok=True)
        path = folder / name
        with gzip.open(path, 'wb') as stream:
            stream.write(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def note(self, **fields):
        (self.tmp / wizard.INVENTORY_DIR).mkdir(exist_ok=True)
        value = {'format': 'neko-last-run-v1', 'at': self.STAMP, **fields}
        (self.tmp / wizard.INVENTORY_DIR / wizard.LAST_RUN).write_text(json.dumps(value))

    def test_a_listed_saved_copy_is_a_plain_name_beside_the_file(self):
        good = '0' * 64
        self.assertEqual(wizard.inventory_entry({'copy': 'a.jpg', 'sha256': good}), ('a.jpg', good))
        self.assertEqual(wizard.inventory_entry('a.jpg'), ('a.jpg', None))
        self.assertIsNone(wizard.inventory_entry({'copy': 'p/a.jpg', 'sha256': good}))
        self.assertIsNone(wizard.inventory_entry('..'))
        self.assertIsNone(wizard.inventory_entry({'copy': 5, 'sha256': good}))

    def test_a_list_right_at_the_size_limit_is_still_read(self):
        raw = json.dumps({'format': 'neko-inventory-v1', 'files': {'p/a.jpg': 'a.jpg'}}).encode()
        path = self.inventory('20261101T020000Z.json.gz', raw)
        self.assertEqual(wizard.INVENTORY_LIMIT, 1 << 30)
        with mock.patch.object(wizard, 'INVENTORY_LIMIT', len(raw)):
            self.assertEqual(wizard.read_inventory(path), {'p/a.jpg': ('a.jpg', None)})
        with mock.patch.object(wizard, 'INVENTORY_LIMIT', len(raw) - 1):
            self.assertIsNone(wizard.read_inventory(path))

    def test_a_newest_run_without_its_list_is_found(self):
        listed = [(self.tmp / 'L.json.gz', 'then')]
        when = wizard.readable(self.STAMP)
        self.note(state='pending', list='L.json.gz')
        self.assertEqual(wizard.list_gap(self.tmp, listed), when)
        self.note(state='complete', list='older.json.gz')
        self.assertEqual(wizard.list_gap(self.tmp, listed), when)
        self.note(list='L.json.gz')  # no state: complete
        self.assertIsNone(wizard.list_gap(self.tmp, listed))
        self.note(state='pending', list='L.json.gz', at=5)
        self.assertIsNone(wizard.list_gap(self.tmp, listed))

    def test_a_note_put_there_as_a_link_is_not_read(self):
        (self.tmp / wizard.INVENTORY_DIR).mkdir()
        elsewhere = self.tmp / 'elsewhere.json'
        elsewhere.write_text(json.dumps({'format': 'neko-last-run-v1', 'at': self.STAMP, 'state': 'pending'}))
        (self.tmp / wizard.INVENTORY_DIR / wizard.LAST_RUN).symlink_to(elsewhere)
        self.assertIsNone(wizard.list_gap(self.tmp, [(self.tmp / 'L.json.gz', 'then')]))

    def offered(self, listed):
        """The keys of the "Which files?" choices restore_scope offers."""
        with mock.patch.object(wizard, 'choose', return_value=None) as choose:
            self.assertIsNone(wizard.restore_scope(listed, None, lambda *_: None))
        return [key for key, _ in choose.call_args[0][1]]

    def test_one_good_list_offers_it_and_everything_but_no_date_choice(self):
        files = {'format': 'neko-inventory-v1', 'files': {'p/a.jpg': 'a.jpg'}}
        listed = [(self.inventory('20261101T020000Z.json.gz', files), 'Nov 1')]
        self.assertEqual(self.offered(listed), ['last', 'everything'])

    def test_a_damaged_newest_list_is_not_offered_again_as_everything(self):
        files = {'format': 'neko-inventory-v1', 'files': {'p/a.jpg': 'a.jpg'}}
        listed = [
            (self.inventory('20261101T020000Z.json.gz', b'not json'), 'Nov 1'),
            (self.inventory('20261001T020000Z.json.gz', files), 'Oct 1'),
        ]
        self.assertEqual(self.offered(listed), ['everything', 'older', 'date'])

    def test_only_the_earlier_choice_restores_as_of_the_earlier_list(self):
        listed = [(Path('new'), 'Nov 1'), (Path('old'), 'Oct 1')]
        earlier = (Path('old'), 'Oct 1')
        asking = (None, None)
        self.assertEqual(wizard.scope_picked('earlier', listed, None, earlier, asking), ('as_of', Path('old'), 'Oct 1'))
        self.assertEqual(wizard.scope_picked('everything', listed, None, earlier, asking), ('everything', None, None))
        self.assertIsNone(wizard.scope_picked(None, listed, None, earlier, asking))


class NewestVersionTests(TempCase):
    def test_the_newest_version_is_said_with_its_date(self):
        path = self.tmp / 'photo.neko-20261020T010000Z.jpg'
        group = {Path('p/photo.jpg'): [(('20261020T010000Z', 0), path, 'photo.jpg')]}
        with mock.patch.object(wizard, 'version_groups', return_value=group):
            chosen = wizard.latest_versions([])
        self.assertEqual(chosen, [(path, Path('p/photo.jpg'), wizard.readable('20261020T010000Z'), 1)])

    def test_only_files_with_several_versions_are_named(self):
        for name in ('a', 'b'):
            (self.tmp / name).write_bytes(b'x')
        chosen = [
            (self.tmp / 'a', Path('p/a.jpg'), 'first backup', 1),
            (self.tmp / 'b', Path('p/b.jpg'), 'Oct 20', 3),
        ]
        said = []
        checked = wizard.Checked(None)
        self.assertEqual(wizard.copy_latest([], self.tmp / 'restore', said.append, checked, chosen), 2)
        os.close(checked.target.fd)
        self.assertEqual(
            said,
            [
                '1 file had more than one version; you get its newest one:',
                '  p/b.jpg: version from Oct 20 (3 versions on the backup)',
            ],
        )


class FilesReviewTests(unittest.TestCase):
    def files(self, missing, other):
        checked = wizard.Checked(None, other)
        checked.missing = missing
        return wizard.Files([], checked)

    def test_what_this_backup_lost_is_said_with_where_else_it_may_be(self):
        self.assertEqual(
            self.files(['p/a.jpg', 'p/b.jpg'], 'the HDD').missing(),
            (
                '2 files backed up and checked before are not on this backup any more (for example p/a.jpg, '
                'p/b.jpg), and cannot come back from it. The HDD may still have them.'
            ),
        )
        self.assertEqual(
            self.files(['p/a.jpg'], 'the HDD').missing(),
            (
                '1 file backed up and checked before is not on this backup any more (for example p/a.jpg), and '
                'cannot come back from it. The HDD may still have it.'
            ),
        )
        self.assertEqual(
            self.files(['p/a.jpg'], None).missing(),
            (
                '1 file backed up and checked before is not on this backup any more (for example p/a.jpg), and '
                'cannot come back from it.'
            ),
        )

    def test_an_unapproved_restore_says_where_the_missing_files_are(self):
        self.assertEqual(
            self.files(['p/a.jpg'], 'the HDD').not_restored(),
            f'Nothing was restored. For the missing files: use the HDD instead, or choose {wizard.FIX_COPIES}.',
        )


if __name__ == '__main__':
    unittest.main()
