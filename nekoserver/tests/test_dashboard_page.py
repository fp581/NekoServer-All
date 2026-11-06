"""dashboard/app.js run in Node against a stand-in page (skipped without Node): what the
page lists as needing attention, how it reads backup checksums and app states."""

import json
import os
import re
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
    result = subprocess.run(
        [NODE, str(HARNESS), json.dumps(calls)], capture_output=True, text=True, timeout=60, check=True, env=env
    )
    return json.loads(result.stdout)


def status(**parts):
    return {
        'format': 'neko-dashboard-v1',
        'generated_at': time.time(),
        'version': 'x',
        'acceptance': {'status': 'complete'},
        **parts,
    }


@unittest.skipUnless(NODE, 'node is not installed')
class DashboardPageTests(unittest.TestCase):
    def test_each_waiting_version_says_when_it_really_installs(self):
        # Found in the lab: a version that had waited long enough said "installs from
        # Nov 1" on Nov 3, and every waiting version said it installs, though the update
        # night installs one: the one you chose, else the newest that has waited.
        now, day = 1_800_000_000, 86400
        old = {'version': '10.11.2', 'first_seen': now - 9 * day, 'installs_from': now - 2 * day}
        older = {'version': '10.11.1', 'first_seen': now - 12 * day, 'installs_from': now - 5 * day}
        new = {'version': '10.11.3', 'first_seen': now - 2 * day, 'installs_from': now + 5 * day}
        said = page(
            ('waitingWhen', {'waiting': [new, old, older], 'now': now}),
            ('waitingWhen', {'waiting': [{**new, 'approved': True}, old], 'now': now}),
        )
        self.assertEqual(said[0][1], 'has waited long enough: installs at the next update night')
        self.assertEqual(said[0][2], 'a newer version that has waited long enough installs instead')
        self.assertTrue(said[0][0].startswith('installs from '))
        self.assertEqual(
            said[1],
            [
                'installs at the next update night (you chose it)',
                'not installed by itself (you chose another version for the next update night)',
            ],
        )
        self.assertIn('an update, an upgrade) need your PIN', (ROOT / 'dashboard/app.js').read_text())

    def test_lists_of_words_are_shown_not_dropped(self):
        # Found in the lab (0.45.4): the restart's reason read "not known: ." and an
        # update that restarts Docker never said so. `list` keeps objects (rows) only,
        # so every list of words passed through it came back empty (since 0.41.1).
        answers = page(
            ('words', ['Docker (every app restarts)', 3, '', {'x': 1}, None]),
            ('words', None),
            ('arrayShown', {'name': 'md0', 'holds_names': ['Application SSD', 'Media HDD']}),
        )
        self.assertEqual(answers, [['Docker (every app restarts)'], [], 'md0 (Application SSD, Media HDD)'])
        source = (ROOT / 'dashboard/app.js').read_text()
        for field in ('sys.restart_reasons', 'u.restarts', 'a.holds', 'a.holds_names'):
            self.assertIn(f'words({field})', source)
            self.assertNotIn(f'list({field})', source)

    def test_the_updates_page_links_each_apps_own_release_notes(self):
        answers = page(
            ('releaseNotes', {'app': 'immich', 'version': 'v2.3.1'}),
            ('releaseNotes', {'app': 'immich', 'version': 'new build of 2026-11-01'}),
            ('releaseNotes', {'app': 'jellyfin', 'version': '10.11.2'}),
            ('releaseNotes', {'app': 'unknown', 'version': 'x'}),
        )
        self.assertEqual(answers[0], 'https://github.com/immich-app/immich/releases/tag/v2.3.1')
        self.assertEqual(answers[1], 'https://github.com/immich-app/immich/releases')
        self.assertEqual(answers[2], 'https://github.com/jellyfin/jellyfin/releases')
        self.assertEqual(answers[3], '')
        # What the server sends is kept, the waiting versions listed per app.
        shown = page(('validStatus', status(waiting_updates={'settle_days': 7, 'apps': [{'app': 'immich'}, 'bad']})))
        self.assertEqual(shown[0]['waiting_updates'], {'settle_days': 7, 'apps': [{'app': 'immich'}]})
        # The buttons call the background update service, never a pull of an image.
        source = (ROOT / 'dashboard/app.js').read_text()
        self.assertIn("appAction('update-run', { app, sig }", source)
        self.assertNotIn('docker pull', source)

    def test_app_states_come_from_health_checks_never_just_running(self):
        cases = [
            ({'state': 'exited'}, 'exited'),
            ({}, 'unknown'),
            ({'state': 'running', 'health': 'unhealthy'}, 'unhealthy'),
            ({'state': 'running', 'health': 'starting'}, 'starting'),
            ({'state': 'running', 'responding': False}, 'unhealthy'),
            ({'state': 'running', 'health': 'healthy'}, 'healthy'),
            ({'state': 'running', 'responding': True}, 'healthy'),
            ({'state': 'running'}, 'running'),
        ]
        answers = page(*[('appState', app) for app, _ in cases])
        self.assertEqual(answers, [want for _, want in cases])

    def test_checksums_match_only_with_a_recent_full_check_and_no_damage(self):
        now = time.time()
        recent = {'id': 'usb', 'full_at': now - 86400}
        answers = page(
            ('filesIntact', []),
            ('filesIntact', [recent]),
            ('filesIntact', [{**recent, 'damaged': 2, 'missing': 1}]),
            ('filesIntact', [recent, {'id': 'nas'}]),
            ('filesIntact', [{**recent, 'full_at': now - 40 * 86400}]),
            ('filesIntact', [{**recent, 'unverified': 4}]),
            ('filesIntact', [{**recent, 'full_at': now + 86400}]),
        )
        self.assertEqual(
            answers,
            [
                ['unknown', 'No check yet'],
                ['pass', 'Checksums match'],
                ['fail', '3 files'],
                ['unknown', 'No full check yet'],
                ['stale', 'Check overdue'],
                ['unknown', '4 not yet checked'],
                ['unknown', 'No full check yet'],
            ],
        )

    def test_issues_list_urgent_first_and_keep_every_server_alert(self):
        [issues] = page(
            (
                'collectIssues',
                status(
                    alerts=[
                        {'id': 'backup:weekly', 'title': 'Weekly copy failed', 'level': 'warning'},
                        {'id': 'tailscale:login', 'title': 'Signed out', 'level': 'critical'},
                    ],
                    storage=[
                        {'id': 'media', 'name': 'Media', 'status': 'missing'},
                        {'id': 'ssd', 'name': 'SSD', 'status': 'mounted', 'used_percent': 95},
                    ],
                    applications=[
                        {'id': 'jellyfin', 'name': 'Jellyfin', 'state': 'running', 'health': 'unhealthy'},
                        {'id': 'immich', 'state': 'running', 'responding': True},
                    ],
                    backups=[
                        {'id': 'music', 'status': 'disabled', 'timer': {'installed': True}},
                        {'id': 'weekly', 'name': 'Weekly', 'status': 'fail'},
                    ],
                    integrity=[{'id': 'usb', 'name': 'USB', 'damaged': 1}],
                ),
            )
        )
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

    def test_the_upgrade_has_no_schedule_to_be_off(self):
        """Seen on the rig (0.47.5): after an upgrade that worked, Home listed "NekoServer
        upgrade schedule is off: no new copies are made" for good, the Backups page said
        "Not all checked", and the upgrade finishing was celebrated as a backup copy. The
        upgrade runs when you choose it; the server says so ("scheduled": false)."""
        off = {'installed': False, 'active': False}
        on = {'installed': True, 'active': True}
        upgrade = {
            'id': 'upgrade',
            'name': 'NekoServer upgrade',
            'status': 'pass',
            'last_success': 1000.0,
            'timer': off,
        }
        weekly = {'id': 'weekly', 'name': 'Weekly', 'status': 'pass', 'last_success': 1000.0}
        said, old, stopped = page(
            ('collectIssues', status(backups=[{**upgrade, 'scheduled': False}])),
            ('collectIssues', status(backups=[upgrade])),  # an earlier release's readings
            ('collectIssues', status(backups=[{**weekly, 'timer': off}])),
        )
        self.assertEqual(said, [])
        self.assertEqual([i['id'] for i in old], ['timer:upgrade'])
        self.assertEqual([i['id'] for i in stopped], ['timer:weekly'])  # a real schedule still is
        ok, unchecked = page(
            ('recentCopies', [{**upgrade, 'scheduled': False}, {**weekly, 'timer': on}]),
            ('recentCopies', [{**upgrade, 'scheduled': False}, {**weekly, 'timer': off}]),
        )
        self.assertEqual((ok[1], unchecked[1]), ('All OK', 'Not all checked'))
        done = lambda at: status(backups=[{**upgrade, 'scheduled': False, 'last_success': at}])  # noqa: E731
        self.assertEqual(page(('newlyFinished', [done(1000.0), done(2000.0)])), [[None, None]])
        # The server's side of it: the row of a job with no schedule says so.
        source = (ROOT / 'runtime/src/collect.rs').read_text()
        self.assertIn('row["scheduled"] = json!(false)', source)

    def test_the_updates_page_says_a_jobs_state_in_the_pages_words(self):
        """Seen on the rig (0.47.5): "Update night: unknown (No recorded successful run)" on
        a server whose first update night is still to come, and "last upgrade: pass"."""
        said = page(
            (
                'jobLine',
                ['Update night', {'id': 'updates', 'status': 'unknown', 'reason': 'No recorded successful run'}],
            ),
            ('jobLine', ['last upgrade', {'id': 'upgrade', 'status': 'pass', 'last_success': 1000.0}]),
            (
                'jobLine',
                ['last upgrade', {'id': 'upgrade', 'status': 'partial', 'last_success': 1.0, 'reason': 'Put back'}],
            ),
            ('jobLine', ['Update night', {'id': 'updates', 'status': 'fail', 'reason': 'Docker is not running'}]),
            ('jobLine', ['last upgrade', None]),
        )
        self.assertEqual(
            said,
            [
                'Update night: has not run yet',
                'last upgrade: passed',
                'last upgrade: incomplete (Put back)',
                'Update night: failed (Docker is not running)',
                '',
            ],
        )

    def test_only_backups_in_use_say_whether_your_copies_are_fine(self):
        """Seen on the rig (0.47.6): Home said "Backups: Not all checked yet" and listed
        "Weekly app updates has not succeeded yet" on a server whose backups had all
        passed, because the update night had not run yet. With the music backup switched
        off, Home could never say all was fine. Neither copies anything."""
        on = {'installed': True, 'active': True}
        good = {'id': 'applications', 'name': 'Apps', 'status': 'pass', 'last_success': 1000.0, 'timer': on}
        rows = [
            good,
            {'id': 'music', 'name': 'Music', 'status': 'disabled', 'timer': {'installed': False}},
            {'id': 'updates', 'name': 'Weekly app updates', 'status': 'unknown', 'timer': on},
            {'id': 'update_check', 'name': 'Daily update check', 'status': 'pass', 'last_success': 1.0, 'timer': on},
        ]
        summary, copies, issues = page(
            ('backupSummary', status(backups=rows)),
            ('recentCopies', rows),
            ('collectIssues', status(backups=rows)),
        )
        self.assertEqual((summary, copies[1]), ('All recent copies OK', 'All OK'))
        self.assertEqual(issues, [])
        # A backup that has not run yet still is one to wait for; a failed update night is listed.
        first = {**good, 'status': 'unknown', 'last_success': None}
        failed = {'id': 'updates', 'name': 'Weekly app updates', 'status': 'fail', 'timer': on}
        summary, issues = page(
            ('backupSummary', status(backups=[first, failed])),
            ('collectIssues', status(backups=[first, failed])),
        )
        self.assertEqual(summary, 'Not all checked yet')
        self.assertEqual(sorted(i['id'] for i in issues), ['job:applications', 'job:updates'])

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
        storage=[
            {'id': 'data_ssd', 'name': 'Application SSD', 'status': 'mounted'},
            {'id': 'media_hdd', 'name': 'Media HDD', 'status': 'missing'},
        ],
        alerts=[
            {
                'id': 'mount:media_hdd',
                'level': 'critical',
                'title': 'Media HDD unavailable',
                'detail': 'Reconnect it. Stopped until it is back: Jellyfin, Immich, Nextcloud.',
            }
        ],
        applications=[{'id': app, 'name': name, 'state': 'exited', 'held': True} for app, name in names.items()],
        backups=[
            {
                'id': job,
                'name': job,
                'status': 'pass',
                'last_success': now - 3600,
                'blocked': HDD_AWAY,
                'timer': {'installed': True, 'active': True},
            }
            for job in ('archive', 'applications', 'maintenance')
        ],
        integrity=[
            {'id': 'archive', 'name': 'HDD photo archive', 'full_at': now - 86400, 'blocked': 'away'},
            {'id': 'usb_photos', 'name': 'USB photo copy', 'full_at': now - 86400},
        ],
    )


