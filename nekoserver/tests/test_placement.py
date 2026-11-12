"""0.50.0, free drive placement, step 1: the one table of what lives on which drive
(placement.py). A server set up before 0.50.0 gets its table worked out from the layout it
has, with no question and no change on disk (the coordinator's rule G6); the table of the
settings file is checked before it is trusted."""

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common  # noqa: E402
import placement  # noqa: E402

# The fixed configurations the golden files are made from (tests/test_golden_units.py).
spec = importlib.util.spec_from_file_location('golden_configurations', ROOT / 'tests/test_golden_units.py')
golden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(golden)

TODAY = {
    'core': {'install': 'ssd'},
    'photos': {'install': 'ssd', 'data': 'ssd'},
    'movies': {'install': 'ssd', 'data': 'media'},
    'music': {'install': 'ssd', 'data': 'ssd'},
    'cloud': {'install': 'ssd', 'data': 'media'},
    'backup': {'data': 'media'},
    'offline': {'data': 'usb'},
}


def configuration(name):
    return golden.CONFIGURATIONS[name][0]()


class DerivedTests(unittest.TestCase):
    """What every server before 0.50.0 has, written down as the table."""

    def test_the_shipped_layout_is_everything_on_the_ssd_but_films_cloud_files_and_backups(self):
        config = configuration('defaults')
        self.assertNotIn('placement', config)
        self.assertEqual(placement.table(config), TODAY)
        self.assertEqual(
            placement.drives(config),
            {
                'ssd': {'uuid': golden.SSD_UUID, 'fstype': 'ext4'},
                'media': {'uuid': golden.HDD_UUID, 'fstype': 'ext4'},
                'usb': {'uuid': golden.USB_UUID, 'fstype': 'ext4'},
            },
        )
        self.assertTrue(placement.is_default(config))

    def test_the_places_a_server_chose_itself_are_kept_and_no_usb_drive_is_none(self):
        config = configuration('media-drive')  # photos, music and cloud on the media drive, no USB
        table = placement.table(config)
        self.assertEqual([table[row]['data'] for row in ('photos', 'music', 'cloud')], ['media'] * 3)
        self.assertEqual(table['offline'], {'data': None})
        self.assertEqual(
            {row: table[row] for row in ('core', 'movies', 'backup')},
            {k: TODAY[k] for k in ('core', 'movies', 'backup')},
        )
        self.assertNotIn('usb', placement.drives(config))
        self.assertTrue(placement.is_default(config))

    def test_folders_a_nas_shares_are_drives_like_any_other(self):
        config = configuration('nfs-folders')
        drives = placement.drives(config)
        self.assertEqual(drives['media']['network']['export'], '192.168.1.50:/volume1/media')
        self.assertEqual(drives['usb']['network']['export'], '192.168.1.50:/volume1/backup')
        self.assertEqual(placement.table(config), TODAY)

    def test_every_golden_configuration_has_a_table_and_none_is_changed_by_reading_it(self):
        import copy

        for name in golden.CONFIGURATIONS:
            with self.subTest(configuration=name):
                config = configuration(name)
                before = copy.deepcopy(config)
                table = placement.table(config)
                self.assertEqual(set(table), {row for row, *_ in placement.ROWS})
                self.assertEqual(config, before, 'reading the table writes nothing')
                self.assertTrue(placement.is_default(config))
                for line in placement.said(config):
                    self.assertRegex(line, r'^[A-Z].*: (installed on|its|the backup|a copy)')


