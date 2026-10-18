"""Native transaction tests: real tar/restore/copies and HTTP, simulated Docker/UUIDs."""
import copy
import datetime
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules import nextcloud, maintenance
import installer

ROOT=Path(__file__).resolve().parents[1]
BIN=ROOT/'runtime/bin/nekoserver-runtime'
JOURNAL=Path('/etc/nekoserver-backups/maintenance-resume.json')

SHIM=r'''#!/usr/bin/python3
import json,os,sys,signal,hashlib
from pathlib import Path
args=sys.argv[1:];name=Path(sys.argv[0]).name
root=Path(os.environ['MROOT']);state_path=root/'docker.json';s=json.loads(state_path.read_text())
def save():state_path.write_text(json.dumps(s))
def log():
 with (root/'commands.log').open('a') as f:f.write(json.dumps([name,*args])+'\n')
log()
if name=='findmnt':
 target=args[-1];uid='SSD' if target==str(root/'ssd') else 'HDD' if target==str(root/'hdd') else 'USB'
 if os.environ.get('WRONG_USB') and uid=='USB':uid='WRONG'
 print(uid+' ext4' if '-o' in args and args[args.index('-o')+1]=='UUID,FSTYPE' else uid);sys.exit(0)
if name=='lsblk':print(args[-1].split('/')[-1]+' disk');sys.exit(0)
if name=='du':print('4096 fixture');sys.exit(0)
assert name=='docker' and args[:2]==['--host','unix:///var/run/docker.sock'];args=args[2:]
if args[0]=='ps':
 project=next((v.split('=')[-1] for v in args if v.startswith('label=com.docker.compose.project=')),None)
 service=next((v.split('=')[-1] for v in args if v.startswith('label=com.docker.compose.service=')),None)
 print('\n'.join(k for k,v in s['containers'].items() if (not project or v['Config']['Labels']['com.docker.compose.project']==project) and (not service or v['Config']['Labels']['com.docker.compose.service']==service) and ('-aq' in args or v['State']['Running'])))
elif args[0]=='inspect':print(json.dumps([s['containers'][key] for key in args[1:]]))
elif args[:2]==['image','inspect']:print(json.dumps([{'Id':args[2] if args[2].startswith('sha256:') else 'sha256:'+'b'*64,'Size':4096}]))
elif args[:2]==['image','save']:Path(args[args.index('--output')+1]).write_bytes(b'fake-docker-image-archive')
elif args[0]=='pull':pass
elif args[0]=='stop':s['containers'][args[-1]]['State']['Running']=False;save()
elif args[0]=='compose':
 project=args[args.index('-p')+1];file=Path(args[args.index('-f')+1]);model=json.loads(file.read_text());rehearsal=project.startswith('neko-rehearsal-')
 action=next(a for a in ('config','up','down','stop','ps','port','exec') if a in args)
 if action=='config':print(json.dumps(model))
 elif action=='up':
  if rehearsal and os.environ.get('FAIL_REHEARSAL'):sys.exit(1)
  new=file.name.endswith('-new.json')
  if new:
   (root/'ssd/appdata/nextcloud/html/settings').write_text('migrated-state')
   s['new']=True;save()
   if os.environ.get('KILL_UPDATE'):os.kill(os.getppid(),signal.SIGKILL);sys.exit(1)
  if not rehearsal:s['new']=new
  for index,(service,v) in enumerate(model['services'].items()):
   key=hashlib.sha256((project+service).encode()).hexdigest()
   mounts=[{'Type':'bind','Source':m['source'],'Destination':m['target'],'RW':not m.get('read_only',False)} for m in v.get('volumes',[]) if m['type']=='bind']
   s['containers'][key]={'Id':key,'Image':v['image'] if v['image'].startswith('sha256:') else 'sha256:'+'a'*64,'State':{'Running':True,'Paused':False,'Restarting':False,'ExitCode':0},'Config':{'Labels':{'com.docker.compose.project':project,'com.docker.compose.service':service}},'Mounts':mounts}
  save()
 elif action=='down':s['containers']={k:v for k,v in s['containers'].items() if v['Config']['Labels']['com.docker.compose.project']!=project};save()
 elif action=='stop':
  for v in s['containers'].values():
   if v['Config']['Labels']['com.docker.compose.project']==project:v['State']['Running']=False
  save()
 elif action=='ps':print('\n'.join(k for k,v in s['containers'].items() if v['Config']['Labels']['com.docker.compose.project']==project and v['State']['Running']))
 elif action=='port':print('127.0.0.1:'+os.environ['PORT'])
 elif action=='exec':
  if os.environ.get('FAIL_UPDATE') and s.get('new') and not rehearsal:sys.exit(1)
  if 'psql' in ' '.join(args):print('1')
else:sys.exit(1)
'''

