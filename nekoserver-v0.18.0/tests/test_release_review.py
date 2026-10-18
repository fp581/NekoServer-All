"""Regression tests for step-by-step installation audit findings."""
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import installer
import config_checks
import acceptance_summary
import neko_common
import permission_checks
from modules import photo_backup,photos,tailscale,samba,dashboard
import test_gateway_v090 as gateway_fixtures
import test_dashboard_v080 as dashboard_fixtures

ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())
BIN=ROOT/'runtime/bin/nekoserver-runtime'


class PreflightTests(unittest.TestCase):
    def test_invalid_selected_settings_fail_before_module_runs_or_state_write(self):
        variants=[('heartbeat',{'heartbeat':{'interval_seconds':True}}),('server_os',{'server_os':{'journal_ram_mb':0}}),('media',{'media':{**CONFIG['media'],'uid':True}}),('photo_backup',{'photos':{**CONFIG['photos'],'backup_time':'25:00'}})]
        for name,values in variants:
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);mods=root/'modules';mods.mkdir();(mods/(name+'.py')).write_text("DEPENDENCIES=[]\ndef run(ctx):\n    raise AssertionError('module ran')\n")
                cfg=copy.deepcopy(CONFIG);cfg.update(values);p=root/'config.json';p.write_text(json.dumps(cfg))
                args=SimpleNamespace(config=str(p),state_dir=str(root/'state'),profile=None,module=name,retry_failed=False,check=False,dry_run=False,apply=True)
                with self.assertRaises(RuntimeError):installer.execute(args,root)
                self.assertFalse((root/'state').exists())
    def test_schema_rejects_boolean_strings_and_bad_profiles(self):
        for values in ({'modules':{'media':{'enabled':'false'}}},{'profiles':{'full':'media'}},{'runtime':[]},{'profiles':{'full':['media','media']}}):
            cfg=copy.deepcopy(CONFIG);cfg.update(values)
            with self.assertRaises(installer.InstallerError):installer.validate_config(cfg)
    def test_unsafe_state_path_refused_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'state';p.mkdir();(p/'state.json').symlink_to('/etc/passwd')
            args=SimpleNamespace(config=str(ROOT/'config.json'),state_dir=str(p),profile=None,module='system',retry_failed=False,check=True,dry_run=False,apply=False)
            with self.assertRaises(RuntimeError):installer.execute(args)
    def test_concurrent_apply_refused(self):
        with Path('/run/nekoserver-install.lock').open('a') as lock:
            os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(installer.InstallerError,'active'):installer.execute(SimpleNamespace(apply=True))
    def test_read_only_inspection_preserves_installation_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);mods=root/'modules';mods.mkdir();(mods/'system.py').write_text('DEPENDENCIES=[]\ndef run(ctx):\n    ctx.log("inspection only")\n')
            p=root/'config.json';p.write_text(json.dumps(CONFIG));state=root/'state';state.mkdir();old=state/'state.json';old.write_text('{"system":{"status":"failed"}}');before=old.read_bytes()
            args=SimpleNamespace(config=str(p),state_dir=str(state),profile=None,module='system',retry_failed=False,check=False,dry_run=False,apply=False)
            with redirect_stdout(io.StringIO()):self.assertEqual(installer.execute(args,root),0)
            self.assertEqual(old.read_bytes(),before);self.assertEqual(json.loads((state/'inspection-state.json').read_text())['system']['status'],'inspected')
            self.assertEqual((state/'inspection.log').stat().st_mode&0o777,0o600)
    def test_private_apply_log_survives_public_umask(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);mods=root/'modules';mods.mkdir();(mods/'system.py').write_text('DEPENDENCIES=[]\ndef run(ctx):\n    ctx.log("fixture")\n')
            p=root/'config.json';p.write_text(json.dumps(CONFIG))
            args=SimpleNamespace(config=str(p),state_dir=str(root/'state'),profile=None,module='system',retry_failed=False,check=False,dry_run=False,apply=True)
            previous=os.umask(0o022)
            try:
                with redirect_stdout(io.StringIO()):installer.execute(args,root)
            finally:os.umask(previous)
            self.assertEqual((root/'state/install.log').stat().st_mode&0o777,0o600)
    def test_running_state_is_durable_before_a_module_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);mods=root/'modules';mods.mkdir()
            (mods/'system.py').write_text('DEPENDENCIES=[]\ndef run(ctx):\n    raise KeyboardInterrupt()\n')
            cfg=root/'config.json';cfg.write_text(json.dumps(CONFIG))
            args=SimpleNamespace(config=str(cfg),state_dir=str(root/'state'),profile=None,module='system',retry_failed=False,check=False,dry_run=False,apply=True)
            with redirect_stdout(io.StringIO()),self.assertRaises(KeyboardInterrupt):installer.execute(args,root)
            self.assertEqual(json.loads((root/'state/state.json').read_text())['system']['status'],'running')
            args.module=None;args.retry_failed=True;args.apply=False;args.check=True
            output=io.StringIO()
            with redirect_stdout(output):self.assertEqual(installer.execute(args,root),0)
            self.assertIn('system:',output.getvalue())
    def test_persisted_selection_updates_both_config_copies(self):
        with tempfile.TemporaryDirectory() as temp:
            first=Path(temp)/'original.json';saved=Path(temp)/'saved.json';first.write_text('{}');saved.write_text('{}')
            ctx=installer.Context({'gateway':{'lan_ip':'192.168.1.20'}},lambda _:None,True,first);ctx.config_path=saved;ctx.persist_config()
            self.assertEqual(json.loads(first.read_text()),ctx.config);self.assertEqual(first.read_bytes(),saved.read_bytes())


