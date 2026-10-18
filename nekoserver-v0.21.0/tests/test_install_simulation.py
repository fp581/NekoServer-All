import os
from pathlib import Path
import tempfile
import unittest
import install_simulation

class InstallationSimulation(unittest.TestCase):
    def test_fresh_setup_rerun_and_failure_guards(self):
        if not install_simulation.COMPOSE.exists():self.skipTest('Official standalone Compose audit binary is required')
        with tempfile.TemporaryDirectory() as temp:
            row=install_simulation.run(Path(temp))
            self.assertEqual(row['first'],0,row)
            self.assertEqual(row['rerun'],0,row)
            self.assertEqual(row['custom_manifest_run'],1,row)
            self.assertTrue(row['custom_manifest_preserved'])
            self.assertNotEqual(row['wrong_uuid_guard'],0)
            self.assertEqual(row['source_usb_uuid'],'USB-0001')
            self.assertEqual(row['saved_usb_uuid'],'USB-0001')
