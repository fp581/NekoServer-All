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


if __name__ == '__main__':
    unittest.main()
