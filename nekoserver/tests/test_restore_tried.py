"""The dashboard's RESTORE TRIED answer needs a witnessed restore test for every kind of
data the server backs up (photos, Nextcloud files, music, app state, host settings),
read from each backup row's own list (collect.rs restore_tests, the one place that says
which restore tests each backup needs)."""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NODE = shutil.which('node')


def restore_tried(backups, passed):
    """Run the dashboard's own restoreTried on a status with these rows."""
    app = (ROOT / 'dashboard/app.js').read_text()
    helpers = '\n'.join(re.search(rf'^const {name} = .*$', app, re.MULTILINE).group(0) for name in ('list', 'values'))
    names = re.search(r'^const RESTORE_NAMES = \{.*?\};', app, re.MULTILINE | re.DOTALL).group(0)
    func = re.search(r'^function restoreTried\(s\) \{.*?^\}', app, re.MULTILINE | re.DOTALL).group(0)
    status = {
        'backups': backups,
        'acceptance': {'status': 'pending', 'manual_checks': [{'id': i, 'status': 'pass'} for i in passed]},
    }
    script = f'{helpers}\n{names}\n{func}\nconsole.log(JSON.stringify(restoreTried({json.dumps(status)})));'
    return json.loads(subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True).stdout)


def row(key, restore):
    """A backup row as the collector writes it (collect.rs backups)."""
    return {'id': key, 'status': 'pass', 'restore': restore}


def rust_restore_tests():
    """Every restore test the collector can ask for (collect.rs restore_tests)."""
    source = (ROOT / 'runtime/src/collect.rs').read_text()
    start = source.index('fn restore_tests(')
    return set(re.findall(r'"([a-z_]+_restore)"', source[start : source.index('\n}\n', start)]))


@unittest.skipUnless(NODE, 'node is not installed')
class RestoreTriedTests(unittest.TestCase):
    def test_nextcloud_only_asks_for_a_nextcloud_restore_not_photos(self):
        backups = [row('external', ['cloud_restore'])]
        result = restore_tried(backups, [])
        self.assertIn('Nextcloud files', result['words'])
        self.assertNotIn('photos', result['words'])
        self.assertEqual(restore_tried(backups, ['cloud_restore'])['result'], ['pass', 'Yes'])

    def test_music_backup_needs_its_own_restore_test(self):
        backups = [row('music', ['music_restore'])]
        self.assertEqual(restore_tried(backups, [])['result'][0], 'needs_setup')
        self.assertIn('music', restore_tried(backups, [])['words'])

    def test_photos_and_nextcloud_on_one_usb_need_both(self):
        backups = [
            row('external', ['independent_restore', 'cloud_restore']),
            row('archive', ['independent_restore']),
            row('database', ['independent_restore']),
        ]
        partly = restore_tried(backups, ['independent_restore'])
        self.assertEqual(partly['result'], ['warning', '1 of 2'])
        self.assertIn('Not yet: Nextcloud files', partly['words'])
        self.assertEqual(restore_tried(backups, ['independent_restore', 'cloud_restore'])['result'], ['pass', 'Yes'])

    def test_every_restore_test_is_on_the_checklist(self):
        import acceptance

        modules = {'photos', 'photo_backup', 'music_backup', 'app_backup', 'host_backup', 'nextcloud'}
        listed = {
            row['id']
            for row in acceptance.manual_tests(
                modules, {'photos': {'deploy_immich': True}, 'nextcloud': {'enabled': True}}
            )
        }
        wanted = rust_restore_tests()
        self.assertGreaterEqual(len(wanted), 5)
        self.assertLessEqual(wanted, listed)


@unittest.skipUnless(NODE, 'node is not installed')
class BackupIssuesTests(unittest.TestCase):
    def issues(self, backups, units=False):
        app = (ROOT / 'dashboard/app.js').read_text()
        pieces = [
            re.search(rf'^const {name} = .*$', app, re.MULTILINE).group(0)
            for name in ('numeric', 'list', 'values', 'object', 'text', 'severity', 'COPIES_NOTHING')
        ]
        for name in ('JOB_UNITS', 'LABELS'):
            pieces.append(re.search(rf'^const {name} = \{{.*?\}};', app, re.MULTILINE | re.DOTALL).group(0))
        for name in ('backupIssues', 'jobUnit', 'onDemand'):
            pieces.append(re.search(rf'^function {name}\(.*?^\}}', app, re.MULTILINE | re.DOTALL).group(0))
        script = (
            f'const units = {json.dumps(units)};\n'
            + '\n'.join(pieces)
            + f'''
const jobLabel = () => '', guideFor = () => '';
const rows = []; backupIssues({{backups: {json.dumps(backups)}}}, (id, title, d, g, l, unit) => rows.push(units ? [id, unit] : id));
console.log(JSON.stringify(rows));'''
        )
        return json.loads(subprocess.run([NODE, '-e', script], capture_output=True, text=True, check=True).stdout)

    def test_a_switched_off_backup_is_not_an_issue(self):
        rows = [
            {
                'id': 'archive',
                'name': 'Photo archive',
                'status': 'disabled',
                'last_success': 1.0,
                'timer': {'installed': False, 'active': False},
            }
        ]
        self.assertEqual(self.issues(rows), [])
        rows[0]['status'] = 'pass'
        self.assertEqual(self.issues(rows), ['timer:archive'])

    def test_the_log_commands_name_the_service_that_ran(self):
        """0.45.8: with weekly photo backups the archive job runs as the database copy's
        service, and an upgrade as nekoserver-upgrade@<release>; the page named the
        usual service (or none), so its log commands showed the wrong logs."""
        failed = {'status': 'fail', 'timer': {}, 'last_success': 1.0}
        rows = [
            {**failed, 'id': 'archive', 'unit': 'nekoserver-immich-db-backup.service'},
            {**failed, 'id': 'upgrade', 'unit': 'nekoserver-upgrade@v0.45.8Beta.service'},
            {**failed, 'id': 'updates'},
            {**failed, 'id': 'host', 'unit': 'ssh.service; reboot'},
        ]
        self.assertEqual(
            [row for row in self.issues(rows, units=True) if row[0].startswith('job:')],
            [
                ['job:archive', 'nekoserver-immich-db-backup.service'],
                ['job:upgrade', 'nekoserver-upgrade@v0.45.8Beta.service'],
                ['job:updates', 'nekoserver-updates.service'],
                ['job:host', 'nekoserver-host-backup.service'],
            ],
        )


if __name__ == '__main__':
    unittest.main()
