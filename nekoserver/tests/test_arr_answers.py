"""media-connect.py against a small Arr API stand-in on this PC: an answer that is not
the app's JSON (a web page, an object where a list belongs) is said plainly and the
other apps are still connected, never a Python traceback that stops the step.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('media_connect_answers', ROOT / 'media-connect.py')
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)


def arr_app(body):
    """An Arr app stand-in that answers every request with `body` (status 200)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = body.encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class OddArrAnswers(unittest.TestCase):
    def test_an_odd_answer_is_said_plainly_and_the_other_apps_go_on(self):
        for body in ('<!doctype html><html>Sonarr</html>', '{"message": "odd"}', '["a", 1]'):
            server = arr_app(body)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            with (
                self.subTest(body=body),
                tempfile.TemporaryDirectory() as root,
                mock.patch.dict(connect.PORTS, {'sonarr': server.server_address[1]}),
                mock.patch.object(connect, 'app_key', return_value='key'),
                mock.patch.object(connect.account_store, 'get', return_value={'username': 'u', 'password': 'p'}),
                redirect_stdout(StringIO()) as said,
            ):
                with self.assertRaisesRegex(RuntimeError, 'Download-client setup incomplete: sonarr'):
                    connect.configure_download_clients(root, False, apps=('sonarr',), verify_qbit=False)
                self.assertIn('not what its API gives', said.getvalue())


if __name__ == '__main__':
    unittest.main()
