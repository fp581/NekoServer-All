"""Setup's drive check covers the drives the runtime's guard watches (coordinator, after
T2's 8d62a8c): a drive that holds only a folder the Media folders page made (part C) is
checked too, so another disk in its place is refused at setup's re-run as it is at a
job's start. neko_common.drives_watched follows common::drives_watched: the drives in use,
then those holding only a made folder."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neko_common  # noqa: E402


def config(**more):
    base = {
        'data_mount': '/srv/nekoserver-data',
        'media_mount': '/mnt/nekoserver-media',
        'storage': {
            'data_ssd': {'uuid': 's'},
            'media_hdd': {'uuid': 'h'},
            'd3': {'uuid': 'd3-uuid'},
            'd4': {'uuid': 'd4-uuid'},
        },
        'locations': {'photos': 'd3'},
        'placement': {
            'movies': {'extra': [{'path': '/mnt/nekoserver-drives/d4/media/movies', 'made': 'd4'}]},
            'music': {'extra': [{'path': '/nas/music', 'use': 'in_place'}]},
            'tv': {'extra': [{'path': '/mnt/nekoserver-drives/d9/media/tv', 'made': 'd9'}]},
        },
    }
    base.update(more)
    return base


class WatchedTests(unittest.TestCase):
    def test_a_drive_with_only_a_made_folder_is_watched_after_the_ones_in_use(self):
        self.assertEqual(neko_common.drives_in_use(config()), ['d3'])
        self.assertEqual(neko_common.drives_watched(config()), ['d3', 'd4'])

    def test_a_made_folder_on_a_drive_in_use_is_not_listed_twice_and_no_unknown_drive_counts(self):
        cfg = config(locations={'photos': 'd4'})
        self.assertEqual(neko_common.drives_watched(cfg), ['d4'])
        self.assertEqual(neko_common.drives_watched({'storage': {}, 'placement': 'x'}), [])

    def test_setups_drive_check_asks_the_runtime_about_them(self):
        asked = []

        def ask(folder, cfg, args):
            asked.append(args[args.index('--drives') + 1])
            return 0, ''

        neko_common.verify_storage(config(), ask=ask)
        self.assertEqual(asked, ['data_ssd,media_hdd,d3,d4'])


if __name__ == '__main__':
    unittest.main()
