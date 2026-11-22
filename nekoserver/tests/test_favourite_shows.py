"""Favourite shows keep coming (favourite_shows.py): a show favourited in Jellyfin that
Sonarr does not follow is given to Sonarr, watching only episodes still to air."""

import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import favourite_shows  # noqa: E402
import media_readiness  # noqa: E402
import seerr_providers  # noqa: E402

BREAKING_BAD, THE_WIRE, SEVERANCE = '81189', '79126', '371980'


class Apps:
    """Jellyfin, Sonarr and Seerr as the job sees them, answering from what a test sets
    and noting every change asked of them."""

    def __init__(self):
        self.users = [{'Id': 'u1', 'Name': 'Dana'}, {'Id': 'u2', 'Name': 'Noa'}]
        self.favourites = {'u1': [], 'u2': []}
        self.sonarr_has = []
        self.lookup = {}
        self.seerr_profile = 6
        self.sonarr_roots = ['/data/tv']
        self.added = []
        self.closed = []

    def factory(self, app):
        apps = self

        class Fake:
            def __init__(self):
                self.app, self.headers = app, {}

            def call(self, endpoint, method='GET', payload=None):
                return apps.answer(app, endpoint, method, payload)

            def close(self):
                apps.closed.append(app)

        return Fake()

    def answer(self, app, endpoint, method, payload):
        path, _, query = endpoint.partition('?')
        asked = dict(urllib.parse.parse_qsl(query))
        if app == 'jellyfin':
            if path == '/System/Info/Public':
                return {'Id': 'server-1', 'StartupWizardCompleted': True}
            if path == '/Users':
                return self.users
            if path == '/Items':
                assert asked == {
                    'userId': asked['userId'],
                    'includeItemTypes': 'Series',
                    'recursive': 'true',
                    'filters': 'IsFavorite',
                    'fields': 'ProviderIds,Path',
                }, asked
                return {'Items': self.favourites[asked['userId']]}
        if app == 'sonarr':
            if path == '/series' and method == 'GET':
                return [{'tvdbId': int(t)} for t in self.sonarr_has]
            if path == '/series/lookup':
                tvdb = asked['term'].removeprefix('tvdb:')
                if tvdb not in self.lookup:
                    return []
                return [{'tvdbId': int(tvdb), 'title': self.lookup[tvdb], 'folder': self.lookup[tvdb], 'seasons': []}]
            if path == '/series' and method == 'POST':
                self.added.append(payload)
                return payload
            if path == '/rootfolder':
                return [{'id': n, 'path': p} for n, p in enumerate(self.sonarr_roots, 1)]
            if path == '/qualityprofile':
                return [{'id': 1, 'name': 'Any'}, {'id': 4, 'name': 'HD-1080p'}]
        if app == 'seerr':
            if path == '/settings/public':
                return {'initialized': True}
            if path == '/settings/sonarr':
                return [{'id': 0, 'is4k': False, 'isDefault': True, 'activeProfileId': self.seerr_profile}]
        raise AssertionError(f'{app} was not expected to be asked {method} {endpoint}')


def show(name, tvdb, folder=None):
    """A favourite as Jellyfin lists it; `folder`: its folder as the apps see it."""
    item = {'Name': name, 'ProviderIds': {'Tvdb': tvdb} if tvdb else {}}
    if folder:
        item['Path'] = folder
    return item


class FavouriteShowsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.apps = Apps()
        self.config = {'data_mount': str(self.data), 'media': {'services': ['jellyfin', 'seerr', 'sonarr']}}
        for patch in (
            mock.patch.object(favourite_shows, 'media_services', lambda config: config['media']['services']),
            mock.patch.object(media_readiness, 'jellyfin_key', lambda *a: 'jellyfin-key'),
            mock.patch.object(seerr_providers, 'app_key', lambda *a: 'sonarr-key'),
            mock.patch.object(favourite_shows, 'open_seerr', lambda client, *a: None),
            mock.patch.object(seerr_providers, 'request_folders', lambda config: {'sonarr': '/data/tv'}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_job(self):
        return favourite_shows.run_once(self.config, factory=self.apps.factory, now=lambda: 1000)

    def test_a_favourite_sonarr_does_not_follow_is_given_to_it_for_new_episodes_only(self):
        # In a folder of Dana's the apps may only read: the new episodes go where
        # requests are saved, in a folder of the same name, and the account says so.
        self.apps.favourites['u1'] = [
            show('Breaking Bad', BREAKING_BAD, folder='/data/existing/tv/Breaking Bad (2008)')
        ]
        self.apps.lookup[BREAKING_BAD] = 'Breaking Bad'
        account = self.run_job()
        self.assertEqual(
            account['followed'],
            [{'title': 'Breaking Bad', 'by': ['Dana'], 'saved_in': '/data/tv/Breaking Bad (2008)', 'apart': True}],
        )
        (added,) = self.apps.added
        # Episodes still to air, and every new season; nothing searched for now.
        self.assertEqual(
            added['addOptions'],
            {'monitor': 'future', 'searchForMissingEpisodes': False, 'searchForCutoffUnmetEpisodes': False},
        )
        self.assertEqual((added['monitored'], added['monitorNewItems']), (True, 'all'))
        # Where Seerr's requests go, with Seerr's quality.
        self.assertEqual(added['qualityProfileId'], 6)
        self.assertEqual(added['rootFolderPath'], '/data/tv')
        self.assertEqual(added['path'], '/data/tv/Breaking Bad (2008)')
        self.assertEqual(added['tvdbId'], int(BREAKING_BAD))
        self.assertEqual(sorted(set(self.apps.closed)), ['jellyfin', 'seerr', 'sonarr'])

    def test_new_episodes_land_beside_the_old_ones_where_sonarr_may_save(self):
        """A folder of the person's chosen for new downloads (one of Sonarr's root
        folders), and a show copied into NekoServer's library: the show's own folder."""
        self.apps.sonarr_roots = ['/data/tv', '/data/existing/tv-new']
        self.apps.favourites['u1'] = [
            show('Severance', SEVERANCE, folder='/data/existing/tv-new/Severance'),
            show('The Wire', THE_WIRE, folder='/data/tv/The Wire (2002)'),
        ]
        self.apps.lookup.update({SEVERANCE: 'Severance', THE_WIRE: 'The Wire'})
        account = self.run_job()
        self.assertEqual(
            [(a['rootFolderPath'], a['path']) for a in self.apps.added],
            [('/data/existing/tv-new', '/data/existing/tv-new/Severance'), ('/data/tv', '/data/tv/The Wire (2002)')],
        )
        self.assertFalse(any(f.get('apart') for f in account['followed']))

    def test_a_show_sonarr_has_already_is_left_as_it_is(self):
        """Followed or not there: what Sonarr has is the person's to manage."""
        self.apps.favourites['u1'] = [show('The Wire', THE_WIRE)]
        self.apps.sonarr_has = [THE_WIRE]
        account = self.run_job()
        self.assertEqual(self.apps.added, [])
        self.assertEqual(account['already'], ['The Wire'])
        self.assertEqual(account['followed'], [])

    def test_a_show_two_people_like_is_added_once(self):
        self.apps.favourites = {'u1': [show('Severance', SEVERANCE)], 'u2': [show('Severance', SEVERANCE)]}
        self.apps.lookup[SEVERANCE] = 'Severance'
        account = self.run_job()
        self.assertEqual(len(self.apps.added), 1)
        self.assertEqual(account['followed'][0]['by'], ['Dana', 'Noa'])

    def test_a_show_that_cannot_be_followed_is_said_and_the_others_still_are(self):
        self.apps.favourites['u1'] = [
            show('Home video', None),  # Jellyfin does not know which show it is
            show('Not in Sonarr', '999'),  # Sonarr's list has no such show
            show('Severance', SEVERANCE),
        ]
        self.apps.lookup[SEVERANCE] = 'Severance'
        account = self.run_job()
        self.assertEqual([a['tvdbId'] for a in self.apps.added], [int(SEVERANCE)])
        self.assertEqual(
            account['not_followed'],
            [
                {'title': 'Home video', 'why': 'Jellyfin does not know which show it is (no TVDB ID)'},
                {'title': 'Not in Sonarr', 'why': 'Sonarr could not find it in its show list (TVDB 999)'},
            ],
        )

    def test_without_seerr_it_takes_the_profile_setup_would_pick(self):
        self.config['media']['services'] = ['jellyfin', 'sonarr']
        self.apps.favourites['u1'] = [show('Severance', SEVERANCE)]
        self.apps.lookup[SEVERANCE] = 'Severance'
        self.run_job()
        self.assertEqual(self.apps.added[0]['qualityProfileId'], 4)  # HD-1080p, as STANDARD_PROFILES has it
        self.assertEqual(self.apps.added[0]['path'], '/data/tv/Severance')  # Sonarr's own folder name

    def test_without_jellyfin_or_sonarr_nothing_is_asked(self):
        for services in (['sonarr', 'seerr'], ['jellyfin', 'seerr']):
            self.config['media']['services'] = services
            account = self.run_job()
            self.assertIn('nothing to follow', account['said'])
        self.assertEqual(self.apps.closed, [])

    def test_the_run_is_kept_for_the_dashboard_and_said_in_one_line(self):
        self.apps.favourites['u1'] = [show('Severance', SEVERANCE), show('The Wire', THE_WIRE)]
        self.apps.sonarr_has = [THE_WIRE]
        self.apps.lookup[SEVERANCE] = 'Severance'
        settings = self.data / 'config.json'
        settings.write_text(json.dumps(self.config))
        printed = []
        with (
            mock.patch.object(favourite_shows, 'read_settings', lambda path: self.config),
            mock.patch.object(favourite_shows, 'Client', self.apps.factory),
            mock.patch('builtins.print', printed.append),
        ):
            self.assertEqual(favourite_shows.main(['run', '--config', str(settings)]), 0)
        kept = json.loads((self.data / favourite_shows.REPORT).read_text())
        self.assertEqual(kept['format'], 'neko-favourite-shows-v1')
        self.assertEqual(kept['followed'], [{'title': 'Severance', 'by': ['Dana'], 'saved_in': '/data/tv/Severance'}])
        self.assertEqual(printed, ['Favourite shows: 1 now followed, 1 followed already'])

    def test_apps_that_do_not_answer_are_tried_again_next_hour(self):
        def down(app):
            raise RuntimeError(f'{app}: unavailable or incompatible API response')

        printed = []
        with (
            mock.patch.object(favourite_shows, 'read_settings', lambda path: self.config),
            mock.patch.object(favourite_shows, 'Client', down),
            mock.patch('builtins.print', printed.append),
        ):
            self.assertEqual(favourite_shows.main(['run', '--config', 'x.json']), 1)
        self.assertEqual(
            printed, ['Favourite shows: not looked at this time: jellyfin: unavailable or incompatible API response']
        )
        kept = json.loads((self.data / favourite_shows.REPORT).read_text())
        self.assertEqual(kept['failed'], 'jellyfin: unavailable or incompatible API response')


if __name__ == '__main__':
    unittest.main()


class ScheduleTests(unittest.TestCase):
    """Setup's step that connects the apps sets the hourly job up (modules/onboarding.py)."""

    def setUp(self):
        import neko_common
        from modules import onboarding

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.units = Path(tmp.name) / 'systemd'
        self.units.mkdir()
        self.onboarding = onboarding
        self.ran = []
        for patch in (
            mock.patch.object(neko_common, 'SYSTEMD_UNITS', self.units),
            mock.patch.object(neko_common, 'TIMER_STAMPS', Path(tmp.name) / 'stamps'),
            mock.patch.object(onboarding, 'SYSTEMD_UNITS', self.units),
            mock.patch.object(onboarding.os, 'geteuid', lambda: 0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.said = []
        self.ctx = mock.Mock(
            config={'data_mount': tmp.name, 'media': {'services': ['jellyfin', 'sonarr']}}, log=self.said.append
        )

    def step(self, services):
        self.ctx.config['media']['services'] = services
        with mock.patch.object(self.onboarding, 'media_services', lambda config: config['media']['services']):
            self.onboarding.follow_favourite_shows(self.ctx, run=lambda cmd, **_: self.ran.append(' '.join(cmd)))

    def test_every_setup_run_and_upgrade_writes_this_releases_job_even_when_connecting_fails(self):
        """An upgrade's setup whose connection check did not pass left the job running the
        previous release's copy of itself."""
        ctx = self.ctx
        ctx.apply = True
        real = self.onboarding.follow_favourite_shows

        def job_step(ctx):
            real(ctx, run=lambda cmd, **_: self.ran.append(' '.join(cmd)))

        ctx.config['onboarding'] = {}
        with (
            mock.patch.object(self.onboarding, 'media_services', lambda config: ['jellyfin', 'sonarr']),
            mock.patch.object(self.onboarding, 'apps_to_connect', lambda config: True),
            mock.patch.object(self.onboarding, 'follow_favourite_shows', side_effect=job_step) as job,
            mock.patch.object(self.onboarding, 'docker_running', side_effect=RuntimeError('Docker is not running')),
            mock.patch.object(self.onboarding.subprocess, 'run'),
        ):
            with self.assertRaises(RuntimeError):
                self.onboarding.run(ctx)
        job.assert_called_once()
        release = Path(ctx.config['data_mount']) / 'installer/releases'
        self.assertIn(str(release), (self.units / 'nekoserver-favourite-shows.service').read_text())

    def test_it_runs_every_hour_where_jellyfin_and_sonarr_both_are_and_goes_when_one_does_not(self):
        self.step(['jellyfin', 'seerr', 'sonarr'])
        service = (self.units / 'nekoserver-favourite-shows.service').read_text()
        timer = (self.units / 'nekoserver-favourite-shows.timer').read_text()
        self.assertIn('/favourite_shows.py run --config ', service)
        self.assertIn('OnUnitInactiveSec=60min', timer)
        self.assertIn('systemctl enable --now nekoserver-favourite-shows.timer', self.ran)
        # Sonarr switched off: the job is taken away, its files too.
        self.ran.clear()
        self.step(['jellyfin', 'seerr'])
        self.assertIn('systemctl disable --now nekoserver-favourite-shows.timer', self.ran)
        self.assertEqual(list(self.units.iterdir()), [])
