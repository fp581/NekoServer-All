"""The storage page before the approval (0.50.0, modules/storage.choose_ahead): the drives
are chosen on a copy of the settings before anything is installed, written only once the
person approves, and the storage step of the same run then asks nothing again."""

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
import existing_page  # noqa: E402
import setup_gui  # noqa: E402
from modules import storage  # noqa: E402


def settings(**more):
    return {
        'data_mount': '/srv/nekoserver-test-apps',
        'media_mount': '/mnt/nekoserver-test-media',
        'storage': {},
        'photos': {},
        **more,
    }


class ChangesTests(unittest.TestCase):
    def test_only_what_the_page_changed_is_kept(self):
        before = settings(photos={'deploy_immich': True, 'external_backup_network': 'old'})
        after = copy.deepcopy(before)
        after['storage']['data_ssd'] = {'uuid': 'u1', 'fstype': 'ext4'}
        after['photos'].update(external_backup_uuid='u9', external_backup_fstype='ext4')
        del after['photos']['external_backup_network']
        after[existing_page.ASKED] = True
        self.assertEqual(
            storage.section_changes(before, after),
            {
                'storage': ({'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}}, []),
                'photos': (
                    {'external_backup_uuid': 'u9', 'external_backup_fstype': 'ext4'},
                    ['external_backup_network'],
                ),
                existing_page.ASKED: True,
            },
        )
        self.assertEqual(storage.section_changes(before, copy.deepcopy(before)), {})

    def test_they_go_into_the_settings_as_they_are_at_the_approval(self):
        """The first page's switches were saved meanwhile: they stay as saved."""
        self.addCleanup(drive_table.PLACES_AHEAD.clear)
        now = settings(photos={'deploy_immich': False, 'external_backup_network': 'old'}, tv={'wanted': True})
        chosen = {
            'settings': {
                'storage': ({'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}}, []),
                'photos': ({'external_backup_wanted': False}, ['external_backup_network']),
                existing_page.ASKED: True,
            },
            'places': {'photos': 'ssd'},
            'lines': [],
        }
        storage.keep_ahead(now, chosen)
        self.assertEqual(now['storage'], {'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}})
        self.assertEqual(now['photos'], {'deploy_immich': False, 'external_backup_wanted': False})
        self.assertEqual(now['tv'], {'wanted': True})
        self.assertIs(now[existing_page.ASKED], True)
        self.assertEqual(drive_table.PLACES_AHEAD, {'photos': 'ssd'})


class AskedAheadTests(unittest.TestCase):
    def test_nothing_is_asked_with_nobody_there_or_nothing_left_to_ask(self):
        nothing = {'settings': {}, 'places': {}, 'lines': []}
        with mock.patch.object(storage, 'inventory_now', side_effect=AssertionError('looked at the drives')):
            with mock.patch.object(storage.sys.stdin, 'isatty', return_value=False):
                self.assertEqual(storage.choose_ahead(settings(), print), nothing)
            with (
                mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
                mock.patch.object(storage, 'page_fields', return_value={}),
            ):
                self.assertEqual(storage.choose_ahead(settings(), print), nothing)

    def test_the_choices_are_made_on_a_copy_and_nothing_is_mounted(self):
        config = settings()
        before = copy.deepcopy(config)

        def roles(draft, inventory, **how):
            draft['storage']['data_ssd'] = {'uuid': 'u1', 'fstype': 'ext4'}
            draft['storage']['media_hdd'] = {'uuid': 'u2', 'fstype': 'ext4'}

        def offline(context):
            context.config['photos']['external_backup_wanted'] = False

        said = []
        with (
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(storage, 'inventory_now', return_value={'blockdevices': []}),
            mock.patch.object(storage, 'storage_page', return_value=mock.MagicMock()),
            mock.patch.object(storage, 'resolve_storage_identity', side_effect=roles),
            mock.patch.object(storage, 'wished_places', return_value={'photos': 'media'}),
            mock.patch.object(storage, 'choose_usb', side_effect=offline),
            mock.patch.object(existing_page, 'ask', return_value=False),
            mock.patch.object(storage, 'mount_all', side_effect=AssertionError('mounted')),
            mock.patch.object(storage, 'save_storage', side_effect=AssertionError('saved')),
        ):
            chosen = storage.choose_ahead(config, said.append)
        self.assertEqual(config, before)  # the settings themselves are as they were
        self.assertEqual(sorted(chosen['settings']), ['photos', 'storage'])
        self.assertEqual(chosen['places'], {'photos': 'media'})
        self.assertEqual(
            chosen['lines'],
            [
                'NekoServer and your apps: the drive chosen before',
                'Backups: the drive chosen before',
                "Immich's photos and videos: on the drive of the backups. " + storage.SAME_DRIVE_AS_BACKUP,
                'Offline backup: none for now',
            ],
        )

    def test_the_room_of_a_drive_that_is_not_open_comes_from_the_list(self):
        config = settings(storage={'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'u2'}})
        table = [{'uuid': 'u1', 'size': '100G', 'full': 25}, {'uuid': 'u2', 'size': '1T', 'full': None}]
        with mock.patch.object(storage, 'findmnt', return_value=None):
            room = storage.room_ahead(config, table)
        self.assertEqual(room, {'ssd': 80, 'media': 1099})
        self.assertEqual(storage.free_on(None), 0)


class StepTests(unittest.TestCase):
    """The storage step of the run that asked ahead."""

    def setUp(self):
        self.addCleanup(drive_table.PLACES_AHEAD.clear)

    def context(self, config):
        context = mock.Mock(config=config, apply=True)
        context.log = self.said.append
        return context

    def places(self, config, kept=(), before=False):
        self.said = []
        with (
            mock.patch.object(storage, 'free_gb', return_value=100),
            mock.patch.object(storage, 'kept_location', return_value=list(kept)),
            mock.patch.object(storage, 'set_up_before', return_value=before),
            mock.patch.object(storage, 'asked_where', side_effect=lambda config, what: what in ('photos', 'movies')),
        ):
            storage.choose_locations(
                self.context(config), input_fn=mock.Mock(side_effect=AssertionError('asked')), interactive=True
            )
        return config.get('locations', {})

    def test_a_place_chosen_ahead_is_not_asked_again(self):
        drive_table.PLACES_AHEAD.update(photos='ssd', movies='media')
        self.assertEqual(self.places(settings()), {'photos': 'ssd', 'movies': 'media'})
        self.assertIn("Immich's photos and videos: on the SSD", self.said)

    def test_files_found_on_the_mounted_drive_stay_where_they_are(self):
        """A drive from an earlier server: the choice made before it was open gives way."""
        drive_table.PLACES_AHEAD.update(photos='ssd')
        self.assertEqual(self.places(settings(), kept=['media'], before=True), {'photos': 'media'})
        self.assertIn("Immich's photos and videos: kept where they already are, on the media drive", self.said)

    def test_the_step_opens_no_page_for_what_was_chosen_ahead(self):
        config = settings(
            storage={'data_ssd': {'uuid': 'u1'}, 'media_hdd': {'uuid': 'u2'}},
            photos={'external_backup_wanted': False},
        )
        with (
            mock.patch.object(storage, 'set_up_before', return_value=False),
            mock.patch.object(storage, 'asked_where', side_effect=lambda config, what: what == 'photos'),
        ):
            self.assertEqual(storage.page_fields(config), {'photos': ''})
            drive_table.PLACES_AHEAD.update(photos='ssd')
            self.assertEqual(storage.page_fields(config), {})


class WindowStageTests(unittest.TestCase):
    """The window follows the wizard through the pages before setup starts."""

    def window(self):
        window = setup_gui.Window.__new__(setup_gui.Window)
        window.lock = __import__('threading').Lock()
        window.frozen, window.stage, window.button, window.storage_lines = False, 'apps', None, []
        return window

    def test_a_button_counts_only_on_its_own_page_and_once(self):
        window = self.window()
        self.assertFalse(window.press('back'))  # the first page has no Back
        self.assertTrue(window.press('continue'))
        self.assertEqual((window.pressed(), window.pressed()), ('continue', None))
        window.show('review', ['Offline backup: none for now'])
        self.assertFalse(window.press('continue'))
        self.assertTrue(window.press('back'))
        window.show('apps')
        self.assertIsNone(window.pressed())  # a press of the page before is forgotten
        self.assertEqual(window.storage_lines, [])
        window.frozen = True
        self.assertFalse(window.press('continue'))  # setup has started

    def test_the_drive_questions_are_shown_as_a_page_of_their_own(self):
        window = self.window()
        window.menu, window.started = None, mock.Mock(is_set=lambda: False)
        self.assertEqual(window._phase(None, []), 'choose')
        window.show('storage')
        self.assertEqual(window._phase(None, []), 'running')
        window.show('review')
        self.assertEqual(window._phase(None, []), 'choose')


if __name__ == '__main__':
    unittest.main()
