import copy,hashlib,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import acceptance as a
import acceptance_summary as summary
import dashboard_collect as health
from modules import recovery
ROOT=Path(__file__).resolve().parents[1];CONFIG=json.loads((ROOT/'config.json').read_text())
BOOT1='a'*8+'-'+'a'*4+'-'+'a'*4+'-'+'a'*4+'-'+'a'*12
BOOT2='b'*8+'-'+'b'*4+'-'+'b'*4+'-'+'b'*4+'-'+'b'*12

class AcceptanceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.config=copy.deepcopy(CONFIG);self.config.update(data_mount=str(self.root/'ssd'),media_mount=str(self.root/'hdd'),external_backup_mount=str(self.root/'usb'));self.config['docker']['persistent_root']=self.config['data_mount'];self.config['storage']={'data_ssd':{'uuid':'SSD','fstype':'ext4'},'media_hdd':{'uuid':'HDD','fstype':'ext4'}};self.ssd=Path(self.config['data_mount']);self.ssd.mkdir();Path(self.config['media_mount']).mkdir()
 def tearDown(self):self.tmp.cleanup()
 def inspect(self,**kwargs):
  with patch.object(a.recovery_tools,'host_supported',return_value=True),patch.object(health,'mount_info',return_value={'status':'mounted'}),patch.object(a,'boot_id',return_value=BOOT1),patch.object(a.recovery_tools,'verify_bundle',return_value='0.8.1'),patch.object(a,'dashboard_check',return_value=True),patch.object(a,'service',return_value=True):return a.inspect(self.config,'dashboard',1000,**kwargs)
 def test_first_run_requires_reboot_and_witnessed_tests(self):
  result=self.inspect();self.assertFalse(result['acceptance_complete']);self.assertEqual(next(x['status'] for x in result['automatic'] if x['id']=='reboot'),'needs_setup');self.assertTrue(all(x['status']=='needs_setup' for x in result['manual']))
 def test_invalid_saved_baseline_cannot_prove_reboot(self):
  first=self.inspect();first.update(baseline_boot_id=BOOT2,baseline_verified=False);path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(first));self.assertEqual(next(x['status'] for x in self.inspect()['automatic'] if x['id']=='reboot'),'needs_setup')
 def test_same_boot_cannot_pass_reboot(self):
  first=self.inspect();path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(first));self.assertEqual(next(x['status'] for x in self.inspect()['automatic'] if x['id']=='reboot'),'needs_setup')
 def test_later_boot_with_matching_config_passes_reboot_only(self):
  first=self.inspect();first['baseline_boot_id']=BOOT2;path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(first));result=self.inspect();self.assertEqual(next(x['status'] for x in result['automatic'] if x['id']=='reboot'),'pass');self.assertFalse(result['acceptance_complete'])
 def test_changed_profile_or_config_invalidates_boot_evidence(self):
  first=self.inspect();first['baseline_boot_id']=BOOT2;first['config_fingerprint']='wrong';path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(first));self.assertEqual(next(x['status'] for x in self.inspect()['automatic'] if x['id']=='reboot'),'needs_setup')
 def test_repeated_same_boot_retains_verified_reboot_evidence(self):
  first=self.inspect();first.update(baseline_boot_id=BOOT2,reboot_boot_id=BOOT1,reboot_verified_at=900);path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(first));self.assertEqual(next(x['status'] for x in self.inspect()['automatic'] if x['id']=='reboot'),'pass')
 def test_dashboard_scope_does_not_inspect_media_or_docker(self):
  with patch.object(a,'docker_info',side_effect=AssertionError('docker')),patch.object(a,'media_checks',side_effect=AssertionError('media')):self.inspect()
 def test_invalid_configuration_returns_failure_without_private_exception(self):
  self.config['media']['services']=['INVALID-PRIVATE'];result=a.inspect(self.config);self.assertEqual(result['status'],'failed');self.assertNotIn('PRIVATE',json.dumps(result))
 def test_unknown_profile_fails_without_running_commands(self):
  with patch.object(health,'run',side_effect=AssertionError('command')):self.assertEqual(a.inspect(self.config,'INVALID')['status'],'failed')
 def test_wrong_mount_prevents_private_report_reads_and_docker_inspection(self):
  self.config['profiles']['minimal']=['system','storage','recovery','docker','media']
  with patch.object(a.recovery_tools,'host_supported',return_value=True),patch.object(health,'mount_info',return_value={'status':'wrong_drive'}),patch.object(a,'load_json',side_effect=AssertionError('private read')),patch.object(a,'docker_info',side_effect=AssertionError('Docker')):result=a.inspect(self.config,'minimal',1000)
  self.assertEqual(result['status'],'failed');self.assertTrue(any(x['status']=='blocked' for x in result['automatic']))
 def test_blank_uuid_stays_setup_required(self):
  self.config['storage']['data_ssd']['uuid']=''
  with patch.object(a.recovery_tools,'host_supported',return_value=True),patch.object(health,'mount_info',return_value={'status':'not_configured'}),patch.object(a,'dashboard_check',return_value=False),patch.object(a,'service',return_value=False):result=a.inspect(self.config,'dashboard',1000)
  self.assertEqual(next(x['status'] for x in result['automatic'] if x['id']=='mount:data_ssd'),'needs_setup')
 def test_save_rechecks_drives_before_creating_report(self):
  with patch.object(a.recovery_tools,'mounts',side_effect=RuntimeError('wrong drive')):
   with self.assertRaises(RuntimeError):a.save(self.config,self.inspect())
  self.assertFalse((self.ssd/'reports').exists())
 def test_saved_report_private_permissions(self):
  result=self.inspect()
  with patch.object(a.recovery_tools,'mounts'):path=a.save(self.config,result)
  self.assertEqual(path.stat().st_mode&0o777,0o600)
 def test_manual_requires_exact_test_confirmation(self):
  report={'automatic':[a.item('x','x','pass','')],'manual':[a.item('restore','Restore','needs_setup','Instruction') ]};a.record_manual(report,prompt=lambda _: 'PASS');self.assertFalse(report['acceptance_complete']);a.record_manual(report,prompt=lambda _:'PASS restore');self.assertTrue(report['acceptance_complete']);self.assertEqual(report['manual'][0]['evidence'],'user_reported')
 def test_failed_witnessed_test_blocks_acceptance(self):
  report={'automatic':[a.item('x','x','pass','')],'manual':[a.item('restore','Restore','needs_setup','') ]};a.record_manual(report,prompt=lambda _:'FAIL restore');self.assertEqual(report['status'],'failed')
 def test_automatic_failure_cannot_be_overridden_by_manual_pass(self):
  report={'automatic':[a.item('x','x','fail','')],'manual':[a.item('restore','Restore','needs_setup','') ]};a.record_manual(report,prompt=lambda _:'PASS restore');self.assertFalse(report['acceptance_complete'])
 def test_legacy_or_unobserved_manual_results_not_carried_to_new_run(self):
  previous=self.inspect();previous['manual']=[a.item('missing_drive','Missing drive','pass','')];previous['baseline_boot_id']=BOOT2;path=self.ssd/'reports/acceptance/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(previous));self.assertTrue(all(x['status']=='needs_setup' for x in self.inspect()['manual']))
 def test_scoped_manual_requirements_and_photo_disabled(self):
  minimal=a.manual_tests(['storage','dashboard'],self.config);self.assertEqual([x['id'] for x in minimal],['missing_drive']);self.config['photos']['deploy_immich']=False;self.assertNotIn('independent_restore',[x['id'] for x in a.manual_tests(['photos'],self.config)])
 def test_docker_wrong_root_or_containerd_cannot_pass(self):
  for value in ({'DockerRootDir':'/var/lib/docker','Driver':'overlay2'}, {'DockerRootDir':str(self.ssd/'docker-engine'),'Driver':'overlay2','DriverStatus':[['driver-type','io.containerd.snapshotter.v1']]},[]):
   with patch.object(health,'run',return_value=(0,json.dumps(value))):self.assertFalse(a.docker_info(self.config)[0])
 def test_docker_info_redacts_non_status_data(self):
  value={'DockerRootDir':str(self.ssd/'docker-engine'),'Driver':'overlay2','ServerVersion':'29.0.0','PRIVATE':'SECRET'}
  with patch.object(health,'run',return_value=(0,json.dumps(value))):self.assertEqual(a.docker_info(self.config),(True,'29.0.0'))
 def container(self,**kwargs):
  value={'Config':{'Labels':{'com.docker.compose.project':'neko-jellyfin','com.docker.compose.service':'jellyfin'},'Env':['PRIVATE=SECRET']},'State':{'Status':'running'},'Mounts':[{'Type':'bind','Source':str(self.ssd/'appdata/jellyfin'),'Destination':'/config','RW':True},{'Type':'bind','Source':str(Path(self.config['media_mount'])/'library/movies'),'Destination':'/data/movies','RW':False},{'Type':'bind','Source':str(Path(self.config['media_mount'])/'library/tv'),'Destination':'/data/tv','RW':False}],'NetworkSettings':{'Ports':{'8096/tcp':[{'HostIp':'127.0.0.1','HostPort':'8096'}]},'Networks':{'nekoserver-media':{}}}};value.update(kwargs);return value
 def probe_container(self,value):
  self.config['media']['services']=['jellyfin']
  with patch.object(health,'run',side_effect=[(0,'a'*64+'\n'),(0,json.dumps([value]))]):return a.containers(self.config)[0]
 def test_correct_container_mounts_network_and_bindings_pass(self):self.assertEqual(self.probe_container(self.container())['status'],'pass')
 def test_public_bind_cannot_pass(self):
  value=self.container();value['NetworkSettings']['Ports']['8096/tcp'][0]['HostIp']='0.0.0.0';self.assertEqual(self.probe_container(value)['status'],'fail')
 def test_wrong_mount_destination_cannot_pass(self):
  value=self.container();value['Mounts'][0]['Destination']='/tmp/unused';self.assertEqual(self.probe_container(value)['status'],'fail')
 def test_writable_jellyfin_library_fails_protected_mount_check(self):
  value=self.container();value['Mounts'][1]['RW']=True;self.assertEqual(self.probe_container(value)['status'],'fail')
 def test_unhealthy_container_fails(self):
  value=self.container(State={'Status':'running','Health':{'Status':'unhealthy'}});self.assertEqual(self.probe_container(value)['status'],'fail')
 def test_duplicate_containers_are_not_inspected_or_passed(self):
  self.config['media']['services']=['jellyfin']
  with patch.object(health,'run',return_value=(0,'a'*64+'\n'+'b'*64+'\n')) as call:self.assertEqual(a.containers(self.config)[0]['status'],'fail');self.assertEqual(call.call_count,1)
 def test_container_environment_never_serialized(self):self.assertNotIn('SECRET',json.dumps(self.probe_container(self.container())))
 def test_stale_dashboard_snapshot_fails(self):
  response=SimpleNamespace(status=200,read=lambda n:json.dumps({'format':'neko-dashboard-v1','generated_at':1}).encode());response.__enter__=lambda s:s
  class R:
   status=200
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,n):return json.dumps({'format':'neko-dashboard-v1','generated_at':1}).encode()
  with patch.object(a.OPENER,'open',return_value=R()):self.assertFalse(a.dashboard_check(8787,1000))
 def test_service_requires_active_enabled_loaded(self):
  for out in ('LoadState=loaded\nActiveState=active\nUnitFileState=disabled','LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled'):
   with patch.object(health,'run',return_value=(0,out)):self.assertFalse(a.service('docker.service'))
 def test_guard_requires_actual_loaded_non_ignored_command(self):
  self.config['runtime']={'backend':'python'};runtime=self.ssd/'installer/guards/v0.8.1';runtime.mkdir(parents=True);(runtime/'storage-guard.py').write_bytes((ROOT/'storage-guard.py').read_bytes());(runtime/'storage.json').write_text(json.dumps({k:self.config[k] for k in ('data_mount','media_mount','storage')}));command='/usr/bin/python3 '+str(runtime/'storage-guard.py')+' --config '+str(runtime/'storage.json');text='# Managed by NekoServer\n[Unit]\nRequiresMountsFor='+self.config['data_mount']+' '+self.config['media_mount']+'\nBindsTo=ssd.mount hdd.mount\n[Service]\nExecStartPre='+command+'\n';override=self.root/'override';override.write_text(text);real=a.safe_path
  def safe(value):return override if str(value)=='/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf' else real(value)
  with patch.object(a,'safe_path',side_effect=safe),patch.object(a.docker_module,'guard_contents',return_value=text):
   for ignore,expected in [('no',True),('yes',False)]:
    loaded='ExecStartPre={ argv[]='+command+' ; ignore_errors='+ignore+' ; }\nRequiresMountsFor='+self.config['data_mount']+' '+self.config['media_mount']+'\nBindsTo=ssd.mount hdd.mount'
    with patch.object(health,'run',return_value=(0,loaded)):self.assertEqual(a.guard_check(self.config),expected)
 def test_cli_manual_rejects_noninteractive_or_unsaved_session(self):
  with patch.object(a.sys.stdin,'isatty',return_value=False):
   with self.assertRaises(SystemExit):a.main(['--manual','--save'])
  with patch.object(a.sys.stdin,'isatty',return_value=True):
   with self.assertRaises(SystemExit):a.main(['--manual'])
 def test_cli_default_inspection_never_saves(self):
  with patch.object(a.os,'geteuid',return_value=0),patch.object(a,'load_json',return_value=self.config),patch.object(a,'inspect',return_value=self.inspect()),patch.object(a,'save',side_effect=AssertionError('write')):self.assertEqual(a.main(['--profile','dashboard']),1)
 def test_bundle_preserves_acceptance_imports_and_guide(self):
  with patch.object(recovery.os.path,'ismount',return_value=True):bundle=recovery.preserve(self.ssd,ROOT,self.config)
  self.assertTrue((bundle/'acceptance.py').is_file());self.assertTrue((bundle/'acceptance_summary.py').is_file())

