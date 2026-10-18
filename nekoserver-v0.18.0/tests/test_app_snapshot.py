"""Execute native snapshot worker with Docker/mount fixtures and real files."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from modules import app_backup

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'runtime/bin/nekoserver-runtime'
JOURNAL = Path('/etc/nekoserver-backups/app-resume.json')
spec = importlib.util.spec_from_file_location('app_recovery', ROOT / 'app-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() != 0 or JOURNAL.exists():
            self.skipTest('Isolated root fixture with no existing resume journal required')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: JOURNAL.unlink(missing_ok=True))
        self.root = Path(self.tmp.name)
        self.data, self.media = self.root / 'ssd', self.root / 'hdd'
        self.data.mkdir()
        self.media.mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({
            'data_mount': str(self.data), 'media_mount': str(self.media),
            'storage': {'data_ssd': {'uuid': 'SSD', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'HDD', 'fstype': 'ext4'}},
            'media': {'services': ['sonarr', 'seerr']},
        }))
        self.state = self.root / 'docker.json'
        containers = {}
        for index, name in enumerate(('sonarr', 'seerr')):
            folder = self.data / 'appdata' / name
            folder.mkdir(parents=True)
            (folder / 'settings.json').write_text('PRIVATE-API-KEY-' + name)
            folder.chmod(0o750)
            compose = self.data / 'compose' / name
            compose.mkdir(parents=True)
            (compose / 'compose.yaml').write_text('fixture image')
            cid = str(index + 1) * 64
            containers[cid] = {
                'Id': cid, 'Image': 'sha256:' + 'a' * 64,
                'Config': {'Labels': {'com.docker.compose.project': 'neko-' + name, 'com.docker.compose.service': name}},
                'State': {'Running': index == 0, 'Paused': False, 'Restarting': False},
                'Mounts': [{'Type': 'bind', 'Source': str(folder), 'Destination': '/config' if name == 'sonarr' else '/app/config', 'RW': True}],
            }
        self.state.write_text(json.dumps(containers))
        commands = self.root / 'commands'
        commands.mkdir()
        script = '''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:];name=Path(sys.argv[0]).name
if name=='findmnt':
 print(('WRONG' if os.environ.get('WRONG') else 'SSD')+' ext4' if args[-1]==os.environ['SSD'] else 'HDD ext4');sys.exit(0)
assert args[:2]==['--host','unix:///var/run/docker.sock']
args=args[2:];path=Path(os.environ['STATE']);state=json.loads(path.read_text())
if args[0]=='ps':
 service=next((a.split('=')[-1] for a in args if a.startswith('label=com.docker.compose.service=')),None)
 print('\\n'.join(k for k,v in state.items() if service is None or v['Config']['Labels']['com.docker.compose.service']==service))
elif args[0]=='inspect':print(json.dumps([state[args[1]]]))
elif args[0] in ('stop','start'):
 action=args[0];cid=args[-1]
 with open(os.environ['LOG'],'a') as output:output.write(action+' '+cid+'\\n')
 if os.environ.get('FAIL')==action:sys.exit(1)
 state[cid]['State']['Running']=action=='start'
 if action=='stop' and os.environ.get('KILLED'):state[cid]['State']['ExitCode']=137
 path.write_text(json.dumps(state))
else:sys.exit(1)
'''
        for name in ('docker', 'findmnt'):
            path = commands / name
            path.write_text(script)
            path.chmod(0o755)
        self.log = self.root / 'actions.log'
        self.env = {**os.environ, 'PATH': str(commands) + ':/usr/bin:/bin', 'SSD': str(self.data), 'STATE': str(self.state), 'LOG': str(self.log)}

    def native(self, command='app-snapshot'):
        return subprocess.run([str(BIN), command, '--config', str(self.config)], env=self.env, capture_output=True, text=True, timeout=30)

    def snapshots(self):
        return list((self.media / 'backups/media-applications').glob('snapshot-*'))

    def test_complete_snapshot_restore_and_original_running_state(self):
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.snapshots()[0]
        manifest = recovery.verify(snapshot)
        self.assertEqual(len(manifest['containers']), 2)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertEqual((snapshot / 'payload/appdata/sonarr/settings.json').stat().st_mode & 0o777, 0o600)
        restored = recovery.restore(snapshot, self.root / 'restored')
        self.assertEqual((restored / 'appdata/sonarr/settings.json').read_text(), 'PRIVATE-API-KEY-sonarr')
        report = json.loads((restored / 'restore-report.json').read_text())
        self.assertTrue(report['checksums_verified'])
        self.assertFalse(report['application_startup_verified'])
        actions = self.log.read_text().splitlines()
        self.assertEqual(actions, ['stop ' + '1' * 64, 'start ' + '1' * 64])
        self.assertFalse(JOURNAL.exists())
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)
        self.assertEqual(json.loads((self.data / 'reports/jobs/applications.json').read_text())['status'], 'pass')

    def test_reruns_add_new_snapshots_without_overwriting(self):
        self.assertEqual(self.native().returncode, 0)
        first = self.snapshots()[0]
        before = (first / 'manifest.json').read_bytes()
        self.assertEqual(self.native().returncode, 0)
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual((first / 'manifest.json').read_bytes(), before)

    def test_wrong_drive_prevents_stop_and_report_writes(self):
        self.env['WRONG'] = '1'
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.data / 'reports').exists())

    def test_source_symlink_fails_then_restores_running_apps(self):
        (self.data / 'appdata/sonarr/link').symlink_to('/etc/passwd')
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.snapshots())
        self.assertTrue(json.loads(self.state.read_text())['1' * 64]['State']['Running'])
        self.assertFalse(JOURNAL.exists())

    def test_source_hardlink_is_refused(self):
        path = self.data / 'appdata/sonarr/settings.json'
        os.link(path, path.with_name('hardlink'))
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.snapshots())

    def test_extra_writable_mount_refused_before_stop(self):
        value = json.loads(self.state.read_text())
        value['1' * 64]['Mounts'].append({'Type': 'volume', 'Source': '/var/lib/docker/extra', 'Destination': '/extra', 'RW': True})
        self.state.write_text(json.dumps(value))
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_forced_kill_is_not_treated_as_clean_shutdown(self):
        self.env['KILLED'] = '1'
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.snapshots())
        self.assertTrue(json.loads(self.state.read_text())['1' * 64]['State']['Running'])

    def test_nonprivate_backup_destination_refused(self):
        folder = self.media / 'backups/media-applications'
        folder.mkdir(parents=True)
        folder.chmod(0o755)
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.snapshots())
        self.assertTrue(json.loads(self.state.read_text())['1' * 64]['State']['Running'])

    def test_other_container_writer_refused_before_stop(self):
        value = json.loads(self.state.read_text())
        value['3' * 64] = {
            'Id': '3' * 64, 'Config': {'Labels': {}},
            'Mounts': [{'Source': str(self.data / 'appdata'), 'RW': True}],
        }
        self.state.write_text(json.dumps(value))
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_stop_failure_does_not_publish_success(self):
        self.env['FAIL'] = 'stop'
        self.assertNotEqual(self.native().returncode, 0)
        self.assertFalse(self.snapshots())
        self.assertFalse(JOURNAL.exists())
        self.assertEqual(json.loads((self.data / 'reports/jobs/applications.json').read_text())['status'], 'fail')

    def test_restart_failure_keeps_journal_and_resume_recovers(self):
        self.env['FAIL'] = 'start'
        self.assertNotEqual(self.native().returncode, 0)
        self.assertTrue(JOURNAL.exists())
        del self.env['FAIL']
        self.assertEqual(self.native('app-resume').returncode, 0)
        self.assertFalse(JOURNAL.exists())
        self.assertTrue(json.loads(self.state.read_text())['1' * 64]['State']['Running'])

    def test_resume_marks_interrupted_job_failed_preserves_last_success(self):
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.write_text(json.dumps({'format': 'neko-app-resume-v1', 'containers': [{'service': 'sonarr', 'id': '1' * 64}]}))
        folder = self.data / 'reports/jobs'
        folder.mkdir(parents=True)
        (folder / 'applications.json').write_text(json.dumps({'status': 'running', 'last_success': 123}))
        self.assertEqual(self.native('app-resume').returncode, 0)
        row = json.loads((folder / 'applications.json').read_text())
        self.assertEqual(row['status'], 'fail')
        self.assertEqual(row['last_success'], 123)

    def test_restore_refuses_existing_directory_and_corruption(self):
        self.assertEqual(self.native().returncode, 0)
        snapshot = self.snapshots()[0]
        with self.assertRaises(RuntimeError):
            recovery.restore(snapshot, self.data)
        (snapshot / 'payload/appdata/sonarr/settings.json').write_text('corrupt')
        with self.assertRaises(RuntimeError):
            recovery.restore(snapshot, self.root / 'restore')
        self.assertFalse((self.root / 'restore').exists())

    def test_restore_rejects_traversal_and_unlisted_files(self):
        self.assertEqual(self.native().returncode, 0)
        snapshot = self.snapshots()[0]
        (snapshot / 'payload/extra').write_text('unexpected')
        with self.assertRaises(RuntimeError):
            recovery.verify(snapshot)
        for value in ('../escape', '/absolute', '.', 'appdata/../escape'):
            with self.assertRaises(RuntimeError):
                recovery.relative(value)

    def test_restore_explicit_original_owner_and_modes(self):
        self.assertEqual(self.native().returncode, 0)
        with patch.object(os, 'chown') as owner:
            folder = recovery.restore(self.snapshots()[0], self.root / 'original-modes', True)
        self.assertTrue(owner.called)
        self.assertEqual((folder / 'appdata/sonarr').stat().st_mode & 0o777, 0o750)


class DeploymentTests(unittest.TestCase):
    def test_installer_writes_private_guard_and_enables_recovery_and_timer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = json.loads((ROOT / 'config.json').read_text())
            config.update(data_mount=str(root / 'ssd'), media_mount=str(root / 'hdd'))
            config['storage'] = {'data_ssd': {'uuid': 'SSD', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'HDD', 'fstype': 'ext4'}}
            real_path = Path
            def mapped(value, *args):
                path = real_path(value, *args)
                return root / str(path).lstrip('/') if str(path).startswith('/etc/') else path
            real_safe = app_backup.safe_path
            def safe(value):
                return real_safe(mapped(value))
            with patch.object(app_backup, 'Path', side_effect=mapped), patch.object(app_backup, 'safe_path', side_effect=safe), patch.object(app_backup, 'verify_storage') as verify, patch.object(app_backup.subprocess, 'run') as command:
                app_backup.run(SimpleNamespace(config=config, apply=True, log=lambda _: None))
            verify.assert_called_once_with(config)
            guard = root / 'etc/nekoserver-backups/applications.json'
            self.assertEqual(guard.stat().st_mode & 0o777, 0o600)
            saved = json.loads(guard.read_text())
            self.assertEqual(set(saved), {'data_mount', 'media_mount', 'storage', 'media'})
            self.assertEqual(saved['media']['services'], config['media']['services'])
            units = root / 'etc/systemd/system'
            self.assertEqual(len(list(units.glob('*'))), 3)
            self.assertIn(['systemctl', 'enable', '--now', 'nekoserver-app-resume.service'], [call.args[0] for call in command.call_args_list])
            self.assertIn(['systemctl', 'enable', '--now', 'nekoserver-app-backup.timer'], [call.args[0] for call in command.call_args_list])

    def test_python_backend_is_refused_without_writes(self):
        config = {'runtime': {'backend': 'python'}}
        with patch.object(app_backup, 'atomic_json') as write:
            with self.assertRaises(RuntimeError):
                app_backup.run(SimpleNamespace(config=config, apply=True, log=lambda _: None))
        write.assert_not_called()

    def test_calendar_and_recovery_hooks(self):
        cfg = {'data_mount': '/srv/neko', 'media_mount': '/mnt/media', 'app_backup': {'time': '04:15'}}
        units = app_backup.definitions(cfg, '/srv/neko/runtime', '/etc/neko/config.json')
        self.assertIn('ExecStopPost=/srv/neko/runtime app-resume', units['nekoserver-app-backup.service'])
        self.assertIn('After=docker.service', units['nekoserver-app-resume.service'])
        self.assertIn('Persistent=true', units['nekoserver-app-backup.timer'])
        for value in ('25:00', '04:60', '04:15\nExecStart=bad', None):
            cfg['app_backup']['time'] = value
            with self.assertRaises(RuntimeError):
                app_backup.definitions(cfg, '/runtime', '/config')
