""" "Save new ones here too" reaches Seerr (0.50.0): a request made in Seerr is saved in the
person's own folder when they chose one for new films or shows, not in the library's."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import existing_media  # noqa: E402
import seerr_providers as sp  # noqa: E402

OWN = '/data/existing/movies'


class Seerr:
    """Seerr's provider settings for Radarr, as its API answers."""

    def __init__(self, saved=(), roots=(OWN, '/data/movies')):
        self.saved, self.roots, self.posted = list(saved), roots, []

    def call(self, endpoint, method='GET', payload=None):
        if endpoint == '/settings/radarr' and method == 'POST':
            self.posted.append(payload)
            self.saved.append({**payload, 'id': 0})
            return None
        if endpoint == '/settings/radarr':
            return list(self.saved)
        # A connection test, or the saved provider read through Seerr.
        return {
            'profiles': [{'id': 1, 'name': 'HD'}],
            'rootFolders': [{'path': path} for path in self.roots],
            'urlBase': '',
        }


def radarr(roots=(OWN, '/data/movies')):
    manager = mock.Mock(headers={})
    manager.call.side_effect = lambda endpoint, *more: {
        '/system/status': {'version': '5.0.1', 'urlBase': ''},
        '/rootfolder': [{'path': path, 'accessible': True} for path in roots],
        '/qualityprofile': [{'id': 1, 'name': 'HD'}],
    }[endpoint]
    return manager


def provider(seerr, folder=None, apply=True, verify_only=False):
    with mock.patch.object(sp, 'app_key', return_value='key'):
        return sp.provider(seerr, radarr(), '/srv', 'radarr', apply, verify_only, folder)


def made(folder):
    return {
        'id': 0,
        'is4k': False,
        'isDefault': True,
        'hostname': 'radarr',
        'port': sp.SPECS['radarr'][0],
        'apiKey': 'key',
        'useSsl': False,
        'baseUrl': '',
        'activeDirectory': folder,
        'activeProfileId': 1,
        'preventSearch': False,
    }


class FolderTests(unittest.TestCase):
    def test_the_folder_follows_the_persons_choice(self):
        with mock.patch.object(existing_media, 'new_files_folder', side_effect=lambda config, kind: f'/chosen/{kind}'):
            self.assertEqual(sp.request_folders({}), {'sonarr': '/chosen/tv', 'radarr': '/chosen/movies'})
        # Nothing chosen: the library's own folders, as on every server before.
        self.assertEqual(sp.request_folders({}), {'sonarr': '/data/tv', 'radarr': '/data/movies'})

    def test_a_new_provider_saves_requests_in_the_persons_folder(self):
        seerr = Seerr()
        self.assertEqual(provider(seerr, OWN)['status'], 'pass')
        self.assertEqual(seerr.posted[0]['activeDirectory'], OWN)
        # Without a choice it is the library's, as before.
        seerr = Seerr()
        provider(seerr)
        self.assertEqual(seerr.posted[0]['activeDirectory'], '/data/movies')

    def test_the_apps_must_have_that_folder(self):
        seerr = Seerr(roots=('/data/movies',))
        app = radarr(('/data/movies',))
        with mock.patch.object(sp, 'app_key', return_value='key'), self.assertRaisesRegex(sp.SetupRequired, 'root'):
            sp.provider(seerr, app, '/srv', 'radarr', True, False, OWN)
        self.assertEqual(seerr.posted, [])

    def test_a_provider_made_before_the_choice_is_said_and_left_alone(self):
        seerr = Seerr([made('/data/movies')])
        with self.assertRaises(sp.SetupRequired) as stopped:
            provider(seerr, OWN)
        self.assertIn('Requests are still saved in /data/movies', str(stopped.exception))
        self.assertIn(f'choose the root folder {OWN}', str(stopped.exception))
        self.assertEqual(seerr.posted, [])
        # One that saves there already is verified and kept.
        seerr = Seerr([made(OWN)])
        self.assertEqual(provider(seerr, OWN)['status'], 'pass')
        self.assertEqual(seerr.posted, [])

    def test_an_api_key_made_again_in_the_app_is_said_with_where_to_paste_it(self):
        # Deep check 188: Radarr's key made again by hand. Seerr's provider still holds
        # the old one; setup said only "Existing standard providers/default routing differ".
        seerr = Seerr([{**made('/data/movies'), 'apiKey': 'the-old-key'}])
        for verify_only in (False, True):
            with self.subTest(verify_only=verify_only), self.assertRaises(sp.SetupRequired) as stopped:
                provider(seerr, apply=True, verify_only=verify_only)
            self.assertIn("Radarr's API key was made again", str(stopped.exception))
            self.assertIn('Settings, Services, Radarr', str(stopped.exception))
        self.assertEqual(seerr.posted, [])
        # Another address is still the general message.
        seerr = Seerr([{**made('/data/movies'), 'hostname': 'elsewhere'}])
        with self.assertRaisesRegex(sp.SetupRequired, 'routing differ'):
            provider(seerr)

    def test_the_readiness_check_takes_either_folder_and_no_other(self):
        """It is not told the choice: the library's folder and a person's own both pass."""
        for folder in ('/data/movies', OWN, '/data/existing/movies-new'):
            seerr = Seerr([made(folder)], roots=(folder,))
            with mock.patch.object(sp, 'app_key', return_value='key'):
                found = sp.provider(seerr, radarr((folder,)), '/srv', 'radarr', False, True)
            self.assertEqual(found['status'], 'pass', folder)
        seerr = Seerr([made('/mnt/elsewhere')], roots=('/mnt/elsewhere', '/data/movies'))
        with self.assertRaises(sp.SetupRequired):
            provider(seerr, apply=False, verify_only=True)


