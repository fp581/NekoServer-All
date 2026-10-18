import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('media_connect', ROOT/'media-connect.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class MediaConnectTests(unittest.TestCase):
    def test_api_versions(self):
        self.assertEqual(m.API_VERSIONS['prowlarr'], 'v1')
        self.assertEqual(m.API_VERSIONS['sonarr'], 'v3')
    def test_no_match_field(self):
        fields = [{'name':'host', 'value':'old'}]
        self.assertFalse(m.field_set(fields, 'password', 'private'))
        self.assertTrue(m.field_set(fields, 'HOST', 'qbittorrent'))
        self.assertEqual(fields[0]['value'], 'qbittorrent')
    def test_key_from_xml(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)/'appdata'/'prowlarr'
            p.mkdir(parents=True)
            (p/'config.xml').write_text('<Config><ApiKey>localkey</ApiKey></Config>')
            self.assertEqual(m.app_key('prowlarr', td), 'localkey')
    def test_readonly_indexer_preview(self):
        catalog=[{'name':'Open Example','implementation':'Example','fields':[]}]
        calls=[]
        def fake_request(app,key,endpoint,method='GET',payload=None,timeout=12):
            calls.append((endpoint,method))
            return catalog if endpoint.endswith('/schema') else []
        with patch.object(m,'app_key',return_value='key'), patch.object(m,'request',side_effect=fake_request), patch('builtins.input',side_effect=['Open','1','']):
            m.configure_indexers('/fake',False)
        self.assertTrue(all(method == 'GET' for _,method in calls))
    def test_existing_app_skips_duplicate(self):
        calls=[]
        def fake_request(app,key,endpoint,method='GET',payload=None,timeout=12):
            calls.append((app,endpoint,method))
            if endpoint == '/applications/schema': return []
            if endpoint == '/applications': return [{'name':f'neko-{a}','syncLevel':'addOnly','fields':[{'name':'prowlarrUrl','value':'http://prowlarr:9696'},{'name':'baseUrl','value':f'http://{a}:{m.PORTS[a]}'},{'name':'apiKey','value':'key'}]} for a in ('sonarr','radarr','lidarr')]
            return {}
        with patch.object(m,'app_key',return_value='key'), patch.object(m,'request',side_effect=fake_request):
            m.configure_applications('/fake',True)
        self.assertFalse(any(method == 'POST' for _,_,method in calls))

if __name__=='__main__': unittest.main()
