"""0.50.0, the "Protect your data" page's facts (the wizard specification's P05, T20, T21).

- A new server keeps 3 nightly app backups and 3 nightly settings backups; setup accepts
  only 1 to 7 (it was 7, and 0 to 3650 with 0 for "keep every one").
- A count from before 0.50.0 outside 1 to 7 is kept as it is, marked as the earlier
  setting, until the person approves a change. Setup never removes a backup.
- The table of what is protected, where, when, how many are kept and how far back that
  reaches, read from the placement table and the real schedule.
- The review text of a change, with how many backups it would remove and when.
- The count never applies to the photo archive, the films and music or the offline copy.
  (That the runtime cannot reach them with it is proved by tests/test_spec_t21_retention.py
  of the extras branch; here: the page's table never offers the count for them.)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_plan  # noqa: E402
import installer  # noqa: E402
from modules import app_backup, host_backup  # noqa: E402

COUNTED = ('app_backup', 'host_backup')


def template():
    return json.loads((ROOT / 'config.json').read_text())


def older(app=14, host=14):
    """The settings of a server set up before 0.50.0 (no mark that its counts were reviewed)."""
    config = template()
    del config[backup_plan.REVIEWED]
    config['app_backup']['keep_snapshots'] = app
    config['host_backup']['keep_snapshots'] = host
    return config


class CountTests(unittest.TestCase):
    def test_a_new_server_keeps_three_of_each(self):
        config = template()
        self.assertEqual([config[group]['keep_snapshots'] for group in COUNTED], [3, 3])
        self.assertIs(config[backup_plan.REVIEWED], True)
        self.assertEqual([backup_plan.count(config, group) for group in COUNTED], [(3, False), (3, False)])
        # Also when the settings name no count at all.
        self.assertEqual(app_backup.keep_snapshots({}), 3)
        self.assertEqual((backup_plan.DEFAULT, backup_plan.LEAST, backup_plan.MOST), (3, 1, 7))
        # The weekly Immich database copies stay 4 and the monthly snapshots 3.
        self.assertEqual((config['photos']['database_keep'], config['maintenance']['keep_snapshots']), (4, 3))

    def test_setup_takes_one_to_seven_and_refuses_the_rest_in_plain_words(self):
        for group in COUNTED:
            for keep in range(1, 8):
                config = template()
                config[group]['keep_snapshots'] = keep
                self.assertEqual(backup_plan.count(config, group), (keep, False))
            for keep in (0, 8, 14, 3650, -1, 3.0, '3', True, None):
                config = template()
                config[group]['keep_snapshots'] = keep
                with self.subTest(group=group, keep=keep), self.assertRaises(RuntimeError) as refused:
                    backup_plan.count(config, group)
                said = str(refused.exception)
                self.assertIn(f'"keep_snapshots" under "{group}" in my-config.json is {keep!r}.', said)
                self.assertIn('write a whole number from 1 to 7 there (a new server keeps 3)', said)

    def test_the_jobs_own_settings_refuse_it_too(self):
        config = template()
        config['app_backup']['keep_snapshots'] = 8
        with self.assertRaisesRegex(RuntimeError, 'NekoServer keeps from 1 to 7 nightly app backups'):
            app_backup.keep_snapshots(config)
        config = template()
        config['host_backup']['keep_snapshots'] = 0
        with self.assertRaisesRegex(RuntimeError, 'NekoServer keeps from 1 to 7 nightly settings backups'):
            host_backup.settings(config)

    def test_the_scheduled_job_is_told_when_a_count_is_the_earlier_one(self):
        config = older(app=0)
        backup_plan.note_earlier(config)
        self.assertEqual(
            app_backup.runtime_count(config, 'app_backup', app_backup.APP_BACKUPS),
            {'keep_snapshots': 0, 'earlier': True},
        )
        self.assertEqual(
            app_backup.runtime_count(template(), 'app_backup', app_backup.APP_BACKUPS), {'keep_snapshots': 3}
        )


class UpgradeTests(unittest.TestCase):
    """T20: an earlier count needs the person's own yes; nothing is pruned at launch."""

    def test_an_earlier_count_is_kept_and_marked_never_changed(self):
        for app, host in ((14, 7), (0, 30), (8, 3650)):
            config = older(app, host)
            self.assertTrue(backup_plan.note_earlier(config))
            self.assertEqual(
                (config['app_backup']['keep_snapshots'], config['host_backup']['keep_snapshots']), (app, host)
            )
            self.assertEqual(backup_plan.count(config, 'app_backup'), (app, True))
            self.assertEqual(backup_plan.count(config, 'host_backup'), (host, host > 7))
            self.assertEqual(backup_plan.earlier_groups(config), ['app_backup'] + ['host_backup'] * (host > 7))
            self.assertFalse(backup_plan.note_earlier(config))  # once

    def test_a_count_from_one_to_seven_is_preserved_without_a_question(self):
        config = older(7, 5)
        lines, changed = installer.review_backup_counts(config, ask=lambda question: self.fail('asked'))
        self.assertEqual((lines, changed), ([], True))
        self.assertEqual((config['app_backup']['keep_snapshots'], config['host_backup']['keep_snapshots']), (7, 5))
        self.assertIs(config[backup_plan.REVIEWED], True)

    def test_with_nobody_at_the_keyboard_it_stays_run_after_run(self):
        config = older(14, 0)
        for run in range(2):
            lines, changed = installer.review_backup_counts(config)
            self.assertEqual(changed, run == 0)  # the marks are saved once
            self.assertEqual((config['app_backup']['keep_snapshots'], config['host_backup']['keep_snapshots']), (14, 0))
            self.assertNotIn(backup_plan.REVIEWED, config)  # still to be asked
            self.assertIn('Backups: the newest 14 nightly app backups stay kept, as before', lines[0])
            self.assertIn('every one of the nightly settings backups (none is ever removed) stay kept', lines[1])
            self.assertIn('nothing was removed', lines[0])
        # The settings both jobs run with are valid meanwhile, and say the count is the earlier one.
        self.assertEqual(app_backup.keep_snapshots(config), 14)
        self.assertEqual(host_backup.settings(config)['keep_snapshots'], 0)

    def test_the_person_is_asked_once_and_only_a_yes_changes_the_count(self):
        config = older(14, 30)
        asked = []
        answers = iter([True, False])

        def ask(question):
            asked.append(question)
            return next(answers)

        lines, changed = installer.review_backup_counts(config, ask, held={'app_backup': 14, 'host_backup': 9}.get)
        self.assertTrue(changed)
        self.assertEqual((config['app_backup']['keep_snapshots'], config['host_backup']['keep_snapshots']), (3, 30))
        self.assertNotIn('keep_snapshots_earlier', config['app_backup'])
        self.assertIs(config['host_backup']['keep_snapshots_earlier'], True)
        self.assertIs(config[backup_plan.REVIEWED], True)
        self.assertEqual(
            asked[0],
            'This server keeps the newest 14 nightly app backups. New servers keep 3, and the most that can be '
            'chosen from now on is 7. There are 14 now. If you change it to 3, the 11 oldest are removed, but only '
            'after the next backup that succeeds. Nothing is removed now, a backup that fails removes nothing, and '
            'the newest backup always stays. Your photos, videos, films and music are not touched by this number.'
            '\nKeep the newest 3 nightly app backups from now on?',
        )
        self.assertIn('are removed after the next backup that succeeds, none before', lines[0])
        self.assertIn('the newest 30 nightly settings backups stay kept, as before', lines[1])
        # Not asked again: the 30 that was kept goes on working, and the page shows it.
        again = installer.review_backup_counts(config, lambda question: self.fail('asked again'))
        self.assertEqual(again, ([], False))
        self.assertEqual(backup_plan.count(config, 'host_backup'), (30, True))
        self.assertIsNotNone(backup_plan.count_field(config, 'host_backup')['review'])

    def test_after_the_review_a_count_outside_one_to_seven_is_a_mistake(self):
        config = older(7, 7)
        installer.review_backup_counts(config, ask=None)
        config['app_backup']['keep_snapshots'] = 30  # typed into my-config.json afterwards
        self.assertFalse(backup_plan.note_earlier(config))
        with self.assertRaisesRegex(RuntimeError, 'is 30. NekoServer keeps from 1 to 7'):
            app_backup.keep_snapshots(config)

    def test_setup_never_removes_a_backup(self):
        """Nothing in setup's side of the counts deletes: a count is only written down, and
        the scheduled job applies it after its own next backup."""
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder)
            config = older(14, 14)
            config['media_mount'] = str(media)
            for group, (_what, where) in backup_plan.COUNTED.items():
                for number in range(1, 15):
                    made = media / where / f'snapshot-{number}000-1'
                    made.mkdir(parents=True)
                    (made / 'manifest.json').write_text('{}')
                (media / where / 'snapshot-99000-1').mkdir()  # not finished: not counted
            self.assertEqual([backup_plan.held(config, group) for group in COUNTED], [14, 14])
            before = sorted(str(path) for path in media.rglob('*'))
            installer.review_backup_counts(config, lambda question: True, lambda group: backup_plan.held(config, group))
            backup_plan.set_count(config, 'host_backup', 1)
            backup_plan.table(config)
            self.assertEqual((config['app_backup']['keep_snapshots'], config['host_backup']['keep_snapshots']), (3, 1))
            self.assertEqual(sorted(str(path) for path in media.rglob('*')), before)
        # A drive that is not there: how many there are is not known, and nothing fails.
        self.assertIsNone(backup_plan.held(config, 'app_backup'))


