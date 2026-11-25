"""1.0.1 storage from the dashboard: look.json (plans/1.0.1-contract.md §5), what the
Drives page shows before anything is done: every disk with its offer or the reason it is
never offered, each kind's size and where it may go, the old copies, and the disks an
array that lost a drive may take."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402
from tests.test_storage_refusals import TB4, disk, part, world  # noqa: E402

GB = 10**9
COPY = '0123456789abcdef'


class LookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('ssd', 'media', 'drives/d3'):
            (self.root / name).mkdir(parents=True)
        self.config = {
            'data_mount': str(self.root / 'ssd'),
            'media_mount': str(self.root / 'media'),
            'storage': {'data_ssd': {'uuid': 'ssd-1'}, 'media_hdd': {'uuid': 'hdd-1'}, 'd3': {'uuid': 'd3-uuid'}},
            'locations': {},
        }
        self.free = {}
        for patch in (
            mock.patch('neko_common.EXTRA_MOUNTS', self.root / 'drives'),
            # A drive is there while its folder is (T1-18: the real check asks findmnt).
            mock.patch('neko_common.mounted_uuid', lambda folder: '' if Path(folder).is_dir() else None, create=True),
            mock.patch.object(storage_jobs, 'FACTS', {'min_bytes': 16 * GB}),
            # Each folder of the test is a drive of its own, with the room the test gives it.
            mock.patch.object(storage_jobs, 'same_drive', lambda a, b: Path(a) == Path(b)),
            mock.patch.object(storage_jobs, 'free_bytes', lambda folder: self.free.get(Path(folder).name, 100 * GB)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def look(self, *disks, sizes=None, arrays=None):
        sizes = sizes or {}
        measure = lambda paths: sum(sizes.get(str(p), 0) for p in paths)  # noqa: E731
        return storage_jobs.look(self.config, world(*disks), measure=measure, arrays=arrays or {})

    def test_each_disk_says_what_it_offers_and_what_is_on_it(self):
        ext4 = disk('sdc', serial='S-AAAA', children=[part('sdc1', fstype='ext4', label='old', uuid='u-c', size=TB4)])
        ntfs = disk('sdd', serial='S-BBBB', children=[part('sdd1', fstype='ntfs', label='Photos', fsused=12 * 10**11)])
        system = disk('sda', serial='S-CCCC', children=[part('sda1', mountpoints=['/'])])
        shown = {d['dev']: d for d in self.look(ext4, ntfs, system)['disks']}
        self.assertEqual(shown['sdc']['offer'], 'take_over')
        self.assertEqual(shown['sdc']['fs'], {'type': 'ext4', 'label': 'old', 'uuid': 'u-c', 'used_bytes': None})
        self.assertEqual(shown['sdd']['offer'], 'erase')
        self.assertEqual(shown['sdd']['on_it'], '1 partition: NTFS "Photos" (1.2 TB used)')
        self.assertEqual(shown['sdd']['confirm_words'], '4 TB BBBB')
        self.assertIsNone(shown['sda']['offer'])
        self.assertEqual(shown['sda']['refused']['code'], 'system')

    def test_a_disk_with_two_filesystems_can_only_be_erased(self):
        two = disk(
            children=[part('sdc1', fstype='ext4', size=TB4 // 2), part('sdc2', fstype='xfs', size=TB4 // 2)],
        )
        self.assertEqual(self.look(two)['disks'][0]['offer'], 'erase')
        efi = disk(children=[part('sdc1', fstype='vfat', size=512 * 2**20), part('sdc2', fstype='btrfs', size=TB4)])
        self.assertEqual(self.look(efi)['disks'][0]['offer'], 'take_over')

    def test_a_blank_disk(self):
        self.assertEqual(self.look(disk())['disks'][0]['on_it'], 'Nothing: the disk is blank.')

    def test_each_kind_has_its_size_and_the_places_it_may_go(self):
        photos = str(self.root / 'ssd/photos/immich-library')
        self.free.update(d3=400 * GB, media=1000 * GB)
        shown = self.look(sizes={photos: 412 * GB})
        self.assertEqual(shown['sizes']['photos']['bytes'], 412 * GB)
        to = {t['to']: t for t in shown['targets']['photos']}
        self.assertEqual(sorted(to), ['d3', 'media'])
        self.assertTrue(to['media']['ok'])
        self.assertEqual(to['d3']['why_not'], 'Not enough room: needs 433 GB, 400 GB free.')
        self.assertEqual(to['d3']['words'], 'Extra drive d3')

    def test_backups_never_go_to_the_apps_drive_and_an_unplugged_drive_is_said(self):
        (self.root / 'drives/d3').rmdir()
        shown = self.look()
        to = {t['to']: t for t in shown['targets']['backups']}
        self.assertEqual(
            to['ssd']['why_not'], 'Your backups are copies of the Apps drive: they must be on another drive.'
        )
        self.assertEqual(
            to['d3']['why_not'], f"Extra drive d3 is not connected (nothing is mounted at {self.root / 'drives/d3'})."
        )

    def test_the_backups_place_is_media_unless_the_settings_say(self):
        self.assertEqual(storage_jobs.place_of(self.config, 'backups'), 'media')
        self.config['locations']['backups'] = 'd3'
        self.assertEqual(storage_jobs.place_of(self.config, 'backups'), 'd3')
        self.assertEqual(storage_jobs.kind_folders('backups'), ('backups', 'photos/archive'))

    def test_old_copies_are_found_on_every_drive_with_their_delete_words(self):
        (self.root / 'ssd' / f'nekoserver-old-photos-{COPY}').mkdir()
        (self.root / 'ssd' / 'nekoserver-old-photos-xyz').mkdir()  # not a job's id
        (self.root / 'drives/d3' / f'nekoserver-old-movies-{COPY}').mkdir()
        copies = self.look()['old_copies']
        self.assertEqual([(c['kind'], c['place']) for c in copies], [('photos', 'ssd'), ('movies', 'd3')])
        self.assertEqual(copies[1]['delete_words'], 'delete films and shows')
        self.assertEqual(copies[1]['place_words'], 'Extra drive d3')

    def test_an_extra_drive_may_be_given_up_only_when_nothing_is_on_it(self):
        d3 = disk(children=[part('sdc1', fstype='ext4', uuid='d3-uuid', size=TB4)])
        shown = self.look(d3)['disks'][0]
        self.assertEqual((shown['drive'], shown['can_release'], shown['refused']['code']), ('d3', True, 'settings'))
        self.config['locations']['photos'] = 'd3'
        shown = self.look(d3)['disks'][0]
        self.assertEqual((shown['can_release'], shown['why_not']), (False, 'Your photos are on it. Move them first.'))
        self.config['locations'] = {}
        (self.root / 'drives/d3' / f'nekoserver-old-music-{COPY}').mkdir()
        self.assertEqual(self.look(d3)['disks'][0]['why_not'], 'An old copy is on it. Delete it or move it back first.')

    def test_an_array_that_lost_a_drive_is_offered_the_disks_big_enough(self):
        small = disk('sdc', serial='S-AAAA', size=GB * 100)
        big = disk('sdd', serial='S-BBBB', size=TB4)
        offers = self.look(small, big, arrays={'md-md0': 2 * 10**12})['mirror_offers']['md-md0']
        self.assertEqual([o['ok'] for o in offers], [False, True])
        self.assertEqual(offers[0]['words'], 'It is too small: at least 2 TB is needed.')


if __name__ == '__main__':
    unittest.main()
