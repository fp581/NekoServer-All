"""1.0 round T1-R16: the server's Tailscale name changed (renamed in the admin console, or
added again and given a "-1" name) while its Serve routes were still stored under the old
name. Setup's "Access from anywhere" then failed on every run with "Gateway command failed
(tailscale serve --bg): try `tailscale serve --help`": `tailscale serve --bg --https=8443
off` looks under the current name only, and answers "failed to remove web serve: handler
does not exist" (tailscale 1.104.7). A route that is already not there under the current
name is not a failure, and a failure says tailscale's own reason."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_checks  # noqa: E402
import gateway_tools as g  # noqa: E402
import test_tailnet_ownership as own  # noqa: E402

NO_HANDLER = 'error: failed to remove web serve: handler does not exist\n\ntry `tailscale serve --help` for usage info\n'


class Tailscale_1_104:
    """tailscale serve as 1.104.7 does it: routes are kept under the name they were set
    with, and "off" works on the current name's route only (and drops the port's TCP
    entry with it)."""

    def __init__(self, host):
        self.host, self.state, self.calls = host, {'TCP': {}, 'Web': {}}, []

    def serve(self, args, timeout=30, env=None):
        self.calls.append(args)
        port = args[3].split('=')[1]
        key = self.host + ':' + port
        if args[4] == 'off':
            if key not in self.state['Web']:
                raise g.CommandFailed(
                    'Gateway command failed (tailscale serve --bg): try `tailscale serve --help` for usage info',
                    1,
                    '',
                    NO_HANDLER,
                )
            del self.state['Web'][key]
            self.state['TCP'].pop(port, None)
        else:
            self.state['TCP'][port] = {'HTTPS': True}
            self.state['Web'][key] = {'Handlers': {'/': {'Proxy': args[4]}}}
        return ''

    def current(self):
        return json.loads(json.dumps(self.state))


class RenamedServerTests(unittest.TestCase):
    def setUp(self):
        own.OwnershipTests.setUp(self)
        self.ts = Tailscale_1_104('neko.tail1.ts.net')

    apply = own.OwnershipTests.apply
    ledger = own.OwnershipTests.ledger

    def renamed(self):
        self.apply()
        self.ts.host = 'neko-1.tail1.ts.net'  # renamed, or added again
        self.ts.calls.clear()

    def test_setup_goes_on_and_sets_the_routes_under_the_current_name(self):
        self.renamed()
        self.apply()
        web = self.ts.state['Web']
        self.assertIn('neko-1.tail1.ts.net:8443', web)
        self.assertEqual(web['neko-1.tail1.ts.net:8448']['Handlers']['/']['Proxy'], self.board)
        self.assertEqual(self.ledger()['host'], 'neko-1.tail1.ts.net')
        self.assertEqual(self.ledger()['status'], 'configured')

    def test_the_next_run_changes_nothing_and_the_check_passes(self):
        self.renamed()
        self.apply()
        self.ts.calls.clear()
        self.apply()
        self.assertEqual(self.ts.calls, [])
        row = lambda name, state, why: state  # noqa: E731
        with (
            mock.patch.object(g, 'tailnode', side_effect=lambda: self.ts.host),
            mock.patch.object(g, 'serve_state', side_effect=self.ts.current),
            mock.patch.object(g, 'installed', return_value=True),
            mock.patch.object(g, 'dashboard_target', side_effect=lambda config: self.board),
            mock.patch.object(g, 'https_problem', return_value=None),
        ):
            self.assertEqual(gateway_checks.tailnet_row(self.config, row), 'pass')

    def test_an_app_taken_out_after_the_rename_loses_its_route(self):
        self.config['gateway']['services'] = self.config['gateway']['confirmed_services'] = ['jellyfin', 'immich']
        self.renamed()
        self.config['gateway']['services'] = self.config['gateway']['confirmed_services'] = ['jellyfin']
        self.apply()
        self.assertNotIn('neko-1.tail1.ts.net:8445', self.ts.state['Web'])
        self.assertEqual(sorted(self.ledger()['owned']['TCP']), ['8443', '8448'])


class TailscaleOwnReasonTests(unittest.TestCase):
    def test_a_failed_tailscale_command_says_tailscales_reason_not_try_help(self):
        failed = subprocess.CompletedProcess([], 1, '', NO_HANDLER)
        with mock.patch.object(g.subprocess, 'run', return_value=failed):
            with self.assertRaises(g.CommandFailed) as caught:
                g.command(['tailscale', 'serve', '--bg', '--https=8443', 'off'])
        self.assertIn('failed to remove web serve: handler does not exist', str(caught.exception))
        self.assertNotIn('--help', str(caught.exception))

    def test_a_route_that_cannot_be_switched_off_for_another_reason_still_stops_setup(self):
        failed = g.CommandFailed('Gateway command failed (tailscale serve --bg): access denied', 1, '', 'access denied')
        with mock.patch.object(g, 'command', side_effect=failed), self.assertRaises(g.CommandFailed):
            g.route_off('8443')


if __name__ == '__main__':
    unittest.main()
