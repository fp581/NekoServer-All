"""Authenticated music setup over real local HTTP and native media-audit integration."""
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch
from types import SimpleNamespace

import music_client
import installer
from modules import music, media
import test_audit_v0101 as existing

ROOT=Path(__file__).resolve().parents[1]
CFG=json.loads((ROOT/'config.json').read_text())

class LocalMusic(unittest.TestCase):
    def setUp(self):
        self.values={'password':'listener-secret','admin':False,'empty':False,'folders':True,'malformed':False,'seen':[]}
        owner=self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                parsed=urllib.parse.urlsplit(self.path);query=urllib.parse.parse_qs(parsed.query);endpoint=parsed.path.split('/')[-1].split('.')[0]
                owner.values['seen'].append(endpoint)
                token=hashlib.md5((owner.values['password']+query.get('s',[''])[0]).encode(),usedforsecurity=False).hexdigest()
                value={'subsonic-response':{'status':'ok'}};response=value['subsonic-response']
                if query.get('u')!=['listener'] or query.get('t')!=[token]:response.update(status='failed',error={'code':40,'message':'PRIVATE-ERROR'})
                elif endpoint=='getUser':response['user']={'username':'listener','adminRole':owner.values['admin']}
                elif endpoint=='getMusicFolders':response['musicFolders']={'musicFolder':[{'id':1,'name':'Music'}] if owner.values['folders'] else []}
                elif endpoint=='getAlbumList2':response['albumList2']={'album':[] if owner.values['empty'] else [{'id':'album1'}]}
                if owner.values['malformed']:value={'subsonic-response':{'status':'ok','user':[]}}
                raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(lambda:(self.server.shutdown(),self.server.server_close()))
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.cred=music_client.credential('listener',self.values['password'])
    def test_authentication_and_catalogue_without_saving_plaintext_password(self):
        result=music_client.inspect(self.cred,self.base)
        self.assertEqual(result['status'],'pass');self.assertFalse(result['playback_verified']);self.assertFalse(result['library_empty'])
        self.assertNotIn(self.values['password'],json.dumps(self.cred));self.assertEqual(self.values['seen'],['ping','getUser','getMusicFolders','getAlbumList2'])
    def test_empty_library_is_ready_for_import_not_fake_playback(self):
        self.values['empty']=True;r=music_client.inspect(self.cred,self.base);self.assertTrue(r['library_empty']);self.assertFalse(r['playback_verified'])
    def test_http_200_with_failed_authentication_does_not_pass(self):
        self.values['password']='changed'
        with self.assertRaises(RuntimeError):music_client.inspect(self.cred,self.base)
    def test_administrator_account_is_refused(self):
        self.values['admin']=True
        with self.assertRaises(RuntimeError):music_client.inspect(self.cred,self.base)
    def test_missing_library_and_malformed_payload_fail(self):
        self.values['folders']=False
        with self.assertRaises(RuntimeError):music_client.inspect(self.cred,self.base)
        self.values['malformed']=True
        with self.assertRaises(RuntimeError):music_client.inspect(self.cred,self.base)
    def test_private_token_is_published_only_after_successful_verification(self):
        with tempfile.TemporaryDirectory() as t:
            cfg={'data_mount':t}
            with patch.object(music_client,'inspect',return_value={'status':'pass'}):music_client.save(cfg,self.cred)
            path=music_client.credential_path(cfg);before=path.read_bytes();self.assertEqual(path.stat().st_mode&0o777,0o600)
            with patch.object(music_client,'inspect',side_effect=RuntimeError('bad account')):
                with self.assertRaises(RuntimeError):music_client.save(cfg,{**self.cred,'token':'c'*32})
            self.assertEqual(path.read_bytes(),before)
    def test_public_token_file_is_refused_before_any_request(self):
        with tempfile.TemporaryDirectory() as t:
            cfg={'data_mount':t};path=music_client.credential_path(cfg);path.parent.mkdir(parents=True);path.write_text(json.dumps(self.cred));path.chmod(0o644)
            with patch.object(music_client,'inspect',side_effect=AssertionError('network')):self.assertEqual(music_client.check(cfg)['status'],'fail')
    def test_invalid_token_and_remote_endpoint_are_refused(self):
        with self.assertRaises(RuntimeError):music_client.inspect({**self.cred,'token':'x'},self.base)
        with self.assertRaises(RuntimeError):music_client.inspect(self.cred,'https://remote.example')

