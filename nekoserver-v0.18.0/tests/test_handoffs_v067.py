import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import installer
import qbit_setup
import source_import
from modules import recovery,onboarding
from neko_common import VERSION

ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())
def load(filename):
    spec=importlib.util.spec_from_file_location(filename.replace('-','_'),ROOT/filename);obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
connect=load('media-connect.py');board=load('onboard.py');wizard=load('source-wizard.py')

class QbitFixture:
    def __init__(self,path='/downloads/',torrents=None,categories=None):
        self.preferences={'save_path':path};self.categories={} if categories is None else categories;self.torrents=torrents or [];self.calls=[];self.closed=False
    def login(self,user,password):self.calls.append(('login',))
    def close(self):self.closed=True
    def call(self,endpoint,form=None,json_result=False):
        self.calls.append((endpoint,form))
        if endpoint=='/app/preferences':return copy.deepcopy(self.preferences)
        if endpoint=='/torrents/categories':return copy.deepcopy(self.categories)
        if endpoint=='/torrents/info':return self.torrents
        if endpoint=='/app/setPreferences':self.preferences.update(json.loads(form['json']));return ''
        if endpoint=='/torrents/createCategory':self.categories[form['category']]={'savePath':form['savePath']};return ''
        raise AssertionError(endpoint)

