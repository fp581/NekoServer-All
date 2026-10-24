"""v0.23.1: install order, system update first, drive picking and weekly catch-up."""
import json
import os
import tempfile
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
        refresh = script.index('apt_wait[@]}" update')
        packages = script.index('host.py" bootstrap')
        start = script.index('exec python3')
        self.assertLess(update, packages)
        self.assertLess(refresh, packages)
        self.assertLess(packages, start)
        self.assertIn('/usr/lib/modules/$(uname -r)', script)  # reboot after a new kernel
        self.assertIn('/run/reboot-required', script)  # Debian/Ubuntu: restart before changes
        import host
        for name in ('docker', 'docker-compose', 'tailscale', 'caddy', 'samba', 'qrencode'):
            self.assertIn(name, host.APP_PACKAGES)

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
        answers = iter(['1', 'sdc1', 'sdc1', 'sdc1'])   # another drive's name, three times
        with self.assertRaisesRegex(RuntimeError, 'confirmation failed'):
            storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                             input_fn=lambda q='': next(answers), out_fn=lambda s: None)
        self.assertNotIn('uuid', config['storage']['data_ssd'])

    def test_a_typo_is_asked_again(self):
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['9', '1', 'sbd1', 'sdb1', 'x', '1', 'sdc1'])
        out = []
        storage.resolve_storage_identity(config, self.INVENTORY, apply=True, interactive=True,
                                         input_fn=lambda q='': next(answers), out_fn=out.append)
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-uuid-0001')
        self.assertEqual(config['storage']['media_hdd']['uuid'], 'hdd-uuid-0001')
        self.assertIn('That is not sdb1. Type sdb1 exactly as shown, to be sure of the drive.', out)

    def test_nothing_eligible_says_why_drive_by_drive(self):
        inventory = {'blockdevices': [
            {'name': 'nvme0n1', 'type': 'disk', 'children': [
                {'name': 'nvme0n1p1', 'type': 'part', 'uuid': 'efi-0001', 'fstype': 'vfat', 'mountpoint': '/efi'},
                {'name': 'nvme0n1p2', 'type': 'part', 'uuid': 'root-0001', 'fstype': 'ext4', 'mountpoint': '/'}]},
            {'name': 'sdb', 'type': 'disk', 'children': [
                {'name': 'sdb1', 'type': 'part', 'uuid': 'ssd-0001', 'fstype': 'ext4', 'mountpoint': None}]},
            {'name': 'sdc', 'type': 'disk', 'children': [
                {'name': 'sdc1', 'type': 'part', 'uuid': 'win-0001', 'fstype': 'ntfs', 'mountpoint': None}]},
            {'name': 'sdd', 'type': 'disk', 'children': [
                {'name': 'sdd1', 'type': 'part', 'uuid': 'old-0001', 'fstype': 'ext4', 'mountpoint': '/mnt/films'}]},
        ]}
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        answers = iter(['1', 'sdb1'])
        with self.assertRaises(RuntimeError) as caught:
            storage.resolve_storage_identity(config, inventory, apply=True, interactive=True,
                                             input_fn=lambda q='': next(answers), out_fn=lambda s: None)
        message = str(caught.exception)
        self.assertIn('/dev/sdc1 is formatted NTFS (Windows)', message)
        self.assertIn('/dev/sdd1 is already in use at /mnt/films', message)
        self.assertIn('sudo umount /mnt/films', message)
        self.assertNotIn('nvme0n1p1', message)   # the system disk's boot partition
        self.assertNotIn('/dev/sdb1', message)   # already chosen for the SSD

    def test_a_new_unformatted_drive_is_named_not_called_missing(self):
        inventory = {'blockdevices': [
            {'name': 'nvme0n1', 'type': 'disk', 'size': '500G', 'children': [
                {'name': 'nvme0n1p1', 'type': 'part', 'uuid': 'root-0001', 'fstype': 'ext4', 'mountpoint': '/'}]},
            {'name': 'sdb', 'type': 'disk', 'size': '2T', 'model': 'WDC WD20EZAZ', 'fstype': None, 'mountpoint': None},
            # Not drives to format: swap in RAM, an eMMC boot area, an empty slot, and a
            # partition without a filesystem (Windows keeps such reserved partitions).
            {'name': 'zram0', 'type': 'disk', 'size': '4G', 'mountpoint': '[SWAP]'},
            {'name': 'mmcblk0boot0', 'type': 'disk', 'size': '4M'},
            {'name': 'nbd0', 'type': 'disk', 'size': '0B'},
            {'name': 'sdc', 'type': 'disk', 'size': '1T', 'children': [
                {'name': 'sdc1', 'type': 'part', 'size': '16M', 'fstype': None}]},
        ]}
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media'}
        with self.assertRaises(RuntimeError) as caught:
            storage.resolve_storage_identity(config, inventory, apply=True, interactive=True,
                                             input_fn=lambda q='': '', out_fn=lambda s: None)
        message = str(caught.exception)
        self.assertIn('/dev/sdb (2T) has no filesystem yet. To use it, format it as ext4', message)
        self.assertNotIn('No other drive was found', message)
        for name in ('zram0', 'mmcblk0boot0', 'nbd0', 'sdc'):
            self.assertNotIn('/dev/' + name, message)


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
    def setUp(self):
        # A dashboard is installed (the route is offered only then).
        import gateway_tools
        from unittest import mock
        unit = tempfile.NamedTemporaryFile(suffix='.service')
        self.addCleanup(unit.close)
        patcher = mock.patch.object(gateway_tools, 'DASHBOARD_UNIT', Path(unit.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dashboard_that_is_not_installed_is_not_offered(self):
        import gateway_tools as g
        with mock.patch.object(g, 'DASHBOARD_UNIT', Path('/nonexistent/nekoserver-dashboard.service')):
            self.assertIsNone(g.dashboard_target({}))
            self.assertNotIn(str(g.DASHBOARD_PORT), g.desired('n.ts.net', ['jellyfin'], g.dashboard_target({}))['TCP'])

    def test_phone_setup_runs_after_every_service_it_links_to(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        for service in ('dashboard', 'media', 'photos', 'nextcloud', 'music', 'tv'):
            self.assertLess(full.index(service), full.index('gateway_tailnet'), service)

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
        self.assertEqual(gpu.video_choices([('amd', '/dev/dri/renderD129'), ('intel', '/dev/dri/renderD128')]),
                         [('intel', '/dev/dri/renderD128', 'qsv', 'qsv'), ('amd', '/dev/dri/renderD129', 'vaapi', 'vaapi')])
        self.assertEqual(gpu.video_choices([('amd', '/dev/dri/renderD128')])[0][2:], ('vaapi', 'vaapi'))
        self.assertEqual(gpu.video_choices([('nvidia', '/dev/dri/renderD128')]), [])
        # Two chips of one vendor: both are tried, in device order.
        self.assertEqual([c[1] for c in gpu.video_choices([('intel', '/dev/dri/renderD128'), ('intel', '/dev/dri/renderD129')])],
                         ['/dev/dri/renderD128', '/dev/dri/renderD129'])

    def test_the_kernel_names_the_chip_when_lspci_is_missing(self):
        import gpu
        with tempfile.TemporaryDirectory() as folder:
            card = Path(folder) / 'card0/device'
            card.mkdir(parents=True)
            (card / 'vendor').write_text('0x10de\n')
            self.assertEqual(gpu.graphics('', Path(folder)), ['nvidia'])
            self.assertEqual(gpu.graphics('', Path(folder) / 'none'), [])

    def test_a_second_chip_is_tried_when_the_preferred_one_fails(self):
        from modules import hardware_video as hv
        import account_setup
        import account_store
        intel = ('intel', '/dev/dri/renderD128', 'qsv', 'qsv')
        amd = ('amd', '/dev/dri/renderD129', 'vaapi', 'vaapi')
        probed, set_to = [], {}

        def check(app, node):
            probed.append((app, node))
            return node == amd[1]          # Intel fails the check, AMD passes

        def jellyfin(entry, method, node, client, ours=False, several=False):
            set_to['jellyfin'] = (method, node)
            return 'on (' + method + ')'
        client = mock.Mock()
        with mock.patch.object(account_store, 'enabled', return_value=True), \
                mock.patch.object(account_store, 'get', return_value={'username': 'a', 'password': 'b'}), \
                mock.patch.object(account_setup, 'wait'), \
                mock.patch.object(hv, 'configure_jellyfin', jellyfin):
            done = hv.apps_on({}, [intel, amd], lambda line: None, client_factory=lambda app: client,
                              apps=['jellyfin'], check=check)
        self.assertEqual(probed, [('jellyfin', intel[1]), ('jellyfin', amd[1])])
        self.assertEqual(set_to['jellyfin'], ('vaapi', amd[1]))
        self.assertEqual(done, {'jellyfin': 'on (vaapi)'})
        # No chip passes: software, without trying to log in.
        done = hv.apps_on({}, [intel], lambda line: None, client_factory=None, apps=['jellyfin'],
                          check=lambda app, node: False)
        self.assertEqual(done, {'jellyfin': hv.SOFTWARE})

    def test_render_nodes_read_the_chip_vendor(self):
        import tempfile
        from unittest import mock
        import gpu
        with tempfile.TemporaryDirectory() as folder:
            for name in ('renderD128', 'renderD129'):
                node = Path(folder) / name / 'device'
                node.mkdir(parents=True)
                (node / 'vendor').write_text('0x8086\n')
            with mock.patch.object(gpu.Path, 'exists', return_value=True):
                self.assertEqual(gpu.render_nodes(Path(folder)),
                                 [('intel', '/dev/dri/renderD128'), ('intel', '/dev/dri/renderD129')])

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
        self.assertEqual(hv.configure_jellyfin(entry, 'qsv', '/dev/dri/renderD129', fake), 'on (qsv)')
        # Quick Sync's own device setting, not VA-API's.
        self.assertEqual(fake.saved['jellyfin']['QsvDevice'], '/dev/dri/renderD129')
        self.assertNotIn('VaapiDevice', fake.saved['jellyfin'])
        hv.configure_jellyfin(entry, 'vaapi', '/dev/dri/renderD128', fake)
        self.assertEqual(fake.saved['jellyfin']['VaapiDevice'], '/dev/dri/renderD128')
        self.assertTrue(fake.saved['jellyfin']['EnableHardwareEncoding'])
        # The chip that passed the check, not whichever Immich would pick by itself.
        self.assertEqual(hv.configure_immich(entry, 'vaapi', '/dev/dri/renderD129', fake), 'on (vaapi)')
        self.assertEqual(fake.saved['immich']['ffmpeg'], {'accel': 'vaapi', 'accelDecode': True, 'preferredHwDevice': '/dev/dri/renderD129'})
        second = Fake({'HardwareAccelerationType': 'none'}, {'accel': 'disabled', 'preferredHwDevice': 'auto'})
        hv.configure_app('immich', ('AMD', '/dev/dri/renderD129', 'vaapi', 'vaapi'), entry, second)
        self.assertEqual(second.saved['immich']['ffmpeg']['preferredHwDevice'], '/dev/dri/renderD129')
        mine = Fake({'HardwareAccelerationType': 'vaapi'}, {'accel': 'vaapi'})
        self.assertIn('kept your setting', hv.configure_jellyfin(entry, 'qsv', '/dev/dri/renderD128', mine))
        self.assertIn('kept your setting', hv.configure_immich(entry, 'qsv', '/dev/dri/renderD128', mine))
        self.assertEqual(mine.saved, {})

    def test_a_setting_an_older_release_made_gets_its_device(self):
        from modules import hardware_video as hv
        fake = mock.Mock()
        fake.headers = {}
        saved = {}

        def call(endpoint, method='GET', payload=None):
            if endpoint == '/api/auth/login':
                return {'accessToken': 't', 'isAdmin': True}
            if endpoint == '/api/system-config':
                if method == 'PUT':
                    saved['immich'] = payload
                return {'ffmpeg': {'accel': 'qsv', 'accelDecode': True, 'preferredHwDevice': 'auto'}}
            return {}
        fake.call = call
        entry = {'username': 'admin', 'password': 'pw'}
        chip = ('intel', '/dev/dri/renderD129', 'qsv', 'qsv')
        # NekoServer turned it on before ("on (qsv)" in its last report): finished now.
        self.assertEqual(hv.configure_app('immich', chip, entry, fake, before='on (qsv)', several=True), 'on (qsv)')
        self.assertEqual(saved['immich']['ffmpeg']['preferredHwDevice'], '/dev/dri/renderD129')
        saved.clear()
        # Set by someone else, several chips: not reported as done, nothing changed.
        result = hv.configure_app('immich', chip, entry, fake, before=None, several=True)
        self.assertIn('choose /dev/dri/renderD129', result)
        self.assertFalse(result.startswith(('on', 'kept')))
        self.assertEqual(saved, {})
        # One chip only: their setting can't pick a wrong one.
        self.assertIn('kept your setting', hv.configure_app('immich', chip, entry, fake, before=None, several=False))

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


class HardwareVideoAppsTests(unittest.TestCase):
    """Only the apps this server runs are checked, switched over or waited for."""
    CHOICE = ('intel', '/dev/dri/renderD128', 'qsv', 'qsv')

    def run_module(self, config, chip_works):
        from modules import hardware_video as hv

        class Ctx:
            apply = True
            logs = []

            def log(self, m):
                self.logs.append(m)
        ctx = Ctx()
        with tempfile.TemporaryDirectory() as data:
            ctx.config = {'data_mount': data, **config}
            checked, configured, chipped = [], [], []

            def give_chip(*_):
                chipped.append('immich')

            def apps_on(cfg, choice, log, apps=None, before=None):
                done = {}
                for app in apps:
                    checked.append(app)
                    done[app] = 'on (qsv)' if chip_works.get(app) else hv.SOFTWARE
                    if chip_works.get(app):
                        configured.append(app)
                return done
            with mock.patch.object(hv.gpu, 'graphics', return_value=['intel']), \
                    mock.patch.object(hv.gpu, 'video_choices', return_value=[self.CHOICE]), \
                    mock.patch.object(hv.gpu, 'render_nodes', return_value=[]), \
                    mock.patch.object(hv, 'install_drivers'), \
                    mock.patch.object(hv, 'apps_on', apps_on), \
                    mock.patch.object(hv, 'give_immich_the_chip', side_effect=give_chip), \
                    mock.patch('recovery_tools.mounts'), \
                    mock.patch('recovery_tools.lock', return_value=mock.MagicMock()), \
                    mock.patch('neko_common.verify_storage'):
                result = hv.run(ctx)
                report = json.loads((Path(data) / 'reports/hardware-video.json').read_text())
        return result, checked, configured, chipped, report

    def test_immich_alone_is_checked_and_switched_over_without_jellyfin(self):
        result, checked, configured, chipped, report = self.run_module(
            {'media': {'services': []}, 'photos': {'deploy_immich': True}}, {'immich': True})
        self.assertIsNone(result)
        self.assertEqual((checked, configured, chipped), (['immich'], ['immich'], ['immich']))
        self.assertEqual(report['status'], 'hardware')

    def test_jellyfin_alone_never_asks_for_immich(self):
        result, checked, configured, chipped, report = self.run_module(
            {'media': {'services': ['jellyfin']}, 'photos': {'deploy_immich': False}}, {'jellyfin': True})
        self.assertIsNone(result)
        self.assertEqual((checked, chipped), (['jellyfin'], []))
        self.assertEqual(list(report['apps']), ['jellyfin'])

    def test_a_chip_that_fails_one_apps_check_leaves_that_app_in_software(self):
        result, checked, configured, _, report = self.run_module(
            {'media': {'services': ['jellyfin']}, 'photos': {'deploy_immich': True}}, {'immich': True})
        self.assertIsNone(result)   # a failed check is an answer, not a step left to do
        self.assertEqual((checked, configured), (['jellyfin', 'immich'], ['immich']))
        self.assertIn('software', report['apps']['jellyfin'])

    def test_neither_app_means_nothing_to_do(self):
        from modules import hardware_video as hv
        self.assertEqual(hv.selected_apps({'media': {'services': ['sonarr']}}), [])
        ctx = mock.Mock(apply=True, config={'media': {'services': []}})
        with mock.patch.object(hv.gpu, 'graphics') as graphics:
            self.assertIsNone(hv.run(ctx))
        graphics.assert_not_called()

    def test_each_app_is_checked_inside_its_own_container(self):
        from modules import hardware_video as hv
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if argv[:2] == ['docker', 'ps']:
                return mock.Mock(stdout='abc123\n', returncode=0)
            return mock.Mock(stdout='VAProfileH264Main : VAEntrypointVLD\n', returncode=0)
        self.assertTrue(hv.decodes('immich', '/dev/dri/renderD128', run))
        self.assertIn('label=com.docker.compose.service=immich-server', calls[0])


class HardwareVideoLockTests(unittest.TestCase):
    def test_immich_is_not_touched_while_a_backup_holds_the_lock(self):
        import fcntl
        import tempfile
        import recovery_tools
        from modules import hardware_video as hv

        class Ctx:
            def __init__(self, data):
                self.config, self.logs = {'data_mount': data, 'photos': {'deploy_immich': True}}, []

            def log(self, m):
                self.logs.append(m)
        with tempfile.TemporaryDirectory() as data:
            calls = []
            lock_file = Path(data) / 'installer/recovery.lock'
            lock_file.parent.mkdir(parents=True)
            def give_chip(*_):
                calls.append('immich')

            def apps_on(*_, **__):
                calls.append('apps')
                return {}
            # The test's own record of an unfinished monthly update (none), never this PC's.
            with mock.patch.object(recovery_tools.neko_common, 'MAINTENANCE_JOURNAL', Path(data) / 'no-journal.json'), \
                    mock.patch.object(recovery_tools, 'mounts'), \
                    mock.patch.object(hv, 'give_immich_the_chip', side_effect=give_chip), \
                    mock.patch.object(hv, 'apps_on', side_effect=apps_on), \
                    mock.patch.object(recovery_tools.sys.stdin, 'isatty', return_value=False):
                with lock_file.open('a') as held:  # a backup or monthly update is running
                    fcntl.flock(held, fcntl.LOCK_EX)
                    with self.assertRaisesRegex(RuntimeError, 'running right now'):
                        hv.switch_apps(Ctx(data), None, check_storage=lambda c: None)
                    self.assertEqual(calls, [])  # no settings written, nothing restarted
                hv.switch_apps(Ctx(data), None, check_storage=lambda c: calls.append('storage'))
            self.assertEqual(calls, ['storage', 'immich', 'apps'])


class ManualSnapshotTests(unittest.TestCase):
    """The manual Immich snapshot (recovery_tools): nothing silently left out, and no
    copy of an app that was killed instead of stopped."""

    def broken_listing(self, bad):
        import neko_common
        real = neko_common.os.walk

        def walk(top, onerror=None, **kw):
            for folder, dirs, files in real(top, **kw):
                if Path(folder) == bad:
                    onerror(PermissionError(13, 'Permission denied', str(bad)))
                    dirs[:] = []
                    continue
                yield folder, dirs, files
        return mock.patch.object(neko_common.os, 'walk', walk)

    def test_an_unreadable_album_stops_the_snapshot(self):
        import recovery_tools as rt
        import neko_common
        with tempfile.TemporaryDirectory() as d:
            album = Path(d) / 'library/album'
            album.mkdir(parents=True)
            (album / 'photo.jpg').write_bytes(b'photo')
            (Path(d) / 'library/other.jpg').write_bytes(b'other')
            self.assertEqual(len(rt.regular_files(Path(d) / 'library')), 2)
            with self.broken_listing(album):
                with self.assertRaises(neko_common.UnreadableEntries) as caught:
                    rt.regular_files(Path(d) / 'library')
            self.assertIn('album', str(caught.exception))
            # A snapshot whose listing fails keeps its .incomplete marker (not restorable).
            snap = Path(d) / 'snap'
            snap.mkdir()
            (snap / '.incomplete').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, 'Incomplete snapshot'):
                rt.verify_snapshot(snap)

    def test_a_killed_app_is_not_snapshotted(self):
        import recovery_tools as rt
        states = []

        def command(argv, timeout=60, stdin=None, stdout=None):
            if 'inspect' in argv:
                return json.dumps(states)
            return 'abc123\n'
        with mock.patch.object(rt, 'command', command):
            states[:] = [{'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False}}]
            rt.stopped_cleanly(['docker', 'compose'], ['immich-server'])
            for bad, why in (({'Running': True}, 'still running'), ({'Running': False, 'ExitCode': 137}, 'killed'),
                             ({'Running': False, 'ExitCode': 0, 'OOMKilled': True}, 'killed')):
                states[:] = [{'State': bad}]
                with self.assertRaisesRegex(RuntimeError, why):
                    rt.stopped_cleanly(['docker', 'compose'], ['immich-server'])

        def no_container(argv, timeout=60, stdin=None, stdout=None):
            return ''
        with mock.patch.object(rt, 'command', no_container):
            with self.assertRaisesRegex(RuntimeError, 'no container'):
                rt.stopped_cleanly(['docker', 'compose'], ['immich-server'])


class SnapshotUnderLockTests(unittest.TestCase):
    def test_the_snapshot_records_the_version_that_made_the_database(self):
        # A monthly update finishes (v1.0.0 -> v2.0.0) while the snapshot waits for the
        # lock. What it records must be read after that, not before.
        import tempfile
        from contextlib import contextmanager
        import recovery_tools as rt
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / 'photos/immich-library').mkdir(parents=True)
            (data / 'appdata/immich/postgres').mkdir(parents=True)
            env = data / 'compose/immich/.env'
            env.parent.mkdir(parents=True)
            env.write_text('IMMICH_VERSION=v1.0.0\n')
            image = ['immich-server@sha256:old']

            @contextmanager
            def upgrade_then_lock(config):
                env.write_text('IMMICH_VERSION=v2.0.0\n')  # the update completed first
                image[0] = 'immich-server@sha256:new'
                yield

            def command(argv, timeout=60, stdin=None, stdout=None):
                if 'inspect' in argv:  # stopped cleanly
                    return json.dumps([{'State': {'Running': False, 'ExitCode': 0, 'OOMKilled': False}}])
                if 'ps' in argv:
                    return 'database\nimmich-server\n'
                if 'exec' in argv:
                    stdout.write(b'-- dump made by the database of v2.0.0\n' * 5 + b'-- PostgreSQL database dump complete\n')
                return ''
            destination = data / 'snapshot'
            with mock.patch.multiple(rt, mounts=mock.DEFAULT, validate_model=mock.DEFAULT,
                                     enough_space=mock.DEFAULT, require_root=mock.DEFAULT,
                                     compose_args=mock.Mock(return_value=['docker', 'compose']),
                                     json_command=mock.Mock(return_value={'services': {}}),
                                     new_folder=mock.Mock(return_value=destination),
                                     freeze_images=mock.Mock(side_effect=lambda base, model: {'image': image[0]}),
                                     command=command, lock=upgrade_then_lock,
                                     SNAPSHOT_RESUME=data / 'resume.json', SNAPSHOT_HOOK=data / 'hook.service'):
                rt.snapshot({'data_mount': str(data)}, destination, apply=True)
            recorded = json.loads((destination / 'snapshot.json').read_text())
            self.assertEqual(recorded['immich_version'], 'v2.0.0')
            self.assertEqual(json.loads((destination / 'compose-resolved.json').read_text()),
                             {'image': 'immich-server@sha256:new'})


class SnapshotRestartTests(unittest.TestCase):
    """A manual Immich snapshot cut off with no `finally` (power cut, killed process)
    still gets Immich running again: from a durable record and a boot hook."""

    def setUp(self):
        import recovery_tools as rt
        self.rt = rt
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.running = {'database'}
        self.calls = []
        for name, value in (('SNAPSHOT_RESUME', self.root / 'resume.json'),
                            ('SNAPSHOT_HOOK', self.root / 'hook.service'),
                            ('command', self.command),
                            ('compose_args', mock.Mock(return_value=['docker', 'compose'])),
                            ('mounts', mock.Mock()), ('require_root', mock.Mock())):
            patcher = mock.patch.object(rt, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, argv, timeout=60, stdin=None, stdout=None):
        self.calls.append(argv)
        if argv[-2:] == ['--status', 'running'] or argv[-1] == '--services':
            return '\n'.join(sorted(self.running))
        if 'start' in argv:
            self.running |= set(argv[argv.index('start') + 1:])
        return ''

    def test_the_record_and_hook_exist_before_anything_stops(self):
        config = {'data_mount': str(self.root)}
        self.rt.owe_restart(config, self.root / 'config.json', ['immich-server', 'redis'])
        saved = json.loads((self.root / 'resume.json').read_text())
        self.assertEqual(saved['services'], ['immich-server', 'redis'])
        hook = (self.root / 'hook.service').read_text()
        self.assertIn('recover.py" "--config" "' + str((self.root / 'config.json').resolve()) + '" "resume"', hook)
        self.assertIn('ConditionPathExists=' + str(self.root / 'resume.json'), hook)
        self.assertIn('RequiresMountsFor=', hook)
        self.assertIn(['systemctl', 'enable', '--quiet', 'hook.service'], self.calls)

    def test_the_hook_works_from_a_folder_with_spaces(self):
        import shlex
        folder = self.root / 'Neko Server $HOME 100%'
        folder.mkdir()
        hook = self.rt.snapshot_hook({'data_mount': str(self.root)}, folder / 'config.json')
        line = next(x for x in hook.splitlines() if x.startswith('ExecStart='))[len('ExecStart='):]
        # systemd reads a quoted argument like the shell does, once $$ and %% are undone.
        args = shlex.split(line.replace('$$', '$').replace('%%', '%'))
        self.assertEqual(args[1:], [str(Path(self.rt.__file__).resolve().with_name('recover.py')),
                                    '--config', str((folder / 'config.json').resolve()), 'resume'])
        self.assertIn('$$HOME 100%%', line)
        with self.assertRaisesRegex(RuntimeError, 'cannot be used'):
            self.rt.systemd_arg('a\nb')  # shared from neko_common

    def test_the_hook_prefers_the_installer_bundle_kept_on_the_ssd(self):
        bundle = self.root / 'installer/releases' / ('v' + self.rt.VERSION)
        bundle.mkdir(parents=True)
        (bundle / 'recover.py').write_text('')
        (bundle / 'config.json').write_text('{}')
        hook = self.rt.snapshot_hook({'data_mount': str(self.root)}, '/tmp/Downloads/nekoserver/config.json')
        self.assertIn('ExecStart="/usr/bin/python3" "' + str(bundle / 'recover.py') + '" "--config" "' + str(bundle / 'config.json') + '" "resume"', hook)

    def test_after_a_cut_off_snapshot_the_hook_restarts_immich_and_clears_itself(self):
        config = {'data_mount': str(self.root)}
        self.rt.owe_restart(config, self.root / 'config.json', ['immich-server', 'redis'])
        # ... the machine lost power here: nothing restarted Immich.
        lock = mock.MagicMock()
        with mock.patch.object(self.rt, 'lock', return_value=lock):
            result = self.rt.resume(config)
        self.assertEqual(result['restarted'], ['immich-server', 'redis'])
        self.assertIn(['docker', 'compose', 'start', 'immich-server', 'redis'], self.calls)
        self.assertFalse((self.root / 'resume.json').exists())
        self.assertFalse((self.root / 'hook.service').exists())
        self.assertIn(['systemctl', 'disable', '--quiet', 'hook.service'], self.calls)

    def test_a_restart_that_does_not_take_keeps_the_record(self):
        config = {'data_mount': str(self.root)}
        self.rt.owe_restart(config, self.root / 'config.json', ['immich-server'])
        self.command = lambda argv, timeout=60, stdin=None, stdout=None: 'database\n' if 'ps' in argv else ''
        with mock.patch.object(self.rt, 'command', self.command):
            with self.assertRaisesRegex(RuntimeError, 'did not start again'):
                self.rt.restart_owed(config)
        self.assertTrue((self.root / 'resume.json').exists())
        self.assertTrue((self.root / 'hook.service').exists())

    def test_nothing_owed_does_nothing(self):
        self.assertEqual(self.rt.restart_owed({'data_mount': str(self.root)}), [])
        self.assertEqual(self.calls, [])

    def test_a_damaged_record_is_not_guessed_at(self):
        (self.root / 'resume.json').write_text('{"format":"neko-immich-snapshot-resume-v1","services":["sshd"]}')
        with self.assertRaisesRegex(RuntimeError, 'Unreadable record'):
            self.rt.restart_owed({'data_mount': str(self.root)})


class DashboardPortTests(unittest.TestCase):
    def test_the_dashboard_cannot_take_an_app_port(self):
        from modules import dashboard, media
        for port in (8088, 8096, 2283, 8448):  # Nextcloud, Jellyfin, Immich, Tailscale dashboard
            with self.subTest(port), self.assertRaisesRegex(RuntimeError, 'taken or not allowed'):
                dashboard.options({'dashboard': {'port': port}})
        self.assertEqual(dashboard.options({'dashboard': {'port': 8787}})[0], 8787)
        self.assertEqual(dashboard.options({})[0], 8787)
        # A custom dashboard port and qBittorrent's peer port cannot be the same.
        clash = {'dashboard': {'port': 9999}, 'media': {'torrent_port': 9999}}
        with self.assertRaisesRegex(RuntimeError, 'taken or not allowed'):
            dashboard.options(clash)
        import config_checks
        with self.assertRaisesRegex(RuntimeError, 'is the dashboard port'):
            config_checks.validate(clash, ['media'])
        self.assertEqual(dashboard.options({'dashboard': {'port': 9999}, 'media': {'torrent_port': 50000}})[0], 9999)
        # One list for both checks: everything the apps use is also kept away from imports.
        from neko_common import APP_PORTS
        self.assertTrue(APP_PORTS <= media.RESERVED_PORTS and 8787 in media.RESERVED_PORTS)


class UnfinishedMaintenanceTests(unittest.TestCase):
    def test_an_upgrade_waits_for_an_unfinished_monthly_update(self):
        import tempfile
        import neko_common
        idle = lambda: False     # noqa: E731 (no monthly update running on this machine)
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'maintenance-resume.json'
            self.assertIsNone(neko_common.pending_maintenance(journal, running=idle))  # nothing pending
            # Even the same version: setup would start the apps before a pending rollback.
            journal.write_text(json.dumps({'format': 'neko-maintenance-resume-v1', 'version': neko_common.VERSION}))
            self.assertIn('nekoserver-maintenance-resume.service', neko_common.pending_maintenance(journal, running=idle))
            journal.write_text(json.dumps({'format': 'neko-maintenance-resume-v1', 'version': '0.24.4'}))
            message = neko_common.pending_maintenance(journal, running=idle)
            self.assertIn('NekoServer v0.24.4', message)
            self.assertIn('sudo systemctl start nekoserver-maintenance-resume.service', message)
            journal.write_text('not json')
            self.assertIn('an earlier NekoServer', neko_common.pending_maintenance(journal, running=idle))
            # The installer stops before taking its lock or running any module.
            journal.write_text(json.dumps({'version': '0.24.4'}))
            args = mock.Mock(apply=True)
            with mock.patch.object(installer.os, 'geteuid', return_value=0), \
                    mock.patch.object(neko_common, 'MAINTENANCE_JOURNAL', journal), \
                    mock.patch.object(installer, 'pending_maintenance', return_value=neko_common.pending_maintenance(journal, running=idle)), \
                    mock.patch.object(installer, '_execute') as run_modules:
                with self.assertRaisesRegex(installer.InstallerError, 'did not finish'):
                    installer.execute(args)
            run_modules.assert_not_called()

    def test_a_monthly_update_running_right_now_is_called_running_not_cut_off(self):
        import neko_common
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'maintenance-resume.json'
            journal.write_text(json.dumps({'version': neko_common.VERSION}))
            message = neko_common.pending_maintenance(journal, running=lambda: True)
            self.assertIn('is running right now', message)
            self.assertNotIn('did not finish', message)
            self.assertNotIn('maintenance-resume.service', message)   # nothing to start: it is working

    def test_running_means_activating_for_either_monthly_service(self):
        import neko_common
        for states, expected in (('activating\ninactive\n', True), ('inactive\nactivating\n', True),
                                 ('inactive\ninactive\n', False), ('failed\ninactive\n', False)):
            run = mock.Mock(return_value=mock.Mock(stdout=states))
            self.assertEqual(neko_common.maintenance_running(run), expected, states)
        self.assertEqual(run.call_args[0][0], ['systemctl', 'is-active', *neko_common.MAINTENANCE_UNITS])
        self.assertFalse(neko_common.maintenance_running(mock.Mock(side_effect=OSError)))

    def test_setup_rechecks_once_it_holds_the_shared_lock(self):
        """A monthly update that starts (and fails, keeping its journal) while setup waits
        for the shared lock must stop setup before it changes or starts any app."""
        import fcntl
        import threading
        import time
        import neko_common
        import recovery_tools
        real_wait = recovery_tools.wait_for
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'maintenance-resume.json'
            config = {'data_mount': d}
            lock_file = Path(d) / 'installer/recovery.lock'
            lock_file.parent.mkdir()
            ran, errors = [], []

            def setup():
                try:
                    with recovery_tools.lock(config):
                        ran.append('compose up -d')
                except RuntimeError as exc:
                    errors.append(str(exc))
            with mock.patch.object(neko_common, 'MAINTENANCE_JOURNAL', journal), \
                    mock.patch.object(neko_common, 'maintenance_running', return_value=False), \
                    mock.patch.object(recovery_tools, 'wait_for',
                                      lambda stream, patient: real_wait(stream, True, sleep=lambda s: time.sleep(0.02))):
                with lock_file.open('a') as held:  # the monthly update holds the lock
                    fcntl.flock(held, fcntl.LOCK_EX)
                    waiting = threading.Thread(target=setup)
                    waiting.start()
                    time.sleep(0.1)
                    journal.write_text(json.dumps({'version': neko_common.VERSION}))  # it fails, journal kept
                waiting.join(5)
                self.assertEqual(ran, [])
                self.assertIn('did not finish', errors[0])
                self.assertIn('Setup stopped before changing anything else.', errors[0])
                # Docker re-attachment and backup repair can still run.
                with recovery_tools.lock(config, changes_apps=False):
                    ran.append('repair')
                journal.unlink()
                with recovery_tools.lock(config):
                    ran.append('compose up -d')
            self.assertEqual(ran, ['repair', 'compose up -d'])

    def test_install_sh_checks_before_the_system_update(self):
        text = (Path(installer.__file__).parent / 'install.sh').read_text()
        self.assertLess(text.index('maintenance-resume.json'), text.index('pacman -Syu'))
        # Too old a Debian/Ubuntu is refused before apt runs; free space is measured in
        # MB (df rounds GB up); dpkg never asks about an edited settings file.
        self.assertLess(text.index('needs Debian 12 or newer'), text.index('apt-get "${apt_wait[@]}" update'))
        self.assertIn('df -BM --output=avail', text)
        self.assertIn('system_free < 5120', text)
        self.assertIn('Dpkg::Options::=--force-confold', text)

    def test_install_sh_release_floor(self):
        import subprocess
        script = (Path(installer.__file__).parent / 'install.sh').read_text()
        check = script[script.index('        release="${VERSION_ID:-}"'):script.index('        fi\n', script.index('needs Debian 12 or newer'))] + '        fi\n'
        for osr, old in (('ID=ubuntu\nVERSION_ID=22.04', True), ('ID=ubuntu\nVERSION_ID=24.04', False),
                         ('ID=debian\nVERSION_ID=11', True), ('ID=debian\nVERSION_ID=13', False), ('ID=debian', False)):
            run = subprocess.run(['bash', '-c', osr + '\n' + check + 'echo fine'], capture_output=True, text=True, check=False)
            self.assertEqual(run.stdout.strip() != 'fine', old, osr)

    def test_too_old_a_python_says_which_releases_have_it(self):
        import subprocess
        script = (Path(installer.__file__).parent / 'install.sh').read_text()
        start = script.index("if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'")
        check = script[start:script.index('\nfi\n', start) + 4]
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / 'python3'   # Linux Mint 21 (Ubuntu 22.04): Python 3.10
            fake.write_text('#!/bin/sh\ncase "$*" in *version_info*) exit 1;; *python_version*) echo 3.10.12;; esac\n')
            fake.chmod(0o755)
            run = subprocess.run(['bash', '-c', check + 'echo fine'], capture_output=True, text=True, check=False,
                                 env={**os.environ, 'PATH': d + ':' + os.environ['PATH']})
        self.assertEqual(run.returncode, 1)
        self.assertIn('this system has 3.10.12', run.stderr)
        self.assertIn('upgrade to the next release first', run.stderr)


class DumpCompleteTests(unittest.TestCase):
    def test_a_cut_off_database_dump_is_recognised(self):
        import tempfile
        import recovery_tools as rt
        with tempfile.TemporaryDirectory() as d:
            dump = Path(d) / 'database.sql'
            dump.write_bytes(b'CREATE TABLE asset (id int);\n' * 500 + b'--\n-- PostgreSQL database dump complete\n--\n')
            self.assertTrue(rt.dump_complete(dump))
            dump.write_bytes(b'CREATE TABLE asset (id int);\n' * 500)  # the disk filled up here
            self.assertFalse(rt.dump_complete(dump))


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

    def test_a_typo_in_the_usb_picker_is_asked_again(self):
        import copy

        from modules import photo_backup
        config = copy.deepcopy(self.CONFIG)
        answers = iter(['5', '1', 'sdb1', 'sdc1'])
        photo_backup.select_external(config, self.INVENTORY, input_fn=lambda q='': next(answers), out_fn=lambda s: None)
        self.assertEqual(config['photos']['external_backup_uuid'], 'usb-uuid-0001')

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
        config['nextcloud'] = {'enabled': True}

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


class UsbCopyWithoutImmichTests(unittest.TestCase):
    """Nextcloud's files have no other off-server copy (monthly snapshots leave them out):
    the weekly USB copy must not need Immich."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = {'data_mount': str(root / 'ssd'), 'media_mount': str(root / 'hdd'),
                       'external_backup_mount': str(root / 'usb'), 'timezone': 'UTC',
                       'storage': {'data_ssd': {'uuid': 'ssd-uuid-0001'}, 'media_hdd': {'uuid': 'hdd-uuid-0001'}},
                       'photos': {'deploy_immich': False, 'external_backup_uuid': 'usb-uuid-0001'},
                       'nextcloud': {'enabled': True}}

    def install(self):
        from modules import photo_backup

        class Ctx:
            apply = True
            config = self.config
            log = staticmethod(lambda message: None)
        written = {}
        with mock.patch.object(photo_backup.os, 'geteuid', return_value=0), \
                mock.patch.object(storage, 'inventory_now', return_value={'blockdevices': []}), \
                mock.patch.object(photo_backup.native_runtime, 'deploy', return_value=Path('/rt')), \
                mock.patch.object(photo_backup.native_runtime, 'backup_guard', return_value=Path('/guard.json')), \
                mock.patch.object(photo_backup, 'atomic_write', side_effect=lambda path, text, mode: written.update({path.name: text})), \
                mock.patch.object(photo_backup.subprocess, 'run'):
            result = photo_backup.run(Ctx())
        return result, written

    def test_nextcloud_alone_gets_its_weekly_usb_copy(self):
        from modules import photo_backup
        self.assertEqual(photo_backup.DEPENDENCIES, ['storage', 'capacity'])
        result, written = self.install()   # no photos/archive folder exists here
        self.assertIsNone(result)
        service = written['nekoserver-photo-backup.service']
        self.assertIn(' --cloud ' + self.config['media_mount'] + '/cloud/files', service)
        self.assertNotIn('--source', service)
        self.assertNotIn('immich-db-backup', service)

    def test_with_immich_the_archive_is_still_required_and_copied(self):
        self.config['photos']['deploy_immich'] = True
        # Before the photos step has made the archive, this step waits for it (it is not
        # installed half-way, and does not fail on the photos step's account).
        result, written = self.install()
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('Waiting for the photos (Immich) step', result['reason'])
        self.assertNotIn('nekoserver-photo-backup.service', written)
        archive = Path(self.config['media_mount']) / 'photos/archive'
        archive.mkdir(parents=True)
        _, written = self.install()
        self.assertIn('--source ' + str(archive), written['nekoserver-photo-backup.service'])

    def test_nothing_to_copy_is_not_a_failure(self):
        self.config['nextcloud']['enabled'] = False
        result, written = self.install()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(written, {})

    def test_no_photo_schedule_without_immich(self):
        from modules import photo_preservation
        units = Path(self.tmp.name) / 'units'
        units.mkdir()
        ctx = mock.Mock(apply=True, config=self.config, log=lambda message: None)
        run = mock.Mock()
        with mock.patch.object(photo_preservation.os, 'geteuid', return_value=0):
            photo_preservation.switch_off(ctx, units, run)
        run.assert_not_called()   # never installed: nothing to do
        # Installed by an earlier setup, then Immich switched off: stopped and removed.
        for name in ('nekoserver-immich-db-backup', 'nekoserver-photo-preservation'):
            (units / (name + '.service')).write_text(f'[Unit]\nDescription={name}\n')
            (units / (name + '.timer')).write_text(f'[Unit]\nDescription={name} timer\n')
        (units / 'nekoserver-photo-backup.timer').write_text('[Unit]\nDescription=Weekly NekoServer photo backup\n')
        calls = []
        photo_preservation.switch_off(ctx, units, lambda argv, **_: calls.append(argv))
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-immich-db-backup.timer'], calls)
        self.assertIn(['systemctl', 'stop', 'nekoserver-photo-preservation.service'], calls)
        self.assertEqual(calls[-1], ['systemctl', 'daemon-reload'])
        # The USB copy (Nextcloud's files) keeps its own schedule.
        self.assertEqual([p.name for p in units.iterdir()], ['nekoserver-photo-backup.timer'])

    def test_photo_schedule_someone_else_wrote_is_kept(self):
        from modules import photo_preservation
        units = Path(self.tmp.name) / 'units'
        units.mkdir()
        (units / 'nekoserver-immich-db-backup.timer').write_text('[Unit]\nDescription=mine\n')
        ctx = mock.Mock(apply=True, config=self.config, log=lambda message: None)
        run = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged schedule preserved'):
            photo_preservation.switch_off(ctx, units, run)
        self.assertTrue((units / 'nekoserver-immich-db-backup.timer').exists())

    def test_the_runtime_accepts_a_copy_without_photos(self):
        source = (ROOT / 'runtime/src/backup.rs').read_text()
        self.assertIn('let source = match a.value("source", "").as_str()', source)
        self.assertIn('Nothing to copy: give --source, --cloud or both', source)


class ImmichSwitchedOffTests(unittest.TestCase):
    """Immich switched off after it was set up must not keep running, unwatched."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'compose/immich'
        self.logs = []
        self.ctx = mock.Mock(log=self.logs.append)

    def test_a_running_deployment_is_taken_down_and_its_data_kept(self):
        from modules import photos
        self.folder.mkdir(parents=True)
        (self.folder / 'compose.yaml').write_text('services: {}\n')
        (self.folder / '.env').write_text('DB_PASSWORD=x\n')
        calls = []

        def run(argv, **_):
            calls.append(argv)
            return mock.Mock(stdout='', returncode=0)
        photos.switch_off(self.ctx, self.folder, run, journal=self.folder / 'no-journal')
        self.assertEqual(calls[0][:5], ['docker', 'compose', '-p', 'neko-immich', '-f'])
        self.assertIn('down', calls[0])
        self.assertNotIn('-v', calls[0])           # volumes (the model cache) kept
        self.assertNotIn('--volumes', calls[0])
        self.assertTrue((self.folder / '.env').exists())
        self.assertIn('kept on the SSD', self.logs[-1])

    def test_one_still_running_is_reported(self):
        from modules import photos
        self.folder.mkdir(parents=True)
        (self.folder / 'compose.yaml').write_text('services: {}\n')
        with self.assertRaisesRegex(RuntimeError, 'still running'):
            photos.switch_off(self.ctx, self.folder, lambda argv, **_: mock.Mock(stdout='abc123\n', returncode=0),
                              journal=self.folder / 'no-journal')

    def test_not_during_an_unfinished_monthly_update(self):
        from modules import photos
        self.folder.mkdir(parents=True)
        (self.folder / 'compose.yaml').write_text('services: {}\n')
        (self.folder / 'journal.json').write_text('{}')
        run = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'monthly update did not finish'):
            photos.switch_off(self.ctx, self.folder, run, journal=self.folder / 'journal.json')
        run.assert_not_called()

    def test_never_set_up_does_nothing(self):
        from modules import photos
        run = mock.Mock()
        photos.switch_off(self.ctx, self.folder, run)
        run.assert_not_called()


class AccountRepairTests(unittest.TestCase):
    """Retrying only the accounts step works on the apps set up now, not on every login
    ever saved (an app switched off since would be asked for, with its containers gone)."""
    def test_only_enabled_apps_are_set_up_and_saved_logins_stay(self):
        import account_setup
        import account_store
        from modules import accounts
        config = {'media': {'services': ['jellyfin']}, 'photos': {'deploy_immich': False},
                  'nextcloud': {'enabled': False}, 'accounts': {'enabled': True}}
        ctx = mock.Mock(apply=True, config=config, log=lambda m: None, plan=('accounts',))
        saved = {'apps': {'jellyfin': {}, 'immich': {}, 'nextcloud': {}}}
        provisioned = []
        with mock.patch.object(accounts, 'verify_storage'), mock.patch.object(accounts, 'lock', return_value=mock.MagicMock()), \
                mock.patch.object(account_store, 'load', return_value=saved), \
                mock.patch.object(account_setup, 'provision', side_effect=lambda cfg, apps, output: provisioned.extend(apps) or []):
            self.assertIsNone(accounts.run(ctx))
        self.assertEqual(provisioned, ['jellyfin'])
        self.assertEqual(set(saved['apps']), {'jellyfin', 'immich', 'nextcloud'})  # kept for when they come back

    def test_every_app_step_alone_gets_its_password_step(self):
        """--module media (an app added later), photos or nextcloud: the login is chosen
        before the app first starts, so it is never mistaken for an existing account."""
        modules = installer.load_modules(ROOT / 'modules')
        for step in ('nextcloud', 'media', 'photos'):
            with self.subTest(step):
                order = installer.ordered(modules, [step])
                self.assertLess(order.index('account_credentials'), order.index(step))


class ReleaseIdentityTests(unittest.TestCase):
    def test_every_part_of_the_release_names_the_same_version(self):
        """Releases are installed into immutable per-version folders (installer/releases/vX,
        /usr/local/lib/nekoserver-*/vX). Two different releases with the same version
        collide there and the upgrade is refused, so every place that names it must agree.
        (The source folder itself is simply `nekoserver` since 0.38.0: git pull updates it
        in place, and the version lives in neko_common.VERSION.)"""
        import re
        from neko_common import VERSION
        project = Path(installer.__file__).parent
        repo = project.parent
        self.assertEqual(project.name, 'nekoserver')
        cargo = (project / 'runtime/Cargo.toml').read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', cargo, re.M)[1], VERSION)
        lock = (project / 'runtime/Cargo.lock').read_text()
        self.assertIn(f'name = "nekoserver-runtime"\nversion = "{VERSION}"', lock)
        self.assertEqual(json.loads((project / 'config.json').read_text())['project_name'], 'NekoServer v' + VERSION)
        self.assertTrue((project / 'README.md').read_text().startswith(f'# NekoServer v{VERSION}\n'))
        ci = repo / '.github/workflows/ci.yml'
        if ci.is_file():  # not shipped inside a release folder copied on its own
            self.assertIn('RELEASE_DIR: nekoserver\n', ci.read_text())
        for doc in project.glob('*.md'):
            for found in re.findall(r'(?:releases|nekoserver-[a-z]+)/v(\d+\.\d+\.\d+)', doc.read_text()):
                self.assertEqual(found, VERSION, doc.name)



class KeptCopyTests(unittest.TestCase):
    """The copy of the release kept on the SSD (modules/recovery.preserve): another build of
    the same version replaces NekoServer's own untouched copy, setting it aside; a copy
    someone changed is never overwritten; set-aside copies go with their version."""
    def setUp(self):
        from modules import recovery
        self.recovery = recovery
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'ssd'
        self.project = Path(self.tmp.name) / 'nekoserver'
        for folder in ('modules', 'compose', 'dashboard', 'setup-gui', 'runtime/src', 'runtime/licenses', 'runtime/bin'):
            (self.project / folder).mkdir(parents=True)
        for name in ('install.sh', 'installer.py', 'README.md', 'runtime/build.py', 'runtime/Cargo.toml', 'runtime/Cargo.lock',
                     'runtime/build.json', 'runtime/bin/nekoserver-runtime', 'runtime/THIRD-PARTY-NOTICES.md', 'runtime/src/main.rs'):
            (self.project / name).write_text(name + ' v1\n')
        self.data.mkdir()
        patcher = mock.patch.object(self.recovery.os.path, 'ismount', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []

    def preserve(self):
        return self.recovery.preserve(self.data, self.project, {'data_mount': str(self.data)}, self.logged.append)

    def set_aside(self, dest):
        return [p for p in dest.parent.iterdir() if p.name.startswith(dest.name + self.recovery.SET_ASIDE)]

    def test_another_build_of_the_version_replaces_the_untouched_kept_copy(self):
        dest = self.preserve()
        self.assertEqual((dest / 'runtime/bin/nekoserver-runtime').read_text(), 'runtime/bin/nekoserver-runtime v1\n')
        (self.project / 'runtime/bin/nekoserver-runtime').write_text('v2 build\n')
        (self.project / 'runtime/build.json').write_text('v2 record\n')
        self.assertEqual(self.preserve(), dest)
        self.assertEqual((dest / 'runtime/bin/nekoserver-runtime').read_text(), 'v2 build\n')
        aside = self.set_aside(dest)
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / 'runtime/bin/nekoserver-runtime').read_text(), 'runtime/bin/nekoserver-runtime v1\n')
        self.assertTrue(any('set aside at ' + str(aside[0]) in line for line in self.logged), self.logged)
        self.assertTrue(self.recovery.own_untouched(dest))

    def test_a_kept_copy_someone_changed_is_never_overwritten(self):
        dest = self.preserve()
        (dest / 'install.sh').write_text('edited by hand\n')
        (self.project / 'install.sh').write_text('install.sh v2\n')
        with self.assertRaisesRegex(RuntimeError, 'not as NekoServer wrote it'):
            self.preserve()
        self.assertEqual((dest / 'install.sh').read_text(), 'edited by hand\n')
        self.assertEqual([p.name for p in dest.parent.iterdir()], [dest.name])

    def test_one_set_aside_copy_per_version_and_it_goes_with_its_version(self):
        import native_runtime
        dest = self.preserve()
        for build in ('v2', 'v3'):
            (self.project / 'runtime/bin/nekoserver-runtime').write_text(build + '\n')
            self.preserve()
        self.assertEqual(len(self.set_aside(dest)), 1)
        # Retention: the set-aside copy of the current version stays; one of another version goes.
        other = dest.parent / 'v0.1.0.replaced-20260101-000000'
        other.mkdir()
        lib, units = Path(self.tmp.name) / 'lib', Path(self.tmp.name) / 'units'
        lib.mkdir()
        units.mkdir()
        removed = native_runtime.retire_old(self.data, self.logged.append, lib=lib, units=units)
        self.assertIn(other, removed)
        self.assertFalse(other.exists())
        self.assertEqual(len(self.set_aside(dest)), 1)
