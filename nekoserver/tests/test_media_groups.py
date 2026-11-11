"""0.50.0: the media apps in three groups, each chosen on its own (the user, 2026-11-11):
Photos (Immich); Movies and shows (Jellyfin, Seerr and the download apps); Music
(Navidrome). Any combination is allowed, none and photos alone included. A group
switched off on a server that has it is stopped and keeps everything; a question that
belongs to a group that is off is not asked (the setup review, S2, S6 and S7)."""

import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance  # noqa: E402
import account_store  # noqa: E402
import explanations  # noqa: E402
import guided_setup  # noqa: E402
import neko_common  # noqa: E402
import recovery_wizard  # noqa: E402
import setup_gui  # noqa: E402
from modules import (  # noqa: E402
    app_backup,
    dashboard,
    desktop,
    media,
    media_monitor,
    music,
    music_backup,
    onboarding,
    samba,
    storage,
    tv,
)

SERVICES = ['jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent']
MOVIES = ['jellyfin', 'sonarr', 'radarr', 'prowlarr', 'seerr', 'qbittorrent']
GROUPS = neko_common.MEDIA_GROUPS


def settings(folder='/srv/nekoserver', **groups):
    """A server's settings with the shipped apps; `groups`: the answers given (photos=False)."""
    config = {
        'data_mount': folder,
        'media_mount': '/mnt/nekoserver-media',
        'media': {'services': list(SERVICES)},
        'photos': {'deploy_immich': True},
        'nextcloud': {'enabled': True},
        'music': {},
        'monitoring': {'enabled': True},
        'onboarding': {},
        'networking': {'shares': {'Media': 'library', 'Photos': 'photos/archive'}},
    }
    if groups:
        config['media_apps'] = dict(groups)
    return config


def only(*on):
    """Every group answered: those named on, the others off."""
    return {group: group in on for group in GROUPS}


def context(config, **more):
    logs = []
    return SimpleNamespace(config=config, apply=True, log=logs.append, logs=logs, persist_config=mock.Mock(), **more)


