"""0.50.0: the media apps can be left out (guided_setup.choose_media_apps). Yes keeps what
every server had; no leaves out Jellyfin with Seerr, the apps that find and download,
Immich, Navidrome, hardware video, the media monitor and the TV mode, and every step
that depends on them handles it. No on a server that already has them stops them and
keeps all their data.

These tests are of every group off together, as the one yes/no this began as still
reads ("wanted": false in a settings file); tests/test_media_groups.py has each group
on its own."""

import json
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
import gateway_tools  # noqa: E402
import guided_setup  # noqa: E402
import neko_common  # noqa: E402
import recovery_wizard  # noqa: E402
from modules import (  # noqa: E402
    app_backup,
    desktop,
    hardware_video,
    media,
    media_monitor,
    music,
    music_backup,
    onboarding,
    photos,
    samba,
    storage,
    tv,
)

SERVICES = ['jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent']


def settings(wanted=None, folder='/srv/nekoserver'):
    """A server's settings: the shipped apps, Immich and Nextcloud on; `wanted` the answer."""
    config = {
        'data_mount': folder,
        'media_mount': '/mnt/nekoserver-media',
        'media': {'services': list(SERVICES)},
        'photos': {'deploy_immich': True},
        'nextcloud': {'enabled': True},
        'music': {},
        'monitoring': {'enabled': True},
        'onboarding': {},
        'gateway': {'services': list(gateway_tools.DEFAULT_SERVICES)},
        'networking': {'shares': {'Media': 'library', 'Photos': 'photos/archive'}},
    }
    if wanted is not None:
        config['media_apps'] = {'wanted': wanted}
    return config


def context(config, **more):
    logs = []
    ctx = SimpleNamespace(config=config, apply=True, log=logs.append, logs=logs, persist_config=mock.Mock(), **more)
    return ctx


class SettingTests(unittest.TestCase):
    def test_yes_unless_no(self):
        # Every server set up before 0.50.0 has no answer: it keeps its apps.
        for wanted, expected in ((None, True), (True, True), (False, False)):
            with self.subTest(wanted=wanted):
                config = settings(wanted)
                self.assertIs(neko_common.media_apps_wanted(config), expected)
                self.assertEqual(neko_common.media_services(config), SERVICES if expected else [])
                self.assertIs(neko_common.immich_wanted(config), expected)

    def test_immich_also_follows_its_own_switch(self):
        config = settings(True)
        config['photos']['deploy_immich'] = False
        self.assertFalse(neko_common.immich_wanted(config))

    def test_the_setting_itself_is_never_emptied(self):
        # No never rewrites media.services or photos.deploy_immich: yes later brings back
        # exactly what was there.
        config = settings(False)
        neko_common.media_services(config)
        self.assertEqual(config['media']['services'], SERVICES)
        self.assertTrue(config['photos']['deploy_immich'])

    def test_it_is_one_of_the_optional_questions(self):
        self.assertEqual(list(neko_common.OPTIONAL_QUESTIONS)[0], neko_common.MEDIA_SECTION)
        self.assertEqual(neko_common.OPTIONAL_QUESTIONS[neko_common.MEDIA_SECTION], 'Media apps')


class QuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(guided_setup, 'interactive', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, config, answers, **more):
        ctx = context(config, **more)
        typed = iter(answers)
        shown = []
        # What the person reads: what setup prints and what each question says.
        guided_setup.choose_media_apps(ctx, prompt=lambda q: shown.append(q) or next(typed), output=shown.append)
        return ctx, shown

    def test_asked_once_explained_and_saved(self):
        config = settings(folder=self.tmp.name)
        ctx, shown = self.ask(config, ['no', 'yes', 'no'])
        self.assertEqual(config['media_apps'], {'photos': False, 'movies': True, 'music': False})
        ctx.persist_config.assert_called_once()
        said = '\n'.join(shown)
        for words in (
            'Jellyfin',
            'Seerr',
            'Sonarr, Radarr and Prowlarr',
            'Prowlarr',
            'qBittorrent',
            'Lidarr',
            'Immich',
            'Navidrome',
        ):
            self.assertIn(words, said)
        self.assertIn('never deletes anything', said)
        self.assertIn('GB', said)  # what it costs
        # Chosen now, not asked again in the same run or the next.
        self.ask(config, [])

    def test_ctrl_d_leaves_yes_and_asks_again_next_time(self):
        config = settings(folder=self.tmp.name)

        def ended(_):
            raise EOFError

        ctx = context(config)
        guided_setup.choose_media_apps(ctx, prompt=ended, output=lambda _: None)
        self.assertNotIn('wanted', config['media_apps'])
        self.assertTrue(neko_common.media_apps_wanted(config))

    def test_nobody_at_the_terminal_keeps_yes_and_saves_nothing(self):
        config = settings(folder=self.tmp.name)
        with mock.patch.object(guided_setup, 'interactive', return_value=False):
            guided_setup.choose_media_apps(context(config), prompt=mock.Mock(side_effect=AssertionError))
        self.assertTrue(neko_common.media_apps_wanted(config))
        self.assertNotIn('wanted', config.get('media_apps', {}))

    def test_chosen_in_the_window_is_not_asked(self):
        config = settings(False, folder=self.tmp.name)
        self.ask(config, [])  # no answer typed: a question would raise StopIteration
        self.assertFalse(config['media_apps']['wanted'])

    def test_a_rerun_asks_nothing_about_them(self):
        config = settings(True, folder=self.tmp.name)
        neko_common.remember_answer(self.tmp.name, neko_common.MEDIA_SECTION, 'yes')
        # Answered before: nothing is asked and nothing is said (setup's first page, which
        # every run shows, is where an app is switched on or off).
        ctx, shown = self.ask(config, [])
        self.assertEqual(shown, [])
        self.assertTrue(config['media_apps']['wanted'])

    def test_remembered_only_once_the_ssd_is_there(self):
        config = settings(False, folder=self.tmp.name)
        guided_setup.remember_media_apps(context(config))
        self.assertEqual(neko_common.optional_answers(self.tmp.name), {'media_apps': 'none'})
        unanswered = settings(folder=self.tmp.name + '/never')
        guided_setup.remember_media_apps(context(unanswered))
        self.assertFalse(Path(self.tmp.name, 'never').exists())  # nothing written for no answer

    def test_a_broken_answer_stops_setup_with_the_reason(self):
        config = settings(folder=self.tmp.name)
        config['media_apps'] = {'wanted': 'no'}
        with self.assertRaisesRegex(RuntimeError, 'must be true or false'):
            self.ask(config, [])

    def test_the_drives_step_asks_it_before_the_drives_and_keeps_it_after(self):
        source = (ROOT / 'modules/storage.py').read_text()
        body = source[source.index("raise RuntimeError('Mount configuration requires root')") :]
        self.assertLess(body.index('choose_media_apps(context)'), body.index('resolve_storage_identity('))
        self.assertLess(body.index('mount_all(context, specs)'), body.index('remember_media_apps(context)'))

    def test_the_drives_are_described_by_what_goes_on_them(self):
        self.assertIn('movies', storage.drive_label(settings(), 'media_hdd', storage.DRIVES[1][1]))
        hdd = storage.drive_label(settings(False), 'media_hdd', storage.DRIVES[1][1])
        self.assertEqual(hdd, 'the HDD (Nextcloud files and the backups)')
        self.assertNotIn('photos', storage.drive_label(settings(False), 'data_ssd', storage.DRIVES[0][1]))

    def test_only_nextclouds_place_is_asked_without_the_media_apps(self):
        config = settings(False, folder=self.tmp.name)
        config['media_mount'] = self.tmp.name
        ctx = context(config)
        with mock.patch.object(storage, 'new_place', return_value='media') as asked:
            storage.choose_locations(ctx, interactive=False)
        self.assertEqual([call.args[1] for call in asked.call_args_list], ['cloud'])
        self.assertEqual(config['locations'], {'cloud': 'media'})