class NativeMaintenance(unittest.TestCase):
    def setUp(self):
        if os.geteuid()!=0 or JOURNAL.exists():self.skipTest('Isolated root worker with no existing maintenance journal required')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda:JOURNAL.unlink(missing_ok=True))
        self.root=Path(self.temp.name)
        self.cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()))
        for key,name in [('data_mount','ssd'),('media_mount','hdd'),('external_backup_mount','usb')]:
            self.cfg[key]=str(self.root/name);(self.root/name).mkdir()
        self.cfg['storage']={'data_ssd':{'uuid':'SSD','fstype':'ext4'},'media_hdd':{'uuid':'HDD','fstype':'ext4'}}
        self.cfg['media']['services']=[];self.cfg['photos']['deploy_immich']=False
        self.cfg['photos']['external_backup_uuid']='USB';self.cfg['maintenance'].update(minimum_free_gb=0,health_wait_seconds=0)
        self.cfg['nextcloud'].update(enabled=True,image='nextcloud:35-apache')
        folder=self.root/'ssd/compose/nextcloud';folder.mkdir(parents=True)
        model=nextcloud.model(self.cfg,'nextcloud:35-apache')
        for v in model['services'].values():
            volumes=[]
            for raw in v.get('volumes',[]):
                src,target=raw.split(':',1);path=Path(src);path.mkdir(parents=True,exist_ok=True)
                volumes.append({'type':'bind','source':src,'target':target})
            v['volumes']=volumes
            if 'ports' in v:v['ports']=[{'target':80,'published':'8088','host_ip':'127.0.0.1'}]
        (folder/'compose.yaml').write_text(json.dumps(model));(folder/'.env').write_text('PRIVATE=secret');(folder/'.env').chmod(0o600)
        (self.root/'ssd/appdata/nextcloud/html/settings').write_text('original-state')
        (self.root/'hdd/cloud/files/test.txt').write_text('original-file')
        self.file=self.root/'config.json';self.file.write_text(json.dumps(self.cfg));self.file.chmod(0o600)
        (self.root/'docker.json').write_text(json.dumps({'containers':{}}))
        commands=self.root/'commands';commands.mkdir()
        for name in ('docker','findmnt','lsblk','du'):
            p=commands/name;p.write_text(SHIM);p.chmod(0o755)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'{"installed":true,"maintenance":false,"needsDbUpgrade":false}')
            def log_message(self,*args):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(2)))
        self.env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','MROOT':str(self.root),'PORT':str(server.server_port),'TZ':'UTC-'+str((2-datetime.datetime.now(datetime.timezone.utc).hour)%24)}
        subprocess.run([str(commands/'docker'),'--host','unix:///var/run/docker.sock','compose','-p','neko-nextcloud','-f',str(folder/'compose.yaml'),'up','-d'],env=self.env,check=True)
    def worker(self,mode='maintenance'):
        return subprocess.run([str(BIN),mode,'--config',str(self.file)],env=self.env,capture_output=True,text=True,timeout=60)
    def snapshots(self,drive='hdd'):return list((self.root/drive/'backups/monthly').glob('*'))
    def test_success_copies_rehearses_updates_and_pins(self):
        r=self.worker();self.assertEqual(r.returncode,0,r.stderr)
        for drive in ('ssd','hdd','usb'):
            snapshot=self.snapshots(drive)[0];manifest=json.loads((snapshot/'manifest.json').read_text())
            for name in ('data','media','images'):self.assertEqual(hashlib.sha256((snapshot/(name+'.tar')).read_bytes()).hexdigest(),manifest['sha256'][name])
        self.assertTrue((self.snapshots()[0]/'restore-tests.json').is_file());self.assertFalse(JOURNAL.exists())
        compose=ROOT.parents[1]/'compose-review/docker-compose'
        if compose.is_file():
            result=subprocess.run([str(compose),'-p','audit-restored','-f',str(self.snapshots()[0]/'nextcloud-rehearsal.json'),'config','-q'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)

        pins=json.loads((self.root/'ssd/compose/nextcloud/maintenance-images.json').read_text())
        self.assertTrue(all(v['image']=='sha256:'+'b'*64 for v in pins['services'].values()))
        self.assertTrue(all(v['State']['Running'] for v in json.loads((self.root/'docker.json').read_text())['containers'].values()))
    def test_private_account_inventory_is_in_all_independent_backups(self):
        import tarfile
        private = self.root / 'ssd/secrets/accounts'
        private.mkdir(parents=True, mode=0o700)
        payload = private / 'credentials.json'
        payload.write_text('{"fixture-only":"private-password"}')
        payload.chmod(0o600)
        result = self.worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        for drive in ('ssd', 'hdd', 'usb'):
            with tarfile.open(self.snapshots(drive)[0] / 'data.tar') as archive:
                member = archive.getmember('secrets/accounts/credentials.json')
                self.assertEqual(member.mode & 0o777, 0o600)
                self.assertEqual(archive.extractfile(member).read(), payload.read_bytes())

    def test_failed_update_restores_with_optional_private_inventory_present(self):
        private = self.root / 'ssd/secrets/accounts'
        private.mkdir(parents=True, mode=0o700)
        payload = private / 'credentials.json'
        payload.write_text('{"fixture-only":"private-password"}')
        payload.chmod(0o600)
        self.env['FAIL_UPDATE'] = '1'
        result = self.worker()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(JOURNAL.exists(), result.stderr)
        self.assertEqual(payload.read_text(), '{"fixture-only":"private-password"}')
        self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.root / 'ssd/compose/nextcloud/maintenance-images.json').is_file())

    def test_independent_copy_stages_and_corruption_is_refused(self):
        self.assertEqual(self.worker().returncode,0)
        spec=importlib.util.spec_from_file_location('monthly_recovery_fixture',ROOT/'monthly-recovery.py');helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        backup=self.snapshots('usb')[0];stage=self.root/'independent-stage';helper.stage(backup,stage)
        self.assertEqual((stage/'data/appdata/nextcloud/html/settings').read_text(),'original-state')
        self.assertEqual((stage/'media/cloud/files/test.txt').read_text(),'original-file')
        with (backup/'data.tar').open('ab') as f:f.write(b'corruption')
        with self.assertRaises(RuntimeError):helper.verify(backup)
    def test_exfat_alias_cannot_expose_private_snapshots(self):
        self.cfg['photos']['external_backup_fstype']='exfat';self.file.write_text(json.dumps(self.cfg))
        self.assertNotEqual(self.worker().returncode,0);self.assertFalse(self.snapshots());self.assertFalse(JOURNAL.exists())
    def test_bad_new_version_restores_files_and_old_images(self):
        self.env['FAIL_UPDATE']='1';r=self.worker();self.assertNotEqual(r.returncode,0)
        self.assertEqual((self.root/'ssd/appdata/nextcloud/html/settings').read_text(),'original-state')
        self.assertEqual((self.root/'hdd/cloud/files/test.txt').read_text(),'original-file')
        pins=json.loads((self.root/'ssd/compose/nextcloud/maintenance-images.json').read_text())
        self.assertTrue(all(v['image']=='sha256:'+'a'*64 for v in pins['services'].values()))
        self.assertFalse(JOURNAL.exists());self.assertEqual(json.loads((self.root/'ssd/reports/jobs/maintenance.json').read_text())['status'],'fail')
    def test_old_version_journal_is_preserved_without_touching_live_state(self):
        journal={'format':'neko-maintenance-resume-v1','version':'0.16.1','storage':self.cfg['storage'],'data_mount':self.cfg['data_mount'],'media_mount':self.cfg['media_mount']}
        JOURNAL.parent.mkdir(parents=True,exist_ok=True);JOURNAL.write_text(json.dumps(journal));JOURNAL.chmod(0o600)
        before=JOURNAL.read_bytes();r=self.worker('maintenance-resume')
        self.assertNotEqual(r.returncode,0);self.assertEqual(JOURNAL.read_bytes(),before)
        self.assertEqual((self.root/'ssd/appdata/nextcloud/html/settings').read_text(),'original-state')

    def test_process_death_recovers_from_durable_journal(self):
        self.env['KILL_UPDATE']='1';r=self.worker();self.assertNotEqual(r.returncode,0);self.assertTrue(JOURNAL.exists())
        self.env.pop('KILL_UPDATE');r=self.worker('maintenance-resume');self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual((self.root/'ssd/appdata/nextcloud/html/settings').read_text(),'original-state');self.assertFalse(JOURNAL.exists())
    def test_wrong_external_stops_before_any_application_is_stopped(self):
        self.env['WRONG_USB']='1';self.assertNotEqual(self.worker().returncode,0)
        self.assertFalse(self.snapshots());self.assertFalse(JOURNAL.exists())
        self.assertTrue(all(v['State']['Running'] for v in json.loads((self.root/'docker.json').read_text())['containers'].values()))
    def test_failed_rehearsal_blocks_updates_and_resumes_originals(self):
        self.env['FAIL_REHEARSAL']='1';self.assertNotEqual(self.worker().returncode,0)
        self.assertEqual((self.root/'ssd/appdata/nextcloud/html/settings').read_text(),'original-state');self.assertFalse(JOURNAL.exists())
        self.assertFalse(json.loads((self.root/'docker.json').read_text()).get('new'))
    def add_immich(self):
        self.cfg['photos']['deploy_immich']=True;self.file.write_text(json.dumps(self.cfg))
        folder=self.root/'ssd/compose/immich';folder.mkdir(parents=True)
        db=self.root/'ssd/appdata/immich/postgres';db.mkdir(parents=True);(db/'database').write_text('immich-database')
        files=self.root/'ssd/photos/immich-library';files.mkdir(parents=True);(files/'photo.jpg').write_bytes(b'original-photo')
        model={'services':{}}
        for name in ('immich-server','database','redis','immich-machine-learning'):
            volumes=[{'type':'bind','source':str(files),'target':'/data'}] if name=='immich-server' else [{'type':'bind','source':str(db),'target':'/var/lib/postgresql/data'}] if name=='database' else []
            model['services'][name]={'image':'immich:v3.3.1','volumes':volumes}
            if name=='immich-server':model['services'][name]['ports']=[{'host_ip':'127.0.0.1','published':'2283','target':2283}]
        (folder/'compose.yaml').write_text(json.dumps(model));(folder/'.env').write_text('IMMICH_VERSION=v3.3.1\n');(folder/'.env').chmod(0o600)
        curl=self.root/'commands/curl';curl.write_text("#!/usr/bin/python3\nimport json,os,sys\nif 'https://api.github.com/repos/immich-app/immich/releases/latest' in sys.argv:print(json.dumps({'tag_name':os.environ.get('GITHUB_TAG','v3.3.1'),'prerelease':False,'draft':False}))\nelse:os.execv('/usr/bin/curl',['curl',*sys.argv[1:]])\n");curl.chmod(0o755)
        subprocess.run([str(self.root/'commands/docker'),'--host','unix:///var/run/docker.sock','compose','-p','neko-immich','-f',str(folder/'compose.yaml'),'up','-d'],env=self.env,check=True)
    def test_immich_database_and_assets_exist_in_all_three_copies(self):
        import tarfile
        self.add_immich();r=self.worker();self.assertEqual(r.returncode,0,r.stderr)
        for drive in ('ssd','hdd','usb'):
            folder=self.snapshots(drive)[0]
            with tarfile.open(folder/'data.tar') as archive:self.assertEqual(archive.extractfile('appdata/immich/postgres/database').read(),b'immich-database')
            with tarfile.open(folder/'data.tar') as archive:self.assertEqual(archive.extractfile('photos/immich-library/photo.jpg').read(),b'original-photo')
    def test_immich_major_jump_stays_pending(self):
        self.add_immich();self.env['GITHUB_TAG']='v4.0.0';r=self.worker();self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue((self.snapshots()[0]/'immich-major-pending.json').exists())
        self.assertIn('v3.3.1',(self.root/'ssd/compose/immich/.env').read_text())
    def test_daytime_run_refused(self):
        self.env['TZ']='UTC-'+str((12-datetime.datetime.now(datetime.timezone.utc).hour)%24)
        self.assertNotEqual(self.worker().returncode,0);self.assertFalse(self.snapshots())
    def test_insufficient_capacity_blocks_updates(self):
        self.cfg['maintenance']['minimum_free_gb']=100000;self.file.write_text(json.dumps(self.cfg))
        self.assertNotEqual(self.worker().returncode,0);self.assertFalse(self.snapshots())