class MembershipTests(unittest.TestCase):
    def test_each_groups_apps_come_from_the_shared_app_list(self):
        self.assertEqual(GROUPS, ('photos', 'movies', 'music'))
        by_group = {
            group: [app for app, groups in neko_common.APP_GROUPS.items() if groups == (group,)] for group in GROUPS
        }
        self.assertEqual(by_group['photos'], ['immich'])
        self.assertEqual(by_group['movies'], ['jellyfin'])
        self.assertEqual(by_group['music'], ['navidrome'])
        # The apps that find and download are a switch of their own: Prowlarr and
        # qBittorrent with it alone; Sonarr, Radarr and Seerr need films and shows too,
        # Lidarr music (and not films and shows).
        groups = neko_common.APP_GROUPS
        self.assertEqual((groups['prowlarr'], groups['qbittorrent']), (('downloads',), ('downloads',)))
        for app in ('sonarr', 'radarr', 'seerr'):
            self.assertEqual(groups[app], ('movies', 'downloads'))
        self.assertEqual(groups['lidarr'], ('downloads', 'music'))
        # Nextcloud is in no group: it has its own switch, as before.
        self.assertEqual(neko_common.APP_GROUPS['nextcloud'], ())
        # Every media app is in a group, and nothing else is.
        grouped = {app for app, groups in neko_common.APP_GROUPS.items() if groups}
        self.assertEqual(grouped, {*neko_common.MEDIA_APPS, 'immich'})

    def test_every_combination_gives_the_apps_of_the_groups_that_are_on(self):
        for chosen in itertools.product((True, False), repeat=3):
            answers = dict(zip(GROUPS, chosen))
            with self.subTest(**answers):
                config = settings(**answers)
                expected = [
                    app
                    for app in SERVICES
                    if (app in MOVIES and answers['movies'])
                    or (app == 'navidrome' and answers['music'])
                    or (app == 'lidarr' and answers['movies'] and answers['music'])
                ]
                self.assertEqual(neko_common.media_services(config), expected)
                self.assertIs(neko_common.immich_wanted(config), answers['photos'])
                self.assertIs(neko_common.media_apps_wanted(config), any(chosen))
                # The settings themselves are never emptied: yes later brings back what was there.
                self.assertEqual(config['media']['services'], SERVICES)

    def test_the_downloads_are_a_switch_of_their_own(self):
        apps = neko_common.media_services
        # Playing without downloading: Jellyfin alone, Navidrome alone.
        self.assertEqual(apps(settings(**only('movies'), downloads=False)), ['jellyfin'])
        self.assertEqual(apps(settings(**only('movies', 'music'), downloads=False)), ['jellyfin', 'navidrome'])
        # Music downloads do not need the film player.
        self.assertEqual(
            apps(settings(**only('music'), downloads=True)), ['navidrome', 'lidarr', 'prowlarr', 'qbittorrent']
        )
        self.assertEqual(apps(settings(**only('movies'), downloads=True)), MOVIES)
        # Nothing to download for: the switch does nothing.
        self.assertEqual(apps(settings(**only('photos'), downloads=True)), [])
        self.assertFalse(neko_common.media_group(settings(**only('photos'), downloads=True), 'downloads'))
        # With no answer of its own it is as Movies and TV is: what every server had before
        # the switch existed, so an upgrade neither stops the download apps nor adds them.
        self.assertEqual(apps(settings()), SERVICES)
        self.assertEqual(apps(settings(**only('movies'))), MOVIES)
        self.assertEqual(apps(settings(**only('music'))), ['navidrome'])
        self.assertFalse(neko_common.media_group_chosen(settings(**only('movies')), 'downloads'))
        # The connecting step and the hourly check have work with Jellyfin or the download apps.
        left = neko_common.media_steps_left_out
        self.assertEqual(left(settings(**only('music'), downloads=True)) & {'onboarding', 'media_monitor'}, set())
        self.assertEqual(left(settings(**only('movies'), downloads=False)) & {'onboarding', 'media_monitor'}, set())

    def test_a_group_with_no_answer_is_on_and_the_old_yes_no_still_reads(self):
        self.assertEqual(neko_common.media_services(settings()), SERVICES)
        old_no = settings(wanted=False)
        self.assertEqual((neko_common.media_services(old_no), neko_common.immich_wanted(old_no)), ([], False))
        # A group's own answer wins over it, either way.
        self.assertEqual(neko_common.media_services(settings(wanted=False, music=True)), ['navidrome'])
        self.assertTrue(neko_common.immich_wanted(settings(wanted=False, photos=True)))
        self.assertFalse(neko_common.immich_wanted(settings(wanted=True, photos=False)))
        self.assertTrue(neko_common.media_group_chosen(old_no, 'photos'))
        self.assertFalse(neko_common.media_group_chosen(settings(), 'photos'))

    def test_the_steps_left_out_follow_the_groups(self):
        left = neko_common.media_steps_left_out
        self.assertEqual(left(settings()), set())
        self.assertEqual(left(settings(**only('movies', 'music'))), {'photos', 'photo_preservation'})
        self.assertEqual(left(settings(**only('photos', 'music'))), {'onboarding', 'media_monitor', 'tv'})
        self.assertEqual(left(settings(**only('photos', 'movies'))), {'music', 'music_backup'})
        self.assertEqual(left(settings(**only())), set(neko_common.MEDIA_STEPS))


