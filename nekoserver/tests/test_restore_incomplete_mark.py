"""1.0 round T1-R15: a recovery-menu restore killed part-way (kill -9, a power cut) left a
folder under restored/ with only some of the files, no sign that it was incomplete (only
Ctrl+C says so, and a kill cannot print), and the next menu run did not mention it. The
restore now marks its folder as incomplete while it writes, and the menu, when it starts,
names any folder still marked, with the words Ctrl+C uses."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard  # noqa: E402


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class IncompleteMarkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.media = Path(tmp.name) / 'hdd'
        self.home = self.media / 'restored'
        self.home.mkdir(parents=True)
        self.config = {'media_mount': str(self.media), 'data_mount': str(Path(tmp.name) / 'ssd')}
        self.target = self.home / 'nextcloud-neko-20261124-110719'

    def marks(self):
        return sorted(p.name for p in self.home.iterdir() if not p.is_dir())

    def test_the_folder_is_marked_while_it_is_written_and_unmarked_when_it_is_done(self):
        with recovery_wizard.part_way(self.target, print):
            self.target.mkdir()
            # A kill here runs no more code: the mark must already be on the drive.
            self.assertEqual(self.marks(), ['nextcloud-neko-20261124-110719-INCOMPLETE.txt'])
            mark = self.home / self.marks()[0]
            self.assertIn('stopped part-way', mark.read_text())
            self.assertIn(f'sudo rm -r "{self.target}"', mark.read_text())
        self.assertEqual(self.marks(), [])

    def test_a_restore_stopped_by_ctrl_c_keeps_its_mark(self):
        with self.assertRaises(KeyboardInterrupt), recovery_wizard.part_way(self.target, lambda line: None):
            self.target.mkdir()
            raise KeyboardInterrupt
        self.assertEqual(self.marks(), ['nextcloud-neko-20261124-110719-INCOMPLETE.txt'])

    def test_a_restore_ended_by_an_error_keeps_its_mark_and_one_that_returns_early_does_not(self):
        with self.assertRaises(OSError), recovery_wizard.part_way(self.target, print):
            self.target.mkdir()
            raise OSError('No space left on device')
        self.assertEqual(self.marks(), ['nextcloud-neko-20261124-110719-INCOMPLETE.txt'])
        other = self.home / 'photos-neko-20261124-120000'

        def returns_from_inside():
            with recovery_wizard.part_way(other, print):
                other.mkdir()
                return 65

        self.assertEqual(returns_from_inside(), 65)  # as Chosen.copy returns its count
        self.assertEqual(self.marks(), ['nextcloud-neko-20261124-110719-INCOMPLETE.txt'])

    def test_the_next_menu_run_names_a_marked_folder_with_the_words_ctrl_c_uses(self):
        def killed():
            with recovery_wizard.part_way(self.target, lambda line: None):
                self.target.mkdir()
                (self.target / 'one-of-65.txt').write_text('x')
                raise SystemExit  # stands in for the kill: nothing after it runs

        with self.assertRaises(SystemExit):
            killed()
        finished = self.home / 'photos-neko-20261124-090000'
        finished.mkdir()
        out = []
        self.assertEqual(recovery_wizard.menu_loop(self.config, answers('0'), out.append), 0)
        said = '\n'.join(out)
        self.assertIn(recovery_wizard.PART_WAY.format(target=self.target).strip(), said)
        self.assertNotIn(str(finished), said)
        # Nothing is deleted by the menu, and no question is added: '0' alone quit it.
        self.assertTrue((self.target / 'one-of-65.txt').is_file())
        self.assertEqual(self.marks(), ['nextcloud-neko-20261124-110719-INCOMPLETE.txt'])

    def test_a_menu_without_marks_or_without_a_restore_folder_says_nothing_of_them(self):
        out = []
        recovery_wizard.menu_loop(self.config, answers('0'), out.append)
        self.home.rmdir()
        recovery_wizard.menu_loop(self.config, answers('0'), out.append)
        self.assertNotIn('part-way', '\n'.join(out))

    def test_a_mark_whose_folder_was_deleted_as_told_is_not_named_and_goes_too(self):
        self.target.mkdir()
        (self.home / 'nextcloud-neko-20261124-110719-INCOMPLETE.txt').write_text('x')
        self.target.rmdir()  # sudo rm -r "<folder>", as the words say
        out = []
        recovery_wizard.menu_loop(self.config, answers('0'), out.append)
        self.assertNotIn('part-way', '\n'.join(out))
        self.assertEqual(self.marks(), [])  # it marked nothing any more


if __name__ == '__main__':
    unittest.main()