class RowTests(unittest.TestCase):
    def test_the_rows_are_the_groups_that_are_on_and_the_rest(self):
        config = configuration('defaults')
        self.assertEqual(placement.rows(config), ['core', 'photos', 'movies', 'music', 'cloud', 'backup', 'offline'])
        config['media_apps'] = {'photos': True, 'movies': False, 'music': False}
        self.assertEqual(placement.rows(config), ['core', 'photos', 'cloud', 'backup', 'offline'])
        config['nextcloud']['enabled'] = False
        self.assertEqual(placement.rows(config), ['core', 'photos', 'backup', 'offline'])
        self.assertEqual(placement.rows(configuration('no-media')), ['core', 'cloud', 'backup', 'offline'])

    def test_what_each_rows_installation_drive_holds(self):
        self.assertEqual(placement.apps_of('photos'), ['immich'])
        self.assertEqual(
            placement.apps_of('movies'), ['jellyfin', 'seerr', 'sonarr', 'radarr', 'prowlarr', 'qbittorrent']
        )
        self.assertEqual(placement.apps_of('music'), ['navidrome', 'lidarr'])
        self.assertEqual(placement.apps_of('cloud'), ['nextcloud'])
        self.assertEqual(placement.install_folders('photos'), ['compose/immich', 'appdata/immich'])
        self.assertIn('docker-engine', placement.install_folders('core'))
        # Every app is installed with exactly one row.
        placed = [app for row in ('photos', 'movies', 'music', 'cloud') for app in placement.apps_of(row)]
        self.assertEqual(sorted(placed), sorted(neko_common.APPS))

    def test_what_each_rows_data_is(self):
        config = configuration('defaults')
        self.assertEqual(placement.data_of(config, 'photos'), ('ssd', ['photos/immich-library']))
        self.assertEqual(
            placement.data_of(config, 'movies'), ('media', ['library/movies', 'library/tv', 'library/downloads'])
        )
        self.assertEqual(placement.data_of(config, 'music'), ('ssd', ['library/music', 'library/downloads/music']))
        self.assertEqual(placement.data_of(config, 'cloud'), ('media', ['cloud/files']))
        self.assertEqual(placement.data_of(config, 'backup'), ('media', ['backups', 'photos/archive']))
        self.assertTrue(placement.has('backup', 'data'))
        self.assertFalse(placement.has('backup', 'install'))
        self.assertFalse(placement.has('core', 'data'))
        with self.assertRaises(KeyError):
            placement.drive_of(config, 'backup', 'install')

    def test_what_is_on_each_drive(self):
        used = placement.used(configuration('defaults'))
        self.assertEqual(used['media'], [('movies', 'data'), ('cloud', 'data'), ('backup', 'data')])
        self.assertEqual(used['usb'], [('offline', 'data')])
        self.assertEqual(len(used['ssd']), 7)  # five installations, the photos and the music
        self.assertEqual(
            placement.said(configuration('media-drive'))[-2:],
            ['Backup: the backup copies on media', 'Offline backup (optional): a copy on a removable drive on none'],
        )


class SavedTableTests(unittest.TestCase):
    """A table in the settings file names drives listed there; anything else stops setup
    with the reason, before a folder is mounted from it."""

    def saved(self, **changes):
        config = configuration('defaults')
        config['drives'] = {**placement.derived_drives(config), 'd3': {'uuid': 'third-0001', 'fstype': 'xfs'}}
        config['placement'] = {**{row: dict(entry) for row, entry in TODAY.items()}, **changes}
        return config

    def test_a_row_placed_on_another_drive_is_read_as_written(self):
        config = self.saved(photos={'install': 'ssd', 'data': 'd3'}, offline={'data': None})
        self.assertEqual(placement.drive_of(config, 'photos', 'data'), 'd3')
        self.assertIsNone(placement.drive_of(config, 'offline', 'data'))
        self.assertFalse(placement.is_default(config))
        self.assertEqual(placement.used(config)['d3'], [('photos', 'data')])

    def test_a_table_that_cannot_be_trusted_stops_with_the_reason(self):
        cases = {
            'names the drive \'gone\', which is not under "drives"': {'photos': {'install': 'ssd', 'data': 'gone'}},
            'has no row "backup"': {'backup': 'media'},
            '"install" of "core" under "placement" names the drive None': {'core': {}},
        }
        for words, change in cases.items():
            with self.subTest(words=words), self.assertRaisesRegex(RuntimeError, words):
                placement.table(self.saved(**change))
        config = self.saved()
        config['placement'] = ['ssd']
        with self.assertRaisesRegex(RuntimeError, '"placement" in my-config.json must be an object'):
            placement.table(config)
        config = self.saved()
        config['drives'] = {'ssd': 'uuid'}
        with self.assertRaisesRegex(RuntimeError, '"drives" in my-config.json must be an object of drives'):
            placement.table(config)
        # Only the offline backup may be none.
        with self.assertRaisesRegex(RuntimeError, '"data" of "backup"'):
            placement.table(self.saved(backup={'data': None}))