class NoOnAServerWithTheAppsTests(unittest.TestCase):
    """One place decides what no means on a server that already has the media apps
    (guided_setup.settle_no_with_apps); nothing is deleted whichever way it is set."""

    def server(self, wanted=False, apps=('jellyfin', 'immich')):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for app in apps:
            folder = Path(tmp.name, 'compose', app)
            folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_text('services: {}\n')
            Path(tmp.name, 'appdata', app).mkdir(parents=True)
            Path(tmp.name, 'appdata', app, 'kept.db').write_text('data')
        return context(settings(wanted, folder=tmp.name)), Path(tmp.name)

    def kept(self, folder):
        return sorted(str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file())

    def test_the_apps_a_server_has_are_found_by_their_compose_files(self):
        ctx, folder = self.server()
        self.assertEqual(neko_common.media_apps_installed(ctx.config), ['jellyfin', 'immich'])
        new, _ = self.server(apps=())
        self.assertEqual(neko_common.media_apps_installed(new.config), [])
        # A link out of the SSD is never followed to "find" an app.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(outside.name, 'compose.yaml').write_text('')
        (folder / 'compose' / 'sonarr').symlink_to(outside.name)
        self.assertNotIn('sonarr', neko_common.media_apps_installed(ctx.config))

    def test_stop_is_what_ships_the_answer_stands_and_it_is_said(self):
        ctx, folder = self.server()
        before = self.kept(folder)
        self.assertEqual(guided_setup.settle_no_with_apps(ctx), ['jellyfin', 'immich'])
        self.assertIs(ctx.config['media_apps']['wanted'], False)
        said = '\n'.join(ctx.logs)
        self.assertIn(
            'you switched off a group this server already has (Jellyfin, Immich). They are stopped in this run', said
        )
        self.assertIn('Nothing is deleted', said)
        self.assertEqual(self.kept(folder), before)

    def test_a_new_server_and_a_yes_have_nothing_to_settle(self):
        new, _ = self.server(apps=())
        self.assertEqual(guided_setup.settle_no_with_apps(new), [])
        yes, _ = self.server(wanted=True)
        self.assertEqual(guided_setup.settle_no_with_apps(yes), [])
        self.assertEqual(new.logs + yes.logs, [])

    def test_every_way_of_saying_no_goes_through_it(self):
        # The question, the window's switch (the answer already in the settings) and a
        # run with nobody at the terminal all end in the same place.
        for interactive in (True, False):
            ctx, _ = self.server()
            with (
                mock.patch.object(guided_setup, 'interactive', return_value=interactive),
                mock.patch.object(guided_setup, 'settle_no_with_apps') as settled,
            ):
                guided_setup.choose_media_apps(ctx, prompt=mock.Mock(side_effect=AssertionError))
            settled.assert_called_once_with(ctx)


class WindowTests(unittest.TestCase):
    def test_the_window_offers_it_first(self):
        # Three switches, one for each group (tests/test_media_groups.py), first on the page.
        first = explanations.EXTRAS[:3]
        self.assertEqual(
            [(e['id'], e['section'], e['key'], e['on_default']) for e in first],
            [
                ('media_photos', 'media_apps', 'photos', True),
                ('media_movies', 'media_apps', 'movies', True),
                ('media_music', 'media_apps', 'music', True),
            ],
        )
        config = {}
        for extra in first:
            explanations.set_extra(config, extra, False)
        self.assertEqual(config, {'media_apps': {'photos': False, 'movies': False, 'music': False}})
        self.assertFalse(neko_common.media_apps_wanted(config))


