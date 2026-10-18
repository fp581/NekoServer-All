"""Execute the shipped Rust binary; mock only hardware/system commands in job tests."""
import copy,hashlib,http.client,importlib.util,json,os,shutil,socket,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import native_runtime,acceptance_summary,dashboard_collect as py_collect
from modules import dashboard,recovery,photo_preservation,photo_backup
ROOT=Path(__file__).resolve().parents[1];BIN=ROOT/'runtime/bin/nekoserver-runtime';CFG=json.loads((ROOT/'config.json').read_text())

def free_port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def native(*args,env=None):return subprocess.run([str(BIN),*map(str,args)],capture_output=True,text=True,timeout=30,env=env)
def load(name):
 spec=importlib.util.spec_from_file_location('native_reference_'+name.replace('-','_'),ROOT/name);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class Fixture(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'ssd';self.media=self.root/'hdd';self.data.mkdir();self.media.mkdir();self.cfg=copy.deepcopy(CFG);self.cfg.update(data_mount=str(self.data),media_mount=str(self.media),external_backup_mount=str(self.root/'usb'));self.cfg['storage']={'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'ext4'}};self.cfg['docker']['persistent_root']=str(self.data);self.cfg['gateway'].update(lan_ip='192.168.1.20',lan_cidr='192.168.1.0/24');self.config=self.root/'config.json';self.config.write_text(json.dumps(self.cfg));self.env={**os.environ,'NEKO_FIXTURE_DATA':str(self.data),'NEKO_FIXTURE_MEDIA':str(self.media)}
 def tearDown(self):self.tmp.cleanup()
 def commands(self):
  folder=self.root/'commands';folder.mkdir();script='''#!/usr/bin/python3
import json,os,sys
name=os.path.basename(sys.argv[0]);args=sys.argv[1:]
if name=='findmnt':
 path=args[-1];is_data=path==os.environ['NEKO_FIXTURE_DATA'];is_media=path==os.environ['NEKO_FIXTURE_MEDIA']
 if os.environ.get('NEKO_FIXTURE_MOUNT')=='missing' and is_data:sys.exit(1)
 uuid=('WRONG-UUID' if os.environ.get('NEKO_FIXTURE_MOUNT')=='wrong' else 'SSD-UUID') if is_data else 'HDD-UUID' if is_media else 'OS-UUID'
 fields=args[args.index('-o')+1] if '-o' in args else ''
 values={'UUID':uuid,'FSTYPE':'ext4','SOURCE':'/dev/vda1' if is_data else '/dev/vdb1' if is_media else '/dev/vdc1'}
 print(' '.join(values.get(k,'') for k in fields.split(',')))
elif name=='lsblk':print(args[-1].split('/')[-1].rstrip('1234567890'))
elif name=='smartctl':
 print(json.dumps({'smart_status':{'passed':True},'temperature':{'current':38},'ata_smart_attributes':{'table':[]},'secret':'PRIVATE-OUTPUT'}))
elif name=='systemctl':print('LoadState=loaded\\nActiveState=active\\nNextElapseUSecRealtime=Tomorrow')
elif name=='docker':
 if 'ps' in args:
  for app in ['jellyfin','immich','seerr','navidrome','sonarr','radarr','lidarr','prowlarr','qbittorrent']:
   print(json.dumps({'Labels':'com.docker.compose.project=neko-'+app+',com.docker.compose.service='+('immich-server' if app=='immich' else app),'State':'running','Status':'Up (healthy)','secret':'PRIVATE-OUTPUT'}))
 else:
  print('-- fixture database\\n'+('SELECT 1;\\n'*100));print('PRIVATE-SQL-ERROR',file=sys.stderr)
  if os.environ.get('NEKO_FIXTURE_DUMP')=='fail':sys.exit(1)
else:sys.exit(1)
'''
  for name in ('findmnt','lsblk','smartctl','systemctl','docker'):
   p=folder/name;p.write_text(script);p.chmod(0o755)
  self.env['PATH']=str(folder)+':/usr/bin:/bin';return self.env
 def original(self,name='photo.JPG',payload=b'original'):
  p=self.data/'photos/immich-library/upload/user'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(payload);os.utime(p,(time.time()-1200,time.time()-1200));return p

class FileWorkerTests(Fixture):
 def test_archive_matches_python_for_all_supported_extensions(self):
  ref=load('photo-preserve.py');source=self.data/'photos/immich-library';archive=self.media/'photos/archive';expected=self.root/'reference'
  for i,ext in enumerate(sorted(ref.PHOTO_EXT|ref.VIDEO_EXT)):self.original(str(i)+ext.upper(),str(i).encode())
  self.original('ignored.txt');r=native('archive','--source',source,'--archive',archive,'--apply');self.assertEqual(r.returncode,0,r.stderr);ref.transfer(source,expected,True,600)
  actual={str(p.relative_to(archive)):p.read_bytes() for p in archive.rglob('*') if p.is_file()};want={str(p.relative_to(expected)):p.read_bytes() for p in expected.rglob('*') if p.is_file()};self.assertEqual(actual,want)
 def test_archive_dry_run_does_not_create_destination(self):
  p=self.original();target=self.root/'archive';r=native('archive','--source',p.parents[2],'--archive',target);self.assertEqual(r.returncode,0,r.stderr);self.assertFalse(target.exists())
 def test_archive_recent_original_is_skipped(self):
  p=self.original();os.utime(p,None);r=native('archive','--source',p.parents[2],'--archive',self.root/'archive','--apply');self.assertEqual(r.returncode,0);self.assertFalse((self.root/'archive').exists())
 def test_archived_copy_survives_source_deletion(self):
  p=self.original();source=p.parents[2];archive=self.root/'archive';self.assertEqual(native('archive','--source',source,'--archive',archive,'--apply').returncode,0);p.unlink();self.assertEqual(native('archive','--source',source,'--archive',archive,'--apply').returncode,0);self.assertEqual((archive/'upload/user/photo.JPG').read_bytes(),b'original')
 def test_changed_original_collision_preserves_archive(self):
  p=self.original();archive=self.root/'archive';source=p.parents[2];self.assertEqual(native('archive','--source',source,'--archive',archive,'--apply').returncode,0);p.write_bytes(b'changed');os.utime(p,(time.time()-1200,)*2);r=native('archive','--source',source,'--archive',archive,'--apply');self.assertNotEqual(r.returncode,0);self.assertEqual((archive/'upload/user/photo.JPG').read_bytes(),b'original')
 def test_archive_source_and_destination_symlinks_are_rejected(self):
  p=self.original();source=p.parents[2];archive=self.root/'archive';outside=self.root/'outside';outside.mkdir();(source/'upload/link').symlink_to(outside,target_is_directory=True);self.assertNotEqual(native('archive','--source',source,'--archive',archive,'--apply').returncode,0);(source/'upload/link').unlink();archive.symlink_to(outside,target_is_directory=True);self.assertNotEqual(native('archive','--source',source,'--archive',archive,'--apply').returncode,0);self.assertFalse(list(outside.iterdir()))
 def test_overlap_and_invalid_age_rejected(self):
  p=self.original();source=p.parents[2]
  for target,age in [(source,600),(source/'nested',600),(self.root,600),(self.root/'archive',59)]:self.assertNotEqual(native('archive','--source',source,'--archive',target,'--min-age',age,'--apply').returncode,0)
 def test_stale_partial_is_recovered_and_rerun_keeps_published_file(self):
  p=self.original();archive=self.root/'archive';target=archive/'upload/user/photo.JPG';target.parent.mkdir(parents=True);target.with_name(target.name+'.neko-part').write_bytes(b'interrupted');self.assertEqual(native('archive','--source',p.parents[2],'--archive',archive,'--apply').returncode,0);before=target.stat().st_mtime_ns;self.assertEqual(native('archive','--source',p.parents[2],'--archive',archive,'--apply').returncode,0);self.assertEqual(target.stat().st_mtime_ns,before);self.assertEqual(target.read_bytes(),b'original');self.assertFalse(list(archive.rglob('*.neko-part')))
 def test_external_copy_matches_python_and_skips_partials(self):
  source=self.root/'archive';source.mkdir();(source/'photo.heic').write_bytes(b'image');(source/'database.sql').write_bytes(b'db');(source/'photo.neko-part').write_bytes(b'partial');(source/'db.sql.part').write_bytes(b'partial');dest=self.root/'native';ref=self.root/'python';env=self.commands();env['NEKO_FIXTURE_DATA']=str(dest);dest.mkdir();r=native('copy','--source',source,'--dest',dest,'--uuid','SSD-UUID','--apply',env=env);self.assertEqual(r.returncode,0,r.stderr);load('photo-backup.py').backup(source,ref,True);self.assertEqual({p.name:p.read_bytes() for p in dest.iterdir()},{p.name:p.read_bytes() for p in ref.iterdir()})
 def test_external_copy_wrong_uuid_blocks_all_writes(self):
  p=self.original();dest=self.root/'external';dest.mkdir();env=self.commands();r=native('copy','--source',p.parents[1],'--dest',dest,'--uuid','NO-SUCH-UUID','--apply',env=env);self.assertNotEqual(r.returncode,0);self.assertFalse(list(dest.iterdir()))
 def test_copy_collision_and_source_symlink_fail(self):
  source=self.root/'source';source.mkdir();(source/'photo').write_bytes(b'new');dest=self.root/'dest';dest.mkdir();(dest/'photo').write_bytes(b'old');env=self.commands();env['NEKO_FIXTURE_DATA']=str(dest);self.assertNotEqual(native('copy','--source',source,'--dest',dest,'--uuid','SSD-UUID','--apply',env=env).returncode,0);self.assertEqual((dest/'photo').read_bytes(),b'old');(source/'link').symlink_to(source/'photo');self.assertNotEqual(native('copy','--source',source,'--dest',self.root/'dry').returncode,0)
 def test_source_mutation_during_transfer_is_not_published(self):
  p=self.original('large.mp4');chunk=b'x'*1024**2
  with p.open('wb') as out:
   for _ in range(64):out.write(chunk)
  os.utime(p,(time.time()-1200,)*2);archive=self.root/'archive';target=archive/'upload/user/large.mp4';proc=subprocess.Popen([str(BIN),'archive','--source',str(p.parents[2]),'--archive',str(archive),'--apply'],stdout=subprocess.PIPE,stderr=subprocess.PIPE);partial=target.with_name(target.name+'.neko-part');deadline=time.time()+10
  while not partial.exists() and proc.poll() is None and time.time()<deadline:time.sleep(.001)
  self.assertTrue(partial.exists(),'fixture must observe active copy')
  with p.open('ab') as out:out.write(b'mutation')
  proc.communicate(timeout=20);self.assertNotEqual(proc.returncode,0);self.assertFalse(target.exists());self.assertFalse(partial.exists())
 def test_concurrent_destination_is_never_overwritten(self):
  p=self.original('large.mp4');chunk=b'x'*1024**2
  with p.open('wb') as out:
   for _ in range(64):out.write(chunk)
  os.utime(p,(time.time()-1200,)*2);archive=self.root/'archive';target=archive/'upload/user/large.mp4';proc=subprocess.Popen([str(BIN),'archive','--source',str(p.parents[2]),'--archive',str(archive),'--apply'],stdout=subprocess.PIPE,stderr=subprocess.PIPE);partial=target.with_name(target.name+'.neko-part');deadline=time.time()+10
  while not partial.exists() and proc.poll() is None and time.time()<deadline:time.sleep(.001)
  self.assertTrue(partial.exists());target.write_bytes(b'concurrent-owner');proc.communicate(timeout=20);self.assertNotEqual(proc.returncode,0);self.assertEqual(target.read_bytes(),b'concurrent-owner');self.assertFalse(partial.exists())
 def db_inputs(self):
  compose=self.data/'compose/immich/compose.yaml';compose.parent.mkdir(parents=True);compose.write_text('fixture');env=compose.with_name('.env');env.write_text('SECRET=PRIVATE-CONFIG');return compose,env,self.data/'backups/immich-database'
 def test_database_dump_private_and_no_overwrite(self):
  compose,env,dest=self.db_inputs();r=native('database','--compose',compose,'--env',env,'--dest',dest,'--apply',env=self.commands());self.assertEqual(r.returncode,0,r.stderr);dumps=list(dest.glob('*.sql'));self.assertEqual(len(dumps),1);self.assertEqual(dumps[0].stat().st_mode&0o777,0o600);self.assertFalse(list(dest.glob('*.part')));self.assertNotIn('PRIVATE',r.stdout+r.stderr)
 def test_failed_database_dump_is_not_published(self):
  compose,env,dest=self.db_inputs();e=self.commands();e['NEKO_FIXTURE_DUMP']='fail';r=native('database','--compose',compose,'--env',env,'--dest',dest,'--apply',env=e);self.assertNotEqual(r.returncode,0);self.assertFalse(list(dest.iterdir()));self.assertNotIn('PRIVATE',r.stdout+r.stderr)
 def test_preservation_verifies_uuids_before_any_job_write(self):
  self.original();env=self.commands();env['NEKO_FIXTURE_MOUNT']='wrong';r=native('preservation','--config',self.config,'--data',self.data,'--media',self.media,env=env);self.assertNotEqual(r.returncode,0);self.assertFalse((self.data/'reports').exists());self.assertFalse((self.media/'photos/archive').exists())

 def test_preservation_reports_success_and_copies_database_to_both_drives(self):
  self.original();self.db_inputs();env=self.commands();lock=Path('/run/nekoserver-photo-preservation.lock');owned=not lock.exists()
  try:
   r=native('preservation','--config',self.config,'--data',self.data,'--media',self.media,'--db-backup',env=env);self.assertEqual(r.returncode,0,r.stderr);report=self.data/'reports/jobs/database.json';saved=json.loads(report.read_text());self.assertEqual(saved['status'],'pass');self.assertIsNotNone(saved['last_success']);self.assertEqual(report.stat().st_mode&0o777,0o600);ssd=list((self.data/'backups/immich-database').glob('*.sql'));hdd=list((self.media/'backups/immich-database').glob('*.sql'));self.assertEqual(len(ssd),1);self.assertEqual(len(hdd),1);self.assertEqual(ssd[0].read_bytes(),hdd[0].read_bytes());self.assertEqual(hdd[0].stat().st_mode&0o777,0o600);self.assertEqual((self.media/'photos/archive/upload/user/photo.JPG').read_bytes(),b'original');self.assertNotIn('PRIVATE',report.read_text()+r.stdout+r.stderr)
  finally:
   if owned:lock.unlink(missing_ok=True)
 def test_preservation_failure_retains_last_success_without_false_pass(self):
  self.original();self.db_inputs();folder=self.data/'reports/jobs';folder.mkdir(parents=True);stamp=time.time()-60;(folder/'database.json').write_text(json.dumps({'status':'pass','last_success':stamp}));env=self.commands();env['NEKO_FIXTURE_DUMP']='fail';lock=Path('/run/nekoserver-photo-preservation.lock');owned=not lock.exists()
  try:
   r=native('preservation','--config',self.config,'--data',self.data,'--media',self.media,'--db-backup',env=env);self.assertNotEqual(r.returncode,0);saved=json.loads((folder/'database.json').read_text());self.assertEqual(saved['status'],'fail');self.assertEqual(saved['last_success'],stamp);self.assertFalse(list((self.data/'backups/immich-database').glob('*.sql')));self.assertFalse((self.media/'backups').exists())
  finally:
   if owned:lock.unlink(missing_ok=True)
 def test_busy_preservation_lock_blocks_duplicate_job(self):
  import fcntl
  self.original();lock=Path('/run/nekoserver-photo-preservation.lock');owned=not lock.exists()
  try:
   with lock.open('a') as f:
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);r=native('preservation','--config',self.config,'--data',self.data,'--media',self.media,env=self.commands());self.assertNotEqual(r.returncode,0);self.assertFalse((self.data/'reports').exists());self.assertFalse((self.media/'photos/archive').exists())
  finally:
   if owned:lock.unlink(missing_ok=True)

class CollectorTests(Fixture):
 def collect(self,env):
  output=self.root/'public/status.json';state=self.data/'dashboard-state';r=native('collect','--config',self.config,'--output',output,'--state',state,env=env);self.assertEqual(r.returncode,0,r.stderr);return json.loads(output.read_text()),output,state
 def test_snapshot_matches_python_sanitized_contract(self):
  env=self.commands();now=time.time();(self.data/'reports/jobs').mkdir(parents=True)
  for key in ('archive','database','external'):(self.data/f'reports/jobs/{key}.json').write_text(json.dumps({'status':'pass','last_success':now,'started_at':now,'secret':'PRIVATE-JOB'}))
  (self.data/'reports/media').mkdir();(self.data/'reports/media/latest.json').write_text(json.dumps({'generated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'services':{'jellyfin':{'result':'pass','secret':'PRIVATE-API'}},'secret':'PRIVATE-API'}))
  result,output,state=self.collect(env)
  with patch.dict(os.environ,env):reference=py_collect.collect(self.cfg,self.root/'reference/status.json',self.data/'python-state')
  self.assertEqual(result['applications'],reference['applications']);self.assertEqual(result['backups'],reference['backups']);self.assertEqual(result['recovery'],reference['recovery']);self.assertEqual(result['acceptance'],reference['acceptance']);self.assertEqual(result['alerts'],reference['alerts']);self.assertEqual(result['media']['counts'],reference['media']['counts']);self.assertEqual(result['media']['checks'],reference['media']['checks']);self.assertEqual(result['media']['status'],reference['media']['status']);self.assertEqual([(d['id'],d['status'],d['smart']['status']) for d in result['storage']],[(d['id'],d['status'],d['smart']['status']) for d in reference['storage']]);self.assertEqual(output.stat().st_mode&0o777,0o644);self.assertEqual((state/'collector.json').stat().st_mode&0o777,0o600);self.assertNotIn('PRIVATE',output.read_text())
 def test_wrong_drive_blocks_private_reads_and_state_write(self):
  state=self.data/'dashboard-state';state.mkdir();(state/'collector.json').write_text(json.dumps({'history':[{'at':time.time(),'cpu':42,'memory':33,'temperature':44}]}));before=(state/'collector.json').read_bytes();(self.data/'reports/jobs').mkdir(parents=True);(self.data/'reports/jobs/archive.json').write_text(json.dumps({'status':'pass','last_success':time.time()}));env=self.commands();env['NEKO_FIXTURE_MOUNT']='wrong';result,output,_=self.collect(env);self.assertEqual((state/'collector.json').read_bytes(),before);self.assertEqual(len(result['history']),1);self.assertEqual(result['backups'][0]['status'],'unknown');self.assertEqual(next(d for d in result['storage'] if d['id']=='data_ssd')['status'],'wrong_drive')
 def test_second_sample_retains_history_and_smart_cache(self):
  env=self.commands();self.collect(env);result,_,_=self.collect(env);self.assertEqual(len(result['history']),2);self.assertIsNotNone(result['metrics']['cpu_percent']);self.assertGreaterEqual(result['metrics']['cpu_percent'],0);self.assertLessEqual(result['metrics']['cpu_percent'],100)
 def test_acceptance_summary_matches_python_for_valid_and_invalid_witness(self):
  env=self.commands();folder=self.data/'reports/acceptance';folder.mkdir(parents=True);now=time.time();base={'format':'neko-acceptance-v1','version':'0.15.1','profile':'full','generated_at':now,'dashboard_scope':acceptance_summary.scope_digest(self.cfg),'automatic':[{'id':'storage','status':'pass'}],'manual':[{'id':'gateway_clients','status':'pass','evidence':'user_reported','confirmed_at':now}],'acceptance_complete':True}
  for mutate in ({},{'version':'old'},{'generated_at':now-90000},{'manual':[{'id':'gateway_clients','status':'pass'}]},{'manual':[{'id':'secret','status':'pass'}]},{'manual':[]},{'automatic':[{'status':'fail'}]}):
   report={**base,**mutate};(folder/'latest.json').write_text(json.dumps(report));result,_,_=self.collect(env);self.assertEqual(result['acceptance'],acceptance_summary.summarize(report,self.cfg,result['generated_at']))
 def test_malformed_or_symlinked_status_inputs_stay_private(self):
  folder=self.data/'reports/jobs';folder.mkdir(parents=True);(folder/'archive.json').write_text('not JSON PRIVATE');(folder/'database.json').symlink_to(folder/'archive.json');r,output,_=self.collect(self.commands());self.assertEqual(r['backups'][0]['status'],'unknown');self.assertEqual(r['backups'][1]['status'],'unknown');self.assertNotIn('PRIVATE',output.read_text())
 def test_wrong_state_path_rejected(self):
  r=native('collect','--config',self.config,'--output',self.root/'public/status.json','--state',self.root/'wrong',env=self.commands());self.assertNotEqual(r.returncode,0);self.assertFalse((self.root/'public').exists())

class ServerTests(Fixture):
 def setUp(self):
  super().setUp();self.root.chmod(0o755);self.assets=self.root/'assets';shutil.copytree(ROOT/'dashboard',self.assets);self.status=self.root/'status.json';self.status.write_text(json.dumps({'format':'neko-dashboard-v1','version':'0.15.1'}));self.binary=self.root/'runtime';server_binary=Path(os.environ.get('NEKO_TEST_SERVER_BINARY',str(BIN)));shutil.copyfile(server_binary,self.binary);self.binary.chmod(0o755);self.port=free_port();self.proc=subprocess.Popen([str(self.binary),'serve','--assets',str(self.assets),'--status',str(self.status),'--port',str(self.port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  deadline=time.time()+5
  while time.time()<deadline:
   try:
    with socket.create_connection(('127.0.0.1',self.port),timeout=.1):return
   except OSError:
    if self.proc.poll() is not None:break
    time.sleep(.02)
  self.fail('native server did not start: '+str(self.proc.communicate(timeout=1)))
 def tearDown(self):
  self.proc.terminate();self.proc.communicate(timeout=5);super().tearDown()
 def request(self,path='/',method='GET',headers=None):
  conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5);conn.request(method,path,headers=headers or {});r=conn.getresponse();data=(r.status,dict(r.getheaders()),r.read());conn.close();return data
 def raw(self,payload):
  with socket.create_connection(('127.0.0.1',self.port),timeout=5) as s:
   s.sendall(payload);out=b''
   while True:
    try:block=s.recv(65536)
    except ConnectionResetError:break
    if not block:break
    out+=block
   return out
 def test_assets_status_and_security_headers_match(self):
  for route,name in [('/','index.html'),('/index.html','index.html'),('/app.js','app.js'),('/style.css','style.css'),('/logo.svg','logo.svg')]:
   code,headers,body=self.request(route);self.assertEqual(code,200);self.assertEqual(body,(self.assets/name).read_bytes());self.assertEqual(headers['Cache-Control'],'no-store');self.assertEqual(headers['X-Content-Type-Options'],'nosniff');self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy']);self.assertEqual(headers['Connection'],'close')
  self.assertEqual(self.request('/api/status?x=1')[2],self.status.read_bytes())
 def test_host_origin_and_cross_site_denied(self):
  for headers in [{'Host':'attacker.invalid'},{'Origin':'https://attacker.invalid'},{'Sec-Fetch-Site':'cross-site'}]:self.assertEqual(self.request(headers=headers)[0],403)
  self.assertEqual(self.request(headers={'Host':f'localhost:{self.port}','Origin':f'http://localhost:{self.port}'})[0],200)
 def test_head_and_mutations(self):
  code,h,b=self.request(method='HEAD');self.assertEqual(code,200);self.assertEqual(b,b'');self.assertEqual(int(h['Content-Length']),(self.assets/'index.html').stat().st_size)
  for method in ('POST','PUT','PATCH','DELETE','OPTIONS'):self.assertEqual(self.request(method=method)[0],405)
 def test_exact_routes_prevent_file_access(self):
  for route in ('/.env','/config.json','/../config.json','/%2e%2e/config.json','//api/status','/preview.html','http://localhost/api/status'):self.assertEqual(self.request(route,headers={'Host':f'127.0.0.1:{self.port}'})[0],404)
 def test_symlink_and_malformed_or_oversize_status_fail_closed(self):
  self.status.write_text('bad JSON');self.assertEqual(self.request('/api/status')[0],503);self.status.write_text(json.dumps({'format':'wrong'}));self.assertEqual(self.request('/api/status')[0],503);self.status.write_bytes(b' '*2_000_001);self.assertEqual(self.request('/api/status')[0],503);self.status.unlink();self.status.symlink_to(self.assets/'index.html');self.assertEqual(self.request('/api/status')[0],503);(self.assets/'app.js').unlink();(self.assets/'app.js').symlink_to(self.assets/'index.html');self.assertEqual(self.request('/app.js')[0],503)
 def test_duplicate_host_and_body_framing_are_rejected(self):
  prefix=f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'.encode()
  for extra in [f'Host: 127.0.0.1:{self.port}\r\n'.encode(),b'Content-Length: 1\r\n',b'Transfer-Encoding: chunked\r\n',b'Content-Length: 0\r\nContent-Length: 0\r\n']:
   response=self.raw(prefix+extra+b'\r\n');self.assertTrue(response.startswith((b'HTTP/1.1 400',b'HTTP/1.1 403')),response[:80])
 def test_large_headers_bounded_and_pool_stays_fixed(self):
  response=self.raw(f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX: '.encode()+b'x'*17000+b'\r\n\r\n');self.assertTrue(response.startswith(b'HTTP/1.1 431'));metrics=json.loads(self.request('/__fixture_metrics')[2]);self.assertEqual(metrics['threads'],5);self.assertEqual(self.request()[0],200)
 def test_loopback_only_listener_and_root_refusal(self):
  for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
   f=line.split()
   if f[1].endswith(':'+format(self.port,'04X')) and f[3]=='0A':self.assertTrue(f[1].startswith('0100007F:'))
  if os.geteuid()==0:self.assertNotEqual(native('serve','--assets',self.assets,'--port',free_port()).returncode,0)

class DeploymentTests(Fixture):
 def test_default_rust_legacy_python_and_invalid_backend(self):
  self.assertEqual(native_runtime.backend(CFG),'rust');self.assertEqual(native_runtime.backend({}),'python')
  with self.assertRaises(RuntimeError):native_runtime.backend({'runtime':{'backend':'other'}})
  import installer
  for value in ('bad',None,{'backend':'other'}):
   cfg=copy.deepcopy(CFG);cfg['runtime']=value
   with self.assertRaises(installer.InstallerError):installer.validate_config(cfg)
 def test_binary_and_source_integrity_are_checked(self):
  self.assertEqual(native_runtime.payload(ROOT,CFG),BIN.read_bytes());project=self.root/'project';shutil.copytree(ROOT/'runtime',project/'runtime',ignore=shutil.ignore_patterns('target'))
  (project/'runtime/bin/nekoserver-runtime').write_bytes(b'wrong')
  with self.assertRaisesRegex(RuntimeError,'checksum'):native_runtime.payload(project,CFG)
  shutil.copyfile(BIN,project/'runtime/bin/nekoserver-runtime');(project/'runtime/src/main.rs').write_text('changed')
  with self.assertRaisesRegex(RuntimeError,'source differs'):native_runtime.payload(project,CFG)
 def test_custom_native_file_preserved(self):
  dest=self.root/'runtime';path=native_runtime.deploy(ROOT,CFG,dest);self.assertTrue(path.stat().st_mode&0o111);self.assertEqual(native_runtime.deploy(ROOT,CFG,dest),path);path.write_bytes(b'custom')
  with self.assertRaisesRegex(RuntimeError,'custom file preserved'):native_runtime.deploy(ROOT,CFG,dest)
  self.assertEqual(path.read_bytes(),b'custom')
 def test_native_units_keep_hardening_and_python_fallback(self):
  rust=dashboard.units(self.data,self.root/'code',self.root/'etc/config.json',8787,60,'rust');python=dashboard.units(self.data,self.root/'code',self.root/'etc/config.json',8787,60,'python')
  self.assertIn('nekoserver-runtime serve',rust['nekoserver-dashboard.service']);self.assertIn('nekoserver-runtime collect',rust['nekoserver-dashboard-collect.service']);self.assertIn('DynamicUser=yes',rust['nekoserver-dashboard.service']);self.assertIn('InaccessiblePaths=-/run/docker.sock',rust['nekoserver-dashboard.service']);self.assertIn('dashboard_server.py',python['nekoserver-dashboard.service'])
 def test_recovery_preserves_native_source_lock_and_executable(self):
  with patch.object(recovery.os.path,'ismount',return_value=True):saved=recovery.preserve(self.data,ROOT,self.cfg)
  for rel in ('runtime/Cargo.lock','runtime/build.py','runtime/build.json','runtime/src/main.rs','runtime/bin/nekoserver-runtime'):self.assertTrue((saved/rel).is_file())
  self.assertTrue((saved/'runtime/bin/nekoserver-runtime').stat().st_mode&0o111)
 def test_native_preservation_unit_has_identity_guard(self):
  self.original();units=self.root/'units';guard=self.root/'guard.json';real=Path
  def mapped(value,*args):
   p=real(value,*args)
   return units/p.relative_to('/etc/systemd/system') if str(p).startswith('/etc/systemd/system') else p
  with patch.object(photo_preservation,'Path',side_effect=mapped),patch.object(photo_preservation.subprocess,'run',return_value=SimpleNamespace(returncode=0)),patch.object(native_runtime,'backup_guard',return_value=guard):photo_preservation.run(SimpleNamespace(config=self.cfg,apply=True,log=lambda _:None))
  text=(units/'nekoserver-immich-db-backup.service').read_text();self.assertIn('nekoserver-runtime preservation --config '+str(guard),text);self.assertNotIn('photo-preservation-job.py',text)
 def test_native_weekly_unit_uses_same_worker(self):
  (self.media/'photos/archive').mkdir(parents=True);self.cfg['photos']['external_backup_uuid']='EXT-12345678';units=self.root/'units';guard=self.root/'guard.json';real=Path
  def mapped(value,*args):
   p=real(value,*args)
   return units/p.relative_to('/etc/systemd/system') if str(p).startswith('/etc/systemd/system') else p
  with patch.object(photo_backup,'Path',side_effect=mapped),patch.object(photo_backup.subprocess,'run',return_value=SimpleNamespace(returncode=0)),patch.object(native_runtime,'backup_guard',return_value=guard):photo_backup.run(SimpleNamespace(config=self.cfg,apply=True,log=lambda _:None))
  text=(units/'nekoserver-photo-backup.service').read_text();self.assertIn('nekoserver-runtime weekly --config '+str(guard),text);self.assertIn('--media-root '+str(self.media),text)
 def test_cli_rejects_unknown_repeated_or_missing_options(self):
  for args in [('other',),('archive','--source','a','--source','b'),('serve','--wildcard','0.0.0.0'),('archive','--source')]:self.assertNotEqual(native(*args).returncode,0)

 def test_production_binary_has_no_fixture_route_or_marker(self):
  self.assertNotIn(b'/__fixture_metrics',BIN.read_bytes());self.assertEqual(native('--version').stdout.strip(),'nekoserver-runtime 0.21.0')
 def test_native_acceptance_rejects_missing_and_wrong_loaded_commands(self):
  with patch('dashboard_collect.run',return_value=(0,'ExecStart=/usr/bin/python3 old.py')):rows=native_runtime.acceptance_checks(self.cfg,['dashboard','photo_preservation'])
  self.assertEqual(len(rows),3);self.assertNotIn('runtime:nekoserver-photo-preservation.service',{r['id'] for r in rows});self.assertTrue(all(r['status']=='needs_setup' for r in rows))
