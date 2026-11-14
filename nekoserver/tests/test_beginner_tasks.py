"""Deep checks 241-260: whole tasks a beginner does, followed only by the docs and the
product's own messages ("I deleted a movie by mistake", "I forgot the backup password")."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_wizard  # noqa: E402


def doc(name):
    return (ROOT / name).read_text()


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class DeletedFilmTests(unittest.TestCase):
    """Deep check 253: a film deleted by mistake. The recovery menu listed photos, Nextcloud
    files, passwords and settings, and said nothing about films: the beginner looked for a
    line that is never there, and RECOVERY.md's "Which situation are you in?" had no row."""

    def said(self, config):
        out = []
        recovery_wizard.restore_section(config, answers('0'), out.append)
        return '\n'.join(out)

    def test_the_restore_list_says_films_and_music_are_not_backed_up(self):
        said = self.said({'media_mount': '/nonexistent/hdd', 'data_mount': '/nonexistent/ssd'})
        self.assertIn('Films, shows and music are not backed up, so they are not in this list.', said)
        self.assertIn('Radarr or Sonarr', said)
        # Said before the list, where it is read.
        self.assertLess(said.index('not backed up'), said.index('What would you like to restore?'))

    def test_a_server_without_films_or_music_is_not_told_about_them(self):
        config = {
            'media_mount': '/nonexistent/hdd',
            'data_mount': '/nonexistent/ssd',
            'media_apps': {'photos': True, 'movies': False, 'music': False},
        }
        self.assertNotIn('Films, shows and music', self.said(config))

    def test_recovery_md_has_a_row_for_a_deleted_film(self):
        table = doc('RECOVERY.md').split('## Which situation are you in?')[1].split('\n## ')[0]
        row = next(line for line in table.splitlines() if 'deleted a film' in line)
        self.assertIn('not backed up', row)


if __name__ == '__main__':
    unittest.main()
