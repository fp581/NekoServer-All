"""0.50.0, the wizard (P05): the backups page asks, with the times, how many nightly
backups are kept: a number from 1 to 7 for each kind (3 on a new server). The numbers,
their limits and the upgrade of an earlier count are backup_plan's
(tests/test_backup_plan.py); this is the page's side: what it shows, and that a number
is written only when one is chosen."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_plan  # noqa: E402
import guided_setup  # noqa: E402
import setup_progress  # noqa: E402


def context(config):
    saved = []
    ctx = SimpleNamespace(config=config, log=lambda line: None, saved=saved)
    ctx.persist_config = lambda: saved.append(json.loads(json.dumps(ctx.config)))
    return ctx


class BackupsPageTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(guided_setup, 'save_settings', lambda ctx: ctx.persist_config())
        patch.start()
        self.addCleanup(patch.stop)
        self.template = json.loads((ROOT / 'config.json').read_text())

    def test_a_new_server_is_offered_three_of_each(self):
        counts = guided_setup.backup_counts(self.template)
        self.assertEqual([field['group'] for field in counts], ['app_backup', 'host_backup'])
        self.assertEqual([guided_setup.count_offered(field) for field in counts], ['3', '3'])
        self.assertEqual({(field['least'], field['most']) for field in counts}, {(1, 7)})
        # The table: what is backed up, when, how many are kept.
        rows = guided_setup.backup_rows(self.template)
        self.assertIn('app_backup', [row['key'] for row in rows])
        self.assertEqual(set(rows[0]), {'key', 'what', 'drive', 'when', 'kept', 'covers', 'same_drive', 'note'})

    def test_enter_changes_nothing_and_a_number_is_written_once(self):
        ctx = context(self.template)
        counts = guided_setup.backup_counts(ctx.config)
        said = []
        with setup_progress.page('backup_times', {}):
            chosen = guided_setup.ask_backup_counts(ctx, counts, lambda question='': '', said.append)
        self.assertEqual(chosen, {'app_backup': '3', 'host_backup': '3'})
        guided_setup.keep_counts(ctx, chosen, said.append)
        self.assertEqual(ctx.saved, [])
        # 0 and 8 are refused in words; 5 is taken for the app backups alone.
        typed = iter(['0', '5', ''])
        with setup_progress.page('backup_times', {}):
            chosen = guided_setup.ask_backup_counts(ctx, counts, lambda question='': next(typed), said.append)
        self.assertIn('Type a whole number from 1 to 7.', said)
        guided_setup.keep_counts(ctx, chosen, said.append)
        self.assertEqual(
            (backup_plan.count(ctx.config, 'app_backup')[0], backup_plan.count(ctx.config, 'host_backup')[0]), (5, 3)
        )
        self.assertEqual(len(ctx.saved), 1)
        self.assertTrue(any('nothing is removed before the next backup succeeds' in line for line in said))

    def test_an_earlier_count_stays_until_a_number_is_chosen(self):
        config = {**self.template, 'app_backup': {'keep_snapshots': 14}}
        config.pop(backup_plan.REVIEWED, None)
        backup_plan.note_earlier(config)
        ctx = context(config)
        field = next(f for f in guided_setup.backup_counts(config) if f['group'] == 'app_backup')
        self.assertTrue(field['earlier'] and field['review'])
        self.assertEqual(guided_setup.count_offered(field), '')  # Enter leaves it as it is
        said = []
        with setup_progress.page('backup_times', {}):
            chosen = guided_setup.ask_backup_counts(ctx, [field], lambda question='': '', said.append)
        self.assertIn(field['review'], said)
        guided_setup.keep_counts(ctx, chosen, said.append)
        self.assertEqual((backup_plan.count(config, 'app_backup'), ctx.saved), ((14, True), []))
        # Chosen: written, and no longer an earlier count.
        guided_setup.keep_counts(ctx, {'app_backup': '3'}, said.append)
        self.assertEqual(backup_plan.count(config, 'app_backup'), (3, False))


if __name__ == '__main__':
    unittest.main()
