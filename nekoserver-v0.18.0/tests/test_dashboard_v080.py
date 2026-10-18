import copy,importlib.util,json,os,socket,tempfile,threading,time,unittest,urllib.error,urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import dashboard_collect as collect
import dashboard_server as web
import job_status
import installer
from modules import dashboard,recovery
ROOT=Path(__file__).resolve().parents[1];CONFIG=json.loads((ROOT/'config.json').read_text())

class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg=copy.deepcopy(CONFIG);self.cfg['photos']['backup_schedule']='legacy';self.cfg['photos']['archive_interval_minutes']=360;self.cfg['monitoring']['enabled']=True;self.cfg.update(data_mount=str(self.root/'ssd'),media_mount=str(self.root/'hdd'),external_backup_mount=str(self.root/'usb'));self.cfg['storage']={'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'ext4'}};self.cfg['docker']['persistent_root']=self.cfg['data_mount'];self.ssd=Path(self.cfg['data_mount']);self.hdd=Path(self.cfg['media_mount']);self.ssd.mkdir();self.hdd.mkdir()
    def tearDown(self):self.tmp.cleanup()
    def test_cpu_delta_uses_idle_and_iowait(self):
        self.assertEqual(collect.cpu_percent({'total':200,'idle':80,'boot':'a'},{'total':100,'idle':40,'boot':'a'}),60)
    def test_cpu_boot_change_is_unknown(self):
        self.assertIsNone(collect.cpu_percent({'total':200,'idle':80,'boot':'b'},{'total':100,'idle':40,'boot':'a'}))
    def test_cpu_invalid_or_missing_previous_is_unknown(self):
        for old in ({},{'total':300,'idle':100,'boot':'a'},None):self.assertIsNone(collect.cpu_percent({'total':200,'idle':80,'boot':'a'},old))
    def test_real_proc_fixture_memory_cpu_uptime_and_thermal(self):
        proc=self.root/'proc';(proc/'sys/kernel/random').mkdir(parents=True);(proc/'sys/kernel/random/boot_id').write_text('boot');(proc/'stat').write_text('cpu  10 2 5 100 3 0 0 0 0 0\n');(proc/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 600 kB\n');(proc/'uptime').write_text('12345 900\n');(proc/'cpuinfo').write_text('model name : Example CPU\n');sys=self.root/'sys';sensor=sys/'class/hwmon/hwmon0';sensor.mkdir(parents=True);(sensor/'name').write_text('coretemp');(sensor/'temp1_input').write_text('43000');(sensor/'temp2_input').write_text('45000');m,current=collect.metrics({},proc,sys);self.assertEqual(m['memory_percent'],40);self.assertEqual(m['memory_used'],400*1024);self.assertEqual(m['temperature'],45);self.assertEqual(m['uptime_seconds'],12345);self.assertEqual(current['idle'],103)
    def test_missing_metrics_stay_unknown(self):
        values,_=collect.metrics({},self.root/'absent',self.root/'absent');self.assertTrue(all(x is None for x in values.values()))
    def test_number_rejects_bool_nan_and_infinity(self):
        for value in (True,float('nan'),float('inf'),-1,'8'):self.assertIsNone(collect.number(value))
    def test_smart_health_failure_is_critical(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':False}},8)['status'],'fail')
    def test_nvme_critical_warning_overrides_pass(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':True},'nvme_smart_health_information_log':{'critical_warning':1}},0)['status'],'fail')
    def test_nvme_wear_warning(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':True},'nvme_smart_health_information_log':{'percentage_used':95}},0)['status'],'warning')
    def test_ata_pending_sectors_override_pass(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':True},'ata_smart_attributes':{'table':[{'id':197,'raw':{'value':1}}]}},0)['status'],'fail')
    def test_reallocated_sectors_warn(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':True},'ata_smart_attributes':{'table':[{'id':5,'raw':{'value':2}}]}},0)['status'],'warning')
    def test_smart_sleep_or_error_cannot_be_green(self):
        for data in ({},{'smart_status':None},{'smart_status':{'passed':'true'}}):self.assertEqual(collect.smart_summary(data,2)['status'],'unknown')
    def test_smart_error_bits_warn(self):self.assertEqual(collect.smart_summary({'smart_status':{'passed':True}},64)['status'],'warning')
    def test_smart_cache_avoids_repeat_probe_and_drops_private_fields(self):
        cache={'uuid|/dev/sda':{'status':'pass','checked_at':900,'reason':'SMART health passed','SECRET':'PRIVATE','temperature':40}}
        with patch.object(collect,'run',side_effect=AssertionError('probe')):result=collect.smart('/dev/sda','uuid',cache,1000)
        self.assertEqual(result['status'],'pass');self.assertNotIn('PRIVATE',json.dumps(result))
    def test_smart_identity_change_forces_new_readonly_probe(self):
        cache={'old|/dev/sda':{'status':'pass','checked_at':999}}
        with patch.object(collect,'run',return_value=(0,json.dumps({'smart_status':{'passed':True}}))) as call:collect.smart('/dev/sda','new',cache,1000)
        self.assertEqual(call.call_args.args[0],['smartctl','--json','--health','--attributes','--nocheck=standby','/dev/sda'])
    def test_invalid_device_never_becomes_command_argument(self):
        with patch.object(collect,'run',side_effect=AssertionError('command')):self.assertIsNone(collect.physical_device('/dev/sda;touch /tmp/secret'))
    def test_partition_maps_to_parent_disk(self):
        with patch.object(collect,'run',return_value=(0,'nvme0n1\n')):self.assertEqual(collect.physical_device('/dev/nvme0n1p2'),'/dev/nvme0n1')
    def test_mount_identity_mismatch(self):
        with patch.object(collect,'run',return_value=(0,'OTHER ext4 /dev/sda1')):self.assertEqual(collect.mount_info(self.ssd,'SSD-UUID','ext4')['status'],'wrong_drive')
    def test_unconfigured_uuid_is_not_adopted(self):
        with patch.object(collect,'run',side_effect=AssertionError('command')):self.assertEqual(collect.mount_info(self.ssd,'','ext4')['status'],'not_configured')
    def test_wrong_mount_never_reads_capacity_or_smart(self):
        with patch.object(collect,'mount_info',return_value={'status':'wrong_drive'}),patch.object(collect.shutil,'disk_usage',side_effect=AssertionError('capacity')),patch.object(collect,'smart',side_effect=AssertionError('SMART')):disks,good=collect.storage(self.cfg,{},1000)
        self.assertFalse(good);self.assertTrue(all(d['total'] is None for d in disks))
    def test_application_project_and_service_labels_are_exact(self):
        rows=[{'Labels':'com.docker.compose.project=neko-jellyfin,com.docker.compose.service=jellyfin,SECRET=PRIVATE','State':'running','Status':'Up (healthy)'},{'Labels':'com.docker.compose.project=other,com.docker.compose.service=radarr','State':'running','Status':'Up'}]
        with patch.object(collect,'run',return_value=(0,'\n'.join(json.dumps(x) for x in rows))):values=collect.applications(self.cfg)
        self.assertEqual(next(x['state'] for x in values if x['id']=='jellyfin'),'running');self.assertEqual(next(x['state'] for x in values if x['id']=='radarr'),'missing');self.assertNotIn('PRIVATE',json.dumps(values))
    def test_docker_unavailable_is_unknown(self):
        with patch.object(collect,'run',return_value=(None,'')):self.assertTrue(all(a['state']=='unknown' for a in collect.applications(self.cfg)))
    def test_unhealthy_container_is_visible(self):
        row={'Labels':'com.docker.compose.project=neko-jellyfin,com.docker.compose.service=jellyfin','State':'running','Status':'Up (unhealthy)'}
        with patch.object(collect,'run',return_value=(0,json.dumps(row))):self.assertEqual(collect.applications(self.cfg)[0]['health'],'unhealthy')
    def test_duplicate_application_records_do_not_pass(self):
        row={'Labels':'com.docker.compose.project=neko-jellyfin,com.docker.compose.service=jellyfin','State':'running','Status':'Up'}
        with patch.object(collect,'run',return_value=(0,json.dumps(row)+'\n'+json.dumps(row))):self.assertEqual(collect.applications(self.cfg)[0]['state'],'ambiguous')
    def test_backup_private_details_never_reach_dashboard(self):
        path=self.ssd/'reports/jobs/archive.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'status':'fail','last_success':900,'started_at':990,'error':'PRIVATE-KEY','password':'PRIVATE-PASSWORD'}))
        with patch.object(collect,'timer_status',return_value={'installed':True,'active':True,'next_run':None}):rows=collect.backups(self.cfg,1000,True)
        row=next(r for r in rows if r['id']=='archive');self.assertEqual(row['status'],'fail');self.assertEqual(row['last_success'],900);self.assertNotIn('PRIVATE',json.dumps(rows))
    def test_interrupted_running_backup_becomes_overdue(self):
        path=self.ssd/'reports/jobs/archive.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'status':'running','started_at':1}))
        with patch.object(collect,'timer_status',return_value={}):rows=collect.backups(self.cfg,50000,True)
        self.assertEqual(next(r for r in rows if r['id']=='archive')['status'],'stale')
    def test_media_report_whitelists_states_and_omits_secrets(self):
        from datetime import datetime,timezone
        path=self.ssd/'reports/media/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'generated_at':datetime.fromtimestamp(900,timezone.utc).isoformat(),'readiness':{'seerr:current_libraries':{'result':'pass','apiKey':'PRIVATE'}},'password':'PRIVATE'}));value=collect.media_report(self.cfg,1000,True);self.assertEqual(value['counts'],{'pass':1});self.assertEqual(value['status'],'current');self.assertNotIn('PRIVATE',json.dumps(value))
    def test_stale_media_report_is_labelled(self):
        from datetime import datetime,timezone
        path=self.ssd/'reports/media/latest.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'generated_at':datetime.fromtimestamp(1,timezone.utc).isoformat()}));self.assertEqual(collect.media_report(self.cfg,10000,True)['status'],'stale')
    def test_no_reads_of_private_reports_when_ssd_missing(self):
        with patch.object(collect,'read_json',side_effect=AssertionError('read')),patch.object(collect,'timer_status',return_value={}):self.assertTrue(all(x['status']=='unknown' for x in collect.backups(self.cfg,1000,False)));self.assertEqual(collect.media_report(self.cfg,1000,False)['status'],'unknown')
    def test_restore_report_remains_pending_and_sanitized(self):
        path=self.ssd/'recovery-tests/test/restore-report.json';path.parent.mkdir(parents=True);path.write_text(json.dumps({'status':'needs_setup','database_tables':64,'http_ready':True,'sql':'PRIVATE'}));value=collect.recovery_status(self.cfg,True);self.assertEqual(value['status'],'needs_setup');self.assertEqual(value['latest_rehearsal']['database_tables'],64);self.assertNotIn('PRIVATE',json.dumps(value))
    def test_collector_bounds_history_and_never_writes_wrong_ssd(self):
        internal=self.ssd/'dashboard-state';public=self.root/'run/status.json';base={'id':'data_ssd','name':'SSD','status':'wrong_drive','used_percent':None,'smart':{'status':'unknown'}}
        with patch.object(collect,'mount_info',return_value={'status':'wrong_drive'}),patch.object(collect,'storage',return_value=([base],False)),patch.object(collect,'metrics',return_value=({'cpu_percent':None,'memory_percent':None,'temperature':None},{})),patch.object(collect,'applications',return_value=[]),patch.object(collect,'backups',return_value=[]),patch.object(collect,'media_report',return_value={'status':'unknown','counts':{}}),patch.object(collect,'recovery_status',return_value={}):result=collect.collect(self.cfg,public,internal,1000)
        self.assertFalse(internal.exists());self.assertEqual(public.stat().st_mode&0o777,0o644);self.assertEqual(result['alerts'][0]['level'],'critical')
    def test_collector_reuses_smart_cache_and_sanitizes_history(self):
        internal=self.ssd/'dashboard-state';internal.mkdir();(internal/'collector.json').write_text(json.dumps({'history':[{'at':800,'cpu':5,'memory':20,'temperature':42,'SECRET':'PRIVATE'}]*200,'smart':{'disk':{'checked_at':900,'status':'pass'}},'cpu':{}}));public=self.root/'run/status.json';seen=[]
        def disks(config,cache,now):seen.append(cache);return ([{'id':'data_ssd','name':'SSD','status':'mounted','used_percent':20,'smart':{'status':'pass'}}],True)
        with patch.object(collect,'mount_info',return_value={'status':'mounted'}),patch.object(collect,'storage',side_effect=disks),patch.object(collect,'metrics',return_value=({'cpu_percent':5,'memory_percent':20,'temperature':42},{})),patch.object(collect,'applications',return_value=[]),patch.object(collect,'backups',return_value=[]),patch.object(collect,'media_report',return_value={'status':'unknown','counts':{}}),patch.object(collect,'recovery_status',return_value={}):result=collect.collect(self.cfg,public,internal,1000)
        self.assertIn('disk',seen[0]);self.assertEqual(len(result['history']),120);self.assertNotIn('PRIVATE',public.read_text());self.assertEqual((internal/'collector.json').stat().st_mode&0o777,0o600)

class JobTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.mount=patch.object(job_status.os.path,'ismount',return_value=True);self.mount.start()
    def tearDown(self):self.mount.stop();self.tmp.cleanup()
    def test_success_records_private_last_success(self):
        self.assertEqual(job_status.execute(self.root,'archive',lambda:42,clock=lambda:1000),42);path=self.root/'reports/jobs/archive.json';value=json.loads(path.read_text());self.assertEqual(value['status'],'pass');self.assertEqual(value['last_success'],1000);self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_failure_preserves_previous_success_without_error_details(self):
        job_status.execute(self.root,'database',lambda:None,clock=lambda:900)
        def fail():raise RuntimeError('PRIVATE SECRET')
        with self.assertRaises(RuntimeError):job_status.execute(self.root,'database',fail,clock=lambda:1000)
        value=json.loads((self.root/'reports/jobs/database.json').read_text());self.assertEqual(value['last_success'],900);self.assertEqual(value['status'],'fail');self.assertNotIn('PRIVATE',json.dumps(value))
    def test_running_record_exists_before_operation(self):
        def operation():self.assertEqual(json.loads((self.root/'reports/jobs/external.json').read_text())['status'],'running')
        job_status.execute(self.root,'external',operation)
    def test_missing_mount_prevents_status_and_backup(self):
        with patch.object(job_status.os.path,'ismount',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'mounted'):job_status.execute(self.root,'archive',lambda:self.fail('operation'))
        self.assertFalse((self.root/'reports').exists())
    def test_invalid_job_name_cannot_traverse(self):
        with self.assertRaises(RuntimeError):job_status.execute(self.root,'../SECRET',lambda:None)
    def test_source_status_symlink_refused(self):
        (self.root/'reports').symlink_to(self.root/'outside')
        with self.assertRaisesRegex(RuntimeError,'symlink'):job_status.execute(self.root,'archive',lambda:None)
    def test_legacy_no_data_argument_runs_without_tracking(self):self.assertEqual(job_status.execute(None,'external',lambda:3),3)

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.status=self.root/'status.json';self.status.write_text(json.dumps({'format':'neko-dashboard-v1','metrics':{'cpu_percent':5}}));self.http=web.server(ROOT/'dashboard',self.status,0);self.port=self.http.server_port;self.http.RequestHandlerClass=web.handler(ROOT/'dashboard',self.status,self.port);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.port}'
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,path,headers=None,method='GET'):
        req=urllib.request.Request(self.url+path,headers=headers or {},method=method)
        try:return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:return e
    def test_loopback_binding_and_exact_api(self):
        self.assertEqual(self.http.server_address[0],'127.0.0.1')
        with self.request('/api/status') as r:self.assertEqual(r.status,200);self.assertEqual(json.load(r)['metrics']['cpu_percent'],5)
    def test_security_headers_no_cors_or_cache(self):
        with self.request('/') as r:self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy']);self.assertEqual(r.headers['Cache-Control'],'no-store');self.assertIsNone(r.headers.get('Access-Control-Allow-Origin'));self.assertEqual(r.headers['X-Content-Type-Options'],'nosniff')
    def test_host_rebinding_rejected(self):
        with self.request('/api/status',{'Host':'evil.example'}) as r:self.assertEqual(r.code,403)
    def test_foreign_origin_rejected(self):
        with self.request('/api/status',{'Origin':'https://evil.example'}) as r:self.assertEqual(r.code,403)
    def test_cross_site_fetch_rejected(self):
        with self.request('/api/status',{'Sec-Fetch-Site':'cross-site'}) as r:self.assertEqual(r.code,403)
    def test_local_origin_accepted(self):
        with self.request('/api/status',{'Origin':self.url}) as r:self.assertEqual(r.code,200)
    def test_post_put_delete_cannot_manage_services(self):
        for method in ('POST','PUT','DELETE','PATCH'):
            with self.request('/api/status',method=method) as r:self.assertEqual(r.code,405)
    def test_private_files_and_traversal_never_served(self):
        for path in ('/.env','/config.json','/../neko_common.py','/%2e%2e/config.json','/preview.html','/dashboard_collect.py'):
            with self.request(path) as r:self.assertEqual(r.code,404,path)
    def test_missing_snapshot_is_503(self):
        self.status.unlink()
        with self.request('/api/status') as r:self.assertEqual(r.code,503)
    def test_invalid_snapshot_never_served(self):
        self.status.write_text('{"password":"PRIVATE"}')
        with self.request('/api/status') as r:self.assertEqual(r.code,503);self.assertNotIn(b'PRIVATE',r.read())
    def test_symlink_snapshot_refused(self):
        target=self.root/'secret';self.status.rename(target);self.status.symlink_to(target)
        with self.request('/api/status') as r:self.assertEqual(r.code,503)
    def test_assets_have_correct_mime(self):
        for path,mime in (('/style.css','text/css'),('/app.js','text/javascript'),('/logo.svg','image/svg+xml')):
            with self.request(path) as r:self.assertEqual(r.code,200);self.assertTrue(r.headers['Content-Type'].startswith(mime))

class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg=copy.deepcopy(CONFIG);self.cfg['photos']['backup_schedule']='legacy';self.cfg['photos']['archive_interval_minutes']=360;self.cfg['monitoring']['enabled']=True;self.cfg.update(data_mount=str(self.root/'ssd'),media_mount=str(self.root/'hdd'));self.cfg['storage']={'data_ssd':{'uuid':'SSD-UUID','fstype':'ext4'},'media_hdd':{'uuid':'HDD-UUID','fstype':'ext4'}};Path(self.cfg['data_mount']).mkdir();self.calls=[];self.ctx=SimpleNamespace(config=self.cfg,apply=True,log=lambda _:None)
        from contextlib import ExitStack
        self.stack=ExitStack();original=Path
        def mapped(value,*args):
            p=original(value,*args)
            for base in ('/etc/systemd/system','/etc/nekoserver-dashboard','/usr/local/lib/nekoserver-dashboard'):
                if p==original(base) or original(base) in p.parents:return self.root/'os'/base.lstrip('/')/p.relative_to(base)
            return p
        def fake(args,**kwargs):
            self.calls.append(args)
            output=''
            if args[0]=='findmnt':output=('SSD-UUID' if args[-1]==self.cfg['data_mount'] else 'HDD-UUID')+' ext4'
            return SimpleNamespace(returncode=0,stdout=output,stderr='')
        self.stack.enter_context(patch.object(dashboard,'Path',side_effect=mapped));self.stack.enter_context(patch.object(dashboard.subprocess,'run',side_effect=fake));self.stack.enter_context(patch.object(dashboard.shutil,'which',return_value='/usr/bin/fixture'))
    def tearDown(self):self.stack.close();self.tmp.cleanup()
    def test_module_installs_separate_services_and_private_config(self):
        self.cfg['password']='PRIVATE';dashboard.run(self.ctx);etc=self.root/'os/etc';cfg=etc/'nekoserver-dashboard/config.json';self.assertEqual(cfg.stat().st_mode&0o777,0o600);self.assertNotIn('PRIVATE',cfg.read_text());service=(etc/'systemd/system/nekoserver-dashboard.service').read_text();self.assertIn('DynamicUser=yes',service);self.assertIn('InaccessiblePaths=-/run/docker.sock',service);self.assertIn('--port 8787',service);collector=(etc/'systemd/system/nekoserver-dashboard-collect.service').read_text();self.assertIn('RestrictAddressFamilies=AF_UNIX',collector);self.assertNotIn('RequiresMountsFor',collector)
    def test_rerun_keeps_identical_asset(self):
        dashboard.run(self.ctx);asset=self.root/'os/usr/local/lib/nekoserver-dashboard'/('v'+CONFIG['project_name'].split('v')[-1])/'dashboard/app.js';before=asset.stat().st_mtime_ns;dashboard.run(self.ctx);self.assertEqual(asset.stat().st_mtime_ns,before)
    def test_custom_assets_preserved_before_service_changes(self):
        dashboard.run(self.ctx);asset=next((self.root/'os/usr/local/lib/nekoserver-dashboard').glob('*/dashboard/app.js'));asset.write_text('CUSTOM');self.calls=[]
        with self.assertRaisesRegex(RuntimeError,'custom files'):dashboard.run(self.ctx)
        self.assertEqual(asset.read_text(),'CUSTOM');self.assertFalse(any(a[0]=='systemctl' for a in self.calls))
    def test_foreign_unit_preserved(self):
        target=self.root/'os/etc/systemd/system/nekoserver-dashboard.service';target.parent.mkdir(parents=True);target.write_text('CUSTOM')
        with self.assertRaisesRegex(RuntimeError,'Unmanaged'):dashboard.run(self.ctx)
        self.assertEqual(target.read_text(),'CUSTOM')
    def test_managed_web_service_restarts_after_updated_definitions(self):
        dashboard.run(self.ctx);self.assertIn(['systemctl','restart','nekoserver-dashboard.service'],self.calls)
        self.assertFalse(any(a[:2]==['systemctl','restart'] and a[-1]=='docker' for a in self.calls))
    def test_dry_run_does_not_write(self):
        self.ctx.apply=False;dashboard.run(self.ctx);self.assertFalse((self.root/'os').exists());self.assertFalse(self.calls)
    def test_native_public_code_permissions_survive_private_umask(self):
        previous=os.umask(0o077)
        try:dashboard.run(self.ctx)
        finally:os.umask(previous)
        code=self.root/'os/usr/local/lib/nekoserver-dashboard'/('v'+CONFIG['project_name'].split('v')[-1])
        for path in (code.parent,code,code/'dashboard',code/'nekoserver-runtime'):
            self.assertEqual(path.stat().st_mode&0o005,0o005)
        self.assertEqual((self.root/'os/etc/nekoserver-dashboard/config.json').stat().st_mode&0o777,0o600)
    def test_invalid_port_and_intervals_refused(self):
        for key,value in [('port',8096),('port',True),('interval_seconds',5),('smart_interval_seconds',30)]:
            cfg=copy.deepcopy(self.cfg);cfg['dashboard'][key]=value
            with self.assertRaises(RuntimeError):dashboard.options(cfg)
    def test_full_profile_orders_dashboard_before_tv(self):
        plan=installer.ordered(installer.load_modules(ROOT/'modules'),CONFIG['profiles']['full']);self.assertLess(plan.index('dashboard'),plan.index('tv'));self.assertNotIn('docker',dashboard.DEPENDENCIES)
    def test_recovery_bundle_includes_dashboard_assets_and_job_imports(self):
        with patch.object(recovery.os.path,'ismount',return_value=True):saved=recovery.preserve(Path(self.cfg['data_mount']),ROOT,self.cfg)
        for name in ('dashboard_collect.py','dashboard_server.py','job_status.py','dashboard/index.html','dashboard/style.css','dashboard/app.js','dashboard/logo.svg'):self.assertTrue((saved/name).exists(),name)

if __name__=='__main__':unittest.main()
