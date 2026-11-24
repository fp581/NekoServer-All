"""1.0 round T1-R13: a whole-server restore put Sonarr's and Radarr's settings back
(APP-BACKUPS.md), with the old server's qBittorrent login in them; qBittorrent is new, with
a new login. Their retries with the old one got their address banned by qBittorrent, and
setup's plain save of the new login failed, because Sonarr and Radarr test a download client
before they save it and the ban refused the test: "could not save the new qBittorrent login
(... HTTP 400: Authentication Failure); enter it in sonarr", a page that fails the same way.
Downloads stayed broken. The login is saved so that the ban cannot stop it (forceSave), the
ban is cleared by restarting qBittorrent (its ban list is in memory) when nothing in it would
be harmed by that, and the client is tested again, with the truth said."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load():
    spec = importlib.util.spec_from_file_location('media_connect_ban', ROOT / 'media-connect.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CATEGORY = {'sonarr': ('tvCategory', 'tv'), 'radarr': ('movieCategory', 'movies'), 'lidarr': ('musicCategory', 'music')}


def client(app, username='old_server_login'):
    field, category = CATEGORY[app]
    return {
        'id': 1,
        'name': 'Neko-qBittorrent',
        'enable': True,
        'fields': [
            {'name': 'host', 'value': 'qbittorrent'},
            {'name': 'port', 'value': 8080},
            {'name': 'username', 'value': username},
            {'name': 'password', 'value': '********'},
            {'name': field, 'value': category},
        ],
    }


class Rig:
    """Sonarr's and Radarr's API and qBittorrent as T1 met them: each app holds the old
    login and its address is banned. A plain save is tested first and refused; a forced one
    is taken (202); the test is refused while the ban lasts; a restart of qBittorrent ends it."""

    def __init__(self, apps=('sonarr', 'radarr'), banned=True, login_ok=False, torrents=()):
        self.banned = set(apps) if banned else set()
        self.login_ok = {app: login_ok for app in apps}
        self.calls, self.restarts = [], []
        self.torrents = list(torrents)
        self.test_reason = 'HTTP 400: Authentication failure'

    def request(self, app, key, endpoint, method='GET', payload=None, timeout=12):
        self.calls.append((app, endpoint, method))
        if method == 'GET' and endpoint == '/downloadclient':
            return [client(app)]
        if method == 'PUT' and endpoint == '/downloadclient/1':
            raise RuntimeError(f'{app} {endpoint}: HTTP 400: Authentication Failure')  # tested first: banned
        if method == 'PUT' and endpoint == '/downloadclient/1?forceSave=true':
            self.login_ok[app] = True
            return {}
        if method == 'POST' and endpoint == '/downloadclient/test':
            if app in self.banned:
                raise RuntimeError(f'{app} {endpoint}: {self.test_reason}')
            if not self.login_ok[app]:
                raise RuntimeError(f'{app} {endpoint}: HTTP 400: Authentication failure')
            return {}
        raise AssertionError((app, endpoint, method))

    def run(self, argv, timeout, what=None):
        self.restarts.append(argv)
        self.banned.clear()  # qBittorrent keeps its bans in memory
        return mock.Mock(returncode=0)

    def qbittorrent(self):
        fake = mock.Mock()
        fake.call.side_effect = lambda endpoint, form=None, json_result=False: (
            [{'name': name, 'state': state} for name, state in self.torrents] if endpoint == '/torrents/info' else ''
        )
        return fake

    def puts(self):
        return [(app, endpoint) for app, endpoint, method in self.calls if method == 'PUT']


class BannedDownloadClientTests(unittest.TestCase):
    def setUp(self):
        self.connect = load()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.stored = {'username': 'neko_new', 'password': 'new-' + 'x' * 12, 'state': 'verified'}

    def connected(self, rig, apps=('sonarr', 'radarr'), pass_on=False):
        said = []
        with (
            mock.patch.object(self.connect, 'request', rig.request),
            mock.patch.object(self.connect, 'app_key', return_value='k'),
            mock.patch.object(self.connect.account_store, 'get', return_value=self.stored),
            mock.patch.object(self.connect.qbit_setup, 'configure'),
            mock.patch.object(self.connect.qbit_setup, 'Client', side_effect=rig.qbittorrent),
            mock.patch.object(self.connect, 'checked_run', side_effect=rig.run, create=True),
            mock.patch('time.sleep'),
            mock.patch('builtins.print', side_effect=lambda *a: said.append(' '.join(map(str, a)))),
        ):
            try:
                if pass_on:
                    self.connect.pass_on_login(self.root, apps)
                else:
                    self.connect.configure_download_clients(self.root, True, apps)
                error = None
            except RuntimeError as exc:
                error = str(exc)
        return '\n'.join(said), error

    def test_a_banned_client_gets_the_new_login_and_qbittorrent_is_restarted_once(self):
        rig = Rig()
        said, error = self.connected(rig)
        self.assertIsNone(error)
        self.assertEqual(
            rig.puts(), [('sonarr', '/downloadclient/1?forceSave=true'), ('radarr', '/downloadclient/1?forceSave=true')]
        )
        self.assertEqual(len(rig.restarts), 1)  # one restart clears both bans
        restart = rig.restarts[0]
        self.assertEqual(restart[:2], ['docker', 'compose'])
        self.assertIn('neko-qbittorrent', restart)
        self.assertEqual(restart[-1], 'restart')
        self.assertIn(str(Path(self.root) / 'compose/qbittorrent/compose.yaml'), restart)
        self.assertIn('qBittorrent was restarted', said)
        self.assertIn('sonarr: its download client signs in to qBittorrent with the current login', said)
        self.assertIn('radarr: its download client signs in to qBittorrent with the current login', said)
        self.assertNotIn('Settings, Download Clients', said)

    def test_the_command_for_a_changed_login_does_the_same(self):
        rig = Rig()
        said, error = self.connected(rig, pass_on=True)
        self.assertIsNone(error)
        self.assertEqual(len(rig.restarts), 1)
        self.assertIn('sonarr: its download client signs in to qBittorrent with the current login', said)

    def test_a_torrent_being_moved_or_checked_is_never_cut_off_by_a_restart(self):
        for state in ('moving', 'checkingDL', 'checkingUP', 'checkingResumeData', 'allocating'):
            rig = Rig(torrents=[('Some.Show.S01E01', 'downloading'), ('Some.Film', state)])
            said, error = self.connected(rig)
            self.assertEqual(rig.restarts, [], state)
            self.assertIn('sonarr, radarr', error)
            self.assertIn('qBittorrent was not restarted', said)
            self.assertIn('1 download is being moved or checked', said)
            # What works: waiting (the ban ends by itself) or setup again; never the app's
            # own page, which is refused the same way while the ban lasts.
            self.assertIn('ends by itself within an hour', said)
            self.assertIn('sudo bash install.sh  again', said)
            self.assertNotIn('Settings, Download Clients', said)

    def test_qbittorrent_that_cannot_say_what_it_is_doing_is_not_restarted(self):
        rig = Rig()
        rig.qbittorrent = lambda: mock.Mock(login=mock.Mock(side_effect=RuntimeError('qBittorrent did not answer')))
        said, error = self.connected(rig)
        self.assertEqual(rig.restarts, [])
        self.assertIn('qBittorrent was not restarted', said)
        self.assertIsNotNone(error)

    def test_a_login_that_did_not_change_restarts_nothing(self):
        rig = Rig(banned=False, login_ok=True)
        said, error = self.connected(rig)
        self.assertIsNone(error)
        self.assertEqual(rig.puts(), [])
        self.assertEqual(rig.restarts, [])
        self.assertIn('existing client verified', said)

    def test_a_failure_that_is_not_a_ban_or_a_login_is_said_and_restarts_nothing(self):
        rig = Rig()
        rig.test_reason = 'HTTP 400: Unable to connect to qBittorrent'
        said, error = self.connected(rig)
        self.assertEqual(rig.restarts, [])
        self.assertIn('sonarr', error)
        self.assertIn('Unable to connect to qBittorrent', said)
        self.assertNotIn('Settings, Download Clients', said)

    def test_a_ban_still_there_after_the_restart_is_said_as_such(self):
        rig = Rig()
        rig.run = lambda argv, timeout, what=None: rig.restarts.append(argv)  # the restart does not clear it
        said, error = self.connected(rig)
        self.assertEqual(len(rig.restarts), 1)
        self.assertIn('sonarr, radarr', error)
        self.assertIn('ends by itself within an hour', said)
        self.assertNotIn('Settings, Download Clients', said)


if __name__ == '__main__':
    unittest.main()
