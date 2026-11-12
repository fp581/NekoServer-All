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
    """What is typed at each question in turn; Enter alone once these run out (the last
    question of the page, how many nightly backups are kept, unless a test answers it)."""
    given = iter(values)
    return lambda question='': next(given, '')


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
        self.assertEqual((times['usb_day'], times['usb']), ('Wed', '00:20'))
        # 45 minutes before Wed 00:20 is Tue 23:35, not Wed 23:35 (23 hours after the USB copy).
        self.assertEqual((times['photo_database_weekly_day'], times['photo_database_weekly']), ('Tue', '23:35'))
        self.assertEqual(neko_common.day_before_if_wrapped('Mon', '00:30', -45), 'Sun')
        self.assertEqual(neko_common.day_before_if_wrapped('Mon', '00:45', -45), 'Mon')
        self.assertEqual(neko_common.day_before_if_wrapped('Sun', '23:30', 45), 'Mon')
        # Settings from before 0.48.0 name the database copy's own time: its day is the USB copy's.
        before = {'photos': {'backup_weekday': 'Mon', 'backup_time': '00:20', 'database_backup_time': '23:35'}}
        self.assertEqual(job_times(before)['photo_database_weekly_day'], 'Mon')
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
        # A USB copy just after midnight: the database copy the evening before, on the day before.
        early = photo_preservation.calendars(settings(weekly_day='Sun', weekly='00:20'), 'weekly', 10080)
        self.assertIn('OnCalendar=Sat *-*-* 23:35:00 UTC\n', early['nekoserver-immich-db-backup'])
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
        too_late = settings(monthly='06:30')
        with self.assertRaises(RuntimeError) as caught:
            maintenance.options(too_late)
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
        # With Immich, so the weekly USB copy is on this server and its day and time are asked.
        return {'data_mount': self.dir.name, 'timezone': 'UTC', 'photos': {'deploy_immich': True}, **extra}

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
            }.get(len(asked), '')

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
        self.assertFalse(
            any('yes or no' in q for q in asked), 'text questions: the setup window shows a field for them'
        )

    def window_prompt(self, first, fields=None, skip=False):
        """A terminal prompt whose first question the setup window answers, with the rest
        of the page (`fields`) or with its "Skip for now"; any further prompt fails the test."""
        import setup_progress

        def prompt(question=''):
            record = setup_progress.question(question)
            self.assertTrue(setup_progress.answer_from_window(record['seq'], first, skip=skip, fields=fields))
            setup_progress.answered()
            if skip:
                raise EOFError
            return first

        return prompt

    def test_the_setup_window_answers_the_three_as_one_page(self):
        import setup_progress

        ctx = Ctx(self.config())
        page = {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00'}
        asked = []
        printed = []

        def prompt(question=''):
            asked.append(question)
            self.assertEqual(len(asked), 1, 'the window answered the whole page: nothing else is asked')
            # What the window draws with the first question: the page, every field and its current value.
            shown = setup_progress.page_shown()
            self.assertEqual(shown['form'], 'backup_times')
            self.assertEqual(
                shown['fields'],
                {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30', 'keep_host_backup': '3'},
            )
            self.assertEqual((shown['asking'], shown['weekdays'][-1], shown['night_end']), ('daily', 'Sun', 6))
            return self.window_prompt('23:30', {**page, 'keep_host_backup': '5'})(question)

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=printed.append)
        self.assertEqual(ctx.config['backup_times'], page)
        # How many settings backups are kept came with the page too (0.50.0: 1 to 7).
        self.assertEqual(ctx.config['host_backup']['keep_snapshots'], 5)
        self.assertEqual(sum('(from the setup window)' in line for line in printed), 4)
        self.assertIsNone(setup_progress.page_shown(), 'the page is over once the answers are saved')

    def test_the_windows_skip_for_now_keeps_the_times_shown(self):
        ctx = Ctx(self.config())
        printed = []
        guided_setup.choose_backup_times(ctx, prompt=self.window_prompt('', skip=True), output=printed.append)
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30'}
        )
        self.assertEqual(ctx.saved, 1)

    def test_a_field_the_window_sent_wrong_is_asked_in_the_terminal(self):
        # The window checks its fields itself; a value that still arrives wrong (another
        # browser, say) is explained and asked the plain way, the others kept.
        ctx = Ctx(self.config())
        printed = []
        first = self.window_prompt('23:30', {'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '07:00'})
        later = answers('01:15')
        prompts = iter([first])

        def prompt(question=''):
            return next(prompts, later)(question)

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=printed.append)
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:15'}
        )
        self.assertIn('Type a time of day as HH:MM before 06:00, for example 02:30.', printed)

    def test_a_time_in_other_digits_is_refused(self):
        """Python's \\d is any Unicode digit: "1٠:3٠" (Arabic-Indic zeros) passed the check and
        would have been written into the timers' OnCalendar, which systemd cannot read."""
        for written in ('1٠:3٠', '1٠:0٥', '0１:3０', '٢٣:٣٠'):
            self.assertFalse(neko_common.valid_time(written), written)
        self.assertTrue(neko_common.valid_time('23:30'))
        # Typed in the terminal: explained and asked again, the time shown kept.
        ctx = Ctx(self.config())
        printed = self.ask(ctx, '1٠:3٠', '', '', '', '')
        self.assertEqual(ctx.config['backup_times']['daily'], '04:15')
        self.assertIn('Type a time of day as HH:MM, for example 04:15.', printed)
        # Sent by the setup window: refused there too, and asked in the terminal.
        neko_common.optional_answers_file(self.dir.name).unlink()  # the case above's answer, not this one's
        ctx = Ctx(self.config())
        printed = []
        first = self.window_prompt('23:30', {'weekly_day': 'Sat', 'weekly': '1٩:0٠', 'monthly': '01:00'})
        later = answers('21:15')
        prompts = iter([first])
        guided_setup.choose_backup_times(
            ctx, prompt=lambda question='': next(prompts, later)(question), output=printed.append
        )
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:15', 'monthly': '01:00'}
        )
        self.assertIn('Type a time of day as HH:MM, for example 03:00.', printed)

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
                photos={'deploy_immich': True, 'backup_weekday': 'Fri', 'backup_time': '22:00'},
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
        # Enter everywhere: its own times stand, unsaved (saving them would move the other jobs).
        self.assertNotIn('backup_times', ctx.config)
        self.assertIn(
            'Backup times kept as they were: daily 23:00, weekly Fri 22:00, monthly on the 1st at 01:30', ctx.logs
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
            return 'yes' if 'yes or no' in question else ''

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

    def before_0_48(self):
        """Settings from before 0.48.0: every job with a time of its own."""
        return self.config(
            app_backup={'time': '23:00'},
            host_backup={'time': '02:00'},
            music={'backup_time': '03:45'},
            photos={
                'deploy_immich': True,
                'backup_weekday': 'Fri',
                'backup_time': '22:00',
                'database_backup_time': '21:00',
                'backup_schedule': 'weekly',
            },
            maintenance={'time': '01:30'},
        )

    def test_enter_everywhere_on_a_server_set_up_before_keeps_its_own_times(self):
        # Saved as backup_times, the times shown would still have moved the music copy to
        # 22:30 and the copy of this PC's settings to 00:00, their fixed distances from
        # the daily time, without a word.
        ctx = Ctx(self.before_0_48())
        printed = self.ask(ctx, '', '', '', '')
        self.assertNotIn('backup_times', ctx.config)
        self.assertEqual(ctx.saved, 0)
        said = 'daily 23:00, weekly Fri 22:00, monthly on the 1st at 01:30'
        self.assertIn('Backup times kept as they were: ' + said, ctx.logs)
        self.assertEqual(neko_common.optional_answers(self.dir.name)['backup_times'], said)
        self.assertTrue(any(line.startswith('Backups run ' + said) for line in printed))
        self.assertFalse(any('moves from' in line for line in printed))
        # The setup window's "Skip for now" is Enter everywhere.
        ctx = Ctx(self.before_0_48())
        guided_setup.choose_backup_times(ctx, prompt=self.window_prompt('', skip=True), output=lambda *_: None)
        self.assertNotIn('backup_times', ctx.config)
        self.assertEqual(ctx.saved, 0)

    def test_a_changed_time_on_a_server_set_up_before_says_which_jobs_move(self):
        ctx = Ctx(self.before_0_48())
        printed = self.ask(ctx, '22:30', '', '', '')
        self.assertEqual(
            ctx.config['backup_times'], {'daily': '22:30', 'weekly_day': 'Fri', 'weekly': '22:00', 'monthly': '01:30'}
        )
        self.assertEqual(ctx.saved, 1)
        self.assertIn(
            "With these times the music copy moves from 03:45 to 22:00, the copy of this PC's own settings moves "
            "from 02:00 to 23:30, Immich's database copy moves from Fri 21:00 to Fri 21:15.",
            printed,
        )
        # From the setup window the same: here only the weekly time changed, yet three jobs move.
        ctx = Ctx(self.before_0_48())
        printed = []
        page = {'daily': '23:00', 'weekly_day': 'Fri', 'weekly': '20:00', 'monthly': '01:30'}
        guided_setup.choose_backup_times(ctx, prompt=self.window_prompt('23:00', page), output=printed.append)
        self.assertEqual(ctx.config['backup_times'], page)
        self.assertIn(
            "With these times the music copy moves from 03:45 to 22:30, the copy of this PC's own settings moves "
            "from 02:00 to 00:00, Immich's database copy moves from Fri 21:00 to Fri 19:15.",
            printed,
        )
        # A server set up with 0.48.0 has no times of its own to lose: nothing to say
        # (yes: ask again, after the answer the cases above left on the SSD).
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times'])))
        printed = self.ask(ctx, 'yes', '22:30', '', '', '')
        self.assertFalse(any('moves from' in line for line in printed))
        self.assertEqual(ctx.saved, 1)

    def test_a_remembered_answer_that_is_not_what_the_settings_hold_is_asked_again(self):
        # The settings file was made again from the template (04:15, Sun 03:00, 02:30)
        # while the SSD still remembered the old answer: the rerun showed the old times,
        # "no" kept them in words only, and every timer was written at the template's.
        old = 'daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00'
        template = 'daily 04:15, weekly Sun 03:00, monthly on the 1st at 02:30'
        neko_common.remember_answer(self.dir.name, 'backup_times', old)
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times'])))
        asked = []

        def prompt(question):
            asked.append(question)
            return '22:00' if question.startswith('When should the daily') else ''

        printed = []
        guided_setup.choose_backup_times(ctx, prompt=prompt, output=printed.append)
        self.assertFalse(any('yes or no' in question for question in asked), 'not offered to keep')
        self.assertEqual(len(asked), 5)  # the three times, the day, and how many are kept
        self.assertIn(
            f'The backup times in the settings ({template}) are not the ones chosen last time ({old}), '
            'so they are asked again.',
            printed,
        )
        self.assertEqual(ctx.config['backup_times']['daily'], '22:00')
        self.assertEqual(ctx.saved, 1)
        # The same answer as the settings: offered to keep, as before.
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times'])))
        neko_common.remember_answer(self.dir.name, 'backup_times', template)
        printed = self.ask(ctx, 'no')
        self.assertEqual(ctx.saved, 0)
        self.assertIn('  Backup times: ' + template, printed)

    def test_the_list_of_last_answers_shows_the_times_the_settings_hold(self):
        # Another step's turn to offer the list, the drives step having kept quiet: the
        # backup times shown are the settings' own, not the SSD's remembered words.
        old = 'daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00'
        neko_common.remember_answer(self.dir.name, 'backup_times', old)
        neko_common.remember_answer(self.dir.name, 'playback', 'done')
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times'])))
        printed = []
        kept = guided_setup.optional_kept(ctx, ['playback'], prompt=answers('no'), output=printed.append)
        self.assertEqual(kept, ['playback'])
        self.assertIn('  Backup times: daily 04:15, weekly Sun 03:00, monthly on the 1st at 02:30', printed)
        self.assertIn('  Playback test: done', printed)
        self.assertFalse(any('22:00' in line for line in printed))

    def test_a_monthly_time_no_longer_at_night_is_offered_the_latest_time_that_is(self):
        # maintenance.night_end_hour lowered to 2 since the times were chosen: Enter, Ctrl+D
        # or the window's Skip kept 02:30 unchecked, and the maintenance step refused it.
        import setup_progress

        self.assertEqual(guided_setup.monthly_offered('02:30', 2), '01:30')
        self.assertEqual(guided_setup.monthly_offered('02:30', 6), '02:30')
        self.assertEqual(guided_setup.monthly_offered('05:45', 1), '00:45')
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times']), maintenance={'night_end_hour': 2}))
        asked = []

        def prompt(question):
            asked.append(question)
            if question.startswith('At what time should the monthly'):
                self.assertEqual(setup_progress.page_shown()['fields']['monthly'], '01:30', 'the page shows it too')
            return ''

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=lambda *_: None)
        self.assertEqual(
            asked[3],
            'At what time should the monthly check start? (HH:MM before 02:00, Enter for 01:30; '
            '02:30 is not before 02:00): ',
        )
        self.assertEqual(ctx.config['backup_times']['monthly'], '01:30')
        self.assertEqual(maintenance.options(ctx.config)['time'], '01:30')
        # The window's Skip for now keeps the time offered, not the refused one.
        ctx = Ctx(self.config(backup_times=dict(settings()['backup_times']), maintenance={'night_end_hour': 2}))
        guided_setup.choose_backup_times(ctx, prompt=self.window_prompt('', skip=True), output=lambda *_: None)
        self.assertEqual(ctx.config['backup_times']['monthly'], '01:30')

    def test_a_weekday_typed_in_lowercase_reaches_the_page_as_the_day_itself(self):
        # The page recorded "sat" as typed; the window's day selector knows only Sat and
        # went blank.
        import setup_progress

        typed = guided_setup.ask_on_page(
            'd', 'Day: ', 'Mon', lambda a: True, 'no', answers('tue'), lambda *_: None, str.capitalize
        )
        self.assertEqual(typed, 'Tue')
        recorded = []

        def record(key, value):
            recorded.append((key, value))

        with mock.patch.object(setup_progress, 'page_set', side_effect=record):
            ctx = Ctx(self.config())
            self.ask(ctx, '', 'sat', '', '')
            self.assertIn(('weekly_day', 'Sat'), recorded)
            # Sent by the window (another browser's page, say): the same.
            ctx = Ctx(self.config())
            fields = {'weekly_day': 'SUN', 'weekly': '21:00', 'monthly': '01:00'}
            guided_setup.choose_backup_times(ctx, prompt=self.window_prompt('23:30', fields), output=lambda *_: None)
            self.assertIn(('weekly_day', 'Sun'), recorded)
        self.assertFalse(any(value in ('sat', 'SUN') for _, value in recorded))
        self.assertEqual(ctx.config['backup_times']['weekly_day'], 'Sun')

    def test_a_run_that_does_not_write_every_timer_again_keeps_the_times_and_asks_nothing(self):
        # sudo bash install.sh --module host_backup --apply reaches the drives step (nearly
        # every step needs it): new times were saved, and only host_backup's timer was
        # written at them, the other jobs left at the old ones.
        installed = {step: {'status': 'success'} for step in ('storage', 'app_backup', 'host_backup', 'maintenance')}
        chosen = {'daily': '22:00', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00'}
        config = self.config(backup_times=chosen)
        ctx = Ctx(dict(config))
        ctx.plan, ctx.previous = ['system', 'storage', 'recovery', 'host_backup'], installed
        printed = self.ask(ctx)  # any question would fail the test
        self.assertEqual(ctx.config, config)
        self.assertEqual(ctx.saved, 0)
        said = 'daily 22:00, weekly Sat 21:00, monthly on the 1st at 01:00'
        self.assertIn(
            f'Backup times are changed only by a full setup run (sudo bash install.sh); this run keeps {said}.', printed
        )
        self.assertIn('Backup times kept, not a full run: ' + said, ctx.logs)
        self.assertFalse(guided_setup.revisits_every_job(ctx))
        # The full run has every job step: asked. So is a first run, with nothing installed yet.
        for plan, previous in (
            (['system', 'storage', 'recovery', 'app_backup', 'host_backup', 'maintenance'], installed),
            (['system', 'storage', 'recovery', 'app_backup'], {}),
        ):
            ctx = Ctx(dict(config))
            ctx.plan, ctx.previous = plan, previous
            asked = []

            def prompt(question):
                if 'yes or no' in question:  # the first round's answer, offered on the second: ask again
                    return 'yes'
                asked.append(question)
                return ''

            guided_setup.choose_backup_times(ctx, prompt=prompt, output=lambda *_: None)
            self.assertEqual(len(asked), 5, plan)
        self.assertTrue(guided_setup.revisits_every_job(Ctx({})), 'no plan known: asked')

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
        # Asked by the drives step, after the drives (which are one page of their own).
        self.assertLess(
            source.index('        place_drives(context, inventory)\n    choose_backup_times(context)'),
            source.index('def save_storage'),
        )


if __name__ == '__main__':
    unittest.main()


class OnlyWhatTheServerHasTests(unittest.TestCase):
    """Gate runs B2 and B4: the page named the music copy with Music off, and asked the
    weekly USB copy's day and time on a server with no USB drive, no Immich and no
    Nextcloud."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(guided_setup, 'interactive', return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_daily_words_name_only_the_jobs_there_are(self):
        films_only = {'media_apps': {'movies': True, 'music': False}}
        self.assertNotIn('music', guided_setup.daily_words(films_only))
        self.assertIn("the media apps' snapshot", guided_setup.daily_words(films_only))
        both = {'media_apps': {'movies': True, 'music': True}}
        self.assertIn('the music copy 30 minutes before it', guided_setup.daily_words(both))
        neither = {'media_apps': {'movies': False, 'music': False}}
        # It runs an hour after the time chosen (job_times), media apps or not (gate run J06).
        self.assertEqual(
            guided_setup.daily_words(neither), "the copy of this PC's own settings an hour after the time you choose"
        )
        self.assertEqual(neko_common.minutes_later('23:00', 60), job_times(settings(daily='23:00'))['host'])

    def test_no_weekly_question_without_a_weekly_copy(self):
        config = {
            'data_mount': self.dir.name,
            'timezone': 'UTC',
            'media_apps': {'movies': True, 'music': False},
            'photos': {'deploy_immich': False},
            'nextcloud': {'enabled': False},
        }
        ctx = Ctx(config)
        asked, printed = [], []

        def prompt(question=''):
            asked.append(question)
            return ''

        guided_setup.choose_backup_times(ctx, prompt=prompt, output=printed.append)
        self.assertFalse([q for q in asked if 'weekly USB copy' in q or 'on that day' in q], asked)
        self.assertFalse([line for line in printed if 'USB copy' in line or 'music copy' in line], printed)
        # The weekly times stay what they were.
        self.assertEqual((ctx.config['backup_times']['weekly_day'], ctx.config['backup_times']['weekly']), ('Sun', '03:00'))

    def test_the_offline_drive_left_out_asks_no_weekly_time(self):
        config = {'photos': {'deploy_immich': True, 'external_backup_wanted': False}}
        self.assertFalse(guided_setup.weekly_copy(config))
        config['photos']['external_backup_wanted'] = True
        self.assertTrue(guided_setup.weekly_copy(config))
        self.assertEqual(
            guided_setup.weekly_words({'photos': {'deploy_immich': False}, 'nextcloud': {'enabled': True}}),
            'your Nextcloud files',
        )