class FilesAndSetupTests(unittest.TestCase):
    def test_immich_all_three_yaml_quote_forms(self):
        for port in ('2283:2283','"2283:2283"',"'2283:2283'"):
            text=photos.localize_manifest('services:\n  app:\n    ports:\n      - '+port+'\n')
            self.assertIn('127.0.0.1:2283:2283',text)
    def test_immich_unknown_or_ambiguous_mapping_refused(self):
        for text in ('ports:\n - 0.0.0.0:2283:2283\n','ports:\n - 2283:2283\n - 2283:2283\n'):
            with self.assertRaises(RuntimeError):photos.localize_manifest(text)
    def test_shared_helper_rejects_parent_traversal(self):
        with self.assertRaises(RuntimeError):neko_common.safe_path('/tmp/../etc')
    def test_bind_root_checks_do_not_require_host_parent_access(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'bind';p.mkdir();p.chmod(0o777)
            self.assertFalse(permission_checks.access(p,1000,{1000},7))
            self.assertTrue(permission_checks.access(p,1000,{1000},7,bind_root=True))
    def test_samba_missing_lan_is_setup_pending(self):
        ctx=SimpleNamespace(config=CONFIG,apply=True,log=lambda _:None)
        result=samba.run(ctx);self.assertEqual(result['status'],'needs_setup')
    def test_tailscale_login_is_not_reported_complete(self):
        with patch.object(tailscale,'detect_arch',return_value=True),patch.object(tailscale.shutil,'which',return_value='/fixture'),patch.object(tailscale,'run_cmd'),patch.object(tailscale.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"BackendState":"NeedsLogin"}')):
            result=tailscale.run(SimpleNamespace(config={},apply=True,log=lambda _:None))
        self.assertEqual(result['status'],'needs_setup')
    def test_arch_detection_handles_quoted_id(self):
        fake=SimpleNamespace(exists=lambda:True,read_text=lambda:'ID="arch"\n')
        with patch.object(tailscale,'Path',return_value=fake):self.assertTrue(tailscale.detect_arch())
    def test_other_partitions_on_the_os_disk_are_not_candidates(self):
        from modules import storage
        tree={'blockdevices':[{'name':'osdisk','type':'disk','children':[{'name':'root','type':'part','uuid':'OS-UUID','fstype':'ext4','mountpoint':'/'},{'name':'unused','type':'part','uuid':'OTHER-UUID','fstype':'ext4'}]},{'name':'data','type':'disk','uuid':'DATA-UUID','fstype':'ext4'}]}
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(tree)],['DATA-UUID'])
    def test_data_and_media_cannot_be_different_partitions_of_one_disk(self):
        from modules import storage
        cfg=copy.deepcopy(CONFIG);cfg['storage']={'data_ssd':{'uuid':'SSD-0001'},'media_hdd':{'uuid':'HDD-0001'}}
        tree={'blockdevices':[{'name':'same','type':'disk','children':[{'name':'one','type':'part','uuid':'SSD-0001','fstype':'ext4'},{'name':'two','type':'part','uuid':'HDD-0001','fstype':'ext4'}]}]}
        with self.assertRaisesRegex(RuntimeError,'physical'):storage.resolve_storage_identity(cfg,tree,apply=True)
    def test_external_missing_uuid_remains_setup_noninteractive(self):
        cfg=copy.deepcopy(CONFIG)
        with patch.object(photo_backup.sys.stdin,'isatty',return_value=False),patch.object(photo_backup.subprocess,'run') as cmd:
            row=photo_backup.run(SimpleNamespace(config=cfg,apply=True,log=lambda _:None))
        self.assertEqual(row['status'],'needs_setup');cmd.assert_not_called()
    def test_external_selection_requires_exact_confirmation_and_ignores_os_data(self):
        cfg=copy.deepcopy(CONFIG);cfg['storage']={'data_ssd':{'uuid':'SSD-UUID'},'media_hdd':{'uuid':'HDD-UUID'}}
        rows={'blockdevices':[{'name':'os','type':'part','uuid':'OS-UUID','fstype':'ext4','mountpoint':'/'},{'name':'ssd','type':'part','uuid':'SSD-UUID','fstype':'ext4'},{'name':'usb','type':'part','uuid':'USB-UUID','fstype':'exfat','mountpoint':'/run/media/user/Photos'}]}
        answers=iter(['1','USB-UUID']);photo_backup.select_external(cfg,rows,lambda _:next(answers),lambda _:None)
        self.assertEqual(cfg['photos']['external_backup_uuid'],'USB-UUID');self.assertEqual(cfg['photos']['external_backup_fstype'],'exfat')
        answers=iter(['1','WRONG'])
        with self.assertRaises(RuntimeError):photo_backup.select_external(cfg,rows,lambda _:next(answers),lambda _:None)
    def test_custom_archive_interval_does_not_generate_false_overdue_status(self):
        import dashboard_collect as health
        cfg=copy.deepcopy(CONFIG);cfg['photos']['archive_interval_minutes']=1440
        with patch.object(health,'read_json',return_value={'status':'pass','last_success':100,'started_at':100}),patch.object(health,'timer_status',return_value={}):
            row=next(v for v in health.backups(cfg,50000,True) if v['id']=='archive')
        self.assertEqual(row['status'],'pass')
    def test_fstab_dangling_backup_link_is_never_followed(self):
        from modules import storage
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'fstab';p.write_text('# original\n');p.with_name('fstab.nekoserver.bak').symlink_to(Path(temp)/'foreign')
            with self.assertRaises(RuntimeError):storage.install_fstab([('data','SSD-0001','ext4','/srv/neko')],p)
            self.assertEqual(p.read_text(),'# original\n')
    def test_scope_changes_with_backup_and_os_choices(self):
        for group,key,value in [('heartbeat','interval_seconds',900),('server_os','journal_disk_mb',1024),('host_backup','extra_paths',['/home/user/private'])]:
            cfg=copy.deepcopy(CONFIG);cfg[group][key]=value
            self.assertNotEqual(acceptance_summary.scope_digest(CONFIG),acceptance_summary.scope_digest(cfg))