class ReviewTests(unittest.TestCase):
    def test_the_review_says_how_many_go_and_when(self):
        config = older(14)
        backup_plan.note_earlier(config)
        said = backup_plan.review(config, 'app_backup', 3, held=5)
        self.assertIn('This server keeps the newest 14 nightly app backups. New servers keep 3', said)
        self.assertIn('There are 5 now. If you change it to 3, the 2 oldest are removed, but only after', said)
        self.assertIn('the 1 oldest is removed', backup_plan.review(config, 'app_backup', 3, held=4))
        # Fewer there than the new count: nothing would go, and it says so.
        self.assertIn('none of them is removed', backup_plan.review(config, 'app_backup', 3, held=2))
        # Not known how many there are (the drive is away): no number is made up.
        unknown = backup_plan.review(config, 'app_backup', 3)
        self.assertIn('the ones older than the newest 3 are removed, but only after', unknown)
        self.assertNotIn('now.', unknown.split('If you')[0])

    def test_keep_every_one_is_said_as_that(self):
        config = older(app=0)
        backup_plan.note_earlier(config)
        said = backup_plan.review(config, 'app_backup', 3, held=40)
        self.assertIn('This server keeps every one of the nightly app backups (none is ever removed).', said)
        self.assertIn('the 37 oldest are removed', said)

    def test_a_change_inside_one_to_seven_is_reviewed_the_same_way(self):
        config = template()
        self.assertIsNone(backup_plan.review(config, 'app_backup', 3))
        more = backup_plan.review(config, 'app_backup', 7, held=3)
        self.assertEqual(
            more,
            'This server keeps the newest 3 nightly app backups. There are 3 now. With 7, more are kept '
            'from now on. Nothing is removed.',
        )
        config['host_backup']['keep_snapshots'] = 7
        fewer = backup_plan.review(config, 'host_backup', 2, held=7)
        self.assertIn('the 5 oldest are removed, but only after the next backup that succeeds', fewer)
        self.assertNotIn('New servers keep', fewer)  # said only for an earlier count

    def test_only_a_whole_number_from_one_to_seven_can_be_approved(self):
        config = older(14)
        backup_plan.note_earlier(config)
        for wrong in (0, 8, 30, '3', 2.0, None, True):
            with self.subTest(count=wrong), self.assertRaisesRegex(RuntimeError, 'choose a whole number from 1 to 7'):
                backup_plan.set_count(config, 'app_backup', wrong)
        self.assertEqual(backup_plan.count(config, 'app_backup'), (14, True))  # untouched by a refused one
        with self.assertRaisesRegex(RuntimeError, 'photos has no count of backups to keep'):
            backup_plan.set_count(config, 'photos', 3)
        backup_plan.set_count(config, 'app_backup', 5)
        self.assertEqual(backup_plan.count(config, 'app_backup'), (5, False))


