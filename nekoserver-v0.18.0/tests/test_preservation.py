import importlib.util,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def load(name):
 spec=importlib.util.spec_from_file_location(name.replace('-','_'),ROOT/name);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
preserve=load('photo-preserve.py');db=load('immich-db-backup.py')
class PreservationTests(unittest.TestCase):
 def test_originals_archive_and_survive_deletion(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'managed';arc=root/'archive';f=src/'upload'/'user'/'x.jpg';f.parent.mkdir(parents=True);f.write_bytes(b'photo')
   preserve.transfer(src,arc,True,600,time.time()+1000)
   f.unlink();preserve.transfer(src,arc,True,600,time.time()+1000)
   self.assertEqual((arc/'upload/user/x.jpg').read_bytes(),b'photo')
 def test_collision_is_not_overwritten(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'managed';arc=root/'archive';f=src/'library'/'a.heic';f.parent.mkdir(parents=True);f.write_bytes(b'new');dst=arc/'library'/'a.heic';dst.parent.mkdir(parents=True);dst.write_bytes(b'old')
   with self.assertRaises(RuntimeError):preserve.transfer(src,arc,True,600,time.time()+1000)
   self.assertEqual(dst.read_bytes(),b'old')
 def test_skips_recent_files(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);f=root/'managed'/'upload'/'x.jpg';f.parent.mkdir(parents=True);f.write_bytes(b'data')
   self.assertEqual(preserve.transfer(root/'managed',root/'archive',True,600),0)
   self.assertFalse((root/'archive'/'upload'/'x.jpg').exists())
 def test_ignores_thumbnails_and_other_files(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);src=root/'managed';(src/'thumbs').mkdir(parents=True);(src/'thumbs'/'thumb.jpg').write_bytes(b'x');(src/'upload').mkdir();(src/'upload'/'readme.txt').write_bytes(b'not photo')
   preserve.transfer(src,root/'archive',True,600,time.time()+1000)
   self.assertFalse((root/'archive').exists())
 def test_no_nested_archive(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'managed'/'upload').mkdir(parents=True)
   with self.assertRaises(RuntimeError):preserve.transfer(root/'managed',root/'managed'/'archive',True,600)
 def test_db_backup_dryrun(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'compose.yaml').write_text('test');(root/'.env').write_text('test')
   self.assertIsNone(db.backup(root/'compose.yaml',root/'.env',root/'dumps',False))
   self.assertFalse((root/'dumps').exists())
if __name__=='__main__':unittest.main()
