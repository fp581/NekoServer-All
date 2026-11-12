""" "Save new ones here too" reaches Seerr (0.50.0): a request made in Seerr is saved in the
person's own folder when they chose one for new films or shows, not in the library's."""

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
        return sp.provider(seerr, radarr(), '/srv', 'radarr', apply, input, verify_only, folder)


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
            sp.provider(seerr, app, '/srv', 'radarr', True, input, False, OWN)
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

    def test_the_readiness_check_takes_either_folder_and_no_other(self):
        """It is not told the choice: the library's folder and a person's own both pass."""
        for folder in ('/data/movies', OWN, '/data/existing/movies-new'):
            seerr = Seerr([made(folder)], roots=(folder,))
            with mock.patch.object(sp, 'app_key', return_value='key'):
                found = sp.provider(seerr, radarr((folder,)), '/srv', 'radarr', False, input, True)
            self.assertEqual(found['status'], 'pass', folder)
        seerr = Seerr([made('/mnt/elsewhere')], roots=('/mnt/elsewhere', '/data/movies'))
        with self.assertRaises(sp.SetupRequired):
            provider(seerr, apply=False, verify_only=True)


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


if __name__ == '__main__':
    unittest.main()
