import importlib.util
import unittest
from pathlib import Path
p = Path(__file__).resolve().parents[1] / 'source-wizard.py'
spec = importlib.util.spec_from_file_location('sources_wizard', p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SourceTests(unittest.TestCase):
    def test_local(self):
        self.assertEqual(m.validate_source({'category':'music','type':'local','path':'/mnt/music'})['path'],'/mnt/music')
    def test_reject_relative(self):
        with self.assertRaises(ValueError):m.valid_local_path('my/music')
    def test_remote(self):
        self.assertEqual(m.validate_source({'category':'tv','type':'remote','uri':'smb://nas/tv'})['type'],'remote')
    def test_reject_embedded_password(self):
        with self.assertRaises(ValueError):m.validate_source({'category':'tv','type':'remote','uri':'smb://bob:secret@nas/tv'})
    def test_indexer(self):
        self.assertEqual(m.validate_source({'category':'movies','type':'indexer','uri':'https://tracker.example/'})['type'],'indexer')
    def test_reject_unencrypted_indexer(self):
        with self.assertRaises(ValueError):m.validate_source({'category':'movies','type':'indexer','uri':'http://tracker.example/'})
    def test_reject_invalid_category(self):
        with self.assertRaises(ValueError):m.validate_source({'category':'photos','type':'local','path':'/mnt/photos'})
