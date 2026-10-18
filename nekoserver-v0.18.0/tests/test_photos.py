import importlib.util, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('photo_backup',ROOT/'photo-backup.py'); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
class PhotoBackupTests(unittest.TestCase):
 def test_add_only_no_delete(self):
  with tempfile.TemporaryDirectory() as temp:
   src=Path(temp)/'src'; dest=Path(temp)/'dst'; src.mkdir(); dest.mkdir()
   (src/'a.jpg').write_bytes(b'photo'); mod.backup(src,dest,True)
   (src/'a.jpg').unlink(); mod.backup(src,dest,True)
   self.assertEqual((dest/'a.jpg').read_bytes(),b'photo')
 def test_collision_never_overwrites(self):
  with tempfile.TemporaryDirectory() as temp:
   src=Path(temp)/'src'; dest=Path(temp)/'dst'; src.mkdir(); dest.mkdir()
   (src/'a.jpg').write_bytes(b'new'); (dest/'a.jpg').write_bytes(b'old')
   with self.assertRaises(RuntimeError): mod.backup(src,dest,True)
   self.assertEqual((dest/'a.jpg').read_bytes(),b'old')
 def test_dryrun_no_writes(self):
  with tempfile.TemporaryDirectory() as temp:
   src=Path(temp)/'src'; dest=Path(temp)/'dst'; src.mkdir(); dest.mkdir();(src/'a.jpg').write_bytes(b'yes')
   mod.backup(src,dest,False)
   self.assertFalse((dest/'a.jpg').exists())
if __name__=='__main__':unittest.main()
