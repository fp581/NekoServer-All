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
        self.assertTrue(any('install.sh --module photo_backup' in line for line in said), said)
        self.assertFalse(any('my-config.json' in line for line in said), said)


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
