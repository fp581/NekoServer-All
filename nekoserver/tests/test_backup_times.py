"""When the backups run (0.48.0): setup asks three times, daily, weekly and monthly, kept
as "backup_times" in the settings; every timer is written from them through one helper
(neko_common.job_times), and a server set up before keeps the times its settings had."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import neko_common  # noqa: E402
from modules import app_backup, host_backup, maintenance, music_backup, photo_backup, photo_preservation, storage  # noqa: E402
from neko_common import job_times, minutes_later  # noqa: E402

BIN = '/usr/local/lib/nekoserver-runtime/v1/nekoserver-runtime'
GUARD = Path('/etc/nekoserver-backups/x.json')


def settings(**times):
    """Settings with the three times (the shipped defaults unless given)."""
    chosen = {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30', **times}
    return {
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
        'timezone': 'UTC',
        'backup_times': chosen,
        'photos': {'backup_schedule': 'weekly'},
    }


def answers(*values):
    given = iter(values)
    return lambda question='': next(given)


class Ctx:
    def __init__(self, config, apply=True):
        self.config, self.apply, self.logs, self.saved = config, apply, [], 0

    def log(self, message):
        self.logs.append(message)

    def persist_config(self):
        self.saved += 1


class JobTimesTests(unittest.TestCase):
    def test_the_shipped_settings_keep_every_job_at_the_time_it_always_had(self):
        shipped = json.loads((ROOT / 'config.json').read_text())
        self.assertEqual(
            shipped['backup_times'], {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30'}
        )
        times = job_times(shipped)
        self.assertEqual(times['app'], '04:15')
        self.assertEqual(times['music'], '03:45')
        self.assertEqual(times['host'], '05:15')
        self.assertEqual((times['usb_day'], times['usb']), ('Sun', '03:00'))
        self.assertEqual(times['photo_database_weekly'], '02:15')  # 45 minutes before the USB copy
        self.assertEqual(times['photo_database'], '02:15')  # the old daily schedule: two hours before the apps
        self.assertEqual(times['monthly'], '02:30')
        for section, key in (
            ('app_backup', 'time'),
            ('host_backup', 'time'),
            ('maintenance', 'time'),
            ('music', 'backup_time'),
        ):
            self.assertNotIn(key, shipped[section], f'{section}.{key} is no longer a setting of its own')
        for key in ('backup_time', 'backup_weekday', 'database_backup_time'):
            self.assertNotIn(key, shipped['photos'])

    def test_the_nightly_jobs_keep_their_distance_from_the_chosen_time(self):
        times = job_times(settings(daily='23:00'))
        self.assertEqual((times['music'], times['app'], times['host']), ('22:30', '23:00', '00:00'))
        times = job_times(settings(daily='00:10', weekly_day='Wed', weekly='00:20', monthly='01:00'))
        self.assertEqual((times['music'], times['app'], times['host']), ('23:40', '00:10', '01:10'))
        self.assertEqual(times['photo_database'], '22:10')
        self.assertEqual((times['usb_day'], times['usb'], times['photo_database_weekly']), ('Wed', '00:20', '23:35'))
        self.assertEqual(times['monthly'], '01:00')
        self.assertEqual(minutes_later('00:00', -1), '23:59')
        self.assertEqual(minutes_later('23:59', 1), '00:00')

    def test_settings_from_before_keep_each_jobs_own_time(self):
        """A settings file made before 0.48.0 has no backup_times: each job's own setting
        (or its old default) stands, so an upgrade changes no schedule."""
        before = {
            'app_backup': {'time': '01:00'},
            'host_backup': {'time': '02:00'},
            'music': {'backup_time': '00:30'},
            'photos': {'backup_weekday': 'Mon', 'backup_time': '05:00', 'database_backup_time': '04:30'},
            'maintenance': {'time': '03:00'},
        }
        self.assertIsNone(neko_common.backup_times(before))
        times = job_times(before)
        self.assertEqual((times['app'], times['host'], times['music']), ('01:00', '02:00', '00:30'))
        self.assertEqual((times['usb_day'], times['usb']), ('Mon', '05:00'))
        self.assertEqual((times['photo_database'], times['photo_database_weekly']), ('04:30', '04:30'))
        self.assertEqual(times['monthly'], '03:00')
        defaults = job_times({})
        self.assertEqual((defaults['app'], defaults['host'], defaults['music']), ('04:15', '05:15', '03:45'))
        self.assertEqual((defaults['usb_day'], defaults['usb'], defaults['monthly']), ('Sun', '03:00', '02:30'))
        # Once setup asked, backup_times is the one place; an old key left behind changes nothing.
        self.assertEqual(job_times({**before, 'backup_times': {'daily': '22:00'}})['app'], '22:00')
        self.assertEqual(job_times({**before, 'backup_times': {}})['monthly'], '02:30')

    def test_a_wrong_time_is_refused_in_plain_words(self):
        for bad, words in (
            ({'daily': '24:00'}, '"daily" under "backup_times" must be a time of day as HH:MM'),
            ({'weekly': '3:00'}, '"weekly" under "backup_times"'),
            ({'monthly': 230}, '"monthly" under "backup_times"'),
            ({'weekly_day': 'Sunday'}, '"weekly_day" under "backup_times" must be one of Mon, Tue'),
        ):
            with self.assertRaises(RuntimeError) as caught:
                job_times({'backup_times': bad})
            self.assertIn(words, str(caught.exception))
        with self.assertRaises(RuntimeError):
            job_times({'backup_times': 'early'})
        with self.assertRaises(RuntimeError) as caught:
            job_times({'app_backup': {'time': 'noon'}})
        self.assertIn('app_backup.time must be HH:MM', str(caught.exception))
        with self.assertRaises(RuntimeError):
            job_times({'photos': {'backup_weekday': 'Fun'}})

    def test_the_times_are_said_in_a_few_words(self):
        self.assertEqual(
            neko_common.backup_times_said(settings(daily='22:00', weekly_day='Sat', weekly='21:00', monthly='01:15')),
            'daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:15',
        )


class TimersTests(unittest.TestCase):
    """Every timer setup writes takes its time from the three settings."""

    def test_the_nightly_timers(self):
        config = settings(daily='22:00')
        self.assertIn(
            'OnCalendar=*-*-* 22:00:00 UTC\n', app_backup.definitions(config, BIN, GUARD)['nekoserver-app-backup.timer']
        )
        self.assertIn(
            'OnCalendar=*-*-* 23:00:00 UTC\n', host_backup.definitions(config, BIN, GUARD)[host_backup.BACKUP_TIMER]
        )
        self.assertIn(
            'OnCalendar=*-*-* 21:30:00 UTC\n',
            music_backup.definitions(config, BIN, GUARD)['nekoserver-music-backup.timer'],
        )

    def test_the_weekly_timers(self):
        config = settings(weekly_day='Thu', weekly='20:00')
        self.assertEqual(photo_backup.schedule(config), ('Thu', '20:00'))
        _, timer = photo_backup.unit_texts(config, f'{BIN} weekly --config {GUARD} --x', True, 'Thu', '20:00')
        self.assertIn('OnCalendar=Thu *-*-* 20:00:00 UTC\n', timer)
        # The Immich database copy goes 45 minutes before the USB copy, which takes it along.
        weekly = photo_preservation.calendars(config, 'weekly', 10080)
        self.assertEqual(list(weekly), ['nekoserver-immich-db-backup'])
        self.assertIn('OnCalendar=Thu *-*-* 19:15:00 UTC\n', weekly['nekoserver-immich-db-backup'])
        daily = photo_preservation.calendars(settings(daily='06:00'), 'legacy', 360)
        self.assertIn('OnCalendar=*-*-* 04:00:00 UTC\n', daily['nekoserver-immich-db-backup'])
        self.assertIn('OnUnitInactiveSec=360min\n', daily['nekoserver-photo-preservation'])

    def test_the_monthly_timers_and_the_runtimes_copy_of_the_time(self):
        config = settings(monthly='01:45')
        units = maintenance.definitions(config, BIN, GUARD)
        self.assertIn('OnCalendar=*-*-01 01:45:00 UTC\n', units['nekoserver-maintenance.timer'])
        self.assertIn('OnCalendar=Wed *-*-02..31 01:45:00 UTC\n', units[maintenance.UPDATES_TIMER])
        # The runtime reads the monthly time from the step's own settings file (its retry
        # "again next night" timer uses it), so that file carries the resolved time.
        self.assertEqual(maintenance.options(config)['time'], '01:45')
        with mock.patch.object(maintenance.native_runtime, 'saved_paths', return_value={'photos': '/p'}):
            guard = maintenance.guard_settings(config)
        self.assertEqual(guard['maintenance']['time'], '01:45')
        self.assertEqual(guard['paths'], {'photos': '/p'})
        self.assertEqual(guard['backup_times'], config['backup_times'])
        with self.assertRaises(RuntimeError) as caught:
            maintenance.options(settings(monthly='06:30'))
        self.assertIn('must start at night, before 06:00', str(caught.exception))
        self.assertIn('not at 06:30', str(caught.exception))

    def test_a_changed_schedule_makes_witnessed_results_outdated(self):
        import acceptance_summary

        first = acceptance_summary.scope_digest(settings())
        self.assertNotEqual(acceptance_summary.scope_digest(settings(weekly_day='Mon')), first)
        before = {k: v for k, v in settings().items() if k != 'backup_times'}
        self.assertEqual(acceptance_summary.scope_digest(before), acceptance_summary.scope_digest(dict(before)))


class SetupQuestionTests(unittest.TestCase):
    """The drives step asks the three times; Enter keeps the time shown."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.interactive = mock.patch.object(guided_setup, 'interactive', return_value=True)
        self.interactive.start()
        self.addCleanup(self.interactive.stop)

    def config(self, **extra):
        return {'data_mount': self.dir.name, 'timezone': 'UTC', **extra}

    def ask(self, ctx, *typed):
        printed = []
        guided_setup.choose_backup_times(ctx, prompt=answers(*typed), output=printed.append)
        return printed

    def test_enter_keeps_the_times_shown_and_the_answer_is_kept(self):
        ctx = Ctx(self.config())
        printed = self.ask(ctx, '', '', '', '')
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30'}
        )
        self.assertEqual(ctx.saved, 1)
        self.assertEqual(
            neko_common.optional_answers(self.dir.name),
            {'backup_times': 'daily 04:15, weekly Sun 03:00, monthly on the 1st at 02:30'},
        )
        self.assertTrue(
            any('Backups run daily 04:15, weekly Sun 03:00, monthly on the 1st at 02:30' in line for line in printed)
        )
        self.assertIn('Backup times chosen: daily 04:15, weekly Sun 03:00, monthly on the 1st at 02:30', ctx.logs)
        # Said where the person answers: the time zone, what runs when, and the default.
        self.assertTrue(any('(UTC)' in line for line in printed))
        self.assertTrue(any('30 minutes before it' in line and 'an hour after it' in line for line in printed))

    def test_answers_are_checked_and_asked_again(self):
        ctx = Ctx(self.config())
        asked = []

        def prompt(question):
            asked.append(question)
            return {
                1: '25:00',
                2: '7:00',
                3: '23:30',
                4: 'monday',
                5: 'sat',
                6: '21:00',
                7: '06:30',
                8: '01:00',
            }[len(asked)]

        printed = []
        guided_setup.choose_backup_times(ctx, prompt=prompt, output=printed.append)
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00'}
        )
        self.assertEqual(asked[0], 'When should the daily backups run? (HH:MM, Enter for 04:15): ')
        self.assertEqual(
            asked[3],
            'On which day should the weekly USB copy run? (Mon, Tue, Wed, Thu, Fri, Sat, Sun; Enter for Sun): ',
        )
        self.assertEqual(
            asked[7], 'At what time should the monthly check start? (HH:MM before 06:00, Enter for 02:30): '
        )
        self.assertEqual(printed.count('Type a time of day as HH:MM, for example 04:15.'), 2)
        self.assertEqual(printed.count('Type the day as one of Mon, Tue, Wed, Thu, Fri, Sat, Sun.'), 1)
        self.assertEqual(printed.count('Type a time of day as HH:MM before 06:00, for example 02:30.'), 1)
        self.assertFalse(any('[yes/no]' in q for q in asked), 'text questions: the setup window shows a field for them')

    def test_three_wrong_answers_keep_the_default_and_say_so(self):
        ctx = Ctx(self.config())
        printed = self.ask(ctx, 'x', 'y', 'z', '', '', '')
        self.assertEqual(ctx.config['backup_times']['daily'], '04:15')
        self.assertIn('Kept as 04:15.', printed)

    def test_ctrl_d_keeps_the_default(self):
        ctx = Ctx(self.config())

        def prompt(question):
            raise EOFError

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=lambda *_: None)
        self.assertEqual(ctx.config['backup_times']['monthly'], '02:30')
        self.assertEqual(ctx.saved, 1)

    def test_a_server_set_up_before_is_offered_its_own_times(self):
        ctx = Ctx(
            self.config(
                app_backup={'time': '23:00'},
                photos={'backup_weekday': 'Fri', 'backup_time': '22:00'},
                maintenance={'time': '01:30'},
            )
        )
        asked = []

        def prompt(question):
            asked.append(question)
            return ''

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=lambda *_: None)
        self.assertIn('Enter for 23:00', asked[0])
        self.assertIn('Enter for Fri', asked[1])
        self.assertIn('Enter for 22:00', asked[2])
        self.assertIn('Enter for 01:30', asked[3])
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '23:00', 'weekly_day': 'Fri', 'weekly': '22:00', 'monthly': '01:30'}
        )

    def test_a_rerun_keeps_the_answer_unless_asked_again(self):
        config = self.config(
            backup_times={'daily': '22:00', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00'}
        )
        neko_common.remember_answer(
            self.dir.name, 'backup_times', 'daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00'
        )
        neko_common.remember_answer(self.dir.name, 'playback', 'skipped')
        ctx = Ctx(dict(config))
        printed = self.ask(ctx, 'no')
        self.assertEqual(ctx.config['backup_times']['daily'], '22:00')
        self.assertEqual(ctx.saved, 0)
        self.assertIs(ctx.ask_optional_again, False)  # the later steps ask no more
        self.assertIn('Last time, the optional questions were answered this way:', printed)
        self.assertIn('  Backup times: daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00', printed)
        self.assertIn('  Playback test: skipped', printed)
        self.assertIn('Backup times kept: daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00', ctx.logs)
        self.assertIn('Optional questions: kept as answered last time', ctx.logs)
        # yes: asked again, with the current times as the defaults.
        ctx = Ctx(dict(config))
        asked = []

        def prompt(question):
            asked.append(question)
            return 'yes' if '[yes/no]' in question else ''

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=lambda *_: None)
        self.assertIs(ctx.ask_optional_again, True)
        self.assertIn('Enter for 22:00', asked[1])
        self.assertIn('Enter for Sat', asked[2])
        self.assertEqual(ctx.config['backup_times']['daily'], '22:00')
        self.assertEqual(ctx.saved, 1)
        # A decision already made in this run is not asked twice.
        ctx = Ctx(dict(config))
        ctx.ask_optional_again = False
        self.assertEqual(self.ask(ctx), [])
        self.assertEqual(ctx.saved, 0)

    def test_nobody_at_the_terminal_changes_nothing(self):
        self.interactive.stop()
        ctx = Ctx(self.config(), apply=True)
        with mock.patch.object(guided_setup.sys.stdin, 'isatty', return_value=False):
            guided_setup.choose_backup_times(ctx, prompt=answers(), output=lambda *_: None)
        self.assertNotIn('backup_times', ctx.config)
        self.interactive.start()

    def test_the_drives_step_asks_only_when_the_run_writes_a_timer(self):
        asked = []
        with mock.patch.object(guided_setup, 'choose_backup_times', side_effect=lambda ctx: asked.append(ctx)):
            ctx = Ctx({})
            ctx.plan = ['system', 'storage', 'recovery', 'server_os', 'gateway_lan', 'samba']  # the network profile
            storage.choose_backup_times(ctx)
            self.assertEqual(asked, [])
            ctx.plan = ['system', 'storage', 'recovery', 'app_backup']
            storage.choose_backup_times(ctx)
            self.assertEqual(asked, [ctx])
            other = Ctx({})  # no plan known: asked
            storage.choose_backup_times(other)
            self.assertEqual(asked, [ctx, other])
        source = (ROOT / 'modules/storage.py').read_text()
        self.assertLess(
            source.index('    choose_usb(context)\n    choose_backup_times(context)'), source.index('def save_storage')
        )


if __name__ == '__main__':
    unittest.main()