class SummaryTests(unittest.TestCase):
 def setUp(self):
  self.config=copy.deepcopy(CONFIG);self.saved={'format':'neko-acceptance-v1','version':a.VERSION,'generated_at':900,'profile':'full','dashboard_scope':summary.scope_digest(self.config),'automatic':[a.item('x','SECRET','pass','PASSWORD')],'manual':[a.item('independent_restore','SECRET','needs_setup','PASSWORD')],'SECRET':'PRIVATE'}
 def test_public_summary_recomputes_counts_and_redacts_details(self):
  self.saved['summary']={'automatic':{'pass':900}};result=summary.summarize(self.saved,self.config,1000);self.assertEqual(result['automatic']['pass'],1);self.assertEqual(result['status'],'pending');self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('PASSWORD',json.dumps(result));self.assertEqual(result['manual_checks'][0]['name'],'Independent full restore')
 def test_stale_complete_report_stays_stale(self):
  self.saved['manual'][0].update(status='pass',evidence='user_reported',confirmed_at=900);self.assertEqual(summary.summarize(self.saved,self.config,90000)['status'],'stale')
 def test_version_and_config_changes_mark_outdated(self):
  self.saved['version']='0.1';self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'outdated');self.saved['version']=a.VERSION;self.config['media']['services']=['jellyfin'];self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'outdated')
 def test_no_report_or_invalid_timestamp_cannot_pass(self):
  for value in ({},{**self.saved,'generated_at':float('nan')},{**self.saved,'generated_at':True},{**self.saved,'generated_at':5000}):self.assertEqual(summary.summarize(value,self.config,1000)['status'],'unknown')
 def test_empty_malformed_or_unknown_manual_items_cannot_pass(self):
  for rows in ([],None,[{'id':'SECRET','status':'pass'}],[{'id':'independent_restore','status':'invented'}]):
   self.saved['manual']=rows;self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'unknown')
 def test_manual_pass_without_witness_metadata_cannot_complete(self):
  self.saved['acceptance_complete']=True;self.saved['manual'][0]['status']='pass';self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'unknown')
 def test_complete_requires_all_checks_and_witness_metadata(self):
  self.saved['acceptance_complete']=True;self.saved['manual'][0].update(status='pass',evidence='user_reported',confirmed_at=900);self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'complete')
 def test_failure_overrides_complete_flag(self):
  self.saved['acceptance_complete']=True;self.saved['automatic'][0]['status']='fail';self.assertEqual(summary.summarize(self.saved,self.config,1000)['status'],'failed')
 def test_missing_ssd_does_not_read_acceptance_report(self):
  cfg=copy.deepcopy(self.config);disk={'id':'data_ssd','status':'wrong_drive','smart':{'status':'unknown'},'name':'Application SSD'}
  with tempfile.TemporaryDirectory() as tmp,patch.object(health,'mount_info',return_value={'status':'wrong_drive'}),patch.object(health,'read_json',side_effect=lambda path,*args: {} if str(path) in ('/run/nekoserver-notifications/delivery.json','/run/nekoserver-heartbeat/status.json') else (_ for _ in ()).throw(AssertionError('SSD private read'))),patch.object(health,'storage',return_value=([disk],False)),patch.object(health,'metrics',return_value=({'cpu_percent':None,'memory_percent':None,'temperature':None},{})),patch.object(health,'applications',return_value=[]),patch.object(health,'backups',return_value=[]),patch.object(health,'media_report',return_value={'status':'unknown','counts':{}}),patch.object(health,'recovery_status',return_value={}):self.assertEqual(health.collect(cfg,Path(tmp)/'public.json',Path(tmp)/'private',1000)['acceptance']['status'],'unknown')

if __name__=='__main__':unittest.main()