class InstallationOptions(unittest.TestCase):
    def setUp(self):self.cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()))
    def test_nextcloud_storage_and_loopback_model(self):
        v=nextcloud.model(self.cfg,'nextcloud:35-apache');self.assertEqual(v['services']['nextcloud']['ports'],['127.0.0.1:8088:80'])
        self.assertEqual(v['services']['nextcloud']['volumes'],v['services']['cron']['volumes']);self.assertNotIn('ports',v['services']['database'])
    def test_unsafe_cloud_hosts_and_images_rejected(self):
        for key,value in [('trusted_hosts',['*']),('image','nextcloud:latest'),('public_url','http://cloud'),('admin_user','a;bad')]:
            cfg=copy.deepcopy(self.cfg);cfg['nextcloud'][key]=value
            with self.assertRaises(RuntimeError):nextcloud.options(cfg)
    def test_schedule_uses_explicit_timezone_without_daytime_catchup(self):
        units=maintenance.definitions(self.cfg,'/bin/runtime','/private/config')
        self.assertIn('OnCalendar=*-*-01 02:30:00 Asia/Jerusalem',units['nekoserver-maintenance.timer'])
        self.assertIn('Persistent=false',units['nekoserver-maintenance.timer']);self.assertIn('ExecStopPost=',units['nekoserver-maintenance.service'])
    def test_reject_daytime_and_disabled_external_copy(self):
        for key,value in [('time','13:00'),('require_external',False),('timezone','../../etc')]:
            cfg=copy.deepcopy(self.cfg);cfg['maintenance'][key]=value
            with self.assertRaises(RuntimeError):maintenance.options(cfg)
    def test_full_profile_orders_cloud_before_gateway_and_maintenance(self):
        modules=installer.load_modules(ROOT/'modules');plan=installer.ordered(modules,self.cfg['profiles']['full'])
        self.assertLess(plan.index('nextcloud'),plan.index('gateway_lan'));self.assertLess(plan.index('photo_backup'),plan.index('maintenance'))

