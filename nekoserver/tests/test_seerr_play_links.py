"""Deep check 563: Seerr on a phone. Once a request has arrived, Seerr shows "Play on
Jellyfin" (and its alerts link there). The link is built from Seerr's Jellyfin "External
URL", and setup left that empty, so it led to the address Seerr itself uses inside Docker,
http://jellyfin:8096, which no phone can open. Each gateway now puts its Jellyfin address
there: the Tailscale one over a home one, never over an address someone typed."""

import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools as g  # noqa: E402
import media_libraries  # noqa: E402
from tests.test_tailnet_ownership import FakeTailscale  # noqa: E402

KEY = 'seerr-key-123'


class FakeSeerr(http.server.ThreadingHTTPServer):
    """Seerr's /api/v1/settings/jellyfin as its source has it: GET returns the Jellyfin
    settings, POST merges the fields sent into them (server/routes/settings/index.ts)."""

    def __init__(self, jellyfin):
        self.jellyfin, self.posts, self.refuse = jellyfin, [], False
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, code, value):
                body = json.dumps(value).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path != '/api/v1/settings/jellyfin' or self.headers.get('X-Api-Key') != KEY:
                    return self.answer(403, {'message': 'You do not have permission'})
                self.answer(200, server.jellyfin)

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/api/v1/settings/jellyfin' or self.headers.get('X-Api-Key') != KEY or server.refuse:
                    return self.answer(500, {'message': 'INVALID_URL'})
                server.posts.append(sent)
                server.jellyfin.update(sent)
                self.answer(200, server.jellyfin)

            def log_message(self, *args):
                pass

        super().__init__(('127.0.0.1', 0), Handler)


class SeerrPlayLinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / 'appdata/seerr').mkdir(parents=True)
        (self.data / 'appdata/seerr/settings.json').write_text(json.dumps({'main': {'apiKey': KEY}}))
        (self.data / 'gateway').mkdir()
        self.config = {
            'data_mount': str(self.data),
            'gateway': {
                'services': ['jellyfin', 'seerr'],
                'lan_ip': '192.168.1.20',
                'lan_cidr': '192.168.1.0/24',
                'lan_ports': {'jellyfin': 8451},
            },
        }
        self.seerr = FakeSeerr({'ip': 'jellyfin', 'port': 8096, 'serverId': 'abc', 'externalHostname': ''})
        threading.Thread(target=self.seerr.serve_forever, daemon=True).start()
        self.addCleanup(self.seerr.server_close)
        self.addCleanup(self.seerr.shutdown)
        for patch in (
            mock.patch.dict(media_libraries.PORTS, {'seerr': self.seerr.server_address[1]}),
            mock.patch.object(g, 'installed', return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def link(self, url, kind):
        return g.seerr_play_links(self.config, url, kind)

    def test_the_tailscale_address_goes_into_seerr(self):
        ts = FakeTailscale('neko.tail1.ts.net')
        with (
            mock.patch.object(g.recovery_tools, 'require_root'),
            mock.patch.object(g.recovery_tools, 'mounts'),
            mock.patch.object(g, 'readiness'),
            mock.patch.object(g, 'configure_cloud_origin'),
            mock.patch.object(g, 'tailnode', side_effect=lambda: ts.host),
            mock.patch.object(g, 'dashboard_target', return_value=None),
            mock.patch.object(g, 'serve_state', side_effect=ts.current),
            mock.patch.object(g, 'command', side_effect=ts.serve),
        ):
            result = g._apply_tailnet(self.config)
        self.assertEqual(result['status'], 'configured')
        self.assertEqual(self.seerr.posts, [{'externalHostname': 'https://neko.tail1.ts.net:8443'}])
        self.assertNotIn('notes', result)

    def test_a_home_address_only_while_there_is_no_tailscale_one(self):
        self.assertIsNone(self.link('https://192.168.1.20:8451', 'lan'))
        self.assertEqual(self.seerr.jellyfin['externalHostname'], 'https://192.168.1.20:8451')
        self.link('https://neko.tail1.ts.net:8443', 'tailnet')
        self.link('https://192.168.1.20:8451', 'lan')  # the home gateway again: Tailscale's stays
        self.assertEqual(self.seerr.jellyfin['externalHostname'], 'https://neko.tail1.ts.net:8443')
        self.link('https://renamed.tail1.ts.net:8443', 'tailnet')  # the server renamed
        self.assertEqual(self.seerr.jellyfin['externalHostname'], 'https://renamed.tail1.ts.net:8443')
        self.assertEqual(len(self.seerr.posts), 3)

    def test_a_home_address_that_changed_is_brought_up_to_date(self):
        self.seerr.jellyfin['externalHostname'] = 'https://192.168.0.7:8443'
        self.link('https://192.168.1.20:8451', 'lan')
        self.assertEqual(self.seerr.jellyfin['externalHostname'], 'https://192.168.1.20:8451')

    def test_an_address_someone_typed_is_kept(self):
        self.seerr.jellyfin['externalHostname'] = 'https://films.example.org'
        self.link('https://neko.tail1.ts.net:8443', 'tailnet')
        self.assertEqual(self.seerr.posts, [])

    def test_seerr_not_set_up_or_not_linked_is_left_alone(self):
        self.seerr.jellyfin = {'ip': '', 'externalHostname': ''}
        self.assertIsNone(self.link('https://neko.tail1.ts.net:8443', 'tailnet'))
        (self.data / 'appdata/seerr/settings.json').unlink()
        self.assertIsNone(self.link('https://neko.tail1.ts.net:8443', 'tailnet'))
        self.assertEqual(self.seerr.posts, [])

    def test_a_refusal_says_what_to_type_in_seerr(self):
        self.seerr.refuse = True
        said = self.link('https://neko.tail1.ts.net:8443', 'tailnet')
        self.assertIn('"External URL"', said)
        self.assertIn('https://neko.tail1.ts.net:8443', said)


if __name__ == '__main__':
    unittest.main()
