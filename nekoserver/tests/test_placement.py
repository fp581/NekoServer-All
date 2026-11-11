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
            placement.apps_of('movies'), ['jellyfin', 'seerr', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'qbittorrent']
        )
        self.assertEqual(placement.apps_of('music'), ['navidrome'])
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


if __name__ == '__main__':
    unittest.main()
