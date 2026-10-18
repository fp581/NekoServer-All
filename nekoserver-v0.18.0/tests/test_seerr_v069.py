import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import seerr_providers as provider
import media_libraries
from modules import recovery,onboarding
from neko_common import VERSION
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('board069',ROOT/'onboard.py');board=importlib.util.module_from_spec(spec);spec.loader.exec_module(board)

class Manager:
    def __init__(self,app):
        self.app=app;self.headers={};self.calls=[];self.closed=False;self.version='4.0.1' if app=='sonarr' else '6.0.1';self.base='';self.quality=[{'id':7,'name':'1080p'}];self.roots=[{'id':2,'path':provider.SPECS[app][1],'accessible':True}]
    def call(self,endpoint,method='GET',payload=None):
        self.calls.append((endpoint,method,payload))
        if endpoint=='/system/status':return {'version':self.version,'urlBase':self.base}
        if endpoint=='/rootfolder':return copy.deepcopy(self.roots)
        if endpoint=='/qualityprofile':return copy.deepcopy(self.quality)
        raise AssertionError(endpoint)
    def close(self):self.closed=True;self.headers.clear()

class Seerr:
    def __init__(self,managers):
        self.managers=managers;self.headers={};self.values={'sonarr':[],'radarr':[]};self.calls=[];self.closed=False;self.initialized=True;self.retained=True;self.reject=set();self.mismatch=set();self.race=False;self.language=[{'id':3,'name':'English'}];self.bad_readback=False
    def call(self,endpoint,method='GET',payload=None):
        self.calls.append((endpoint,method,payload))
        if endpoint=='/settings/public':return {'initialized':self.initialized}
        app=endpoint.split('/')[2]
        if endpoint.endswith('/test'):
            if app in self.reject:raise RuntimeError('SECRET_API_TOKEN')
            manager=self.managers[app];response={'profiles':copy.deepcopy(manager.quality),'rootFolders':copy.deepcopy(manager.roots),'urlBase':manager.base,'languageProfiles':copy.deepcopy(self.language) if manager.version.startswith('3.') else None}
            if app in self.mismatch:response['rootFolders']=[]
            if self.bad_readback and self.values[app]:response['profiles']=[]
            return response
        if method=='POST':
            if self.retained:self.values[app].append(dict(payload,id=1))
            return dict(payload,id=1)
        if self.race and sum(c[:2]==(endpoint,'GET') for c in self.calls)>1:
            self.values[app].append({'id':99,'is4k':False,'isDefault':True})
        return copy.deepcopy(self.values[app])
    def close(self):self.closed=True;self.headers.clear()

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.managers={a:Manager(a) for a in provider.SPECS};self.seerr=Seerr(self.managers)
        for app in ('sonarr','radarr','seerr'):(self.root/'appdata'/app).mkdir(parents=True)
        for app in self.managers:(self.root/'appdata'/app/'config.xml').write_text('<Config><ApiKey>KEY-'+app+'</ApiKey></Config>')
        (self.root/'appdata/seerr/settings.json').write_text(json.dumps({'main':{'apiKey':'PRIVATE-KEY'}}))
    def tearDown(self):self.tmp.cleanup()
    def run_setup(self,apply=True,apps=('seerr','sonarr','radarr'),answer='1'):
        with redirect_stdout(io.StringIO()):return provider.configure(self.root,apply,apps,factory=lambda a:self.seerr if a=='seerr' else self.managers[a],prompt=lambda _:answer)
    def created(self,app):return self.seerr.values[app][0]
    def test_create_default_providers_test_and_readback(self):
        rows=self.run_setup();self.assertEqual([r['status'] for r in rows],['pass','pass'])
        for app in self.managers:
            value=self.created(app);self.assertEqual(value['hostname'],app);self.assertEqual(value['port'],provider.SPECS[app][0]);self.assertEqual(value['activeDirectory'],provider.SPECS[app][1]);self.assertEqual(value['activeProfileId'],7);self.assertTrue(value['isDefault']);self.assertFalse(value['is4k'] or value['syncEnabled'] or value['preventSearch'])
            self.assertEqual(sum(c[0]=='/settings/'+app+'/test' for c in self.seerr.calls),2)
        self.assertNotIn('activeLanguageProfileId',self.created('sonarr'));self.assertEqual(self.created('radarr')['minimumAvailability'],'released');self.assertTrue(self.created('sonarr')['enableSeasonFolders'])
        self.assertTrue(self.seerr.closed and all(m.closed for m in self.managers.values()));self.assertNotIn('KEY',json.dumps(rows))
    def test_rerun_preserves_existing_provider_and_does_not_prompt(self):
        self.run_setup();before=copy.deepcopy(self.seerr.values);self.seerr.calls=[]
        with redirect_stdout(io.StringIO()):rows=provider.configure(self.root,True,factory=lambda a:self.seerr if a=='seerr' else self.managers[a],prompt=lambda _:self.fail('prompted'))
        self.assertEqual(self.seerr.values,before);self.assertEqual([r['status'] for r in rows],['pass','pass']);self.assertFalse(any(c[1]=='POST' and not c[0].endswith('/test') for c in self.seerr.calls))
    def test_preview_only_tests_connections(self):
        rows=self.run_setup(False);self.assertEqual([r['status'] for r in rows],['needs_setup']*2);self.assertFalse(any(self.seerr.values.values()));self.assertTrue(all(c[1]=='GET' or c[0].endswith('/test') for c in self.seerr.calls))
    def test_multiple_profiles_select_live_id(self):
        self.managers['sonarr'].quality.append({'id':23,'name':'Anime 1080p'});self.run_setup(answer='2');self.assertEqual(self.created('sonarr')['activeProfileId'],23)
    def test_skipped_profile_does_not_block_other_provider(self):
        self.managers['sonarr'].quality.append({'id':23,'name':'Anime'});rows=self.run_setup(answer='');self.assertEqual([r['status'] for r in rows],['needs_setup','pass']);self.assertFalse(self.seerr.values['sonarr'])
    def test_invalid_profile_choice_preserves_settings(self):
        self.managers['sonarr'].quality.append({'id':23,'name':'Anime'});self.assertEqual(self.run_setup(answer='99')[0]['status'],'needs_setup')
    def test_sonarr_v3_uses_live_language_profile(self):
        self.managers['sonarr'].version='3.0.1';self.run_setup();self.assertEqual(self.created('sonarr')['activeLanguageProfileId'],3)
    def test_sonarr_v3_missing_language_profiles_stays_incomplete(self):
        self.managers['sonarr'].version='3.0.1';self.seerr.language=[];self.assertEqual(self.run_setup()[0]['status'],'needs_setup')
    def test_unsupported_sonarr_version_preserved(self):
        self.managers['sonarr'].version='5.0.0';self.assertEqual(self.run_setup()[0]['status'],'needs_setup')
    def test_custom_default_host_preserved(self):
        self.run_setup();self.created('sonarr')['hostname']='other-server';before=copy.deepcopy(self.seerr.values);self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertEqual(self.seerr.values,before)
    def test_nondefault_standard_provider_not_promoted(self):
        self.run_setup();self.created('sonarr')['isDefault']=False;self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertFalse(self.created('sonarr')['isDefault'])
    def test_existing_key_not_silently_rotated(self):
        self.run_setup();self.created('sonarr')['apiKey']='CUSTOM-KEY';self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertEqual(self.created('sonarr')['apiKey'],'CUSTOM-KEY')
    def test_remote_profile_mismatch_refuses_creation(self):
        original=self.seerr.call
        def different_profile(endpoint,method='GET',payload=None):
            result=original(endpoint,method,payload)
            if endpoint=='/settings/sonarr/test':result['profiles']=[{'id':99,'name':'Different server profile'}]
            return result
        with patch.object(self.seerr,'call',side_effect=different_profile):rows=self.run_setup()
        self.assertEqual([r['status'] for r in rows],['needs_setup','pass']);self.assertFalse(self.seerr.values['sonarr'])
    def test_4k_default_preserved_when_standard_added(self):
        original={'id':99,'is4k':True,'isDefault':True,'hostname':'4k-server'};self.seerr.values['radarr'].append(copy.deepcopy(original));self.run_setup();self.assertEqual(self.seerr.values['radarr'][0],original);self.assertTrue(self.seerr.values['radarr'][1]['isDefault'])
    def test_duplicate_standard_providers_refused(self):
        self.run_setup();self.seerr.values['sonarr'].append(copy.deepcopy(self.created('sonarr')));self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertEqual(len(self.seerr.values['sonarr']),2)
    def test_custom_root_preserved(self):
        self.run_setup();self.created('sonarr')['activeDirectory']='/custom';self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertEqual(self.created('sonarr')['activeDirectory'],'/custom')
    def test_stale_quality_profile_not_replaced(self):
        self.run_setup();self.created('radarr')['activeProfileId']=999;self.assertEqual(self.run_setup()[1]['status'],'needs_setup');self.assertEqual(self.created('radarr')['activeProfileId'],999)
    def test_search_disabled_not_reported_ready(self):
        self.run_setup();self.created('sonarr')['preventSearch']=True;self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertTrue(self.created('sonarr')['preventSearch'])
    def test_missing_root_blocks_only_its_provider(self):
        self.managers['sonarr'].roots=[];rows=self.run_setup();self.assertEqual([r['status'] for r in rows],['needs_setup','pass'])
    def test_inaccessible_root_refused(self):
        self.managers['radarr'].roots[0]['accessible']=False;self.assertEqual(self.run_setup()[1]['status'],'needs_setup')
    def test_seerr_connection_failure_redacted_and_independent(self):
        self.seerr.reject={'sonarr'};rows=self.run_setup();self.assertEqual([r['status'] for r in rows],['fail','pass']);self.assertNotIn('SECRET',json.dumps(rows))
    def test_seerr_root_mismatch_refused(self):
        self.seerr.mismatch={'sonarr'};self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertFalse(self.seerr.values['sonarr'])
    def test_readback_not_retained_fails(self):
        self.seerr.retained=False;self.assertEqual([r['status'] for r in self.run_setup()],['fail']*2)
    def test_retained_connection_failure_is_not_success(self):
        self.seerr.bad_readback=True;self.assertEqual([r['status'] for r in self.run_setup()],['fail']*2)
    def test_settings_race_refuses_write(self):
        self.seerr.race=True;self.assertEqual([r['status'] for r in self.run_setup()],['needs_setup']*2);self.assertFalse(any(c[1]=='POST' and not c[0].endswith('/test') for c in self.seerr.calls))
    def test_custom_base_url_passed_to_seerr(self):
        self.managers['sonarr'].base='/sonarr';self.run_setup();self.assertEqual(self.created('sonarr')['baseUrl'],'/sonarr')
    def test_unsafe_base_url_refused(self):
        self.managers['sonarr'].base='//external';self.assertEqual(self.run_setup()[0]['status'],'needs_setup')
    def test_uninitialized_seerr_no_manager_requests(self):
        self.seerr.initialized=False;self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertTrue(all(not x.calls for x in self.managers.values()));self.assertTrue(self.seerr.closed)
    def test_missing_seerr_key_no_api_calls(self):
        (self.root/'appdata/seerr/settings.json').unlink();self.assertEqual(self.run_setup()[0]['status'],'needs_setup');self.assertFalse(self.seerr.calls)
    def test_symlink_manager_key_refused(self):
        p=self.root/'appdata/sonarr/config.xml';p.rename(p.with_suffix('.orig'));p.symlink_to(p.with_suffix('.orig'));self.assertEqual(self.run_setup()[0]['status'],'fail')
    def test_only_selected_manager_is_connected(self):
        rows=self.run_setup(apps=('seerr','sonarr'));self.assertEqual(len(rows),1);self.assertFalse(self.managers['radarr'].calls)
    def test_providers_absent_from_selection_needs_setup(self):
        self.assertEqual(self.run_setup(apps=('seerr',))[0]['status'],'needs_setup');self.assertFalse(self.seerr.calls)
    def test_onboarding_runs_providers_after_arr_and_retains_report(self):
        cfg=json.loads((ROOT/'config.json').read_text());cfg.update(data_mount=str(self.root),media_mount=str(self.root/'hdd'));cfg['media']['services']=['seerr','sonarr','radarr'];p=self.root/'config.json';p.write_text(json.dumps(cfg));events=[]
        def connector(cmd):events.append('arr');return 0
        def providers(*args):events.append('providers');return [provider.row('seerr:provider:sonarr','pass'),provider.row('seerr:provider:radarr','fail')]
        with patch.object(board,'mount_ok',return_value=True),patch.object(board,'report',return_value=[]),patch.object(board.subprocess,'call',side_effect=connector),patch.object(board.seerr_providers,'configure',side_effect=providers),redirect_stdout(io.StringIO()):self.assertEqual(board.main(['--config',str(p),'--configure','--request-providers','--apply','--skip-http']),2)
        self.assertEqual(events,['arr','providers']);rows=json.loads((self.root/'reports/onboarding.json').read_text())['checks'];self.assertEqual([r['status'] for r in rows if r['check'].startswith('seerr:provider:')],['pass','fail'])
    def test_provider_only_command_supported(self):
        cfg=json.loads((ROOT/'config.json').read_text());cfg.update(data_mount=str(self.root),media_mount=str(self.root/'hdd'));p=self.root/'config.json';p.write_text(json.dumps(cfg))
        with patch.object(board,'mount_ok',return_value=True),patch.object(board,'report',return_value=[]),patch.object(board.subprocess,'call',side_effect=AssertionError('connector')),patch.object(board.seerr_providers,'configure',return_value=[provider.row('seerr:provider:sonarr','pass')]) as call,redirect_stdout(io.StringIO()):self.assertEqual(board.main(['--config',str(p),'--request-providers','--apply','--skip-http']),1)
        self.assertTrue(call.call_args.args[1])
    def test_interactive_module_adds_providers_headless_does_not(self):
        cfg=json.loads((ROOT/'config.json').read_text());cfg['data_mount']=str(self.root);release=self.root/'installer/releases'/('v'+VERSION);release.mkdir(parents=True);(release/'onboard.py').write_text('fixture');ctx=SimpleNamespace(config=cfg,apply=True,log=lambda x:None)
        for interactive in (True,False):
            with patch.object(onboarding.sys.stdin,'isatty',return_value=interactive),patch.object(onboarding.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as call:onboarding.run(ctx)
            self.assertEqual('--request-providers' in call.call_args.args[0],interactive)
    def test_preserved_helper_can_import_from_new_bundle(self):
        import subprocess,sys
        with patch.object(recovery.os.path,'ismount',return_value=True):dest=recovery.preserve(self.root/'ssd',ROOT,json.loads((ROOT/'config.json').read_text()))
        result=subprocess.run([sys.executable,'-c','import seerr_providers; import media_libraries'],cwd=dest,capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)


class ProviderHTTPTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.managers={a:Manager(a) for a in provider.SPECS};self.seerr=Seerr(self.managers);self.calls=[];self.servers=[];self.threads=[];ports={};owner=self
        for app in ('seerr','sonarr','radarr'):
            folder=self.root/'appdata'/app;folder.mkdir(parents=True)
            if app=='seerr':(folder/'settings.json').write_text(json.dumps({'main':{'apiKey':'PRIVATE-SEERR'}}))
            else:(folder/'config.xml').write_text('<Config><ApiKey>KEY-'+app+'</ApiKey></Config>')
            def handler_type(role):
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):self.respond()
                    def do_POST(self):self.respond()
                    def respond(self):
                        payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'null');owner.calls.append((role,self.path,self.command,self.headers.get('X-Api-Key'),payload))
                        prefix='/api/v1' if role=='seerr' else '/api/v3';assert self.path.startswith(prefix)
                        fixture=owner.seerr if role=='seerr' else owner.managers[role]
                        try:value=fixture.call(self.path[len(prefix):],self.command,payload);code=200
                        except RuntimeError:value={'error':'SECRET_REMOTE_RESPONSE'};code=500
                        self.send_response(code);self.end_headers();self.wfile.write(json.dumps(value).encode())
                    def log_message(self,*args):pass
                return Handler
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_type(app));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();self.servers.append(server);self.threads.append(thread);ports[app]=server.server_port
        self.ports=patch.dict(media_libraries.PORTS,ports);self.ports.start()
    def tearDown(self):
        self.ports.stop()
        for server in self.servers:server.shutdown();server.server_close()
        for thread in self.threads:thread.join()
        self.tmp.cleanup()
    def test_real_http_provisioning_api_versions_keys_and_rerun(self):
        rows=provider.configure(self.root,True,prompt=lambda _:self.fail('unexpected prompt'))
        self.assertEqual([r['status'] for r in rows],['pass','pass'])
        for app in ('sonarr','radarr'):
            posts=[c for c in self.calls if c[0]=='seerr' and c[1]=='/api/v1/settings/'+app and c[2]=='POST'];self.assertEqual(len(posts),1);self.assertEqual(posts[0][3],'PRIVATE-SEERR');self.assertEqual(posts[0][4]['hostname'],app)
            local=[c for c in self.calls if c[0]==app];self.assertTrue(local);self.assertTrue(all(c[3]=='KEY-'+app and c[1].startswith('/api/v3/') for c in local))
        self.calls=[];rows=provider.configure(self.root,True,prompt=lambda _:self.fail('prompt on rerun'));self.assertTrue(all(r['status']=='pass' for r in rows));self.assertFalse(any(c[2]=='POST' and not c[1].endswith('/test') for c in self.calls));self.assertNotIn('KEY',json.dumps(rows))
    def test_real_http_test_failure_redacted_other_provider_continues(self):
        self.seerr.reject={'sonarr'};rows=provider.configure(self.root,True,prompt=lambda _:self.fail('unexpected prompt'));self.assertEqual([r['status'] for r in rows],['fail','pass']);self.assertNotIn('SECRET',json.dumps(rows));self.assertFalse(self.seerr.values['sonarr']);self.assertTrue(self.seerr.values['radarr'])

if __name__=='__main__':unittest.main()