class StepTests(unittest.TestCase):
    """Each step that depends on the media apps, with them left out."""

    def setUp(self):
        root = mock.patch('os.geteuid', return_value=0)
        root.start()
        self.addCleanup(root.stop)

    def test_media_sets_nothing_up_on_a_new_server(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = context(settings(False, folder=folder))
            run = mock.Mock()
            media.left_out(ctx, run=run)
        run.assert_not_called()
        self.assertIn('Media apps: left out (none of them is switched on); nothing was set up', ctx.logs)

    def test_media_stops_the_apps_a_server_had_and_keeps_their_data(self):
        with tempfile.TemporaryDirectory() as folder:
            for app in ('jellyfin', 'sonarr'):
                Path(folder, 'compose', app).mkdir(parents=True)
                Path(folder, 'compose', app, 'compose.yaml').write_text('services: {}\n')
                Path(folder, 'appdata', app).mkdir(parents=True)
            ctx = context(settings(False, folder=folder))
            run = mock.Mock()
            media.left_out(ctx, run=run)
            kept = [p.name for p in Path(folder, 'appdata').iterdir()]
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([c[c.index('-p') + 1] for c in commands], ['neko-jellyfin', 'neko-sonarr'])
        self.assertTrue(all(c[-1] == 'down' and '-v' not in c and '--volumes' not in c for c in commands))
        self.assertEqual(sorted(kept), ['jellyfin', 'sonarr'])
        self.assertTrue(any('stopped, as its group is switched off' in line for line in ctx.logs))

    def test_the_media_step_goes_there_before_anything_else(self):
        ctx = context(settings(False))
        with mock.patch.object(media, 'left_out') as left_out, mock.patch.object(media, 'check_mount') as mounts:
            media._run(ctx)
        left_out.assert_called_once_with(ctx)
        mounts.assert_not_called()

    def test_immich_is_switched_off_and_keeps_its_photos(self):
        ctx = context(settings(False))
        with mock.patch.object(photos, 'switch_off') as off:
            photos._run(ctx)
        off.assert_called_once()

    def test_music_tv_onboarding_and_hardware_video_have_nothing_to_do(self):
        config = settings(False)
        for step, words in (
            (music, 'Music: switched off (Music (Navidrome) in setup); no music player to set up'),
            (tv, 'TV setup: left out, as Movies and shows is switched off (the TV mode opens Jellyfin)'),
            (
                onboarding,
                'Connecting the apps: Movies and shows and the downloads are switched off, so there is nothing to connect',
            ),
            (hardware_video, 'Hardware video: neither Jellyfin nor Immich runs on this server; nothing to switch over'),
        ):
            with self.subTest(step=step.__name__):
                ctx = context(config)
                self.assertIsNone(step.run(ctx))
                self.assertIn(words, ctx.logs)

    def test_the_media_schedules_are_retired(self):
        # The hourly app check, the music backup and the nightly app backup.
        for step, unit in (
            (media_monitor, 'nekoserver-media-health.timer'),
            (music_backup, 'nekoserver-music-backup.timer'),
            (app_backup, 'nekoserver-app-backup.timer'),
        ):
            with self.subTest(step=step.__name__), mock.patch('modules._schedules.retire') as retire:
                step.run(context(settings(False)))
                self.assertIn(unit, [Path(p).name for p in retire.call_args.args[0]])

    def test_no_logins_are_made_for_the_apps_left_out(self):
        plan = ['media', 'photos', 'nextcloud']
        self.assertEqual(account_store.selected(settings(False), plan), ['nextcloud'])
        self.assertIn('jellyfin', account_store.selected(settings(), plan))

    def test_the_addresses_are_nextclouds_only(self):
        # The Tailscale addresses, the home gateway and the phone page follow this list.
        self.assertEqual(gateway_tools.selected(settings(False)), ['nextcloud'])
        self.assertEqual(gateway_tools.selected(settings()), list(gateway_tools.DEFAULT_SERVICES))

    def test_no_shortcuts_for_the_apps_left_out(self):
        with tempfile.TemporaryDirectory() as folder:
            for app in ('immich', 'nextcloud'):
                Path(folder, 'compose', app).mkdir(parents=True)
                Path(folder, 'compose', app, 'compose.yaml').write_text('')
            self.assertEqual(desktop.installed_apps(settings(False, folder=folder)), ['nextcloud'])

    def test_the_shared_folders_follow_what_is_there(self):
        with tempfile.TemporaryDirectory() as hdd:
            config = settings(False)
            config['media_mount'] = hdd
            self.assertEqual(samba.wanted_shares(config), {})
            Path(hdd, 'library').mkdir()  # a server that had the apps keeps sharing its films
            self.assertEqual(samba.wanted_shares(config), {'Media': 'library'})
            config['networking']['shares']['Mine'] = 'mine'
            self.assertIn('Mine', samba.wanted_shares(config))

    def test_samba_with_nothing_to_share_says_so_and_goes_on(self):
        with tempfile.TemporaryDirectory() as hdd:
            config = settings(False)
            config['media_mount'] = hdd
            ctx = context(config)
            with mock.patch.object(samba.host, 'current'):
                self.assertIsNone(samba.run(ctx))
        self.assertTrue(any(line.startswith('Shared folders: nothing to share') for line in ctx.logs))

    def test_the_first_checkup_and_acceptance_leave_their_checks_out(self):
        modules = ['system', 'storage', 'docker', 'nextcloud', 'dashboard', 'host_backup', *neko_common.MEDIA_STEPS]
        with mock.patch.object(acceptance.tv, 'available', return_value=True):
            kept = acceptance.accepted_modules(settings(False), modules)
            self.assertEqual(kept, ['system', 'storage', 'docker', 'nextcloud', 'dashboard', 'host_backup'])
            self.assertEqual(acceptance.accepted_modules(settings(), modules), modules)

    def test_the_recovery_menu_offers_the_media_restores_only_with_a_backup_of_theirs(self):
        with tempfile.TemporaryDirectory() as hdd:
            config = settings(False)
            config['media_mount'] = hdd
            keys = [key for key, _ in recovery_wizard.menu(config)]
            self.assertNotIn('photos', keys)
            self.assertNotIn('apps', keys)
            self.assertIn('cloud', keys)
            Path(hdd, 'backups/media-applications/snapshot-1-1').mkdir(parents=True)
            self.assertIn('apps', [key for key, _ in recovery_wizard.menu(config)])
        self.assertEqual(recovery_wizard.menu(settings()), recovery_wizard.MENU)


class RuntimeHandOffTests(unittest.TestCase):
    """What the runtime's jobs are given: the apps set up (their Rust side reads
    media_apps too, common::media_services)."""

    def test_the_dashboard_and_the_app_backup_see_no_media_apps(self):
        from modules import dashboard

        with mock.patch.object(dashboard, 'scope_digest', return_value='x'):
            clean = dashboard.collector_settings(
                {**settings(False), 'external_backup_mount': '/mnt/x', 'storage': {}}, 1800
            )
        self.assertEqual(clean['media'], {'services': []})
        self.assertEqual(clean['media_apps'], {'wanted': False})
        self.assertFalse(clean['photos']['deploy_immich'])
        with mock.patch.object(app_backup.native_runtime, 'storage_config', return_value={}):
            self.assertEqual(app_backup.guard_config({**settings(False), 'app_backup': {}})['media'], {'services': []})

    def test_the_runtime_reads_the_same_rule(self):
        common = (ROOT / 'runtime/src/common.rs').read_text()
        self.assertIn('cfg["media_apps"]["wanted"] != false', common)
        json.dumps(settings(False))  # (the settings stay plain JSON)


if __name__ == '__main__':
    unittest.main()
