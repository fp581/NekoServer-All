"""A film from request to the TV (deep checks 691-710), with stand-ins that behave as the
apps do: what happens to a finished download once Radarr (or Sonarr, Lidarr) has imported it.
"""

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qbit_setup  # noqa: E402

# qBittorrent's own preferences on a fresh install (src/webui/api/appcontroller.cpp:
# a limit that is off is -1): no seeding limit at all.
QBITTORRENT_DEFAULTS = {
    'save_path': '/data/downloads',
    'auto_tmm_enabled': False,
    'max_ratio_enabled': False,
    'max_ratio': -1,
    'max_seeding_time_enabled': False,
    'max_seeding_time': -1,
    'max_inactive_seeding_time_enabled': False,
    'max_inactive_seeding_time': -1,
    'max_ratio_act': 0,
}


def arr_removes(preferences, torrent):
    """Radarr's, Sonarr's and Lidarr's rule for a finished qBittorrent download, as their
    QBittorrent.cs has it (CanBeRemoved, HasReachedSeedLimit): removed (with its files)
    only when qBittorrent has stopped it and a seeding limit is reached. The torrent uses
    qBittorrent's global limits (-2), as the apps add it without limits of its own."""
    if torrent['state'] not in ('pausedUP', 'stoppedUP'):
        return False
    if preferences.get('max_ratio_enabled') and preferences['max_ratio'] - torrent['ratio'] <= 0.001:
        return True
    if preferences.get('max_seeding_time_enabled'):
        return torrent['seeding_time'] >= preferences['max_seeding_time'] * 60
    return False


def qbittorrent_stops(preferences, torrent):
    """qBittorrent's side: it stops a seeding torrent at a global limit when the action is Stop (0)."""
    if preferences.get('max_ratio_act') != 0:
        return torrent
    reached = (preferences.get('max_ratio_enabled') and torrent['ratio'] >= preferences['max_ratio']) or (
        preferences.get('max_seeding_time_enabled') and torrent['seeding_time'] >= preferences['max_seeding_time'] * 60
    )
    return dict(torrent, state='stoppedUP') if reached else torrent


