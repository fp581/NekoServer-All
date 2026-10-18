"""Guard OS writes and execute the native SATA command against mount/device fixtures."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dashboard_collect as health
from modules import server_os

ROOT=Path(__file__).resolve().parents[1]
BIN=ROOT/'runtime/bin/nekoserver-runtime'


class HddTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.data=self.root/'ssd';self.media=self.root/'hdd'
        self.data.mkdir();self.media.mkdir();self.config=self.root/'config.json'
        self.config.write_text(json.dumps({'data_mount':str(self.data),'media_mount':str(self.media),'storage':{'data_ssd':{'uuid':'SSD','fstype':'ext4'},'media_hdd':{'uuid':'HDD','fstype':'ext4'}}}))
        commands=self.root/'commands';commands.mkdir();self.log=self.root/'hdparm.log'
        script='''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name;args=sys.argv[1:]
if name=='findmnt':
 field=args[args.index('-o')+1]
 if field=='UUID,FSTYPE':print(('WRONG' if os.environ.get('WRONG') else 'SSD')+' ext4' if args[-1]==os.environ['SSD'] else 'HDD ext4')
 else:
  counter=Path(os.environ['COUNTER']);count=int(counter.read_text()) if counter.exists() else 0;counter.write_text(str(count+1))
  print('/dev/sdy1' if os.environ.get('CHANGED') and count>0 else os.environ.get('SOURCE','/dev/sdz1'))
elif name=='lsblk':
 field=args[args.index('-o')+1]
 if field=='TYPE':print(os.environ.get('KIND','part'))
 elif field=='PKNAME':print('sdz')
 else:print('disk '+os.environ.get('ROTA','1')+' '+os.environ.get('TRAN','sata'))
elif name=='hdparm':
 Path(os.environ['LOG']).write_text(json.dumps(args))
 sys.exit(1 if os.environ.get('FAIL') else 0)
else:sys.exit(1)
'''
        for name in ('findmnt','lsblk','hdparm'):
            path=commands/name;path.write_text(script);path.chmod(0o755)
        self.env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','SSD':str(self.data),'LOG':str(self.log),'COUNTER':str(self.root/'counter')}

    def run_native(self):
        return subprocess.run([str(BIN),'hdd-policy','--config',str(self.config)],env=self.env,capture_output=True,text=True,timeout=10)

    def test_targets_only_verified_parent_media_disk(self):
        result=self.run_native();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(self.log.read_text()),['-S','0','/dev/sdz'])

    def test_wrong_uuid_never_changes_drive_settings(self):
        self.env['WRONG']='1';self.assertNotEqual(self.run_native().returncode,0);self.assertFalse(self.log.exists())

    def test_usb_ssd_and_mapped_devices_are_refused(self):
        for key,value in [('TRAN','usb'),('ROTA','0'),('KIND','crypt')]:
            self.env[key]=value;self.assertNotEqual(self.run_native().returncode,0);self.assertFalse(self.log.exists());del self.env[key]

    def test_changed_mount_source_is_refused(self):
        self.env['CHANGED']='1';self.assertNotEqual(self.run_native().returncode,0);self.assertFalse(self.log.exists())

    def test_whole_disk_source_is_supported(self):
        self.env.update(SOURCE='/dev/sdz',KIND='disk');self.assertEqual(self.run_native().returncode,0);self.assertEqual(json.loads(self.log.read_text()),['-S','0','/dev/sdz'])

    def test_hdparm_failure_is_not_success(self):
        self.env['FAIL']='1';self.assertNotEqual(self.run_native().returncode,0)


class OsSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config=json.loads((ROOT/'config.json').read_text());self.config['server_os']['hdd_policy']='unchanged'
        self.config.update(data_mount=str(self.root/'ssd'),media_mount=str(self.root/'hdd'))
        self.logs=[]

    def apply(self):
        real_path=Path;real_safe=server_os.safe_path
        def mapped(value,*args):
            path=real_path(value,*args)
            return self.root/str(path).lstrip('/') if str(path).startswith(('/etc/','/usr/local/')) else path
        def safe(value):return real_safe(mapped(value))
        with patch.object(server_os,'Path',side_effect=mapped),patch.object(server_os,'safe_path',side_effect=safe),patch.object(server_os.shutil,'which',return_value='/usr/bin/fixture'),patch.object(server_os,'verify_storage'),patch.object(server_os.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            result=server_os.run(SimpleNamespace(config=self.config,apply=True,log=self.logs.append))
        return result,run.call_args_list

    def test_masks_all_sleep_paths_and_installs_bounded_journal(self):
        result,calls=self.apply();self.assertIsNone(result)
        commands=[call.args[0] for call in calls]
        self.assertIn(['systemctl','mask',*server_os.TARGETS],commands)
        self.assertNotIn(['systemctl','restart','systemd-logind.service'],commands)
        text=(self.root/'etc/systemd/journald.conf.d/90-nekoserver.conf').read_text()
        self.assertIn('SystemMaxUse=512M',text);self.assertIn('RuntimeMaxUse=64M',text)
        self.assertIn('MaxRetentionSec=14day',text)
        self.assertFalse(any('drop_caches' in str(command) for command in commands))

    def test_custom_os_dropin_is_preserved(self):
        path=self.root/'etc/systemd/sleep.conf.d/90-nekoserver.conf';path.parent.mkdir(parents=True);path.write_text('administrator settings')
        with self.assertRaises(RuntimeError):self.apply()
        self.assertEqual(path.read_text(),'administrator settings')

    def test_custom_sleep_target_is_preserved_before_writes(self):
        path=self.root/'etc/systemd/system/sleep.target';path.parent.mkdir(parents=True);path.write_text('custom target')
        with self.assertRaises(RuntimeError):self.apply()
        self.assertFalse((self.root/'etc/systemd/sleep.conf.d').exists())

    def test_existing_dev_null_masks_are_accepted(self):
        folder=self.root/'etc/systemd/system';folder.mkdir(parents=True)
        for name in server_os.TARGETS:(folder/name).symlink_to('/dev/null')
        self.apply()

    def test_unchanged_disables_previous_managed_boot_policy(self):
        path=self.root/'etc/systemd/system/nekoserver-hdd-policy.service';path.parent.mkdir(parents=True);path.write_text('[Unit]\nDescription=NekoServer verified media HDD standby policy\n')
        _,calls=self.apply();self.assertIn(['systemctl','disable','--now','nekoserver-hdd-policy.service'],[call.args[0] for call in calls])

    def test_hdd_policy_is_deployed_and_reapplied(self):
        self.config['server_os']['hdd_policy']='keep_spinning';result,calls=self.apply();self.assertIsNone(result)
        command=[call.args[0] for call in calls]
        self.assertIn(['systemctl','enable','nekoserver-hdd-policy.service'],command)
        self.assertIn(['systemctl','restart','nekoserver-hdd-policy.service'],command)
        guard=self.root/'etc/nekoserver-os/storage.json';self.assertEqual(guard.stat().st_mode&0o777,0o600)
        unit=(self.root/'etc/systemd/system/nekoserver-hdd-policy.service').read_text()
        self.assertIn('hdd-policy --config',unit)
        self.assertNotIn('/dev/sd',unit)

    def test_options_reject_invalid_values(self):
        for value in (True,0,100000,'64'):
            with self.assertRaises(RuntimeError):server_os.settings({'server_os':{'journal_ram_mb':value}})
        with self.assertRaises(RuntimeError):server_os.settings({'server_os':{'hdd_policy':'all_drives'}})

    def test_cache_is_not_reported_as_used_application_memory(self):
        proc=self.root/'proc';proc.mkdir();(proc/'meminfo').write_text('MemTotal: 100000 kB\nMemAvailable: 80000 kB\nCached: 70000 kB\n')
        metrics,_=health.metrics({},proc,self.root/'sys')
        self.assertEqual(metrics['memory_percent'],20.0)

    def test_genuine_low_available_ram_generates_alert(self):
        base={'storage':[],'applications':[],'backups':[],'media':{'status':'unknown','counts':{}},'metrics':{'memory_percent':95,'temperature':40}}
        alert=next(a for a in health.alerts(base,1000) if a['id']=='memory-pressure');self.assertEqual(alert['level'],'warning')
        base['metrics']['memory_percent']=98
        self.assertEqual(next(a for a in health.alerts(base,1000) if a['id']=='memory-pressure')['level'],'critical')