class NextcloudInstallation(unittest.TestCase):
    def test_real_file_publication_and_official_compose_parser(self):
        import contextlib
        compose=ROOT.parents[1]/'compose-review/docker-compose'
        if not compose.is_file():self.skipTest('Official Compose parser required')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()))
            cfg.update(data_mount=str(root/'ssd'),media_mount=str(root/'hdd'))
            import account_store
            account_store.collect(cfg, ['nextcloud'], prompt=lambda _: '1', output=lambda _: None)
            chosen = account_store.get(cfg, 'nextcloud')
            config=root/'config.json';config.write_text(json.dumps(cfg))
            ctx=installer.Context(cfg,lambda _:None,True,config)
            calls=[]
            def command(args):
                calls.append(args)
                if 'config' in args:
                    r=subprocess.run([str(compose),*args[args.index('compose')+1:]],capture_output=True,text=True)
                    self.assertEqual(r.returncode,0,r.stderr)
            with patch.object(nextcloud,'verify_storage'),patch.object(nextcloud,'lock',return_value=contextlib.nullcontext()),patch.object(nextcloud,'resolve_image',return_value='nextcloud:35-apache'),patch.object(nextcloud.os,'chown'),patch.object(nextcloud,'command',side_effect=command):
                nextcloud.run(ctx);nextcloud.run(ctx)
            folder=root/'ssd/compose/nextcloud'
            self.assertEqual((folder/'.env').stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(config.read_text())['nextcloud']['image'],'nextcloud:35-apache')
            self.assertEqual(sum('up' in args for args in calls),2)
            self.assertEqual(cfg['nextcloud']['admin_user'], chosen['username'])
            self.assertEqual(account_store.get(cfg, 'nextcloud')['password'], chosen['password'])
            value=json.loads((folder/'compose.yaml').read_text());value['services']['nextcloud']['ports']=['0.0.0.0:8088:80'];(folder/'compose.yaml').write_text(json.dumps(value))
            with patch.object(nextcloud,'verify_storage'),patch.object(nextcloud,'lock',return_value=contextlib.nullcontext()):
                with self.assertRaises(RuntimeError):nextcloud.run(ctx)
    def test_existing_cloud_files_are_never_silently_adopted(self):
        import contextlib
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()));cfg.update(data_mount=str(root/'ssd'),media_mount=str(root/'hdd'))
            files=root/'hdd/cloud/files';files.mkdir(parents=True);(files/'important').write_text('keep')
            ctx=installer.Context(cfg,lambda _:None,True,root/'config')
            with patch.object(nextcloud,'verify_storage'),patch.object(nextcloud,'lock',return_value=contextlib.nullcontext()):
                with self.assertRaises(RuntimeError):nextcloud.run(ctx)
            self.assertEqual((files/'important').read_text(),'keep')

