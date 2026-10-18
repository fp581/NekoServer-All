import importlib.util, tempfile, unittest, json, os, time
from pathlib import Path
from unittest.mock import patch
from neko_common import VERSION
ROOT=Path(__file__).resolve().parents[1]
def load(name):
 p=ROOT/name;sp=importlib.util.spec_from_file_location(name.replace('/','_').replace('-','_'),p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m
class Integration(unittest.TestCase):
 def test_matching_immich_release_required(self):
  m=load('modules/photos.py');self.assertFalse(m.version_ok('v3'));self.assertTrue(m.version_ok('v2.2.0'));self.assertIn('{version}',m.IMMICH_COMPOSE)
 def test_database_and_deploy_project_agree(self):
  self.assertIn("'-p','neko-immich'",(ROOT/'immich-db-backup.py').read_text())
  self.assertIn("'-p','neko-immich'",(ROOT/'modules/photos.py').read_text())
 def test_runtime_installed_on_data(self):
  m=load('modules/_runtime.py')
  with tempfile.TemporaryDirectory() as t:
   p=m.deploy(t,ROOT);self.assertTrue((p/'photo-weekly.py').is_file());self.assertIn('runtime/v'+VERSION,str(p));self.assertEqual(p,m.deploy(t,ROOT))
 def test_interrupted_external_copy_recovery_and_no_deletions(self):
  m=load('photo-backup.py')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'src';dst=root/'dst';src.mkdir();dst.mkdir();(src/'a.jpg').write_bytes(b'original');(dst/'a.jpg.neko-part').write_bytes(b'incomplete')
   self.assertEqual(m.backup(src,dst,True),1);self.assertEqual((dst/'a.jpg').read_bytes(),b'original');self.assertFalse((dst/'a.jpg.neko-part').exists())
   (src/'a.jpg').unlink();m.backup(src,dst,True);self.assertEqual((dst/'a.jpg').read_bytes(),b'original')
 def test_mounted_docker_rerun_reuses_healthy_engine(self):
  m=load('modules/docker.py')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);rootpath=str(root);target=root/'docker-engine';target.mkdir();ctx=type('C',(),{'config':{'data_mount':rootpath,'docker':{}},'apply':True,'log':lambda self,x:None})()
   with patch.object(m,'install_storage_guard'),patch.object(m,'verify_classic_store'),patch.object(m,'write_atomic'),patch.object(m,'mounted_data_root',return_value=(rootpath,'/dev/test')),patch.object(m.os,'geteuid',return_value=0),patch.object(m.shutil,'which',return_value='/bin/true'),patch.object(m,'prepare_daemon_config',return_value={'data-root':str(target)}),patch.object(m.subprocess,'run',return_value=type('P',(),{'returncode':0})()),patch.object(m,'call',return_value=str(target)),patch.object(m.Path,'exists',autospec=True) as exists,patch.object(m.Path,'read_text',autospec=True) as read:
    exists.return_value=True;read.side_effect=lambda path,*a,**k: json.dumps({'data-root':str(target)}) if str(path).endswith('daemon.json') else '[Unit]\nRequiresMountsFor='+rootpath
    m.run(ctx)
 def test_full_profile_declared(self):
  cfg=json.loads((ROOT/'config.json').read_text());self.assertTrue({'system','storage','docker','media','photos','photo_preservation','photo_backup'}.issubset(set(cfg['profiles']['full'])))
if __name__=='__main__':unittest.main()
