"""Backup cards (0.52.0), the setup side: the switch on setup's first page, what it tells
the runtime's jobs and the dashboard, and that a server set up before is left exactly as
it was until the switch is turned on."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import explanations  # noqa: E402
import native_runtime  # noqa: E402
import neko_common  # noqa: E402
from modules import dashboard  # noqa: E402

SSD_UUID = '1a2b3c4d-0001-4000-8000-00000000aaaa'
HDD_UUID = '1a2b3c4d-0002-4000-8000-00000000bbbb'


def settings_0513():
    """A settings file as 0.51.3 left it: the shipped one with drives chosen, Immich and
    Nextcloud on, and no word about backup cards."""
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg['storage'] = {
        'data_ssd': {'uuid': SSD_UUID, 'fstype': 'ext4'},
        'media_hdd': {'uuid': HDD_UUID, 'fstype': 'ext4'},
    }
    cfg['photos']['deploy_immich'] = True
    cfg['nextcloud']['enabled'] = True
    cfg.pop('cards', None)
    return cfg


def switched_on(cfg):
    cfg = json.loads(json.dumps(cfg))
    explanations.set_extra(cfg, explanations.EXTRA_BY_ID['backup_cards'], True)
    return cfg


class SwitchTests(unittest.TestCase):
    def test_the_switch_is_on_the_first_page_under_storage_and_off_until_chosen(self):
        extra = explanations.EXTRA_BY_ID['backup_cards']
        self.assertEqual(dict(explanations.PARTS)[extra['part']], 'Storage')
        self.assertTrue(extra['explanation'].startswith('Copy your photos and documents onto SD cards or USB sticks'))
        for exists in (True, False):  # a server set up before, and a new one
            with mock.patch.object(neko_common, 'server_exists', return_value=exists):
                self.assertIs(explanations.states({})['backup_cards'], False)
                self.assertIs(neko_common.cards_enabled({}), False)
        cfg = switched_on({})
        self.assertEqual(cfg, {'cards': {'enabled': True}})
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            self.assertIs(neko_common.cards_enabled(cfg), True)
            self.assertIn('Backup cards', [line for _, lines in explanations.review(cfg) for line in lines])

    def test_a_saved_switch_is_a_setting_setup_knows(self):
        from config_checks import unknown_settings

        template = json.loads((ROOT / 'config.json').read_text())
        self.assertEqual(unknown_settings(switched_on(settings_0513()), template), [])


class UpgradeTests(unittest.TestCase):
    """Settings from 0.51.3: nothing NekoServer writes for its jobs or the dashboard
    changes until the switch is turned on."""

    def written(self, cfg):
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            guard = native_runtime.storage_config(cfg)
            collector = dashboard.collector_settings(cfg, 1800)
        return guard, collector

    def test_settings_from_0513_write_the_same_files_as_before(self):
        guard, collector = self.written(settings_0513())
        self.assertNotIn('cards', guard)
        self.assertNotIn('cards', collector)
        off = settings_0513()
        off['cards'] = {'enabled': False}
        self.assertEqual(self.written(off), (guard, collector))

    def test_switched_on_the_jobs_and_the_dashboard_are_told_what_to_copy(self):
        guard, collector = self.written(switched_on(settings_0513()))
        self.assertEqual(guard['cards'], {'enabled': True, 'immich': True, 'nextcloud': True})
        self.assertEqual(collector['cards'], guard['cards'])
        without_nextcloud = switched_on(settings_0513())
        without_nextcloud['nextcloud']['enabled'] = False
        self.assertEqual(self.written(without_nextcloud)[0]['cards']['nextcloud'], False)


if __name__ == '__main__':
    unittest.main()
