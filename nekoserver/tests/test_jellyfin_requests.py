"""Search and request in Jellyfin (jellyfin_requests.py): Jellyfin Enhanced installed and
connected to Seerr through Jellyfin's own API, as Jellyfin's source has it (Jellyfin.Api
Controllers: Packages, Repositories, Plugins, System)."""

import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jellyfin_requests  # noqa: E402
import media_libraries  # noqa: E402
import media_readiness  # noqa: E402
import onboard  # noqa: E402

PLUGIN_ID = jellyfin_requests.GUID.replace('-', '')  # as Jellyfin lists plugin IDs
WANTED = {
    'JellyseerrEnabled': True,
    'JellyseerrShowSearchResults': True,
    'JellyseerrUrls': 'http://seerr:5055',
    'JellyseerrApiKey': 'seerr-key',
    'JellyseerrAutoImportUsers': True,
}


class Jellyfin:
    """A Jellyfin as its API answers: a plugin installed waits for a restart ("Restart")
    and is "Active" after it; every change asked is noted."""

    def __init__(self, plugin=None, settings=None, repositories=None):
        self.plugin = plugin  # None, or its Status
        self.settings = dict(settings or {'JellyseerrEnabled': False, 'CustomSplashText': 'mine'})
        self.repositories = list(
            repositories or [{'Name': 'Mine', 'Url': 'https://example.org/m.json', 'Enabled': True}]
        )
        self.changes = []
        self.available = True  # the plugin's repository has a build for this Jellyfin
        self.keeps_settings = True
        self.comes_back = True
        self.down = False
        self.headers = {}

    def call(self, endpoint, method='GET', payload=None):
        if self.down:
            raise RuntimeError('jellyfin: unavailable or incompatible API response')
        path, _, query = endpoint.partition('?')
        if method != 'GET':
            self.changes.append((method, path))
        if path == '/Plugins':
            return [{'Id': 'b' * 32, 'Name': 'Another', 'Status': 'Active'}] + (
                [{'Id': PLUGIN_ID, 'Name': 'Jellyfin Enhanced', 'Version': '12.16.0.0', 'Status': self.plugin}]
                if self.plugin
                else []
            )
        if path == '/Repositories':
            if method == 'POST':
                self.repositories = payload
            return self.repositories
        if path == '/Packages/Installed/Jellyfin%20Enhanced':
            asked = dict(urllib.parse.parse_qsl(query))
            assert asked == {'assemblyGuid': jellyfin_requests.GUID, 'repositoryUrl': jellyfin_requests.REPOSITORY}
            assert any(r['Url'] == jellyfin_requests.REPOSITORY for r in self.repositories), 'repository first'
            if not self.available:
                raise RuntimeError('jellyfin: HTTP 404')
            self.plugin = 'Restart'
            return {}
        if path == '/System/Restart':
            if self.comes_back:
                self.plugin = 'Active'
            return {}
        if path == f'/Plugins/{PLUGIN_ID}/Configuration':
            if method == 'POST':
                if self.keeps_settings:
                    self.settings = payload
                return {}
            return dict(self.settings)
        raise AssertionError(f'Jellyfin was not expected to be asked {method} {endpoint}')


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SearchAndRequestTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(jellyfin_requests, 'seerr_key', return_value='seerr-key')
        patch.start()
        self.addCleanup(patch.stop)
        self.said = []

    def connect(self, jellyfin, apply=True, wanted=True):
        clock = Clock()
        return jellyfin_requests.connect(
            jellyfin, '/mnt/ssd', apply, wanted, log=self.said.append, clock=clock, sleep=clock.sleep
        )

    def rows(self, jellyfin, apply=True, wanted=True):
        """As setup's library step reports it (media_libraries.search_and_request_rows)."""
        clock = Clock()
        with (
            mock.patch.object(jellyfin_requests.time, 'monotonic', clock),
            mock.patch.object(jellyfin_requests.time, 'sleep', clock.sleep),
            mock.patch('builtins.print', self.said.append),
        ):
            return media_libraries.search_and_request_rows(jellyfin, '/mnt/ssd', apply, wanted)

    def test_it_is_installed_by_its_id_jellyfin_restarts_and_it_is_connected_to_seerr(self):
        jellyfin = Jellyfin()
        self.assertEqual(self.connect(jellyfin), [{'check': 'jellyfin:search_and_request', 'status': 'pass'}])
        self.assertEqual(
            jellyfin.changes,
            [
                ('POST', '/Repositories'),
                ('POST', '/Packages/Installed/Jellyfin%20Enhanced'),
                ('POST', '/System/Restart'),
                ('POST', f'/Plugins/{PLUGIN_ID}/Configuration'),
            ],
        )
        # The person's own repository is kept beside the plugin's.
        self.assertEqual(
            [r['Url'] for r in jellyfin.repositories], ['https://example.org/m.json', jellyfin_requests.REPOSITORY]
        )
        # Seerr's settings are set; the plugin's other settings are kept as they were.
        self.assertEqual({k: jellyfin.settings[k] for k in WANTED}, WANTED)
        self.assertEqual(jellyfin.settings['CustomSplashText'], 'mine')
        self.assertIn(
            'search and request in Jellyfin is on (Jellyfin Enhanced, connected to Seerr)', '\n'.join(self.said)
        )

    def test_a_server_where_it_is_set_up_already_is_not_changed(self):
        jellyfin = Jellyfin(plugin='Active', settings=dict(WANTED), repositories=[])
        self.assertEqual(self.connect(jellyfin)[0]['status'], 'pass')
        self.assertEqual(jellyfin.changes, [])

    def test_only_the_seerr_settings_that_differ_are_set_again(self):
        jellyfin = Jellyfin(plugin='Active', settings={**WANTED, 'JellyseerrApiKey': 'an old key'})
        self.connect(jellyfin)
        self.assertEqual(jellyfin.changes, [('POST', f'/Plugins/{PLUGIN_ID}/Configuration')])
        self.assertEqual(jellyfin.settings['JellyseerrApiKey'], 'seerr-key')

    def test_a_check_without_apply_changes_nothing(self):
        for jellyfin in (Jellyfin(), Jellyfin(plugin='Active')):
            (only,) = self.connect(jellyfin, apply=False)
            self.assertEqual(only['status'], 'needs_setup')
            self.assertTrue(only['reason'].startswith('Preview: Jellyfin Enhanced would be'))
            self.assertEqual(jellyfin.changes, [])

    def test_switched_off_it_is_neither_installed_nor_changed(self):
        jellyfin = Jellyfin(plugin='Active')
        (only,) = self.connect(jellyfin, wanted=False)
        self.assertEqual(only['status'], 'pass')
        self.assertIn('Switched off', only['reason'])
        self.assertEqual(jellyfin.changes, [])

    def test_no_build_for_this_jellyfin_is_said_and_holds_nothing_up(self):
        jellyfin = Jellyfin()
        jellyfin.available = False
        (only,) = self.rows(jellyfin)
        self.assertEqual(only['status'], 'needs_setup')
        self.assertIn('found no Jellyfin Enhanced for this Jellyfin version', only['reason'])
        self.assertIn('Seerr itself works as before', only['reason'])
        # Reported, never holding setup up: Seerr works without it.
        self.assertEqual(onboard.readiness_exit([only, {'check': 'jellyfin:current_libraries', 'status': 'pass'}]), 0)
        self.assertIn('jellyfin:search_and_request', media_readiness.REPORTED_ONLY)

    def test_a_plugin_jellyfin_will_not_run_is_said_as_jellyfin_says_it(self):
        (only,) = self.rows(Jellyfin(plugin='NotSupported'))
        self.assertEqual(only['status'], 'needs_setup')
        self.assertTrue(only['reason'].startswith('Jellyfin Enhanced is NotSupported in Jellyfin'))

    def test_jellyfin_that_does_not_start_again_is_waited_for_three_minutes_then_said(self):
        jellyfin = Jellyfin()
        jellyfin.comes_back = False
        (only,) = self.rows(jellyfin)
        self.assertEqual(only['status'], 'needs_setup')
        self.assertIn('did not start again with Jellyfin Enhanced within 3 minutes', only['reason'])

    def test_settings_jellyfin_did_not_keep_are_a_failure(self):
        jellyfin = Jellyfin(plugin='Active')
        jellyfin.keeps_settings = False
        (only,) = self.rows(jellyfin)
        self.assertEqual(only['status'], 'fail')
        self.assertIn('did not keep its Seerr settings', only['reason'])

    def test_the_switch_is_checked_like_the_other_onboarding_switches(self):
        import config_checks

        source = Path(config_checks.__file__).read_text()
        self.assertIn("'search_and_request_in_jellyfin',", source)


if __name__ == '__main__':
    unittest.main()
