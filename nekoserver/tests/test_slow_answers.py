"""An app that answers a byte every few seconds, from a small stand-in on this PC: each
read came within the socket's timeout, so media-connect.py and qbit_setup.py waited for
good. Every request now has a time limit in all (neko_common.TimeLimit) and says so
plainly when it runs out.
"""

import importlib.util
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('media_connect_slow', ROOT / 'media-connect.py')
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)

import neko_common  # noqa: E402
import qbit_setup  # noqa: E402

DRIP_SECONDS = 0.2  # between the bytes the stand-in sends: far within any read timeout
LIMIT = 1  # the time limit the tests give each request (seconds)
GIVE_UP = 15  # a request still waiting after this long waits for good


def dripping_app(headers_too=False):
    """An app stand-in that answers every request a byte at a time (the body; with
    `headers_too`, the status line and headers as well) and never finishes."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def drip(self, data):
            for byte in data:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                time.sleep(DRIP_SECONDS)

        def do_GET(self):
            head = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n'
            try:
                self.drip(head) if headers_too else self.wfile.write(head)
                self.drip(b' ' * 100000)
            except OSError:
                pass  # the client gave up

        do_POST = do_GET

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads, server.block_on_close = True, False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SlowAnswers(unittest.TestCase):
    def serve(self, headers_too=False):
        server = dripping_app(headers_too)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def ends(self, call):
        """What `call` raised, and how long it took; fails when it is still waiting."""
        found = []

        def run():
            started = time.monotonic()
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - what it raised is what is checked
                found.append((exc, time.monotonic() - started))

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(GIVE_UP)
        self.assertFalse(worker.is_alive(), f'still waiting after {GIVE_UP} seconds')
        self.assertTrue(found, 'the request returned an answer that never came')
        return found[0]

    def test_media_connect_gives_up_on_an_answer_that_never_ends(self):
        for headers_too in (False, True):
            port = self.serve(headers_too)
            with (
                self.subTest(headers_too=headers_too),
                mock.patch.dict(connect.PORTS, {'sonarr': port}),
                mock.patch.object(connect, 'REQUEST_TIME_LIMIT', LIMIT),
                mock.patch.object(connect, 'OPENER', neko_common.local_opener(seconds=LIMIT)),
            ):
                error, took = self.ends(lambda: connect.request('sonarr', 'key', '/system/status'))
                self.assertIsInstance(error, RuntimeError)
                self.assertIn('sonarr /system/status: no whole answer within 1 seconds', str(error))
                self.assertIn('then run setup again', str(error))
                self.assertLess(took, LIMIT + 2)

    def test_qbittorrent_setup_gives_up_on_an_answer_that_never_ends(self):
        for headers_too in (False, True):
            port = self.serve(headers_too)
            with self.subTest(headers_too=headers_too), mock.patch.object(qbit_setup, 'REQUEST_TIME_LIMIT', LIMIT):
                error, took = self.ends(lambda: qbit_setup.Client(port).call('/app/version'))
                self.assertIsInstance(error, qbit_setup.RequestFailed)
                self.assertLess(took, LIMIT + 2)


if __name__ == '__main__':
    unittest.main()
