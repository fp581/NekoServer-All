"""The storage page before the approval (0.50.0, modules/storage.choose_ahead): the drives
are chosen on a copy of the settings before anything is installed, written only once the
person approves, and the storage step of the same run then asks nothing again."""

import copy
import sys
import tempfile
import types
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
        after[existing_page.COPIES] = [{'holds': 'movies', 'path': '/mnt/tank/films'}]
        self.assertEqual(
            storage.section_changes(before, after),
            {
                'storage': ({'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}}, []),
                'photos': (
                    {'external_backup_uuid': 'u9', 'external_backup_fstype': 'ext4'},
                    ['external_backup_network'],
                ),
                existing_page.ASKED: True,
                existing_page.COPIES: [{'holds': 'movies', 'path': '/mnt/tank/films'}],
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
                existing_page.COPIES: [{'holds': 'music', 'path': '/mnt/tank/music'}],
            },
            'places': {'photos': 'ssd'},
            'lines': [],
        }
        storage.keep_ahead(now, chosen)
        self.assertEqual(now['storage'], {'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}})
        self.assertEqual(now['photos'], {'deploy_immich': False, 'external_backup_wanted': False})
        self.assertEqual(now['tv'], {'wanted': True})
        self.assertIs(now[existing_page.ASKED], True)
        self.assertEqual(now[existing_page.COPIES], [{'holds': 'music', 'path': '/mnt/tank/music'}])
        self.assertEqual(drive_table.PLACES_AHEAD, {'photos': 'ssd'})

    def test_a_drive_chosen_in_this_run_is_not_said_as_chosen_last_time(self):
        # A first install's storage step said "Last time you chose drive 2 ... as the app
        # SSD. It is used again." of the drive picked a minute before (the Ubuntu lab).
        self.addCleanup(drive_table.ROLES_AHEAD.clear)
        table = [{'number': 2, 'uuid': 'u1'}]
        now = settings()
        chosen = {'settings': {'storage': ({'data_ssd': {'uuid': 'u1', 'fstype': 'ext4'}}, [])}, 'places': {}}
        storage.keep_ahead(now, chosen)
        said = []
        with mock.patch.object(drive_table, 'brief', return_value='drive 2 (Samsung SSD 870)'):
            storage.said_last_time(table, 'u1', 'data_ssd', said.append)
            self.assertEqual(said, ['As chosen before the approval, drive 2 (Samsung SSD 870) is the apps drive.'])
            # A rerun whose page kept the saved drives: chosen at an earlier run.
            storage.keep_ahead(now, {'settings': {}, 'places': {}})
            storage.said_last_time(table, 'u1', 'data_ssd', said.append)
        self.assertEqual(said[1], 'Last time you chose drive 2 (Samsung SSD 870) as the apps drive. It is used again.')


class AskedAheadTests(unittest.TestCase):
    def setUp(self):
        # The drives here; the backups and accounts pages asked with them are tested in
        # tests/test_accounts_ahead.py.
        others = mock.patch.object(storage, 'others_ahead')
        others.start()
        self.addCleanup(others.stop)
        existing = mock.patch.object(storage, 'existing_ahead')
        self.existing = existing.start()
        self.addCleanup(existing.stop)

    def test_with_the_drives_known_the_films_question_is_still_before_the_approval(self):
        # Gate run D (an update from 0.48.2): "Do you already have films...?" came after it.
        with (
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(storage, 'page_fields', return_value={}),
        ):
            storage.choose_ahead(settings(), print)
        self.existing.assert_called_once()

    def test_the_films_answer_made_ahead_goes_into_the_settings(self):
        import existing_page

        def answered(context, output=print):
            context.config[existing_page.ASKED] = True
            return True

        self.existing.side_effect = lambda draft, log: answered(mock.Mock(config=draft))
        with (
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(storage, 'page_fields', return_value={}),
        ):
            chosen = storage.choose_ahead(settings(), print)
        self.assertIs(chosen['settings'][existing_page.ASKED], True)

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
                'Apps drive: the drive chosen before',
                'Media and backups drive: the drive chosen before',
                "Immich's photos and videos: on the media and backups drive. " + storage.SAME_DRIVE_AS_BACKUP,
                'Offline backup: none for now',
            ],
        )

    def test_on_one_drive_the_review_names_that_drive_not_the_drive_of_the_backups(self):
        """Deep check plan 199: with the apps and the backups on one drive, the page offered
        the photos "the drive of this server, with the apps", and the review said "on the
        drive of the backups"."""
        config = settings(
            storage={
                key: {'uuid': 'one-1', 'fstype': 'ext4', 'folder': storage.SHARED_FOLDERS[key]}
                for key in ('data_ssd', 'media_hdd')
            }
        )
        self.assertTrue(storage.one_place(config))
        for place in ('ssd', 'media'):
            with self.subTest(place=place):
                lines = storage.ahead_lines(config, {'blockdevices': []}, {'photos': place})
                line = next(line for line in lines if line.startswith("Immich's"))
                self.assertEqual(
                    line, "Immich's photos and videos: " + storage.ONE_PLACE_SAID + '. ' + storage.SAME_DRIVE_AS_BACKUP
                )

    def test_no_drive_that_can_be_the_offline_backup_is_said(self):
        """Sweep BH5: with no drive that could be the offline backup nothing was asked, and
        the review had no offline-backup line at all."""
        config = settings(photos={'deploy_immich': True}, nextcloud={'enabled': False})
        lines = storage.ahead_lines(config, {'blockdevices': []}, {})
        self.assertIn(
            'Offline backup: none yet, as no drive connected now can be one. Connect a drive you can unplug and '
            'run  sudo bash install.sh  again to choose it',
            lines,
        )
        # Nothing for the weekly copy to take: no line, as before.
        config = settings(photos={'deploy_immich': False}, nextcloud={'enabled': False})
        self.assertFalse([line for line in storage.ahead_lines(config, {'blockdevices': []}, {}) if 'Offline' in line])

    def test_the_review_says_what_happens_to_folders_of_the_persons_own(self):
        own = {'path': '/mnt/tank/films', 'holds': 'movies', 'use': 'in_place', 'new_files': True}
        new = {'path': '/mnt/big/shows', 'holds': 'tv', 'use': 'new_files', 'new_files': True}
        config = settings(**{existing_page.COPIES: [{'holds': 'music', 'path': '/mnt/tank/music'}]})
        with mock.patch('existing_media.extras', return_value=[own, new]):
            lines = storage.ahead_lines(config, {'blockdevices': []}, {})
        self.assertEqual(
            lines[-3:],
            [
                'Your movies in /mnt/tank/films: used where they are, and new ones are saved there',
                'New shows are saved in /mnt/big/shows',
                'Your music in /mnt/tank/music: copied into the NekoServer library; your folder is kept as it is',
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
        self.assertIn("Immich's photos and videos: on the apps drive", self.said)

    def test_files_found_on_the_mounted_drive_stay_where_they_are(self):
        """A drive from an earlier server: the choice made before it was open gives way."""
        drive_table.PLACES_AHEAD.update(photos='ssd')
        self.assertEqual(self.places(settings(), kept=['media'], before=True), {'photos': 'media'})
        self.assertIn(
            "Immich's photos and videos: kept where they already are, on the media and backups drive", self.said
        )

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
        window.revision = 0
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


class AskedOnceTests(unittest.TestCase):
    """Gate run D: "Change any of these answers?" was asked before the approval and again
    by the onboarding step after it."""

    def test_the_answer_before_the_approval_stands_for_the_steps_after_it(self):
        import guided_setup

        self.addCleanup(guided_setup.OPTIONAL_AGAIN.clear)
        guided_setup.OPTIONAL_AGAIN['again'] = False
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'secrets').mkdir()
            ctx = types.SimpleNamespace(config={'data_mount': folder}, log=lambda _: None)
            import neko_common

            with mock.patch.object(neko_common, 'optional_answers', return_value={'backup_times': 'daily 04:15'}):
                kept = guided_setup.optional_kept(ctx, ['backup_times'], prompt=mock.Mock(side_effect=AssertionError))
        self.assertEqual(kept, ['backup_times'])
