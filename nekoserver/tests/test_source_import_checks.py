"""What source_import's split-out steps must keep doing (0.52.2 mutation test, T6).

Each test pins one thing a person sees: the refusal of a settings file that is not JSON,
and the progress shown while files are copied (with a file that has a second name)."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source_import  # noqa: E402


class SettingsFileTests(unittest.TestCase):
    def test_a_settings_file_that_is_not_json_is_refused_before_it_is_read(self):
        """`--config my-config.txt` (a typo, or the wrong file) stops with what to give."""
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / 'my-config.txt'
            wrong.write_text('{}')
            said = io.StringIO()
            with contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
                code = source_import.main(['--config', str(wrong)])
        self.assertEqual(code, 2)
        self.assertEqual(
            said.getvalue().strip(),
            f'Source import stopped: the settings file must be a .json file; {wrong} is not one. Nothing was copied.',
        )


class CopyProgressTests(unittest.TestCase):
    """copy_entries with the copying itself left out: what it tells the progress bar."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / 'source'
        self.library = Path(tmp.name) / 'library'
        self.source.mkdir()
        self.library.mkdir()
        # One film with a second name in the source (a hard link there).
        self.film = self.source / 'film.mkv'
        self.film.write_bytes(b'x' * 1000)
        self.second = self.source / 'film-again.mkv'
        self.second.write_bytes(b'y' * 300)

    def copy(self, linked):
        """The progress calls when the second name is made a hard link (`linked`) or has
        to be copied, and the folders whose drive was asked about its inode numbers."""
        shown, asked = [], []
        first_dest = self.library / 'film.mkv'
        entries = [(self.film, first_dest, self.film.stat())]
        links = [(self.second, self.library / 'film-again.mkv', self.second.stat(), first_dest)]
        with (
            mock.patch.object(source_import, 'import_file'),
            mock.patch.object(source_import, 'room_for_one_more'),
            mock.patch.object(source_import, 'link_copy', return_value=linked),
            mock.patch.object(source_import, 'stable_inodes', side_effect=lambda p: asked.append(p) or True),
        ):
            source_import.copy_entries(entries, self.source, 0, 0, None, links, lambda *p: shown.append(p))
        return shown, asked

    def test_a_second_name_made_a_link_adds_no_bytes(self):
        shown, _ = self.copy(linked=True)
        self.assertEqual(shown, [(0, 0, 2, 1000), (1, 1000, 2, 1000), (2, 1000, 2, 1000)])

    def test_a_second_name_that_has_to_be_copied_adds_its_size_to_both(self):
        shown, _ = self.copy(linked=False)
        self.assertEqual(shown, [(0, 0, 2, 1000), (1, 1000, 2, 1000), (2, 1300, 2, 1300)])

    def test_the_source_drive_is_asked_whether_it_keeps_its_inode_numbers(self):
        """The check that a file did not change while it was copied trusts inode numbers
        only on a source drive that keeps them; it is the source that is asked, not the
        library."""
        _, asked = self.copy(linked=True)
        self.assertEqual(asked, [self.source])


if __name__ == '__main__':
    unittest.main()
