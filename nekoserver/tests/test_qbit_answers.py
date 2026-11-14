"""qBittorrent's refusals and odd answers, from a small WebUI stand-in on this PC: a
refused login says why (wrong password, or this PC blocked for a while), and an answer
setup does not understand is said plainly, never as a Python error that stops setup.
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qbit_setup  # noqa: E402


def webui(answers):
    """A qBittorrent stand-in on a free port: {path: (status, body, cookie)}; the login
    path defaults to a good login."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length') or 0))
            path = self.path.split('/api/v2', 1)[1]
            status, body, cookie = answers.get(path, (200, '', None))
            if path == '/auth/login' and path not in answers:
                status, body, cookie = 200, 'Ok.', 'SID=abc; path=/'
            data = body.encode()
            self.send_response(status)
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class QbittorrentAnswers(unittest.TestCase):
    def configure(self, answers):
        server = webui(answers)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = qbit_setup.Client(server.server_address[1])
        qbit_setup.configure('admin', 'secret-word', apply=True, client=client, output=lambda *_: None)

    def test_a_refused_login_says_why(self):
        for status, said in ((401, 'did not accept the saved username and password'), (403, 'Wait an hour')):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, said):
                self.configure({'/auth/login': (status, 'Fails.', None)})

    def test_an_odd_answer_is_said_plainly(self):
        for path, body in (
            ('/app/preferences', '<html>login</html>'),
            ('/app/preferences', '[]'),
            ('/torrents/categories', '{"tv": null}'),
            ('/torrents/categories', '["tv"]'),
        ):
            answers = {'/app/preferences': (200, json.dumps({'save_path': '/data/downloads'}), None)}
            answers[path] = (200, body, None)
            with self.subTest(path=path, body=body), self.assertRaisesRegex(RuntimeError, 'does not understand'):
                self.configure(answers)


if __name__ == '__main__':
    unittest.main()