@unittest.skipUnless(NODE, 'node is not installed')
class UserPanelFindingsTests(unittest.TestCase):
    """What the 0.40.1 user panel and Dana found on the dashboard (UP-xx, D10)."""

    def test_a_missing_hdd_never_shows_backups_as_ok_and_raises_one_alert(self):
        # UP-03 and UP-04: no "All recent copies OK" or "Checksums match" while the HDD is
        # missing, and the apps it stopped are not eight more items.
        scene = hdd_missing()
        summary, recent, intact, archive, usb, issues = page(
            ('backupSummary', scene),
            ('recentCopies', scene['backups']),
            ('filesIntact', scene['integrity']),
            ('integrityResult', scene['integrity'][0]),
            ('integrityResult', scene['integrity'][1]),
            ('issueDetails', scene),
        )
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
        for app in back['applications']:
            app['held'] = False  # the collector no longer holds them
        for job in back['backups']:
            del job['blocked']
        [issues] = page(('collectIssues', back))
        self.assertIn('app:jellyfin', [i['id'] for i in issues])

    def test_a_rolled_back_update_is_a_warning_that_opens_the_updates_guide(self):
        # UP-05: the collector marks it "partial" with rolled_back; the page says so.
        job = {
            'id': 'maintenance',
            'name': 'Monthly backup and updates',
            'status': 'partial',
            'rolled_back': True,
            'last_success': time.time() - 33 * 86400,
            'timer': {'installed': True, 'active': True},
            'reason': 'The update was undone: your apps are back on their previous version and work as before.',
        }
        issues, guide = page(('issueDetails', status(backups=[job])), ('guideFor', 'job:maintenance'))
        self.assertEqual(
            issues,
            [
                {
                    'id': 'job:maintenance',
                    'level': 'warning',
                    'guide': 'updates',
                    'title': 'Monthly backup and updates · Update undone',
                    'detail': job['reason'],
                }
            ],
        )
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
            (
                'pinRefusal',
                {'answer': {'error': 'E44', 'message': 'Wrong PIN. 3 tries left on this device', 'tries_left': 3}},
            ),
            (
                'pinRefusal',
                {
                    'answer': {
                        'error': 'E45',
                        'wait_seconds': 300,
                        'message': 'Too many wrong PINs: this device can try again in 5 minutes',
                    },
                    'now': noon,
                },
            ),
            ('pinRefusal', {'answer': {'error': 'E44'}}),
            zone='Asia/Jerusalem',
        )
        self.assertEqual(tries, 'E44 · Wrong PIN. 3 tries left on this device.')
        # 12:05 UTC is 14:05 in Israel (written as the browser's language writes times).
        self.assertRegex(
            locked,
            r'^E45 · Too many wrong PINs: this device can try again in 5 minutes '
            r'\(at (14:05|0?2:05\s?PM)\)\.$',
        )
        self.assertEqual(old, 'E44 · Wrong PIN.')

    def test_the_pin_dialog_shows_its_own_refusals(self):
        # UP-01: a toast sits under the modal dialog's backdrop, so the refusal is written
        # inside the dialog, and any toast moves into an open dialog.
        app = (ROOT / 'dashboard/app.js').read_text()
        unlock = app[app.index('function unlockDialog(') : app.index('function lockAgain(')]
        self.assertIn('said.textContent = pinRefusal(answer)', unlock)
        self.assertNotIn('toast(controlMessage(answer))', unlock)
        self.assertIn("said.setAttribute('role', 'alert')", unlock)
        self.assertIn("$('dialog').open ? $('dialog') : document.body", app)

    def test_next_runs_are_shown_in_the_households_time(self):
        # UP-11: 03:00 UTC is 05:00 in Israel; the systemd text is only a fallback.
        at = 1793502000  # Sun 2026-11-01 03:00:00 UTC
        local, fallback = page(
            ('nextRun', {'installed': True, 'next_run': 'Sun 2026-11-01 03:00:00 UTC', 'next_run_at': at}),
            ('nextRun', {'installed': True, 'next_run': 'Sun 2026-11-01 03:00:00 UTC'}),
            zone='Asia/Jerusalem',
        )
        self.assertIn('05:00', local)
        self.assertNotIn('UTC', local)
        self.assertEqual(fallback, 'Next run: Sun 2026-11-01 03:00:00 UTC')

    def test_apps_setup_never_reached_are_not_set_up_yet(self):
        # D10: one item to finish setup, not "try opening it" for each app.
        apps = [
            {'id': 'jellyfin', 'name': 'Jellyfin', 'state': 'not_set_up'},
            {'id': 'immich', 'name': 'Immich', 'state': 'not_set_up'},
        ]
        scene = status(applications=apps)
        issues, summary, partly = page(
            ('issueDetails', scene),
            ('appsSummary', scene),
            ('appsSummary', status(applications=[apps[0], {'id': 'immich', 'state': 'running'}])),
        )
        self.assertEqual(
            [(i['id'], i['title'], i['guide']) for i in issues],
            [('setup-unfinished', 'Setup isn’t finished yet', 'setup')],
        )
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
        intact, result, recent, summary = page(
            ('filesIntact', [one]),
            ('integrityResult', one),
            ('recentCopies', jobs),
            ('backupSummary', status(backups=jobs)),
        )
        self.assertEqual(intact, ['fail', '1 file'])
        self.assertEqual(result['words'], '1 damaged or missing file.')
        self.assertEqual(recent, ['warning', '1 needs a look'])
        self.assertEqual(summary, '1 needs a look')
        [two] = page(('filesIntact', [{**one, 'damaged': 2}]))
        self.assertEqual(two, ['fail', '2 files'])

    def test_steam_is_offered_only_where_the_tv_was_set_up(self):
        # D10: collect.rs says whether the TV step set Steam up; older readings don't say.
        shown = page(
            ('shownApps', status(tv={'set_up': False})),
            ('shownApps', status(tv={'set_up': True})),
            ('shownApps', status()),
        )
        self.assertEqual([('steam' in ids) for ids in shown], [False, True, True])

    def test_apps_not_set_up_have_nothing_to_open_or_restart(self):
        # D10: no "Open" link and no ⋯ restart menu (nor right-click) on such a card.
        app = (ROOT / 'dashboard/app.js').read_text()
        card = app[app.index('function appCard(') : app.index('function noAppsMessage(')]
        self.assertIn(
            "const notSetUp = appState(app) === 'not_set_up',\n    controls = !spec.shortcut && !notSetUp;", card
        )
        self.assertIn("...(controls ? [menuButton(", card)
        self.assertIn('if (!notSetUp) {\n    bottom.append(openControl(app, spec));\n  }', card)
        self.assertIn("if (controls) {\n    card.addEventListener('contextmenu'", card)


