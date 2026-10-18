"""Native host snapshots and isolated restore, with filesystem/process fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modules import host_backup
import dashboard_collect

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT/'runtime/bin/nekoserver-runtime'
JOURNAL = Path('/etc/nekoserver-backups/host-resume.json')
spec = importlib.util.spec_from_file_location('host_recovery', ROOT/'host-recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class HostTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() != 0 or JOURNAL.exists(): self.skipTest('Isolated root fixture required')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: JOURNAL.unlink(missing_ok=True))
        self.root = Path(self.tmp.name); self.data = self.root/'ssd'; self.media = self.root/'hdd'; self.source = self.root/'user'
        for p in (self.data, self.media, self.source): p.mkdir()
        (self.source/'save.dat').write_bytes(b'PRIVATE-SAVE')
        (self.source/'prefix-link').symlink_to('/etc/shadow')
        self.config = self.root/'config.json'
        self.value = {'data_mount':str(self.data),'media_mount':str(self.media),'storage':{'data_ssd':{'uuid':'SSD','fstype':'ext4'},'media_hdd':{'uuid':'HDD','fstype':'ext4'}},'host_backup':{'sources':[{'id':'source0','path':str(self.source),'optional':False}],'samba':False,'steam_uid':1000,'minimum_free_bytes':1024**3}}
        self.save_config()
        commands = self.root/'commands'; commands.mkdir(); self.state = self.root/'samba-state'; self.state.write_text('active'); self.log = self.root/'actions'
        script = '''#!/usr/bin/python3
import os,sys
from pathlib import Path
args=sys.argv[1:];name=Path(sys.argv[0]).name
if name=='findmnt': print(('WRONG' if os.environ.get('WRONG') else 'SSD')+' ext4' if args[-1]==os.environ['SSD'] else 'HDD ext4');sys.exit(0)
if name=='pgrep':
 if os.environ.get('PROBE_ERROR'):sys.exit(2)
 if '-u' in args and os.environ.get('STEAM'):sys.exit(0)
 if '-u' not in args and os.environ.get('WRITER'):sys.exit(0)
 if '-u' not in args and os.environ.get('MUTATE'):
  Path(os.environ['SOURCE']+'/new-file').write_text('changed')
 sys.exit(1)
if name=='testparm':
 print(os.environ.get('BACKEND','tdbsam') if args[-1].endswith('passdb backend') else os.environ.get('DBDIR','/var/lib/samba'));sys.exit(0)
if name=='systemctl':
 state=Path(os.environ['STATE'])
 if args[0]=='show':print('LoadState=loaded\\nActiveState='+state.read_text());sys.exit(0)
 with open(os.environ['LOG'],'a') as out:out.write(args[0]+'\\n')
 if os.environ.get('FAIL')==args[0]:sys.exit(1)
 state.write_text('active' if args[0]=='start' else 'inactive');sys.exit(0)
sys.exit(1)
'''
        for name in ('findmnt','pgrep','systemctl','testparm'):
            p=commands/name; p.write_text(script); p.chmod(0o755)
        self.env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','SSD':str(self.data),'STATE':str(self.state),'LOG':str(self.log),'SOURCE':str(self.source)}

    def save_config(self): self.config.write_text(json.dumps(self.value))
    def native(self, action='host-snapshot'):
        return subprocess.run([str(BIN),action,'--config',str(self.config)],env=self.env,capture_output=True,text=True,timeout=20)
    def snapshots(self): return list((self.media/'backups/host-user').glob('snapshot-*'))

    def test_native_snapshot_and_restore_checksums(self):
        r=self.native(); self.assertEqual(r.returncode,0,r.stderr)
        snapshot=self.snapshots()[0]; manifest=recovery.verify(snapshot)
        dest=self.root/'restored'; recovery.restore(snapshot,dest)
        self.assertEqual((dest/'source0/save.dat').read_bytes(),b'PRIVATE-SAVE')
        self.assertFalse((dest/'source0/prefix-link').is_symlink())
        self.assertEqual((dest/'source0/prefix-link').read_text(),'/etc/shadow')
        self.assertEqual(snapshot.stat().st_mode&0o777,0o700)
        self.assertTrue(manifest['links_are_inert'])
        report=json.loads((self.data/'reports/jobs/host.json').read_text())
        self.assertEqual(report['status'],'pass'); self.assertNotIn('PRIVATE',json.dumps(report)+r.stdout+r.stderr)
        self.assertFalse(JOURNAL.exists())

    def test_repeated_runs_do_not_overwrite(self):
        self.assertEqual(self.native().returncode,0); self.assertEqual(self.native().returncode,0); self.assertEqual(len(self.snapshots()),2)
    def test_single_file_source_is_supported(self):
        self.value['host_backup']['sources'][0]['path']=str(self.source/'save.dat'); self.save_config()
        result=self.native(); self.assertEqual(result.returncode,0,result.stderr)
        dest=self.root/'restored'; recovery.restore(self.snapshots()[0],dest)
        self.assertEqual((dest/'source0').read_bytes(),b'PRIVATE-SAVE')
    def test_privileged_file_mode_is_refused(self):
        (self.source/'save.dat').chmod(0o4755)
        self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_steam_running_fails_without_success(self):
        self.env['STEAM']='1'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
        self.assertEqual(json.loads((self.data/'reports/jobs/host.json').read_text())['status'],'fail')
    def test_process_probe_failure_is_not_idle(self):
        self.env['PROBE_ERROR']='1'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_wrong_drive_never_publishes(self):
        self.env['WRONG']='1'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_missing_required_source_fails(self):
        self.value['host_backup']['sources'][0]['path']=str(self.root/'missing'); self.save_config()
        self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_missing_optional_source_is_recorded(self):
        self.value['host_backup']['sources'].append({'id':'source1','path':str(self.root/'missing'),'optional':True}); self.save_config()
        self.assertEqual(self.native().returncode,0); self.assertEqual(recovery.verify(self.snapshots()[0])['missing_optional'],['source1'])
    def test_recursive_source_is_refused(self):
        self.value['host_backup']['sources'][0]['path']=str(self.media); self.save_config()
        self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_special_file_fails(self):
        os.mkfifo(self.source/'fifo'); self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.snapshots())
    def test_hardlink_fails(self):
        os.link(self.source/'save.dat',self.source/'hardlink'); self.assertNotEqual(self.native().returncode,0)
    def test_public_backup_root_is_preserved_and_refused(self):
        p=self.media/'backups/host-user'; p.mkdir(parents=True); p.chmod(0o755)
        self.assertNotEqual(self.native().returncode,0); self.assertEqual(p.stat().st_mode&0o777,0o755)
    def test_tampered_snapshot_fails_verification(self):
        self.assertEqual(self.native().returncode,0); s=self.snapshots()[0]; (s/'payload/source0/save.dat').write_bytes(b'bad')
        with self.assertRaises(RuntimeError): recovery.verify(s)
    def test_restore_never_reuses_live_directory(self):
        self.assertEqual(self.native().returncode,0)
        with self.assertRaises(RuntimeError): recovery.restore(self.snapshots()[0],self.source)
        self.assertEqual((self.source/'save.dat').read_bytes(),b'PRIVATE-SAVE')
    def test_resume_restarts_only_previously_active_samba(self):
        JOURNAL.parent.mkdir(parents=True,exist_ok=True); JOURNAL.write_text(json.dumps({'format':'neko-host-resume-v1','smb_was_active':True})); self.state.write_text('inactive')
        self.assertEqual(self.native('host-resume').returncode,0); self.assertEqual(self.state.read_text(),'active'); self.assertFalse(JOURNAL.exists())
    def test_resume_failure_retains_journal(self):
        JOURNAL.parent.mkdir(parents=True,exist_ok=True); JOURNAL.write_text(json.dumps({'format':'neko-host-resume-v1','smb_was_active':True})); self.env['FAIL']='start'
        self.assertNotEqual(self.native('host-resume').returncode,0); self.assertTrue(JOURNAL.exists())
    def test_resume_leaves_originally_inactive_service(self):
        JOURNAL.parent.mkdir(parents=True,exist_ok=True); JOURNAL.write_text(json.dumps({'format':'neko-host-resume-v1','smb_was_active':False})); self.state.write_text('inactive')
        self.assertEqual(self.native('host-resume').returncode,0); self.assertEqual(self.state.read_text(),'inactive'); self.assertFalse(self.log.exists())


class SettingsTests(unittest.TestCase):
    def test_bad_calendar_rejected(self):
        for value in ('25:01','4:15',True):
            with self.assertRaises(RuntimeError): host_backup.settings({'host_backup':{'time':value}})
    def test_bool_and_reserve_validation(self):
        for values in ({'steam':'yes'},{'samba':1},{'minimum_free_gb':0},{'minimum_free_gb':True},{'extra_paths':'/home/user'}):
            with self.assertRaises(RuntimeError): host_backup.settings({'host_backup':values})
    def test_units_resume_on_failure_and_boot(self):
        cfg={'data_mount':'/srv/neko','media_mount':'/mnt/media'}
        units=host_backup.definitions(cfg,'/runtime','/private')
        self.assertIn('ExecStopPost=/runtime host-resume',units['nekoserver-host-backup.service'])
        self.assertIn('Persistent=true',units['nekoserver-host-backup.timer'])
        self.assertIn('WantedBy=multi-user.target',units['nekoserver-host-resume.service'])
        self.assertNotIn('python',str(units))
    def test_disabled_steam_needs_no_desktop_user(self):
        cfg={'data_mount':'/srv/neko','media_mount':'/mnt/media','storage':{},'host_backup':{'steam':False,'samba':False}}
        with patch.object(host_backup.pwd,'getpwuid',side_effect=AssertionError('unexpected lookup')): value=host_backup.guard(cfg)
        self.assertIsNone(value['host_backup']['steam_uid'])
    def test_dashboard_has_host_job(self): self.assertIn('host',dashboard_collect.JOBS)
    def test_recovery_rejects_traversal(self):
        for value in ('source0/../shadow','/source0/x','etc/shadow'):
            with self.assertRaises(RuntimeError): recovery.relative(value)

class SambaSnapshotTests(HostTests):
    # Exercise the service branch without touching any existing Samba installation.
    def setUp(self):
        super().setUp()
        folder=Path('/etc/samba')
        if folder.exists(): self.skipTest('Samba directory already present; isolated fixture required')
        folder.mkdir(); (folder/'smb.conf').write_text('[global]\n   include = /etc/samba/nekoserver.conf\n')
        self.addCleanup(lambda: folder.rmdir())
        self.addCleanup(lambda: (folder/'smb.conf').unlink(missing_ok=True))
        self.value['host_backup']['samba']=True; self.save_config()

    # Inherited cases also exercise the same failures with managed Samba enabled.
    def test_samba_stopped_then_restarted(self):
        result=self.native(); self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.log.read_text().splitlines(),['stop','start']); self.assertEqual(self.state.read_text(),'active')
    def test_failed_stop_still_attempts_recovery(self):
        self.env['FAIL']='stop'; self.assertNotEqual(self.native().returncode,0)
        self.assertEqual(self.log.read_text().splitlines(),['stop','start']); self.assertFalse(self.snapshots())
    def test_snapshot_failure_restarts_samba(self):
        os.mkfifo(self.source/'fifo'); self.assertNotEqual(self.native().returncode,0)
        self.assertEqual(self.state.read_text(),'active'); self.assertEqual(self.log.read_text().splitlines(),['stop','start'])
    def test_failed_restart_retains_recovery_journal(self):
        self.env['FAIL']='start'; self.assertNotEqual(self.native().returncode,0)
        self.assertTrue(JOURNAL.exists()); self.assertEqual(self.state.read_text(),'inactive')
        self.env.pop('FAIL'); self.assertEqual(self.native('host-resume').returncode,0); self.assertFalse(JOURNAL.exists())
    def test_unmanaged_samba_preserved(self):
        Path('/etc/samba/smb.conf').write_text('custom setup')
        self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.log.exists())
    def test_active_unmanaged_writer_refused(self):
        self.env['WRITER']='1'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.log.exists())

    def test_custom_database_directory_is_refused(self):
        self.env['DBDIR']='/var/unmanaged-samba'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.log.exists())
    def test_custom_auth_backend_is_refused(self):
        self.env['BACKEND']='ldapsam'; self.assertNotEqual(self.native().returncode,0); self.assertFalse(self.log.exists())
