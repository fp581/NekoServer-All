"""T1-20 (4), 1.0.1 drill 4: films and shows own library/downloads, and the music's
downloads are library/downloads/music, inside it (qBittorrent's music downloads must be on
the music's drive, for Lidarr). After the music moved back to the media drive, the films'
move back was refused: "library/downloads is in the way". Each kind now owns its own
folders only: another kind's folder inside one is never copied, compared, retired or moved
with it, and never counts as in the way. Every order of moves and moves back ends with
each file where its kind is."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402
from tests.test_storage_jobs import Jobs, last_result  # noqa: E402

FILES = {
    'movies': {
        'library/movies/film.mkv': 'film',
        'library/tv/show.mkv': 'show',
        'library/downloads/complete/x.iso': 'dl',
    },
    'music': {'library/music/song.flac': 'song', 'library/downloads/music/new.flac': 'new'},
}


class OverlapTests(Jobs):
    def setUp(self):
        super().setUp()
        self.config['locations'] = {'music': 'media', 'movies': 'media'}
        for path in self.settings:
            path.write_text(json.dumps(self.config))
        for files in FILES.values():
            self.files(self.root / 'media', **files)

    def step(self, job, kind, **fields):
        code = self.run_job(job, kind=kind, **fields)
        self.assertEqual(code, 0, last_result(self.root))
        self.config = self.saved()

    def move(self, kind, to):
        self.step('move', kind, to=to)

    def back(self, kind):
        found = [c for c in storage_jobs.old_copies(self.config, measure=lambda p: 0) if c['kind'] == kind]
        self.assertEqual(len(found), 1, found)
        self.step('back', kind, copy=found[0]['copy'])
        # The old copy left where they were is deleted, so the next move back finds one.
        left = [c for c in storage_jobs.old_copies(self.config, measure=lambda p: 0) if c['kind'] == kind]
        for copy in left:
            __import__('shutil').rmtree(copy['path'])

    def where(self):
        """Each file at the place of its kind, and nowhere else."""
        for kind, files in FILES.items():
            root = storage_jobs.root_of(self.config, storage_jobs.place_of(self.config, kind))
            for rel, text in files.items():
                self.assertEqual((root / rel).read_text(), text, f'{kind}: {root / rel}')
            for place in ('ssd', 'media', 'd3'):
                other = storage_jobs.root_of(self.config, place)
                if other != root:
                    for rel in files:
                        self.assertFalse((other / rel).exists(), f'{kind} left at {other / rel}')

    def test_t1_20_music_then_films_then_back_in_the_same_order(self):
        self.move('music', 'd3')
        self.where()
        self.move('movies', 'd3')
        self.where()
        self.back('music')
        self.where()
        self.back('movies')
        self.where()

    def test_films_then_music_then_back_in_the_same_order(self):
        self.move('movies', 'd3')
        self.where()
        self.move('music', 'd3')
        self.where()
        self.back('movies')
        self.where()
        self.back('music')
        self.where()

    def test_music_then_films_back_in_the_other_order(self):
        self.move('music', 'd3')
        self.move('movies', 'd3')
        self.back('movies')
        self.where()
        self.back('music')
        self.where()

    def test_films_then_music_back_in_the_other_order(self):
        self.move('movies', 'd3')
        self.move('music', 'd3')
        self.back('music')
        self.where()
        self.back('movies')
        self.where()

    def test_the_films_moved_away_leave_the_music_downloads_in_place(self):
        self.move('movies', 'd3')
        self.assertEqual((self.root / 'media/library/downloads/music/new.flac').read_text(), 'new')
        self.assertFalse((self.root / 'drives/d3/library/downloads/music').exists())
        old = [c for c in storage_jobs.old_copies(self.config, measure=lambda p: 0) if c['kind'] == 'movies'][0]
        self.assertFalse((Path(old['path']) / 'library/downloads/music').exists())

    def test_films_moved_onto_the_music_drive_are_not_in_its_way(self):
        self.move('music', 'd3')
        self.move('movies', 'd3')
        self.assertEqual((self.root / 'drives/d3/library/downloads/music/new.flac').read_text(), 'new')
        self.assertEqual((self.root / 'drives/d3/library/downloads/complete/x.iso').read_text(), 'dl')

    def test_each_kind_names_only_its_own_folders(self):
        self.assertEqual(storage_jobs.inner_names('movies', 'library/downloads'), ('music',))
        self.assertEqual(storage_jobs.inner_names('music', 'library/downloads/music'), ())


if __name__ == '__main__':
    unittest.main()