class CloudGateway(unittest.TestCase):
    def test_trusted_host_and_https_origin_are_set_explicitly(self):
        import gateway_tools
        cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()));calls=[]
        def command(args,*a,**kw):
            calls.append(args)
            return '["localhost","127.0.0.1"]' if 'config:system:get' in args else ''
        with patch.object(gateway_tools,'command',side_effect=command):gateway_tools.configure_cloud_origin(cfg,'192.168.1.20',8447)
        self.assertTrue(any('trusted_domains' in row and '--value=192.168.1.20' in row for row in calls))
        self.assertTrue(any('--value=https://192.168.1.20:8447' in row for row in calls))
        model=gateway_tools.lan_model({**cfg,'gateway':{**cfg['gateway'],'lan_ip':'192.168.1.20','lan_cidr':'192.168.1.0/24'}},Path('/portal'))
        self.assertEqual(model['apps']['http']['servers']['nextcloud']['routes'][0]['handle'][0]['upstreams'][0]['dial'],'127.0.0.1:8088')
    def test_malformed_occ_output_is_a_clear_setup_error(self):
        import gateway_tools
        cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()))
        with patch.object(gateway_tools,'command',return_value='not-json'):
            with self.assertRaises(RuntimeError):gateway_tools.configure_cloud_origin(cfg,'192.168.1.20',8447)
