import copy
import importlib.util
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import media_libraries as lib
from modules import onboarding, recovery
from neko_common import VERSION

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('board068',ROOT/'onboard.py');board=importlib.util.module_from_spec(spec);spec.loader.exec_module(board)

class Fixture:
    def __init__(self,app):
        self.app=app;self.headers={};self.calls=[];self.closed=False;self.startup=True;self.admin=True;self.retained=True;self.values=[];self.server='server-neko';self.fail_sync=False
    def call(self,endpoint,method='GET',payload=None):
        self.calls.append((endpoint,method,payload))
        if endpoint=='/System/Info/Public':return {'Id':'server-neko','StartupWizardCompleted':self.startup}
        if endpoint=='/Users/AuthenticateByName':return {'AccessToken':'secret-token','User':{'Policy':{'IsAdministrator':self.admin}}}
        if endpoint=='/Library/VirtualFolders':return copy.deepcopy(self.values)
        if endpoint.startswith('/Library/VirtualFolders?'):
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)
            if self.retained:self.values.append({'Name':q['name'][0],'Locations':[q['paths'][0]],'CollectionType':q['collectionType'][0],'ItemId':'m' if q['collectionType'][0]=='movies' else 't'})
            return {}
        if endpoint=='/settings/jellyfin':return {'serverId':self.server}
        if endpoint=='/settings/jellyfin/library/sync':
            if self.fail_sync:raise RuntimeError('PRIVATE-SECRET')
            return self.values
        if endpoint=='/settings/jellyfin/library':return copy.deepcopy(self.values)
        if endpoint.startswith('/settings/jellyfin/library/'):
            if self.retained:
                for x in self.values:
                    if x['id']==endpoint.rsplit('/',1)[1]:x['enabled']=payload['enabled']
            return {}
        raise AssertionError((endpoint,method))
    def close(self):self.closed=True;self.headers.clear()

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.media=self.root/'hdd'
        for cat in ('movies','tv'):(self.media/'library'/cat).mkdir(parents=True)
        cfg=self.root/'appdata/seerr';cfg.mkdir(parents=True);(cfg/'settings.json').write_text(json.dumps({'main':{'apiKey':'secret-api'}}))
        self.jf=Fixture('jellyfin');self.sr=Fixture('seerr');self.sr.values=[{'id':'m','enabled':False},{'id':'t','enabled':False},{'id':'custom','enabled':False}]
    def tearDown(self):self.tmp.cleanup()
    def run_setup(self,apply=True,apps=('jellyfin','seerr')):
        return lib.configure(self.root,self.media,apply,apps,factory=lambda a:self.jf if a=='jellyfin' else self.sr,username=lambda _: 'neko',password=lambda _: 'PRIVATE-PASSWORD')
    def test_create_readback_sync_enable_and_preserve_other_library(self):
        rows=self.run_setup();self.assertEqual([x['status'] for x in rows[:-1]],['pass']*6)
        self.assertFalse(self.sr.values[-1]['enabled']);self.assertEqual(self.sr.calls[1][0:2],('/settings/jellyfin/library/sync','POST'))
        self.assertEqual([c[1] for c in self.sr.calls if c[0].endswith(('/m','/t'))],['PUT','PUT'])
        self.assertTrue(self.jf.closed and self.sr.closed);self.assertFalse(self.jf.headers or self.sr.headers)
        self.assertNotIn('PRIVATE',json.dumps(rows));self.assertNotIn('secret',json.dumps(rows))
    def test_rerun_does_not_duplicate_or_reset(self):
        self.run_setup();self.jf.calls=[];self.run_setup()
        self.assertFalse(any(c[1]=='POST' and 'VirtualFolders?' in c[0] for c in self.jf.calls))
        self.assertFalse(any('Startup' in c[0] or 'Password' in c[0] for c in self.jf.calls));self.assertEqual(len(self.jf.values),2)
    def test_fresh_wizard_stops_before_password_prompt(self):
        self.jf.startup=False
        rows=lib.configure(self.root,self.media,True,factory=lambda a:self.jf,username=lambda _:self.fail('prompted'),password=lambda _:self.fail('prompted'))
        self.assertEqual(rows[0]['status'],'needs_setup');self.assertEqual(len(self.jf.calls),1);self.assertTrue(self.jf.closed)
    def test_nonadministrator_stops_before_library_writes(self):
        self.jf.admin=False;rows=self.run_setup();self.assertEqual(rows[0]['status'],'needs_setup');self.assertFalse(self.jf.values);self.assertFalse(self.sr.calls)
    def test_skip_does_not_claim_ready(self):
        rows=lib.configure(self.root,self.media,True,factory=lambda a:self.jf,username=lambda _: '',password=lambda _:self.fail('prompted'))
        self.assertEqual(rows[0]['status'],'needs_setup');self.assertEqual(len(self.jf.calls),1)
    def test_blank_password_refused(self):
        rows=lib.configure(self.root,self.media,True,factory=lambda a:self.jf,username=lambda _: 'neko',password=lambda _: '')
        self.assertEqual(rows[0]['status'],'needs_setup');self.assertEqual(len(self.jf.calls),1)
    def test_preview_no_library_mutations(self):
        rows=self.run_setup(False);self.assertFalse(self.jf.values);self.assertFalse(any(c[1]!='GET' for c in self.sr.calls));self.assertTrue(any(r['status']=='needs_setup' for r in rows))
    def test_custom_library_name_preserved_other_category_continues(self):
        self.jf.values=[{'Name':'Movies','Locations':['/custom'],'CollectionType':'movies','ItemId':'custom'}]
        rows=self.run_setup();self.assertEqual(rows[1]['status'],'needs_setup');self.assertEqual(rows[2]['status'],'pass');self.assertEqual(self.jf.values[0]['Locations'],['/custom'])
    def test_wrong_type_at_expected_path_refused(self):
        self.jf.values=[{'Name':'Mine','Locations':['/data/movies'],'CollectionType':'music','ItemId':'custom'}]
        rows=self.run_setup();self.assertEqual(rows[1]['status'],'needs_setup');self.assertEqual(len(self.jf.values),2)
    def test_existing_renamed_correct_libraries_preserved(self):
        self.jf.values=[{'Name':'My Movies','Locations':['/data/movies/'],'CollectionType':'movies','ItemId':'m'},{'Name':'Anime','Locations':['/data/tv'],'CollectionType':'tvshows','ItemId':'t'}]
        self.run_setup();self.assertFalse(any(c[0].startswith('/Library/VirtualFolders?') for c in self.jf.calls))
    def test_duplicate_paths_refused(self):
        self.jf.values=[{'Name':str(i),'Locations':['/data/movies'],'CollectionType':'movies','ItemId':str(i)} for i in range(2)]
        self.assertEqual(self.run_setup()[1]['status'],'needs_setup')
    def test_missing_host_library_not_created_via_api(self):
        (self.media/'library/movies').rmdir();rows=self.run_setup();self.assertEqual(rows[1]['status'],'needs_setup');self.assertFalse(any(x['CollectionType']=='movies' for x in self.jf.values))
    def test_creation_not_retained_is_failure(self):
        self.jf.retained=False;rows=self.run_setup();self.assertEqual(rows[1]['status'],'fail');self.assertEqual(rows[2]['status'],'fail')
    def test_symlink_hdd_folder_rejected(self):
        (self.media/'library/movies').rmdir();(self.media/'library/movies').symlink_to(self.media/'library/tv');rows=self.run_setup();self.assertEqual(rows[1]['status'],'fail')
    def test_seerr_wrong_server_no_mutations(self):
        self.sr.server='other';rows=self.run_setup();self.assertEqual(rows[-1]['status'],'needs_setup');self.assertFalse(any(c[1]!='GET' for c in self.sr.calls))
    def test_seerr_missing_key_needs_setup(self):
        (self.root/'appdata/seerr/settings.json').unlink();rows=self.run_setup();self.assertEqual(rows[-1]['status'],'needs_setup');self.assertFalse(self.sr.calls)
    def test_seerr_symlink_settings_refused(self):
        p=self.root/'appdata/seerr/settings.json';p.rename(p.with_suffix('.orig'));p.symlink_to(p.with_suffix('.orig'));self.assertEqual(self.run_setup()[-1]['status'],'fail')
    def test_enable_not_retained_is_failure(self):
        self.sr.retained=False;self.assertEqual(self.run_setup()[-1]['status'],'fail')
    def test_sync_error_does_not_echo_api_body(self):
        self.sr.fail_sync=True;rows=self.run_setup();self.assertEqual(rows[-1]['status'],'fail');self.assertNotIn('PRIVATE',json.dumps(rows));self.assertTrue(self.sr.closed)
    def test_no_seerr_service_skips_its_api(self):
        rows=self.run_setup(apps=('jellyfin',));self.assertTrue(all(r['status']=='pass' for r in rows));self.assertFalse(self.sr.calls)
    def test_no_jellyfin_needs_setup(self):
        self.assertEqual(self.run_setup(apps=('seerr',))[0]['status'],'needs_setup');self.assertFalse(self.jf.calls)
    def test_onboarding_libraries_only_retains_results(self):
        config=json.loads((ROOT/'config.json').read_text());config.update(data_mount=str(self.root),media_mount=str(self.media));config['media']['services']=['jellyfin','seerr']
        p=self.root/'config.json';p.write_text(json.dumps(config))
        with patch.object(board,'mount_ok',return_value=True),patch.object(board,'report',return_value=[]),patch.object(board.media_libraries,'configure',return_value=[lib.row('jellyfin:movies','pass')]) as call,patch.object(board.subprocess,'call',side_effect=AssertionError('unexpected connector')),redirect_stdout(io.StringIO()):
            self.assertEqual(board.main(['--config',str(p),'--configure','--libraries','--apply','--skip-http']),1)
        self.assertTrue(call.call_args.args[2]);rows=json.loads((self.root/'reports/onboarding.json').read_text())['checks'];self.assertEqual(rows[0]['check'],'jellyfin:movies');self.assertFalse(any(x['check']=='connector' for x in rows))
    def test_preserved_bundle_includes_library_helper(self):
        with patch.object(recovery.os.path,'ismount',return_value=True):dest=recovery.preserve(self.root/'ssd',ROOT,json.loads((ROOT/'config.json').read_text()))
        self.assertTrue((dest/'media_libraries.py').is_file())