class QuestionTests(unittest.TestCase):
    """The terminal: a yes or no for each group, saved under "media_apps"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(guided_setup, 'interactive', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        new = mock.patch.object(neko_common, 'server_exists', return_value=False)  # a PC setup runs on first
        new.start()
        self.addCleanup(new.stop)

    def ask(self, config, answers):
        ctx = context(config)
        typed = iter(answers)
        asked, shown = [], []

        def prompt(question):
            asked.append(question)
            return next(typed)

        guided_setup.choose_media_apps(ctx, prompt=prompt, output=shown.append)
        return ctx, asked, shown

    def test_each_group_is_asked_by_name_and_any_combination_is_saved(self):
        # The downloads are asked after the three, when there is something to download for.
        cases = {
            ('yes', 'no', 'no'): only('photos'),  # photos alone
            ('no', 'yes', 'no', 'no'): {**only('movies'), 'downloads': False},  # playing, no downloading
            ('no', 'no', 'yes', 'yes'): {**only('music'), 'downloads': True},
            ('no', 'no', 'no'): only(),  # none
            ('yes', 'yes', 'yes', 'yes'): {**only(*GROUPS), 'downloads': True},  # all
        }
        names = [*neko_common.GROUP_NAMES.values(), neko_common.DOWNLOADS_NAME]
        for answers, expected in cases.items():
            with self.subTest(answers=answers):
                config = settings(self.tmp.name)
                ctx, asked, shown = self.ask(config, answers)
                self.assertEqual(config['media_apps'], expected)
                self.assertEqual(len(asked), len(answers))
                for question, name in zip(asked, names):
                    self.assertIn(f'Install {name}?', question)
                ctx.persist_config.assert_called_once()
        said = '\n'.join(shown)
        self.assertIn('each can be left out', said)
        self.assertIn('never deletes anything', said)

    def test_a_group_chosen_in_the_window_is_not_asked_again(self):
        config = settings(self.tmp.name, movies=False)
        _, asked, _ = self.ask(config, ['yes', 'yes', 'no'])
        self.assertEqual(
            [q.strip().split('?')[0] for q in asked],
            ['Install Photos (Immich)', 'Install Music (Navidrome)', 'Install ' + neko_common.DOWNLOADS_NAME],
        )
        self.assertEqual(config['media_apps'], {'movies': False, 'photos': True, 'music': True, 'downloads': False})
        # All chosen: nothing is asked.
        self.assertEqual(self.ask(config, [])[1], [])

    def test_a_group_left_alone_is_on_and_asked_again_next_time(self):
        config = settings(self.tmp.name)

        typed = iter(['no'])

        def prompt(_):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError from None  # Ctrl+D at the others

        ctx = context(config)
        guided_setup.choose_media_apps(ctx, prompt=prompt, output=lambda _: None)
        # The downloads left alone on a new server are not set up: playing needs none of them.
        self.assertEqual(config['media_apps'], {'photos': False, 'downloads': False})
        self.assertEqual(neko_common.media_services(config), ['jellyfin', 'navidrome'])
        _, asked, _ = self.ask(config, ['yes', 'yes'])
        self.assertEqual(len(asked), 2)

    def test_a_rerun_asks_nothing_and_a_group_is_changed_on_the_first_page(self):
        config = settings(self.tmp.name, **only(*GROUPS), downloads=True)
        guided_setup.remember_media_apps(context(config))
        self.assertEqual(neko_common.optional_answers(self.tmp.name), {'media_apps': 'photos, movies, music'})
        # Every one has its answer: the step asks nothing, also with answers kept from before
        # (they are changed on setup's first page, which every run shows).
        ctx, asked, shown = self.ask(config, [])
        self.assertEqual((asked, shown), ([], []))
        # Music switched off there: Navidrome and Lidarr go, the rest stays.
        config['media_apps']['music'] = False
        self.assertEqual(neko_common.media_services(config), MOVIES)
        guided_setup.remember_media_apps(ctx)
        self.assertEqual(neko_common.optional_answers(self.tmp.name), {'media_apps': 'photos, movies'})

    def test_a_broken_answer_stops_setup_with_the_reason(self):
        config = settings(self.tmp.name, music='no')
        with self.assertRaisesRegex(RuntimeError, '"music" under "media_apps" must be true or false'):
            self.ask(config, [])


class TurnedOffLaterTests(unittest.TestCase):
    """A group switched off on a server that has its apps goes through the one seam
    (guided_setup.settle_no_with_apps): stopped, everything kept."""

    def server(self, apps, **groups):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for app in apps:
            folder = Path(tmp.name, 'compose', app)
            folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_text('services: {}\n')
            Path(tmp.name, 'appdata', app).mkdir(parents=True)
            Path(tmp.name, 'appdata', app, 'kept.db').write_text('data')
        return context(settings(tmp.name, **groups)), Path(tmp.name)

    def test_only_the_apps_of_the_group_turned_off_are_named_and_stopped(self):
        ctx, folder = self.server(['jellyfin', 'navidrome', 'lidarr', 'immich'], **only('photos', 'movies'))
        before = sorted(str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file())
        self.assertEqual(guided_setup.settle_no_with_apps(ctx), ['navidrome', 'lidarr'])
        said = '\n'.join(ctx.logs)
        self.assertIn('a group this server already has (Navidrome, Lidarr). They are stopped in this run', said)
        self.assertIn('Nothing is deleted', said)
        # The media step then stops exactly those, and never removes a volume or a file.
        run = mock.Mock()
        selected = neko_common.media_services(ctx.config)
        stopped = media.stop_unselected(folder / 'compose', selected, ctx.log, run)
        self.assertEqual(stopped, ['navidrome', 'lidarr'])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all(c[-1] == 'down' and '-v' not in c and '--volumes' not in c for c in commands))
        self.assertEqual(sorted(str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()), before)

    def test_photos_turned_off_names_immich_alone(self):
        ctx, _ = self.server(['jellyfin', 'immich'], **only('movies', 'music'))
        self.assertEqual(guided_setup.settle_no_with_apps(ctx), ['immich'])

    def test_nothing_to_settle_when_the_group_turned_off_was_never_set_up(self):
        ctx, _ = self.server(['jellyfin'], **only('movies'))
        self.assertEqual(guided_setup.settle_no_with_apps(ctx), [])
        self.assertEqual(ctx.logs, [])


class WindowTests(unittest.TestCase):
    def test_three_switches_each_saved_on_its_own(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(setup_gui, 'family_packages', return_value={'names': lambda n: [n], 'vulkan': []}),
            mock.patch.object(neko_common, 'server_exists', return_value=False),
        ):
            path = Path(folder) / 'my-config.json'
            path.write_text('{}')
            window = setup_gui.Window(path, {}, {}, [])
            self.addCleanup(window.stop)
            shown = {e['id']: (e['title'], e['on'], e['part']) for e in window._extras()[:3]}
            self.assertEqual(
                shown,
                {
                    'media_photos': ('Photos and phone backup - Immich', True, 'Apps'),
                    'media_movies': ('Movies and TV shows - Jellyfin', True, 'Apps'),
                    'media_music': ('Music - Navidrome', True, 'Apps'),
                },
            )
            # Photos alone: two switches off.
            self.assertTrue(window.choose({'media_movies': False, 'media_music': False}))
            self.assertEqual(window.config['media_apps'], {'movies': False, 'music': False})
            after = {e['id']: e['on'] for e in window._extras()[:3]}
            self.assertEqual(after, {'media_photos': True, 'media_movies': False, 'media_music': False})
            self.assertEqual(neko_common.media_services(window.config), [])
            self.assertTrue(neko_common.media_group(window.config, 'photos'))

    def test_a_settings_file_with_the_old_no_shows_every_switch_off_and_decided(self):
        old = {'media_apps': {'wanted': False}}
        for extra in explanations.EXTRAS[:3]:
            self.assertFalse(explanations.extra_state(old, extra))
            self.assertTrue(explanations.extra_chosen(old, extra))
        # One switched back on wins over it.
        explanations.set_extra(old, explanations.EXTRAS[2], True)
        self.assertEqual(neko_common.media_services({**settings(), **old}), ['navidrome'])


class StepTests(unittest.TestCase):
    """What each step does with one group off."""

    def setUp(self):
        root = mock.patch('os.geteuid', return_value=0)
        root.start()
        self.addCleanup(root.stop)

    def test_the_media_step_takes_an_empty_list_of_apps(self):
        """The backend refused an empty media-app list ("must be a nonempty list"), which
        blocked a photos-only server."""
        for config in (settings(**only('photos')), {**settings(), 'media': {'services': []}}):
            ctx = context(config)
            with mock.patch.object(media, 'left_out') as left_out, mock.patch.object(media, 'check_mount') as mounts:
                media._run(ctx)
            left_out.assert_called_once_with(ctx)
            mounts.assert_not_called()
        # A list that names something else is still refused.
        wrong = context({**settings(), 'media': {'services': ['plex']}})
        with self.assertRaisesRegex(RuntimeError, 'media.services must be a list of known services'):
            media._run(wrong)

    def test_music_alone_asks_nothing_about_jellyfin(self):
        """S6: a music-only setup still asked for a playback test in Jellyfin."""
        config = settings(**only('music'))
        ctx = context(config)
        self.assertIsNone(onboarding.run(ctx))
        self.assertIn(
            'Connecting the apps: Movies and TV and the downloads are switched off, so there is nothing to connect',
            ctx.logs,
        )
        self.assertIsNone(tv.run(context(config)))
        with mock.patch('modules._schedules.retire') as retire:
            media_monitor.run(context(config))
        self.assertIn('nekoserver-media-health.timer', [Path(p).name for p in retire.call_args.args[0]])
        # The same for a server whose list of apps simply has no Jellyfin.
        by_hand = settings()
        by_hand['media']['services'] = ['navidrome']
        with tempfile.TemporaryDirectory() as folder:
            by_hand['data_mount'] = folder
            asked = mock.Mock(side_effect=AssertionError('the playback question was asked'))
            onboarding.witness_playback(context(by_hand), checkpoint=asked)
            # With Jellyfin it is asked, as before.
            asked = mock.Mock(return_value=False)
            onboarding.witness_playback(context({**settings(folder), 'media_apps': only('movies')}), checkpoint=asked)
            asked.assert_called_once()

    def test_the_music_steps_follow_the_music_group(self):
        off = settings(**only('photos', 'movies'))
        ctx = context(off)
        self.assertIsNone(music.run(ctx))
        self.assertIn('Music: switched off (Music (Navidrome) in setup); no music player to set up', ctx.logs)
        with mock.patch('modules._schedules.retire') as retire:
            music_backup.run(context(off))
        self.assertIn('nekoserver-music-backup.timer', [Path(p).name for p in retire.call_args.args[0]])
        # With music alone the music backup stays and the nightly app backup has Navidrome to keep.
        with mock.patch.object(app_backup.native_runtime, 'storage_config', return_value={}):
            kept = app_backup.guard_config({**settings(**only('music')), 'app_backup': {}})
        self.assertEqual(kept['media'], {'services': ['navidrome']})

    def test_no_place_is_asked_for_files_no_app_here_keeps(self):
        """S7: apps that are off still produced storage questions (photos, cloud storage)."""

        def asked(config):
            with tempfile.TemporaryDirectory() as folder:
                config = {**config, 'data_mount': folder, 'media_mount': folder}
                with mock.patch.object(storage, 'new_place', return_value='media') as place:
                    storage.choose_locations(context(config), interactive=False)
                return [call.args[1] for call in place.call_args_list]

        self.assertEqual(asked(settings()), ['photos', 'music', 'cloud'])
        self.assertEqual(asked(settings(**only('movies', 'music'))), ['music', 'cloud'])
        self.assertEqual(asked(settings(**only('photos', 'movies'))), ['photos', 'cloud'])
        self.assertEqual(asked(settings(**only())), ['cloud'])
        # Immich switched off by its own setting, and Nextcloud switched off.
        no_immich = settings()
        no_immich['photos']['deploy_immich'] = False
        self.assertEqual(asked(no_immich), ['music', 'cloud'])
        no_cloud = settings(**only('photos'))
        no_cloud['nextcloud']['enabled'] = False
        self.assertEqual(asked(no_cloud), ['photos'])

    def test_logins_shortcuts_shares_and_checks_follow_the_groups(self):
        plan = ['media', 'photos', 'nextcloud']
        photos_only = settings(**only('photos'))
        self.assertEqual(account_store.selected(photos_only, plan), ['immich', 'nextcloud'])
        self.assertEqual(
            account_store.selected(settings(**only('music')), plan), ['navidrome', 'navidrome_listener', 'nextcloud']
        )
        with tempfile.TemporaryDirectory() as folder:
            for app in ('immich', 'nextcloud'):
                Path(folder, 'compose', app).mkdir(parents=True)
                Path(folder, 'compose', app, 'compose.yaml').write_text('')
            self.assertEqual(desktop.installed_apps({**photos_only, 'data_mount': folder}), ['immich', 'nextcloud'])
            no_photos = {**settings(**only('movies')), 'data_mount': folder}
            self.assertEqual(
                desktop.installed_apps(no_photos), [*[a for a in neko_common.APPS if a in MOVIES], 'nextcloud']
            )
            # Photos alone: no library was ever made, so the Media share is not offered.
            self.assertEqual(samba.wanted_shares({**photos_only, 'media_mount': folder}), {'Photos': 'photos/archive'})
            self.assertEqual(
                samba.wanted_shares({**settings(**only('music')), 'media_mount': folder}), {'Media': 'library'}
            )
        modules = ['system', 'nextcloud', *neko_common.MEDIA_STEPS]
        with mock.patch.object(acceptance.tv, 'available', return_value=True):
            kept = acceptance.accepted_modules(photos_only, modules)
        self.assertEqual(
            kept, ['system', 'nextcloud', 'media', 'photos', 'photo_preservation', 'hardware_video', 'app_backup']
        )

    def test_the_recovery_menu_keeps_the_restores_of_the_groups_that_are_on(self):
        with tempfile.TemporaryDirectory() as hdd:
            keys = [key for key, _ in recovery_wizard.menu({**settings(**only('photos')), 'media_mount': hdd})]
            self.assertIn('photos', keys)
            self.assertNotIn('apps', keys)
            keys = [key for key, _ in recovery_wizard.menu({**settings(**only('music')), 'media_mount': hdd})]
            self.assertNotIn('photos', keys)
            self.assertIn('apps', keys)

    def test_the_dashboard_is_told_each_group_once_one_has_its_own_answer(self):
        with mock.patch.object(dashboard, 'scope_digest', return_value='x'):
            base = {'external_backup_mount': '/mnt/x', 'storage': {}}
            clean = dashboard.collector_settings({**settings(**only('photos')), **base}, 1800)
            untouched = dashboard.collector_settings({**settings(), **base}, 1800)
        self.assertEqual(clean['media_apps'], {'wanted': True, 'photos': True, 'movies': False, 'music': False})
        self.assertEqual(clean['media'], {'services': []})
        self.assertTrue(clean['photos']['deploy_immich'])
        self.assertEqual(untouched['media_apps'], {'wanted': True})  # a server from before: its file as it was

    def test_the_runtime_reads_the_same_rule_from_the_same_list(self):
        common = (ROOT / 'runtime/src/common.rs').read_text()
        self.assertIn('pub fn media_group(cfg: &Value, group: &str) -> bool', common)
        self.assertIn('a["groups"].as_array()', common)


if __name__ == '__main__':
    unittest.main()
