"""dashboard/app.js run in Node against a stand-in page (skipped without Node): what the
page lists as needing attention, how it reads backup checksums and app states."""
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

HARNESS = Path(__file__).resolve().parent / 'dashboard_harness.mjs'
ROOT = HARNESS.parents[1]
NODE = shutil.which('node')


def page(*calls, zone=None):
    """Answers of the page's own functions, one per (name, argument) call; `zone` is the
    time zone the stand-in browser is in."""
    env = {**os.environ, 'TZ': zone} if zone else None
    result = subprocess.run([NODE, str(HARNESS), json.dumps(calls)], capture_output=True, text=True,
                            timeout=60, check=True, env=env)
    return json.loads(result.stdout)


def status(**parts):
    return {'format': 'neko-dashboard-v1', 'generated_at': time.time(), 'version': 'x',
            'acceptance': {'status': 'complete'}, **parts}


@unittest.skipUnless(NODE, 'node is not installed')
class DashboardPageTests(unittest.TestCase):
    def test_app_states_come_from_health_checks_never_just_running(self):
        cases = [({'state': 'exited'}, 'exited'), ({}, 'unknown'),
                 ({'state': 'running', 'health': 'unhealthy'}, 'unhealthy'),
                 ({'state': 'running', 'health': 'starting'}, 'starting'),
                 ({'state': 'running', 'responding': False}, 'unhealthy'),
                 ({'state': 'running', 'health': 'healthy'}, 'healthy'),
                 ({'state': 'running', 'responding': True}, 'healthy'),
                 ({'state': 'running'}, 'running')]
        answers = page(*[('appState', app) for app, _ in cases])
        self.assertEqual(answers, [want for _, want in cases])

    def test_checksums_match_only_with_a_recent_full_check_and_no_damage(self):
        now = time.time()
        recent = {'id': 'usb', 'full_at': now - 86400}
        answers = page(('filesIntact', []),
                       ('filesIntact', [recent]),
                       ('filesIntact', [{**recent, 'damaged': 2, 'missing': 1}]),
                       ('filesIntact', [recent, {'id': 'nas'}]),
                       ('filesIntact', [{**recent, 'full_at': now - 40 * 86400}]),
                       ('filesIntact', [{**recent, 'unverified': 4}]),
                       ('filesIntact', [{**recent, 'full_at': now + 86400}]))
        self.assertEqual(answers, [['unknown', 'No check yet'], ['pass', 'Checksums match'], ['fail', '3 files'],
                                   ['unknown', 'No full check yet'], ['stale', 'Check overdue'],
                                   ['unknown', '4 not yet checked'], ['unknown', 'No full check yet']])

    def test_issues_list_urgent_first_and_keep_every_server_alert(self):
        [issues] = page(('collectIssues', status(
            alerts=[{'id': 'backup:weekly', 'title': 'Weekly copy failed', 'level': 'warning'},
                    {'id': 'tailscale:login', 'title': 'Signed out', 'level': 'critical'}],
            storage=[{'id': 'media', 'name': 'Media', 'status': 'missing'},
                     {'id': 'ssd', 'name': 'SSD', 'status': 'mounted', 'used_percent': 95}],
            applications=[{'id': 'jellyfin', 'state': 'running', 'health': 'unhealthy'},
                          {'id': 'immich', 'state': 'running', 'responding': True}],
            backups=[{'id': 'music', 'status': 'disabled', 'timer': {'installed': True}},
                     {'id': 'weekly', 'name': 'Weekly', 'status': 'fail'}],
            integrity=[{'id': 'usb', 'name': 'USB', 'damaged': 1}])))
        ids = [i['id'] for i in issues]
        levels = [i['level'] for i in issues]
        self.assertEqual(levels, sorted(levels, key=lambda level: level != 'critical'), issues)
        for wanted in ('tailscale:login', 'mount:media', 'space:ssd', 'app:jellyfin', 'integrity:usb', 'job:weekly'):
            self.assertIn(wanted, ids)
        # A server alert about a backup and the page's own finding are listed once.
        self.assertEqual(ids.count('job:weekly'), 1)
        self.assertEqual(next(i for i in issues if i['id'] == 'job:weekly')['title'], 'Weekly copy failed')
        # A switched-off backup, a working app and a finished server check need nothing.
        self.assertFalse([i for i in ids if 'music' in i or 'immich' in i or i == 'acceptance'], ids)
        self.assertEqual(next(i for i in issues if i['id'] == 'app:jellyfin')['title'], 'Jellyfin · Not responding')

    def test_an_unfinished_server_check_is_listed(self):
        [issues] = page(('collectIssues', status(acceptance={'status': 'pending'})))
        self.assertEqual(issues, [{'id': 'acceptance', 'level': 'warning', 'title': 'Finish checking your server'}])


    def test_the_fixed_speed_slider_starts_at_the_never_slower_than_floor(self):
        # The control service clamps a fixed speed to "Never slower than" (and 20 %), so the
        # slider never offers a speed the fan would not really get.
        floors = page(['fixedFloor', {'min': 35}], ['fixedFloor', {'min': 5}], ['fixedFloor', {}])
        self.assertEqual(floors, [35, 20, 20])
        raised, kept = page(['raiseFixed', {'min': 40, 'fixed': 30}], ['raiseFixed', {'min': 40, 'fixed': 60}])
        self.assertEqual(raised, {'min': 40, 'value': 40, 'fixed': 40})
        self.assertEqual(kept, {'min': 40, 'value': 60, 'fixed': 60})


