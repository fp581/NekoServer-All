import copy
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import installer
import media_libraries
import media_readiness
import seerr_providers
from modules import tv,media_monitor
ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())
spec=importlib.util.spec_from_file_location('health070',ROOT/'media-health.py');health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)

class TVTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.home=self.base/'home';self.home.mkdir();self.owners={str(self.home):(1000,1000)}
        self.user=SimpleNamespace(pw_name='neko',pw_uid=1000,pw_gid=1000,pw_dir=str(self.home),pw_shell='/bin/bash');self.cfg=copy.deepcopy(CONFIG);self.cfg['data_mount']=str(self.base/'ssd');(self.base/'ssd').mkdir();self.cfg['tv']['user']='neko';self.calls=[];self.installed=set();self.unavailable=set();self.install_error=False;self.retained=True;self.power="'suspend'";self.power_writable=True
        self.ctx=SimpleNamespace(config=self.cfg,apply=True,log=lambda x:None);self.stack=ExitStack();self.stack.enter_context(patch.object(tv,'arch_host',return_value=True));self.stack.enter_context(patch.object(tv.platform,'machine',return_value='x86_64'));self.stack.enter_context(patch.object(tv.shutil,'which',return_value='/usr/bin/fixture'));self.stack.enter_context(patch.object(tv.pwd,'getpwnam',return_value=self.user));self.stack.enter_context(patch.object(tv.os.path,'ismount',return_value=True));self.stack.enter_context(patch.object(tv,'command',side_effect=self.command))
        original_stat=Path.stat
        def owned_stat(p,*args,**kwargs):
            value=original_stat(p,*args,**kwargs)
            if str(p) not in self.owners:return value
            class Owned:
                def __getattr__(self,key):return getattr(value,key)
            result=Owned();result.st_uid,result.st_gid=self.owners[str(p)];return result
        self.stack.enter_context(patch.object(Path,'stat',autospec=True,side_effect=owned_stat))
        self.stack.enter_context(patch.object(tv.os,'chown',side_effect=lambda p,uid,gid:self.owners.update({str(p):(uid,gid)})))
        original=Path.is_file
        self.stack.enter_context(patch.object(Path,'is_file',autospec=True,side_effect=lambda p:True if str(p) in ('/usr/bin/steam','/usr/bin/firefox') else original(p)))
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def command(self,args,timeout=30):
        self.calls.append(args);code=0;text=''
        if args[0]=='pacman':
            if args[1]=='-Q':code=0 if args[2] in self.installed else 1
            elif args[1]=='-Si':code=1 if args[2] in self.unavailable else 0
            elif args[1]=='-S':
                code=1 if self.install_error else 0
                if not code and self.retained:self.installed.update(args[4:])
            else:raise AssertionError(args)
        elif args[0]=='runuser':
            if 'xdg-user-dir' in args:text=str(self.home/'Desktop')+'\n'
            elif 'gsettings' in args:
                operation=args[args.index('gsettings')+1]
                if operation=='writable':text='true' if self.power_writable else 'false'
                if operation=='set':self.power="'nothing'"
                if operation=='get':text=self.power
            else:raise AssertionError(args)
        elif args[0]!='systemctl':raise AssertionError(args)
        return SimpleNamespace(returncode=code,stdout=text,stderr='')
    def test_install_for_user_packages_launchers_report_and_power(self):
        result=tv.run(self.ctx);self.assertEqual(result['status'],'needs_setup');self.assertEqual(self.power,"'nothing'")
        install=[x for x in self.calls if x[0]=='pacman' and x[1]=='-S'];self.assertEqual(len(install),1);self.assertIn('lib32-vulkan-intel',install[0]);self.assertIn('steam-devices',install[0]);self.assertNotIn('-Sy',install[0])
        for folder in (self.home/'Desktop',self.home/'.local/share/applications'):
            files=list(folder.glob('*.desktop'));self.assertEqual(len(files),6)
            for p in files:self.assertEqual(p.stat().st_uid,1000);self.assertTrue(p.stat().st_mode&0o100)
        self.assertIn('Exec=/usr/bin/steam -gamepadui',(self.home/'Desktop/nekoserver-steam.desktop').read_text());self.assertFalse((self.home/'.config/autostart/nekoserver-steam.desktop').exists())
        report=self.base/'ssd/reports/tv-setup.json';self.assertEqual(report.stat().st_mode&0o777,0o600);self.assertEqual(json.loads(report.read_text())['user'],'neko')
        self.assertFalse(any(x[0] in ('steam','firefox') for x in self.calls))
    def test_rerun_keeps_launchers_and_skips_package_install(self):
        tv.run(self.ctx);p=self.home/'Desktop/nekoserver-steam.desktop';before=p.stat().st_mtime_ns;self.calls=[];tv.run(self.ctx);self.assertEqual(p.stat().st_mtime_ns,before);self.assertFalse(any(x[:2]==['pacman','-S'] for x in self.calls))
    def test_custom_shortcut_blocks_before_package_or_power_changes(self):
        tv.run(self.ctx);p=self.home/'Desktop/nekoserver-steam.desktop';p.write_text('CUSTOM');self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'custom shortcut'):tv.run(self.ctx)
        self.assertEqual(p.read_text(),'CUSTOM');self.assertFalse(any(x[0]=='pacman' or 'gsettings' in x for x in self.calls))
    def test_symlink_shortcut_refused(self):
        tv.run(self.ctx);p=self.home/'Desktop/nekoserver-steam.desktop';p.unlink();p.symlink_to(self.home/'Desktop/nekoserver-youtube.desktop');self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'symlink'):tv.run(self.ctx)
        self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_root_owned_existing_user_directory_not_chowned(self):
        p=self.home/'.local/share/applications';p.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError,'owned'):tv.run(self.ctx)
        self.assertEqual(p.stat().st_uid,0);self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_root_owned_identical_launcher_refused_before_packages(self):
        tv.run(self.ctx);p=self.home/'Desktop/nekoserver-steam.desktop';self.owners[str(p)]=(0,0);self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'ownership'):tv.run(self.ctx)
        self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_nonexecutable_identical_launcher_reported_for_review(self):
        tv.run(self.ctx);p=self.home/'Desktop/nekoserver-steam.desktop';p.chmod(0o644);self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'executable'):tv.run(self.ctx)
        self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_root_owned_power_config_refused_before_packages(self):
        (self.home/'.config').mkdir()
        with self.assertRaisesRegex(RuntimeError,'owned'):tv.run(self.ctx)
        self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_disabled_autostart_symlink_refused_before_packages(self):
        folder=self.home/'.config/autostart';folder.mkdir(parents=True);(folder/'nekoserver-steam.desktop').symlink_to(self.home/'missing')
        with self.assertRaisesRegex(RuntimeError,'symlink'):tv.run(self.ctx)
        self.assertFalse(any(x[0]=='pacman' for x in self.calls))
    def test_multilib_missing_leaves_files_and_repository_untouched(self):
        self.unavailable={'steam','lib32-vulkan-intel'};result=tv.run(self.ctx);self.assertEqual(result['status'],'needs_setup');self.assertFalse((self.home/'Desktop').exists());self.assertFalse(any(x[:2]==['pacman','-S'] for x in self.calls))
    def test_install_error_visible(self):
        self.install_error=True
        with self.assertRaisesRegex(RuntimeError,'installation failed'):tv.run(self.ctx)
        self.assertFalse((self.home/'Desktop').exists())
    def test_install_not_retained_fails(self):
        self.retained=False
        with self.assertRaisesRegex(RuntimeError,'retained'):tv.run(self.ctx)
    def test_optional_autostart_same_user_and_command(self):
        self.cfg['tv']['autostart_big_picture']=True;tv.run(self.ctx);p=self.home/'.config/autostart/nekoserver-steam.desktop';self.assertEqual(p.stat().st_uid,1000);self.assertIn('-gamepadui',p.read_text())
    def test_existing_autostart_preserved_when_disabled(self):
        self.cfg['tv']['autostart_big_picture']=True;tv.run(self.ctx);p=self.home/'.config/autostart/nekoserver-steam.desktop';before=p.read_bytes();self.cfg['tv']['autostart_big_picture']=False;tv.run(self.ctx);self.assertEqual(before,p.read_bytes());rows=json.loads((self.base/'ssd/reports/tv-setup.json').read_text())['checks'];self.assertIn('tv:autostart',[r['check'] for r in rows])
    def test_desktop_optional_menu_still_installed(self):
        self.cfg['tv']['desktop_shortcuts']=False;tv.run(self.ctx);self.assertFalse((self.home/'Desktop').exists());self.assertTrue((self.home/'.local/share/applications/nekoserver-steam.desktop').exists())
    def test_bluetooth_disabled_no_bluetooth_package_or_service(self):
        self.cfg['tv']['bluetooth']=False;tv.run(self.ctx);self.assertNotIn('bluez',self.installed);self.assertFalse(any('bluetooth.service' in x for x in self.calls))
    def test_power_disabled_no_gsettings(self):
        self.cfg['tv']['keep_awake']=False;tv.run(self.ctx);self.assertFalse(any('gsettings' in x for x in self.calls))
    def test_locked_power_setting_reports_incomplete(self):
        self.power_writable=False;tv.run(self.ctx);rows=json.loads((self.base/'ssd/reports/tv-setup.json').read_text())['checks'];self.assertEqual(next(r['status'] for r in rows if r['check']=='tv:idle_suspend'),'needs_setup');self.assertEqual(self.power,"'suspend'")
    def test_power_runs_as_desktop_user_with_owned_home(self):
        tv.run(self.ctx);calls=[x for x in self.calls if 'gsettings' in x];self.assertTrue(calls);self.assertTrue(all(x[:4]==['runuser','-u','neko','--'] and 'HOME='+str(self.home) in x for x in calls))
    def test_no_apply_no_commands(self):
        self.ctx.apply=False;tv.run(self.ctx);self.assertFalse(self.calls)
    def test_unsupported_host_no_commands(self):
        with patch.object(tv,'arch_host',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'Nyarch/Arch'):tv.run(self.ctx)
        self.assertFalse(self.calls)
    def test_missing_ssd_no_packages(self):
        with patch.object(tv.os.path,'ismount',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'SSD'):tv.run(self.ctx)
        self.assertFalse(self.calls)
    def test_root_user_refused(self):
        self.user.pw_uid=0
        with self.assertRaisesRegex(RuntimeError,'non-root'):tv.run(self.ctx)
    def test_wrong_home_owner_refused(self):
        os.chown(self.home,0,0)
        with self.assertRaisesRegex(RuntimeError,'owned home'):tv.run(self.ctx)
    def test_unsafe_desktop_location_refused(self):
        with patch.object(tv,'command',return_value=SimpleNamespace(returncode=0,stdout='/tmp/outside',stderr='')):
            with self.assertRaisesRegex(RuntimeError,'inside'):tv.run(self.ctx)
    def test_string_switch_refused(self):
        self.cfg['tv']['autostart_big_picture']='false'
        with self.assertRaisesRegex(RuntimeError,'booleans'):tv.run(self.ctx)
    def test_profile_dependencies_independent_of_media_onboarding(self):
        modules=installer.load_modules(ROOT/'modules');plan=installer.ordered(modules,CONFIG['profiles']['tv']);self.assertEqual(plan,['system','storage','recovery','server_os','tv']);self.assertIn('tv',CONFIG['profiles']['full']);self.assertNotIn('onboarding',modules['tv']['deps']);self.assertFalse(CONFIG['modules']['tv']['enabled'])

