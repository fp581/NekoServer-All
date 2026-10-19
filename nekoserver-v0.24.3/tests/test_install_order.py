"""v0.23.1: install order, system update first, drive picking and weekly catch-up."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer  # noqa: E402
from modules import storage  # noqa: E402


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.full = self.config['profiles']['full']
        self.modules = installer.load_modules(ROOT / 'modules')

    def test_prerequisites_then_tailscale_then_the_rest(self):
        self.assertEqual(self.full[:3], ['system', 'tailscale', 'storage'])
        self.assertLess(self.full.index('gateway_tailnet'), self.full.index('gateway_lan'))

    def test_every_step_runs_after_what_it_needs(self):
        # The profile order is the order users see; nothing may be pulled forward.
        # (Helper steps a module needs, such as music_backup, may be added in between.)
        planned = installer.ordered(self.modules, self.full)
        self.assertEqual([name for name in planned if name in self.full], self.full)

    def test_system_update_and_app_packages_come_before_the_installer(self):
        script = (ROOT / 'install.sh').read_text()
        update = script.index('pacman -Syu --noconfirm')
        packages = script.index('app_packages=(')
        start = script.index('exec python3')
        self.assertLess(update, packages)
        self.assertLess(packages, start)
        self.assertIn('/usr/lib/modules/$(uname -r)', script)  # reboot after a new kernel
        for name in ('docker', 'docker-compose', 'tailscale', 'caddy', 'samba', 'qrencode'):
            self.assertIn(name, script[packages:start])

    def test_missed_weekly_backups_run_at_next_start(self):
        for name in ('photo_backup.py', 'photo_preservation.py'):
            text = (ROOT / 'modules' / name).read_text()
            self.assertNotIn('Persistent=false', text)


class DrivePickingTests(unittest.TestCase):
    INVENTORY = {'blockdevices': [
        {'name': 'nvme0n1', 'type': 'disk', 'children': [
            {'name': 'nvme0n1p2', 'type': 'part', 'uuid': 'root-uuid-0001', 'fstype': 'ext4', 'mountpoint': '/'}]},
        {'name': 'sdb', 'type': 'disk', 'model': 'SSD', 'children': [
            {'name': 'sdb1', 'type': 'part', 'uuid': 'ssd-uuid-0001', 'fstype': 'ext4',
             'mountpoint': '/run/media/neko/fast', 'size': '1T'}]},
        {'name': 'sdc', 'type': 'disk', 'model': 'HDD', 'children': [
            {'name': 'sdc1', 'type': 'part', 'uuid': 'hdd-uuid-0001', 'fstype': 'ext4',
             'mountpoint': None, 'size': '12T'}]},
    ]}

    def test_desktop_mounted_drive_can_be_chosen_by_its_name(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['1', 'sdb1', '1', '/dev/sdc1'])
        out = []
        storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                         input_fn=lambda q='': next(answers), out_fn=out.append)
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-uuid-0001')
        self.assertEqual(config['storage']['media_hdd']['uuid'], 'hdd-uuid-0001')
        self.assertFalse(any('1TB' in line or '12TB' in line for line in out))

    def test_wrong_confirmation_is_refused(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['1', 'sdc1'])
        with self.assertRaisesRegex(RuntimeError, 'confirmation failed'):
            storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                             input_fn=lambda q='': next(answers), out_fn=lambda s: None)


if __name__ == '__main__':
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """v0.23.2: findings from the external review."""

    def test_failed_space_check_blocks_everything_that_writes_to_the_drives(self):
        modules = installer.load_modules(ROOT / 'modules')

        def needs(name, target, seen=()):
            deps = modules[name]['deps']
            return target in deps or any(needs(d, target, seen + (name,)) for d in deps if d not in seen)

        for name in ('docker', 'media', 'photos', 'nextcloud', 'maintenance', 'app_backup',
                     'host_backup', 'photo_backup', 'photo_preservation', 'server_os'):
            self.assertTrue(needs(name, 'capacity'), name)
        # Tailscale does not touch the drives and still runs first.
        self.assertFalse(needs('tailscale', 'capacity'))

    def test_seerr_folder_is_handed_to_the_configured_user(self):
        import tempfile
        from unittest import mock
        from modules import media
        with tempfile.TemporaryDirectory() as folder:
            seerr = Path(folder) / 'seerr'
            (seerr / 'db').mkdir(parents=True)
            (seerr / 'settings.json').write_text('{}')
            owners = {str(seerr): (1000, 1000)}
            calls = []

            class Stat:
                def __init__(self, uid, gid):
                    self.st_uid, self.st_gid = uid, gid

            def lstat(path):
                return Stat(*owners.get(str(path), (1000, 1000)))

            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)), \
                    mock.patch.object(media.os, 'chown', lambda p, u, g, follow_symlinks=True: calls.append((str(p), u, g, follow_symlinks))):
                self.assertTrue(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))
            self.assertIn((str(seerr), 2000, 2000, False), calls)
            self.assertIn((str(seerr / 'settings.json'), 2000, 2000, False), calls)
            self.assertIn((str(seerr / 'db'), 2000, 2000, False), calls)
            # Already right, or owned by someone else entirely: left alone.
            owners[str(seerr)] = (2000, 2000)
            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)):
                self.assertFalse(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))
            owners[str(seerr)] = (3000, 3000)
            with mock.patch.object(Path, 'lstat', lambda self: lstat(self)):
                self.assertFalse(media.adopt_seerr_folder(seerr, 2000, 2000, lambda s: None))

    def test_nightly_backup_settings_check_matches_what_setup_writes(self):
        from modules import app_backup
        config = json.loads((ROOT / 'config.json').read_text())
        guard = app_backup.guard_config(config)
        self.assertIn('keep_snapshots', guard['app_backup'])
        self.assertIn('app_backup.guard_config(config)', (ROOT / 'native_runtime.py').read_text())


class BusyLockTests(unittest.TestCase):
    def test_installer_waits_for_a_running_backup_then_explains(self):
        import fcntl
        import tempfile
        import recovery_tools
        with tempfile.NamedTemporaryFile() as f, open(f.name, 'a') as holder, open(f.name, 'a') as mine:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, 'backup or monthly update is running'):
                recovery_tools.wait_for(mine, False)
            with self.assertRaisesRegex(RuntimeError, 'run  sudo bash install.sh  again'):
                recovery_tools.wait_for(mine, True, limit=10, sleep=lambda s: None)
            tries = []

            def finish(seconds):
                tries.append(seconds)
                fcntl.flock(holder, fcntl.LOCK_UN)
            recovery_tools.wait_for(mine, True, sleep=finish)
            self.assertEqual(len(tries), 1)


class SeerrPermissionCheckTests(unittest.TestCase):
    def test_seerr_is_checked_as_the_configured_user(self):
        from unittest import mock
        import permission_checks
        config = {'data_mount': '/ssd', 'media_mount': '/hdd',
                  'media': {'uid': 2000, 'gid': 2000, 'services': ['seerr', 'sonarr']}}
        seen = {}

        def access(path, uid, groups, mask, bind_root=False):
            seen[str(path)] = (uid, groups)
            return True
        with mock.patch.object(permission_checks, 'access', access):
            rows = permission_checks.check(config, ['media'])
        self.assertEqual(seen['/ssd/appdata/seerr'], (2000, {2000}))
        self.assertEqual(seen['/ssd/appdata/sonarr'], (2000, {2000}))
        self.assertTrue(all(r['status'] == 'pass' for r in rows))


class DashboardOverTailscaleTests(unittest.TestCase):
    def test_route_is_added_and_older_journals_still_recognised(self):
        import gateway_tools as g
        board = g.dashboard_target({})
        self.assertEqual(board, 'http://127.0.0.1:8787')
        self.assertIsNone(g.dashboard_target({'dashboard': {'remote': False}}))
        host = 'neko.tail1234.ts.net'
        with_board = g.desired(host, ['jellyfin'], board)
        self.assertEqual(with_board['Web'][host + ':8448']['Handlers']['/']['Proxy'], board)
        self.assertTrue(g.gateway_owned(host, with_board, board))
        # A journal from before the dashboard route (apps only) is still ours.
        self.assertTrue(g.gateway_owned(host, g.desired(host, ['jellyfin']), board))
        # Anything else on 8448 is not.
        odd = g.desired(host, ['jellyfin'], 'http://127.0.0.1:9999')
        self.assertFalse(g.gateway_owned(host, odd, board))

    def test_phone_steps_are_not_repeated_for_the_new_dashboard_link(self):
        from modules import gateway_tailnet as t
        earlier = {'jellyfin': 'https://n.ts.net:8443/'}
        self.assertTrue(t.phone_done(earlier, {**earlier, 'dashboard': 'https://n.ts.net:8448/'}))
        self.assertFalse(t.phone_done(earlier, {'jellyfin': 'https://other.ts.net:8443/'}))
        self.assertFalse(t.phone_done(earlier, {**earlier, 'immich': 'https://n.ts.net:8445/'}))
        self.assertFalse(t.phone_done(None, earlier))


class HardwareVideoTests(unittest.TestCase):
    def test_chip_choice(self):
        import gpu
        self.assertEqual(gpu.video_choice({'amd': '/dev/dri/renderD129', 'intel': '/dev/dri/renderD128'}),
                         ('intel', '/dev/dri/renderD128', 'qsv', 'qsv'))
        self.assertEqual(gpu.video_choice({'amd': '/dev/dri/renderD128'})[2:], ('vaapi', 'vaapi'))
        self.assertIsNone(gpu.video_choice({'nvidia': '/dev/dri/renderD128'}))

    def test_render_nodes_read_the_chip_vendor(self):
        import tempfile
        from unittest import mock
        import gpu
        with tempfile.TemporaryDirectory() as folder:
            node = Path(folder) / 'renderD128/device'
            node.mkdir(parents=True)
            (node / 'vendor').write_text('0x8086\n')
            with mock.patch.object(gpu.Path, 'exists', return_value=True):
                self.assertEqual(gpu.render_nodes(Path(folder)), {'intel': '/dev/dri/renderD128'})

    def test_apps_are_switched_on_but_a_choice_you_made_is_kept(self):
        from modules import hardware_video as hv

        class Fake:
            def __init__(self, encoding, ffmpeg):
                self.headers = {'Authorization': 'MediaBrowser Client="x"'}
                self.encoding, self.ffmpeg, self.saved = encoding, ffmpeg, {}

            def call(self, endpoint, method='GET', payload=None):
                if endpoint == '/Users/AuthenticateByName':
                    return {'AccessToken': 't', 'User': {'Policy': {'IsAdministrator': True}}}
                if endpoint == '/api/auth/login':
                    return {'accessToken': 't', 'isAdmin': True}
                if endpoint == '/System/Configuration/encoding':
                    if method == 'POST':
                        self.saved['jellyfin'] = payload
                    return dict(self.encoding)
                if endpoint == '/api/system-config':
                    if method == 'PUT':
                        self.saved['immich'] = payload
                    return {'ffmpeg': dict(self.ffmpeg)}
                return {}
        entry = {'username': 'admin', 'password': 'pw'}
        fake = Fake({'HardwareAccelerationType': 'none'}, {'accel': 'disabled'})
        self.assertEqual(hv.configure_jellyfin(entry, 'qsv', '/dev/dri/renderD128', fake), 'on (qsv)')
        self.assertEqual(fake.saved['jellyfin']['VaapiDevice'], '/dev/dri/renderD128')
        self.assertTrue(fake.saved['jellyfin']['EnableHardwareEncoding'])
        self.assertEqual(hv.configure_immich(entry, 'qsv', fake), 'on (qsv)')
        self.assertEqual(fake.saved['immich']['ffmpeg'], {'accel': 'qsv', 'accelDecode': True})
        mine = Fake({'HardwareAccelerationType': 'vaapi'}, {'accel': 'vaapi'})
        self.assertIn('kept your setting', hv.configure_jellyfin(entry, 'qsv', '/dev/dri/renderD128', mine))
        self.assertIn('kept your setting', hv.configure_immich(entry, 'qsv', mine))
        self.assertEqual(mine.saved, {})

    def test_decode_check_runs_inside_jellyfin(self):
        from unittest import mock
        from modules import hardware_video as hv
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ['docker', 'ps']:
                return mock.Mock(stdout='abc123\n', returncode=0)
            return mock.Mock(stdout='VAProfileH264Main : VAEntrypointVLD\n', returncode=0)
        self.assertTrue(hv.jellyfin_decodes('/dev/dri/renderD128', run))
        self.assertIn('/usr/lib/jellyfin-ffmpeg/vainfo', calls[-1])


class UsbPickerTests(unittest.TestCase):
    INVENTORY = {'blockdevices': [
        {'name': 'nvme0n1', 'type': 'disk', 'children': [
            {'name': 'nvme0n1p2', 'type': 'part', 'uuid': 'root-0001', 'fstype': 'ext4', 'mountpoint': '/'},
            {'name': 'nvme0n1p3', 'type': 'part', 'uuid': 'spare-0001', 'fstype': 'ext4', 'mountpoint': None}]},
        {'name': 'sdb', 'type': 'disk', 'children': [
            {'name': 'sdb1', 'type': 'part', 'uuid': 'hdd-uuid-0001', 'fstype': 'ext4', 'mountpoint': '/mnt/nekoserver-media'},
            {'name': 'sdb2', 'type': 'part', 'uuid': 'hdd-second-0001', 'fstype': 'exfat', 'mountpoint': None}]},
        {'name': 'sdc', 'type': 'disk', 'children': [
            {'name': 'sdc1', 'type': 'part', 'uuid': 'usb-uuid-0001', 'fstype': 'exfat', 'mountpoint': '/run/media/neko/BACKUP', 'size': '1T'}]},
    ]}
    CONFIG = {'external_backup_mount': '/mnt/nekoserver-photos',
              'storage': {'data_ssd': {'uuid': 'ssd-uuid-0001'}, 'media_hdd': {'uuid': 'hdd-uuid-0001'}}}

    def test_only_a_separate_physical_disk_is_offered(self):
        from modules import photo_backup
        import copy
        shown = []
        config = copy.deepcopy(self.CONFIG)
        answers = iter(['1', 'sdc1'])
        photo_backup.select_external(config, self.INVENTORY, input_fn=lambda q='': next(answers), out_fn=shown.append)
        self.assertEqual(config['photos']['external_backup_uuid'], 'usb-uuid-0001')
        offered = '\n'.join(shown)
        self.assertNotIn('sdb2', offered)       # second partition of the media HDD
        self.assertNotIn('nvme0n1p3', offered)  # spare partition of the system disk

    def test_a_saved_drive_on_the_hdd_is_refused_too(self):
        # Saved settings skip the picker; the same disk check still applies.
        from modules import photo_backup
        import copy
        self.assertIn('/dev/sdb', photo_backup.shared_disk(self.CONFIG, self.INVENTORY, 'hdd-second-0001'))
        self.assertIn('/dev/nvme0n1', photo_backup.shared_disk(self.CONFIG, self.INVENTORY, 'spare-0001'))
        self.assertIsNone(photo_backup.shared_disk(self.CONFIG, self.INVENTORY, 'usb-uuid-0001'))
        self.assertIsNone(photo_backup.shared_disk(self.CONFIG, self.INVENTORY, 'not-plugged-in'))
        config = copy.deepcopy(self.CONFIG)
        config['photos'] = {'external_backup_uuid': 'hdd-second-0001', 'external_backup_fstype': 'exfat'}

        class Ctx:
            apply = True

            def __init__(self):
                self.config = config
        with mock.patch.object(photo_backup.os, 'geteuid', return_value=0), \
                mock.patch.object(storage, 'inventory_now', return_value=self.INVENTORY), \
                mock.patch.object(photo_backup, 'atomic_write') as written:
            with self.assertRaisesRegex(RuntimeError, 'separate drive'):
                photo_backup.run(Ctx())
        written.assert_not_called()  # no weekly timer for that partition

    def test_a_second_partition_of_the_hdd_is_refused(self):
        from modules import photo_backup
        import copy
        only_hdd = {'blockdevices': self.INVENTORY['blockdevices'][:2]}
        with self.assertRaisesRegex(RuntimeError, 'drive of its own'):
            photo_backup.select_external(copy.deepcopy(self.CONFIG), only_hdd, input_fn=lambda q='': '1', out_fn=lambda s: None)