HDD_AWAY = 'The Media HDD is missing, so this backup can’t run until it is back.'


def hdd_missing():
    """The user panel's "HDD missing" scene, as the collector reports it."""
    now = time.time()
    names = {'jellyfin': 'Jellyfin', 'immich': 'Immich', 'nextcloud': 'Nextcloud'}
    return status(
        storage=[{'id': 'data_ssd', 'name': 'Application SSD', 'status': 'mounted'},
                 {'id': 'media_hdd', 'name': 'Media HDD', 'status': 'missing'}],
        alerts=[{'id': 'mount:media_hdd', 'level': 'critical', 'title': 'Media HDD unavailable',
                 'detail': 'Reconnect it. Stopped until it is back: Jellyfin, Immich, Nextcloud.'}],
        applications=[{'id': app, 'name': name, 'state': 'exited'} for app, name in names.items()],
        backups=[{'id': job, 'name': job, 'status': 'pass', 'last_success': now - 3600, 'blocked': HDD_AWAY,
                  'timer': {'installed': True, 'active': True}} for job in ('archive', 'applications', 'maintenance')],
        integrity=[{'id': 'archive', 'name': 'HDD photo archive', 'full_at': now - 86400, 'blocked': 'away'},
                   {'id': 'usb_photos', 'name': 'USB photo copy', 'full_at': now - 86400}])


