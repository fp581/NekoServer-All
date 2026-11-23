"""Backup cards (0.52.0), the setup side: the switch on setup's first page, what it tells
the runtime's jobs and the dashboard, and that a server set up before is left exactly as
it was until the switch is turned on."""

import json
import os
import pwd
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import explanations  # noqa: E402
import native_runtime  # noqa: E402
import neko_common  # noqa: E402
from modules import backup_cards, dashboard  # noqa: E402

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
        self.assertIs(self.written(without_nextcloud)[0]['cards']['nextcloud'], False)


class Ctx:
    def __init__(self, config, apply=False):
        self.config, self.apply, self.said = config, apply, []

    def log(self, line):
        self.said.append(line)


class ScheduleTests(unittest.TestCase):
    """The nightly run: an hour after the other nightly jobs, the job's own service (which
    the dashboard's "Write to the card now" starts too), nothing while switched off."""

    def test_the_cards_go_an_hour_after_the_last_nightly_job(self):
        cfg = switched_on(settings_0513())
        times = neko_common.job_times(cfg)
        self.assertEqual(cfg['backup_times']['daily'], '04:15')
        self.assertEqual((times['host'], times['cards']), ('05:15', '06:15'))
        units = backup_cards.definitions(cfg, '/rt/nekoserver-runtime', '/etc/nekoserver-backups/storage.json')
        self.assertEqual(set(units), {'nekoserver-cards.service', 'nekoserver-cards.timer'})
        self.assertIn('OnCalendar=*-*-* 06:15:00 ', units['nekoserver-cards.timer'])
        self.assertIn(
            'ExecStart=/rt/nekoserver-runtime cards --config /etc/nekoserver-backups/storage.json',
            units['nekoserver-cards.service'],
        )
        # A settings file from before 0.48.0 (each job its own time): after this PC's settings.
        old = switched_on(settings_0513())
        del old['backup_times']
        old['host_backup']['time'] = '23:30'
        self.assertEqual(neko_common.job_times(old)['cards'], '00:30')

    def test_switched_off_nothing_is_written_and_an_old_schedule_is_retired(self):
        ctx = Ctx(settings_0513(), apply=True)
        with mock.patch('os.geteuid', return_value=0), mock.patch('modules._schedules.retire') as retire:
            backup_cards.run(ctx)
        retired = [p.name for p in retire.call_args[0][0]]
        self.assertEqual(retired, ['nekoserver-cards.timer', 'nekoserver-cards.service'])
        self.assertIn('Backup cards: off', ctx.said[0])
        dry = Ctx(switched_on(settings_0513()))
        backup_cards.run(dry)
        self.assertIn('every night at 06:15', dry.said[0])

    def test_a_server_from_0513_gets_the_step_which_does_nothing_until_switched_on(self):
        from config_checks import merge_profiles

        template = json.loads((ROOT / 'config.json').read_text())
        old = settings_0513()
        old['profiles']['full'].remove('backup_cards')  # the profile as 0.51.3 saved it
        old['profile_steps_known'] = {'full': list(old['profiles']['full'])}
        lines, _ = merge_profiles(old, template)
        self.assertTrue(any('backup_cards' in line for line in lines), lines)
        self.assertIn('backup_cards', old['profiles']['full'])
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            self.assertIs(neko_common.cards_enabled(old), False)


class ControlUnitTests(unittest.TestCase):
    """The control service enrols the cards: with the switch on it may write the set's
    folder and the places a card is mounted, and has the restore tool beside it."""

    def test_the_control_service_is_as_before_until_the_switch_is_on(self):
        code = Path('/usr/local/lib/nekoserver-dashboard/v' + neko_common.VERSION)
        before = dashboard.control_units(code, False, True, '/srv/nekoserver')[dashboard.CONTROL]
        self.assertNotIn('/media', before)
        on = dashboard.control_units(code, False, True, '/srv/nekoserver', cards=True)[dashboard.CONTROL]
        self.assertIn(' -/srv/nekoserver/cards -/media -/run/media -/mnt\n', on)
        self.assertEqual(on.replace(' -/srv/nekoserver/cards -/media -/run/media -/mnt', ''), before)


