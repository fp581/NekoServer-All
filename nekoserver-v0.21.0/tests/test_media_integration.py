import importlib.util, json, pathlib, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('doctor',ROOT/'media-doctor.py')
doctor=importlib.util.module_from_spec(spec); spec.loader.exec_module(doctor)
class Tests(unittest.TestCase):
 def test_all_eight_services(self):
  cfg=json.loads((ROOT/'config.json').read_text()); self.assertEqual(set(cfg['media']['services']),set(doctor.SERVICES))
 def test_links_refer_to_real_services(self):
  for a,b,port in doctor.LINKS.values():
   self.assertIn(a,doctor.SERVICES); self.assertIn(b,doctor.SERVICES); self.assertTrue(port.isdigit())
 def test_missing_network_detected(self):
  cfg=json.loads((ROOT/'config.json').read_text()); records={'sonarr':{'status':'running','network':False,'mounts':[cfg['data_mount']+'/appdata/sonarr',cfg['media_mount']+'/library']}}
  self.assertIn('sonarr: missing shared network',doctor.analyze(cfg,records))
 def test_all_manifests_isolated_and_networked(self):
  for name in doctor.SERVICES:
   text=(ROOT/'compose'/f'{name}.yaml').read_text()
   self.assertIn('nekoserver-media',text);self.assertIn('${NEKO_DATA}/appdata/'+name,text)
 def test_admin_ports_local_only(self):
  for name in doctor.SERVICES:
   text=(ROOT/'compose'/f'{name}.yaml').read_text()
   self.assertIn('${NEKO_BIND}:',text)
if __name__=='__main__': unittest.main()
