import importlib.util, pathlib, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/'modules'/f'{name}.py'); mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
class NetworkingTests(unittest.TestCase):
 def test_samba_safe_shares(self):
  m=load('samba'); cfg=m.share_config({'shares':{'Media':'library','Photos':'photos/archive'}},pathlib.Path('/mnt/media'),'tester')
  self.assertIn('interfaces = lo tailscale0',cfg)
  self.assertIn('guest ok = no',cfg)
  self.assertIn('read only = yes',cfg)
  self.assertNotIn('0.0.0.0',cfg)
 def test_reject_traversal(self):
  m=load('samba')
  for path in ('/etc','../private','foo/../../other'):
   with self.assertRaises(RuntimeError):m.validate_share('Media',path)
 def test_require_tailscale(self):
  m=load('samba');self.assertNotIn('tailscale',m.DEPENDENCIES)
 def test_tailscale_no_auth_secret(self):
  source=(ROOT/'modules/tailscale.py').read_text()
  self.assertNotIn('--authkey',source)
  self.assertIn('tailscale up',source)
 def test_new_modules_plan(self):
  import sys
  sys.path.insert(0,str(ROOT))
  from installer import load_modules,ordered
  mods=load_modules(ROOT/'modules'); self.assertNotIn('tailscale',ordered(mods,['samba']))
