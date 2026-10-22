"""Setup wizard, capacity check and recovery wizard."""
import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import first_checkup
import recovery_wizard  # noqa: E402
import wizard  # noqa: E402
from modules import capacity  # noqa: E402

GIB = 1024 ** 3


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class Ctx:
    def __init__(self, config, apply=True):
        self.config, self.apply, self.logs = config, apply, []

    def log(self, message):
        self.logs.append(message)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'config.json', self.root / 'config.json')

    def test_created_once_with_the_sudo_users_ids(self):
        me = pwd.getpwuid(os.getuid())
        path, created = wizard.prepare_settings(self.root, me)
        self.assertTrue(created)
        settings = json.loads(path.read_text())
        self.assertEqual((settings['media']['uid'], settings['media']['gid']), (me.pw_uid, me.pw_gid))
        self.assertEqual(settings['timezone'], 'auto')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Second run keeps the file (and the choices the installer saved in it).
        settings['storage']['data_ssd']['uuid'] = 'kept'
        path.write_text(json.dumps(settings))
        again, created_again = wizard.prepare_settings(self.root, me)
        self.assertFalse(created_again)
        self.assertEqual(json.loads(again.read_text())['storage']['data_ssd']['uuid'], 'kept')

    def test_template_runs_tailscale_and_capacity_in_full(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        self.assertEqual(full[:4], ['system', 'tailscale', 'storage', 'capacity'])
        self.assertLess(full.index('tailscale'), full.index('gateway_tailnet'))


class WizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / 'config.json', self.root / 'config.json')
        geteuid = mock.patch('os.geteuid', return_value=0)
        geteuid.start()
        self.addCleanup(geteuid.stop)

    def test_saying_no_changes_nothing(self):
        execute = mock.Mock()
        out = []
        code = wizard.run(execute, self.root / 'state', prompt=answers('no'), output=out.append, root=self.root)
        self.assertEqual(code, 0)
        execute.assert_not_called()
        self.assertTrue(any('No apps were set up' in line for line in out))

    def test_yes_runs_the_full_install_with_the_settings_file(self):
        execute = mock.Mock(return_value=0)
        out = []
        with mock.patch('first_checkup.run') as checkup:
            wizard.run(execute, self.root / 'state', prompt=answers('yes'), output=out.append, root=self.root)
        args = execute.call_args[0][0]
        self.assertEqual((args.profile, args.apply, args.config), ('full', True, str(self.root / 'my-config.json')))
        self.assertTrue(any('NekoServer is' in line for line in out))
        # Every step finished: the first checkup and backup run straight away.
        checkup.assert_called_once()

    def test_a_failed_first_checkup_fails_the_setup(self):
        with mock.patch('first_checkup.run', return_value=False):
            code = wizard.run(mock.Mock(return_value=0), self.root / 'state', prompt=answers('yes'),
                              output=lambda s: None, root=self.root)
        self.assertEqual(code, 1)

    def test_no_first_backup_while_steps_are_unfinished(self):
        with mock.patch('first_checkup.run') as checkup:
            wizard.run(mock.Mock(return_value=1), self.root / 'state', prompt=answers('yes'),
                       output=lambda s: None, root=self.root)
        checkup.assert_not_called()

    def test_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(wizard.run(mock.Mock(), self.root, prompt=answers(), output=lambda s: None,
                                        root=self.root), 2)


class FirstCheckupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'data_mount': self.tmp.name}
        (Path(self.tmp.name) / 'reports/jobs').mkdir(parents=True)

    def report(self, job, status, **extra):
        (Path(self.tmp.name) / 'reports/jobs' / (job + '.json')).write_text(json.dumps({'status': status, **extra}))

    def systemctl(self, enabled, failing=()):
        calls = []

        def run(argv, **_):
            calls.append(argv)
            unit = argv[-1].rsplit('.', 1)[0]
            if argv[1] == 'is-enabled':
                return mock.Mock(returncode=0 if unit in enabled else 1,
                                 stdout='enabled\n' if unit in enabled else 'disabled\n')
            return mock.Mock(returncode=1 if unit in failing else 0)
        return run, calls

    def test_only_switched_on_backups_run_in_order_then_the_checkup(self):
        self.report('applications', 'pass')
        run, calls = self.systemctl({'nekoserver-app-backup', 'nekoserver-media-health'})
        out = []
        ok = first_checkup.run(self.config, out.append, run, checks=lambda config: (12, []))
        started = [argv[-1] for argv in calls if argv[1] == 'start']
        self.assertEqual(started, ['nekoserver-media-health.service', 'nekoserver-app-backup.service'])
        self.assertTrue(ok)
        self.assertIn('  Checkup: 12 checks passed.', out)

    def test_a_failed_backup_and_a_failed_check_are_named(self):
        run, _ = self.systemctl({'nekoserver-host-backup'}, failing={'nekoserver-host-backup'})
        out = []
        ok = first_checkup.run(self.config, out.append, run, checks=lambda config: (3, ['Drive space']))
        self.assertFalse(ok)
        text = '\n'.join(out)
        self.assertIn('journalctl -u nekoserver-host-backup.service', text)
        self.assertIn('Needs a look: Drive space', text)

    def test_a_partial_backup_is_not_called_done(self):
        # systemctl says it finished fine; its own report says part was left out.
        self.report('host', 'partial', missing={'source': 'Steam was open'})
        run, _ = self.systemctl({'nekoserver-host-backup'})
        out = []
        self.assertFalse(first_checkup.run(self.config, out.append, run, checks=lambda config: (3, [])))
        self.assertIn('    done in part: Steam was open', out)

    def test_a_check_still_to_set_up_needs_a_look(self):
        report = {'automatic': [{'id': 'a', 'name': 'Drives', 'status': 'pass'},
                                {'id': 'b', 'name': 'Phone alerts', 'status': 'needs_setup'}]}
        with mock.patch('acceptance.inspect', return_value=report), mock.patch('acceptance.save'):
            self.assertEqual(first_checkup.inspect(self.config), (1, ['Phone alerts (still to set up)']))

    def test_a_checkup_that_cannot_run_says_how_to_run_it_later(self):
        run, _ = self.systemctl(set())

        def broken(config):
            raise RuntimeError('SSD not mounted')
        out = []
        self.assertFalse(first_checkup.run(self.config, out.append, run, checks=broken))
        self.assertIn('acceptance.py --save', out[-1])

    def test_a_failed_checkup_is_never_ready(self):
        out = []
        states = {'storage': {'status': 'success'}}
        config = {'data_mount': self.tmp.name}
        self.assertFalse(wizard.finishing_screen(config, states, None, out.append, checkup=False))
        text = '\n'.join(out)
        self.assertIn('almost ready', text)
        self.assertNotIn('NekoServer is ready!', text)
        self.assertIn('First checkup and backup: something needs a look', text)
        self.assertTrue(wizard.finishing_screen(config, states, None, lambda s: None, checkup=True))


class FinishingScreenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / 'reports').mkdir()
        (self.data / 'reports/gateway-lan.json').write_text(json.dumps({'portal': 'https://192.168.1.20:8442/'}))
        (self.data / 'reports/gateway-tailnet.json').write_text(json.dumps(
            {'links': {'jellyfin': 'https://neko.tail1.ts.net:8443/'}}))
        self.config = {'data_mount': str(self.data)}

    def screen(self, states):
        out = []
        complete = wizard.finishing_screen(self.config, states, None, out.append)
        return complete, '\n'.join(out)

    def test_all_done(self):
        complete, text = self.screen({'storage': {'status': 'success'}, 'media': {'status': 'success'}})
        self.assertTrue(complete)
        self.assertIn('NekoServer is ready!', text)
        self.assertIn('https://192.168.1.20:8442/', text)
        self.assertIn('https://neko.tail1.ts.net:8443/', text)
        self.assertIn('sudo cat', text)
        self.assertIn('--recover', text)

    def test_the_dashboard_address_uses_its_configured_port(self):
        _, text = self.screen({'storage': {'status': 'success'}})
        self.assertIn('http://127.0.0.1:8787', text)
        self.config['dashboard'] = {'port': 8899}
        _, text = self.screen({'storage': {'status': 'success'}})
        self.assertIn('Dashboard:          http://127.0.0.1:8899', text)
        self.assertNotIn('8787', text)

    def test_the_finishing_screen_says_whether_a_pin_was_chosen(self):
        with tempfile.TemporaryDirectory() as folder:
            pin = Path(folder) / 'pin.json'
            out = []
            wizard.show_dashboard(self.config, out.append, pin)
            self.assertIn('not set', out[-1])
            pin.write_text('{}')
            out = []
            wizard.show_dashboard(self.config, out.append, pin)
            self.assertEqual(out[-1], 'Dashboard PIN:      set')

    def test_missing_or_unreadable_state_is_never_ready(self):
        complete, text = self.screen({})
        self.assertFalse(complete)
        self.assertNotIn('NekoServer is ready!', text)
        self.config['profiles'] = {'full': ['system', 'storage', 'media']}
        complete, text = self.screen({'system': {'status': 'success'}, 'storage': {'status': 'success'}})
        self.assertFalse(complete)
        self.assertIn('Movie, TV and music apps: did not run', text)
        complete, text = self.screen({'system': {'status': 'success'}, 'storage': {'status': 'success'},
                                      'media': {'status': 'success'}})
        self.assertTrue(complete)

    def test_pending_and_failed_steps_are_named_in_plain_words(self):
        complete, text = self.screen({
            'storage': {'status': 'success'},
            'tailscale': {'status': 'needs_setup', 'reason': 'Tailscale authorization pending'},
            'gateway_tailnet': {'status': 'blocked', 'reason': "Dependencies not successful: ['tailscale']"},
        })
        self.assertFalse(complete)
        self.assertIn('almost ready', text)
        self.assertIn('Tailscale: Tailscale authorization pending', text)
        self.assertIn('Access from anywhere and phone setup', text)
        self.assertIn('run  sudo bash install.sh  again', text)


