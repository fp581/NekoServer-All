"""Odd answers from outside services, from a stand-in web server on this PC: setup's
downloads say what went wrong in plain words (deep check 237)."""

import http.server
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common


class StandIn(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/cut':  # says 1000 bytes, sends 10, then closes
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            self.wfile.write(b'{"tag_name"')
            return
        if self.path == '/drip':  # one byte a second, for longer than any time limit
            self.send_response(200)
            self.end_headers()
            for _ in range(30):
                try:
                    self.wfile.write(b' ')
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(1)
            return
        self.send_response(200)
        if self.path == '/big':
            self.end_headers()
            self.wfile.write(b'services:' + b' ' * 5000)
            return
        self.send_header('X-Big', 'a' * 100_000)  # a header line no web site sends
        self.end_headers()


class OutsideAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), StandIn)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_cut_off_or_odd_answer_is_said_in_plain_words(self):
        """Python's own words came out before: "got more than 65536 bytes when reading
        header line", "IncompleteRead(10 bytes read, 990 more expected)"."""
        for path in ('/header', '/cut'):
            with self.subTest(path=path), self.assertRaises(RuntimeError) as said:
                neko_common.fetch(self.base + path, 'Finding the current Immich release', 100_000, 10)
            self.assertIn(
                'Finding the current Immich release: the answer from ' + self.base + path, str(said.exception)
            )
            self.assertIn('try again later', str(said.exception))
        with self.assertRaisesRegex(RuntimeError, 'is larger than expected; try again later'):
            neko_common.fetch(self.base + '/big', 'Downloading Immich v1.2.3\'s compose file', 1000, 10)


if __name__ == '__main__':
    unittest.main()