@unittest.skipUnless(NODE, 'node is not installed')
class UserPanelFindingsTests(unittest.TestCase):
    """What the 0.40.1 user panel and Dana found on the dashboard (UP-xx, D10)."""

    def test_a_missing_hdd_never_shows_backups_as_ok_and_raises_one_alert(self):
        # UP-03 and UP-04: no "All recent copies OK" or "Checksums match" while the HDD is
        # missing, and the apps it stopped are not eight more items.
        scene = hdd_missing()
        summary, recent, intact, archive, usb, issues = page(
            ('backupSummary', scene), ('recentCopies', scene['backups']), ('filesIntact', scene['integrity']),
            ('integrityResult', scene['integrity'][0]), ('integrityResult', scene['integrity'][1]),
            ('issueDetails', scene))
        self.assertEqual(summary, 'Paused: a drive is missing')
        self.assertEqual(recent, ['warning', 'Can’t run: drive missing'])
        self.assertEqual(intact, ['warning', 'Can’t check: drive missing'])
        self.assertEqual((archive['state'], archive['label']), ('warning', 'Drive missing'))
        self.assertEqual(usb['label'], 'Matches', 'the USB copy is on its own drive')
        self.assertEqual([i['id'] for i in issues], ['mount:media_hdd'], issues)
        # With the drive back, a stopped app is an item again.
        back = hdd_missing()
        back['storage'][1]['status'] = 'mounted'
        back['alerts'] = []
        for job in back['backups']:
            del job['blocked']
        [issues] = page(('collectIssues', back))
        self.assertIn('app:jellyfin', [i['id'] for i in issues])

    def test_a_rolled_back_update_is_a_warning_that_opens_the_updates_guide(self):
        # UP-05: the collector marks it "partial" with rolled_back; the page says so.
        job = {'id': 'maintenance', 'name': 'Monthly backup and updates', 'status': 'partial', 'rolled_back': True,
               'last_success': time.time() - 33 * 86400, 'timer': {'installed': True, 'active': True},
               'reason': 'The update was undone: your apps are back on their previous version and work as before.'}
        issues, guide = page(('issueDetails', status(backups=[job])), ('guideFor', 'job:maintenance'))
        self.assertEqual(issues, [{'id': 'job:maintenance', 'level': 'warning', 'guide': 'updates',
                                   'title': 'Monthly backup and updates · Update undone', 'detail': job['reason']}])
        self.assertEqual(guide, 'updates')

    def test_long_reasons_are_shortened_at_a_sentence_never_mid_word(self):
        # UP-06: the start stays whole; "Show more" (longText) gives the rest.
        sentence = 'Backup copies failed their checksum check: 1 Nextcloud file no longer matches. '
        long = sentence + 'To settle it, run sudo bash install.sh --recover and choose the repair option ' * 3
        short, words, kept = page(('shortText', long), ('shortText', 'word ' * 80), ('shortText', 'Short.'))
        self.assertEqual(short, sentence.strip())
        self.assertTrue(words.endswith('word…'), words)
        self.assertEqual(kept, 'Short.')

    def test_pin_refusals_say_the_tries_left_and_until_when_to_wait(self):
        # UP-07: what the control service sends is shown, with the wait as a local time.
        noon = 1793534400000  # 2026-11-01 12:00 UTC
        tries, locked, old = page(
            ('pinRefusal', {'answer': {'error': 'E44', 'message': 'Wrong PIN. 3 tries left on this device', 'tries_left': 3}}),
            ('pinRefusal', {'answer': {'error': 'E45', 'wait_seconds': 300,
                                       'message': 'Too many wrong PINs: this device can try again in 5 minutes'}, 'now': noon}),
            ('pinRefusal', {'answer': {'error': 'E44'}}), zone='Asia/Jerusalem')
        self.assertEqual(tries, 'E44 · Wrong PIN. 3 tries left on this device.')
        # 12:05 UTC is 14:05 in Israel (written as the browser's language writes times).
        self.assertRegex(locked, r'^E45 · Too many wrong PINs: this device can try again in 5 minutes '
                                 r'\(at (14:05|0?2:05\s?PM)\)\.$')
        self.assertEqual(old, 'E44 · Wrong PIN.')

    def test_the_pin_dialog_shows_its_own_refusals(self):
        # UP-01: a toast sits under the modal dialog's backdrop, so the refusal is written
        # inside the dialog, and any toast moves into an open dialog.
        app = (ROOT / 'dashboard/app.js').read_text()
        unlock = app[app.index('function unlockDialog('):app.index('function lockAgain(')]
        self.assertIn('said.textContent = pinRefusal(answer)', unlock)
        self.assertNotIn('toast(controlMessage(answer))', unlock)
        self.assertIn("said.setAttribute('role', 'alert')", unlock)
        self.assertIn("$('dialog').open ? $('dialog') : document.body", app)

    def test_next_runs_are_shown_in_the_households_time(self):
        # UP-11: 03:00 UTC is 05:00 in Israel; the systemd text is only a fallback.
        at = 1793502000  # Sun 2026-11-01 03:00:00 UTC
        local, fallback = page(('nextRun', {'installed': True, 'next_run': 'Sun 2026-11-01 03:00:00 UTC', 'next_run_at': at}),
                               ('nextRun', {'installed': True, 'next_run': 'Sun 2026-11-01 03:00:00 UTC'}),
                               zone='Asia/Jerusalem')
        self.assertIn('05:00', local)
        self.assertNotIn('UTC', local)
        self.assertEqual(fallback, 'Next run: Sun 2026-11-01 03:00:00 UTC')

    def test_apps_setup_never_reached_are_not_set_up_yet(self):
        # D10: one item to finish setup, not "try opening it" for each app.
        apps = [{'id': 'jellyfin', 'state': 'not_set_up'}, {'id': 'immich', 'state': 'not_set_up'}]
        scene = status(applications=apps)
        issues, summary, partly = page(('issueDetails', scene), ('appsSummary', scene),
                                         ('appsSummary', status(applications=[apps[0], {'id': 'immich', 'state': 'running'}])))
        self.assertEqual([(i['id'], i['title'], i['guide']) for i in issues],
                         [('setup-unfinished', 'Setup isn’t finished yet', 'setup')])
        self.assertIn('Jellyfin, Immich are not set up yet', issues[0]['detail'])
        self.assertNotIn('try opening it', issues[0]['detail'])
        self.assertEqual(summary, 'Not set up yet')
        self.assertEqual(partly, '1 of 1 running · 1 not set up yet')


