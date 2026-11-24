"""Two small pieces of the guided restore (recovery_wizard): the date a restored file gets
back (original_date), and the files accepted as lost in the repair menu, which a restore
names instead of asking about as new damage (accepted_check)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard as wizard

CHANGED = 1_600_000_000_123_456_789  # 2020-09-13, in ns
MENU = ', accepted as lost in the recovery menu'  # how the repair menu (accept_lost) ends its reason


class OriginalDateTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        self.file = self.folder / 'IMG_0001.jpg'
        self.file.write_bytes(b'photo')

    def test_a_restored_file_gets_its_originals_date_back(self):
        wizard.original_date(self.file, None, CHANGED)
        info = self.file.stat()
        self.assertEqual((info.st_mtime_ns, info.st_atime_ns), (CHANGED, CHANGED))

    def test_through_the_open_restore_folder(self):
        folder = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)
        wizard.original_date('IMG_0001.jpg', folder, CHANGED)
        self.assertEqual(self.file.stat().st_mtime_ns, CHANGED)

    def test_with_no_date_noted_the_file_keeps_the_time_of_the_restore(self):
        before = self.file.stat().st_mtime_ns
        wizard.original_date(self.file, None, None)
        self.assertEqual(self.file.stat().st_mtime_ns, before)

    def test_a_link_gets_the_date_and_what_it_points_to_does_not(self):
        target = self.folder / 'elsewhere.txt'
        target.write_text('not part of the restore')
        before = target.stat().st_mtime_ns
        link = self.folder / 'link'
        link.symlink_to(target)
        wizard.original_date(link, None, CHANGED)
        self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(link.lstat().st_mtime_ns, CHANGED)

    def test_a_date_the_drive_cannot_hold_or_a_file_gone_leaves_things_as_they_are(self):
        before = self.file.stat().st_mtime_ns
        wizard.original_date(self.file, None, 10**30)  # OverflowError
        self.assertEqual(self.file.stat().st_mtime_ns, before)
        wizard.original_date(self.folder / 'gone.jpg', None, CHANGED)  # OSError


class AcceptedCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('/mnt/usb')
        self.checked = wizard.Checked(None)
        self.checked.root = self.root
        self.said = []

    def check(self, missing, listed, lost):
        self.checked.lost = lost
        return wizard.accepted_check(missing, self.checked, listed, self.said.append)

    def test_files_given_up_on_are_named_and_not_asked_about(self):
        listed = {
            'photos/a.jpg': ('a.jpg.20260101', 'x'),
            'photos/b.jpg': ('b.jpg', 'x'),
            'photos/c.jpg': ('c.jpg', 'x'),
        }
        lost = {
            # by the saved copy's name (beside the file) and by the file's own name
            'photos/a.jpg.20260101': 'damaged; kept in neko-damaged/20261101T020000Z' + MENU,
            'photos/c.jpg': 'missing' + MENU,
        }
        rest = self.check(['photos/a.jpg', 'photos/b.jpg', 'photos/c.jpg'], listed, lost)
        self.assertEqual(rest, ['photos/b.jpg'])
        self.assertEqual(
            self.checked.accepted,
            [
                (
                    'photos/a.jpg',
                    (
                        'damaged; kept in neko-damaged/20261101T020000Z; its damaged copy is still on the backup, '
                        'as it is: /mnt/usb/neko-damaged/20261101T020000Z/photos/a.jpg.20260101'
                    ),
                ),
                ('photos/c.jpg', 'missing; it was already missing'),
            ],
        )
        self.assertEqual(
            self.said,
            [
                (
                    'Not restored, as you accepted them as lost when fixing damaged backup copies: 2 files '
                    '(for example photos/a.jpg: damaged; kept in neko-damaged/20261101T020000Z; its damaged copy is '
                    'still on the backup, as it is: /mnt/usb/neko-damaged/20261101T020000Z/photos/a.jpg.20260101).'
                )
            ],
        )

    def test_one_file_is_said_as_one(self):
        self.check(['b/x.txt'], {'b/x.txt': ('x.txt', 'y')}, {'b/x.txt': 'missing' + MENU})
        self.assertEqual(
            self.said,
            [
                (
                    'Not restored, as you accepted them as lost when fixing damaged backup copies: 1 file '
                    '(for example b/x.txt: missing; it was already missing).'
                )
            ],
        )

    def test_nothing_accepted_says_nothing_and_keeps_every_file(self):
        rest = self.check(['b/x.txt', 'b/y.txt'], {'b/x.txt': ('x.txt',), 'b/y.txt': ('y.txt',)}, {})
        self.assertEqual(rest, ['b/x.txt', 'b/y.txt'])
        self.assertEqual(self.said, [])
        self.assertEqual(self.checked.accepted, [])

    def test_the_menus_reason_is_the_one_read_back(self):
        """accept_lost writes the reason this reads: its ending and the kept-aside folder."""
        source = (ROOT / 'recovery_wizard.py').read_text()
        self.assertIn("f'damaged; kept in {QUARANTINE}/{aside}') + ', accepted as lost in the recovery menu'", source)
        self.assertEqual(
            wizard.KEPT_ASIDE.search('damaged; kept in neko-damaged/20261101T020000Z, x')[1], '20261101T020000Z'
        )


if __name__ == '__main__':
    unittest.main()
