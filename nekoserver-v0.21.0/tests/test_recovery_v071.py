import copy,hashlib,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import recovery_tools as recovery
from modules import recovery as preservation,recovery_checkpoint
ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())
DIGEST='ghcr.io/immich-app/test@sha256:'+'a'*64

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ssd=self.root/'ssd';self.hdd=self.root/'hdd';self.ssd.mkdir();self.hdd.mkdir();self.cfg=copy.deepcopy(CONFIG);self.cfg.update(data_mount=str(self.ssd),media_mount=str(self.hdd),external_backup_mount=str(self.root/'external'));self.cfg['docker']['persistent_root']=str(self.ssd);self.cfg['storage']={'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'ext4'}}
        self.upload=self.ssd/'photos/immich-library';self.db=self.ssd/'appdata/immich/postgres';self.upload.mkdir(parents=True);self.db.mkdir(parents=True);(self.upload/'upload/u').mkdir(parents=True);(self.upload/'upload/u/a.jpg').write_bytes(b'ORIGINAL');(self.upload/'profile/u').mkdir(parents=True);(self.upload/'profile/u/picture.jpg').write_bytes(b'PROFILE');(self.upload/'thumbs').mkdir();(self.upload/'upload/.immich').write_bytes(b'IMMICH');(self.upload/'upload/.incomplete').write_bytes(b'ORIGINAL-MARKER');(self.db/'PG_VERSION').write_text('14')
        manifest=self.ssd/'compose/immich';manifest.mkdir(parents=True);(manifest/'compose.yaml').write_text('fixture');(manifest/'.env').write_text('IMMICH_VERSION=v2.5.0\nDB_PASSWORD=SECRET\n')
        self.model={'services':{'database':{'image':DIGEST,'environment':{'POSTGRES_USER':'postgres','POSTGRES_DB':'immich','POSTGRES_PASSWORD':'SECRET'},'volumes':[{'type':'bind','source':str(self.db),'target':'/var/lib/postgresql/data'}]},'redis':{'image':DIGEST},'immich-server':{'image':DIGEST,'container_name':'immich_server','environment':{'DB_USERNAME':'postgres','DB_PASSWORD':'SECRET'},'ports':[{'host_ip':'0.0.0.0','published':'2283','target':2283}],'networks':{'default':None},'volumes':[{'type':'bind','source':str(self.upload),'target':'/data'},{'type':'bind','source':'/etc/localtime','target':'/etc/localtime','read_only':True}],'depends_on':{'database':{'condition':'service_started'},'redis':{'condition':'service_started'}}},'immich-machine-learning':{'image':DIGEST,'volumes':[{'type':'volume','source':'model-cache','target':'/cache'}]}},'volumes':{'model-cache':{'external':True,'name':'PRODUCTION-CACHE'}},'networks':{'default':{'name':'PRODUCTION-NETWORK'}}}
        self.calls=[];self.running=['database','immich-server','redis'];self.fail=None;self.snapshot=self.hdd/'recovery-snapshots/test';self.workspace=self.ssd/'recovery-tests/test';self.sql=b'-- PostgreSQL database dump\n'+b'-- data\n'*20+b"SELECT pg_catalog.set_config('search_path', '', false);\nCREATE TABLE example (id int);\n"
        self.mount_patch=patch.object(recovery,'mounts');self.mount_patch.start();self.cmd_patch=patch.object(recovery,'command',side_effect=self.command);self.cmd_patch.start()
    def tearDown(self):self.cmd_patch.stop();self.mount_patch.stop();self.tmp.cleanup()
    def command(self,args,timeout=60,stdin=None,stdout=None):
        self.calls.append(args)
        if self.fail and self.fail(args):raise RuntimeError('Recovery command failed: fixture')
        if stdout is not None:stdout.write(self.sql);return ''
        if 'compose' in args:
            if args[-3:]==['config','--format','json']:return json.dumps(self.model)
            if args[-4:-1]==['ps','-a','-q']:return 'container-'+args[-1]
            if args[-4:]==['ps','--status','running','--services']:return '\n'.join(self.running)
            if 'information_schema.tables' in args[-1]:return '42\n'
            if stdin is not None:self.imported=stdin.read()
            return ''
        if '{{.Config.User}}' in args:return ''
        if 'image' in args:return json.dumps([DIGEST])
        if 'inspect' in args:return 'sha256:fixture'
        if '--version' in args:return 'Docker version 27.5.1, build fixture'
        if 'info' in args:return json.dumps({'DockerRootDir':str(self.ssd/'docker-engine'),'ServerVersion':'27.5.1','Driver':'overlay2','DriverStatus':[]})
        return ''
    def make_snapshot(self):
        result=recovery.snapshot(self.cfg,self.snapshot,apply=True);self.assertEqual(result['status'],'pass');return result
    def test_snapshot_pauses_and_resumes_only_previously_running_services(self):
        before=(self.upload/'upload/u/a.jpg').read_bytes();self.make_snapshot();stops=[x for x in self.calls if 'stop' in x];starts=[x for x in self.calls if 'start' in x];self.assertEqual(stops[0][-2:],['immich-server','redis']);self.assertEqual(starts[0][-2:],['immich-server','redis']);self.assertNotIn('database',stops[0][-2:]);self.assertFalse(any('immich-machine-learning' in x for x in starts));self.assertEqual(before,(self.upload/'upload/u/a.jpg').read_bytes())
    def test_snapshot_includes_profiles_markers_empty_folders_and_checksums(self):
        self.make_snapshot();meta,_=recovery.verify_snapshot(self.snapshot);self.assertIn('upload/profile/u/picture.jpg',meta['sha256']);self.assertIn('upload/upload/.immich',meta['sha256']);self.assertIn('upload/upload/.incomplete',meta['sha256']);self.assertTrue((self.snapshot/'upload/thumbs').is_dir());self.assertFalse((self.snapshot/'.incomplete').exists());self.assertEqual((self.snapshot/'compose-resolved.json').stat().st_mode&0o777,0o600);self.assertEqual(self.snapshot.stat().st_mode&0o777,0o700)
    def test_snapshot_preview_never_stops_or_writes(self):
        result=recovery.snapshot(self.cfg,self.snapshot);self.assertEqual(result['status'],'needs_setup');self.assertFalse(self.snapshot.exists());self.assertFalse(any('stop' in c for c in self.calls))
    def test_snapshot_failure_restores_services_and_retains_incomplete(self):
        self.fail=lambda a:'pg_dump' in a[-1]
        with self.assertRaisesRegex(RuntimeError,'command failed'):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertTrue((self.snapshot/'.incomplete').exists());self.assertEqual(json.loads((self.snapshot/'.incomplete').read_text())['resume_services'],['immich-server','redis']);self.assertTrue(any('start' in a for a in self.calls));self.assertFalse((self.snapshot/'snapshot.json').exists())
    def test_partial_stop_failure_still_attempts_original_service_resume(self):
        self.fail=lambda a:'stop' in a
        with self.assertRaises(RuntimeError):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertTrue(any('start' in a for a in self.calls))
    def test_failed_resume_visible_even_when_snapshot_published(self):
        self.fail=lambda a:'start' in a
        with self.assertRaises(RuntimeError):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertTrue((self.snapshot/'snapshot.json').exists())
    def test_existing_snapshot_never_overwritten(self):
        self.make_snapshot();self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'exists'):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertFalse(any('stop' in a for a in self.calls))
    def test_custom_external_library_mount_refused_before_stop(self):
        self.model['services']['immich-server']['volumes'].append({'type':'bind','source':'/outside/photos','target':'/external'})
        with self.assertRaisesRegex(RuntimeError,'External/custom'):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertFalse(any('stop' in a for a in self.calls))
    def test_external_database_routing_refused(self):
        self.model['services']['immich-server']['environment']['DB_HOSTNAME']='production.example'
        with self.assertRaisesRegex(RuntimeError,'routing'):recovery.snapshot(self.cfg,self.snapshot)
    def test_external_database_url_refused(self):
        self.model['services']['immich-server']['environment']['DB_URL']='postgres://example/db'
        with self.assertRaisesRegex(RuntimeError,'routing'):recovery.snapshot(self.cfg,self.snapshot)
    def test_mismatched_database_credentials_refused(self):
        self.model['services']['immich-server']['environment']['DB_PASSWORD']='DIFFERENT'
        with self.assertRaisesRegex(RuntimeError,'credentials'):recovery.snapshot(self.cfg,self.snapshot)
    def test_isolated_environment_removes_host_storage_paths(self):
        self.model['services']['immich-server']['environment'].update(UPLOAD_LOCATION=str(self.upload),DB_DATA_LOCATION=str(self.db))
        self.make_snapshot();meta,model=recovery.verify_snapshot(self.snapshot);isolated=recovery.isolated_model(model,meta,self.workspace,2284);self.assertNotIn(str(self.upload),json.dumps(isolated));self.assertNotIn(str(self.db),json.dumps(isolated));self.assertEqual(isolated['services']['immich-server']['environment']['DB_HOSTNAME'],'database')
    def test_restore_changed_photo_after_verification_refused(self):
        self.make_snapshot();original=recovery.copy_verified
        def changed(source,target):
            source.write_bytes(b'CHANGED');original(source,target)
        with patch.object(recovery,'copy_verified',side_effect=changed):
            with self.assertRaisesRegex(RuntimeError,'changed after verification'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True,confirm='RESTORE-ISOLATED')
        self.assertFalse(any('up' in a for a in self.calls))
    def test_missing_primary_upload_mount_refused(self):
        self.model['services']['immich-server']['volumes']=[]
        with self.assertRaisesRegex(RuntimeError,'Required'):recovery.snapshot(self.cfg,self.snapshot)
    def test_custom_services_refused(self):
        self.model['services']['custom']={'image':DIGEST}
        with self.assertRaisesRegex(RuntimeError,'four standard'):recovery.snapshot(self.cfg,self.snapshot)
    def test_custom_privileged_service_refused(self):
        self.model['services']['immich-server']['privileged']=True
        with self.assertRaisesRegex(RuntimeError,'Custom'):recovery.snapshot(self.cfg,self.snapshot)
    def test_unpinned_immich_version_refused(self):
        (self.ssd/'compose/immich/.env').write_text('IMMICH_VERSION=release')
        with self.assertRaisesRegex(RuntimeError,'pinned'):recovery.snapshot(self.cfg,self.snapshot)
    def test_missing_image_digest_refused(self):
        original=self.command
        with patch.object(recovery,'command',side_effect=lambda args,*a,**k:'[]' if '{{json .RepoDigests}}' in args else original(args,*a,**k)):
            with self.assertRaisesRegex(RuntimeError,'digest'):recovery.snapshot(self.cfg,self.snapshot)
    def test_nonroot_images_require_manual_permission_plan(self):
        original=self.command
        with patch.object(recovery,'command',side_effect=lambda args,*a,**k:'1000' if '{{.Config.User}}' in args else original(args,*a,**k)):
            with self.assertRaisesRegex(RuntimeError,'Non-root image'):recovery.snapshot(self.cfg,self.snapshot)
    def test_verified_copy_never_overwrites_published_destination(self):
        target=self.root/'existing';target.write_bytes(b'PRESERVE')
        with self.assertRaisesRegex(RuntimeError,'never overwrite'):recovery.copy_verified(self.upload/'upload/u/a.jpg',target)
        self.assertEqual(target.read_bytes(),b'PRESERVE')
    def test_http_readiness_real_local_response_and_redirect_refusal(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            redirect=False
            def log_message(self,*a):pass
            def do_GET(self):
                self.send_response(302 if self.redirect else 200)
                if self.redirect:self.send_header('Location','http://example.invalid/')
                self.end_headers();self.wfile.write(b'IMMICH FIXTURE')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertTrue(recovery.http_ready(server.server_port));Handler.redirect=True
            with patch.object(recovery.time,'sleep'):self.assertFalse(recovery.http_ready(server.server_port))
        finally:server.shutdown();server.server_close();thread.join()
    def test_insufficient_space_never_stops(self):
        with patch.object(recovery.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError,'space'):recovery.snapshot(self.cfg,self.snapshot,True)
        self.assertFalse(any('stop' in a for a in self.calls))
    def test_database_not_running_refused(self):
        self.running=['immich-server']
        with self.assertRaisesRegex(RuntimeError,'database must'):recovery.snapshot(self.cfg,self.snapshot)
    def test_snapshot_source_symlink_refused(self):
        (self.upload/'upload/u/link').symlink_to(self.upload/'upload/u/a.jpg')
        with self.assertRaisesRegex(RuntimeError,'symlink'):recovery.snapshot(self.cfg,self.snapshot)
    def test_snapshots_require_designated_folder(self):
        with self.assertRaisesRegex(RuntimeError,'named directory'):recovery.snapshot(self.cfg,self.upload/'snapshot')
    def test_snapshot_corruption_detected_before_restore_writes(self):
        self.make_snapshot();(self.snapshot/'upload/upload/u/a.jpg').write_bytes(b'CORRUPTED')
        with self.assertRaisesRegex(RuntimeError,'checksum'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True,confirm='RESTORE-ISOLATED')
        self.assertFalse(self.workspace.exists())
    def test_snapshot_added_file_detected(self):
        self.make_snapshot();(self.snapshot/'unexpected').write_text('x')
        with self.assertRaisesRegex(RuntimeError,'inventory'):recovery.verify_snapshot(self.snapshot)
    def test_snapshot_incomplete_refused(self):
        self.make_snapshot();(self.snapshot/'.incomplete').write_text('interrupted')
        with self.assertRaisesRegex(RuntimeError,'Incomplete'):recovery.verify_snapshot(self.snapshot)
    def test_snapshot_directory_traversal_refused(self):
        self.make_snapshot();p=self.snapshot/'snapshot.json';data=json.loads(p.read_text());data['empty_directories']=['../production'];p.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError,'directory'):recovery.verify_snapshot(self.snapshot)
    def test_restore_preview_no_files_or_mutations(self):
        self.make_snapshot();self.calls=[];result=recovery.restore_test(self.cfg,self.snapshot,self.workspace);self.assertEqual(result['status'],'needs_setup');self.assertFalse(self.workspace.exists());self.assertFalse(self.calls)
    def test_restore_requires_confirmation(self):
        self.make_snapshot()
        with self.assertRaisesRegex(RuntimeError,'confirm'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True)
        self.assertFalse(self.workspace.exists())
    def test_restore_production_target_refused(self):
        self.make_snapshot()
        with self.assertRaisesRegex(RuntimeError,'named directory'):recovery.restore_test(self.cfg,self.snapshot,self.upload)
    def test_restore_port_conflict_refused(self):
        import socket
        self.make_snapshot()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError,'port is already'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,port)
    def test_restore_production_port_refused(self):
        self.make_snapshot()
        with self.assertRaisesRegex(RuntimeError,'distinct'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,2283)
    def test_isolated_model_has_no_production_paths_names_or_public_ports(self):
        self.make_snapshot();meta,model=recovery.verify_snapshot(self.snapshot);isolated=recovery.isolated_model(model,meta,self.workspace,2284);self.assertNotIn('container_name',isolated['services']['immich-server']);self.assertNotIn('volumes',isolated);self.assertTrue(isolated['networks']['default']['internal']);self.assertEqual(isolated['services']['immich-server']['ports'][0]['host_ip'],'127.0.0.1');self.assertNotIn(str(self.upload),json.dumps(isolated));self.assertNotIn(str(self.db),json.dumps(isolated));self.assertNotIn('PRODUCTION',json.dumps(isolated));self.assertTrue(all(Path(v['source']).is_relative_to(self.workspace) for s in isolated['services'].values() for v in s.get('volumes',[])))
    def test_restore_import_precedes_server_start_and_uses_transaction(self):
        self.make_snapshot();before=(self.snapshot/'database.sql').read_bytes();self.calls=[]
        with patch.object(recovery,'http_ready',return_value=True):result=recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True,confirm='RESTORE-ISOLATED')
        self.assertEqual(result['status'],'needs_setup');self.assertEqual(result['database_tables'],42);self.assertNotIn('SECRET',json.dumps(result));self.assertFalse((self.workspace/'.incomplete').exists());self.assertEqual(before,(self.snapshot/'database.sql').read_bytes());self.assertIn(b"'public, pg_catalog', true",self.imported)
        import_index=next(i for i,a in enumerate(self.calls) if '--single-transaction' in a[-1]);ups=[(i,a) for i,a in enumerate(self.calls) if 'up' in a];self.assertEqual(ups[0][1][-1],'database');self.assertLess(ups[0][0],import_index);self.assertGreater(ups[-1][0],import_index);self.assertIn('--set ON_ERROR_STOP=on -f -',self.calls[import_index][-1]);self.assertTrue(all('neko-immich' not in a for a in self.calls));self.assertTrue((self.workspace/'upload/profile/u/picture.jpg').exists())
    def test_failed_import_stops_only_isolated_project_and_keeps_backup(self):
        self.make_snapshot();before=recovery.digest(self.snapshot/'database.sql');self.calls=[];self.fail=lambda a:'--single-transaction' in a[-1]
        with self.assertRaises(RuntimeError):recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True,confirm='RESTORE-ISOLATED')
        self.assertEqual(before,recovery.digest(self.snapshot/'database.sql'));stops=[a for a in self.calls if 'stop' in a];self.assertEqual(len(stops),1);self.assertTrue(stops[0][stops[0].index('-p')+1].startswith('neko-restore-'));self.assertTrue((self.workspace/'.incomplete').exists());self.assertFalse(any('down' in a or '-v' in a for a in self.calls))
    def test_http_failure_leaves_incomplete_and_stops_rehearsal(self):
        self.make_snapshot();self.calls=[]
        with patch.object(recovery,'http_ready',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'HTTP-ready'):recovery.restore_test(self.cfg,self.snapshot,self.workspace,apply=True,confirm='RESTORE-ISOLATED')
        self.assertTrue((self.workspace/'restore-report.json').exists());self.assertTrue((self.workspace/'.incomplete').exists());self.assertTrue(any('stop' in a for a in self.calls))
    def test_existing_workspace_not_reused(self):
        self.make_snapshot();self.workspace.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError,'exists'):recovery.restore_test(self.cfg,self.snapshot,self.workspace)
    def test_checkpoint_persists_verified_identity(self):
        data=recovery.checkpoint(self.cfg,True);self.assertEqual(data['docker_version'],'27.5.1');self.assertEqual((self.ssd/'installer/host-checkpoint.json').stat().st_mode&0o777,0o600);self.assertNotIn('SECRET',json.dumps(data))
    def test_checkpoint_wrong_backend_refused(self):
        with patch.object(recovery,'json_command',return_value={'DockerRootDir':str(self.ssd/'docker-engine'),'ServerVersion':'27.5.1','Driver':'overlayfs','DriverStatus':[['driver-type','io.containerd.snapshotter.v1']]}):
            with self.assertRaisesRegex(RuntimeError,'classic'):recovery.checkpoint(self.cfg,True)
    def test_checkpoint_preview_does_not_write(self):
        recovery.checkpoint(self.cfg);self.assertFalse((self.ssd/'installer/host-checkpoint.json').exists())
    def test_checkpoint_module_reports_missing_support(self):
        with patch.object(recovery_checkpoint,'checkpoint',side_effect=RuntimeError('failure')):result=recovery_checkpoint.run(SimpleNamespace(config=self.cfg,apply=True,log=lambda _:None))
        self.assertEqual(result['status'],'needs_setup')
    def test_preserved_bundle_checksum_and_current_storage_checked(self):
        with patch.object(preservation.os.path,'ismount',return_value=True):bundle=preservation.preserve(self.ssd,ROOT,self.cfg)
        recovery.verify_bundle(bundle,self.cfg);(bundle/'installer.py').write_text('BROKEN')
        with self.assertRaisesRegex(RuntimeError,'checksum'):recovery.verify_bundle(bundle,self.cfg)
    def test_checkpoint_profile_ordering(self):
        import installer
        modules=installer.load_modules(ROOT/'modules');plan=installer.ordered(modules,CONFIG['profiles']['full']);self.assertGreater(plan.index('recovery_checkpoint'),plan.index('docker'))
    def test_command_timeout_never_exposes_raw_arguments(self):
        import subprocess
        original=recovery.command
        self.cmd_patch.stop()
        try:
            with patch.object(recovery.subprocess,'run',side_effect=subprocess.TimeoutExpired(['docker','SECRET'],1)):
                with self.assertRaisesRegex(RuntimeError,'timed out') as caught:recovery.command(['docker','SECRET'])
            self.assertNotIn('SECRET',str(caught.exception))
        finally:self.cmd_patch.start()
    def test_preserved_new_recovery_tools_load_without_original_source(self):
        import subprocess,sys
        with patch.object(preservation.os.path,'ismount',return_value=True):bundle=preservation.preserve(self.ssd,ROOT,self.cfg)
        result=subprocess.run([sys.executable,str(bundle/'recover.py'),'--help'],cwd=self.tmp.name,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertIn('restore-test',result.stdout)
    def test_restore_has_no_app_tables_is_failure(self):
        with patch.object(recovery,'command',return_value='0'):
            with self.assertRaisesRegex(RuntimeError,'no verified'):recovery.restored_tables(['docker','compose'])
    def test_checkpoint_refuses_wrong_mounted_uuid(self):
        self.mount_patch.stop()
        try:
            with patch.object(recovery.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='WRONG ext4')):
                with self.assertRaisesRegex(RuntimeError,'wrong filesystem'):recovery.checkpoint(self.cfg)
        finally:self.mount_patch.start()
    def test_recovery_lock_excludes_second_operation(self):
        with recovery.lock(self.cfg):
            with self.assertRaisesRegex(RuntimeError,'active'):
                with recovery.lock(self.cfg):pass

