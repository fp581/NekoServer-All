"""Backup cards (0.52.0), the setup side: the switch on setup's first page, what it tells
the runtime's jobs and the dashboard, and that a server set up before is left exactly as
it was until the switch is turned on."""

import json
import os
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
        self.assertEqual(self.written(without_nextcloud)[0]['cards']['nextcloud'], False)


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


class StandInControl(socketserver.StreamRequestHandler):
    """The control service as the page sees it: signed in, one empty card plugged in."""

    asked = []

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
            answer = {'ok': True, 'set': a_set(time.time()), 'drives': drives, 'next_number': 2}
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
            'backups': [],
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


if __name__ == '__main__':
    unittest.main()