class ServiceHandoffTests(unittest.TestCase):
    def test_gateway_fresh_public_dirs_survive_private_umask(self):
        f=gateway_fixtures.LanModuleTests('test_rerun_keeps_ca_state');f.setUp();self.addCleanup(f.tearDown)
        old=os.umask(0o077)
        try:f.apply()
        finally:os.umask(old)
        for p in (f.ssd/'gateway',f.root/'os/etc/nekoserver-gateway',f.root/'os/usr/local/share/nekoserver-gateway/lan',f.root/('os/usr/local/lib/nekoserver-gateway/v'+neko_common.VERSION)):
            self.assertEqual(p.stat().st_mode&0o005,0o005)
        self.assertEqual((f.root/'os/etc/nekoserver-gateway/storage.json').stat().st_mode&0o777,0o600)
    def test_gateway_existing_private_parent_is_preserved(self):
        f=gateway_fixtures.LanModuleTests('test_rerun_keeps_ca_state');f.setUp();self.addCleanup(f.tearDown)
        p=f.ssd/'gateway';p.mkdir(mode=0o700)
        with self.assertRaisesRegex(RuntimeError,'traversal'):f.apply()
        self.assertEqual(p.stat().st_mode&0o777,0o700)
    def test_gateway_restrictive_mount_root_is_reported_before_writes(self):
        f=gateway_fixtures.LanModuleTests('test_rerun_keeps_ca_state');f.setUp();self.addCleanup(f.tearDown)
        f.ssd.chmod(0o700)
        with self.assertRaisesRegex(RuntimeError,'traversal'):f.apply()
        self.assertFalse((f.ssd/'gateway').exists());self.assertEqual(f.ssd.stat().st_mode&0o777,0o700)
    def test_dashboard_custom_heartbeat_and_full_scope_are_handed_off(self):
        f=dashboard_fixtures.ModuleTests('test_module_installs_separate_services_and_private_config');f.setUp();self.addCleanup(f.tearDown)
        f.cfg['heartbeat']['interval_seconds']=1800;dashboard.run(f.ctx)
        saved=json.loads((f.root/'os/etc/nekoserver-dashboard/config.json').read_text())
        self.assertEqual(saved['heartbeat']['interval_seconds'],1800)
        self.assertEqual(saved['acceptance_scope'],acceptance_summary.scope_digest(f.cfg))
        self.assertEqual(acceptance_summary.scope_digest(saved),acceptance_summary.scope_digest(f.cfg))


class NativeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.data=self.root/'ssd';self.media=self.root/'hdd';self.data.mkdir();self.media.mkdir()
        self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps({'data_mount':str(self.data),'media_mount':str(self.media),'storage':{'data_ssd':{'uuid':'SSD','fstype':'ext4'},'media_hdd':{'uuid':'HDD','fstype':'ext4'}},'gateway':{'lan_ip':'192.168.1.20','lan_cidr':'192.168.1.0/24'}}))
        commands=self.root/'commands';commands.mkdir()
        text='''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
if Path(sys.argv[0]).name=='findmnt':print(('WRONG' if os.environ.get('WRONG') else 'SSD')+' ext4' if sys.argv[-1]==os.environ['SSD'] else 'HDD ext4')
else:print(json.dumps([{'ifname':os.environ.get('IFACE','enp3s0'),'addr_info':[{'family':'inet','scope':'global','local':os.environ.get('IP','192.168.1.20'),'prefixlen':24}]}]))
'''
        for name in ('findmnt','ip'):
            p=commands/name;p.write_text(text);p.chmod(0o755)
        self.env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','SSD':str(self.data)}
    def native(self,name):return subprocess.run([str(BIN),name,'--config',str(self.cfg)],env=self.env,capture_output=True,timeout=10)
    def test_storage_guard_wrong_uuid_refuses_startup(self):
        self.assertEqual(self.native('storage-guard').returncode,0);self.env['WRONG']='1';self.assertNotEqual(self.native('storage-guard').returncode,0)
    def test_gateway_guard_requires_the_active_physical_ip(self):
        self.assertEqual(self.native('gateway-guard').returncode,0)
        for key,value in [('IFACE','tailscale0'),('IP','192.168.1.21')]:
            self.env[key]=value;self.assertNotEqual(self.native('gateway-guard').returncode,0);self.env.pop(key)
