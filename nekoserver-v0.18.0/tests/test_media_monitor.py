import importlib.util, json, pathlib, unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mh',ROOT/'media-health.py'); mh=importlib.util.module_from_spec(spec);spec.loader.exec_module(mh)
class AuditTests(unittest.TestCase):
 def setUp(self):
  self.config={'data_mount':'/srv/nekoserver','media_mount':'/mnt/nekoserver-media','media':{'services':['sonarr','seerr']}}
 def test_down_container_is_failure(self):
  with patch.object(mh,'inspect',return_value={'state':'missing','health':'unknown','network':False,'mounts':[]}):
   r=mh.audit(self.config)
  self.assertEqual(r['services']['sonarr']['result'],'fail')
 def test_running_and_no_key_requires_setup(self):
  info={'state':'running','health':'not_defined','network':True,'mounts':['/srv/nekoserver/appdata/sonarr','/mnt/nekoserver-media/library']}
  with patch.object(mh,'inspect',return_value=info),patch.object(mh,'arr_key',return_value=None):
   r=mh.audit(self.config,probe=lambda *x:200)
  self.assertEqual(r['services']['sonarr']['result'],'pass')
  self.assertEqual(r['integrations']['sonarr']['result'],'needs_setup')
 def test_sensitive_keys_not_in_report(self):
  info={'state':'running','health':'not_defined','network':True,'mounts':['/srv/nekoserver/appdata/sonarr','/mnt/nekoserver-media/library']}
  key='THISISVERYSECRETKEY123456'
  with patch.object(mh,'inspect',return_value=info),patch.object(mh,'arr_key',return_value=key),patch.object(mh,'fetch_arr',return_value=[{'password':'secret'}]):
   r=mh.audit(self.config,probe=lambda *x:200)
  self.assertNotIn(key,json.dumps(r));self.assertNotIn('secret',json.dumps(r))
 def test_profile_and_module_registered(self):
  c=json.loads((ROOT/'config.json').read_text())
  self.assertIn('media_monitor',c['profiles']['full']);self.assertTrue((ROOT/'modules'/'media_monitor.py').exists())
if __name__=='__main__':unittest.main()
