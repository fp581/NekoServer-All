import importlib.util
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('neko_media', ROOT / 'modules' / 'media.py')
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)

class MediaTests(unittest.TestCase):
    def test_all_service_templates_exist(self):
        for name in media.SERVICES:
            path = ROOT / 'compose' / f'{name}.yaml'
            self.assertTrue(path.exists(), name)
            rendered = media.render(path.read_text(), '/srv/nekoserver', '/mnt/nekoserver-media', 1000,1000,'Asia/Jerusalem','127.0.0.1')
            self.assertNotIn('${NEKO_', rendered)
            self.assertIn('127.0.0.1:', rendered)
            self.assertIn('nekoserver-media', rendered)
    def test_mount_rejects_missing(self):
        with self.assertRaises(RuntimeError):
            media.check_mount('/definitely/not/a/mount')
    def test_non_apply_read_only(self):
        class C:
            apply=False
            config={}
            def log(self,x): pass
        media.run(C())
    def test_config_defaults_disabled(self):
        import json
        data=json.loads((ROOT/'config.json').read_text())
        self.assertFalse(data['modules']['media']['enabled'])
        self.assertEqual(data['media']['bind_ip'],'127.0.0.1')
    def test_read_only_music_library(self):
        txt=(ROOT/'compose'/'navidrome.yaml').read_text()
        self.assertIn('/music:ro',txt)
        self.assertIn('user:',txt)

if __name__=='__main__': unittest.main()