class ReadFixture:
    def __init__(self,app,owner):self.app=app;self.owner=owner;self.headers={};self.calls=[];self.closed=False
    def close(self):self.closed=True;self.headers.clear()
    def call(self,path,method='GET',payload=None):
        if method!='GET' or payload is not None:raise AssertionError('Mutation in read-only check')
        self.calls.append(path);o=self.owner
        if path in o.reject:raise RuntimeError('PRIVATE_REMOTE_BODY')
        if self.app=='jellyfin':
            if path=='/System/Info/Public':return {'Id':'server-neko','StartupWizardCompleted':o.wizard}
            if path=='/Library/VirtualFolders':return copy.deepcopy(o.folders)
        if self.app=='seerr':
            if path=='/settings/public':return {'initialized':o.initialized}
            if path=='/settings/jellyfin':return {'serverId':o.server}
            if path=='/settings/jellyfin/library':return copy.deepcopy(o.enabled)
            if path in ('/settings/sonarr','/settings/radarr'):return copy.deepcopy(o.providers[path.rsplit('/',1)[1]])
            if path.startswith('/service/'):
                app=path.split('/')[2];return {'profiles':[{'id':7,'name':'1080p'}],'rootFolders':[{'id':2,'path':seerr_providers.SPECS[app][1]}],'languageProfiles':None,'urlBase':''}
        if self.app in ('sonarr','radarr'):
            if path=='/system/status':return {'version':'4.0.1' if self.app=='sonarr' else '6.0.1','urlBase':''}
            if path=='/rootfolder':return [{'id':2,'path':seerr_providers.SPECS[self.app][1],'accessible':True}]
            if path=='/qualityprofile':return [{'id':7,'name':'1080p'}]
        raise AssertionError((self.app,path))