@unittest.skipUnless(NODE, 'node is not installed')
class RoundTwoTests(unittest.TestCase):
    """What QA found left after the 0.40.2 fixes."""

    def test_counts_agree_with_their_noun(self):
        now = time.time()
        one = {'id': 'usb', 'full_at': now - 86400, 'damaged': 1}
        jobs = [{'id': 'archive', 'status': 'fail', 'last_success': now, 'timer': {'installed': True, 'active': True}}]
        intact, result, recent, summary = page(('filesIntact', [one]), ('integrityResult', one), ('recentCopies', jobs),
                                               ('backupSummary', status(backups=jobs)))
        self.assertEqual(intact, ['fail', '1 file'])
        self.assertEqual(result['words'], '1 damaged or missing file.')
        self.assertEqual(recent, ['warning', '1 needs a look'])
        self.assertEqual(summary, '1 needs a look')
        [two] = page(('filesIntact', [{**one, 'damaged': 2}]))
        self.assertEqual(two, ['fail', '2 files'])

    def test_steam_is_offered_only_where_the_tv_was_set_up(self):
        # D10: collect.rs says whether the TV step set Steam up; older readings don't say.
        shown = page(('shownApps', status(tv={'set_up': False})), ('shownApps', status(tv={'set_up': True})),
                     ('shownApps', status()))
        self.assertEqual([('steam' in ids) for ids in shown], [False, True, True])

    def test_apps_not_set_up_have_nothing_to_open_or_restart(self):
        # D10: no "Open" link and no ⋯ restart menu (nor right-click) on such a card.
        app = (ROOT / 'dashboard/app.js').read_text()
        card = app[app.index('function appCard('):app.index('function noAppsMessage(')]
        self.assertIn("const notSetUp = appState(app) === 'not_set_up', controls = !spec.shortcut && !notSetUp;", card)
        self.assertIn("...(controls ? [menuButton(", card)
        self.assertIn('if (!notSetUp) { bottom.append(openControl(app, spec)); }', card)
        self.assertIn("if (controls) {\n    card.addEventListener('contextmenu'", card)


class PhoneLayoutTests(unittest.TestCase):
    def test_the_app_filter_chips_wrap_on_a_phone(self):
        # UP-14: a side-scrolling row with a hidden scrollbar cut "Management" off.
        css = (ROOT / 'dashboard/style.css').read_text()
        phone = css[css.index('@media (max-width: 700px)'):]
        self.assertNotIn('.chips { flex-wrap: nowrap', phone)

    def test_the_fans_page_says_the_same_heat_rule_as_the_code_and_docs(self):
        # UP-15: five minutes above 95 °C (fans.rs EMERGENCY_C, HOT_FOR), 105 °C at once.
        page_text = (ROOT / 'dashboard/index.html').read_text()
        fans = (ROOT / 'runtime/src/fans.rs').read_text()
        self.assertIn('const EMERGENCY_C: f64 = 95.0;', fans)
        self.assertIn('const EMERGENCY_NOW_C: f64 = 105.0;', fans)
        self.assertIn('after five minutes above 95 °C, at once at 105 °C', page_text)
        self.assertNotIn('full speed at 95 °C', page_text)
        self.assertIn('after five minutes above 95 °C', (ROOT / 'DASHBOARD.md').read_text())


if __name__ == '__main__':
    unittest.main()
