import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules import storage

class StorageTests(unittest.TestCase):
    def test_missing_uuid_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'explicitly configured'):
            storage.mount_specs({'data_mount':'/srv/nekoserver','media_mount':'/mnt/nekoserver-media','storage':{}})
    def test_duplicate_uuid_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'different UUIDs'):
            storage.mount_specs({'data_mount':'/srv/nekoserver','media_mount':'/mnt/nekoserver-media','storage':{'data_ssd':{'uuid':'aaaaaaaa-0000','fstype':'ext4'},'media_hdd':{'uuid':'aaaaaaaa-0000','fstype':'ext4'}}})
    def test_fstab_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fstab'; p.write_text('UUID=existing / ext4 defaults 0 1\n')
            spec=[('data_ssd','dddddddd-1234','ext4','/srv/nekoserver')]
            self.assertTrue(storage.install_fstab(spec,p))
            self.assertFalse(storage.install_fstab(spec,p))
            self.assertTrue((Path(tmp)/'fstab.nekoserver.bak').exists())
    def test_fstab_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fstab'; p.write_text('UUID=other /srv/nekoserver ext4 defaults 0 2\n')
            with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
                storage.install_fstab([('data_ssd','dddddddd-1234','ext4','/srv/nekoserver')],p)
