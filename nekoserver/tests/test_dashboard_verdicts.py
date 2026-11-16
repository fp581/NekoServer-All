"""The dashboard's own verdicts (dashboard/app.js), run in node: a checksum result rests
on the monthly full check, a complete server check stays complete when it is a day old,
and the fan speed slider never goes below the control service's floor."""

import json
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
APP = (ROOT / 'dashboard/app.js').read_text() if (ROOT / 'dashboard/app.js').exists() else ''


def pieces(consts=(), objects=(), functions=()):
    found = [re.search(rf'^const {name} = .*$', APP, re.MULTILINE).group(0) for name in consts]
    found += [re.search(rf'^const {name} = \{{.*?\}};', APP, re.MULTILINE | re.DOTALL).group(0) for name in objects]
    found += [function(name) for name in functions]
    return '\n'.join(found)


def function(name):
    """The whole function, by counting braces from its start."""
    start = re.search(rf'^function {name}\(', APP, re.MULTILINE).start()
    depth = 0
    for at in range(APP.index('{', start), len(APP)):
        depth += {'{': 1, '}': -1}.get(APP[at], 0)
        if depth == 0:
            return APP[start : at + 1]
    raise ValueError(name)


def run(code):
    prelude = 'const nowSeconds = () => Date.now() / 1000;\nconst ago = at => String(at);\n'
    return json.loads(subprocess.run([NODE, '-e', prelude + code], capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(NODE, 'node is not installed')
class ChecksumVerdictTests(unittest.TestCase):
    def verdicts(self, row):
        code = pieces(
            consts=('numeric', 'count', 'text', 'INTEGRITY_MAX_AGE'),
            functions=(
                'firstOf',
                'plural',
                'integrityBad',
                'checkDated',
                'checkOverdue',
                'filesIntact',
                'localTime',
                'usbCheckWhen',
                'integrityResult',
            ),
        )
        return run(
            code
            + f'\nconst row = {json.dumps(row)};\nconsole.log(JSON.stringify([filesIntact([row]), integrityResult(row).label]));'
        )

    def test_a_quick_run_alone_is_not_shown_as_matching(self):
        now = time.time()
        row = {
            'id': 'usb_photos',
            'at': now,
            'full_at': None,
            'damaged': 0,
            'both_damaged': 0,
            'disagree': 0,
            'missing': 0,
            'unverified': 0,
        }
        intact, label = self.verdicts(row)
        self.assertEqual(intact[0], 'unknown')
        self.assertNotEqual(label, 'Matches')

    def test_matches_rests_on_the_full_check_date(self):
        now = time.time()
        row = {
            'id': 'archive',
            'at': now,
            'full_at': now - 3 * 86400,
            'damaged': 0,
            'both_damaged': 0,
            'disagree': 0,
            'missing': 0,
            'unverified': 0,
        }
        self.assertEqual(self.verdicts(row), [['pass', 'Checksums match'], 'Matches'])
        # A fresh quick run does not hide that the last full check is two months old.
        row['full_at'] = now - 60 * 86400
        self.assertEqual(self.verdicts(row)[0][0], 'stale')

    def words(self, row, monthly_at):
        code = pieces(
            consts=('numeric', 'count', 'text', 'INTEGRITY_MAX_AGE'),
            functions=(
                'firstOf',
                'plural',
                'integrityBad',
                'checkDated',
                'checkOverdue',
                'localTime',
                'usbCheckWhen',
                'integrityResult',
            ),
        )
        return run(
            code + f'\nconsole.log(JSON.stringify(integrityResult({json.dumps(row)}, {json.dumps(monthly_at)}).words));'
        )

    def test_a_usb_drive_plugged_in_only_for_the_weekly_copy_is_told_when_to_plug_it_in(self):
        """Deep check 798: a USB drive plugged in only for the weekly copy is never there
        for the monthly full check, so its row is overdue after 35 days. It said "leave the
        drive plugged in on the night of the 1st", read as the evening of the 1st, while
        the check runs early that morning (02:30 by default): it names the next run now."""
        now = time.time()
        row = {'id': 'usb_photos', 'at': now, 'full_at': now - 40 * 86400, 'unverified': 0}
        at = now + 5 * 86400
        said = self.words(row, at)
        self.assertNotIn('night of the 1st', said)
        expected = run(pieces(functions=('localTime',)) + f'\nconsole.log(JSON.stringify(localTime({at})));')
        self.assertIn('have the drive plugged in when the monthly check runs: next on ' + expected + '.', said)
        # No timer to read: the day and part of the day are said.
        self.assertIn('early on the 1st of each month.', self.words(row, None))
        # Never checked yet: the same advice. The HDD's own copy needs none.
        self.assertIn('next on', self.words(dict(row, full_at=None), at))
        self.assertNotIn('USB', self.words(dict(row, id='archive'), at))


@unittest.skipUnless(NODE, 'node is not installed')
class ServerCheckIssueTests(unittest.TestCase):
    def issues(self, acceptance):
        code = pieces(consts=('ACCEPTANCE_RECHECK', 'CHECK_TO_DO'), functions=('acceptanceIssue',))
        return run(
            code + f'\nconst rows = []; acceptanceIssue({json.dumps(acceptance)}, (id, title) => rows.push(title));'
            '\nconsole.log(JSON.stringify(rows));'
        )

    def test_a_complete_check_a_day_old_needs_nothing(self):
        self.assertEqual(self.issues({'status': 'stale', 'generated_at': time.time() - 2 * 86400}), [])
        self.assertEqual(self.issues({'status': 'complete', 'generated_at': time.time()}), [])

    def test_a_recheck_is_suggested_after_a_month(self):
        self.assertEqual(
            self.issues({'status': 'stale', 'generated_at': time.time() - 40 * 86400}), ['Time to recheck your server']
        )

    def test_a_failed_check_asks_to_finish_one_still_to_do_is_getting_started(self):
        # The user's choice after the VM installs: a check not done yet is Home's "Getting
        # started" note (gettingStarted), not an item to review.
        self.assertEqual(self.issues({'status': 'failed'}), ['Finish checking your server'])
        for status in ('pending', 'unknown', 'outdated'):
            self.assertEqual(self.issues({'status': status}), [], status)


@unittest.skipUnless(NODE, 'node is not installed')
class FanSliderTests(unittest.TestCase):
    def test_a_fixed_speed_is_never_shown_below_the_floor(self):
        code = pieces(
            consts=('numeric', 'list', 'values', 'object', 'text'),
            objects=('PRESETS',),
            functions=('firstOf', 'draftFrom'),
        )
        result = run(code + "\nconsole.log(JSON.stringify(draftFrom({mode: 'fixed', fixed: 5}, []).fixed));")
        self.assertEqual(result, 20)
        # The slider starts at 20 % or "Never slower than", whichever is higher
        # (test_dashboard_page checks fixedFloor and raiseFixed themselves).
        self.assertIn("rangeField(\n    'Speed',\n    fixedFloor(card.draft),\n    100,", APP)


if __name__ == '__main__':
    unittest.main()
