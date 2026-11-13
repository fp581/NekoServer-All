"""The offline backup drive (gate runs B4 and C): a drive too small for it says so, a drive
the storage page just chose for files or installs is not offered for it too, no drive is
asked for a weekly copy that has nothing to take, and "none for now" can be undone
without editing the settings file."""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
from modules import photo_backup, storage  # noqa: E402


def usb_row(size, uuid='usb-0001'):
    row = drive_table.entry({'name': 'sdd1', 'size': size, 'fstype': 'exfat', 'uuid': uuid}, {'name': 'sdd'})
    row['backup'] = {'uuid': uuid}
    return row


class OfflineDriveTests(unittest.TestCase):
    def test_a_drive_under_16_gb_is_too_small_and_says_so(self):
        self.assertIn('too small', drive_table.verdict(usb_row('8G'), 'usb', {}))
        self.assertIn('at least 16 GB', drive_table.verdict(usb_row('8G'), 'usb', {}))
        self.assertEqual(drive_table.verdict(usb_row('64G'), 'usb', {}), '')

    def test_a_drive_chosen_on_the_page_for_files_or_installs_is_not_the_offline_copy_too(self):
        row = usb_row('500G', uuid='hdd2-0001')
        places = {'music': {'uuid': 'hdd2-0001', 'name': 'sde1'}, 'photos': 'ssd'}
        self.assertEqual(storage.taken_rows(places, {}), ['hdd2-0001'])
        self.assertEqual(storage.taken_rows({'photos': 'media'}, {'music': {'uuid': 'x-1'}}), ['x-1'])
        self.addCleanup(drive_table.TAKEN_AHEAD.clear)
        drive_table.TAKEN_AHEAD[:] = storage.taken_rows(places, {})
        self.assertIn("already holds an app's files", drive_table.verdict(row, 'usb', {}))
        drive_table.TAKEN_AHEAD.clear()
        self.assertEqual(drive_table.verdict(row, 'usb', {}), '')

    def test_no_drive_is_asked_for_a_weekly_copy_with_nothing_to_take(self):
        context = types.SimpleNamespace(
            config={'photos': {'deploy_immich': False}, 'nextcloud': {'enabled': False}}, log=lambda _: None
        )
        with (
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(photo_backup, 'select_external', side_effect=AssertionError('asked')),
        ):
            storage.choose_usb(context)
        self.assertNotIn(storage.OFFLINE_WANTED, context.config['photos'])

    def test_none_for_now_says_how_to_add_one_without_editing_settings(self):
        said = []
        context = types.SimpleNamespace(config={'photos': {'deploy_immich': True}}, log=said.append)
        with (
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(storage, 'inventory_now', return_value=[]),
            mock.patch.object(photo_backup, 'select_external', return_value=None),
        ):
            storage.choose_usb(context)
        self.assertIs(context.config['photos'][storage.OFFLINE_WANTED], False)
        self.assertTrue(any('run  sudo bash install.sh  again' in line for line in said), said)
        self.assertFalse(any('my-config.json' in line for line in said), said)


class OfferedAgainTests(unittest.TestCase):
    def test_a_later_setup_run_offers_the_left_out_drive_before_the_approval(self):
        draft = {'photos': {'deploy_immich': True, storage.OFFLINE_WANTED: False}}
        with (
            mock.patch('existing_page.ask', return_value=False),
            mock.patch.object(storage, 'choose_usb') as offered,
        ):
            storage.existing_ahead(draft, lambda _: None)
        offered.assert_called_once()
        self.assertNotIn(storage.OFFLINE_WANTED, draft['photos'])
        # One chosen already is not asked about.
        draft = {'photos': {'deploy_immich': True, 'external_backup_uuid': 'usb-1'}}
        with mock.patch('existing_page.ask', return_value=False), mock.patch.object(storage, 'choose_usb') as offered:
            storage.existing_ahead(draft, lambda _: None)
        offered.assert_not_called()


class AskedAgainTests(unittest.TestCase):
    """install.sh --module photo_backup asks for the drive again; any other run does not."""

    def run_step(self, only, picked):
        ctx = types.SimpleNamespace(
            config={'photos': {'deploy_immich': True, 'external_backup_wanted': False}},
            apply=True,
            log=lambda _: None,
            only=only,
        )
        with (
            mock.patch.object(photo_backup.os, 'geteuid', return_value=0),
            mock.patch.object(photo_backup.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(photo_backup, 'switch_off'),
            mock.patch.object(photo_backup, 'chosen_drive', return_value=picked) as chosen,
        ):
            result = photo_backup.run(ctx)
        return ctx, result, chosen

    def test_a_full_run_keeps_it_left_out_unasked(self):
        ctx, result, chosen = self.run_step(None, None)
        chosen.assert_not_called()
        self.assertEqual(result['status'], 'unavailable')

    def test_the_step_by_name_asks_again_and_none_again_keeps_it_left_out(self):
        ctx, result, chosen = self.run_step('photo_backup', None)
        chosen.assert_called_once()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIs(ctx.config['photos']['external_backup_wanted'], False)


if __name__ == '__main__':
    unittest.main()


class DriveWordsTests(unittest.TestCase):
    """Gate run C: "the drive for your apps, photos and music" with Photos off, and the
    weekly copy "of your photos and Nextcloud files" with neither."""

    def test_the_words_name_only_what_this_server_has(self):
        from neko_common import drive_holds

        everything = {
            'media_apps': {'photos': True, 'movies': True, 'music': True},
            'photos': {'deploy_immich': True},
            'nextcloud': {'enabled': True},
        }
        self.assertEqual(drive_holds(everything, 'data_ssd'), 'your apps, photos and music')
        self.assertEqual(drive_holds(everything, 'media_hdd'), 'your movies, shows, Nextcloud files and the backups')
        self.assertEqual(drive_holds(everything, 'usb'), 'the weekly copy of your photos and Nextcloud files')
        films = {'media_apps': {'photos': False, 'movies': True, 'music': False}, 'nextcloud': {'enabled': False}}
        self.assertEqual(drive_holds(films, 'data_ssd'), 'your apps')
        self.assertEqual(drive_holds(films, 'media_hdd'), 'your movies, shows and the backups')
        none = {'media_apps': {'photos': False, 'movies': False, 'music': False}}
        self.assertEqual(drive_holds(none, 'media_hdd'), 'the backups')
        self.assertEqual(drive_holds(none, 'usb'), 'the weekly copy of your files')


class MountedShareTests(unittest.TestCase):
    """Gate run C1: a network share mounted on this PC was not in the storage page's list."""

    def test_a_mounted_share_is_listed_with_why_it_holds_no_apps(self):
        import json as json_

        shown = {'filesystems': [{'target': '/mnt/tank/films', 'source': 'nas:/films', 'fstype': 'nfs4'}]}
        run = mock.Mock(return_value=mock.Mock(stdout=json_.dumps(shown)))
        found = drive_table.mounted_shares([], run)
        self.assertEqual(len(found), 1)
        row = found[0]
        self.assertEqual((row['name'], row['mountpoint']), ('nas:/films', '/mnt/tank/films'))
        self.assertIn('a network share can hold data, not the apps', drive_table.verdict(row, 'data_ssd', {}))
        self.assertIn('network share (NFS4)', drive_table.words(row))
        # One the network storage step chose is listed by it already.
        self.assertEqual(drive_table.mounted_shares([{'export': 'nas:/films'}], run), [])
        # findmnt missing: nothing more listed.
        self.assertEqual(drive_table.mounted_shares([], mock.Mock(side_effect=OSError)), [])
