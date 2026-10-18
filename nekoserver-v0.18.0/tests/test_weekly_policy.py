"""Weekly photo policy and safe retirement of periodic media/music schedules."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import acceptance
import dashboard_collect as collect
import installer
from modules import media_monitor, music_backup, photo_preservation

ROOT=Path(__file__).resolve().parents[1]
CONFIG=json.loads((ROOT/'config.json').read_text())

class RetiredSchedules(unittest.TestCase):
    def exercise(self,module,base,custom=False):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);units=root/'units';units.mkdir();real=Path
            def mapped(value,*args):
                p=real(value,*args)
                return units/p.relative_to('/etc/systemd/system') if str(p).startswith('/etc/systemd/system') else p
            for suffix in ('timer','service'):
                (units/(base+'.'+suffix)).write_text('CUSTOM' if custom else '[Unit]\nDescription=NekoServer legacy managed schedule\n')
            cfg=copy.deepcopy(CONFIG);before={p.name:p.read_bytes() for p in units.iterdir()}
            ctx=SimpleNamespace(config=cfg,apply=True,log=lambda _:None)
            with patch.object(module,'Path',side_effect=mapped),patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as calls:
                if custom:
                    with self.assertRaises(RuntimeError):module.run(ctx)
                    self.assertFalse(calls.called)
                else:
                    module.run(ctx)
                    commands=[c.args[0] for c in calls.call_args_list]
                    self.assertEqual(commands,[['systemctl','disable','--now',base+'.timer'],['systemctl','stop',base+'.service']])
                self.assertEqual(before,{p.name:p.read_bytes() for p in units.iterdir()})
    def test_old_media_timer_is_disabled_and_worker_stopped(self):self.exercise(media_monitor,'nekoserver-media-health')
    def test_old_music_timer_is_disabled_and_worker_stopped(self):self.exercise(music_backup,'nekoserver-music-backup')
    def test_custom_media_units_are_preserved_before_any_command(self):self.exercise(media_monitor,'nekoserver-media-health',True)
    def test_custom_music_units_are_preserved_before_any_command(self):self.exercise(music_backup,'nekoserver-music-backup',True)
    def test_absent_disabled_units_do_not_create_a_schedule(self):
        with tempfile.TemporaryDirectory() as t:
            real=Path;root=Path(t)
            for module in (media_monitor,music_backup):
                def mapped(value,*args):
                    p=real(value,*args)
                    return root/p.name if str(p).startswith('/etc/systemd/system') else p
                with patch.object(module,'Path',side_effect=mapped),patch.object(module.subprocess,'run') as calls:
                    module.run(SimpleNamespace(config=CONFIG,apply=True,log=lambda _:None))
                    self.assertFalse(calls.called);self.assertFalse(list(root.iterdir()))
    def test_acceptance_requires_old_timer_inactive(self):
        with patch.object(collect,'run',return_value=(0,'LoadState=loaded\nActiveState=active\nUnitFileState=enabled')):
            self.assertFalse(acceptance.retired_timer('fixture.timer'))
        with patch.object(collect,'run',return_value=(0,'LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled')):
            self.assertTrue(acceptance.retired_timer('fixture.timer'))

class WeeklyPhotos(unittest.TestCase):
    def test_weekly_installer_retires_frequent_timer_and_uses_combined_worker(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);ssd=root/'ssd';hdd=root/'hdd';ssd.mkdir();hdd.mkdir();units=root/'units';units.mkdir();guard=root/'guard.json'
            cfg=copy.deepcopy(CONFIG);cfg.update(data_mount=str(ssd),media_mount=str(hdd))
            for suffix in ('timer','service'):(units/('nekoserver-photo-preservation.'+suffix)).write_text('[Unit]\nDescription=nekoserver-photo-preservation'+(' timer' if suffix=='timer' else '')+'\n')
            real=Path
            def mapped(value,*args):
                p=real(value,*args)
                return units/p.relative_to('/etc/systemd/system') if str(p).startswith('/etc/systemd/system') else p
            with patch.object(photo_preservation,'Path',side_effect=mapped),patch.object(photo_preservation.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as calls,patch.object(photo_preservation.native_runtime,'backup_guard',return_value=guard):
                photo_preservation.run(SimpleNamespace(config=cfg,apply=True,log=lambda _:None))
            commands=[c.args[0] for c in calls.call_args_list]
            self.assertIn(['systemctl','disable','--now','nekoserver-photo-preservation.timer'],commands)
            self.assertNotIn(['systemctl','enable','--now','nekoserver-photo-preservation.timer'],commands)
            timer=(units/'nekoserver-immich-db-backup.timer').read_text()
            self.assertIn('OnCalendar=Sun *-*-* 02:15:00 Asia/Jerusalem',timer)
            self.assertIn('Persistent=false',timer);self.assertNotIn('OnUnitInactiveSec',timer)
            service=(units/'nekoserver-immich-db-backup.service').read_text()
            self.assertIn('nekoserver-runtime preservation',service);self.assertIn('--db-backup',service)
    def test_weekly_success_does_not_go_stale_after_two_days(self):
        cfg=copy.deepcopy(CONFIG)
        def saved(path,*a):
            self.assertTrue(str(path).endswith('database.json') or not str(path).endswith('archive.json'))
            return {'status':'pass','last_success':100,'started_at':100}
        with patch.object(collect,'read_json',side_effect=saved),patch.object(collect,'timer_status',return_value={'installed':True,'active':True}):
            rows=collect.backups(cfg,100+6*86400,True)
        self.assertEqual({r['status'] for r in rows if r['id'] in ('archive','database')},{'pass'})
        self.assertNotIn('music',{r['id'] for r in rows})
    def test_disabled_media_report_never_reads_stale_api_report(self):
        with patch.object(collect,'read_json',side_effect=AssertionError('Must not read old audit')):
            result=collect.media_report(CONFIG,1000,True)
        self.assertEqual(result['status'],'disabled');self.assertEqual(result['checks'],[])
    def test_monthly_manual_health_worker_is_retained(self):
        source=(ROOT/'runtime/src/maintenance.rs').read_text()
        self.assertIn('"media-health"',source)
        plan=installer.ordered(installer.load_modules(ROOT/'modules'),CONFIG['profiles']['full'])
        self.assertIn('maintenance',plan);self.assertIn('media_monitor',plan);self.assertIn('music_backup',plan)

class SavedConfiguration(unittest.TestCase):
    def test_old_confirmed_config_gets_current_schedule_defaults(self):
        cfg=copy.deepcopy(CONFIG)
        cfg['monitoring'].pop('enabled');cfg['music'].pop('backup_enabled');cfg['photos'].pop('backup_schedule')
        before=copy.deepcopy(cfg['storage'])
        installer.validate_config(cfg)
        self.assertFalse(cfg['monitoring']['enabled']);self.assertFalse(cfg['music']['backup_enabled'])
        self.assertEqual(cfg['photos']['backup_schedule'],'weekly');self.assertEqual(cfg['storage'],before)
    def test_explicit_opt_in_is_preserved(self):
        cfg=copy.deepcopy(CONFIG);cfg['monitoring']['enabled']=True;cfg['music']['backup_enabled']=True;cfg['photos']['backup_schedule']='legacy'
        installer.validate_config(cfg)
        self.assertTrue(cfg['monitoring']['enabled']);self.assertTrue(cfg['music']['backup_enabled']);self.assertEqual(cfg['photos']['backup_schedule'],'legacy')
