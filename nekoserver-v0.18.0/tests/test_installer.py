import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from installer import InstallerError, load_modules, ordered, main

ROOT = Path(__file__).resolve().parents[1]
class InstallerTests(unittest.TestCase):
    def test_module_order(self):
        modules = load_modules(ROOT / 'modules')
        self.assertEqual(ordered(modules, ['storage']), ['system', 'storage'])
    def test_cycle_rejected(self):
        with self.assertRaises(InstallerError):
            ordered({'a': {'deps': ['b']}, 'b': {'deps': ['a']}}, ['a'])
    def test_missing_dependency_rejected(self):
        with self.assertRaises(InstallerError):
            ordered({'a': {'deps': ['nope']}}, ['a'])
    def test_check_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state'
            self.assertEqual(main(['--check', '--state-dir', str(path)]), 0)
            self.assertFalse(path.exists())
    def test_dry_run_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state'
            self.assertEqual(main(['--dry-run', '--module', 'storage', '--state-dir', str(path)]), 0)
            self.assertFalse(path.exists())
    def test_unknown_module(self):
        self.assertEqual(main(['--module', 'unknown', '--check']), 2)
if __name__ == '__main__':
    unittest.main()
