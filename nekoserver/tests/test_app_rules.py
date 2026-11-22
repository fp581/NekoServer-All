"""The apps' own rules (deep checks 461-475): account setup against small stand-ins of
the apps on this PC that behave as each app's documentation and source say, reached
through setup's real HTTP client. Each case is a setting a person may change in the app
itself, or an app busy after an update."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_setup  # noqa: E402


def stand_in(answer):
    """An app on 127.0.0.1 (a port the system picks): `answer(method, path, headers,
    body)` gives (status, extra headers, body as bytes or a JSON value)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_one(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            status, headers, data = answer(self.command, self.path, self.headers, body)
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = handle_one

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def sonarr(required, key='key123', username='neko_a', password='pw', app='Sonarr'):
    """Sonarr v4 (or Radarr 7, `app`) as its source has it: /api/v3 wants the API key;
    POST /login checks the password and sets the <app>Auth cookie only for the right one
    (a redirect either way); /initialize.json carries the key for a signed-in session,
    and for every request from a local address when "Authentication Required" is
    "Disabled for Local Addresses" (this PC is one). Radarr 7's "Disabled for Localhost"
    lets in only the container's own loopback (UiAuthorizationHandler, IPAddress.IsLoopback),
    and setup reaches the container through Docker's port mapping, from Docker's gateway:
    it is asked to sign in."""
    cookie = app + 'Auth=signed'
    host = {
        'id': 1,
        'authenticationMethod': 'forms',
        'authenticationRequired': required,
        'username': username,
        'password': '$hash$',
    }

    def answer(method, path, headers, body):
        if path.startswith('/api/v3/'):
            if headers.get('X-Api-Key') != key:
                return 401, {}, b''
            if path == '/api/v3/config/host':
                return 200, {}, host
            return 200, {}, {}
        if path == '/login' and method == 'POST':
            form = urllib.parse.parse_qs(body.decode())
            if form.get('username') == [username] and form.get('password') == [password]:
                return 302, {'Location': '/', 'Set-Cookie': cookie + '; Path=/'}, b''
            return 302, {'Location': '/login?loginFailed=true'}, b''
        if path == '/initialize.json':
            if cookie in (headers.get('Cookie') or '') or required == 'disabledForLocalAddresses':
                return 200, {}, {'apiRoot': '/api/v3', 'apiKey': key}
            return 302, {'Location': '/login'}, b''
        return 404, {}, b''

    return stand_in(answer)


class MediaManagerSignInChoice(unittest.TestCase):
    """Sonarr, Radarr, Lidarr and Prowlarr offer two "Authentication Required" choices.
    Setup picks "Enabled"; a person who later picks "Disabled for Local Addresses" (in the
    app: Settings, General, Security) still has a login the app asks for, and the saved
    one must still be found working on later setup runs and before each weekly update."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for app in ('sonarr', 'radarr'):
            folder = Path(self.tmp.name) / 'appdata' / app
            folder.mkdir(parents=True)
            (folder / 'config.xml').write_text('<Config><ApiKey>key123</ApiKey></Config>')
        self.config = {'data_mount': self.tmp.name}

    def signs_in(self, required, password='pw', app='sonarr'):
        server = sonarr(required, app=app.capitalize())
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = account_setup.Client(app, server.server_address[1])
        entry = {'username': 'neko_a', 'password': password, 'allow_create': False}
        account_setup.arr(client, entry, self.config, app)

    def test_the_login_still_counts_when_local_addresses_are_let_in(self):
        self.signs_in('disabledForLocalAddresses')
        self.signs_in('enabled')

    def test_a_wrong_password_is_still_refused_when_local_addresses_are_let_in(self):
        for required in ('disabledForLocalAddresses', 'enabled'):
            with self.subTest(required=required), self.assertRaises(RuntimeError):
                self.signs_in(required, password='wrong')

    def test_radarr_7s_disabled_for_localhost_still_asks_and_counts(self):
        """Deep check 535: Radarr 7.0 (on :latest since 14 November 2026) has a third
        choice, "Disabled for Localhost" (Sonarr 5 has it too). Picked there, setup said on
        every run that the saved login did not work, and the weekly update lost its check."""
        self.signs_in('disabledForLocalhost', app='radarr')
        with self.assertRaises(RuntimeError):
            self.signs_in('disabledForLocalhost', password='wrong', app='radarr')


def seerr(local_login):
    """Seerr as its source has it (server/routes/auth.ts): with "Enable Local Sign-In" off,
    POST /auth/local answers every password with HTTP 500 "Password sign-in is disabled.";
    the public settings say whether it is on (localLogin)."""

    def answer(method, path, headers, body):
        if path == '/api/v1/settings/public':
            return 200, {}, {'initialized': True, 'localLogin': local_login}
        if path == '/api/v1/auth/local' and method == 'POST':
            if not local_login:
                return 500, {}, {'error': 'Password sign-in is disabled.'}
            sent = json.loads(body)
            if sent == {'email': 'neko_s@server.invalid', 'password': 'pw'}:
                return 200, {'Set-Cookie': 'connect.sid=s1; Path=/'}, {'id': 1}
            return 403, {}, {'message': 'Access denied.'}
        if path == '/api/v1/auth/me':
            return 200, {}, {'id': 1, 'email': 'neko_s@server.invalid'}
        if path == '/api/v1/auth/logout':
            return 200, {}, {'status': 'ok'}
        return 404, {}, b''

    return stand_in(answer)


class SeerrPasswordSignInOff(unittest.TestCase):
    """A person who has their users sign in to Seerr with Jellyfin only switches off
    "Enable Local Sign-In" (Settings, Users). Seerr then takes no password at all: the saved
    one is "not tried", never "refused, type the login Seerr has now", and the weekly
    update is told it could not be tried."""

    def setUp(self):
        self.entry = {'username': 'neko_s@server.invalid', 'password': 'pw', 'allow_create': False}

    def client(self, local_login):
        server = seerr(local_login)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return account_setup.Client('seerr', server.server_address[1])

    def test_switched_off_is_said_as_not_tried(self):
        note = account_setup.seerr(self.client(False), self.entry, None)
        self.assertEqual(note, account_setup.SEERR_PASSWORD_OFF)
        self.assertIn('Enable Local Sign-In', note)

    def test_the_weekly_check_is_told_it_could_not_be_tried(self):
        client = self.client(False)
        with (
            mock.patch.object(account_setup.store, 'get', return_value=dict(self.entry, state='verified')),
            mock.patch.object(account_setup, 'verify_project', return_value=0),
            mock.patch.object(account_setup, 'wait'),
        ):
            self.assertIsNone(account_setup.signs_in({'data_mount': '/x'}, 'seerr', lambda *_: client))

    def test_with_password_sign_in_on_the_password_is_still_tried(self):
        self.assertIsNone(account_setup.seerr(self.client(True), self.entry, None))
        client, wrong = self.client(True), dict(self.entry, password='wrong')
        with self.assertRaises(RuntimeError):
            account_setup.seerr(client, wrong, None)


class AppStillStartingAfterAnUpdate(unittest.TestCase):
    """After an update an app can take many minutes to update its database before it
    answers (Jellyfin 12 answers every request with 503 meanwhile). A setup run then is
    told to try again once it answers; never to type the app's login again, which it has
    not refused."""

    def test_a_busy_app_is_tried_again_not_retyped(self):
        server = stand_in(lambda *_: (503, {}, b'Jellyfin is starting'))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        entry = {'username': 'neko_j', 'password': 'pw', 'allow_create': False, 'state': 'verified'}
        said, saved = [], {}
        with (
            mock.patch.object(account_setup.store, 'get', return_value=entry),
            mock.patch.object(account_setup.store, 'update', side_effect=lambda c, a, **v: saved.update(v)),
            mock.patch.object(account_setup.store, 'fresh', return_value=False),
            mock.patch.object(account_setup, 'verify_project', return_value=server.server_address[1]),
        ):
            pending = account_setup.provision(
                {'data_mount': '/x', 'accounts': {'wait_seconds': 2}},
                ['jellyfin'],
                said.append,
                lambda app, port=None: account_setup.Client(app, server.server_address[1]),
            )
        self.assertEqual(pending, ['jellyfin'])
        text = ' '.join(said)
        self.assertIn('did not answer within 2 seconds', text)
        self.assertIn('once Jellyfin administrator answers', text)
        self.assertNotIn('type the login', text)
        self.assertEqual(saved.get('state'), 'needs_setup')


def qbittorrent(let_in, password='pw'):
    """qBittorrent 5's Web UI API as its source has it: a computer it lets in without a
    password ("Bypass authentication for clients on localhost" or "... in whitelisted IP
    subnets") is given a session whatever it sends; otherwise only the right password
    gets one (an empty 204 and the QBT_SID_<port> cookie)."""

    def answer(method, path, headers, body):
        signed = 'QBT_SID_8080=s1' in (headers.get('Cookie') or '')
        if path == '/api/v2/auth/login':
            form = urllib.parse.parse_qs(body.decode())
            if let_in or (form.get('username') == ['neko_q'] and form.get('password') == [password]):
                return 204, {'Set-Cookie': 'QBT_SID_8080=s1; Path=/'}, b''
            return 401, {}, b'Fails.'
        if not (signed or let_in):
            return 403, {}, b'Forbidden'
        if path == '/api/v2/app/preferences':
            return (
                200,
                {},
                {
                    'web_ui_username': 'neko_q',
                    'bypass_local_auth': False,
                    'bypass_auth_subnet_whitelist_enabled': let_in,
                    'bypass_auth_subnet_whitelist': '172.16.0.0/12' if let_in else '',
                },
            )
        if path == '/api/v2/auth/logout':
            return 200, {}, b''
        return 404, {}, b''

    return stand_in(answer)


class QbittorrentLetsInWithoutAPassword(unittest.TestCase):
    """People often tick "Bypass authentication for clients in whitelisted IP subnets" in
    qBittorrent, with Docker's network, so their apps need no password. qBittorrent then
    takes any password from there: the saved one is "not tried", never "type the login
    qBittorrent has now", and the weekly update is told it could not be tried."""

    entry = {'username': 'neko_q', 'password': 'pw', 'allow_create': False, 'state': 'verified'}

    def client(self, let_in):
        server = qbittorrent(let_in)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return account_setup.Client('qbittorrent', server.server_address[1])

    def test_let_in_is_said_as_not_tried(self):
        self.assertEqual(account_setup.qbittorrent(self.client(True), self.entry), account_setup.QBITTORRENT_LET_IN)
        with (
            mock.patch.object(account_setup.store, 'get', return_value=self.entry),
            mock.patch.object(account_setup, 'verify_project', return_value=0),
            mock.patch.object(account_setup, 'wait'),
        ):
            client = self.client(True)
            self.assertIsNone(account_setup.signs_in({'data_mount': '/x'}, 'qbittorrent', lambda *_: client))

    def test_without_it_the_password_is_tried(self):
        self.assertIsNone(account_setup.qbittorrent(self.client(False), self.entry))
        client, wrong = self.client(False), dict(self.entry, password='wrong')
        with self.assertRaises(RuntimeError):
            account_setup.qbittorrent(client, wrong)


if __name__ == '__main__':
    unittest.main()