class CapacityTests(unittest.TestCase):
    CONFIG = {'data_mount': '/ssd', 'media_mount': '/hdd', 'photos': {'external_backup_uuid': 'u'}}

    def assess(self, ssd, hdd, usb=None, photos=0, cloud=0, pending=None, dump=0, unarchived=0):
        free = {'/ssd': ssd * GIB, '/hdd': hdd * GIB}
        used = {'/ssd/photos/immich-library': photos * GIB, '/hdd/cloud/files': cloud * GIB}
        pending = photos + cloud if pending is None else pending
        return capacity.assess(self.CONFIG, free=lambda p: free[str(p)], used=lambda p: used.get(str(p), 0),
                               usb_space=lambda uuid, pairs, **_: None if usb is None else (usb * GIB, pending * GIB),
                               dump=lambda config: dump * GIB, unarchived=lambda config: unarchived * GIB)

    def test_usb_counts_free_space_not_drive_size(self):
        # A big but nearly full drive warns; files the drive already has need no new room.
        _, warn = self.assess(500, 4000, usb=10, photos=100)
        self.assertTrue(any('USB drive has 10 GB free' in w for w in warn))
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=0)
        self.assertEqual(warn, [])

    def test_old_versions_on_the_usb_are_not_counted_as_room(self):
        # 100 GB of new files, 30 GB free, 150 GB of earlier versions: the copy cannot fit.
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=100)
        self.assertTrue(any('still has to add 100 GB' in w for w in warn))
        # This week's database copy counts too.
        _, warn = self.assess(500, 4000, usb=30, photos=100, pending=5, dump=20)
        self.assertTrue(any('still has to add 25 GB' in w for w in warn))

    def test_pending_counts_only_new_and_changed_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ssd, hdd, usb = root / 'ssd', root / 'hdd', root / 'usb'
            files = {
                hdd / 'photos/archive/upload/u/same.jpg': (b'a' * 10, b'a' * 10),
                hdd / 'photos/archive/upload/u/new.jpg': (b'b' * 20, None),
                hdd / 'photos/archive/library/changed.xmp': (b'c' * 30, b'c' * 5),
                hdd / 'cloud/files/alice/files/doc.txt': (b'd' * 40, None),
                hdd / 'cloud/files/appdata_x/files/skip.bin': (b'e' * 50, None),
                hdd / 'photos/archive/upload/u/live.jpg.part': (b'f' * 60, None),
            }
            for path, (source, copy) in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source)
            mapping = {pair[0]: pair[1] for pair in capacity.usb_sources({'data_mount': ssd, 'media_mount': hdd})}
            for path, (source, copy) in files.items():
                base = next((s for s in mapping if s in path.parents), None)
                if copy is not None and base is not None:
                    target = usb / mapping[base] / path.relative_to(base)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(copy)
            # An old dated version on the drive is not room for anything new.
            (usb / 'upload/u/same.neko-20250101T000000Z.jpg').write_bytes(b'x' * 999)
            self.assertEqual(capacity.pending_bytes(usb, list(mapping.items())), 20 + 30 + 40)

    def test_photos_kept_only_in_the_hdd_archive_need_room_on_the_usb(self):
        # A photo deleted from Immich stays in the archive and the USB job copies it; a
        # photo not archived yet goes to the archive first and then to the USB drive too.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config = {'data_mount': root / 'ssd', 'media_mount': root / 'hdd'}
            usb = root / 'usb'
            (root / 'hdd/photos/archive/upload/u').mkdir(parents=True)
            (root / 'hdd/photos/archive/upload/u/deleted-from-immich.jpg').write_bytes(b'k' * 70)
            (root / 'hdd/photos/archive/upload/u/old.neko-20261001T000000Z.jpg').write_bytes(b'v' * 5)
            (root / 'ssd/photos/immich-library/upload/u').mkdir(parents=True)
            (root / 'ssd/photos/immich-library/upload/u/brand-new.jpg').write_bytes(b'n' * 9)
            usb.mkdir()
            self.assertEqual(capacity.pending_bytes(usb, capacity.usb_sources(config)), 75)
            self.assertEqual(capacity.awaiting_archive(config), 9)
        _, warn = self.assess(500, 4000, usb=30, pending=20, unarchived=15)
        self.assertTrue(any('still has to add 35 GB' in w for w in warn), warn)

    def test_a_damaged_usb_photo_counts_its_full_replacement(self):
        # The weekly mirror keeps a damaged USB copy aside and writes the archive file
        # again; the estimate must count that, following the mirror's own checks.
        import json as json_module
        import time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive, usb = root / 'hdd/photos/archive', root / 'usb'
            (archive / 'upload').mkdir(parents=True)
            (usb / 'upload').mkdir(parents=True)
            (archive / 'upload/current.mp4').write_bytes(b'A' * 40)
            (archive / 'upload/.current.mp4.neko-latest').write_text('current.mp4')
            (archive / 'upload/other.mp4').write_bytes(b'B' * 30)
            (usb / 'upload/.current.mp4.neko-latest').write_text('current.mp4')
            (usb / 'upload/current.mp4').write_bytes(b'Z' * 40)  # damaged, same size, newer
            (usb / 'upload/other.mp4').write_bytes(b'Y' * 30)    # damaged, same size, newer
            pairs = [(archive / 'upload', 'upload')]
            # Weekly: the noted file is checked by checksum, the other one is not (yet).
            self.assertEqual(capacity.pending_bytes(usb, pairs, mirror=('upload',)), 40)
            # The 4-weekly full check compares everything.
            self.assertEqual(capacity.pending_bytes(usb, pairs, mirror=('upload',), full=True), 70)
            config = {'data_mount': root / 'ssd'}
            self.assertTrue(capacity.full_check_due(config))
            report = root / 'ssd/reports/jobs/external-full-check.json'
            report.parent.mkdir(parents=True)
            report.write_text(json_module.dumps({'at': time.time()}))
            self.assertFalse(capacity.full_check_due(config))

    def test_a_same_size_edit_needs_room_again(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src, usb = root / 'src', root / 'usb'
            usb.mkdir()
            (usb / 'a.txt').write_bytes(b'AAAA')
            (usb / 'b.txt').write_bytes(b'BBBB')
            (usb / 'c.txt').write_bytes(b'CCCC')
            (usb / 'c.neko-20261001T000000Z.txt').write_bytes(b'DDDD')
            src.mkdir()
            (src / 'a.txt').write_bytes(b'XXXX')  # edited, same size: a new version on the USB
            (src / 'b.txt').write_bytes(b'BBBB')  # rewritten with the same content
            (src / 'c.txt').write_bytes(b'DDDD')  # back to a version the drive already has
            for name in ('a.txt', 'b.txt', 'c.txt', 'c.neko-20261001T000000Z.txt'):
                os.utime(usb / name, (0, 0))  # copies older than the source change
            self.assertEqual(capacity.pending_bytes(usb, [(src, '.')]), 4)

    def test_enough_space(self):
        self.assertEqual(self.assess(500, 4000, usb=1000, photos=200), ([], []))

    def test_too_little_stops_with_a_clear_reason(self):
        stop, _ = self.assess(10, 4000)
        self.assertTrue(stop and 'SSD has only 10 GB free' in stop[0])
        stop, _ = self.assess(500, 30)
        self.assertTrue(stop and 'HDD' in stop[0])

    def test_below_recommended_warns(self):
        stop, warn = self.assess(60, 300)
        self.assertEqual(stop, [])
        self.assertEqual(len(warn), 2)

    def test_usb_must_fit_photos_and_cloud_with_room(self):
        _, warn = self.assess(500, 4000, usb=128, photos=100, cloud=20)
        self.assertTrue(any('USB drive has 128 GB free' in w for w in warn))
        _, warn = self.assess(500, 4000, usb=256, photos=100, cloud=20)
        self.assertEqual(warn, [])

    def test_stop_raises_and_warning_asks(self):
        ctx = Ctx({})
        with mock.patch.object(capacity, 'assess', return_value=(['too small'], [])):
            with self.assertRaisesRegex(RuntimeError, 'too small'):
                capacity.run(ctx)
        with mock.patch.object(capacity, 'assess', return_value=([], ['low'])), \
                mock.patch('guided_setup.interactive', return_value=True):
            self.assertIsNone(capacity.run(Ctx({}), prompt=answers('yes'), output=lambda s: None))
            result = capacity.run(Ctx({}), prompt=answers('no'), output=lambda s: None)
            self.assertEqual(result['status'], 'needs_setup')


class RecoveryWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        self.config = {'media_mount': str(self.media), 'data_mount': str(self.root / 'ssd'),
                       'photos': {'archive_dir': 'photos/archive'}}

    def write(self, rel, body=b'x'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def test_backups_are_listed_newest_first(self):
        for name in ('snapshot-1000000-1', 'snapshot-3000000-1', 'snapshot-2000000-1'):
            self.write(f'hdd/backups/media-applications/{name}/manifest.json')
        self.write('hdd/backups/media-applications/.snapshot-9-1.partial/manifest.json')
        names = [p.name for p, _ in recovery_wizard.snapshots(self.media / 'backups/media-applications')]
        self.assertEqual(names, ['snapshot-3000000-1', 'snapshot-2000000-1', 'snapshot-1000000-1'])
        self.write('hdd/backups/monthly/100-1/manifest.json')
        self.write('hdd/backups/monthly/200-1/manifest.json')
        self.write('hdd/backups/monthly/200-1/secrets.tar.cms')
        runs = recovery_wizard.monthly_snapshots(self.media / 'backups/monthly', need='secrets.tar.cms')
        self.assertEqual([p.name for p, _ in runs], ['200-1'])

    def test_restore_folder_must_be_new(self):
        existing = self.root / 'exists'
        existing.mkdir()
        out = []
        # Suggested folder exists -> refused; typed relative path -> refused; then a good one.
        chosen = recovery_wizard.new_folder(existing, answers('yes', 'relative', str(self.root / 'fresh'), 'yes'),
                                            out.append)
        self.assertEqual(chosen, self.root / 'fresh')
        self.assertTrue(any('already exists' in line for line in out))
        self.assertIsNone(recovery_wizard.new_folder(existing, answers('no', ''), out.append))

    def test_photos_restore_copies_into_a_new_folder_only(self):
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        self.write('hdd/photos/archive/library/b.heic', b'photo2')
        self.write('ssd/photos/immich-library/upload/live.jpg', b'live')
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_photos(self.config, answers('1', 'yes', 'no'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual((restored / 'upload/u1/a.jpg').read_bytes(), b'photo')
        self.assertEqual((restored / 'library/b.heic').read_bytes(), b'photo2')
        self.assertEqual((self.root / 'ssd/photos/immich-library/upload/live.jpg').read_bytes(), b'live')
        self.assertTrue(any('2 files copied' in line for line in out))
        self.assertEqual((self.media / 'restored').stat().st_mode & 0o777, 0o700)

    def test_a_restore_never_goes_inside_a_backup(self):
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        inside = self.media / 'photos/archive/upload/restored-here'
        elsewhere = self.root / 'restore-elsewhere'
        out = []
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=None), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            # Offered folder declined; one inside the archive refused; then a separate one.
            recovery_wizard.restore_photos(self.config, answers('1', 'no', str(inside), 'yes', str(elsewhere), 'yes', 'no'),
                                           out.append)
        self.assertIn('inside (or around) a backup', '\n'.join(out))
        self.assertFalse(inside.exists())
        self.assertEqual((elsewhere / 'upload/u1/a.jpg').read_bytes(), b'photo')
        # The archive still holds its one file only.
        self.assertEqual(len([p for p in (self.media / 'photos/archive').rglob('*') if p.is_file()]), 1)
        # A folder around a backup is refused too.
        self.assertIsNotNone(recovery_wizard.folder_problem(self.media / 'photos', recovery_wizard.backup_trees(self.config)))

    def test_nextcloud_restore_lists_users_from_the_usb(self):
        usb = self.root / 'usb'
        self.write('usb/nextcloud/alice/files/Docs/plan.odt', b'plan')
        self.write('usb/nextcloud/bob/files/todo.txt', b'todo')
        self.assertEqual(recovery_wizard.cloud_users(usb), ['alice', 'bob'])
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('2', 'yes', 'no'), out.append)
        restored = next((self.media / 'restored').iterdir())
        self.assertIn('nextcloud-bob', restored.name)
        self.assertEqual((restored / 'files/todo.txt').read_bytes(), b'todo')

    def test_files_encrypted_by_nextcloud_are_not_called_ready_to_upload(self):
        usb = self.root / 'usb'
        self.write('usb/nextcloud/alice/files/report.pdf', b'HBEGIN:oc_encryption_module:OC_DEFAULT_MODULE:cipher:AES-256-CTR:HEND')
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'no'), out.append)
        text = '\n'.join(out)
        self.assertIn('ENCRYPTED', text)
        self.assertNotIn('upload the', text)

    def test_newest_version_is_restored_under_its_own_name(self):
        usb = self.root / 'usb'
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.docx', b'first')
        self.write(files + 'report.neko-20261001T090000Z.docx', b'second')
        self.write(files + 'report.neko-20261012T140300Z.docx', b'newest')
        self.write(files + 'report.neko-20261012T140300Z-1.docx', b'newest, same second')
        self.write(files + 'notes', b'only one')
        self.write(files + 'IMG_1.HEIC.neko-20261005T000000Z.xmp', b'sidecar edit')
        self.write(files + 'IMG_1.HEIC.xmp', b'sidecar')
        # What the backup records (since 0.28.7) about whose versions these are.
        self.write(files + '.neko-names.json', json.dumps({'format': 'neko-names-v1', 'names': {}, 'versions': {
            'report.neko-20261001T090000Z.docx': 'report.docx', 'report.neko-20261012T140300Z.docx': 'report.docx',
            'report.neko-20261012T140300Z-1.docx': 'report.docx',
            'IMG_1.HEIC.neko-20261005T000000Z.xmp': 'IMG_1.HEIC.xmp'}}).encode())
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), out.append)
        restored = next((self.media / 'restored').iterdir()) / 'files'
        self.assertEqual(sorted(p.name for p in restored.iterdir()), ['IMG_1.HEIC.xmp', 'notes', 'report.docx'])
        self.assertEqual((restored / 'report.docx').read_bytes(), b'newest, same second')
        self.assertEqual((restored / 'IMG_1.HEIC.xmp').read_bytes(), b'sidecar edit')
        text = '\n'.join(out)
        self.assertIn('report.docx: version from 2026-10-12 14:03 UTC (4 versions', text)
        self.assertNotIn('older versions', text)

    def restore_newest(self):
        usb = self.root / 'usb'
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), lambda s: None)
        return next((self.media / 'restored').iterdir()) / 'files'

    def test_a_file_that_only_looks_like_a_version_is_restored_as_itself(self):
        # Two separate originals, both backed up in their own right: never merged.
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'one document')
        self.write(files + 'report.neko-20261022T120000Z.txt', b'another document')
        restored = self.restore_newest()
        self.assertEqual(sorted(p.name for p in restored.iterdir()), ['report.neko-20261022T120000Z.txt', 'report.txt'])
        self.assertEqual((restored / 'report.txt').read_bytes(), b'one document')

    def test_an_older_looking_file_of_its_own_is_never_taken_for_a_version(self):
        # Two separate originals; report.txt was then edited (a recorded version and a
        # note naming it). The older-looking file stays a file of its own.
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'first')
        self.write(files + 'report.neko-20250101T120000Z.txt', b'another document')
        self.write(files + 'report.neko-20261022T130000Z.txt', b'edited')
        self.write(files + '.report.txt.neko-latest', b'report.neko-20261022T130000Z.txt')
        self.write(files + '.neko-names.json', json.dumps({'format': 'neko-names-v1', 'names': {}, 'versions': {
            'report.neko-20261022T130000Z.txt': 'report.txt'}}).encode())
        restored = self.restore_newest()
        self.assertEqual((restored / 'report.txt').read_bytes(), b'edited')
        self.assertEqual((restored / 'report.neko-20250101T120000Z.txt').read_bytes(), b'another document')

    def test_a_recorded_version_list_decides_over_the_name(self):
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'report.txt', b'first')
        self.write(files + 'report.neko-20261001T090000Z.txt', b'edit')
        self.write(files + '.report.txt.neko-latest', b'report.neko-20261001T090000Z.txt')
        self.write(files + 'report.neko-20261022T120000Z.txt', b'a file of its own')
        self.write(files + '.neko-names.json', json.dumps({'format': 'neko-names-v1', 'names': {}, 'versions': {
            'report.neko-20261001T090000Z.txt': 'report.txt'}}).encode())
        restored = self.restore_newest()
        self.assertEqual((restored / 'report.txt').read_bytes(), b'edit')
        self.assertEqual((restored / 'report.neko-20261022T120000Z.txt').read_bytes(), b'a file of its own')

    def test_an_undone_edit_restores_the_content_the_backup_last_saw(self):
        # AAAA -> BBBB -> AAAA: the backup kept BBBB as a dated version and noted that
        # the original is current again; "newest only" must restore AAAA.
        usb = self.root / 'usb'
        files = 'usb/nextcloud/alice/files/'
        self.write(files + 'a.txt', b'AAAA')
        self.write(files + 'a.neko-20261001T090000Z.txt', b'BBBB')
        self.write(files + '.a.txt.neko-latest', b'a.txt')
        self.write(files + 'b.txt', b'one')
        self.write(files + 'b.neko-20261001T090000Z.txt', b'two')
        self.write(files + 'b.neko-20261002T090000Z.txt', b'three')
        self.write(files + '.b.txt.neko-latest', b'b.neko-20261001T090000Z.txt')
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None), \
                mock.patch.object(recovery_wizard, 'stamp', side_effect=['1', '2']):
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'yes'), lambda s: None)
            recovery_wizard.restore_cloud(self.config, answers('1', 'yes', 'no'), lambda s: None)
        latest, every = sorted((self.media / 'restored').iterdir())
        self.assertEqual((latest / 'files/a.txt').read_bytes(), b'AAAA')
        self.assertEqual((latest / 'files/b.txt').read_bytes(), b'two')
        # Versions from before 0.28.7 that no note or record ties to their file come back
        # as files of their own: an extra file, never a lost one.
        self.assertEqual(sorted(p.name for p in (latest / 'files').iterdir()),
                         ['a.neko-20261001T090000Z.txt', 'a.txt', 'b.neko-20261002T090000Z.txt', 'b.txt'])
        # Copying every version leaves the backup's notes out.
        self.assertFalse(any(p.name.endswith('.neko-latest') for p in (every / 'files').iterdir()))

    def ledger(self, rel_root, files):
        import hashlib as h
        entries = {key: {'sha256': h.sha256(body).hexdigest(), 'size': len(body), 'verified_at': 1.0, 'how': 'copied'}
                   for key, body in files.items()}
        self.write(rel_root + '/.neko-ledger.json', json.dumps({'format': 'neko-ledger-v1', 'entries': entries}).encode())

    def restore_cloud(self, *choices):
        usb = self.root / 'usb'
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.restore_cloud(self.config, answers(*choices), out.append)
        return out

    def test_restored_files_are_checked_against_their_recorded_checksums(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept safe')
        self.write('usb/nextcloud/alice/files/old.txt', b'from before checksums')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept safe'})
        out = self.restore_cloud('1', 'yes', 'yes')
        restored = next((self.media / 'restored').iterdir()) / 'files'
        self.assertEqual((restored / 'a.txt').read_bytes(), b'kept safe')
        self.assertIn('1 files matched their recorded checksums; 1 had no recorded checksum yet', '\n'.join(out))

    def test_a_damaged_backup_file_stops_the_restore(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept safe')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept safe'})
        self.write('usb/nextcloud/alice/files/a.txt', b'kept s?fe')  # bit rot on the USB drive
        with self.assertRaisesRegex(RuntimeError, 'damaged on the backup'):
            self.restore_cloud('1', 'yes', 'yes')

    def test_an_unreadable_or_odd_backup_entry_stops_the_restore(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'a')
        os.symlink('/etc/passwd', self.root / 'usb/nextcloud/alice/files/escape')
        with self.assertRaisesRegex(RuntimeError, 'could not be read'):
            self.restore_cloud('1', 'yes', 'no')
        self.assertFalse((self.media / 'restored').exists() and any((self.media / 'restored').iterdir()))

    def test_a_ledger_with_an_unsafe_path_is_not_trusted(self):
        from neko_common import read_ledger
        self.write('usb/nextcloud/.neko-ledger.json', json.dumps({'format': 'neko-ledger-v1', 'entries': {
            '../../etc/shadow': {'sha256': 'a' * 64, 'size': 1}}}).encode())
        with self.assertRaisesRegex(RuntimeError, 'not trusted'):
            read_ledger(self.root / 'usb/nextcloud')

    def test_repair_fixes_each_case_from_a_copy_that_matches_its_checksum(self):
        import hashlib as h
        good = {k: f'good {k}'.encode() for k in 'abcdef'}
        archive, usb = 'hdd/photos/archive', 'usb'
        for k in 'abcf':  # recorded checksums
            self.write(f'{archive}/upload/{k}.jpg', good[k])
            self.write(f'{usb}/upload/{k}.jpg', good[k])
        self.ledger(archive, {f'upload/{k}.jpg': good[k] for k in 'abcf'})
        self.ledger(usb, {f'upload/{k}.jpg': good[k] for k in 'abcf'})
        self.write(f'{usb}/upload/a.jpg', b'rotten a')                  # USB damaged
        self.write(f'{archive}/upload/b.jpg', b'rotten b')              # HDD damaged
        self.write(f'{archive}/upload/c.jpg', b'rot1 c')                # both damaged...
        self.write(f'{usb}/upload/c.jpg', b'rot2 c')
        self.write('ssd/photos/immich-library/upload/c.jpg', good['c'])  # ...Immich still has it
        self.write(f'{archive}/upload/d.jpg', b'hdd d')                 # no checksum, copies differ,
        self.write(f'{usb}/upload/d.jpg', good['d'])                     # the original decides
        self.write('ssd/photos/immich-library/upload/d.jpg', good['d'])
        self.write(f'{archive}/upload/e.jpg', b'hdd e')                 # no checksum, no original:
        self.write(f'{usb}/upload/e.jpg', good['e'])                     # you choose (USB)
        self.write(f'{archive}/upload/f.jpg', b'rot1 f')                # nothing good left
        self.write(f'{usb}/upload/f.jpg', b'rot2 f')
        keys = {'damaged': ['upload/a.jpg', 'upload/b.jpg'], 'both_damaged': ['upload/c.jpg', 'upload/f.jpg'],
                'disagree': ['upload/d.jpg', 'upload/e.jpg']}
        self.write('ssd/reports/integrity.json', json.dumps({'format': 'neko-integrity-v1', 'sections': {
            'usb_photos': {**keys, 'counts': {k: len(v) for k, v in keys.items()}}}}).encode())
        out = []
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.root / usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            recovery_wizard.repair_backups(self.config, answers('2'), out.append)
        for k in 'abcde':
            for where in (archive, usb):
                self.assertEqual((self.root / where / f'upload/{k}.jpg').read_bytes(), good[k], (k, where))
        self.assertEqual((self.root / archive / 'upload/f.jpg').read_bytes(), b'rot1 f')
        self.assertEqual((self.root / usb / 'upload/f.jpg').read_bytes(), b'rot2 f')
        text = '\n'.join(out)
        self.assertIn('upload/f.jpg: no copy matches its recorded checksum', text)
        # Damaged copies are kept aside, never deleted.
        aside = next((self.root / archive / 'neko-damaged').iterdir())
        self.assertEqual((aside / 'upload/b.jpg').read_bytes(), b'rotten b')
        # The chosen checksums were recorded; the recorded ones were never changed.
        from neko_common import read_ledger
        ledger = read_ledger(self.root / archive)
        self.assertEqual(ledger['upload/e.jpg'], h.sha256(good['e']).hexdigest())
        self.assertEqual(ledger['upload/b.jpg'], h.sha256(good['b']).hexdigest())
        # Only the file that could not be repaired is still listed for the dashboard.
        left = json.loads((self.root / 'ssd/reports/integrity.json').read_text())['sections']['usb_photos']
        self.assertEqual([left[k] for k in ('damaged', 'both_damaged', 'disagree')], [[], ['upload/f.jpg'], []])
        self.assertEqual(left['counts'], {'damaged': 0, 'both_damaged': 1, 'disagree': 0, 'missing': 0})

    def report(self, sections):
        self.write('ssd/reports/integrity.json', json.dumps({'format': 'neko-integrity-v1', 'sections': {
            name: {**lists, 'counts': {k: len(v) for k, v in lists.items()}} for name, lists in sections.items()}}).encode())

    def repair(self, usb, *choices):
        out = []
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            recovery_wizard.repair_backups(self.config, answers(*choices), out.append)
        return out, json.loads((self.root / 'ssd/reports/integrity.json').read_text())['sections']

    def test_a_usb_warning_stays_until_the_usb_copy_was_really_checked(self):
        archive = 'hdd/photos/archive'
        self.write(f'{archive}/upload/a.jpg', b'good a')
        self.ledger(archive, {'upload/a.jpg': b'good a'})
        self.report({'archive': {'damaged': ['upload/a.jpg']}, 'usb_photos': {'damaged': ['upload/a.jpg']}})
        out, left = self.repair(None)  # USB drive not plugged in
        self.assertIn('not plugged in', '\n'.join(out))
        self.assertEqual(left['archive']['damaged'], [])            # HDD copy checked: intact
        self.assertEqual(left['usb_photos']['damaged'], ['upload/a.jpg'])  # USB never checked

    def test_a_missing_archive_file_is_restored_from_the_usb_copy(self):
        archive, usb = 'hdd/photos/archive', 'usb'
        self.write(f'{usb}/upload/a.jpg', b'good a')
        self.ledger(archive, {'upload/a.jpg': b'good a'})
        self.ledger(usb, {'upload/a.jpg': b'good a'})
        self.report({'usb_photos': {'missing': ['upload/a.jpg']}})
        out, left = self.repair(self.root / usb)
        self.assertEqual((self.root / archive / 'upload/a.jpg').read_bytes(), b'good a')
        self.assertEqual(left['usb_photos']['missing'], [])

    def disagreeing(self):
        archive, usb = 'hdd/photos/archive', 'usb'
        self.write(f'{archive}/upload/a.jpg', b'hdd version')
        self.write(f'{usb}/upload/a.jpg', b'usb version')
        # Each copy matches its own ledger, and the ledgers disagree.
        self.ledger(archive, {'upload/a.jpg': b'hdd version'})
        self.ledger(usb, {'upload/a.jpg': b'usb version'})
        self.report({'usb_photos': {'disagree': ['upload/a.jpg']}})
        return self.root / archive, self.root / usb

    def test_ledgers_that_disagree_change_nothing_until_you_choose(self):
        hdd, usb = self.disagreeing()
        out, left = self.repair(usb, '3')  # Keep both as they are
        self.assertEqual((hdd / 'upload/a.jpg').read_bytes(), b'hdd version')
        self.assertEqual((usb / 'upload/a.jpg').read_bytes(), b'usb version')
        self.assertIn('both copies were kept', '\n'.join(out))
        self.assertEqual(left['usb_photos']['disagree'], ['upload/a.jpg'])
        self.assertNotEqual(recovery_wizard.read_ledger(hdd), recovery_wizard.read_ledger(usb))

    def test_the_copy_you_choose_wins_in_both_ledgers(self):
        import hashlib
        hdd, usb = self.disagreeing()
        out, left = self.repair(usb, '2')  # the USB copy
        self.assertIn('matches the USB list', '\n'.join(out))
        self.assertEqual((hdd / 'upload/a.jpg').read_bytes(), b'usb version')
        good = hashlib.sha256(b'usb version').hexdigest()
        # Both ledgers now agree with the files: neither copy fails its own check.
        self.assertEqual(recovery_wizard.read_ledger(hdd)['upload/a.jpg'], good)
        self.assertEqual(recovery_wizard.read_ledger(usb)['upload/a.jpg'], good)
        self.assertEqual(left['usb_photos']['disagree'], [])
        kept = list((hdd / 'neko-damaged').rglob('a.jpg'))
        self.assertEqual([k.read_bytes() for k in kept], [b'hdd version'])  # set aside, not deleted

    def test_a_restore_stops_when_a_verified_file_is_missing(self):
        self.write('usb/nextcloud/alice/files/a.txt', b'kept')
        self.ledger('usb/nextcloud', {'alice/files/a.txt': b'kept', 'alice/files/gone.txt': b'was here',
                                      'bob/files/other.txt': b'not restored'})
        with self.assertRaisesRegex(RuntimeError, 'alice/files/gone.txt'):
            self.restore_cloud('1', 'yes', 'yes')
        self.assertFalse((self.media / 'restored').exists() and any((self.media / 'restored').iterdir()))

    def test_a_dated_version_is_repaired_from_its_live_original(self):
        archive = 'hdd/photos/archive'
        dated = 'upload/photo.neko-20261020T010000Z.jpg'
        self.write(f'{archive}/{dated}', b'rotten')
        self.write(f'{archive}/upload/.photo.jpg.neko-latest', dated.split('/')[1].encode())
        self.ledger(archive, {dated: b'photo as it is now'})
        self.write('ssd/photos/immich-library/upload/photo.jpg', b'photo as it is now')
        self.report({'archive': {'damaged': [dated]}})
        out, left = self.repair(None)
        self.assertEqual((self.root / archive / dated).read_bytes(), b'photo as it is now')
        self.assertEqual(left['archive']['damaged'], [])
        self.assertEqual(recovery_wizard.live_name('a/b.neko-20261020T010000Z-2.jpg'), 'a/b.jpg')
        self.assertEqual(recovery_wizard.live_name('a/plain.jpg'), 'a/plain.jpg')

    def test_a_dated_version_never_takes_a_later_live_edit(self):
        archive = 'hdd/photos/archive'
        dated = 'upload/photo.neko-20261020T010000Z.jpg'
        self.write(f'{archive}/{dated}', b'old version')
        self.ledger(archive, {dated: b'old version'})
        self.write('ssd/photos/immich-library/upload/photo.jpg', b'edited since')
        self.write(f'{archive}/{dated}', b'rotten')
        self.report({'archive': {'damaged': [dated]}})
        out, left = self.repair(None)
        self.assertEqual((self.root / archive / dated).read_bytes(), b'rotten')  # kept untouched
        self.assertIn('no copy matches its recorded checksum', '\n'.join(out))
        self.assertEqual(left['archive']['damaged'], [dated])

    def test_repair_with_nothing_reported(self):
        out = []
        recovery_wizard.repair_backups(self.config, answers(), out.append)
        self.assertIn('nothing to repair', out[0])

    def test_old_usb_snapshots_are_kept_unless_you_type_delete(self):
        usb = self.root / 'usb'
        self.write('usb/backups/monthly/100-1/manifest.json', b'{"format":"neko-maintenance-v1"}')
        self.write('usb/backups/monthly/100-1/media.tar', b'old photos')
        self.write('usb/backups/monthly/200-1/manifest.json', b'{"format":"other"}')
        with mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=usb), \
                mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None):
            out = []
            recovery_wizard.review_old_usb(self.config, answers('no', 'delete'), out.append)
            self.assertTrue((usb / 'backups/monthly/100-1/media.tar').exists())
            self.assertIn('Kept. Nothing was removed.', out)
            # Copy to the HDD first, then remove only the old-format snapshot.
            recovery_wizard.review_old_usb(self.config, answers('yes', 'yes', 'DELETE'), out.append)
        self.assertFalse((usb / 'backups/monthly/100-1').exists())
        self.assertTrue((usb / 'backups/monthly/200-1').exists())
        kept = next((self.media / 'restored').iterdir())
        self.assertEqual((kept / '100-1/media.tar').read_bytes(), b'old photos')

    def test_a_password_list_from_a_snapshot_without_a_backup_key(self):
        import tarfile
        source = self.root / 'ssd'
        self.write('ssd/secrets/accounts/APP-CREDENTIALS.txt', b'jellyfin: admin / pw')
        self.write('ssd/appdata/jellyfin/config.xml', b'<x/>')
        run = self.media / 'backups/monthly/100-1'
        run.mkdir(parents=True)
        with tarfile.open(run / 'data.tar', 'w') as archive:
            for rel in ('appdata/jellyfin', 'secrets/accounts'):
                archive.add(source / rel, arcname=rel)
        import hashlib
        digest = hashlib.sha256((run / 'data.tar').read_bytes()).hexdigest()
        (run / 'manifest.json').write_text(json.dumps({'format': 'neko-maintenance-v1', 'password_files': 'unencrypted_in_data_tar',
                                                       'paths': {'data': ['appdata/jellyfin', 'secrets/accounts'], 'media': []},
                                                       'sha256': {'data': digest}}))
        # A changed archive is refused before anything is taken out of it.
        good = (run / 'data.tar').read_bytes()
        (run / 'data.tar').write_bytes(good.replace(b'admin / pw', b'admin / XX'))
        out = []
        recovery_wizard.restore_passwords(self.config, answers('1', 'no', str(self.root / 'bad'), 'yes'), out.append)
        self.assertIn('damaged (checksum mismatch)', '\n'.join(out))
        self.assertFalse((self.root / 'bad').exists())
        (run / 'data.tar').write_bytes(good)
        target = self.root / 'passwords'
        out = []
        recovery_wizard.restore_passwords(self.config, answers('1', 'no', str(target), 'yes'), out.append)
        self.assertIn('not encrypted', '\n'.join(out))
        self.assertEqual((target / 'secrets/accounts/APP-CREDENTIALS.txt').read_bytes(), b'jellyfin: admin / pw')
        self.assertFalse((target / 'appdata').exists())  # only the password list

    def test_nothing_found_is_explained(self):
        out = []
        recovery_wizard.restore_passwords(self.config, answers(), out.append)
        self.assertIn('No monthly snapshot', out[-1])

    def test_menu_back_and_needs_sudo(self):
        with mock.patch('os.geteuid', return_value=1000):
            self.assertEqual(recovery_wizard.run(answers(), lambda s: None), 2)
        with mock.patch('os.geteuid', return_value=0), \
                mock.patch.object(recovery_wizard, 'settings', return_value=self.config):
            out = []
            self.assertEqual(recovery_wizard.run(answers('0'), out.append), 0)
            self.assertTrue(any('NEW folder' in line for line in out))


if __name__ == '__main__':
    unittest.main()
