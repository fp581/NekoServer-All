"""The dashboard's RESTORE TRIED answer needs a witnessed restore test for every kind of
data the server backs up (photos, Nextcloud files, music, app state, host settings),
read from each backup row's own list (collect.rs and status_checks restore_tests)."""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import status_checks  # noqa: E402

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


def rows(config):
    return [
        {
            'id': key,
            'status': 'disabled' if status_checks.retired(config, key) else 'pass',
            'restore': [] if status_checks.retired(config, key) else status_checks.restore_tests(config, key),
        }
        for key in status_checks.JOBS
    ]


@unittest.skipUnless(NODE, 'node is not installed')
class RestoreTriedTests(unittest.TestCase):
    def test_nextcloud_only_asks_for_a_nextcloud_restore_not_photos(self):
        config = {
            'photos': {'deploy_immich': False},
            'nextcloud': {'enabled': True},
            'music': {'backup_enabled': False},
        }
        backups = [r for r in rows(config) if r['id'] == 'external']
        result = restore_tried(backups, [])
        self.assertIn('Nextcloud files', result['words'])
        self.assertNotIn('photos', result['words'])
        self.assertEqual(restore_tried(backups, ['cloud_restore'])['result'], ['pass', 'Yes'])

    def test_music_backup_needs_its_own_restore_test(self):
        backups = [r for r in rows({'music': {}}) if r['id'] == 'music']
        self.assertEqual(restore_tried(backups, [])['result'][0], 'needs_setup')
        self.assertIn('music', restore_tried(backups, [])['words'])

    def test_photos_and_nextcloud_on_one_usb_need_both(self):
        config = {'photos': {'deploy_immich': True}, 'nextcloud': {'enabled': True}}
        backups = [r for r in rows(config) if r['id'] in ('external', 'archive', 'database')]
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
        wanted = {
            t for r in rows({'photos': {'deploy_immich': True}, 'nextcloud': {'enabled': True}}) for t in r['restore']
        }
        self.assertLessEqual(wanted, listed)


@unittest.skipUnless(NODE, 'node is not installed')
class BackupIssuesTests(unittest.TestCase):
    def issues(self, backups):
        app = (ROOT / 'dashboard/app.js').read_text()
        pieces = [
            re.search(rf'^const {name} = .*$', app, re.MULTILINE).group(0)
            for name in ('numeric', 'list', 'values', 'object', 'text', 'severity')
        ]
        for name in ('JOB_UNITS', 'LABELS'):
            pieces.append(re.search(rf'^const {name} = \{{.*?\}};', app, re.MULTILINE | re.DOTALL).group(0))
        pieces.append(re.search(r'^function backupIssues\(s, add\) \{.*?^\}', app, re.MULTILINE | re.DOTALL).group(0))
        script = (
            '\n'.join(pieces)
            + f'''
const rows = []; backupIssues({{backups: {json.dumps(backups)}}}, (id, title) => rows.push(id));
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


if __name__ == '__main__':
    unittest.main()