class NativeMusic(unittest.TestCase):
    def setUp(self):
        if os.geteuid()!=0:self.skipTest('Private root fixture required')
        self.f=existing.NativeMedia('test_complete_chain_checks_saved_settings_and_live_connections');self.f.setUp();self.addCleanup(self.f.tearDown)
        self.value={'format':music_client.FORMAT,'user':'listener','salt':'a'*32,'token':'b'*32}
        path=music_client.credential_path(self.f.config);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(self.value));path.chmod(0o600)
        self.path=path
        for endpoint,extra,payload in [('ping','',{}),('getUser','&username=listener',{'user':{'username':'listener','adminRole':False}}),('getMusicFolders','',{'musicFolders':{'musicFolder':[{'id':1,'name':'Music'}]}}),('getAlbumList2','&type=newest&size=1',{'albumList2':{'album':[]}})]:
            url='/rest/'+endpoint+'.view?u=listener&t='+self.value['token']+'&s='+self.value['salt']+'&v=1.16.1&c=NekoServer&f=json'+extra
            self.f.responses[('navidrome',url)]=(200,{'subsonic-response':{'status':'ok',**payload}})
    def test_native_listener_check_passes_but_playback_remains_witnessed(self):
        r,report=self.f.run_audit();self.assertEqual(r.returncode,3,r.stderr)
        self.assertEqual(report['readiness']['navidrome:catalogue']['result'],'pass')
        self.assertEqual(report['readiness']['navidrome:authenticated_playback_or_transfer']['result'],'needs_setup')
        self.assertNotIn(self.value['token'],json.dumps(report)+r.stdout+r.stderr)
    def test_native_bad_credentials_fail_even_with_http_200(self):
        key=next(k for k in self.f.responses if k[0]=='navidrome' and '/rest/ping.' in k[1]);self.f.responses[key]=(200,{'subsonic-response':{'status':'failed'}})
        r,report=self.f.run_audit();self.assertEqual(r.returncode,2);self.assertEqual(report['readiness']['navidrome:catalogue']['result'],'fail')
    def test_native_public_credentials_are_never_used(self):
        self.path.chmod(0o644);r,report=self.f.run_audit();self.assertEqual(r.returncode,2)
        self.assertFalse(any('/rest/' in request[2] for request in self.f.requests))
    def test_native_missing_credentials_stays_pending(self):
        self.path.unlink();r,report=self.f.run_audit();self.assertEqual(r.returncode,3)
        self.assertEqual(report['readiness']['navidrome:catalogue']['result'],'needs_setup')

class MusicInstaller(unittest.TestCase):
    def test_profile_orders_deployment_before_music_and_keeps_vm_unnecessary(self):
        plan=installer.ordered(installer.load_modules(ROOT/'modules'),CFG['profiles']['music'])
        self.assertLess(plan.index('media'),plan.index('music'));self.assertIn('media_monitor',plan)
    def test_headless_music_install_does_not_invent_accounts(self):
        with tempfile.TemporaryDirectory() as t:
            cfg=copy.deepcopy(CFG);cfg['data_mount']=t;cfg['music']['create_desktop_shortcut']=False
            ctx=SimpleNamespace(config=cfg,apply=True,log=lambda _:None)
            with patch('neko_common.verify_storage'),patch.object(music.sys.stdin,'isatty',return_value=False):result=music.run(ctx)
            self.assertEqual(result['status'],'needs_setup');self.assertFalse(music_client.credential_path(cfg).exists())
    def test_music_requires_the_player_and_boolean_options(self):
        cfg=copy.deepcopy(CFG);cfg['media']['services']=['jellyfin']
        with self.assertRaises(RuntimeError):music.options(cfg)
        cfg=copy.deepcopy(CFG);cfg['music']['create_desktop_shortcut']='yes'
        with self.assertRaises(RuntimeError):music.options(cfg)
    def test_pristine_previous_manifest_migrates_but_custom_edits_are_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            data=Path(t)/'ssd';hdd=Path(t)/'hdd';data.mkdir();hdd.mkdir();cfg=copy.deepcopy(CFG);cfg.update(data_mount=str(data),media_mount=str(hdd));cfg['media']['services']=['navidrome']
            template=(ROOT/'compose/navidrome.yaml').read_text();old=template.replace('ND_MUSICFOLDER: "/music"\n      ND_DATAFOLDER: "/data"\n      ND_SCANNER_SCHEDULE: "0 * * * *"\n      ND_SCANNER_WATCHERWAIT: "10s"\n      ND_ENABLESHARING: "false"','ND_SCANSCHEDULE: "1h"')
            old=media.render(old,data,hdd,1000,1000,'Asia/Jerusalem','127.0.0.1')
            dest=data/'compose/navidrome/compose.yaml';dest.parent.mkdir(parents=True);dest.write_text(old)
            ctx=SimpleNamespace(config=cfg,apply=True,log=lambda _:None)
            with patch.object(media,'check_mount',side_effect=lambda p:str(p)),patch.object(media,'verify_storage'),patch.object(media.shutil,'which',return_value='/bin/true'),patch.object(media,'run_cmd',return_value=''),patch.object(media,'network_exists',return_value=True),patch.object(media.os,'chown'):
                media._run(ctx)
                self.assertIn('ND_SCANNER_WATCHERWAIT',dest.read_text());self.assertEqual((dest.parent/'compose.before-music-setup.yaml').read_text(),old)
                dest.write_text('CUSTOM')
                with self.assertRaises(RuntimeError):media._run(ctx)
                self.assertEqual(dest.read_text(),'CUSTOM')