class PhoneLayoutTests(unittest.TestCase):
    def test_the_app_filter_chips_wrap_on_a_phone(self):
        # UP-14: a side-scrolling row with a hidden scrollbar cut "Management" off.
        css = (ROOT / 'dashboard/style.css').read_text()
        phone = css[css.index('@media (max-width: 700px)') :]
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


@unittest.skipUnless(NODE, 'node is not installed')
class CatCompanionTests(unittest.TestCase):
    """The cat theme: the cat shows the server's state in a fixed order and never decides
    it; each panel's cat follows that panel's own information."""

    def readings(self, **job):
        backup = {
            'id': 'app-backup',
            'name': 'Nightly app backup',
            'status': 'pass',
            'last_success': 1000.0,
            'timer': {'installed': True, 'active': True},
        }
        return status(
            backups=[{**backup, **job}],
            storage=[{'id': 'media_hdd', 'name': 'Media HDD', 'status': 'mounted', 'used_percent': 40}],
        )

    def test_a_real_problem_comes_first_then_old_readings_then_a_job_then_a_success(self):
        failed, running, calm = self.readings(status='fail'), self.readings(status='running'), self.readings()
        moods = page(
            ('companionMood', {'status': None, 'stale': False, 'done': False}),
            ('companionMood', {'status': failed, 'stale': True, 'done': True}),
            ('companionMood', {'status': calm, 'stale': True, 'done': True}),
            ('companionMood', {'status': running, 'stale': False, 'done': True}),
            ('companionMood', {'status': calm, 'stale': False, 'done': True}),
            ('companionMood', {'status': calm, 'stale': False, 'done': False}),
        )
        self.assertEqual(moods, ['curious', 'afraid', 'curious', 'playful', 'happy', 'resting'])

    def moods(self, readings, **now):
        return page(('panelMoods', {'status': readings, 'now': now}))[0]

    # The cat placement package (placement-map.json), by route: all eleven pages.
    PACKAGE = {
        'home': 'happy',
        'apps': 'curious',
        'backups': 'sleeping',
        'hardware': 'resting',
        'sensors': 'listening',
        'cooling': 'breezy',
        'processes': 'playful',
        'health': 'grooming',
        'updates': 'grooming',
        'attention': 'happy',
        'help': 'curious',
    }

    def test_each_page_has_its_own_cat_while_all_is_well(self):
        self.assertEqual(self.moods(self.readings()), self.PACKAGE)

    def test_each_page_names_one_cat_where_the_package_puts_it(self):
        """The owner's review of 0.47.1: Home showed the cats of the panels it shares with
        Your apps and Needs attention, grooming sat at the drives, and Updates and Help had
        none. Each page names one cat, on its own route, in the section the package says."""
        html = (ROOT / 'dashboard/index.html').read_text()
        named = dict(
            re.findall(r'data-cat="([a-z]+)">\s*<div>\s*<span class="eyebrow">[^<]*</span>\s*<h2>([^<]*)</h2>', html)
        )
        self.assertEqual(sorted(named), sorted(self.PACKAGE))
        self.assertEqual(named['health'], 'Checks &amp; monitoring')
        self.assertEqual(named['updates'], 'Updates')
        self.assertEqual(named['help'], 'Help when you need it')
        self.assertLessEqual(set(named), set(re.findall(r'data-route="([a-z]+)"', html)))
        app = (ROOT / 'dashboard/app.js').read_text()
        draw = app[app.index('function renderCats()') : app.index('/* ---------- apps ---------- */')]
        self.assertIn(
            "key === state.route || (state.route === 'home' && key === 'attention' && issues.length > 0)", draw
        )

    def test_needs_attention_is_worried_only_beside_real_issues(self):
        self.assertEqual(self.moods(self.readings())['attention'], 'happy')
        failed = self.moods(self.readings(status='fail'))
        self.assertEqual((failed['backups'], failed['attention']), ('afraid', 'afraid'))

    def test_a_page_keeps_its_cat_and_a_problem_outlines_its_panel(self):
        """The package: a page's cat is its identity, not a verdict. A problem marks the
        panel (and the issue says what is wrong); a reading that can't be fetched (fan
        control away, E21) keeps the breeze cat beside its error."""
        gone = self.readings()
        gone['storage'][0]['status'] = 'missing'
        self.assertEqual(self.moods(gone)['health'], 'grooming')
        self.assertTrue(page(('panelAlerts', gone))[0]['health'])
        unreachable = self.moods(self.readings(), cooling=True, processes=True)
        self.assertEqual((unreachable['cooling'], unreachable['processes']), ('breezy', 'playful'))
        # The backup cat plays while a job runs and is happy once it has finished.
        self.assertEqual(self.moods(self.readings(status='running'))['backups'], 'playful')
        self.assertEqual(self.moods(self.readings(), done=True)['backups'], 'happy')
        self.assertEqual(self.moods(self.readings(status='running'), stale=True)['backups'], 'sleeping')

    def test_a_missing_animation_tries_the_same_cats_other_size_before_its_still(self):
        app = (ROOT / 'dashboard/app.js').read_text()
        show = app[app.index('function showCat(') : app.index('function noteFinished(')]
        self.assertIn("[...new Set([file, 'cat-' + mood + '.webp', 'cat-' + mood + '.png'])]", show)
        self.assertIn('img.dataset.catRequest === file', show)
        for mood in set(self.PACKAGE.values()) | {'afraid'}:
            for name in (f'cat-{mood}.webp', f'cat-{mood}-small.webp', f'cat-{mood}.png'):
                self.assertTrue((ROOT / 'dashboard' / name).is_file(), name)

    def test_the_theme_switch_is_its_own_and_set_on_the_root(self):
        """The owner's review of 0.47.1: the switch did nothing in the live preview, whose
        host page keeps its own data-theme on the root. The switch uses a name of its own
        and sets the colour scheme on the root itself."""
        app = (ROOT / 'dashboard/app.js').read_text()
        self.assertIn('document.documentElement.dataset.nekoTheme = shown', app)
        self.assertIn('document.documentElement.style.colorScheme = shown', app)
        for name in ('dashboard/app.js', 'dashboard/style.css', 'dashboard/theme.css', 'dashboard/index.html'):
            self.assertNotIn('data-theme=', (ROOT / name).read_text().replace('data-neko-theme=', ''), name)

    def test_a_finished_job_is_celebrated_once_and_never_on_the_first_look(self):
        first, same, newer, again = (
            self.readings(last_success=1000.0),
            self.readings(last_success=1000.0),
            self.readings(last_success=2000.0),
            self.readings(last_success=2000.0),
        )
        self.assertEqual(page(('newlyFinished', [first, same, newer, again])), [[None, None, 'app-backup', None]])

    def test_a_first_good_copy_is_celebrated_and_jobs_that_are_not_backups_never_are(self):
        """The review of 0.47.0: a backup's very first good copy was never celebrated, and the
        daily update check finishing was celebrated as a backup ("its last good copy")."""
        never, first = self.readings(last_success=None), self.readings(last_success=1000.0)
        self.assertEqual(page(('newlyFinished', [never, first])), [[None, 'app-backup']])
        check = lambda at: status(
            backups=[{'id': 'update_check', 'name': 'Daily update check', 'status': 'pass', 'last_success': at}]
        )  # noqa: E731
        self.assertEqual(page(('newlyFinished', [check(1000.0), check(2000.0)])), [[None, None]])

    def test_an_apps_name_and_ports_are_the_ones_the_server_sends(self):
        """The review of 0.47.1: the page kept its own names, descriptions and ports for
        the apps (worded differently from setup's); they come from each app's row now."""
        row = {'id': 'jellyfin', 'name': 'Jellyfin', 'description': 'Movies & TV shows', 'port': 8096, 'tailnet': 8443}
        spec, odd, shortcut = page(
            ('appSpec', {'id': 'jellyfin', 'app': row}),
            ('appSpec', {'id': 'jellyfin', 'app': {**row, 'port': 99999, 'tailnet': 'x'}}),
            ('appSpec', {'id': 'youtube'}),
        )
        self.assertEqual(
            {k: spec[k] for k in ('name', 'description', 'port', 'tailnet', 'icon')},
            {'name': 'Jellyfin', 'description': 'Movies & TV shows', 'port': 8096, 'tailnet': 8443, 'icon': '▶'},
        )
        self.assertEqual((odd['port'], odd['tailnet']), (None, None))
        self.assertEqual(shortcut['name'], 'YouTube')

    def test_apps_held_by_the_nas_check_are_one_alert_not_one_per_app(self):
        """The review of 0.47.0: the collector said the apps were held, and nothing showed
        it. Then the 0.47.1 preview: each held app was its own "Stopped, try opening it"
        item, listed before the cause. The collector decides which apps are held (`held`)
        and says the hold once; the page shows that, and no item per held app."""
        held = self.readings()
        held['storage'][0].update(network=True, held_by_nas_watch=True)
        held['applications'] = [
            {'id': 'jellyfin', 'name': 'Jellyfin', 'state': 'exited', 'held': True},
            {'id': 'seerr', 'name': 'Seerr', 'state': 'exited', 'held': False},
        ]
        held['alerts'] = [
            {'id': 'nas:hold', 'level': 'critical', 'title': 'The apps are held', 'detail': 'Stopped: Jellyfin.'}
        ]
        issues = page(('collectIssues', held))[0]
        self.assertEqual([i['id'] for i in issues], ['nas:hold', 'app:seerr'])
        self.assertTrue(page(('panelAlerts', held))[0]['health'])
        # The page no longer works the hold out itself: without the collector's word, a
        # stopped app is an item of its own.
        held['applications'][0]['held'] = False
        held['alerts'] = []
        self.assertIn('app:jellyfin', [i['id'] for i in page(('collectIssues', held))[0]])

    def test_each_cat_has_its_animation_and_the_page_serves_every_one(self):
        files = page(
            ('catFile', {'mood': 'afraid', 'small': False}),
            ('catFile', {'mood': 'breezy', 'small': True}),
        )
        self.assertEqual(files, ['cat-afraid.webp', 'cat-breezy-small.webp'])
        from modules import dashboard

        for mood in dashboard.CAT_PAGES:
            self.assertIn(f'cat-{mood}-small.webp', dashboard.ASSETS)
            self.assertIn(f'cat-{mood}.png', dashboard.ASSETS)


if __name__ == '__main__':
    unittest.main()
