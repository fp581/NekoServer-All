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
            return APP[start:at + 1]
    raise ValueError(name)


def run(code):
    prelude = 'const nowSeconds = () => Date.now() / 1000;\nconst ago = at => String(at);\n'
    return json.loads(subprocess.run([NODE, '-e', prelude + code], capture_output=True, text=True, check=True).stdout)


@unittest.skipUnless(NODE, 'node is not installed')
class ChecksumVerdictTests(unittest.TestCase):
    def verdicts(self, row):
        code = pieces(consts=('numeric', 'count', 'text', 'INTEGRITY_MAX_AGE'), functions=(
            'firstOf', 'plural', 'integrityBad', 'checkDated', 'checkOverdue', 'filesIntact', 'integrityResult'))
        return run(code + f'\nconst row = {json.dumps(row)};\nconsole.log(JSON.stringify([filesIntact([row]), integrityResult(row).label]));')

    def test_a_quick_run_alone_is_not_shown_as_matching(self):
        now = time.time()
        row = {'id': 'usb_photos', 'at': now, 'full_at': None, 'damaged': 0, 'both_damaged': 0, 'disagree': 0,
               'missing': 0, 'unverified': 0}
        intact, label = self.verdicts(row)
        self.assertEqual(intact[0], 'unknown')
        self.assertNotEqual(label, 'Matches')

    def test_matches_rests_on_the_full_check_date(self):
        now = time.time()
        row = {'id': 'archive', 'at': now, 'full_at': now - 3 * 86400, 'damaged': 0, 'both_damaged': 0,
               'disagree': 0, 'missing': 0, 'unverified': 0}
        self.assertEqual(self.verdicts(row), [['pass', 'Checksums match'], 'Matches'])
        # A fresh quick run does not hide that the last full check is two months old.
        row['full_at'] = now - 60 * 86400
        self.assertEqual(self.verdicts(row)[0][0], 'stale')


@unittest.skipUnless(NODE, 'node is not installed')
class ServerCheckIssueTests(unittest.TestCase):
    def issues(self, acceptance):
        code = pieces(consts=('ACCEPTANCE_RECHECK',), functions=('acceptanceIssue',))
        return run(code + f'\nconst rows = []; acceptanceIssue({json.dumps(acceptance)}, (id, title) => rows.push(title));'
                          '\nconsole.log(JSON.stringify(rows));')

    def test_a_complete_check_a_day_old_needs_nothing(self):
        self.assertEqual(self.issues({'status': 'stale', 'generated_at': time.time() - 2 * 86400}), [])
        self.assertEqual(self.issues({'status': 'complete', 'generated_at': time.time()}), [])

    def test_a_recheck_is_suggested_after_a_month(self):
        self.assertEqual(self.issues({'status': 'stale', 'generated_at': time.time() - 40 * 86400}),
                         ['Time to recheck your server'])

    def test_an_unfinished_check_still_asks_to_finish(self):
        for status in ('pending', 'failed', 'unknown', 'outdated'):
            self.assertEqual(self.issues({'status': status}), ['Finish checking your server'], status)


@unittest.skipUnless(NODE, 'node is not installed')
class FanSliderTests(unittest.TestCase):
    def test_a_fixed_speed_is_never_shown_below_the_floor(self):
        code = pieces(consts=('numeric', 'list', 'values', 'object', 'text'), objects=('PRESETS',),
                      functions=('firstOf', 'draftFrom'))
        result = run(code + "\nconsole.log(JSON.stringify(draftFrom({mode: 'fixed', fixed: 5}, []).fixed));")
        self.assertEqual(result, 20)
        # The slider starts at 20 % or "Never slower than", whichever is higher
        # (test_dashboard_page checks fixedFloor and raiseFixed themselves).
        self.assertIn("rangeField('Speed', fixedFloor(card.draft), 100,", APP)


if __name__ == '__main__':
    unittest.main()