class ChoiceTakenBackTests(unittest.TestCase):
    """Deep check 391: "save new ones here too" switched off again (or another folder
    chosen for new files) after Seerr's provider was made. The folder is read-only for the
    apps from then on, so a request would be downloaded and never arrive; setup said only
    "root/quality profile is not valid", and the check after it passed on the path alone."""

    def test_setup_says_which_folder_requests_still_go_to_and_what_to_choose(self):
        for wanted in ('/data/movies', '/data/existing/movies-new'):
            for verify_only in (False, True):
                seerr = Seerr([made(OWN)], roots=(OWN, wanted))
                with self.subTest(wanted=wanted, verify_only=verify_only), self.assertRaises(sp.SetupRequired) as said:
                    with mock.patch.object(sp, 'app_key', return_value='key'):
                        sp.provider(
                            seerr, radarr((OWN, wanted)), '/srv', 'radarr', not verify_only, verify_only, wanted
                        )
                self.assertIn(f'Requests are still saved in {OWN}', str(said.exception))
                self.assertIn(f'Settings, Services, Radarr, and choose the root folder {wanted}', str(said.exception))
                self.assertEqual(seerr.posted, [])  # the provider is the person's to change

    def test_a_folder_that_is_nobodys_choice_keeps_the_general_message(self):
        seerr = Seerr([made('/mnt/elsewhere')], roots=('/mnt/elsewhere', '/data/movies'))
        with self.assertRaisesRegex(sp.SetupRequired, 'not valid; settings preserved'):
            provider(seerr, '/data/movies')

    def test_the_check_after_setup_is_told_the_folder_the_settings_name(self):
        import media_readiness

        config = {'data_mount': '/srv/neko', 'media': {'services': ['seerr', 'radarr']}}
        unreachable = mock.Mock(**{'return_value.call.side_effect': RuntimeError('not running')})
        with (
            mock.patch.object(media_readiness, 'media_services', return_value=['seerr', 'radarr']),
            mock.patch.object(media_readiness, 'safe_path', side_effect=Path),
            mock.patch.object(sp, 'configure', return_value=[]) as checked,
        ):
            media_readiness.check(config, unreachable)
        self.assertEqual(checked.call_args.kwargs['folders'], {'sonarr': '/data/tv', 'radarr': '/data/movies'})
        self.assertIs(checked.call_args.kwargs['verify_only'], True)
        # Settings that cannot say leave the check as it was: the folder the provider has.
        with mock.patch.object(existing_media, 'new_files_folder', side_effect=RuntimeError('damaged')):
            self.assertIsNone(media_readiness.chosen_folders(config))


class ProfileChoiceTests(unittest.TestCase):
    """The user (after gate run A): nothing is asked after the one approval that does not
    need the running server. Seerr's request profiles and Lidarr's are picked, not asked."""

    def test_seerr_takes_the_standard_profile_and_says_which(self):
        values = [{'id': 1, 'name': 'Any'}, {'id': 4, 'name': 'HD-1080p'}, {'id': 6, 'name': 'HD - 720p/1080p'}]
        with mock.patch('builtins.print') as said:
            self.assertEqual(sp.standard_profile(values, 'Sonarr request quality profile')['id'], 4)
        self.assertIn('HD-1080p (change it in Seerr', said.call_args[0][0])
        with mock.patch('builtins.print'):
            self.assertEqual(sp.standard_profile([{'id': 9, 'name': 'Mine'}], 'x')['id'], 9)  # none known: the first
        self.assertFalse(hasattr(sp, 'choose'))

    def test_lidarr_takes_standard_or_the_first(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location('media_connect', ROOT / 'media-connect.py')
        mc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mc)
        self.assertEqual(mc.standard_profile([{'id': 1, 'name': 'Any'}, {'id': 3, 'name': 'Standard'}])['id'], 3)
        self.assertEqual(mc.standard_profile([{'id': 2, 'name': 'Lossless'}])['id'], 2)
        self.assertFalse(hasattr(mc, 'choose'))
        self.assertEqual(mc.recommended_names(), ['the pirate bay', 'yts', 'eztv'])
        schemas = [{'name': 'The Pirate Bay'}, {'name': '1337x'}, {'name': 'YTS'}]
        self.assertEqual(mc.recommended_among(schemas, mc.recommended_names()), [1, 3])  # EZTV missing: left out


class SharedRequestFolderTests(unittest.TestCase):
    """Deep check 429: the runtime's hourly check (media.rs chosen_folder) is told the same
    folder for Seerr's requests as setup (request_folders): the cases both run."""

    def test_the_settings_choose_the_folder_the_runtime_expects(self):
        cases = json.loads((ROOT / 'tests/fixtures/request_folder_cases.json').read_text())
        base = json.loads((ROOT / 'config.json').read_text())
        for case in cases:
            config = {**base, 'placement': case['placement']}
            # The folders are only named: each is taken as the real folder it names.
            with mock.patch.object(existing_media, 'real_path', side_effect=lambda path, *a, **k: path):
                try:
                    found = sp.request_folders(config)
                except RuntimeError:
                    found = None
            self.assertEqual(found, case['expected'], case['name'])


if __name__ == '__main__':
    unittest.main()
