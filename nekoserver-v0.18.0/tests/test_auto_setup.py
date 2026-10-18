import json,unittest,sys
from pathlib import Path
from unittest.mock import Mock
from importlib.util import spec_from_file_location,module_from_spec
ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=spec_from_file_location(name,ROOT/'modules'/f'{name}.py');mod=module_from_spec(spec);spec.loader.exec_module(mod);return mod
class AutoSetupTests(unittest.TestCase):
    def test_selection_requires_confirmed_uuid(self):
        s=load('storage');config={'storage':{'data_ssd':{},'media_hdd':{}},'data_mount':'/srv/nekoserver','media_mount':'/mnt/nekoserver-media'}
        inventory={'blockdevices':[{'name':'nvme0n1','type':'disk','children':[{'name':'nvme0n1p1','type':'part','uuid':'0000-ssd1','fstype':'ext4','size':'1T'}]},{'name':'sdb','type':'disk','children':[{'name':'sdb1','type':'part','uuid':'0000-hdd1','fstype':'ext4','size':'12T'}]}]}
        with self.assertRaises(RuntimeError): s.resolve_storage_identity(config,inventory,apply=True,interactive=False)
        answers=iter(['1','0000-ssd1','1','0000-hdd1'])
        r=s.resolve_storage_identity(config,inventory,apply=True,interactive=True,input_fn=lambda _:next(answers),out_fn=lambda _:None)
        self.assertEqual(r['data_ssd']['uuid'],'0000-ssd1')
        self.assertEqual(r['media_hdd']['uuid'],'0000-hdd1')
    def test_refuse_bad_confirmation(self):
        s=load('storage');cfg={'storage':{},'data_mount':'/srv/nekoserver','media_mount':'/mnt/nekoserver-media'}
        inventory={'blockdevices':[{'name':'sda1','type':'part','uuid':'1111-2222','fstype':'ext4'}]}
        with self.assertRaisesRegex(RuntimeError,'confirmation failed'):
            s.resolve_storage_identity(cfg,inventory,apply=True,interactive=True,input_fn=lambda _:next(iter(['1','wrong'])),out_fn=lambda _:None)
    def test_immich_auto_pins_release(self):
        p=load('photos');response=Mock();response.read.return_value=b'{"tag_name":"v2.2.1"}';ctx=Mock();ctx.__enter__=Mock(return_value=response);ctx.__exit__=Mock(return_value=None)
        self.assertEqual(p.resolve_immich_version('auto',opener=lambda *a,**k:ctx),'v2.2.1')
    def test_docker_default_enabled(self):
        c=json.loads((ROOT/'config.json').read_text());self.assertTrue(c['docker']['install_engine']);self.assertEqual(c['photos']['immich_version'],'auto')
if __name__=='__main__':unittest.main()
