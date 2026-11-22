"""Gate runs B7 and J10: with every app off, the home-addresses step failed ("No app for
the gateway") and setup ended "almost ready". With no app it has nothing to do: done."""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools  # noqa: E402
from neko_common import SHARED  # noqa: E402

NONE = {
    'media_apps': {'photos': False, 'movies': False, 'music': False},
    'nextcloud': {'enabled': False},
    'gateway': {'services': list(gateway_tools.DEFAULT_SERVICES)},  # the template's list
}


class NoAppsTests(unittest.TestCase):
    def test_no_app_means_nothing_to_do_not_a_failure(self):
        self.assertTrue(gateway_tools.no_apps(NONE))
        for name in ('modules.gateway_lan', 'modules.gateway_tailnet'):
            step = importlib.import_module(name)
            said = []
            ctx = types.SimpleNamespace(config=NONE, apply=True, log=said.append)
            with mock.patch.object(gateway_tools, 'configure', side_effect=AssertionError('configured')):
                self.assertIsNone(step.run(ctx))
            self.assertIn('no app is set up', said[0])

    def test_one_app_or_a_list_of_ones_own_is_not_skipped(self):
        self.assertFalse(gateway_tools.no_apps({**NONE, 'nextcloud': {'enabled': True}}))
        self.assertFalse(gateway_tools.no_apps({**NONE, 'gateway': {'services': ['jellyfin']}}))


class GatewayRestartTests(unittest.TestCase):
    def test_a_refused_start_check_stops_the_restarts(self):
        """The deep check: with its start check refusing (the home address gone, say), the
        gateway was started again every 10 s for ever, under systemd's default limit of 5
        starts in 10 s. Its own limit now ends the tries (seen under systemd 255: failed
        after 6 tries, where the old unit was still retrying after 20)."""
        text = gateway_tools.unit({'data_mount': '/srv/n', 'media_mount': '/mnt/m'}, Path('/opt/x'), Path('/s'))
        fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        seconds = {'s': 1, 'min': 60}

        def length(value):
            number = value.rstrip('mins')
            return int(number) * seconds[value[len(number) :]]

        self.assertEqual(fields['Restart'], 'on-failure')  # a crash is still started again
        tries = int(fields['StartLimitBurst'])
        self.assertLess(tries * length(fields['RestartSec']), length(fields['StartLimitIntervalSec']))
        self.assertLessEqual(tries, 10)
        self.assertLess(text.index('StartLimitBurst='), text.index('[Service]'))  # read only in [Unit]

    def test_the_start_check_is_given_the_time_it_waits_for_the_home_address(self):
        """Deep check 876: after a power cut the PC was up before the router gave it its
        home address. The start check refused at once, the six tries were used up in about
        a minute and the home addresses stayed down until someone started the gateway. It
        now waits for the address (startup.rs wait_for_lan): systemd must let it, and the
        tries must still end with the address gone for good."""
        text = gateway_tools.unit({'data_mount': '/srv/n', 'media_mount': '/mnt/m'}, Path('/opt/x'), Path('/s'))
        fields = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        wait = SHARED['gateway_start']['address_wait_seconds']
        self.assertGreaterEqual(wait, 120)  # a home router's start, after the PC's
        self.assertGreater(int(fields['TimeoutStartSec'].rstrip('s')), wait)  # systemd's default is 90 s
        self.assertGreater(text.index('TimeoutStartSec='), text.index('[Service]'))
        tries = int(fields['StartLimitBurst'])
        each = wait + int(fields['RestartSec'].rstrip('s'))
        self.assertLess(tries * each, int(fields['StartLimitIntervalSec'].rstrip('s')))


if __name__ == '__main__':
    unittest.main()
