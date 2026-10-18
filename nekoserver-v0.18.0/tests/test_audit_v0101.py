"""Integration and permissions regressions from the complete-plan audit."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import job_status
import permission_checks
import native_runtime
import neko_common
import source_import
from modules import media_monitor

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'runtime/bin/nekoserver-runtime'
FIXTURE = Path(os.environ.get('NEKO_TEST_SERVER_BINARY', str(BIN)))
SECRET = 'a' * 32


def load(name):
    spec = importlib.util.spec_from_file_location('audit_' + name.replace('-', '_'), ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Permissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def test_new_directories_ignore_umask_without_changing_existing(self):
        parent = self.root / 'existing'
        parent.mkdir(mode=0o700)
        old = os.umask(0o077)
        try:
            p = neko_common.ensure_directory(parent / 'one/two', 0o755)
        finally:
            os.umask(old)
        self.assertEqual(p.stat().st_mode & 0o777, 0o755)
        self.assertEqual(p.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
    def test_permissions_detect_blocked_parent_without_chmod(self):
        parent=self.root/'private';parent.mkdir(mode=0o700)
        leaf=parent/'library';leaf.mkdir(mode=0o755)
        before=parent.stat().st_mode
        self.assertFalse(permission_checks.access(leaf,1000,{1000},5))
        self.assertEqual(parent.stat().st_mode,before)
    def test_permissions_detect_insufficient_write_access(self):
        self.root.chmod(0o755)
        leaf=self.root/'library';leaf.mkdir(mode=0o755)
        self.assertTrue(permission_checks.access(leaf,1000,{1000},5))
        self.assertFalse(permission_checks.access(leaf,1000,{1000},7))
    def test_permissions_reject_symlink_traversal(self):
        link=self.root/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(RuntimeError):permission_checks.access(link,1000,{1000},5)
    def test_directory_helper_rejects_symlinks(self):
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            neko_common.ensure_directory(link / 'nested')
    def test_imported_tree_has_shared_group_permissions_with_restrictive_umask(self):
        source = self.root / 'original'
        source.mkdir()
        (source / 'movie.mkv').write_bytes(b'original')
        media = self.root / 'hdd'
        (media / 'library/movies').mkdir(parents=True)
        old = os.umask(0o077)
        try:
            with patch.object(neko_common.os, 'chown') as owner, patch.object(source_import.os, 'fchown') as file_owner:
                result = source_import.copy_source({'type':'local','path':str(source),'category':'movies'}, media, True, 1000, 1000)
        finally:
            os.umask(old)
        target = Path(result['destination'])
        self.assertEqual(target.stat().st_mode & 0o2777, 0o2775)
        self.assertEqual((target / 'movie.mkv').stat().st_mode & 0o777, 0o664)
        owner.assert_called_with(target, 1000, 1000)
        self.assertEqual(file_owner.call_args.args[1:], (1000, 1000))
        self.assertEqual((source / 'movie.mkv').read_bytes(), b'original')
    def test_python_archive_is_readable_under_restrictive_umask(self):
        source = self.root / 'source'
        photo = source / 'upload/user/photo.jpg'
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b'photo')
        os.utime(photo, (time.time()-1200,)*2)
        target = self.root / 'archive'
        old = os.umask(0o077)
        try:
            load('photo-preserve.py').transfer(source, target, True)
        finally:
            os.umask(old)
        self.assertEqual((target/'upload/user').stat().st_mode & 0o777, 0o755)
        self.assertEqual((target/'upload/user/photo.jpg').stat().st_mode & 0o777, 0o644)
    def test_native_archive_is_readable_under_restrictive_umask(self):
        source = self.root / 'source'
        photo = source / 'upload/user/photo.jpg'
        photo.parent.mkdir(parents=True)
        photo.write_bytes(b'photo')
        os.utime(photo, (time.time()-1200,)*2)
        target = self.root / 'archive'
        result = subprocess.run([str(BIN),'archive','--source',str(source),'--archive',str(target),'--apply'], capture_output=True, umask=0o077)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((target/'upload/user').stat().st_mode & 0o777, 0o755)
        self.assertEqual((target/'upload/user/photo.jpg').stat().st_mode & 0o777, 0o644)
    def test_python_external_copy_is_readable_under_restrictive_umask(self):
        source = self.root / 'source'
        source.mkdir()
        (source/'photo.jpg').write_bytes(b'photo')
        target = self.root / 'external'
        old = os.umask(0o077)
        try:
            load('photo-backup.py').backup(source,target,True)
        finally:
            os.umask(old)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((target/'photo.jpg').stat().st_mode & 0o777, 0o644)
    def test_job_does_not_report_success_to_replaced_ssd(self):
        data=self.root/'ssd';data.mkdir()
        calls=[]
        def verify():
            calls.append(1)
            if len(calls)==3:raise RuntimeError('replaced')
        with patch.object(job_status.os.path,'ismount',return_value=True):
            with self.assertRaises(RuntimeError):
                job_status.execute(data,'archive',lambda:None,verify=verify)
        row=json.loads((data/'reports/jobs/archive.json').read_text())
        self.assertEqual(row['status'],'running')
        self.assertIsNone(row['last_success'])
    def test_shared_guard_checks_exact_uuid_and_filesystem(self):
        cfg={'data_mount':str(self.root/'ssd'),'media_mount':str(self.root/'hdd'),'storage':{'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'xfs'}}}
        with patch('subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout='SSD-UUID ext4'),SimpleNamespace(returncode=0,stdout='HDD-UUID ext4')]):
            with self.assertRaises(RuntimeError):neko_common.verify_storage(cfg)
    def test_media_monitor_uses_selected_rust_runtime(self):
        cfg=json.loads((ROOT/'config.json').read_text());data=self.root/'ssd';data.mkdir();cfg.update(data_mount=str(data),media_mount=str(self.root/'hdd'))
        cfg['monitoring']['enabled']=True
        units=self.root/'units';real=Path
        def mapped(value,*args):
            p=real(value,*args)
            return units/p.relative_to('/etc/systemd/system') if str(p).startswith('/etc/systemd/system') else p
        with patch.object(media_monitor,'Path',side_effect=mapped),patch.object(media_monitor.os.path,'ismount',return_value=True),patch.object(media_monitor.shutil,'which',return_value='/bin/true'),patch.object(media_monitor.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            media_monitor.run(SimpleNamespace(config=cfg,apply=True,log=lambda _:None))
        unit=(units/'nekoserver-media-health.service').read_text()
        self.assertIn('nekoserver-runtime media-health --config',unit)
        self.assertIn('SuccessExitStatus=3',unit)
        self.assertIn('TimeoutStartSec=300',unit)
    def test_python_media_audit_checks_disks_before_private_api_access(self):
        health=load('media-health.py');cfg=ROOT/'config.json'
        with patch.object(health,'verify_storage',side_effect=RuntimeError('wrong disk')),patch.object(health,'audit') as audit:
            self.assertEqual(health.main(['--config',str(cfg)]),2)
            audit.assert_not_called()


class NativeMedia(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.data=self.root/'ssd';self.media=self.root/'hdd';self.data.mkdir();self.media.mkdir()
        self.config=json.loads((ROOT/'config.json').read_text());self.config.update(data_mount=str(self.data),media_mount=str(self.media))
        self.config['storage']={'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'ext4'}}
        self.file=self.root/'config.json';self.file.write_text(json.dumps(self.config));self.output=self.data/'reports/media/latest.json'
        self.responses={};self.requests=[];self.servers=[];self.env=dict(os.environ)
        self.containers={}
        self.settings={'main':{'apiKey':SECRET},'jellyfin':{'serverId':'neko-id','apiKey':SECRET}}
        seerr=self.data/'appdata/seerr';seerr.mkdir(parents=True);(seerr/'settings.json').write_text(json.dumps(self.settings))
        for app in ['sonarr','radarr','lidarr','prowlarr']:
            folder=self.data/'appdata'/app;folder.mkdir(parents=True);(folder/'config.xml').write_text('<Config><ApiKey>'+SECRET+'</ApiKey></Config>')
        for app in self.config['media']['services']:
            self.start(app)
            dest='/app/config' if app=='seerr' else '/data' if app=='navidrome' else '/config'
            mounts=[{'Source':str(self.data/'appdata'/app),'Destination':dest,'RW':True}]
            if app=='jellyfin':mounts+=[{'Source':str(self.media/'library'/category),'Destination':'/data/'+category,'RW':False} for category in ('movies','tv')]
            elif app=='navidrome':mounts+=[{'Source':str(self.data/'library/music'),'Destination':'/music','RW':False}]
            elif app in ('sonarr','radarr','lidarr','qbittorrent'):mounts+=[{'Source':str(self.media/'library'),'Destination':'/data','RW':True}]
            if app in ('lidarr','qbittorrent'):mounts+=[{'Source':str(self.data/'library/downloads/music'),'Destination':'/data/downloads/music','RW':True}]
            if app=='lidarr':mounts+=[{'Source':str(self.data/'library/music'),'Destination':'/data/music','RW':True}]
            self.containers[app]={'State':{'Status':'running'},'NetworkSettings':{'Networks':{'nekoserver-media':{}}},'Mounts':mounts}
        self.seed()
        commands=self.root/'commands';commands.mkdir()
        source='''#!/usr/bin/python3
import json,os,sys
args=sys.argv[1:]
if os.path.basename(sys.argv[0])=='findmnt':
 p=args[-1];print(('WRONG-UUID' if os.environ.get('NEKO_WRONG_DISK') else 'SSD-UUID' if p==os.environ['NEKO_DATA'] else 'HDD-UUID')+' ext4')
else:
 if 'ps' in args:
  print(next(s.split('=')[-1] for s in args if 'compose.service=' in s))
 else:
  print(json.dumps([json.load(open(os.environ['NEKO_CONTAINERS']))[args[-1]]]))
'''
        for name in ('findmnt','docker'):
            p=commands/name;p.write_text(source);p.chmod(0o755)
        self.container_file=self.root/'containers.json'
        self.env.update(PATH=str(commands)+':/usr/bin:/bin',NEKO_DATA=str(self.data),NEKO_CONTAINERS=str(self.container_file))
    def tearDown(self):
        threads=[threading.Thread(target=server.shutdown) for server in self.servers]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        for server in self.servers:server.server_close()
        self.tmp.cleanup()
    def start(self,app):
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append((app,self.command,self.path,dict(self.headers)))
                status,value=owner.responses.get((app,self.path),(404,{'error':'PRIVATE-RESPONSE'}))
                if self.path=='/':status,value=200,{'ready':True}
                raw=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                owner.requests.append((app,self.command,self.path,dict(self.headers)));self.send_error(405)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();self.servers.append(server)
        self.env['NEKO_TEST_PORT_'+app.upper()]=str(server.server_port)
    def seed(self):
        for app,version,directory,category in [('sonarr','v3','/data/tv','tv'),('radarr','v3','/data/movies','movies'),('lidarr','v1','/data/music','music'),('prowlarr','v1','','')]:
            self.responses[(app,f'/api/{version}/system/status')]=(200,{'version':'4.0','urlBase':''})
            self.responses[(app,f'/api/{version}/indexer')]=(200,[{'enable':True,'enableAutomaticSearch':True}])
            if app=='prowlarr':
                records=[]
                for manager,port in [('sonarr',8989),('radarr',7878),('lidarr',8686)]:
                    records.append({'syncLevel':'fullSync','fields':[{'name':'baseUrl','value':f'http://{manager}:{port}'},{'name':'prowlarrUrl','value':'http://prowlarr:9696'},{'name':'apiKey','value':SECRET}]})
                self.responses[(app,'/api/v1/applications')]=(200,records)
            else:
                self.responses[(app,f'/api/{version}/rootfolder')]=(200,[{'path':directory,'accessible':True}])
                self.responses[(app,f'/api/{version}/qualityprofile')]=(200,[{'id':1,'name':'HD'}])
                self.responses[(app,f'/api/{version}/downloadclient')]=(200,[{'enable':True,'implementation':'QBittorrent','fields':[{'name':'host','value':'qbittorrent'},{'name':'port','value':8080},{'name':'category','value':category}]}])
        self.responses[('jellyfin','/System/Info/Public')]=(200,{'Id':'neko-id','StartupWizardCompleted':True})
        self.responses[('jellyfin','/Library/VirtualFolders')]=(200,[{'ItemId':'movies-id','Locations':['/data/movies'],'CollectionType':'movies'},{'ItemId':'tv-id','Locations':['/data/tv'],'CollectionType':'tvshows'}])
        self.responses[('seerr','/api/v1/settings/public')]=(200,{'initialized':True})
        self.responses[('seerr','/api/v1/settings/jellyfin')]=(200,{'serverId':'neko-id'})
        self.responses[('seerr','/api/v1/settings/jellyfin/library')]=(200,[{'id':'movies-id','enabled':True},{'id':'tv-id','enabled':True}])
        for app,port,directory in [('sonarr',8989,'/data/tv'),('radarr',7878,'/data/movies')]:
            self.responses[('seerr',f'/api/v1/settings/{app}')]=(200,[{'id':1,'is4k':False,'isDefault':True,'hostname':app,'port':port,'apiKey':SECRET,'useSsl':False,'baseUrl':'','activeDirectory':directory,'activeProfileId':1,'preventSearch':False}])
            self.responses[('seerr',f'/api/v1/service/{app}/1')]=(200,{'profiles':[{'id':1,'name':'HD'}],'rootFolders':[{'path':directory}],'urlBase':''})
    def run_audit(self,output=None):
        self.container_file.write_text(json.dumps(self.containers))
        result=subprocess.run([str(FIXTURE),'media-health','--config',str(self.file),'--output',str(output or self.output)],env=self.env,capture_output=True,text=True,timeout=25)
        return result, json.loads(self.output.read_text()) if self.output.exists() else None
    def test_complete_chain_checks_saved_settings_and_live_connections(self):
        result,report=self.run_audit();self.assertEqual(result.returncode,3,result.stderr)
        self.assertTrue(all(r['result']=='pass' for r in report['services'].values()))
        self.assertTrue(all(r['result']=='pass' for r in report['integrations'].values()))
        for name in ('jellyfin:current_libraries','seerr:current_libraries','seerr:provider:sonarr','seerr:provider:radarr'):self.assertEqual(report['readiness'][name]['result'],'pass')
        self.assertEqual(report['readiness']['end_to_end_media']['result'],'needs_setup')
    def test_production_binary_runs_the_same_local_api_chain(self):
        ports={'jellyfin':8096,'navidrome':4533,'sonarr':8989,'radarr':7878,'lidarr':8686,'prowlarr':9696,'seerr':5055,'qbittorrent':8080}
        originals=list(self.servers)
        for app,original in zip(self.config['media']['services'],originals):
            server=ThreadingHTTPServer(('127.0.0.1',ports[app]),original.RequestHandlerClass)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.servers.append(server)
        self.container_file.write_text(json.dumps(self.containers))
        result=subprocess.run([str(BIN),'media-health','--config',str(self.file),'--output',str(self.output)],env=self.env,capture_output=True,text=True,timeout=25)
        self.assertEqual(result.returncode,3,result.stderr)
        report=json.loads(self.output.read_text())
        self.assertTrue(all(row['result']=='pass' for row in report['services'].values()))
        self.assertTrue(all(row['result']=='pass' for row in report['integrations'].values()))
        self.assertEqual(report['readiness']['seerr:provider:sonarr']['result'],'pass')
        self.assertNotIn(b'NEKO_TEST_PORT_',BIN.read_bytes())
    def test_only_read_requests_and_no_secrets_in_reports(self):
        result,report=self.run_audit();self.assertEqual(result.returncode,3)
        self.assertTrue(all(method=='GET' for _,method,_,_ in self.requests))
        self.assertNotIn(SECRET,self.output.read_text()+result.stderr+result.stdout)
        self.assertNotIn('PRIVATE-RESPONSE',self.output.read_text())
        self.assertEqual(self.output.stat().st_mode&0o777,0o600)
    def test_wrong_disk_prevents_private_reads_requests_and_report(self):
        self.env['NEKO_WRONG_DISK']='1';result,report=self.run_audit();self.assertEqual(result.returncode,1)
        self.assertEqual(self.requests,[]);self.assertIsNone(report)
    def test_report_outside_fixed_private_destination_is_refused(self):
        result,_=self.run_audit(self.root/'leak.json');self.assertEqual(result.returncode,1);self.assertFalse((self.root/'leak.json').exists())
    def test_playback_mount_must_be_read_only(self):
        self.containers['jellyfin']['Mounts'][1]['RW']=True
        result,report=self.run_audit();self.assertEqual(result.returncode,2);self.assertEqual(report['services']['jellyfin']['result'],'fail')
    def test_each_required_playback_folder_must_be_present(self):
        self.containers['jellyfin']['Mounts'].pop()
        _,report=self.run_audit();self.assertEqual(report['services']['jellyfin']['result'],'fail')
    def test_prowlarr_key_must_match_selected_manager(self):
        self.responses[('prowlarr','/api/v1/applications')][1][0]['fields'][-1]['value']='b'*32
        _,report=self.run_audit();self.assertFalse(report['integrations']['prowlarr']['applications_configured'])
    def test_wrong_seerr_provider_key_is_not_accepted(self):
        self.responses[('seerr','/api/v1/settings/sonarr')][1][0]['apiKey']='b'*32
        _,report=self.run_audit();self.assertEqual(report['readiness']['seerr:provider:sonarr']['result'],'needs_setup')
        self.assertEqual(report['readiness']['seerr:provider:radarr']['result'],'pass')
    def test_custom_routing_is_preserved_and_reported(self):
        path=('seerr','/api/v1/settings/radarr');before=copy.deepcopy(self.responses[path][1]);self.responses[path][1][0]['activeDirectory']='/custom'
        expected=copy.deepcopy(self.responses[path][1]);_,report=self.run_audit();self.assertEqual(report['readiness']['seerr:provider:radarr']['result'],'needs_setup');self.assertEqual(self.responses[path][1],expected);self.assertNotEqual(before,expected)
    def test_provider_connection_failure_does_not_stop_other_app(self):
        self.responses[('seerr','/api/v1/service/sonarr/1')]=(500,{'error':SECRET})
        _,report=self.run_audit();self.assertEqual(report['readiness']['seerr:provider:sonarr']['result'],'fail');self.assertEqual(report['readiness']['seerr:provider:radarr']['result'],'pass')
    def test_live_profiles_must_match_saved_profile(self):
        self.responses[('seerr','/api/v1/service/radarr/1')][1]['profiles']=[{'id':2,'name':'different'}]
        _,report=self.run_audit();self.assertEqual(report['readiness']['seerr:provider:radarr']['result'],'needs_setup')
    def test_inaccessible_arr_root_is_not_accepted(self):
        self.responses[('sonarr','/api/v3/rootfolder')][1][0]['accessible']=False
        _,report=self.run_audit();self.assertFalse(report['integrations']['sonarr']['rootfolder_configured']);self.assertEqual(report['readiness']['seerr:provider:sonarr']['result'],'needs_setup')
    def test_missing_account_setup_remains_visible(self):
        self.responses[('jellyfin','/System/Info/Public')][1]['StartupWizardCompleted']=False
        _,report=self.run_audit();self.assertEqual(report['readiness']['jellyfin:current_libraries']['result'],'needs_setup')
    def test_redirects_do_not_receive_local_credentials(self):
        self.responses[('sonarr','/api/v3/system/status')]=(302,{'redirect':SECRET})
        _,report=self.run_audit();self.assertEqual(report['integrations']['sonarr']['result'],'fail')
        self.assertTrue(all(path!='redirect' for _,_,path,_ in self.requests))
    def test_duplicate_standard_providers_are_not_accepted(self):
        values=self.responses[('seerr','/api/v1/settings/sonarr')][1];values.append(copy.deepcopy(values[0]))
        _,report=self.run_audit();self.assertEqual(report['readiness']['seerr:provider:sonarr']['result'],'needs_setup')
    def test_symlink_credentials_are_not_read(self):
        p=self.data/'appdata/sonarr/config.xml';p.unlink();outside=self.root/'private.xml';outside.write_text('<ApiKey>'+SECRET+'</ApiKey>');p.symlink_to(outside)
        _,report=self.run_audit();self.assertEqual(report['integrations']['sonarr']['result'],'needs_setup')
    def test_repeat_audits_preserve_application_settings(self):
        before={p:p.read_bytes() for p in (self.data/'appdata').rglob('*') if p.is_file()}
        self.run_audit();self.run_audit()
        self.assertEqual(before,{p:p.read_bytes() for p in before})