class DataOnAnyDriveTests(unittest.TestCase):
    """Step 2: a row's data may be on any drive listed under "storage", by an explicit
    folder that setup hands to every job (not by a mount inside another drive's folder),
    and the films and shows have a place like the photos and the music."""

    def placed(self, **locations):
        config = configuration('defaults')
        config['storage']['d3'] = {'uuid': 'third-drive-0001', 'fstype': 'xfs'}
        config['locations'] = locations
        return config

    def test_the_films_have_a_place_and_it_is_the_media_drive_unless_placed_elsewhere(self):
        config = configuration('defaults')
        self.assertEqual(neko_common.movies_library(config), Path(config['media_mount']) / 'library')
        self.assertEqual(placement.table(config)['movies'], {'install': 'ssd', 'data': 'media'})
        moved = self.placed(movies='d3')
        self.assertEqual(neko_common.movies_library(moved), Path('/mnt/nekoserver-drives/d3/library'))
        self.assertEqual(placement.table(moved)['movies']['data'], 'd3')
        self.assertEqual(placement.drives(moved)['d3'], {'uuid': 'third-drive-0001', 'fstype': 'xfs'})

    def test_photos_music_and_cloud_files_follow_to_a_third_drive(self):
        config = self.placed(photos='d3', music='d3', cloud='d3')
        drive = Path('/mnt/nekoserver-drives/d3')
        self.assertEqual(neko_common.photo_library(config), drive / 'photos/immich-library')
        self.assertEqual(neko_common.music_library(config), drive / 'library/music')
        self.assertEqual(neko_common.music_downloads(config), drive / 'library/downloads/music')
        self.assertEqual(neko_common.cloud_files(config), drive / 'cloud/files')
        self.assertEqual(neko_common.drives_in_use(config), ['d3'])
        self.assertEqual(placement.used(config)['d3'], [('photos', 'data'), ('music', 'data'), ('cloud', 'data')])

    def test_a_place_that_is_no_drive_of_this_server_stops_with_the_reason(self):
        import config_checks

        for wrong in ('d4', '../etc', 'D3', 'usb'):
            config = self.placed(photos=wrong)
            with self.subTest(drive=wrong), self.assertRaisesRegex(RuntimeError, 'a drive under "storage"'):
                neko_common.files_root(config, 'photos')
        with self.assertRaisesRegex(RuntimeError, 'each "ssd", "media" or a drive under "storage"'):
            config_checks.validate(self.placed(photos='d4'), [])
        config_checks.validate(self.placed(photos='d3', movies='media'), [])
        # A name that could leave the drives' folder is never one, even when listed.
        config = self.placed()
        config['storage']['../x'] = {'uuid': 'u' * 12, 'fstype': 'ext4'}
        with self.assertRaisesRegex(RuntimeError, 'is not a drive under "storage"'):
            neko_common.drive_folder(config, '../x')

    def test_setup_mounts_only_the_drives_something_is_placed_on(self):
        from modules import storage

        unused = self.placed()
        self.assertEqual([spec[0] for spec in storage.mount_specs(unused)], ['data_ssd', 'media_hdd'])
        used = self.placed(movies='d3')
        specs = storage.mount_specs(used)
        self.assertEqual(specs[2], ('d3', 'third-drive-0001', 'xfs', '/mnt/nekoserver-drives/d3', None))
        # The same rules as the two: a filesystem of its own, a Linux one.
        clone = self.placed(movies='d3')
        clone['storage']['d3']['uuid'] = golden.HDD_UUID
        with self.assertRaisesRegex(RuntimeError, 'different UUIDs'):
            storage.mount_specs(clone)
        wrong = self.placed(movies='d3')
        wrong['storage']['d3']['fstype'] = 'ntfs'
        with self.assertRaisesRegex(RuntimeError, 'storage.d3.fstype must be ext4, xfs or btrfs'):
            storage.mount_specs(wrong)

    def test_every_job_is_told_the_folders_and_checks_the_drive(self):
        import native_runtime

        config = self.placed(photos='d3', movies='d3')
        told = native_runtime.storage_config(config)
        self.assertEqual(told['paths']['photos'], '/mnt/nekoserver-drives/d3/photos/immich-library')
        self.assertEqual(told['paths']['library'], '/mnt/nekoserver-drives/d3/library')
        self.assertEqual(told['storage']['d3'], {'uuid': 'third-drive-0001', 'fstype': 'xfs'})
        self.assertEqual(told['locations'], {'photos': 'd3', 'movies': 'd3'})
        # A server with the films where they always were is told what it always was.
        self.assertNotIn('library', native_runtime.storage_config(configuration('defaults'))['paths'])
        # The drive check every step makes covers the third drive.
        asked = []

        def ask(root, config, args):
            asked.append(args)
            return 0, 'ok'

        neko_common.verify_storage(config, ask=ask)
        self.assertEqual(asked[0][-1], 'data_ssd,media_hdd,d3')
        neko_common.verify_storage(configuration('defaults'), ask=ask)
        self.assertEqual(asked[1][-1], 'data_ssd,media_hdd')

    def test_the_apps_are_given_the_library_of_the_drive_it_is_on(self):
        from modules import media

        template = (ROOT / 'compose/sonarr.yaml').read_text()
        config = self.placed(movies='d3')
        films = neko_common.files_root(config, 'movies')
        rendered = media.render(template, '/srv/nekoserver', films, 1000, 1000, 'UTC', '127.0.0.1')
        self.assertIn('source: "/mnt/nekoserver-drives/d3/library"\n        target: /data\n', rendered)
        self.assertIn('- "/srv/nekoserver/appdata/sonarr:/config"', rendered)
        source = (ROOT / 'modules/media.py').read_text()
        self.assertIn("films = files_root(context.config, 'movies')", source)
        self.assertIn('values = (data, films, uid, gid, tz, bind, music)', source)

    def test_the_runtime_has_the_same_rules(self):
        common = (ROOT / 'runtime/src/common.rs').read_text()
        self.assertIn('pub const EXTRA_MOUNTS: &str = "/mnt/nekoserver-drives";', common)
        self.assertEqual(str(neko_common.EXTRA_MOUNTS), '/mnt/nekoserver-drives')
        guard = (ROOT / 'runtime/src/backup.rs').read_text()
        self.assertIn('for name in common::drives_in_use(&c) {', guard)


if __name__ == '__main__':
    unittest.main()