class LibraryHTTPTests(unittest.TestCase):
    def setUp(self):
        owner=self;self.calls=[];self.status=200;self.response={'ok':True};self.redirect=False
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def do_PUT(self):self.respond()
            def respond(self):
                body=self.rfile.read(int(self.headers.get('Content-Length',0)));owner.calls.append((self.path,self.command,dict(self.headers),body))
                self.send_response(302 if owner.redirect else owner.status)
                if owner.redirect:self.send_header('Location',owner.base+'/leak')
                self.end_headers();self.wfile.write(json.dumps(owner.response).encode())
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.base=f'http://127.0.0.1:{self.server.server_port}';self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def test_local_json_authentication_and_logout(self):
        c=lib.Client('jellyfin');c.base=self.base;c.headers['X-Emby-Token']='PRIVATE-TOKEN';self.assertEqual(c.call('/Users/AuthenticateByName','POST',{'Pw':'PRIVATE-PASSWORD'}),{'ok':True});c.close()
        self.assertEqual(self.calls[0][1],'POST');self.assertEqual(json.loads(self.calls[0][3])['Pw'],'PRIVATE-PASSWORD');self.assertEqual(self.calls[-1][0],'/Sessions/Logout');self.assertFalse(c.headers)
    def test_seerr_put_prefix_and_api_key(self):
        c=lib.Client('seerr');c.base=self.base;c.headers['X-Api-Key']='SECRET';c.call('/settings/jellyfin/library/m','PUT',{'enabled':True});self.assertEqual(self.calls[0][0],'/api/v1/settings/jellyfin/library/m');self.assertEqual(self.calls[0][1],'PUT');self.assertEqual(self.calls[0][2]['X-Api-Key'],'SECRET')
    def test_redirect_refused_without_secret_forwarding(self):
        self.redirect=True;c=lib.Client('seerr');c.base=self.base;c.headers['X-Api-Key']='SECRET'
        with self.assertRaisesRegex(RuntimeError,'HTTP 302'):c.call('/settings/jellyfin')
        self.assertEqual(len(self.calls),1)
    def test_error_body_not_exposed(self):
        self.status=500;self.response={'error':'PRIVATE-PASSWORD'};c=lib.Client('seerr');c.base=self.base
        with self.assertRaises(RuntimeError) as error:c.call('/settings/jellyfin')
        self.assertNotIn('PRIVATE',str(error.exception))
    def test_invalid_endpoint_refused_before_network(self):
        c=lib.Client('seerr');c.base=self.base
        with self.assertRaises(ValueError):c.call('//example.com/path')
        self.assertFalse(self.calls)

if __name__=='__main__':unittest.main()
