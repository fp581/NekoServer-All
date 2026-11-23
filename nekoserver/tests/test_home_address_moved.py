"""The router gave the server another home-network address (after a restart, a new router).

The home gateway's start check waits for the saved address, which never comes back. Two
things must still work then (sweep bug 1 of 0.50.0, seen again on 0.52.3):
- the dashboard says so: its collector lists this PC's addresses with `ip`, which needs a
  netlink socket; without one the check said "unknown" and no alert was shown;
- setup run again moves the gateway to the new address at once: `systemctl enable --now`
  joined the start that was still waiting for the old address and timed out after 30 s
  ("Gateway command took over 30 seconds"), though the gateway came up minutes later.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gateway_tools  # noqa: E402
from modules import dashboard  # noqa: E402


class HomeAddressMovedTests(unittest.TestCase):
    def test_the_collector_may_list_this_pcs_addresses(self):
        units = dashboard.units(Path('/srv/n'), Path('/opt/neko'), Path('/etc/neko/config.json'), 8787, 60)
        collector = units['nekoserver-dashboard-collect.service']
        line = next(row for row in collector.splitlines() if row.startswith('RestrictAddressFamilies='))
        self.assertIn('AF_NETLINK', line.split('=', 1)[1].split())
        self.assertIn('AF_UNIX', line.split('=', 1)[1].split())

    def test_the_collector_may_read_addresses_but_not_change_the_network(self):
        # AF_NETLINK alone would let this root unit add or change links; without
        # CAP_NET_ADMIN, `ip -j address show up` still reads and every change is refused.
        units = dashboard.units(Path('/srv/n'), Path('/opt/neko'), Path('/etc/neko/config.json'), 8787, 60)
        rows = units['nekoserver-dashboard-collect.service'].splitlines()
        self.assertIn('CapabilityBoundingSet=~CAP_NET_ADMIN', rows)
        self.assertTrue(any(row.startswith('RestrictAddressFamilies=') and 'AF_NETLINK' in row for row in rows))

    def test_setup_restarts_the_gateway_instead_of_joining_a_start_that_still_waits(self):
        ran = []
        with mock.patch.object(gateway_tools, 'command', side_effect=lambda args, **_: ran.append(list(args)) or ''):
            gateway_tools.start_lan_gateway()
        self.assertNotIn('--now', [word for args in ran for word in args])
        self.assertIn(['systemctl', 'enable', gateway_tools.GATEWAY_LAN_UNIT], ran)
        self.assertEqual(ran[-1], ['systemctl', 'restart', gateway_tools.GATEWAY_LAN_UNIT])


if __name__ == '__main__':
    unittest.main()
