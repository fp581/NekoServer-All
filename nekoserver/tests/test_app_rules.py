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


def sonarr(required, key='key123', username='neko_a', password='pw'):
    """Sonarr v4 as its source has it: /api/v3 wants the API key; POST /login checks the
    password and sets the SonarrAuth cookie only for the right one (a redirect either
    way); /initialize.json carries the key for a signed-in session, and for every
    request from a local address when "Authentication Required" is "Disabled for Local
    Addresses" (this PC is one)."""
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
                return 302, {'Location': '/', 'Set-Cookie': 'SonarrAuth=signed; Path=/'}, b''
            return 302, {'Location': '/login?loginFailed=true'}, b''
        if path == '/initialize.json':
            if 'SonarrAuth=signed' in (headers.get('Cookie') or '') or required == 'disabledForLocalAddresses':
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
        folder = Path(self.tmp.name) / 'appdata' / 'sonarr'
        folder.mkdir(parents=True)
        (folder / 'config.xml').write_text('<Config><ApiKey>key123</ApiKey></Config>')
        self.config = {'data_mount': self.tmp.name}

    def signs_in(self, required, password='pw'):
        server = sonarr(required)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = account_setup.Client('sonarr', server.server_address[1])
        entry = {'username': 'neko_a', 'password': password, 'allow_create': False}
        account_setup.arr(client, entry, self.config, 'sonarr')

    def test_the_login_still_counts_when_local_addresses_are_let_in(self):
        self.signs_in('disabledForLocalAddresses')
        self.signs_in('enabled')

    def test_a_wrong_password_is_still_refused_when_local_addresses_are_let_in(self):
        for required in ('disabledForLocalAddresses', 'enabled'):
            with self.subTest(required=required), self.assertRaises(RuntimeError):
                self.signs_in(required, password='wrong')


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
        with self.assertRaises(RuntimeError):
            account_setup.seerr(self.client(True), dict(self.entry, password='wrong'), None)


if __name__ == '__main__':
    unittest.main()
