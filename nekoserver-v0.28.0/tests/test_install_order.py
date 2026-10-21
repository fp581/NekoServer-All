"""v0.23.1: install order, system update first, drive picking and weekly catch-up."""
import json
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
        refresh = script.index('apt-get update')
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


class HardwareVideoLockTests(unittest.TestCase):
    def test_immich_is_not_touched_while_a_backup_holds_the_lock(self):
        import fcntl
        import tempfile
        import recovery_tools
        from modules import hardware_video as hv

        class Ctx:
            def __init__(self, data):
                self.config, self.logs = {'data_mount': data}, []

            def log(self, m):
                self.logs.append(m)
        with tempfile.TemporaryDirectory() as data:
            calls = []
            lock_file = Path(data) / 'installer/recovery.lock'
            lock_file.parent.mkdir(parents=True)
            with mock.patch.object(recovery_tools, 'mounts'), \
                    mock.patch.object(hv, 'give_immich_the_chip', lambda *a: calls.append('immich')), \
                    mock.patch.object(hv, 'apps_on', lambda *a: calls.append('apps') or {}), \
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
                                     command=command, lock=upgrade_then_lock):
                rt.snapshot({'data_mount': str(data)}, destination, apply=True)
            recorded = json.loads((destination / 'snapshot.json').read_text())
            self.assertEqual(recorded['immich_version'], 'v2.0.0')
            self.assertEqual(json.loads((destination / 'compose-resolved.json').read_text()),
                             {'image': 'immich-server@sha256:new'})


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
        with tempfile.TemporaryDirectory() as d:
            journal = Path(d) / 'maintenance-resume.json'
            self.assertIsNone(neko_common.pending_maintenance(journal))  # nothing pending
            # Even the same version: setup would start the apps before a pending rollback.
            journal.write_text(json.dumps({'format': 'neko-maintenance-resume-v1', 'version': neko_common.VERSION}))
            self.assertIn('nekoserver-maintenance-resume.service', neko_common.pending_maintenance(journal))
            journal.write_text(json.dumps({'format': 'neko-maintenance-resume-v1', 'version': '0.24.4'}))
            message = neko_common.pending_maintenance(journal)
            self.assertIn('NekoServer v0.24.4', message)
            self.assertIn('sudo systemctl start nekoserver-maintenance-resume.service', message)
            journal.write_text('not json')
            self.assertIn('an earlier NekoServer', neko_common.pending_maintenance(journal))
            # The installer stops before taking its lock or running any module.
            journal.write_text(json.dumps({'version': '0.24.4'}))
            args = mock.Mock(apply=True)
            with mock.patch.object(installer.os, 'geteuid', return_value=0), \
                    mock.patch.object(neko_common, 'MAINTENANCE_JOURNAL', journal), \
                    mock.patch.object(installer, 'pending_maintenance', lambda: neko_common.pending_maintenance(journal)), \
                    mock.patch.object(installer, '_execute') as run_modules:
                with self.assertRaisesRegex(installer.InstallerError, 'did not finish'):
                    installer.execute(args)
            run_modules.assert_not_called()

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


class ReleaseIdentityTests(unittest.TestCase):
    def test_every_part_of_the_release_names_the_same_version(self):
        """Releases are installed into immutable per-version folders (installer/releases/vX,
        /usr/local/lib/nekoserver-*/vX). Two different releases with the same version
        collide there and the upgrade is refused, so every place that names it must agree."""
        import re
        from neko_common import VERSION
        project = Path(installer.__file__).parent
        repo = project.parent
        self.assertEqual(project.name, 'nekoserver-v' + VERSION)
        cargo = (project / 'runtime/Cargo.toml').read_text()
        self.assertEqual(re.search(r'^version = "([^"]+)"', cargo, re.M)[1], VERSION)
        lock = (project / 'runtime/Cargo.lock').read_text()
        self.assertIn(f'name = "nekoserver-runtime"\nversion = "{VERSION}"', lock)
        self.assertEqual(json.loads((project / 'config.json').read_text())['project_name'], 'NekoServer v' + VERSION)
        self.assertTrue((project / 'README.md').read_text().startswith(f'# NekoServer v{VERSION}\n'))
        ci = repo / '.github/workflows/ci.yml'
        if ci.is_file():  # not shipped inside a release folder copied on its own
            self.assertIn(f'RELEASE_DIR: nekoserver-v{VERSION}', ci.read_text())
        for doc in project.glob('*.md'):
            for found in re.findall(r'(?:releases|nekoserver-[a-z]+)/v(\d+\.\d+\.\d+)', doc.read_text()):
                self.assertEqual(found, VERSION, doc.name)

