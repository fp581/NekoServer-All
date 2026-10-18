"""Real native music copies/restores, safe layout refusal and HDD idle command fixtures."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from modules import media, music_backup, server_os
import installer
import test_native_v0100 as fixtures
import test_server_os as os_fixtures

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('music_recovery', ROOT/'music-recovery.py')
recovery=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(recovery)

class MusicCopies(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.Fixture();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.env=self.f.commands();self.source=self.f.data/'library/music';self.source.mkdir(parents=True)
        self.backup=self.f.media/'backups/music'
    def song(self,name='Artist/Album/01 - שיר.flac',value=b'lossless-music'):
        p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(value);os.utime(p,(time.time()-1200,)*2);return p
    def run_backup(self):
        return fixtures.native('music-backup','--config',self.f.config,env=self.env)
    def snapshots(self):return sorted((self.backup/'snapshots').glob('*.json'))
    def test_music_copies_and_restores_with_spaces_and_unicode(self):
        song=self.song();r=self.run_backup();self.assertEqual(r.returncode,0,r.stderr)
        snapshot=self.snapshots()[0];dest=self.f.root/'restored'
        self.assertEqual(recovery.restore(self.backup,snapshot,dest),1)
        self.assertEqual((dest/song.relative_to(self.source)).read_bytes(),song.read_bytes())
        self.assertEqual(json.loads((self.f.data/'reports/jobs/music.json').read_text())['status'],'pass')
        self.assertEqual(snapshot.stat().st_mode&0o777,0o600)
    def test_unchanged_music_deduplicates_and_tag_changes_retain_versions(self):
        p=self.song();self.assertEqual(self.run_backup().returncode,0)
        self.assertEqual(self.run_backup().returncode,0)
        objects=lambda:list((self.backup/'objects').glob('*/*'))
        self.assertEqual(len(objects()),1)
        self.song(value=b'new-tags');self.assertEqual(self.run_backup().returncode,0);self.assertEqual(len(objects()),2)
        recovery.restore(self.backup,self.snapshots()[0],self.f.root/'old')
        self.assertEqual((self.f.root/'old'/p.relative_to(self.source)).read_bytes(),b'lossless-music')
    def test_source_deletion_keeps_older_backup(self):
        p=self.song();self.assertEqual(self.run_backup().returncode,0);snapshot=self.snapshots()[0];p.unlink()
        self.assertEqual(self.run_backup().returncode,0)
        self.assertEqual(recovery.restore(self.backup,snapshot,self.f.root/'restored'),1)
    def test_corrupt_object_fails_backup_and_restore(self):
        self.song();self.assertEqual(self.run_backup().returncode,0);snapshot=self.snapshots()[0]
        list((self.backup/'objects').glob('*/*'))[0].write_bytes(b'corrupt')
        self.assertNotEqual(self.run_backup().returncode,0);self.assertEqual(len(self.snapshots()),1)
        with self.assertRaises(RuntimeError):recovery.restore(self.backup,snapshot,self.f.root/'restored')
        self.assertFalse((self.f.root/'restored').exists())
    def test_wrong_uuid_never_publishes_backup(self):
        self.song();self.env['NEKO_FIXTURE_MOUNT']='wrong';self.assertNotEqual(self.run_backup().returncode,0);self.assertFalse(self.backup.exists())
    def test_symlink_is_refused(self):
        self.song();(self.source/'link').symlink_to(self.f.root)
        self.assertNotEqual(self.run_backup().returncode,0);self.assertFalse(self.backup.exists())
    def test_recent_song_does_not_publish_partial_snapshot(self):
        p=self.song();os.utime(p,None);self.assertNotEqual(self.run_backup().returncode,0);self.assertFalse(self.snapshots())
    def test_restore_rejects_traversal_and_existing_destination(self):
        self.song();self.assertEqual(self.run_backup().returncode,0);snapshot=self.snapshots()[0]
        with self.assertRaises(RuntimeError):recovery.restore(self.backup,snapshot,self.source)
        value=json.loads(snapshot.read_text());value['files'][0]['path']='../escape';snapshot.write_text(json.dumps(value))
        with self.assertRaises(RuntimeError):recovery.restore(self.backup,snapshot,self.f.root/'restore')
        self.assertFalse((self.f.root/'restore').exists())

class IdlePolicy(unittest.TestCase):
    def test_native_sixty_minute_timer_targets_verified_hdd(self):
        f=os_fixtures.HddTests();f.setUp();self.addCleanup(f.doCleanups)
        cfg=json.loads(f.config.read_text());cfg['server_os']={'hdd_policy':'idle','hdd_idle_minutes':60};f.config.write_text(json.dumps(cfg))
        self.assertEqual(f.run_native().returncode,0);self.assertEqual(json.loads(f.log.read_text()),['-S','242','/dev/sdz'])
    def test_invalid_idle_time_never_changes_hardware(self):
        f=os_fixtures.HddTests();f.setUp();self.addCleanup(f.doCleanups)
        cfg=json.loads(f.config.read_text());cfg['server_os']={'hdd_policy':'idle','hdd_idle_minutes':1};f.config.write_text(json.dumps(cfg))
        self.assertNotEqual(f.run_native().returncode,0);self.assertFalse(f.log.exists())
    def test_python_config_refuses_invalid_idle_minutes(self):
        for n in (True,0,1,31,331,'60'):
            with self.assertRaises(RuntimeError):server_os.settings({'server_os':{'hdd_policy':'idle','hdd_idle_minutes':n}})
    def test_backup_timer_is_nightly_and_uses_rust(self):
        cfg=json.loads((ROOT/'config.json').read_text());units=music_backup.definitions(cfg,'/runtime','/config')
        self.assertIn('/runtime music-backup --config /config',units['nekoserver-music-backup.service'])
        self.assertIn('03:45:00 Asia/Jerusalem',units['nekoserver-music-backup.timer'])
        self.assertIn('Persistent=false',units['nekoserver-music-backup.timer'])
        plan=installer.ordered(installer.load_modules(ROOT/'modules'),cfg['profiles']['full'])
        self.assertLess(plan.index('music_backup'),plan.index('music'))
    def test_ssd_music_mounts_and_hdd_video_mounts(self):
        cfg=json.loads((ROOT/'config.json').read_text());ssd=Path('/ssd');hdd=Path('/hdd')
        text=lambda name:media.render((ROOT/'compose'/f'{name}.yaml').read_text(),ssd,hdd,1000,1000,'Asia/Jerusalem','127.0.0.1')
        self.assertIn('/ssd/library/music:/music:ro',text('navidrome'))
        self.assertIn('/ssd/library/music:/data/music',text('lidarr'))
        self.assertIn('/ssd/library/downloads/music:/data/downloads/music',text('qbittorrent'))
        self.assertIn('/hdd/library/movies:/data/movies:ro',text('jellyfin'))
        self.assertNotIn('/ssd/library/movies',text('jellyfin'))

class StorageWiring(unittest.TestCase):
    def test_permission_checks_follow_ssd_music_and_hdd_movies(self):
        import permission_checks
        from unittest.mock import patch
        cfg=json.loads((ROOT/'config.json').read_text());seen=[]
        with patch.object(permission_checks,'access',side_effect=lambda p,*a:seen.append(Path(p)) or True):
            rows=permission_checks.check(cfg,['media'])
        self.assertTrue(all(r['status']=='pass' for r in rows))
        self.assertIn(Path(cfg['data_mount'])/'library/music',seen)
        self.assertIn(Path(cfg['data_mount'])/'library/downloads/music',seen)
        self.assertIn(Path(cfg['media_mount'])/'library/movies',seen)
        self.assertNotIn(Path(cfg['media_mount'])/'library/music',seen)
    def test_music_backup_installs_timer_and_preserves_custom_units(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from neko_common import atomic_json
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);cfg=json.loads((ROOT/'config.json').read_text());cfg['music']['backup_enabled']=True;cfg['data_mount']=str(root/'ssd');cfg['media_mount']=str(root/'hdd')
            real=Path
            def mapped(value,*args):
                path=real(value,*args)
                return root/str(path).lstrip('/') if str(path).startswith('/etc/') else path
            def guard(config):
                path=root/'etc/nekoserver-backups/storage.json';atomic_json(path,music_backup.native_runtime.storage_config(config));return path
            ctx=SimpleNamespace(config=cfg,apply=True,log=lambda _:None)
            with patch.object(music_backup,'Path',side_effect=mapped),patch.object(music_backup,'verify_storage'),patch.object(music_backup.native_runtime,'deploy',return_value=ROOT/'runtime/bin/nekoserver-runtime'),patch.object(music_backup.native_runtime,'backup_guard',side_effect=guard),patch.object(music_backup.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as calls:
                music_backup.run(ctx)
                self.assertIn(['systemctl','enable','--now','nekoserver-music-backup.timer'],[c.args[0] for c in calls.call_args_list])
                saved=root/'etc/nekoserver-backups/storage.json';self.assertEqual(saved.stat().st_mode&0o777,0o600)
                unit=root/'etc/systemd/system/nekoserver-music-backup.timer';unit.write_text('CUSTOM')
                with self.assertRaises(RuntimeError):music_backup.run(ctx)
                self.assertEqual(unit.read_text(),'CUSTOM')
    def test_local_music_import_goes_to_ssd_and_movies_to_hdd(self):
        import tempfile
        from unittest.mock import patch
        import source_import
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);ssd=root/'ssd';hdd=root/'hdd';ssd.mkdir();hdd.mkdir()
            source=root/'input';source.mkdir();(source/'track.flac').write_bytes(b'content')
            cfg={'data_mount':str(ssd),'media_mount':str(hdd),'media':{'uid':1000,'gid':1000}}
            entries=[{'type':'local','path':str(source),'category':c} for c in ('music','movies')]
            with patch.object(source_import.os.path,'ismount',return_value=True),patch.object(source_import.os,'fchown'),patch.object(source_import.os,'chown'):
                self.assertEqual(source_import.run(cfg,entries,True),0)
            self.assertEqual(next((ssd/'library/music').glob('*/*')).read_bytes(),b'content')
            self.assertEqual(next((hdd/'library/movies').glob('*/*')).read_bytes(),b'content')
            self.assertFalse((hdd/'library/music').exists())