class QbittorrentStandIn:
    """qBittorrent's WebUI on a free port of this PC, answering the calls setup makes as
    qBittorrent 5 does: a login cookie, its preferences (setPreferences takes a form field
    "json"), its categories. `keeps`: False for one that does not keep what it is sent."""

    def __init__(self, preferences, keeps=True):
        self.preferences = dict(preferences)
        self.saved = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length') or 0)).decode())
                path = self.path.split('/api/v2', 1)[1]
                cookie, body = None, ''
                if path == '/auth/login':
                    cookie, body = 'QBT_SID_8080=abc; path=/', ''
                elif path == '/app/preferences':
                    body = json.dumps(stand_in.preferences)
                elif path == '/app/setPreferences':
                    stand_in.saved.append(json.loads(form['json'][0]))
                    if keeps:
                        stand_in.preferences.update(stand_in.saved[-1])
                elif path == '/torrents/categories':
                    body = json.dumps({c: {'name': c, 'savePath': '/data/downloads/' + c} for c in ('tv', 'movies')})
                elif path.startswith('/torrents/info'):
                    body = '[]'
                data = body.encode()
                self.send_response(200)
                if cookie:
                    self.send_header('Set-Cookie', cookie)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        return qbit_setup.Client(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FinishedDownloadsEndTests(unittest.TestCase):
    """Deep checks 697, 699, 709, 710: with qBittorrent's defaults every download was
    shared for good, so Radarr never removed it, and its files stayed in the downloads
    folder: a second copy of every film saved in a folder of the person's, and the space
    of every film deleted or replaced kept by its hard link. The drive filled up."""

    def configure(self, preferences, keeps=True):
        qbit = QbittorrentStandIn(preferences, keeps)
        self.addCleanup(qbit.close)
        said = []
        qbit_setup.configure(
            'u', 'p', apply=True, categories=('tv', 'movies'), client=qbit.client(), output=said.append
        )
        return qbit, said

    def test_a_film_shared_for_a_week_is_removed_from_the_downloads_folder(self):
        qbit, said = self.configure(QBITTORRENT_DEFAULTS)
        # Eight days of seeding a little-wanted film: ratio 0.4.
        torrent = {'state': 'uploading', 'ratio': 0.4, 'seeding_time': 8 * 24 * 3600}
        self.assertTrue(arr_removes(qbit.preferences, qbittorrent_stops(qbit.preferences, torrent)))
        popular = {'state': 'uploading', 'ratio': 2.0, 'seeding_time': 3600}
        self.assertTrue(arr_removes(qbit.preferences, qbittorrent_stops(qbit.preferences, popular)))
        fresh = {'state': 'uploading', 'ratio': 0.5, 'seeding_time': 3600}
        self.assertFalse(arr_removes(qbit.preferences, qbittorrent_stops(qbit.preferences, fresh)))
        self.assertEqual(qbit.preferences['max_ratio_act'], 0)  # Stop, never Remove (files left behind)
        self.assertTrue(any('7 days' in line and 'Seeding Limits' in line for line in said))

    def test_a_limit_the_person_set_stays(self):
        own = dict(QBITTORRENT_DEFAULTS, max_seeding_time_enabled=True, max_seeding_time=60 * 24 * 30)
        qbit, said = self.configure(own)
        self.assertEqual(qbit.saved, [])
        self.assertFalse(any('Seeding Limits' in line for line in said))

    def test_an_answer_without_the_settings_is_left_alone(self):
        qbit, _ = self.configure({'save_path': '/data/downloads'})
        self.assertEqual(qbit.saved, [])

    def test_the_limits_must_be_kept(self):
        with self.assertRaisesRegex(RuntimeError, 'did not retain'):
            self.configure(QBITTORRENT_DEFAULTS, keeps=False)


class JellyfinStandIn:
    """Jellyfin's library calls on a free port, as Jellyfin does them: an added library
    takes the LibraryOptions sent, else LibraryOptions' own defaults, where real time
    monitoring is off (Jellyfin.Api LibraryStructureController.AddVirtualFolder,
    MediaBrowser.Model LibraryOptions); /Library/VirtualFolders/LibraryOptions replaces them."""

    def __init__(self, libraries=()):
        self.libraries = [dict(x) for x in libraries]
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, value=None, status=200):
                data = b'' if value is None else json.dumps(value).encode()
                self.send_response(status if data else 204)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == '/System/Info/Public':
                    return self.answer({'Id': 'server-1', 'StartupWizardCompleted': True})
                if self.path == '/Library/VirtualFolders':
                    return self.answer(stand_in.libraries)
                self.answer({}, 404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                body = json.loads(raw) if raw.strip() else None
                url = urllib.parse.urlsplit(self.path)
                if url.path == '/Users/AuthenticateByName':
                    return self.answer({'AccessToken': 't', 'User': {'Policy': {'IsAdministrator': True}}})
                if url.path == '/Library/VirtualFolders':
                    query = urllib.parse.parse_qs(url.query)
                    options = {'EnableRealtimeMonitor': False, 'Enabled': True}
                    options.update((body or {}).get('LibraryOptions') or {})
                    stand_in.libraries.append(
                        {
                            'Name': query['name'][0],
                            'CollectionType': query['collectionType'][0],
                            'Locations': query['paths'][0].split(','),
                            'ItemId': 'id-' + query['name'][0],
                            'LibraryOptions': options,
                        }
                    )
                    return self.answer()
                if url.path == '/Library/VirtualFolders/LibraryOptions':
                    for library in stand_in.libraries:
                        if library['ItemId'] == body['Id']:
                            library['LibraryOptions'] = body['LibraryOptions']
                    return self.answer()
                if url.path == '/Sessions/Logout':
                    return self.answer()
                self.answer({}, 404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self):
        import media_libraries

        client = media_libraries.Client('jellyfin')
        client.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        return client

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class JellyfinSeesTheFilmTests(unittest.TestCase):
    """Deep check 691: Radarr puts the film in /data/movies; Jellyfin listed it only at its
    next library scan, every 12 hours, as the libraries setup made had real time
    monitoring off (Jellyfin's default for a library added through its API)."""

    def setUp(self):
        import tempfile

        self.media = Path(tempfile.mkdtemp(prefix='agentAW-media-', dir='/var/tmp'))
        self.addCleanup(__import__('shutil').rmtree, self.media)
        for folder in ('movies', 'tv'):
            (self.media / 'library' / folder).mkdir(parents=True)

    def run_setup(self, jellyfin):
        import media_libraries

        self.addCleanup(jellyfin.close)
        client = jellyfin.client()
        results, _, ids = media_libraries.jellyfin(client, self.media, True, lambda _: 'admin', lambda _: 'secret-word')
        client.close()
        self.assertEqual([r['status'] for r in results], ['pass'] * 3)
        return {x['Locations'][0]: x['LibraryOptions'] for x in jellyfin.libraries}

    def test_the_libraries_setup_makes_are_watched(self):
        options = self.run_setup(JellyfinStandIn())
        self.assertIs(options['/data/movies']['EnableRealtimeMonitor'], True)
        self.assertIs(options['/data/tv']['EnableRealtimeMonitor'], True)

    def test_a_library_an_earlier_setup_made_is_switched_on_and_keeps_its_other_options(self):
        earlier = [
            {
                'Name': 'Movies',
                'CollectionType': 'movies',
                'Locations': ['/data/movies'],
                'ItemId': 'm1',
                'LibraryOptions': {'EnableRealtimeMonitor': False, 'PreferredMetadataLanguage': 'de'},
            },
            {
                'Name': 'TV Shows',
                'CollectionType': 'tvshows',
                'Locations': ['/data/tv'],
                'ItemId': 't1',
                'LibraryOptions': {'EnableRealtimeMonitor': True},
            },
        ]
        options = self.run_setup(JellyfinStandIn(earlier))
        self.assertEqual(options['/data/movies'], {'EnableRealtimeMonitor': True, 'PreferredMetadataLanguage': 'de'})
        self.assertIs(options['/data/tv']['EnableRealtimeMonitor'], True)


if __name__ == '__main__':
    unittest.main()