HARNESS = ROOT / 'tests' / 'dashboard_harness.mjs'
NODE = shutil.which('node')


def page(*calls):
    done = subprocess.run(
        [NODE, str(HARNESS), json.dumps(calls)], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(done.stdout)


def a_set(now):
    return {
        'format': 'neko-cards-v1',
        'updated_at': now,
        'set_name': 'NekoServer cards',
        'cards': [
            {'number': 1, 'state': 'in_use', 'plugged': False, 'files': 1200, 'full': True},
            {'number': 2, 'state': 'in_use', 'plugged': True, 'files': 34, 'full': False},
        ],
        'files_on_cards': 1234,
        'files_waiting': 1,
        'last_write_at': now - 2 * 86400,
        'full_card': None,
        'next_number': 3,
    }


@unittest.skipUnless(NODE, 'node is not installed')
class PageTests(unittest.TestCase):
    def test_the_page_says_which_card_is_in_and_what_waits(self):
        now = time.time()
        full = {**a_set(now), 'full_card': 2, 'cards': [{'number': 2, 'plugged': False, 'files': 3, 'full': True}]}
        said = page(
            ('cardsSummary', a_set(now)),
            ('cardsSummary', full),
            ('cardsSummary', {}),
            ('cardLine', a_set(now)['cards'][0]),
            ('cardsOn', {'cards': {'enabled': True}}),
            ('cardsOn', {}),
        )
        self.assertEqual(said[0][0], 'Card 2 is in.')
        self.assertEqual(said[0][1], '1234 files on the cards, 1 file waiting for a card.')
        self.assertTrue(said[0][2].startswith('Last written '))
        self.assertEqual(said[1][0], 'No card of the set is in. 1 card in the set.')
        self.assertEqual(said[1][-1], 'Card 2 is full: put in a new card (it becomes card 3).')
        self.assertTrue(said[2][0].startswith('No card is set up yet.'))
        self.assertEqual(said[3], 'Card 1 · not in · 1200 files · full')
        self.assertEqual(said[4:], [True, False])

    def test_a_task_is_said_in_a_line_while_it_runs_and_after(self):
        said = page(
            ('cardsTaskLine', a_task('check', True, 1, 4)),
            ('cardsTaskLine', a_task('check', True)),
            ('cardsTaskLine', a_task('take_back', True, 10, 52)),
            ('cardsTaskLine', a_task('check', False, 4, 4, True, 'Card 2 checked: 4 copies fine.')),
            ('cardsTaskLine', a_task('take_back', False, 3, 52, False, '')),
            ('cardsTaskLine', None),
            ('cardsTaskLine', {'kind': 'format', 'running': True}),
        )
        self.assertEqual(said[0], {'line': 'Checking card 2: 1 of 4 files…', 'running': True, 'failed': False})
        self.assertEqual(said[1]['line'], 'Checking card 2…')
        self.assertEqual(said[2]['line'], 'Taking the cards back: 10 of 52 files…')
        self.assertEqual(said[3], {'line': 'Card 2 checked: 4 copies fine.', 'running': False, 'failed': False})
        self.assertEqual(said[4], {'line': 'It stopped before the end.', 'running': False, 'failed': True})
        self.assertEqual(said[5:], [None, None])

    def test_a_card_of_the_set_can_be_checked_and_one_of_an_older_set_taken_back(self):
        unsaid = {k: v for k, v in OLDER_CARDS[1].items() if k != 'why'}
        said = page(('driveOffer', OUR_CARD), ('driveOffer', OLDER_CARDS[1]), ('driveOffer', unsaid))
        self.assertEqual(said[0]['check'], 2)
        self.assertIsNone(said[0]['action'])
        self.assertEqual(said[1]['older'], 2)
        self.assertIsNone(said[1]['action'])
        self.assertEqual(said[1]['said'], 'This card belongs to another backup set.')  # the server's words
        self.assertTrue(said[2]['said'].startswith('Card 2 of an earlier backup set (made on '), said[2])
        self.assertIn('2023', said[2]['said'])  # the set's year: 1700000000 is November 2023

    def test_each_drive_is_offered_only_as_the_server_judged_it(self):
        empty = {'label': 'CARD', 'size': 64e9, 'fstype': 'exfat', 'usable': True}
        theirs = {'label': 'OLD', 'fstype': 'ext4', 'usable': False, 'why': 'This card belongs to another backup set.'}
        ours = {'label': 'C1', 'fstype': 'ext4', 'card': 1}
        said = page(('driveOffer', empty), ('driveOffer', theirs), ('driveOffer', ours))
        # driveOffer(d, next) with next missing: the harness passes one argument.
        self.assertIn('Use this as card', said[0]['action'])
        self.assertEqual(
            said[1], {'name': 'OLD · ext4', 'said': 'This card belongs to another backup set.', 'action': None}
        )
        self.assertEqual(said[2]['said'], 'Card 1 of your set.')


RUNTIME = ROOT / 'runtime/bin/nekoserver-runtime'
BROWSER = ROOT / 'tests' / 'dashboard_browser.mjs'


def playwright_works():
    if not NODE or not RUNTIME.is_file() or os.geteuid() == 0:
        return False
    probe = "require('playwright').chromium.launch().then(b => b.close())"
    try:
        return subprocess.run([NODE, '-e', probe], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


OUR_CARD = {'mount': '/run/media/amy/C2', 'label': 'C2', 'size': 64e9, 'fstype': 'exfat', 'card': 2, 'usable': False}
OLDER_CARDS = [
    {
        'mount': f'/run/media/amy/OLD{n}',
        'label': f'OLD{n}',
        'size': 32e9,
        'fstype': 'exfat',
        'usable': False,
        'why': 'This card belongs to another backup set.',
        'older_set': {'made': 1700000000, 'number': n},
    }
    for n in (1, 2)
]


def a_task(kind, running, done=0, total=0, ok=True, message=''):
    card = 2 if kind == 'check' else None
    return {'kind': kind, 'card': card, 'running': running, 'done': done, 'total': total, 'ok': ok, 'message': message}


class StandInControl(socketserver.StreamRequestHandler):
    """The control service as the page sees it: signed in, one empty card plugged in.
    For a check or a take-back: `drives` (None: that empty card), `plan` (the tasks
    cards-list hands out, one per look, the last one kept), `after_start` (the plan a
    start sets), `start` (the answer to a start; None: started)."""

    asked = []
    drives = None
    plan = []
    after_start = []
    task = None
    start = None
    fail_look = None  # which look after a start fails (as when Wi-Fi drops for a moment)
    looks = 0

    def handle(self):
        request = json.loads(self.rfile.readline())
        StandInControl.asked.append(request)
        op = request.get('op')
        if op == 'status':
            access = {'local': True, 'pin_set': True, 'unlocked': True, 'signed_in': True}
            answer = {'ok': True, 'format': 'neko-control-v1', 'fans': {}, 'lights': {}, 'access': access}
            answer['apps'] = {'available': True, 'actions': []}
        elif op == 'cards_list':
            drives = [
                {'mount': '/run/media/amy/CARD', 'label': 'CARD', 'size': 64e9, 'fstype': 'exfat', 'usable': True},
                {
                    'mount': '/media/amy/WIN',
                    'label': 'WIN',
                    'size': 32e9,
                    'fstype': 'ntfs',
                    'usable': False,
                    'why': 'This card is formatted ntfs, which backup cards are not: they are ext4 or exFAT.',
                },
            ]
            shown = a_set(time.time())
            if StandInControl.drives is not None:
                drives = StandInControl.drives
            if drives is OLDER_CARDS:
                shown = {**shown, 'cards': [], 'files_on_cards': 0, 'next_number': 1}
            StandInControl.looks += 1
            if StandInControl.looks == StandInControl.fail_look:
                self.wfile.write((json.dumps({'ok': False, 'error': 'E01'}) + '\n').encode())
                return
            if len(StandInControl.plan) > 1:
                StandInControl.task = StandInControl.plan.pop(0)
            elif StandInControl.plan:
                StandInControl.task = StandInControl.plan[0]
            answer = {'ok': True, 'set': shown, 'drives': drives, 'next_number': 2, 'task': StandInControl.task}
        elif op in ('cards_check', 'cards_take_back') and StandInControl.start:
            answer = StandInControl.start
        elif op == 'cards_check':
            answer = {'ok': True, 'message': 'Checking card 2. Leave it in until the check has finished.'}
            StandInControl.plan, StandInControl.looks = list(StandInControl.after_start), 0
        elif op == 'cards_take_back':
            answer = {'ok': True, 'message': 'Taking the cards back.'}
            StandInControl.plan, StandInControl.looks = list(StandInControl.after_start), 0
        elif op == 'cards_enrol' and request.get('acknowledged') is True:
            answer = {'ok': True, 'number': 2, 'message': 'Card 2 is ready. Write “2” on it.'}
        else:
            answer = {'ok': False, 'error': 'E42', 'message': 'Tick that you understand first.'}
        self.wfile.write((json.dumps(answer) + '\n').encode())


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class PageInBrowserTests(unittest.TestCase):
    """The page in Chromium, served by the runtime's own web server, with a stand-in for
    the control service behind it."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        here = Path(cls.folder.name)
        now = time.time()
        snapshot = {
            'format': 'neko-dashboard-v1',
            'generated_at': now,
            'late_after': 630,
            'version': 'x',
            'acceptance': {'status': 'complete'},
            'applications': [],
            'storage': [],
            'backups': [
                {
                    'id': 'cards',
                    'name': 'Backup cards',
                    'status': 'pass',
                    'last_success': now - 2 * 86400,
                    'timer': {'installed': True, 'active': True},
                    'unit': 'nekoserver-cards.service',
                }
            ],
            'integrity': [],
            'alerts': [],
            'startup': [],
            'cards': {'enabled': True, 'set': a_set(now)},
        }
        (here / 'status.json').write_text(json.dumps(snapshot))
        cls.control = socketserver.ThreadingUnixStreamServer(str(here / 'control.sock'), StandInControl)
        threading.Thread(target=cls.control.serve_forever, daemon=True).start()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        command = [str(RUNTIME), 'serve', '--assets', str(ROOT / 'dashboard'), '--status', str(here / 'status.json')]
        command += ['--port', str(port), '--control', str(here / 'control.sock')]
        cls.server = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.address = f'http://127.0.0.1:{port}/'
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.control.shutdown()
        cls.folder.cleanup()

    def test_the_backups_page_shows_the_set_with_the_cards_job(self):
        done = subprocess.run(
            [NODE, str(BROWSER), self.address, json.dumps([('pageText', {'route': 'backups', 'id': 'backups'})])],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        shown = json.loads(done.stdout)[0]
        self.assertIn('Backup cards', shown)
        self.assertIn('Card 2 is in.', shown)
        self.assertIn('1234 files on the cards, 1 file waiting for a card.', shown)
        self.assertIn('Write to the card now', shown)

    def test_a_card_is_found_and_enrolled_through_the_page(self):
        for width in (1280, 390):
            StandInControl.asked.clear()
            done = subprocess.run(
                [NODE, str(BROWSER), self.address, json.dumps([('cardsPage', {'width': width})])],
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            seen = json.loads(done.stdout)[0]
            with self.subTest(width=width):
                self.assertTrue(seen['nav'])
                self.assertIn('Card 2 is in.', seen['set'])
                self.assertIn('Write to the card now', seen['set'])
                self.assertIn('Empty: it can be card 2 of the set.', seen['drives'][0])
                self.assertIn('formatted ntfs', seen['drives'][1])
                self.assertEqual(seen['dialog'], 'Use this card as card 2?')
                self.assertEqual(seen['toast'], 'Card 2 is ready. Write “2” on it.')
                enrol = [r for r in StandInControl.asked if r['op'] == 'cards_enrol']
                self.assertEqual(enrol[0]['mount'], '/run/media/amy/CARD')
                self.assertEqual((enrol[0]['number'], enrol[0]['acknowledged']), (2, True))
                self.assertLessEqual(seen['widths'][0], seen['widths'][1], 'no sideways scrolling')


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class CardTasksInBrowserTests(unittest.TestCase):
    """ "Check this card" and "Take these cards back" in Chromium, in both themes, against
    the stand-in: each start, its progress lines, its result, a refusal and the PIN."""

    setUpClass = classmethod(PageInBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(PageInBrowserTests.tearDownClass.__func__)

    RUNNING_CHECK = [a_task('check', True, 1, 4), a_task('check', True, 3, 4)]
    CHECKED = 'Card 2 checked: 3 copies fine, 1 damaged (u1/2024/IMG_1.jpg); the next run copies that file again.'

    def setUp(self):
        StandInControl.asked.clear()
        StandInControl.drives, StandInControl.plan, StandInControl.after_start = None, [], []
        StandInControl.task, StandInControl.start, StandInControl.fail_look = None, None, None

    tearDown = setUp

    def run_task(self, **wanted):
        done = subprocess.run(
            [NODE, str(BROWSER), self.address, json.dumps([('cardsTask', wanted)])],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        return json.loads(done.stdout)[0]

    def test_a_card_of_the_set_is_checked_through_the_page(self):
        for look in ('neko', 'professional'):
            for width in (1280, 390):
                with self.subTest(look=look, width=width):
                    self.setUp()
                    StandInControl.drives = [OUR_CARD]
                    StandInControl.after_start = [*self.RUNNING_CHECK, a_task('check', False, 4, 4, True, self.CHECKED)]
                    seen = self.run_task(look=look, width=width, start='Check this card', go='Check card 2')
                    self.assertIn(['Check this card', True], seen['offered'])
                    self.assertIn(['Write to the card now', True], seen['offered'])
                    self.assertNotIn('Take these cards back', [b[0] for b in seen['offered']])
                    self.assertEqual(seen['dialog'][:2], ['BACKUP CARDS', 'Check card 2?'])
                    self.assertIn('Nothing on the card is changed or erased.', seen['dialog'][2])
                    self.assertEqual(seen['toast'], 'Checking card 2. Leave it in until the check has finished.')
                    self.assertEqual(seen['lines'][0], ['Checking card 2: 1 of 4 files…', False, True])
                    self.assertIn(['Checking card 2: 3 of 4 files…', False, True], seen['lines'])
                    self.assertEqual(seen['lines'][-1], [self.CHECKED, False, False])
                    check = [r for r in StandInControl.asked if r['op'] == 'cards_check']
                    self.assertEqual((check[0]['mount'], check[0]['number']), ('/run/media/amy/C2', 2))
                    self.assertEqual(seen['errors'], [])
                    self.assertLessEqual(seen['widths'][0], seen['widths'][1], 'no sideways scrolling')

    def test_cards_of_an_older_set_are_taken_back_through_the_page(self):
        for look in ('neko', 'professional'):
            with self.subTest(look=look):
                self.setUp()
                StandInControl.drives = OLDER_CARDS
                taken = 'Cards 1 and 2 taken back: 52 files are known again; the next run writes on.'
                StandInControl.after_start = [
                    a_task('take_back', True, 10, 52),
                    a_task('take_back', False, 52, 52, True, taken),
                ]
                seen = self.run_task(look=look, width=390, start='Take these cards back', go='Take the cards back')
                self.assertIn(['Take these cards back', True], seen['offered'])
                self.assertNotIn('Check this card', [b[0] for b in seen['offered']])
                self.assertEqual(seen['dialog'][:2], ['BACKUP CARDS', 'Take these cards back?'])
                self.assertIn('cards 1 and 2', seen['dialog'][2])
                self.assertIn('Only do this with your own cards', seen['dialog'][3])
                self.assertEqual(seen['toast'], 'Taking the cards back.')
                self.assertEqual(seen['lines'][0], ['Taking the cards back: 10 of 52 files…', False, True])
                self.assertEqual(seen['lines'][-1], [taken, False, False])
                self.assertEqual([r['op'] for r in StandInControl.asked].count('cards_take_back'), 1)
                self.assertEqual(seen['errors'], [])
                self.assertLessEqual(seen['widths'][0], seen['widths'][1], 'no sideways scrolling')

    def test_a_check_that_ends_badly_says_so_as_a_failure(self):
        StandInControl.drives = [OUR_CARD]
        out = 'Card 2 was taken out during the check: plug it in again and press Check this card again.'
        StandInControl.after_start = [self.RUNNING_CHECK[0], a_task('check', False, 1, 4, False, out)]
        seen = self.run_task(look='neko', width=1280, start='Check this card', go='Check card 2')
        self.assertEqual(seen['lines'][-1], [out, True, False])

    def test_a_look_that_fails_mid_check_is_tried_again(self):
        # T5's review of ca346e3: one failed look (Wi-Fi gone for a moment) stopped the
        # page looking, and "Checking card 2: 1 of 4 files…" stayed with every button off.
        StandInControl.drives, StandInControl.fail_look = [OUR_CARD], 2
        StandInControl.after_start = [*self.RUNNING_CHECK, a_task('check', False, 4, 4, True, self.CHECKED)]
        seen = self.run_task(look='neko', width=1280, start='Check this card', go='Check card 2')
        self.assertEqual(seen['lines'][-1], [self.CHECKED, False, False])

    def test_a_refused_start_says_why_and_shows_no_progress(self):
        StandInControl.drives = [OUR_CARD]
        why = 'The nightly run is still running: try again when it has finished.'
        StandInControl.start = {'ok': False, 'error': 'E42', 'message': why}
        seen = self.run_task(look='professional', width=1280, start='Check this card', go='Check card 2')
        self.assertEqual(seen['toast'], 'E42 · ' + why)
        self.assertEqual((seen['asked'], seen['lines']), ([], []))

    def test_the_pin_is_asked_for_when_this_device_is_not_signed_in(self):
        StandInControl.drives = OLDER_CARDS
        StandInControl.start = {'ok': False, 'error': 'E43', 'message': 'Enter your PIN to change this'}
        seen = self.run_task(look='neko', width=1280, start='Take these cards back', go='Take the cards back')
        self.assertEqual(seen['asked'], ['BACKUP CARDS', 'Unlock changes'])
        self.assertEqual(seen['lines'], [])

    def test_while_a_task_runs_nothing_else_can_be_started(self):
        for drives, start in (([OUR_CARD], 'Check this card'), (OLDER_CARDS, 'Take these cards back')):
            with self.subTest(start=start):
                StandInControl.drives, StandInControl.plan = drives, [self.RUNNING_CHECK[0]]
                seen = self.run_task(look='neko', width=1280)
                self.assertIn([start, False], seen['offered'])
                self.assertIn(['Write to the card now', False], seen['offered'])
                self.assertIn(['Look again', True], seen['offered'])


class RecoveryMenuTests(unittest.TestCase):
    """The recovery menu's "From backup cards": the card plugged in, read with the
    release's copy of the tool every card carries, into a new folder."""

    def setUp(self):
        import recovery_wizard
        import test_neko_restore

        self.wizard = recovery_wizard
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        card = test_neko_restore.Card(self.tmp / 'card')
        card.add('2001/beach.jpg', b'sand and sea')
        card.add('2002/cake.jpg', b'candles')
        self.mounts = self.tmp / 'mounts'
        self.mounts.write_text(f'/dev/sdb1 {self.tmp}/card exfat rw 0 0\n/dev/sda1 / ext4 rw 0 0\n')
        self.config = {
            'data_mount': str(self.tmp / 'ssd'),
            'media_mount': str(self.tmp / 'hdd'),
            'cards': {'enabled': True},
        }
        roots = mock.patch('neko_restore.CARD_ROOTS', (str(self.tmp) + '/',))
        roots.start()
        self.addCleanup(roots.stop)

    def run_tool(self, card, *args, capture=True):
        """The tool in this process (its card places are the test's folder)."""
        import neko_restore

        said = []
        status = neko_restore.main(['--card', str(card), *args], say=said.append)
        return status, '\n'.join(said)

    def test_it_is_offered_while_cards_are_on_and_restores_the_card_plugged_in(self):
        with mock.patch.object(neko_common, 'server_exists', return_value=True):
            self.assertIn('cards', dict(self.wizard.menu(self.config)))
            self.assertNotIn('cards', dict(self.wizard.menu({**self.config, 'cards': {'enabled': False}})))
        self.assertEqual(self.wizard.MENU[-1][1].split(':')[0], 'From backup cards')
        self.assertEqual(self.wizard.plugged_cards(self.mounts), [str(self.tmp / 'card')])
        target = self.tmp / 'restored'
        said = []
        answers = iter(['beach', 'yes'])
        with (
            mock.patch.object(self.wizard, 'card_tool', self.run_tool),
            mock.patch.object(self.wizard, 'restore_target', return_value=target),
            mock.patch.object(self.wizard, 'asked', side_effect=lambda *a, **k: next(answers) == 'yes'),
        ):
            self.wizard.restore_from_cards(self.config, lambda q='': next(answers), said.append, self.mounts)
        self.assertEqual((target / '2001/beach.jpg').read_bytes(), b'sand and sea')
        self.assertFalse((target / '2002/cake.jpg').exists(), 'only what was asked for')
        text = '\n'.join(said)
        self.assertIn('Example set - 001', text)
        self.assertIn(f'Done: the files are in {target}.', text)

    def test_the_restored_files_are_handed_to_the_desktop_user(self):
        """Review of 0.52: the tool runs as root into a folder only root may open, and the
        card restore never gave it to your account as a photo restore does, so "Done" was
        said over files the desktop user could not open."""
        target = self.tmp / 'restored'
        said, given = [], []
        answers = iter(['', 'yes'])
        uid, gid = os.getuid(), os.getgid()
        with (
            mock.patch.object(self.wizard, 'card_tool', self.run_tool),
            mock.patch.object(self.wizard, 'restore_target', return_value=target),
            mock.patch.object(self.wizard, 'asked', side_effect=lambda *a, **k: next(answers) == 'yes'),
            mock.patch.object(self.wizard, 'owner', return_value=(uid, gid)),
            mock.patch.object(self.wizard, 'give_to', side_effect=lambda *a: given.append(a[:3])),
        ):
            self.wizard.restore_from_cards(self.config, lambda q='': next(answers), said.append, self.mounts)
        self.assertEqual(given, [(target, uid, gid)])
        name = pwd.getpwuid(uid).pw_name
        self.assertIn(f'The folder and its files now belong to {name}', '\n'.join(said))

    def test_no_card_plugged_in_says_how_to_connect_one(self):
        said = []
        self.wizard.restore_from_cards(self.config, lambda q='': '', said.append, self.tmp / 'none')
        self.assertTrue(said[0].startswith('No backup card is plugged in and mounted.'))
        self.assertIn('sudo mount -o ro', said[1])


if __name__ == '__main__':
    unittest.main()