class ReattachTests(unittest.TestCase):
    setUp=RecoveryTests.setUp
    tearDown=RecoveryTests.tearDown
    command=RecoveryTests.command
    def prepare(self):
        recovery.checkpoint(self.cfg,True);store=self.ssd/'docker-engine';store.mkdir();(store/'fixture').write_text('PERSISTENT');self.daemon=self.root/'etc/daemon.json';self.override=self.root/'etc/guard.conf';self.default=self.root/'var/docker';self.daemon.parent.mkdir();self.real_path=Path
        def path(value,*args):
            p=Path(value,*args)
            return {'/etc/docker/daemon.json':self.daemon,'/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf':self.override,'/var/lib/docker':self.default}.get(str(p),p)
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close);self.stack.enter_context(patch.object(recovery,'Path',side_effect=path));self.stack.enter_context(patch.object(recovery,'verify_bundle',return_value='0.7.1'));self.stack.enter_context(patch.object(recovery,'host_supported',return_value=True));self.stack.enter_context(patch.object(recovery.subprocess,'run',return_value=SimpleNamespace(returncode=3)));self.stack.enter_context(patch.object(recovery.docker_module,'install_storage_guard'))
    def test_reattach_preview_keeps_os_config_and_store(self):
        self.prepare();result=recovery.reattach(self.cfg,ROOT);self.assertEqual(result['status'],'needs_setup');self.assertFalse(self.daemon.exists());self.assertEqual((self.ssd/'docker-engine/fixture').read_text(),'PERSISTENT')
    def test_explicit_reattach_sets_guard_then_daemon_and_verifies(self):
        self.prepare();result=recovery.reattach(self.cfg,ROOT,True,'SSD-UUID');self.assertEqual(result['status'],'needs_setup');self.assertEqual(json.loads(self.daemon.read_text())['data-root'],str(self.ssd/'docker-engine'));self.assertFalse(json.loads(self.daemon.read_text())['features']['containerd-snapshotter']);recovery.docker_module.install_storage_guard.assert_called_once();self.assertEqual((self.ssd/'docker-engine/fixture').read_text(),'PERSISTENT')
    def test_wrong_uuid_confirmation_changes_nothing(self):
        self.prepare()
        with self.assertRaisesRegex(RuntimeError,'Confirm'):recovery.reattach(self.cfg,ROOT,True,'WRONG')
        self.assertFalse(self.daemon.exists())
    def test_bazzite_blocks_before_changes(self):
        self.prepare()
        with patch.object(recovery,'host_supported',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Bazzite'):recovery.reattach(self.cfg,ROOT,True,'SSD-UUID')
        self.assertFalse(self.daemon.exists())
    def test_active_docker_or_socket_blocks(self):
        self.prepare()
        with patch.object(recovery.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(RuntimeError,'Stop Docker'):recovery.reattach(self.cfg,ROOT,True,'SSD-UUID')
    def test_docker_version_mismatch_blocks(self):
        self.prepare();original=self.command
        with patch.object(recovery,'command',side_effect=lambda a,*x,**k:'Docker version 28.0.0, build test' if '--version' in a else original(a,*x,**k)):
            with self.assertRaisesRegex(RuntimeError,'checkpoint Docker version'):recovery.reattach(self.cfg,ROOT)
    def test_matching_client_but_mismatched_daemon_blocks(self):
        self.prepare();original=self.command
        with patch.object(recovery,'command',side_effect=lambda a,*x,**k:'Docker version 28.0.0, build fixture' if a[0]=='dockerd' else original(a,*x,**k)):
            with self.assertRaisesRegex(RuntimeError,'checkpoint Docker version'):recovery.reattach(self.cfg,ROOT,True,'SSD-UUID')
        self.assertFalse(self.daemon.exists())
    def test_uid_change_blocks_without_chown(self):
        self.prepare();self.cfg['media']['uid']=2000
        with self.assertRaisesRegex(RuntimeError,'UID/GID'):recovery.reattach(self.cfg,ROOT)
    def test_checkpoint_storage_change_blocks(self):
        self.prepare();self.cfg['storage']['data_ssd']['uuid']='OTHER'
        with self.assertRaisesRegex(RuntimeError,'storage differs'):recovery.reattach(self.cfg,ROOT)
    def test_nonempty_default_docker_store_blocks(self):
        self.prepare();self.default.mkdir(parents=True);(self.default/'other').write_text('OTHER')
        with self.assertRaisesRegex(RuntimeError,'Default Docker'):recovery.reattach(self.cfg,ROOT)
        self.assertFalse(self.daemon.exists())
    def test_existing_other_data_root_preserved(self):
        self.prepare();self.daemon.write_text(json.dumps({'data-root':'/other'}))
        with self.assertRaisesRegex(RuntimeError,'manual migration'):recovery.reattach(self.cfg,ROOT)
        self.assertEqual(json.loads(self.daemon.read_text())['data-root'],'/other')
    def test_unmanaged_guard_preserved(self):
        self.prepare();self.override.write_text('CUSTOM')
        with self.assertRaisesRegex(RuntimeError,'Unmanaged'):recovery.reattach(self.cfg,ROOT)
        self.assertEqual(self.override.read_text(),'CUSTOM')
    def test_failed_start_stops_service_and_socket(self):
        self.prepare();self.fail=lambda a:a[:3]==['systemctl','enable','--now']
        with self.assertRaises(RuntimeError):recovery.reattach(self.cfg,ROOT,True,'SSD-UUID')
        self.assertEqual(recovery.subprocess.run.call_args.args[0],['systemctl','stop','docker.service','docker.socket'])

if __name__=='__main__':unittest.main()
