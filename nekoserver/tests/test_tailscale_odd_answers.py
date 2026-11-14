"""Odd `tailscale status --json` answers (Tailscale still starting, signed out, a field of
another type) are "not ready yet" to every reader, never a Python error that stops setup
or the phone page.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway_tools  # noqa: E402
import phone_status  # noqa: E402
from modules import dashboard  # noqa: E402

NAME = 'neko.tail1234.ts.net'
RUNNING = {
    'BackendState': 'Running',
    'Self': {'DNSName': NAME + '.', 'TailscaleIPs': ['100.64.0.1']},
    'CurrentTailnet': {'MagicDNSEnabled': True},
    'CertDomains': [NAME],
}


def answering(text):
    """A stand-in for gateway_tools.command that gives `text` for `tailscale status --json`."""
    return mock.patch.object(gateway_tools, 'command', return_value=text)


class OddTailscaleAnswers(unittest.TestCase):
    def test_starting_tailscale_keeps_the_dashboard_name_in_use(self):
        # Tailscale writes "Self": null while it starts; the dashboard step keeps the name
        # it has (it catches RuntimeError), instead of failing with an AttributeError.
        for text in ('{"BackendState": "NoState", "Self": null}', '[1, 2]', 'not json'):
            with self.subTest(text=text), answering(text):
                self.assertEqual(dashboard.tailnet_name({}, known='kept.tail1.ts.net'), 'kept.tail1.ts.net')

    def test_tailnode_says_not_ready(self):
        for text in (
            '{"BackendState": "Running", "Self": null}',
            '{"BackendState": "Running", "Self": {"DNSName": null}}',
        ):
            with self.subTest(text=text), answering(text):
                with self.assertRaisesRegex(RuntimeError, 'Authenticate Tailscale'):
                    gateway_tools.tailnode()

    def test_addresses_of_another_type_are_no_address(self):
        for ips in ('100.64.0.1', 5, None):
            with self.subTest(ips=ips):
                self.assertIsNone(gateway_tools.tailscale_address({'Self': {'TailscaleIPs': ips}}))
                self.assertIsNone(gateway_tools.tailscale_address({'Self': [ips]}))

    def test_phone_page_survives_addresses_of_another_type(self):
        status = {**RUNNING, 'Self': {**RUNNING['Self'], 'TailscaleIPs': 5}}
        routes = {'TCP': {'8443': {'HTTPS': True}}, 'Web': {}}
        answers = {('tailscale', 'status', '--json'): status, ('tailscale', 'serve', 'status', '--json'): routes}
        with tempfile.TemporaryDirectory() as data:
            config = {'data_mount': data, 'gateway': {'services': ['jellyfin']}}
            with mock.patch.object(gateway_tools, 'installed', return_value=True):
                rows = phone_status.check(
                    config,
                    command=lambda args, timeout=30: json.dumps(answers[tuple(args)]),
                    probe=lambda *a: False,
                    why=lambda: '',
                )['rows']
        self.assertEqual(rows[0]['status'], phone_status.PASS)


if __name__ == '__main__':
    unittest.main()
