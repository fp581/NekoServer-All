from neko_common import VERSION
"""Cross-module regressions for faults found while auditing the shipped v0.6.5 ZIP."""
import copy
import errno
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import installer
import neko_common as common
from modules import docker, storage, photo_preservation, photo_backup

ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())

def load(filename):
    spec=importlib.util.spec_from_file_location(filename.replace('-','_'),ROOT/filename)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

backup=load('photo-backup.py');guard=load('storage-guard.py');connect=load('media-connect.py')
onboard=load('onboard.py');health=load('media-health.py');weekly=load('photo-weekly.py');db=load('immich-db-backup.py')

class AuditRegressionTests(unittest.TestCase):
    def test_all_profiles_have_prerequisites_before_dependents(self):
        mods=installer.load_modules(ROOT/'modules')
        for profile,selection in CONFIG['profiles'].items():
            plan=installer.ordered(mods,selection)
            for name in plan:
                for dependency in mods[name]['deps']:
                    self.assertLess(plan.index(dependency),plan.index(name),profile)

    def test_nested_mounts_rejected_before_apply(self):
        cfg=copy.deepcopy(CONFIG);cfg['media_mount']=cfg['data_mount']+'/media'
        with self.assertRaisesRegex(installer.InstallerError,'non-nested'):installer.validate_config(cfg)

    def test_unit_injection_mount_rejected(self):
        cfg=copy.deepcopy(CONFIG);cfg['data_mount']='/srv/neko%root'
        with self.assertRaises(RuntimeError):installer.validate_config(cfg)

    def test_archive_cannot_be_inside_immich(self):
        cfg=copy.deepcopy(CONFIG);cfg['photos']['archive_dir']='photos/immich-library/archive'
        with self.assertRaisesRegex(installer.InstallerError,'separate'):installer.validate_config(cfg)

    def test_duplicate_services_rejected(self):
        cfg=copy.deepcopy(CONFIG);cfg['media']['services']=['jellyfin','jellyfin']
        with self.assertRaises(installer.InstallerError):installer.validate_config(cfg)

    def test_cloned_uuid_rejected(self):
        dev={'name':'sda1','type':'part','uuid':'same-uuid','fstype':'ext4'}
        with self.assertRaisesRegex(RuntimeError,'Duplicate'):storage.discover_filesystems({'blockdevices':[dev,dict(dev,name='sdb1')]})

    def test_retry_includes_disabled_profile_module(self):
        with tempfile.TemporaryDirectory() as t:
            state=Path(t);(state/'state.json').write_text(json.dumps({'media':{'status':'failed'}}))
            out=io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(installer.main(['--retry-failed','--check','--state-dir',t]),0)
            self.assertIn('media: Guarded deployment',out.getvalue())

    def test_guard_accepts_expected_uuid_and_type(self):
        cfg=copy.deepcopy(CONFIG);cfg['storage']={'data_ssd':{'uuid':'ssd-0001','fstype':'ext4'},'media_hdd':{'uuid':'hdd-0002','fstype':'xfs'}}
        with patch.object(guard.subprocess,'run',side_effect=[SimpleNamespace(returncode=0,stdout='ssd-0001 ext4'),SimpleNamespace(returncode=0,stdout='hdd-0002 xfs')]):guard.verify(cfg)

    def test_guard_rejects_wrong_hdd(self):
        cfg=copy.deepcopy(CONFIG);cfg['storage']={'data_ssd':{'uuid':'ssd-0001','fstype':'ext4'},'media_hdd':{'uuid':'hdd-0002','fstype':'ext4'}}
        with patch.object(guard.subprocess,'run',side_effect=[SimpleNamespace(returncode=0,stdout='ssd-0001 ext4'),SimpleNamespace(returncode=0,stdout='wrong-hdd ext4')]):
            with self.assertRaisesRegex(RuntimeError,'wrong filesystem'):guard.verify(cfg)

    def test_guard_rejects_missing_hdd(self):
        cfg=copy.deepcopy(CONFIG);cfg['storage']={'data_ssd':{'uuid':'ssd-0001','fstype':'ext4'},'media_hdd':{'uuid':'hdd-0002','fstype':'ext4'}}
        with patch.object(guard.subprocess,'run',side_effect=[SimpleNamespace(returncode=0,stdout='ssd-0001 ext4'),SimpleNamespace(returncode=1,stdout='')]):
            with self.assertRaises(RuntimeError):guard.verify(cfg)

    def test_docker_guard_requires_both_mounts_and_uuid_verifier(self):
        text=docker.guard_contents(CONFIG,Path('/srv/nekoserver/installer/guards/v0.6.6'))
        self.assertIn('RequiresMountsFor=/srv/nekoserver /mnt/nekoserver-media',text)
        self.assertIn('BindsTo=srv-nekoserver.mount mnt-nekoserver\\x2dmedia.mount',text)
        self.assertIn('nekoserver-runtime storage-guard --config',text)

    def test_fresh_docker_disables_separate_containerd_store(self):
        with tempfile.TemporaryDirectory() as t:
            result=docker.prepare_daemon_config(Path(t)/'daemon.json',Path(t)/'engine')
            self.assertIs(result['features']['containerd-snapshotter'],False)

    def test_existing_containerd_backend_is_not_switched(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'daemon.json';path.write_text('{"features":{"containerd-snapshotter":true}}')
            with self.assertRaisesRegex(RuntimeError,'backend switch'):docker.prepare_daemon_config(path,Path(t)/'engine')

    def test_active_containerd_detected_even_with_correct_data_root(self):
        with patch.object(docker,'call',return_value='[["driver-type","io.containerd.snapshotter.v1"]]'):
            with self.assertRaisesRegex(RuntimeError,'separate migration'):docker.verify_classic_store()

    def test_atomic_report_does_not_follow_predictable_temp_symlink(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);victim=base/'victim';victim.write_text('keep')
            report=base/'report.json';report.with_suffix('.tmp').symlink_to(victim)
            onboard.safe_report(report,[{'check':'test','status':'pass'}])
            self.assertEqual(victim.read_text(),'keep')
            self.assertEqual(report.stat().st_mode&0o777,0o600)

    def test_parent_symlink_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);(base/'real').mkdir();(base/'link').symlink_to(base/'real',target_is_directory=True)
            with self.assertRaises(RuntimeError):onboard.safe_report(base/'link'/'report.json',[])
            self.assertFalse((base/'real'/'report.json').exists())

    def test_backup_rejects_parent_symlink_escape(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';dst=base/'dest';outside=base/'outside'
            (src/'folder').mkdir(parents=True);dst.mkdir();outside.mkdir();(src/'folder'/'x.jpg').write_bytes(b'photo')
            (dst/'folder').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(RuntimeError):backup.backup(src,dst,True)
            self.assertFalse((outside/'x.jpg').exists())

    def test_backup_rejects_source_symlink_before_resolving(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);(base/'real').mkdir();(base/'link').symlink_to(base/'real',target_is_directory=True)
            with self.assertRaises(RuntimeError):backup.backup(base/'link',base/'dest',True)

    def test_backup_skips_unpublished_partial_files(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'src';src.mkdir();(src/'x.jpg.neko-part').write_bytes(b'unverified')
            self.assertEqual(backup.backup(src,base/'dest',True),0)
            self.assertFalse((base/'dest').exists())

    def test_no_hardlinks_backup_uses_atomic_no_replace_rename(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);temp=base/'part';target=base/'published';temp.write_bytes(b'checked')
            with patch.object(common.os,'link',side_effect=OSError(errno.EPERM,'not supported')):common.publish_no_replace(temp,target)
            self.assertEqual(target.read_bytes(),b'checked');self.assertFalse(temp.exists())

    def test_no_hardlinks_rename_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);temp=base/'part';target=base/'published';temp.write_bytes(b'new');target.write_bytes(b'old')
            with patch.object(common.os,'link',side_effect=OSError(errno.EPERM,'not supported')):
                with self.assertRaises(FileExistsError):common.publish_no_replace(temp,target)
            self.assertEqual(target.read_bytes(),b'old');self.assertEqual(temp.read_bytes(),b'new')

    def test_indexer_rejection_is_reported_as_failure(self):
        schema={'name':'Example','fields':[]}
        def api(app,key,endpoint,method='GET',payload=None,timeout=12):
            if method=='POST':raise RuntimeError('HTTP 400')
            return [schema] if endpoint.endswith('/schema') else []
        with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',side_effect=api),patch('builtins.input',side_effect=['','1','']):
            with self.assertRaisesRegex(RuntimeError,'incomplete'):connect.configure_indexers('/fake',True)

    def test_name_only_application_does_not_count_as_connected(self):
        def api(app,key,endpoint,method='GET',payload=None,timeout=12):
            if endpoint=='/applications/schema':return []
            if endpoint=='/applications':return [{'name':'neko-sonarr'}]
            return {}
        with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',side_effect=api):
            with self.assertRaisesRegex(RuntimeError,'incomplete'):connect.configure_applications('/fake',True,('sonarr',))

    def test_prewiew_missing_api_key_is_not_success(self):
        with patch.object(connect,'app_key',side_effect=lambda app,root:'key' if app=='prowlarr' else (_ for _ in ()).throw(RuntimeError('not ready'))),patch.object(connect,'request',return_value=[]):
            with self.assertRaises(RuntimeError):connect.configure_applications('/fake',False,('sonarr',))

    def test_disabled_download_client_does_not_count_as_connected(self):
        def api(app,key,endpoint,method='GET',payload=None,timeout=12):return [{'name':'Neko-qBittorrent','enable':False}]
        with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',side_effect=api),patch.object(connect.getpass,'getpass',return_value='secret'),patch('builtins.input',return_value='admin'):
            with self.assertRaises(RuntimeError):connect.configure_download_clients('/fake',True,('sonarr',),verify_qbit=False)

    def test_missing_download_password_is_not_success(self):
        with patch.object(connect.getpass,'getpass',return_value=''):
            with self.assertRaises(RuntimeError):connect.configure_download_clients('/fake',True)

    def test_rootfolder_check_requires_expected_container_path(self):
        self.assertFalse(health.configured('sonarr','rootfolder',[{'path':'/wrong'}],['sonarr']))
        self.assertTrue(health.configured('sonarr','rootfolder',[{'path':'/data/tv'}],['sonarr']))

    def test_disabled_indexer_is_not_ready(self):
        self.assertFalse(health.configured('prowlarr','indexer',[{'enable':False}],['prowlarr']))
        self.assertFalse(health.configured('sonarr','indexer',[{'enableRss':False}],['sonarr']))

    def test_disabled_app_sync_is_not_ready(self):
        items=[{'syncLevel':'disabled','fields':[{'name':'baseUrl','value':'http://sonarr:8989'},{'name':'prowlarrUrl','value':'http://prowlarr:9696'},{'name':'apiKey','value':'secret'}]}]
        self.assertFalse(health.configured('prowlarr','applications',items,['sonarr']))

    def test_http_500_is_failure(self):
        info={'state':'running','health':'not_defined','network':True,'mounts':['/srv/nekoserver/appdata/seerr']}
        with patch.object(health,'inspect',return_value=info):
            report=health.audit(dict(CONFIG,media={'services':['seerr']}),probe=lambda *a:500)
        self.assertEqual(report['services']['seerr']['result'],'fail')

    def test_http_and_folders_alone_are_not_full_onboarding(self):
        self.assertEqual(onboard.readiness_exit([{'status':'needs_setup'}]),1)
        self.assertEqual(onboard.readiness_exit([{'status':'fail'}]),2)

    def test_custom_weekly_archive_uses_explicit_media_root(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);source=base/'media'/'custom'/'deep'/'archive';source.mkdir(parents=True)
            with patch.object(weekly.os,'geteuid',return_value=0),patch.object(weekly.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as run:
                with self.assertRaisesRegex(RuntimeError,'Media drive not mounted'):weekly.run('external-123','ext4',source,base/'usb',base/'media')
            self.assertEqual(run.call_args.args[0][-1],str(base/'media'))

    def test_database_dump_failure_does_not_leak_provider_stderr(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);compose=base/'compose';env=base/'env';compose.write_text('x');env.write_text('x')
            with patch.object(db.subprocess,'run',return_value=SimpleNamespace(returncode=1,stderr=b'password=SECRET')):
                with self.assertRaises(RuntimeError) as result:db.backup(compose,env,base/'backup',True)
            self.assertNotIn('SECRET',str(result.exception));self.assertFalse(list((base/'backup').glob('*.sql')))

    def test_runtime_upgrade_and_custom_archive_reach_both_photo_timers(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);data=base/'ssd';media=base/'hdd';data.mkdir();media.mkdir()
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=str(data);cfg['media_mount']=str(media);cfg['photos']['archive_dir']='custom/archive';cfg['photos']['backup_schedule']='legacy';cfg['runtime']['backend']='python'
            legacy=data/'installer'/'runtime'/'v0.5.2';legacy.mkdir(parents=True);(legacy/'photo-weekly.py').write_text('old content')
            unitdir=base/'units';unitdir.mkdir()
            for name in ('nekoserver-photo-preservation','nekoserver-immich-db-backup'):
                (unitdir/(name+'.service')).write_text(f'[Unit]\nDescription={name}\n[Service]\nType=oneshot\nExecStart=/old/runtime\n')
            def paths(*parts):
                actual=Path(*parts)
                return unitdir/actual.relative_to('/etc/systemd/system') if str(actual).startswith('/etc/systemd/system') else actual
            ctx=SimpleNamespace(config=cfg,apply=True,log=lambda x:None)
            with patch.object(photo_preservation,'Path',side_effect=paths),patch.object(photo_preservation.os,'geteuid',return_value=0),patch.object(photo_preservation.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                photo_preservation.run(ctx)
                photo_preservation.run(ctx)
            for unit in unitdir.glob('*.service'):
                text=unit.read_text();self.assertIn('runtime/v'+VERSION+'/',text);self.assertIn('--archive-dir custom/archive',text)
            self.assertEqual((legacy/'photo-weekly.py').read_text(),'old content')
            self.assertTrue((data/('installer/runtime/v'+VERSION+'/neko_common.py')).is_file())

if __name__=='__main__':unittest.main()

class ConnectorHTTPTests(unittest.TestCase):
    """Exercise real urllib requests against an isolated local API fixture."""
    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        self.calls=[];self.error=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def log_message(self,*args):pass
            def respond(self):
                payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
                owner.calls.append((self.command,self.path,self.headers.get('X-Api-Key'),payload))
                if owner.error:
                    self.send_response(400);self.end_headers();self.wfile.write(b'PRIVATE_PROVIDER_PASSWORD');return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'id':7,'name':'fixture'}).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.ports=patch.dict(connect.PORTS,{'sonarr':self.server.server_port});self.ports.start()
    def tearDown(self):
        self.ports.stop();self.server.shutdown();self.server.server_close();self.thread.join()
    def test_real_http_uses_api_version_key_and_json(self):
        result=connect.request('sonarr','test-key','/downloadclient','POST',{'enable':True})
        self.assertEqual(result['id'],7)
        self.assertEqual(self.calls,[('POST','/api/v3/downloadclient','test-key',{'enable':True})])
    def test_real_http_provider_error_redacts_body_and_key(self):
        self.error=True
        with self.assertRaises(RuntimeError) as error:connect.request('sonarr','test-secret','/downloadclient')
        self.assertIn('HTTP 400',str(error.exception));self.assertNotIn('PRIVATE',str(error.exception));self.assertNotIn('test-secret',str(error.exception))

class InstallerFlowTests(unittest.TestCase):
    def test_full_run_preserves_independent_modules_after_media_failure(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);modules=base/'modules';modules.mkdir()
            actual=installer.load_modules(ROOT/'modules')
            for name,info in actual.items():
                body="raise RuntimeError('simulated media failure')" if name=='media' else "context.log('fixture '+__name__)"
                (modules/(name+'.py')).write_text(f'DEPENDENCIES={info["deps"]!r}\ndef run(context):\n    {body}\n')
            config=base/'config.json';config.write_text(json.dumps(CONFIG))
            args=SimpleNamespace(config=str(config),state_dir=str(base/'state'),profile='full',module=None,retry_failed=False,check=False,dry_run=False,apply=True)
            with redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,base),1)
            state=json.loads((base/'state'/'state.json').read_text())
            self.assertEqual(state['media']['status'],'failed');self.assertEqual(state['media_monitor']['status'],'success')
            for name in ('photos','photo_preservation','photo_backup','gateway_lan','samba'):self.assertEqual(state[name]['status'],'success')
            args.profile=None;args.retry_failed=True
            (modules/'media.py').write_text("DEPENDENCIES=['docker']\ndef run(context):\n    context.log('media repaired')\n")
            with redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,base),0)
            state=json.loads((base/'state'/'state.json').read_text());self.assertEqual(state['media_monitor']['status'],'success')

    def test_webm_original_is_archived(self):
        preserve=load('photo-preserve.py')
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);video=base/'source'/'upload'/'video.webm';video.parent.mkdir(parents=True);video.write_bytes(b'original video')
            preserve.transfer(base/'source',base/'archive',True,60,video.stat().st_mtime+600)
            self.assertEqual((base/'archive'/'upload'/'video.webm').read_bytes(),b'original video')