class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg=copy.deepcopy(CONFIG);self.cfg['data_mount']=str(self.root);self.cfg['media']['services']=['jellyfin','seerr','sonarr','radarr'];self.clients=[];self.reject=set();self.wizard=True;self.initialized=True;self.server='server-neko'
        self.folders=[{'Name':'Movies','CollectionType':'movies','Locations':['/data/movies'],'ItemId':'m'},{'Name':'TV Shows','CollectionType':'tvshows','Locations':['/data/tv'],'ItemId':'t'}];self.enabled=[{'id':'m','enabled':True},{'id':'t','enabled':True}]
        (self.root/'appdata/seerr').mkdir(parents=True);self.settings=self.root/'appdata/seerr/settings.json';self.settings.write_text(json.dumps({'main':{'apiKey':'PRIVATE-SEERR'},'jellyfin':{'serverId':'server-neko','apiKey':'PRIVATE-JELLYFIN'}}))
        self.providers={}
        for app in ('sonarr','radarr'):
            (self.root/'appdata'/app).mkdir();(self.root/'appdata'/app/'config.xml').write_text('<Config><ApiKey>PRIVATE-'+app+'</ApiKey></Config>');self.providers[app]=[{'id':1,'hostname':app,'port':seerr_providers.SPECS[app][0],'apiKey':'PRIVATE-'+app,'useSsl':False,'baseUrl':'','is4k':False,'isDefault':True,'activeDirectory':seerr_providers.SPECS[app][1],'activeProfileId':7,'preventSearch':False}]
    def tearDown(self):self.tmp.cleanup()
    def factory(self,app):
        client=ReadFixture(app,self);self.clients.append(client);return client
    def check(self):return media_readiness.check(self.cfg,self.factory)
    def test_current_libraries_and_providers_get_only_no_secrets_or_writes(self):
        before=self.settings.read_bytes();rows=self.check();self.assertEqual([r['status'] for r in rows[:-1]],['pass']*4);self.assertEqual(rows[-1]['status'],'needs_setup');self.assertEqual(before,self.settings.read_bytes());self.assertNotIn('PRIVATE',json.dumps(rows));self.assertTrue(all(c.closed and not c.headers for c in self.clients));self.assertTrue(any('/service/sonarr/1' in c.calls for c in self.clients))
    def test_stale_source_report_cannot_hide_disabled_library(self):
        self.enabled[0]['enabled']=False;self.assertEqual(next(r['status'] for r in self.check() if r['check']=='seerr:current_libraries'),'needs_setup')
    def test_changed_jellyfin_path_detected(self):
        self.folders[0]['Locations']=['/wrong'];self.assertEqual(self.check()[0]['status'],'needs_setup')
    def test_fresh_wizard_detected_before_authentication(self):
        self.wizard=False;rows=self.check();self.assertEqual(rows[0]['status'],'needs_setup');self.assertEqual(self.clients[0].calls,['/System/Info/Public'])
    def test_wrong_seerr_server_detected(self):
        self.server='different';self.assertEqual(next(r['status'] for r in self.check() if r['check']=='seerr:current_libraries'),'needs_setup')
    def test_missing_library_key_incomplete_not_fake_pass(self):
        self.settings.write_text(json.dumps({'main':{'apiKey':'PRIVATE-SEERR'}}));self.assertEqual(self.check()[0]['status'],'needs_setup')
    def test_missing_provider_never_prompts_or_posts(self):
        self.providers['sonarr']=[];rows=self.check();self.assertEqual(next(r['status'] for r in rows if r['check']=='seerr:provider:sonarr'),'needs_setup');self.assertTrue(all(not any('/test' in p for p in c.calls) for c in self.clients))
    def test_failed_current_connection_visible_and_independent(self):
        self.reject={'/service/sonarr/1'};rows=self.check();self.assertEqual(next(r['status'] for r in rows if r['check']=='seerr:provider:sonarr'),'fail');self.assertEqual(next(r['status'] for r in rows if r['check']=='seerr:provider:radarr'),'pass');self.assertNotIn('PRIVATE',json.dumps(rows))
    def test_symlink_seerr_config_cannot_expose_secret_file(self):
        target=self.root/'target';before=self.settings.read_bytes();self.settings.rename(target);self.settings.symlink_to(target);self.assertEqual(self.check()[0]['status'],'needs_setup');self.assertEqual(target.read_bytes(),before)
    def test_read_client_refuses_mutation_before_network(self):
        c=media_readiness.ReadClient('seerr')
        with patch.object(c.opener,'open',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(RuntimeError,'GET'):c.call('/settings/sonarr','POST',{})
        c.close();self.assertFalse(c.headers)
    def test_verify_only_cannot_apply(self):
        with self.assertRaises(ValueError):seerr_providers.configure(self.root,apply=True,verify_only=True)
    def test_monitor_summary_includes_current_checks(self):
        cfg=copy.deepcopy(self.cfg);cfg['media']['services']=['seerr']
        info={'state':'running','health':'not_defined','network':True,'mounts':[str(self.root/'appdata/seerr')]}
        with patch.object(health,'inspect',return_value=info),patch.object(health.media_readiness,'check',return_value=[{'check':'current','status':'fail','reason':'Connection failed'}]):report=health.audit(cfg,probe=lambda *args:200)
        self.assertEqual(report['summary']['fail'],1);self.assertEqual(report['readiness']['current']['result'],'fail')
    def test_monitor_preserves_helper_dependencies(self):
        cfg=copy.deepcopy(self.cfg);cfg['monitoring']['enabled']=True;units=self.root/'units';units.mkdir();real_path=Path
        def mapped(value,*args):
            p=real_path(value,*args)
            return units/p.relative_to('/etc/systemd/system') if str(p)=='/etc/systemd/system' or str(p).startswith('/etc/systemd/system/') else p
        with patch.object(media_monitor,'Path',side_effect=mapped),patch.object(media_monitor.os.path,'ismount',return_value=True),patch.object(media_monitor.shutil,'which',return_value='/bin/true'),patch.object(media_monitor.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            media_monitor.run(SimpleNamespace(config=cfg,apply=True,log=lambda _:None))
        for name in ('media-health.py','neko_common.py','media_readiness.py','media_libraries.py','seerr_providers.py'):self.assertTrue((self.root/'scripts'/name).is_file())
        import subprocess,sys
        result=subprocess.run([sys.executable,'-c','import media_readiness; import seerr_providers'],cwd=self.root/'scripts',capture_output=True,text=True);self.assertEqual(result.returncode,0,result.stderr)


class ReadinessHTTPTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        self.fixture=ReadinessTests();self.fixture.setUp();owner=self;self.calls=[];self.reject=None
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                key=self.headers.get('X-Api-Key');owner.calls.append((self.command,self.path,key,self.headers.get('X-Emby-Token')))
                if self.path.startswith('/api/v1/'):
                    assert key=='PRIVATE-SEERR';app='seerr';path=self.path[len('/api/v1'):]
                elif self.path.startswith('/api/v3/'):
                    app='sonarr' if key=='PRIVATE-sonarr' else 'radarr';assert key=='PRIVATE-'+app;path=self.path[len('/api/v3'):]
                else:
                    app='jellyfin';path=self.path
                    if path!='/System/Info/Public':assert self.headers.get('X-Emby-Token')=='PRIVATE-JELLYFIN'
                value=ReadFixture(app,owner.fixture).call(path)
                if self.path==owner.reject:code=500;value={'error':'PRIVATE_ERROR_BODY'}
                else:code=200
                self.send_response(code);self.end_headers();self.wfile.write(json.dumps(value).encode())
            def do_POST(self):raise AssertionError('Readiness attempted POST')
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.ports=patch.dict(media_libraries.PORTS,{a:self.server.server_port for a in ('jellyfin','seerr','sonarr','radarr')});self.ports.start()
    def tearDown(self):self.ports.stop();self.server.shutdown();self.server.server_close();self.thread.join();self.fixture.tearDown()
    def test_get_only_authenticated_library_and_live_provider_checks(self):
        before=self.fixture.settings.read_bytes();rows=media_readiness.check(self.fixture.cfg);self.assertEqual([r['status'] for r in rows[:-1]],['pass']*4);self.assertTrue(all(c[0]=='GET' for c in self.calls));self.assertEqual(before,self.fixture.settings.read_bytes());self.assertNotIn('PRIVATE',json.dumps(rows));self.assertTrue(any(c[1]=='/api/v1/service/sonarr/1' for c in self.calls))
    def test_get_connection_error_redacted_and_other_provider_checked(self):
        self.reject='/api/v1/service/sonarr/1';rows=media_readiness.check(self.fixture.cfg);self.assertEqual(next(r['status'] for r in rows if r['check']=='seerr:provider:sonarr'),'fail');self.assertEqual(next(r['status'] for r in rows if r['check']=='seerr:provider:radarr'),'pass');self.assertNotIn('PRIVATE',json.dumps(rows))

if __name__=='__main__':unittest.main()