class TableTests(unittest.TestCase):
    def rows(self, config):
        return {row['key']: row for row in backup_plan.table(config)}

    def test_the_table_of_a_new_server(self):
        rows = self.rows(template())
        self.assertEqual(
            list(rows), ['app_backup', 'host_backup', 'photo_archive', 'photo_database', 'monthly', 'offline']
        )
        app = rows['app_backup']
        self.assertEqual(
            (app['what'], app['drive'], app['folder'], app['when'], app['kept']),
            (
                'The settings and databases of the film, show and music apps',
                'media',
                'backups/media-applications',
                'every night at 04:15',
                'the newest 3 nightly app backups',
            ),
        )
        self.assertEqual((app['covers'], app['covers_days']), ('about the last 3 nights, when every backup runs', 3))
        self.assertEqual(rows['host_backup']['when'], 'every night at 05:15')
        self.assertEqual(rows['photo_archive']['when'], 'every week, Sunday at 03:00')
        self.assertEqual(rows['photo_database']['when'], 'every week, Sunday at 02:15')
        self.assertEqual(
            (rows['photo_database']['kept'], rows['photo_database']['covers_days']), ('the newest 4 weekly copies', 28)
        )
        self.assertEqual(
            (rows['monthly']['when'], rows['monthly']['covers_days']), ('every month, on the 1st at 02:30', 90)
        )
        # No removable drive chosen: said as missing, not as protected.
        offline = rows['offline']
        self.assertEqual(
            (offline['drive'], offline['when'], offline['kept']), (None, 'never: no such drive is set up', 'nothing')
        )
        self.assertIn('Not set up.', offline['note'])

    def test_the_times_are_the_real_schedule(self):
        config = template()
        config['backup_times'] = {'daily': '01:00', 'weekly_day': 'Wed', 'weekly': '23:30', 'monthly': '05:00'}
        rows = self.rows(config)
        self.assertEqual(rows['app_backup']['when'], 'every night at 01:00')
        self.assertEqual(rows['host_backup']['when'], 'every night at 02:00')
        self.assertEqual(rows['photo_archive']['when'], 'every week, Wednesday at 23:30')
        self.assertEqual(rows['photo_database']['when'], 'every week, Wednesday at 22:45')
        self.assertEqual(rows['monthly']['when'], 'every month, on the 1st at 05:00')

    def test_three_backups_are_three_backups_not_three_days(self):
        config = template()
        config['app_backup']['keep_snapshots'] = 1
        row = self.rows(config)['app_backup']
        self.assertEqual((row['covers'], row['covers_days']), ('about the last 1 night, when every backup runs', 1))
        config = older(app=0)
        backup_plan.note_earlier(config)
        row = self.rows(config)['app_backup']
        self.assertEqual((row['covers'], row['covers_days']), ('as far back as the first backup', None))

    def test_the_count_field_is_only_on_the_two_counted_backups(self):
        """T21: the 1-to-7 count is never offered for the photo archive, the database
        copies, the monthly snapshot or the offline copy, and nothing there is removed for it."""
        config = template()
        config['photos']['external_backup_uuid'] = 'usb-0001'
        rows = self.rows(config)
        self.assertEqual([key for key, row in rows.items() if row['group']], list(COUNTED))
        for key in ('photo_archive', 'offline'):
            self.assertIsNone(rows[key]['field'])
            self.assertEqual(
                rows[key]['kept'], 'everything, in every version; nothing is ever removed to keep to a number'
            )
            self.assertIsNone(rows[key]['covers_days'])
        for key in ('photo_database', 'monthly'):
            self.assertIsNone(rows[key]['field'])
            self.assertIn('the 1-to-7 number does not apply to it', rows[key]['note'])
        # Lowering the counted ones to 1 changes no other row.
        others = {key: row for key, row in rows.items() if key not in COUNTED}
        for group in COUNTED:
            backup_plan.set_count(config, group, 1)
        self.assertEqual({key: row for key, row in self.rows(config).items() if key not in COUNTED}, others)
        self.assertEqual((config['photos']['database_keep'], config['maintenance']['keep_snapshots']), (4, 3))
        field = rows['app_backup']['field']
        self.assertEqual(
            (field['value'], field['least'], field['most'], field['default'], field['earlier'], field['review']),
            (3, 1, 7, 3, False, None),
        )

    def test_an_earlier_count_shows_its_review_inline(self):
        config = older(app=30)
        backup_plan.note_earlier(config)
        row = {row['key']: row for row in backup_plan.table(config, held={'app_backup': 30})}['app_backup']
        self.assertEqual((row['field']['value'], row['field']['earlier']), (30, True))
        self.assertIn('There are 30 now. If you change it to 3, the 27 oldest are removed', row['field']['review'])
        self.assertEqual(row['kept'], 'the newest 30 nightly app backups')

    def test_the_drives_come_from_the_placement_table(self):
        config = template()
        config['photos']['external_backup_uuid'] = 'usb-0001'
        rows = self.rows(config)
        self.assertEqual((rows['offline']['drive'], rows['offline']['when']), ('usb', 'every week, Sunday at 03:00'))
        self.assertIsNone(rows['offline']['note'])
        self.assertIn('your photos and videos', rows['offline']['what'])
        # The backups go to the media drive; the apps are on the SSD: not the same drive.
        self.assertFalse(rows['app_backup']['same_drive'])
        self.assertFalse(rows['photo_archive']['same_drive'])
        # Photos kept on the media drive (an earlier "locations" choice): the archive there is
        # on the drive that holds them, and the table says so.
        config['locations'] = {'photos': 'media'}
        self.assertTrue(self.rows(config)['photo_archive']['same_drive'])

    def test_a_server_without_some_apps_has_no_rows_for_them(self):
        config = template()
        config['media_apps'] = {'photos': False, 'movies': True, 'music': True}
        self.assertEqual(list(self.rows(config)), ['app_backup', 'host_backup', 'monthly', 'offline'])


if __name__ == '__main__':
    unittest.main()
