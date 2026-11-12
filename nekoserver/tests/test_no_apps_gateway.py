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


if __name__ == '__main__':
    unittest.main()
