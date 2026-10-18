import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('neko_onboard',Path(__file__).resolve().parents[1]/'onboard.py')
obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj)
class OnboardTests(unittest.TestCase):
    def test_expected_services(self):
        self.assertEqual(len(obj.APPS),8)
        self.assertEqual(set(obj.PORTS),set(obj.APPS))
    def test_paths_not_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(obj.mount_ok(tmp))
    def test_http_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); media=root/'media';media.mkdir()
            (root/'appdata'/'jellyfin').mkdir(parents=True)
            (media/'library'/'movies').mkdir(parents=True)
            rows=obj.report(root,media,False)
            self.assertEqual(next(x['status'] for x in rows if x['check']=='appdata:jellyfin'),'pass')
            self.assertEqual(next(x['status'] for x in rows if x['check']=='appdata:sonarr'),'needs_setup')
            self.assertFalse(any(x['check'].startswith('http:') for x in rows))
    def test_report_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst=Path(tmp)/'reports'/'a.json'
            obj.safe_report(dst,[{'check':'x','status':'pass'}])
            self.assertEqual(json.loads(dst.read_text())['checks'][0]['status'],'pass')
    def test_refuse_symlink_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'target').write_text('SECRET')
            (base/'report.json').symlink_to(base/'target')
            with self.assertRaises(RuntimeError):obj.safe_report(base/'report.json',[])
            self.assertEqual((base/'target').read_text(),'SECRET')
    def test_no_config_without_explicit_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);media=root/'media';media.mkdir()
            with patch.object(obj,'mount_ok',return_value=True):
                with patch.object(obj.subprocess,'call',side_effect=AssertionError('unexpected API setup')):
                    self.assertEqual(obj.main(['--data-root',str(root),'--media-root',str(media),'--skip-http']),1)
if __name__=='__main__':unittest.main()