class HandoffTests(unittest.TestCase):
    def test_persistent_installer_has_all_import_dependencies(self):
        with tempfile.TemporaryDirectory() as t:
            with patch.object(recovery.os.path,'ismount',return_value=True):dest=recovery.preserve(t,ROOT,CONFIG)
            for name in ('installer.py','onboard.py','media-connect.py','qbit_setup.py','source_import.py','neko_common.py','modules/onboarding.py','compose/sonarr.yaml','install.sh'):self.assertTrue((dest/name).is_file(),name)
            self.assertEqual(json.loads((dest/'config.json').read_text()),CONFIG)
            self.assertEqual((dest/'config.json').stat().st_mode&0o777,0o600)
            with patch.object(recovery.os.path,'ismount',return_value=True):self.assertEqual(dest,recovery.preserve(t,ROOT,CONFIG))

    def test_persistent_installer_refuses_changed_existing_code(self):
        with tempfile.TemporaryDirectory() as t:
            with patch.object(recovery.os.path,'ismount',return_value=True):dest=recovery.preserve(t,ROOT,CONFIG)
            (dest/'installer.py').write_text('custom code')
            with patch.object(recovery.os.path,'ismount',return_value=True):
                with self.assertRaisesRegex(RuntimeError,'refusing overwrite'):recovery.preserve(t,ROOT,CONFIG)
            self.assertEqual((dest/'installer.py').read_text(),'custom code')

    def test_persistent_bundle_can_load_modules_without_original_package(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as t:
            with patch.object(recovery.os.path,'ismount',return_value=True):dest=recovery.preserve(t,ROOT,CONFIG)
            result=subprocess.run([sys.executable,str(dest/'installer.py'),'--profile','full','--check'],cwd=t,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('onboarding:',result.stdout)

    def test_interactive_install_calls_persistent_onboarding(self):
        with tempfile.TemporaryDirectory() as t:
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=t
            release=Path(t)/'installer'/'releases'/('v'+VERSION);release.mkdir(parents=True);(release/'onboard.py').write_text('fixture')
            context=SimpleNamespace(config=cfg,apply=True,log=lambda x:None)
            with patch.object(onboarding.sys.stdin,'isatty',return_value=True),patch.object(onboarding.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as call:
                result=onboarding.run(context)
            args=call.call_args.args[0];self.assertIn('--configure',args);self.assertIn('--sources',args);self.assertIn('--apply',args)
            self.assertEqual(result['status'],'needs_setup')
            self.assertEqual(args[1],str(release/'onboard.py'))

    def test_headless_install_does_not_prompt_for_credentials(self):
        with tempfile.TemporaryDirectory() as t:
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=t
            release=Path(t)/'installer'/'releases'/('v'+VERSION);release.mkdir(parents=True);(release/'onboard.py').write_text('fixture')
            with patch.object(onboarding.sys.stdin,'isatty',return_value=False),patch.object(onboarding.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as call:
                onboarding.run(SimpleNamespace(config=cfg,apply=True,log=lambda x:None))
            args=call.call_args.args[0];self.assertIn('--report-only',args);self.assertNotIn('--configure',args)

    def test_onboarding_failure_is_not_marked_complete(self):
        with tempfile.TemporaryDirectory() as t:
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=t
            release=Path(t)/'installer'/'releases'/('v'+VERSION);release.mkdir(parents=True);(release/'onboard.py').write_text('fixture')
            with patch.object(onboarding.sys.stdin,'isatty',return_value=False),patch.object(onboarding.subprocess,'run',return_value=SimpleNamespace(returncode=2)):
                with self.assertRaises(RuntimeError):onboarding.run(SimpleNamespace(config=cfg,apply=True,log=lambda x:None))

    def test_readiness_wait_rechecks_until_ready(self):
        ticks=iter([0,0,0,1,1,2]);calls=[]
        def probe(app):calls.append(app);return len(calls)>1
        self.assertEqual(board.wait_ready(['sonarr'],3,probe,lambda:next(ticks),lambda seconds:None),[])
        self.assertEqual(calls,['sonarr','sonarr'])

    def test_readiness_wait_returns_unavailable_apps(self):
        self.assertEqual(board.wait_ready(['sonarr'],0,lambda app:False,lambda:0,lambda s:None),['sonarr'])

    def test_rootfolders_create_and_verify_standard_path(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'library'/'tv').mkdir(parents=True);calls=[];folders=[]
            def api(app,key,endpoint,method='GET',payload=None,timeout=12):
                calls.append((endpoint,method,payload))
                if method=='POST':folders.append(dict(payload,accessible=True));return folders[-1]
                return folders
            with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',side_effect=api):connect.configure_root_folders('/fake',t,True,('sonarr',))
            self.assertEqual([c for c in calls if c[1]=='POST'],[('/rootfolder','POST',{'path':'/data/tv'})])

    def test_rootfolder_rerun_does_not_create_duplicate(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'library'/'movies').mkdir(parents=True)
            with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',return_value=[{'path':'/data/movies','accessible':True}]) as api:connect.configure_root_folders('/fake',t,True,('radarr',))
            self.assertTrue(all(len(c.args)<4 for c in api.call_args_list))

    def test_rootfolder_preview_does_not_post(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'library'/'tv').mkdir(parents=True)
            with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',return_value=[]) as api:connect.configure_root_folders('/fake',t,False,('sonarr',))
            self.assertEqual(api.call_count,1)

    def test_missing_host_rootfolder_is_failure(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(RuntimeError):connect.configure_root_folders('/fake',t,True,('sonarr',))

    def test_qbit_setup_creates_paths_and_categories(self):
        client=QbitFixture();qbit_setup.configure('admin','secret',True,client=client)
        self.assertEqual(client.preferences['save_path'],'/data/downloads')
        self.assertEqual(set(client.categories),{'tv','movies','music'});self.assertTrue(client.closed)

    def test_qbit_preview_has_no_configuration_writes(self):
        client=QbitFixture();qbit_setup.configure('admin','secret',False,client=client)
        self.assertFalse(any(c[0] in ('/app/setPreferences','/torrents/createCategory') for c in client.calls))

    def test_qbit_rerun_leaves_expected_settings(self):
        client=QbitFixture('/data/downloads',categories={c:{'savePath':'/data/downloads/'+c} for c in ('tv','movies','music')})
        qbit_setup.configure('admin','secret',True,client=client)
        self.assertFalse(any(c[0] in ('/app/setPreferences','/torrents/createCategory') for c in client.calls))

    def test_qbit_custom_path_is_preserved(self):
        client=QbitFixture('/my/custom')
        with self.assertRaises(RuntimeError):qbit_setup.configure('admin','secret',True,client=client)
        self.assertEqual(client.preferences['save_path'],'/my/custom');self.assertTrue(client.closed)

    def test_qbit_active_torrents_block_path_change(self):
        client=QbitFixture(torrents=[{'hash':'existing'}])
        with self.assertRaises(RuntimeError):qbit_setup.configure('admin','secret',True,client=client)
        self.assertEqual(client.preferences['save_path'],'/downloads/')

    def test_qbit_conflicting_category_is_preserved(self):
        client=QbitFixture(categories={'tv':{'savePath':'/custom/tv'}})
        with self.assertRaises(RuntimeError):qbit_setup.configure('admin','secret',True,client=client)
        self.assertEqual(client.categories['tv']['savePath'],'/custom/tv')

    def test_source_preview_does_not_copy(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';media=base/'media';src.mkdir();media.mkdir();(src/'movie.mkv').write_bytes(b'original')
            result=source_import.copy_source({'type':'local','category':'movies','path':str(src)},media,False)
            self.assertEqual(result['new_files'],1);self.assertFalse((media/'library').exists())

    def test_source_import_preserves_original_and_is_rerunnable(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';media=base/'media';src.mkdir();media.mkdir();(src/'music.flac').write_bytes(b'original')
            source={'type':'local','category':'music','path':str(src)}
            first=source_import.copy_source(source,media,True);second=source_import.copy_source(source,media,True)
            self.assertEqual(first['copied'],1);self.assertEqual(second['copied'],0)
            self.assertEqual((src/'music.flac').read_bytes(),b'original')
            self.assertEqual((Path(first['destination'])/'music.flac').read_bytes(),b'original')

    def test_source_import_never_overwrites_changed_file(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';media=base/'media';src.mkdir();media.mkdir();file=src/'video.mkv';file.write_bytes(b'old')
            source={'type':'local','category':'movies','path':str(src)};first=source_import.copy_source(source,media,True);file.write_bytes(b'new')
            with self.assertRaises(RuntimeError):source_import.copy_source(source,media,True)
            self.assertEqual((Path(first['destination'])/'video.mkv').read_bytes(),b'old')

    def test_remote_reference_without_mount_is_incomplete(self):
        with self.assertRaises(RuntimeError):source_import.source_folder({'type':'remote','uri':'smb://nas/videos'})

    def test_remote_local_filesystem_does_not_count_as_network_mount(self):
        with tempfile.TemporaryDirectory() as t:
            with patch.object(source_import.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='ext4')):
                with self.assertRaises(RuntimeError):source_import.source_folder({'type':'remote','mounted_path':t})

    def test_remote_verified_network_mount_is_accepted(self):
        with tempfile.TemporaryDirectory() as t:
            with patch.object(source_import.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='cifs')):
                self.assertEqual(source_import.source_folder({'type':'remote','mounted_path':t}),Path(t))

    def test_source_import_blocks_symlink_escape(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';media=base/'media';src.mkdir();media.mkdir();(src/'link').symlink_to(base)
            with self.assertRaises(RuntimeError):source_import.copy_source({'type':'local','category':'movies','path':str(src)},media,True)

    def test_source_import_blocks_managed_library_as_source(self):
        with tempfile.TemporaryDirectory() as t:
            media=Path(t);src=media/'library'/'tv'/'existing';src.mkdir(parents=True)
            with self.assertRaises(RuntimeError):source_import.copy_source({'type':'local','category':'tv','path':str(src)},media,True)

    def test_source_indexer_is_reference_not_media_copy(self):
        with tempfile.TemporaryDirectory() as t:
            result=source_import.copy_source({'type':'indexer','category':'tv','uri':'https://example.test/'},t,True)
            self.assertEqual(result['status'],'reference_only')

    def test_source_copy_checks_space_before_creating_destinations(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);src=base/'source';media=base/'media';src.mkdir();media.mkdir();(src/'movie.mkv').write_bytes(b'large')
            with patch.object(source_import.shutil,'disk_usage',return_value=SimpleNamespace(free=1)):
                with self.assertRaises(RuntimeError):source_import.copy_source({'type':'local','category':'movies','path':str(src)},media,True)
            self.assertFalse((media/'library').exists())

    def test_inventory_rejects_api_key_url_query(self):
        with self.assertRaises(ValueError):wizard.validate_source({'type':'indexer','category':'movies','uri':'https://example.test/?apikey=secret'})

if __name__=='__main__':unittest.main()

class ControllerStateTests(unittest.TestCase):
    def test_incomplete_onboarding_state_can_be_retried(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);mods=base/'modules';mods.mkdir()
            (mods/'onboarding.py').write_text("DEPENDENCIES=[]\ndef run(ctx):\n    return {'status':'needs_setup','reason':'account setup'}\n")
            config=base/'config.json';config.write_text(json.dumps(CONFIG))
            args=SimpleNamespace(config=str(config),state_dir=str(base/'state'),profile=None,module='onboarding',retry_failed=False,check=False,dry_run=False,apply=True)
            with redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,base),1)
            state=json.loads((base/'state'/'state.json').read_text());self.assertEqual(state['onboarding']['status'],'needs_setup')
            (mods/'onboarding.py').write_text("DEPENDENCIES=[]\ndef run(ctx):\n    return None\n")
            args.module=None;args.retry_failed=True
            with redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,base),0)
            self.assertEqual(json.loads((base/'state'/'state.json').read_text())['onboarding']['status'],'success')

    def test_default_installer_state_moves_to_ssd_after_storage(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);mods=base/'modules';mods.mkdir();ssd=base/'ssd';ssd.mkdir()
            (mods/'storage.py').write_text('DEPENDENCIES=[]\ndef run(ctx):\n    return None\n')
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=str(ssd);cfg['docker']['persistent_root']=str(ssd)
            config=base/'config.json';config.write_text(json.dumps(cfg));default=base/'os-state'
            args=SimpleNamespace(config=str(config),state_dir=str(default),profile=None,module='storage',retry_failed=False,check=False,dry_run=False,apply=True)
            with patch.object(installer,'DEFAULT_STATE',default),patch.object(installer.os.path,'ismount',return_value=False),redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,base),0)
            state=ssd/'installer'/'state'/'state.json';self.assertEqual(json.loads(state.read_text())['storage']['status'],'success')
            self.assertTrue((ssd/'installer'/'state'/'install.log').is_file())
            self.assertEqual(state.stat().st_mode&0o777,0o600)

    def test_onboarding_calls_root_setup_and_updates_final_report(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);ssd=base/'ssd';hdd=base/'hdd';ssd.mkdir();hdd.mkdir()
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=str(ssd);cfg['media_mount']=str(hdd);cfg['media']['services']=['sonarr']
            config=base/'config.json';config.write_text(json.dumps(cfg))
            with patch.object(board,'mount_ok',return_value=True),patch.object(board,'report',return_value=[]),patch.object(board.subprocess,'call',return_value=0) as call:
                self.assertEqual(board.main(['--config',str(config),'--configure','--skip-http']),1)
            args=call.call_args.args[0];self.assertIn('--root-folders',args);self.assertIn('--media-root',args)
            report=json.loads((ssd/'reports'/'onboarding.json').read_text())
            self.assertEqual(next(x['status'] for x in report['checks'] if x['check']=='connector'),'pass')
            self.assertEqual(next(x['status'] for x in report['checks'] if x['check']=='end_to_end_media'),'needs_setup')

class QbitHTTPTests(unittest.TestCase):
    def setUp(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading,urllib.parse
        owner=self;self.calls=[];self.preferences={'save_path':'/downloads/'};self.categories={};self.reject=False
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):self.respond()
            def do_GET(self):self.respond()
            def log_message(self,*args):pass
            def respond(self):
                path=self.path.removeprefix('/api/v2');form=urllib.parse.parse_qs(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode())
                owner.calls.append((path,self.headers.get('Cookie'),self.headers.get('Referer')))
                if path=='/auth/login':
                    self.send_response(200)
                    if not owner.reject:self.send_header('Set-Cookie','SID=fixture-session; Path=/')
                    self.end_headers();self.wfile.write(b'Fails.' if owner.reject else b'Ok.');return
                if 'SID=fixture-session' not in (self.headers.get('Cookie') or ''):
                    self.send_response(403);self.end_headers();return
                response=''
                if path=='/app/preferences':response=json.dumps(owner.preferences)
                elif path=='/torrents/categories':response=json.dumps(owner.categories)
                elif path=='/torrents/info':response='[]'
                elif path=='/app/setPreferences':owner.preferences.update(json.loads(form['json'][0]))
                elif path=='/torrents/createCategory':owner.categories[form['category'][0]]={'savePath':form['savePath'][0]}
                self.send_response(200);self.end_headers();self.wfile.write(response.encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def test_real_cookie_authentication_preferences_and_category_setup(self):
        client=qbit_setup.Client(self.server.server_port)
        qbit_setup.configure('fixture-user','fixture-password',True,client=client)
        self.assertEqual(self.preferences['save_path'],'/data/downloads');self.assertEqual(set(self.categories),{'tv','movies','music'})
        self.assertTrue(all(c[2]==f'http://127.0.0.1:{self.server.server_port}' for c in self.calls))
        self.assertFalse(list(client.jar))
    def test_login_failure_makes_no_preferences_changes(self):
        self.reject=True;client=qbit_setup.Client(self.server.server_port)
        with self.assertRaises(RuntimeError):qbit_setup.configure('fixture-user','fixture-password',True,client=client)
        self.assertEqual(self.preferences['save_path'],'/downloads/');self.assertFalse(self.categories)

class SourceWizardHandoffTests(unittest.TestCase):
    def test_wizard_records_explicit_copy_choice(self):
        answers=iter(['y','movies','local','Existing movies','/mnt/old-movies','y','n'])
        result=wizard.collect(input_fn=lambda prompt:next(answers))
        self.assertTrue(result['sources'][0]['copy_to_library'])
    def test_wizard_copy_defaults_to_reference_only(self):
        answers=iter(['y','tv','local','TV archive','/mnt/old-tv','','n'])
        result=wizard.collect(input_fn=lambda prompt:next(answers))
        self.assertFalse(result['sources'][0]['copy_to_library'])
    def test_onboarding_wizard_only_imports_selected_sources(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);ssd=base/'ssd';hdd=base/'hdd';ssd.mkdir();hdd.mkdir()
            cfg=copy.deepcopy(CONFIG);cfg['data_mount']=str(ssd);cfg['media_mount']=str(hdd);cfg['media']['services']=['sonarr']
            config=base/'config.json';config.write_text(json.dumps(cfg))
            with patch.object(board,'mount_ok',return_value=True),patch.object(board,'report',return_value=[]),patch.object(board.subprocess,'call',return_value=0) as call:
                board.main(['--config',str(config),'--configure','--sources','--skip-http','--apply'])
            commands=[c.args[0] for c in call.call_args_list]
            imports=next(cmd for cmd in commands if any(str(x).endswith('source_import.py') for x in cmd))
            self.assertIn('--selected-only',imports);self.assertIn('--apply',imports)
    def test_lidarr_rootfolder_uses_live_profiles_and_empty_tags(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/'library'/'music').mkdir(parents=True);folders=[];posted=[]
            def api(app,key,endpoint,method='GET',payload=None,timeout=12):
                if method=='POST':posted.append(payload);folders.append(payload);return payload
                if endpoint=='/qualityprofile':return [{'id':2,'name':'Configured quality'}]
                if endpoint=='/metadataprofile':return [{'id':3,'name':'Configured metadata'}]
                return folders
            with patch.object(connect,'app_key',return_value='key'),patch.object(connect,'request',side_effect=api):connect.configure_root_folders(t,t,True,('lidarr',))
            self.assertEqual(posted[0]['defaultQualityProfileId'],2);self.assertEqual(posted[0]['defaultMetadataProfileId'],3);self.assertEqual(posted[0]['defaultTags'],[])

class UpgradeOptInTests(unittest.TestCase):
    def test_old_config_does_not_enable_new_optional_onboarding_under_all(self):
        with tempfile.TemporaryDirectory() as t:
            cfg=copy.deepcopy(CONFIG);cfg['modules'].pop('onboarding');cfg['modules'].pop('recovery')
            config=Path(t)/'config.json';config.write_text(json.dumps(cfg));output=io.StringIO()
            with redirect_stdout(output):self.assertEqual(installer.main(['--all','--check','--config',str(config),'--state-dir',str(Path(t)/'state')]),0)
            self.assertNotIn('onboarding:',output.getvalue());self.assertNotIn('media: Guarded',output.getvalue())
            self.assertIn('recovery:',output.getvalue())
