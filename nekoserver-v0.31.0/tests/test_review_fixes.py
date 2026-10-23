"""Regression tests for the compose-path, Seerr UID and timezone fixes."""
import os
import sys
import http.cookiejar
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common  # noqa: E402
from modules import media  # noqa: E402


def render(name, uid=1234, gid=2345, tz='Europe/London'):
    template = (ROOT / 'compose' / name).read_text()
    return media.render(template, '/srv/nekoserver', '/mnt/nekoserver-media', uid, gid, tz, '127.0.0.1')


class TimezoneTests(unittest.TestCase):
    def setUp(self):
        # Pretend every zone is installed unless a test says otherwise.
        self.zoneinfo = tempfile.TemporaryDirectory()
        for zone in ('Asia/Jerusalem', 'Europe/London', 'America/New_York', 'UTC'):
            path = Path(self.zoneinfo.name) / zone
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        patcher = mock.patch.object(neko_common, 'ZONEINFO', Path(self.zoneinfo.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.zoneinfo.cleanup)

    def test_top_level_key_wins_over_legacy_keys(self):
        cfg = {'timezone': 'Europe/London', 'media': {'timezone': 'Asia/Jerusalem'},
               'maintenance': {'timezone': 'America/New_York'}}
        self.assertEqual(neko_common.host_timezone(cfg), 'Europe/London')

    def test_legacy_media_then_maintenance_keys(self):
        self.assertEqual(neko_common.host_timezone({'media': {'timezone': 'America/New_York'}}), 'America/New_York')
        self.assertEqual(neko_common.host_timezone({'maintenance': {'timezone': 'Europe/London'}}), 'Europe/London')

    def test_falls_back_to_host_localtime_then_utc(self):
        with mock.patch.object(os, 'readlink', return_value='../usr/share/zoneinfo/America/New_York'):
            self.assertEqual(neko_common.host_timezone({}), 'America/New_York')
        # No link and no /etc/timezone either: UTC.
        with mock.patch.object(neko_common, 'local_timezone', return_value=None):
            self.assertEqual(neko_common.host_timezone({}), 'UTC')

    def test_rejects_invalid_or_uninstalled_zones(self):
        for bad in ('../../etc/passwd', 'Europe/London\nExecStart=/bin/sh', 'Mars/Olympus', 5):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                neko_common.host_timezone({'timezone': bad})

    def test_no_hardcoded_zone_in_code(self):
        offenders = [str(p.relative_to(ROOT)) for p in [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py'), *ROOT.glob('runtime/src/*.rs')]
                     if 'Asia/Jerusalem' in p.read_text()]
        self.assertEqual(offenders, [])

    def test_music_backup_timer_uses_configured_zone(self):
        from modules import music_backup
        units = music_backup.definitions({'timezone': 'Europe/London', 'data_mount': '/srv/nekoserver',
                                          'media_mount': '/mnt/nekoserver-media'}, '/bin/rt', '/etc/guard')
        self.assertIn('OnCalendar=*-*-* 03:45:00 Europe/London\n', units['nekoserver-music-backup.timer'])


class ComposeTests(unittest.TestCase):
    def test_seerr_uses_configured_ids(self):
        self.assertIn('user: "1234:2345"', render('seerr.yaml'))

    def test_no_hardcoded_ids_in_any_compose_file(self):
        for path in (ROOT / 'compose').glob('*.yaml'):
            with self.subTest(path=path.name):
                self.assertNotIn('1000:1000', path.read_text())

    def test_lidarr_has_single_ssd_mount_for_downloads_and_library(self):
        text = render('lidarr.yaml')
        self.assertIn('"/srv/nekoserver/library:/data"', text)
        # No nested bind mounts: they split downloads and library across mount points (EXDEV on import).
        self.assertNotIn(':/data/downloads/music', text)
        self.assertNotIn(':/data/music', text)
        self.assertNotIn('/mnt/nekoserver-media', text)

    def test_qbittorrent_still_sees_hdd_and_ssd_music_downloads(self):
        text = render('qbittorrent.yaml')
        self.assertIn('"/mnt/nekoserver-media/library:/data"', text)
        self.assertIn('"/srv/nekoserver/library/downloads/music:/data/downloads/music"', text)


class JellyfinGpuTests(unittest.TestCase):
    def test_gpu_block_kept_when_device_present(self):
        text = render('jellyfin.yaml')
        self.assertEqual(media.adapt_to_host('jellyfin', text, gpu_present=True), text)
        self.assertIn(media.GPU_DEVICE_BLOCK, text)

    def test_gpu_block_removed_without_device_and_compose_stays_valid(self):
        import yaml
        adapted = media.adapt_to_host('jellyfin', render('jellyfin.yaml'), gpu_present=False)
        service = yaml.safe_load(adapted)['services']['jellyfin']
        self.assertNotIn('devices', service)
        self.assertEqual(service['restart'], 'unless-stopped')

    def test_other_services_untouched(self):
        text = render('sonarr.yaml')
        self.assertEqual(media.adapt_to_host('sonarr', text, gpu_present=False), text)


class TorrentPortTests(unittest.TestCase):
    def test_closed_by_default(self):
        self.assertIsNone(media.torrent_port({}))
        text = render('qbittorrent.yaml')
        self.assertEqual(media.adapt_to_host('qbittorrent', text, peer_port=None), text)

    def test_invalid_or_reserved_ports_refused(self):
        for bad in (80, 8080, 8443, 2283, 70000, '6881', True):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                media.torrent_port({'torrent_port': bad})

    def test_opened_port_is_published_and_set_in_qbittorrent(self):
        import yaml
        text = media.adapt_to_host('qbittorrent', render('qbittorrent.yaml'), peer_port=51413)
        service = yaml.safe_load(text)['services']['qbittorrent']
        self.assertEqual(service['environment']['TORRENTING_PORT'], '51413')
        self.assertEqual(service['ports'], ['127.0.0.1:8080:8080', '51413:51413/tcp', '51413:51413/udp'])

    def test_any_earlier_variant_is_recognised_as_ours(self):
        base = render('qbittorrent.yaml')
        for port in (6881, 51413):
            opened = media.adapt_to_host('qbittorrent', base, peer_port=port)
            self.assertEqual(media.without_host_adaptations(opened), base)
        jellyfin = render('jellyfin.yaml')
        self.assertEqual(media.without_host_adaptations(jellyfin),
                         media.adapt_to_host('jellyfin', jellyfin, gpu_present=False))

    def test_acceptance_allows_only_the_configured_peer_port(self):
        import acceptance
        inspect = {'Config': {'Env': ['PUID=1000', 'TORRENTING_PORT=51413']}}
        self.assertEqual(acceptance.peer_port(inspect), 51413)
        config = {'media': {'torrent_port': 51413}}
        self.assertEqual(acceptance.peer_bindings(51413, config), {'51413/tcp', '51413/udp'})
        # Container claims a port the config did not choose: nothing is exempt.
        self.assertEqual(acceptance.peer_bindings(51413, {'media': {'torrent_port': None}}), set())
        self.assertEqual(acceptance.peer_bindings(None, config), set())


class MediaManifestRewriteTests(unittest.TestCase):
    """The media step replaces its own unedited manifests after a settings or timezone
    change, instead of failing every later setup run; a hand-edited one is left alone."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / 'sonarr' / 'compose.yaml'
        self.dest.parent.mkdir()
        self.template = (ROOT / 'compose' / 'sonarr.yaml').read_text()

    def values(self, uid=1000, tz='UTC'):
        return ('/srv/nekoserver', '/mnt/nekoserver-media', uid, 1000, tz, '127.0.0.1')

    def test_a_written_manifest_is_replaced_after_a_settings_change(self):
        old = media.render(self.template, *self.values())
        self.dest.write_text(old)
        media.remember(self.dest, old)
        moved = ('/srv/neko-ssd', '/mnt/nekoserver-media', 1000, 1000, 'Asia/Jerusalem', '127.0.0.1')
        backup, current = media.replacement('sonarr', self.dest, self.template, media.render(self.template, *moved), moved)
        self.assertEqual((backup.parent, current), (self.dest.parent, old))
        self.assertRegex(backup.name, r'^compose\.before-\d+\.yaml$')

    def test_another_app_user_is_refused_with_what_to_do(self):
        for name in ('sonarr', 'seerr'):   # PUID/PGID and user:
            with self.subTest(name=name):
                template = (ROOT / 'compose' / f'{name}.yaml').read_text()
                old = media.render(template, *self.values())
                self.dest.write_text(old)
                media.remember(self.dest, old)
                values = self.values(uid=1500)
                new = media.render(template, *values)
                with self.assertRaisesRegex(RuntimeError, 'from 1000:1000 to 1500:1000.*Put media.uid'):
                    media.replacement(name, self.dest, template, new, values)

    def test_a_manifest_from_before_the_record_is_recognised_after_a_timezone_change(self):
        self.dest.write_text(media.render(self.template, *self.values()))
        new = media.render(self.template, *self.values(tz='Asia/Jerusalem'))
        self.assertIn('TZ: "Asia/Jerusalem"', new)
        self.assertTrue(media.replacement('sonarr', self.dest, self.template, new, self.values(tz='Asia/Jerusalem')))

    def test_a_hand_edited_manifest_is_left_alone(self):
        old = media.render(self.template, *self.values())
        media.remember(self.dest, old)
        self.dest.write_text(old.replace('restart: unless-stopped', 'restart: always'))
        values = self.values(tz='Asia/Jerusalem')
        new = media.render(self.template, *values)
        with self.assertRaisesRegex(RuntimeError, 'changed by hand.*rename it'):
            media.replacement('sonarr', self.dest, self.template, new, values)


class NotificationTopicTests(unittest.TestCase):
    """A topic set in my-config.json gets its token asked; a change of topic is confirmed
    instead of failing the step on every run."""
    NEW, OLD = 'https://ntfy.sh/new-topic', 'https://ntfy.sh/old-topic'

    def ctx(self, interactive):
        from types import SimpleNamespace
        return SimpleNamespace(apply=interactive, config={'guided_setup': {'enabled': True}})

    def switch(self, interactive, current, answers=('yes',)):
        from modules import notifications
        replies = iter(answers)
        with mock.patch('sys.stdin.isatty', return_value=interactive), mock.patch('builtins.print'):
            return notifications.switch(self.ctx(interactive), self.NEW, current, 'tk_old',
                                        prompt=lambda _: next(replies), ask=lambda _: 'tk_new')

    def test_a_first_topic_from_the_settings_gets_its_token(self):
        self.assertEqual(self.switch(True, ''), (self.NEW, 'tk_new'))
        self.assertEqual(self.switch(False, ''), (self.NEW, 'tk_old'))   # nobody to ask

    def test_a_change_of_topic_is_confirmed(self):
        self.assertEqual(self.switch(True, self.OLD, ('yes',)), (self.NEW, 'tk_new'))
        kept = self.switch(True, self.OLD, ('no',))
        self.assertEqual(kept['status'], 'needs_setup')
        self.assertIn('Put it back in notifications.endpoint', kept['reason'])
        unattended = self.switch(False, self.OLD)
        self.assertEqual(unattended['status'], 'needs_setup')
        self.assertIn('--module notifications --apply', unattended['reason'])


class SourceWizardTests(unittest.TestCase):
    """A typed "yes" adds a source and copies it; it never silently counts as no."""
    def wizard(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('source_wizard', ROOT / 'source-wizard.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def collect(self, answers):
        replies = iter(answers)
        with mock.patch('builtins.print'):
            return self.wizard().collect({}, lambda _: next(replies))['sources']

    def test_yes_is_yes(self):
        sources = self.collect(['yes', 'movies', 'local', 'Old films', '/mnt/old/films', 'YES', 'no'])
        self.assertEqual(sources, [{'category': 'movies', 'type': 'local', 'name': 'Old films',
                                    'path': '/mnt/old/films', 'copy_to_library': True}])

    def test_an_unclear_answer_is_asked_again_and_enter_means_no(self):
        sources = self.collect(['sure', 'y', 'tv', 'local', 'Shows', '/mnt/shows', '', ''])
        self.assertEqual([s['copy_to_library'] for s in sources], [False])

    def test_ctrl_d_keeps_the_sources_added_so_far(self):
        def answers():
            yield from ['y', 'music', 'local', 'Songs', '/mnt/songs', 'n']
            raise EOFError
        replies = answers()
        with mock.patch('builtins.print'):
            sources = self.wizard().collect({}, lambda _: next(replies))['sources']
        self.assertEqual([s['name'] for s in sources], ['Songs'])


class JellyfinLibraryLoginTests(unittest.TestCase):
    """A mistyped Jellyfin password is something to do again, not a failed step."""
    def test_a_refused_password_needs_setup(self):
        import media_libraries

        class Refusing:
            def __init__(self):
                self.headers = {}

            def call(self, endpoint, method='GET', payload=None):
                if endpoint == '/System/Info/Public':
                    return {'Id': 'server', 'StartupWizardCompleted': True}
                if endpoint == '/Users/AuthenticateByName':
                    raise RuntimeError('jellyfin: HTTP 401')
                raise AssertionError(endpoint)

            def close(self):
                pass
        with tempfile.TemporaryDirectory() as d:
            rows = media_libraries.configure(Path(d), Path(d), True, ('jellyfin',), factory=lambda app: Refusing(),
                                             username=lambda _: 'admin', password=lambda _: 'typo')
        self.assertEqual([r['status'] for r in rows], ['needs_setup'])
        self.assertIn('did not accept that administrator username and password', rows[0]['reason'])


class ConnectorChoiceTests(unittest.TestCase):
    """A mistyped number in the connector's lists is asked again, not a failed step."""
    def choose(self, answers):
        import importlib.util
        spec = importlib.util.spec_from_file_location('media_connect', ROOT / 'media-connect.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        replies = iter(answers)

        def ask(_):
            value = next(replies)
            if value is EOFError:
                raise EOFError
            return value
        items = [{'name': 'Standard', 'id': 1}, {'name': 'Lossless', 'id': 2}]
        with mock.patch('builtins.print'):
            return module.choose(items, 'Choose', ask=ask)

    def test_a_wrong_number_is_asked_again(self):
        self.assertEqual(self.choose(['7', 'two', '2'])['id'], 2)

    def test_ctrl_d_stops_the_connector_cleanly(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('media_connect', ROOT / 'media-connect.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / 'config.json'
            config.write_text(json.dumps({'data_mount': d, 'media_mount': d, 'media': {'services': ['prowlarr']}}))
            with mock.patch.object(module.os.path, 'ismount', return_value=True), \
                    mock.patch.object(module, 'configure_indexers', side_effect=EOFError), \
                    mock.patch('builtins.print') as shown:
                self.assertEqual(module.main(['--config', str(config), '--indexers']), 1)
        self.assertIn('indexers: incomplete (stopped)', str(shown.call_args_list))

    def test_skips(self):
        self.assertIsNone(self.choose(['']))
        self.assertIsNone(self.choose([EOFError]))
        self.assertIsNone(self.choose(['0', '9', 'x']))   # three tries, then skipped


class WithoutNavidromeTests(unittest.TestCase):
    """Navidrome taken out of media.services: setup goes ahead, the music step has nothing to do."""
    def test_the_full_setup_is_not_refused(self):
        import installer
        from config_checks import validate
        from modules import music
        config = json.loads((ROOT / 'config.json').read_text())
        config['media']['services'].remove('navidrome')
        plan = installer.ordered(installer.load_modules(ROOT / 'modules'), config['profiles']['full'])
        self.assertIn('music', plan)
        validate(config, plan)
        logged = []
        ctx = mock.Mock(config=config, apply=True, log=logged.append)
        self.assertIsNone(music.run(ctx))
        self.assertIn('Navidrome is not among your apps', logged[0])


class RecoveryCheckpointReasonTests(unittest.TestCase):
    def test_the_reason_is_shown(self):
        from modules import recovery_checkpoint
        logged = []
        ctx = mock.Mock(apply=True, config={}, log=logged.append)
        with mock.patch.object(recovery_checkpoint, 'checkpoint', side_effect=RuntimeError('Docker is not running')):
            result = recovery_checkpoint.run(ctx)
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('Docker is not running', result['reason'])
        self.assertIn('Docker is not running', logged[0])


class ExistingDockerTests(unittest.TestCase):
    """Docker already used on this PC: a message that says what to do, never a move."""
    def test_another_data_root_points_to_the_guide(self):
        from modules import docker
        with tempfile.TemporaryDirectory() as d:
            daemon = Path(d) / 'daemon.json'
            daemon.write_text(json.dumps({'data-root': '/home/docker-data'}))
            target = Path('/srv/nekoserver/docker-engine')
            with self.assertRaisesRegex(RuntimeError, 'keeps its data in /home/docker-data.*STORAGE.md'):
                docker.prepare_daemon_config(daemon, target)
            self.assertEqual(json.loads(daemon.read_text()), {'data-root': '/home/docker-data'})   # untouched
        self.assertIn('## If Docker is already used on this PC', (ROOT / 'STORAGE.md').read_text())

    def test_an_earlier_installs_store_after_a_reinstall_points_to_reattach(self):
        from modules import docker
        with tempfile.TemporaryDirectory() as d:
            ssd = Path(d)
            (ssd / 'docker-engine/overlay2').mkdir(parents=True)
            default = ssd / 'var-lib-docker'   # this PC's own Docker store: empty, as after a reinstall
            with mock.patch.object(docker, 'Path', side_effect=lambda p: default if p == '/var/lib/docker' else Path(p)):
                with self.assertRaisesRegex(RuntimeError, 'not empty; refusing'):
                    docker.check_existing_docker(ssd / 'docker-engine')
                (ssd / 'installer').mkdir()
                (ssd / 'installer/host-checkpoint.json').write_text('{}')
                with self.assertRaisesRegex(RuntimeError, r'earlier NekoServer install.*RECOVERY\.md.*recover\.py reattach'):
                    docker.check_existing_docker(ssd / 'docker-engine')
        self.assertIn('## Replace the OS and reattach the existing store', (ROOT / 'RECOVERY.md').read_text())

    def test_a_reinstalled_linux_may_bring_a_newer_docker_never_an_older_one(self):
        import recovery_tools
        ok = recovery_tools.docker_new_enough
        self.assertTrue(ok('Docker version 29.9.2, build a82e528', '29.9.2'))
        self.assertTrue(ok('Docker version 30.1.0, build 1234567', '29.9.2'))
        self.assertTrue(ok('Docker version 29.10.0, build 1234567', '29.9.2'))   # 10 > 9, not text order
        self.assertTrue(ok('30.1.0', '29.9.2'))                                   # the running daemon's own answer
        self.assertFalse(ok('Docker version 29.8.5, build 1234567', '29.9.2'))
        self.assertFalse(ok('Docker version 28.0.0, build 1234567', '29.9.2'))
        self.assertTrue(ok('Docker version 26.1.5+dfsg1, build a72d7cd', '26.1.5+dfsg1'))   # Debian's own package
        # A version that can't be read must match exactly.
        self.assertTrue(ok('Docker version dev, build x', 'dev'))
        self.assertFalse(ok('Docker version dev, build x', '29.9.2'))
        self.assertFalse(ok('', '29.9.2'))


class BrowserLaunchTests(unittest.TestCase):
    """Opening a setup page never installs a browser (Firefox on Ubuntu is a snap)."""
    def test_xdg_open_is_used_and_nothing_is_installed(self):
        import guided_setup
        ctx = mock.Mock(apply=True, config={})
        which = {'xdg-open': '/usr/bin/xdg-open'}.get   # Chromium user: no firefox binary
        child = mock.Mock()
        child.wait.return_value = 0
        with mock.patch.object(guided_setup, 'interactive', return_value=True), \
                mock.patch.object(guided_setup, 'desktop_command', side_effect=lambda config, argv: argv), \
                mock.patch.object(guided_setup.shutil, 'which', side_effect=which), \
                mock.patch.object(guided_setup.host, 'current') as family, \
                mock.patch.object(guided_setup.subprocess, 'Popen', return_value=child) as started:
            self.assertTrue(guided_setup.launch(ctx, 'https://login.tailscale.com/a/1'))
        family.assert_not_called()
        self.assertEqual(started.call_args[0][0], ['/usr/bin/xdg-open', 'https://login.tailscale.com/a/1'])


class GatewayCommandReasonTests(unittest.TestCase):
    def test_a_failed_command_says_what_and_why(self):
        import gateway_tools
        failed = mock.Mock(returncode=1, stdout='', stderr='warning: x\nserve config denied: HTTPS not enabled\n')
        with mock.patch.object(gateway_tools.subprocess, 'run', return_value=failed), \
                self.assertRaisesRegex(RuntimeError, r'\(tailscale serve --bg\): serve config denied: HTTPS not enabled'):
            gateway_tools.command(['tailscale', 'serve', '--bg', '--https=8443', 'off'])


class TailscaleStartReasonTests(unittest.TestCase):
    def test_the_service_error_is_shown(self):
        from modules import tailscale
        failed = mock.Mock(returncode=1, stdout='', stderr='Failed to enable unit: Unit file tailscaled.service does not exist.\n')
        with mock.patch.object(tailscale.subprocess, 'run', return_value=failed), \
                self.assertRaisesRegex(RuntimeError, 'systemctl enable --now tailscaled.service failed: Failed to enable unit'):
            tailscale.run_cmd(['systemctl', 'enable', '--now', 'tailscaled.service'])


class OldReleaseCleanupTests(unittest.TestCase):
    """Each release deploys its own runtime copies; earlier ones are removed once no
    service uses them. The current release and anything still referenced stay."""
    def test_unused_copies_and_old_bundles_go(self):
        import native_runtime
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lib, units, data = root / 'lib', root / 'units', root / 'ssd'
            for home in ('nekoserver-dashboard', 'nekoserver-os'):
                for version in ('v0.28.8', 'v0.28.9', 'v' + neko_common.VERSION):
                    (lib / home / version).mkdir(parents=True)
                    (lib / home / version / 'nekoserver-runtime').write_bytes(b'x')
            for version in ('v0.28.7', 'v0.28.8', 'v0.28.9', 'v' + neko_common.VERSION):
                (data / 'installer/runtime' / version).mkdir(parents=True)
                (data / 'installer/guards' / version).mkdir(parents=True)
                (data / 'installer/releases' / version).mkdir(parents=True)
            (data / 'installer/releases/notes').mkdir()
            units.mkdir()
            # The heartbeat was never set up again: its service still runs 0.28.9's copy.
            (units / 'nekoserver-heartbeat.service').write_text(f'ExecStart={lib}/nekoserver-os/v0.28.9/nekoserver-runtime heartbeat\n')
            logged = []
            removed = native_runtime.retire_old(data, logged.append, lib=lib, units=units)
            self.assertEqual(sorted(p.name for p in removed if 'nekoserver-dashboard' in str(p)), ['v0.28.8', 'v0.28.9'])
            self.assertEqual(sorted(p.name for p in removed if 'nekoserver-os' in str(p)), ['v0.28.8'])   # 0.28.9 in use
            self.assertTrue((lib / 'nekoserver-os/v0.28.9').is_dir())
            self.assertTrue((lib / 'nekoserver-dashboard' / ('v' + neko_common.VERSION)).is_dir())
            for home in ('installer/runtime', 'installer/guards'):
                self.assertEqual(sorted(p.name for p in (data / home).iterdir()), ['v' + neko_common.VERSION])
            self.assertEqual(sorted(p.name for p in (data / 'installer/releases').iterdir()), sorted(['notes', 'v0.28.9', 'v' + neko_common.VERSION]))
            self.assertIn('earlier releases', logged[0])
            # Nothing more to do the second time.
            self.assertEqual(native_runtime.retire_old(data, logged.append, lib=lib, units=units), [])

    def test_the_docker_start_guard_named_in_a_drop_in_stays(self):
        # A setup run without the Docker step (only the shared folders, say) after a new
        # release was downloaded: docker.service.d still names the previous release's guard,
        # and Docker does not start without it.
        import native_runtime
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lib, units, data = root / 'lib', root / 'units', root / 'ssd'
            for version in ('v0.28.9', 'v' + neko_common.VERSION):
                (data / 'installer/guards' / version).mkdir(parents=True)
            (units / 'docker.service.d').mkdir(parents=True)
            (units / 'docker.service.d/10-nekoserver-storage.conf').write_text(
                f'[Service]\nExecStartPre={data}/installer/guards/v0.28.9/nekoserver-runtime storage-guard\n')
            self.assertEqual(native_runtime.retire_old(data, lambda m: None, lib=lib, units=units), [])
            self.assertTrue((data / 'installer/guards/v0.28.9').is_dir())

    def test_versions_sort_by_number_not_text(self):
        import native_runtime
        self.assertLess(native_runtime.release_number('v0.28.9'), native_runtime.release_number('v0.28.11'))
        self.assertIsNone(native_runtime.release_number('notes'))


class TvUserTests(unittest.TestCase):
    """The TV stays set up for the first person, whoever runs setup later."""
    def test_the_report_decides_on_later_runs(self):
        from modules import tv
        with tempfile.TemporaryDirectory() as d:
            config = {'data_mount': d, 'tv': {'user': 'auto'}}
            self.assertIsNone(tv.previous_user(config))
            (Path(d) / 'reports').mkdir()
            (Path(d) / tv.REPORT).write_text(json.dumps({'user': 'neko', 'checks': []}))
            self.assertEqual(tv.previous_user(config), 'neko')
            (Path(d) / tv.REPORT).write_text(json.dumps({'user': '../root'}))
            self.assertIsNone(tv.previous_user(config))
            self.assertIsNone(tv.previous_user({**config, 'data_mount': d + '/missing'}))


class SetupDownloadTests(unittest.TestCase):
    """A download setup needs (Immich's or Nextcloud's release, Immich's compose file)
    fails with a plain reason, not "<urlopen error ...>"."""
    def failing(self, error):
        def opener(request, timeout=0):
            raise error
        return opener

    def test_offline_limited_and_gone(self):
        import urllib.error
        api = 'https://api.github.com/repos/immich-app/immich/releases/latest'
        offline = self.failing(urllib.error.URLError('Temporary failure in name resolution'))
        with self.assertRaisesRegex(RuntimeError, 'Finding the current Immich release: could not download .*Temporary failure.*connected to the internet'):
            neko_common.fetch(api, 'Finding the current Immich release', 1000, opener=offline)
        limited = self.failing(urllib.error.HTTPError(api, 403, 'rate limit exceeded', {}, None))
        with self.assertRaisesRegex(RuntimeError, 'GitHub is limiting requests.*wait an hour'):
            neko_common.fetch(api, 'Finding the current Immich release', 1000, opener=limited)
        gone = 'https://github.com/immich-app/immich/releases/download/v9.9.9/docker-compose.yml'
        missing = self.failing(urllib.error.HTTPError(gone, 404, 'Not Found', {}, None))
        with self.assertRaisesRegex(RuntimeError, 'answered HTTP 404'):
            neko_common.fetch(gone, 'Downloading Immich v9.9.9\'s compose file', 1000, opener=missing)

    def test_a_good_answer_is_returned_with_a_user_agent(self):
        seen = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, limit):
                return b'{"tag_name": "v1.2.3"}'
        def opener(request, timeout=0):
            seen.append(request.get_header('User-agent'))
            return Reply()
        self.assertEqual(neko_common.fetch('https://api.github.com/x', 'x', 100, opener=opener), b'{"tag_name": "v1.2.3"}')
        self.assertEqual(seen, ['NekoServer-installer'])


class AccountSetupReasonTests(unittest.TestCase):
    """An app login that could not be verified says why (never the password)."""
    def test_the_reason_is_in_the_log(self):
        import account_setup

        class Client:
            def __init__(self):
                self.headers = {}

            def close(self):
                pass
        entry = {'username': 'admin', 'password': 'hunter2hunter2', 'state': 'planned', 'allow_create': False}
        logged = []
        with mock.patch.object(account_setup.store, 'get', return_value=entry), \
                mock.patch.object(account_setup.store, 'update'), \
                mock.patch.object(account_setup, 'verify_project'), \
                mock.patch.object(account_setup, 'wait', side_effect=account_setup.HTTPFailure(503)):
            pending = account_setup.provision({'data_mount': '/srv/x'}, ['jellyfin'], output=logged.append, client_factory=lambda app: Client())
        self.assertEqual(pending, ['jellyfin'])
        self.assertIn('Jellyfin administrator: not verified (Local account API request failed (HTTP 503)', logged[-1])
        self.assertNotIn('hunter2', ' '.join(logged))


class ImageDownloadTests(unittest.TestCase):
    """Images are downloaded before an app is started, with hours for a slow connection."""
    def compose(self, config, image_present=True):
        """compose_up against a fake docker: records the pull and up calls."""
        calls = []

        def run(argv, timeout, what):
            if argv[-3:] == ['config', '--format', 'json']:
                return mock.Mock(stdout=json.dumps(config))
            calls.append((argv[argv.index('compose') + 5:] if 'compose' in argv else argv, timeout, what))
            return mock.Mock(stdout='')
        with mock.patch.object(neko_common, 'checked_run', side_effect=run),                 mock.patch.object(neko_common.subprocess, 'run', return_value=mock.Mock(returncode=0 if image_present else 1)):
            neko_common.compose_up(['docker', 'compose', '-p', 'neko-immich', '-f', 'c.yaml'], 'Immich', timeout=1200)
        return calls

    def test_pull_comes_first_with_a_long_limit(self):
        calls = self.compose({'services': {'server': {'image': 'ghcr.io/immich-app/immich-server:v3'}, 'redis': {'image': 'valkey:8'}}})
        self.assertEqual(calls, [(['pull', '--quiet', 'redis', 'server'], 4 * 3600, 'Immich (docker compose pull)'),
                                 (['up', '-d'], 1200, 'Immich (docker compose up)')])

    def test_images_pinned_by_the_monthly_update_are_not_pulled(self):
        pinned = {'services': {'server': {'image': 'sha256:' + 'a' * 64}, 'redis': {'image': 'sha256:' + 'b' * 64}}}
        self.assertEqual(self.compose(pinned), [(['up', '-d'], 1200, 'Immich (docker compose up)')])
        mixed = {'services': {'server': {'image': 'sha256:' + 'a' * 64}, 'redis': {'image': 'valkey:8'}}}
        self.assertEqual(self.compose(mixed)[0][0], ['pull', '--quiet', 'redis'])
        with self.assertRaisesRegex(RuntimeError, 'pinned for redis .* is not on this PC'):
            self.compose(pinned, image_present=False)

    def test_a_busy_registry_is_tried_again_after_a_wait(self):
        answers = iter([RuntimeError('x (docker compose pull) failed: Error response from daemon: toomanyrequests: '
                                     'retry-after: 351.19µs, allowed: 44000/minute'),
                        RuntimeError('x failed: TLS handshake timeout'), 'ok'])
        slept, logged = [], []

        def run(argv, timeout, what):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(neko_common, 'checked_run', side_effect=run):
            result = neko_common.pull_images(['docker', 'compose', 'pull'], 'Immich', logged.append, sleep=slept.append)
        self.assertEqual(result, 'ok')
        self.assertEqual(slept, [30, 60])
        self.assertEqual(len(logged), 2)
        self.assertIn('trying again in 30 seconds (1 of 5)', logged[0])
        self.assertIn('toomanyrequests', logged[0])   # the registry's own reason is shown

    def test_other_pull_failures_and_a_registry_that_stays_busy_still_fail(self):
        with mock.patch.object(neko_common, 'checked_run', side_effect=RuntimeError('x failed: no space left on device')):
            with self.assertRaises(RuntimeError):
                neko_common.pull_images(['docker', 'compose', 'pull'], 'Immich', sleep=lambda s: None)
        slept = []
        with mock.patch.object(neko_common, 'checked_run', side_effect=RuntimeError('x failed: toomanyrequests')):
            with self.assertRaises(RuntimeError):
                neko_common.pull_images(['docker', 'compose', 'pull'], 'Immich', sleep=slept.append)
        self.assertEqual(slept, [30, 60, 120, 240, 480])

    def test_a_failure_message_is_cut_at_a_line_not_mid_word(self):
        output = 'Image a Pulling\n' + 'x' * 600 + '\nError response from daemon: toomanyrequests: retry-after: 1ms\n'
        words = neko_common.last_words(output)
        self.assertTrue(words.startswith('Error response from daemon'))
        self.assertEqual(neko_common.last_words(''), 'no details')
        self.assertEqual(neko_common.last_words('one\n\ntwo\n'), 'one\ntwo')


class GatewayPortTests(unittest.TestCase):
    """A home-address port another program has is replaced by a free spare, kept for
    later runs and used everywhere the address is built."""
    def config(self):
        return {'data_mount': '/nonexistent', 'gateway': {'lan_ip': '127.0.0.1', 'lan_cidr': '127.0.0.0/8', 'services': ['jellyfin', 'nextcloud']}}

    def setUp(self):
        import gateway_tools
        patcher = mock.patch.object(gateway_tools, 'selected', side_effect=lambda config: list(config['gateway']['services']))
        patcher.start()
        self.addCleanup(patcher.stop)

    def choose(self, config, busy):
        import gateway_tools
        saved, logged = [], []
        ctx = mock.Mock(config=config, log=logged.append, persist_config=lambda: saved.append(dict(config['gateway'])))
        with mock.patch.object(gateway_tools, 'lan', return_value=('127.0.0.1', '127.0.0.0/8')), \
                mock.patch.object(gateway_tools, 'owned_listeners', return_value=set()), \
                mock.patch.object(gateway_tools, 'port_free', side_effect=lambda ip, port: port not in busy):
            ports = gateway_tools.choose_lan_ports(ctx)
        return ports, saved, logged

    def test_usual_ports_when_free(self):
        config = self.config()
        ports, saved, logged = self.choose(config, busy=set())
        self.assertEqual(ports, {'portal': 8442, 'jellyfin': 8443, 'nextcloud': 8447})
        self.assertEqual((saved, logged), ([], []))
        self.assertNotIn('lan_ports', config['gateway'])

    def test_a_taken_port_gets_a_spare_that_is_kept_and_used_everywhere(self):
        import gateway_tools
        config = self.config()
        ports, saved, logged = self.choose(config, busy={8442, 8447, 8450})
        self.assertEqual(ports, {'portal': 8451, 'jellyfin': 8443, 'nextcloud': 8452})
        self.assertEqual(config['gateway']['lan_ports'], ports)
        self.assertEqual(len(saved), 1)
        self.assertIn('port 8447 is used by another program on this PC, so Nextcloud uses port 8452', logged[1])
        # The next run keeps the choice even though 8447 is free again.
        ports2, saved2, _ = self.choose(config, busy=set())
        self.assertEqual((ports2, saved2), (ports, []))
        # The app menu page, the gateway's listeners and the report use the chosen ports.
        self.assertIn('https://192.168.1.20:8452/', gateway_tools.portal(config, '192.168.1.20'))
        with mock.patch.object(gateway_tools, 'lan', return_value=('192.168.1.20', '192.168.1.0/24')):
            listens = {name: server['listen'] for name, server in gateway_tools.lan_model(config, Path('/web'))['apps']['http']['servers'].items()}
        self.assertEqual(listens, {'portal': ['192.168.1.20:8451'], 'jellyfin': ['192.168.1.20:8443'], 'nextcloud': ['192.168.1.20:8452']})

    def test_settings_are_checked(self):
        import gateway_tools
        config = self.config()
        config['gateway']['lan_ports'] = {'portal': 8443}   # same as Jellyfin's
        with self.assertRaisesRegex(RuntimeError, 'two apps have the same port'):
            gateway_tools.lan_ports(config)
        config['gateway']['lan_ports'] = {'portal': '8450'}
        with self.assertRaisesRegex(RuntimeError, 'must be a port number'):
            gateway_tools.lan_ports(config)


class GatewayConfirmationTests(unittest.TestCase):
    """Logins the accounts step verified need no second, manual sign-in check."""
    def configure(self, verified, interactive):
        import gateway_tools
        config = {'gateway': {'services': ['jellyfin', 'immich']}, 'accounts': {'enabled': True}}
        saved = []
        ctx = mock.Mock(config=config, apply=True, log=lambda m: None, persist_config=lambda: saved.append(dict(config['gateway'])))
        entry = {'state': 'verified'} if verified else None
        with mock.patch.object(gateway_tools, 'selected', return_value=['jellyfin', 'immich']), \
                mock.patch('account_store.get', return_value=entry), \
                mock.patch('sys.stdin.isatty', return_value=interactive), \
                mock.patch.object(gateway_tools.guided_setup, 'app_security', side_effect=AssertionError('asked')):
            return gateway_tools.configure(ctx, 'tailnet'), config['gateway'].get('confirmed_services')

    def test_verified_logins_confirm_the_apps_by_themselves(self):
        self.assertEqual(self.configure(verified=True, interactive=False), (True, ['jellyfin', 'immich']))

    def test_unverified_logins_still_wait_for_a_person(self):
        self.assertEqual(self.configure(verified=False, interactive=False), (False, None))


class StackedMountTests(unittest.TestCase):
    """Two mounts on one folder: the one in effect (listed last) is what counts."""
    def test_the_last_line_is_the_mount_in_effect(self):
        def run(argv, **kw):
            return mock.Mock(returncode=0, stdout='/dev/sdb1\n/dev/sdc1\n')
        self.assertEqual(neko_common.findmnt('SOURCE', '/srv/x', run=run), '/dev/sdc1')
        self.assertIsNone(neko_common.findmnt('SOURCE', '/srv/x', run=lambda argv, **kw: mock.Mock(returncode=1, stdout='')))
        self.assertIsNone(neko_common.findmnt('SOURCE', '/srv/x', run=mock.Mock(side_effect=OSError)))
        from modules import storage
        self.assertEqual(storage.mounted_subvolume('/srv/x', run=lambda argv, **kw: mock.Mock(returncode=0, stdout='/dev/sdb1\n/dev/sdb1[/@data]\n')), '/@data')


class NextcloudImageTests(unittest.TestCase):
    def test_new_major_waits_for_early_fixes(self):
        from modules import nextcloud
        pick = lambda tag: nextcloud.image_for_release({'tag_name': tag})
        self.assertEqual(pick('v32.0.0'), 'nextcloud:31-apache')
        self.assertEqual(pick('v32.0.1'), 'nextcloud:31-apache')
        self.assertEqual(pick('v32.0.2'), 'nextcloud:32-apache')
        self.assertEqual(pick('v32.1.0'), 'nextcloud:32-apache')
        for bad in ({'tag_name': 'v32.0.3', 'prerelease': True}, {'tag_name': 'nightly'}):
            with self.assertRaises(RuntimeError):
                nextcloud.image_for_release(bad)


class ModuleDependencyTests(unittest.TestCase):
    def test_monthly_updates_do_not_need_the_usb_drive(self):
        import installer
        from modules import maintenance
        self.assertNotIn('photo_backup', maintenance.DEPENDENCIES)
        modules = installer.load_modules(ROOT / 'modules')
        self.assertNotIn('photo_backup', installer.ordered(modules, ['maintenance']))


class UpgradeMigrationTests(unittest.TestCase):
    """Servers installed with v0.21.0 must upgrade in place instead of refusing."""

    FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'v0.21.0'

    def previous(self, name):
        template = (ROOT / 'compose' / f'{name}.yaml').read_text()
        return media.previous_templates(name, template)

    def test_v0210_lidarr_and_seerr_are_recognised_exactly(self):
        for name in ('lidarr', 'seerr'):
            with self.subTest(name=name):
                shipped = (self.FIXTURES / f'{name}.yaml').read_text()
                olds = [old for old, _ in self.previous(name)]
                self.assertIn(shipped, olds)
                self.assertEqual(self.previous(name)[0][1], 'compose.before-v0.21.1.yaml')

    def test_v0210_seerr_with_non_default_uid_is_migrated(self):
        shipped = (self.FIXTURES / 'seerr.yaml').read_text()
        installed = media.render(shipped, '/srv/nekoserver', '/mnt/nekoserver-media', 1234, 2345, 'UTC', '127.0.0.1')
        olds = [media.render(old, '/srv/nekoserver', '/mnt/nekoserver-media', 1234, 2345, 'UTC', '127.0.0.1')
                for old, _ in self.previous('seerr')]
        self.assertIn(installed, olds)
        self.assertIn('user: "1234:2345"', render('seerr.yaml', uid=1234, gid=2345))

    def test_current_navidrome_template_has_no_pending_migration_from_itself(self):
        current = (ROOT / 'compose' / 'navidrome.yaml').read_text()
        self.assertEqual(current, (self.FIXTURES / 'navidrome.yaml').read_text())
        self.assertNotIn(current, [old for old, _ in self.previous('navidrome')])

    def test_locally_edited_manifest_is_not_recognised(self):
        edited = (self.FIXTURES / 'lidarr.yaml').read_text().replace('restart: unless-stopped', 'restart: always')
        self.assertNotIn(edited, [old for old, _ in self.previous('lidarr')])

    def test_unchanged_services_have_no_migrations(self):
        for name in ('jellyfin', 'sonarr', 'radarr', 'prowlarr', 'qbittorrent'):
            with self.subTest(name=name):
                self.assertEqual(self.previous(name), [])



class OnboardingCompletionTests(unittest.TestCase):
    """v0.25.1: witnessed playback never blocks setup; it is recorded or shown as pending."""

    def test_only_automatic_checks_decide_the_onboarding_result(self):
        import onboard
        rows = [{'check': 'jellyfin:current_libraries', 'status': 'pass'},
                {'check': 'end_to_end_media', 'status': 'needs_setup'},
                {'check': 'navidrome:authenticated_playback_or_transfer', 'status': 'needs_setup'}]
        self.assertEqual(onboard.readiness_exit(rows), 0)
        self.assertEqual(onboard.readiness_exit(rows + [{'check': 'seerr:current_libraries', 'status': 'needs_setup'}]), 1)
        self.assertEqual(onboard.readiness_exit(rows + [{'check': 'x', 'status': 'fail'}]), 2)

    def test_apps_with_nothing_to_connect_still_finish_onboarding(self):
        """Only Navidrome, or only qBittorrent: setup asks for --configure as always, and
        onboarding checks their accounts and readiness instead of stopping."""
        import onboard
        for apps in (['navidrome'], ['qbittorrent']):
            with self.subTest(apps=apps), tempfile.TemporaryDirectory() as d:
                root, media = Path(d) / 'ssd', Path(d) / 'hdd'
                root.mkdir()
                media.mkdir()
                config = Path(d) / 'config.json'
                config.write_text(json.dumps({'data_mount': str(root), 'media_mount': str(media), 'media': {'services': apps}}))
                with mock.patch.object(onboard, 'mount_ok', return_value=True), \
                        mock.patch.object(onboard, 'report', return_value=[{'check': apps[0], 'status': 'pass'}]), \
                        mock.patch.object(onboard.subprocess, 'call') as call, \
                        mock.patch.object(onboard.media_readiness, 'check', return_value=[]), \
                        mock.patch('builtins.print'):
                    result = onboard.main(['--config', str(config), '--configure', '--apply'])
                self.assertEqual(result, 0)
                call.assert_not_called()   # no connector program for apps without connections
                saved = json.loads((root / 'reports/onboarding.json').read_text())
                self.assertNotIn('connector', json.dumps(saved))

    def test_a_confirmed_playback_is_recorded_and_a_skipped_one_is_only_noted(self):
        from modules import onboarding
        import wizard
        with tempfile.TemporaryDirectory() as d:
            logs = []
            ctx = mock.Mock(config={'data_mount': d, 'media': {'services': ['jellyfin']}}, log=logs.append)

            def skip(*a, **k):
                raise KeyboardInterrupt
            onboarding.witness_playback(ctx, checkpoint=skip)
            self.assertFalse(onboarding.playback_record(ctx.config).exists())
            self.assertIn('Playback verification pending', logs[-1])
            out = []
            states = {'onboarding': {'status': 'success'}}
            wizard.finishing_screen({'data_mount': d, 'profiles': {'full': ['onboarding']}}, states, output=out.append)
            text = '\n'.join(out)
            self.assertIn('NekoServer is ready!', text)
            self.assertIn('Playback verification pending', text)
            onboarding.witness_playback(ctx, checkpoint=lambda *a, **k: True)
            self.assertTrue(onboarding.playback_record(ctx.config).exists())
            out = []
            wizard.finishing_screen({'data_mount': d, 'profiles': {'full': ['onboarding']}}, states, output=out.append)
            self.assertNotIn('Playback verification pending', '\n'.join(out))


class DashboardFingerprintTests(unittest.TestCase):
    def test_settings_chosen_after_the_dashboard_step_reach_its_collector(self):
        from modules import dashboard
        from acceptance_summary import scope_digest
        with tempfile.TemporaryDirectory() as d:
            cfg = {'data_mount': d, 'media_mount': d, 'external_backup_mount': d, 'storage': {},
                   'media': {'services': ['jellyfin']}, 'tv': {'wanted': False}}
            target = Path(d) / 'collector.json'
            logs = []
            self.assertFalse(dashboard.refresh(cfg, logs.append, target))  # not installed: nothing
            neko_common.atomic_json(target, dashboard.collector_settings(cfg, 1800))
            cfg['tv'] = {'wanted': True}  # answered later in setup
            self.assertTrue(dashboard.refresh(cfg, logs.append, target))
            import json
            self.assertEqual(json.loads(target.read_text())['acceptance_scope'], scope_digest(cfg))
            self.assertFalse(dashboard.refresh(cfg, logs.append, target))  # already current


class JellyfinOnlyReadinessTests(unittest.TestCase):
    def test_jellyfin_without_seerr_uses_its_own_key(self):
        import media_readiness
        import media_libraries

        class Fake:
            def __init__(self, app):
                self.headers = {}

            def call(self, endpoint, method='GET', payload=None):
                if endpoint == '/System/Info/Public':
                    return {'Id': 'srv', 'StartupWizardCompleted': True}
                assert self.headers.get('X-Emby-Token') == 'own-key'
                return [{'Name': n, 'CollectionType': k, 'ItemId': n, 'Locations': ['/data/' + c]}
                        for c, n, k in media_libraries.LIBRARIES]

            def close(self):
                pass
        with tempfile.TemporaryDirectory() as d:
            cfg = {'data_mount': d, 'media_mount': d, 'media': {'services': ['jellyfin']}}
            rows = media_readiness.check(cfg, factory=Fake)
            first = next(r for r in rows if r['check'] == 'jellyfin:current_libraries')
            self.assertEqual(first['status'], 'needs_setup')
            self.assertNotIn('Seerr', first['reason'])
            key = media_libraries.readiness_key_path(d)
            key.parent.mkdir(parents=True)
            key.write_text('own-key\n')
            with mock.patch.object(media_libraries, 'matching', lambda values, c, n, k: next(v for v in values if v['Name'] == n)):
                with mock.patch.object(media_readiness, 'matching', lambda values, c, n, k: next(v for v in values if v['Name'] == n)):
                    rows = media_readiness.check(cfg, factory=Fake)
            self.assertEqual(next(r for r in rows if r['check'] == 'jellyfin:current_libraries')['status'], 'pass')

    def test_jellyfin_key_matches_the_shared_fixture(self):
        """The Rust runtime runs the same cases (media.rs jellyfin_key_matches_the_shared_fixture)."""
        import json
        import media_readiness
        import media_libraries
        cases = json.loads((Path(__file__).parent / 'fixtures/jellyfin_key_cases.json').read_text())
        for case in cases:
            with self.subTest(case['name']), tempfile.TemporaryDirectory() as d:
                if case['own_key'] is not None:
                    key = media_libraries.readiness_key_path(d)
                    key.parent.mkdir(parents=True)
                    key.write_text(case['own_key'])
                if case['expected'] is None:
                    with self.assertRaises(media_readiness.SetupRequired):
                        media_readiness.jellyfin_key(d, case['settings'], case['server_id'])
                else:
                    self.assertEqual(media_readiness.jellyfin_key(d, case['settings'], case['server_id']), case['expected'])

    def test_the_key_is_created_once_while_signed_in(self):
        import media_libraries
        calls = []

        class Admin:
            keys = []

            def call(self, endpoint, method='GET', payload=None):
                calls.append((method, endpoint))
                if method == 'POST':
                    Admin.keys.append({'AppName': media_libraries.READINESS_APP, 'AccessToken': 'new-key'})
                    return {}
                return {'Items': list(Admin.keys)}
        with tempfile.TemporaryDirectory() as d:
            media_libraries.ensure_readiness_key(Admin(), d)
            path = media_libraries.readiness_key_path(d)
            self.assertEqual(path.read_text().strip(), 'new-key')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            media_libraries.ensure_readiness_key(Admin(), d)  # still active: kept, no second key
            self.assertEqual(sum(1 for m, _ in calls if m == 'POST'), 1)
            self.assertEqual(path.read_text().strip(), 'new-key')

    def test_a_revoked_or_replaced_key_is_renewed_by_setup(self):
        import media_libraries
        logs, posts = [], []

        class Admin:
            def __init__(self, keys):
                self.keys = keys

            def call(self, endpoint, method='GET', payload=None):
                if method == 'POST':
                    posts.append(endpoint)
                    self.keys.append({'AppName': media_libraries.READINESS_APP, 'AccessToken': 'fresh-key'})
                    return {}
                return {'Items': list(self.keys)}
        with tempfile.TemporaryDirectory() as d:
            path = media_libraries.readiness_key_path(d)
            path.parent.mkdir(parents=True)
            path.write_text('revoked-key\n')
            # Replaced: Jellyfin has another active NekoServer key; that one is saved, none created.
            media_libraries.ensure_readiness_key(Admin([{'AppName': media_libraries.READINESS_APP, 'AccessToken': 'other-key'},
                                                        {'AppName': 'Seerr', 'AccessToken': 'revoked-key'}]), d, logs.append)
            self.assertEqual(path.read_text().strip(), 'other-key')
            self.assertEqual(posts, [])
            # Revoked with no NekoServer key left: a new one is created and saved.
            media_libraries.ensure_readiness_key(Admin([]), d, logs.append)
            self.assertEqual(path.read_text().strip(), 'fresh-key')
            self.assertEqual(len(posts), 1)
            self.assertIn('revoked or replaced', logs[0])

    def test_a_rejected_key_is_reported_not_changed_by_checks(self):
        import media_readiness
        import media_libraries

        class Jellyfin:
            def __init__(self, app):
                self.headers = {}

            def call(self, endpoint, method='GET', payload=None):
                if endpoint == '/System/Info/Public':
                    return {'Id': 'srv', 'StartupWizardCompleted': True}
                raise RuntimeError('jellyfin: HTTP 401')

            def close(self):
                pass
        with tempfile.TemporaryDirectory() as d:
            path = media_libraries.readiness_key_path(d)
            path.parent.mkdir(parents=True)
            path.write_text('revoked-key\n')
            rows = media_readiness.check({'data_mount': d, 'media_mount': d, 'media': {'services': ['jellyfin']}}, factory=Jellyfin)
            first = next(r for r in rows if r['check'] == 'jellyfin:current_libraries')
            self.assertEqual(first['status'], 'needs_setup')
            self.assertIn('run  sudo bash install.sh  again to renew it', first['reason'])
            self.assertEqual(path.read_text(), 'revoked-key\n')  # untouched


class DockerLogTests(unittest.TestCase):
    def test_log_limits_are_added_without_overriding_your_choices(self):
        from modules import docker
        self.assertEqual(docker.bound_logs({})['log-opts'], {'max-size': '10m', 'max-file': '3'})
        self.assertEqual(docker.bound_logs({'log-opts': {'max-size': '50m'}})['log-opts'],
                         {'max-size': '50m', 'max-file': '3'})
        self.assertNotIn('log-opts', docker.bound_logs({'log-driver': 'journald'}))
        with tempfile.TemporaryDirectory() as d:
            daemon = Path(d) / 'daemon.json'
            daemon.write_text('{"log-driver": "local"}')
            content = docker.prepare_daemon_config(daemon, Path(d) / 'docker-engine')
            self.assertEqual(content['log-driver'], 'local')
            self.assertNotIn('log-opts', content)

    def test_containers_without_a_log_limit_are_listed(self):
        from modules import docker
        inspected = [
            {'Name': '/neko-jellyfin', 'HostConfig': {'LogConfig': {'Type': 'json-file', 'Config': {}}}},
            {'Name': '/neko-sonarr', 'HostConfig': {'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '10m'}}}},
            {'Name': '/neko-immich', 'HostConfig': {'LogConfig': {'Type': 'journald', 'Config': {}}}},
        ]
        self.assertEqual(docker.unbounded_logs(inspected), ['neko-jellyfin'])


class StrictImportTests(unittest.TestCase):
    """v0.25.1: an unreadable folder in a media source makes the import incomplete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.media = root / 'music-source', root / 'library-root'
        (self.source / 'Album').mkdir(parents=True)
        (self.source / 'single.flac').write_bytes(b'single')
        (self.source / 'Album/track.flac').write_bytes(b'track')
        (self.source / 'still-downloading.flac.part').write_bytes(b'partial')
        self.media.mkdir()

    def walk_failing_at(self, name):
        real = os.walk

        def walk(top, onerror=None, **kw):
            for folder, dirs, files in real(top, **kw):
                if name in dirs:
                    dirs.remove(name)
                    if onerror:  # without a handler os.walk drops it silently
                        onerror(PermissionError(13, 'Permission denied', str(Path(folder) / name)))
                yield folder, dirs, files
        return mock.patch.object(neko_common.os, 'walk', walk)

    def test_an_unreadable_album_stops_the_import_and_is_named(self):
        import source_import
        with self.walk_failing_at('Album'):
            with self.assertRaises(source_import.IncompleteSource) as caught:
                source_import.copy_source({'type': 'local', 'path': str(self.source), 'category': 'music'},
                                          self.media, apply=True)
        self.assertEqual(caught.exception.paths, ['Album'])
        self.assertFalse((self.media / 'library').exists())  # nothing copied
        self.assertEqual((self.source / 'Album/track.flac').read_bytes(), b'track')  # originals untouched
        # The report says what could not be read, relative to the source only.
        with self.walk_failing_at('Album'), \
                mock.patch.object(source_import.os.path, 'ismount', return_value=True):
            config = {'media_mount': str(self.media), 'data_mount': str(self.media), 'media': {}}
            self.assertEqual(source_import.run(config, [{'type': 'local', 'path': str(self.source), 'category': 'music'}]), 1)
        import json
        row = json.loads((self.media / 'reports/source-import.json').read_text())['sources'][0]
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('Album', row['reason'])
        self.assertNotIn(str(self.source), row['reason'])

    def test_a_readable_source_is_copied_without_temporary_files(self):
        import source_import
        result = source_import.copy_source({'type': 'local', 'path': str(self.source), 'category': 'music'},
                                           self.media, apply=True)
        self.assertEqual(result['copied'], 2)
        copied = sorted(p.name for p in Path(result['destination']).rglob('*') if p.is_file())
        self.assertEqual(copied, ['single.flac', 'track.flac'])

    def mount_table(self, mount_id=40, mounted=True):
        """A stand-in for the kernel's mount table (/proc/self/mountinfo)."""
        point = str(self.media).replace(' ', '\\040')
        table = Path(self.tmp.name) / 'mountinfo'
        table.write_text('22 1 8:1 / / rw - ext4 /dev/sda1 rw\n'
                         + (f'{mount_id} 22 8:17 / {point} rw - ext4 /dev/sdb1 rw\n' if mounted else ''))
        return table

    def test_an_unmounted_drive_stops_the_import_before_the_next_file(self):
        """The drive drops out after the first file: nothing more is written into the
        folder underneath (the system disk), and the whole import stops."""
        import source_import
        real_publish = source_import.publish_no_replace

        def publish_then_unmount(temp, dest):
            real_publish(temp, dest)
            self.mount_table(mounted=False)      # the USB cable is pulled, say
        with mock.patch.object(source_import, 'MOUNT_TABLE', self.mount_table()), \
                mock.patch.object(source_import.os.path, 'ismount', return_value=True), \
                mock.patch.object(source_import, 'publish_no_replace', side_effect=publish_then_unmount), \
                mock.patch.object(source_import.os, 'geteuid', return_value=0), \
                mock.patch.object(source_import, 'ensure_directory', side_effect=lambda path, *a: Path(path).mkdir(parents=True, exist_ok=True)):
            # Files owned by this test's own user: CI runs the tests without root.
            owner = {'uid': os.getuid() or 1000, 'gid': os.getgid() or 1000}
            config = {'media_mount': str(self.media), 'data_mount': str(self.media), 'media': owner}
            source = {'type': 'local', 'path': str(self.source), 'category': 'music'}
            with self.assertRaises(source_import.DriveGone):
                source_import.run(config, [source, source], apply=True)
        copied = list((self.media / 'library').rglob('*.flac'))
        self.assertEqual(len(copied), 1)
        self.assertFalse(any(p.name.endswith('.neko-part') for p in self.media.rglob('*')))

    def test_another_drive_mounted_in_its_place_is_noticed(self):
        import source_import
        with mock.patch.object(source_import, 'MOUNT_TABLE', self.mount_table(40)):
            guard = source_import.DriveGuard(self.media)
            guard.check(self.media)
            self.mount_table(41)       # unplugged and plugged in again, or another drive
            with self.assertRaises(source_import.DriveGone):
                guard.check(self.media)
            self.mount_table(mounted=False)
            with self.assertRaises(source_import.DriveGone):
                source_import.DriveGuard(self.media)


class ClockAndUsbPrivacyTests(unittest.TestCase):
    def test_a_future_full_check_record_does_not_postpone_checks(self):
        from modules import capacity
        now = 1_800_000_000.0
        self.assertFalse(capacity.due_since(now - 86400, now))
        self.assertTrue(capacity.due_since(now - 28 * 86400, now))
        self.assertTrue(capacity.due_since(0.0, now))
        self.assertTrue(capacity.due_since(float('nan'), now))
        self.assertTrue(capacity.due_since(1_900_000_000.0, now))            # recorded in "2030"
        self.assertTrue(capacity.due_since(1_900_000_000.0, now + 28 * 86400))
        self.assertFalse(capacity.due_since(now + 60, now))                    # small clock difference

    def test_exfat_backup_drives_are_mounted_for_root_only(self):
        self.assertEqual(neko_common.usb_mount_options('ext4', True), 'rw,nosuid,nodev,noexec')
        options = neko_common.usb_mount_options('exfat', True)
        self.assertIn('fmask=0177', options)
        self.assertTrue(neko_common.private_mount_options(options, 'exfat'))
        readable = 'rw,nosuid,nodev,relatime,uid=0,gid=0,fmask=0022,dmask=0022,iocharset=utf8'
        self.assertFalse(neko_common.private_mount_options(readable, 'exfat'))
        self.assertFalse(neko_common.private_mount_options('rw,uid=1000,gid=1000,fmask=0177,dmask=0077', 'exfat'))
        self.assertTrue(neko_common.private_mount_options('rw,relatime', 'ext4'))

    def test_the_restore_helper_refuses_a_readable_exfat_mount(self):
        import recovery_wizard
        with tempfile.TemporaryDirectory() as d:
            config = {'external_backup_mount': d, 'photos': {'external_backup_uuid': 'AB-CD', 'external_backup_fstype': 'exfat'}}

            def run(argv, **kw):
                out = 'AB-CD rw,relatime,uid=0,gid=0,fmask=0022,dmask=0022' if 'UUID,OPTIONS' in argv else ''
                return mock.Mock(returncode=0, stdout=out)
            with mock.patch.object(recovery_wizard, 'USB_LOCK', Path(d) / 'lock'), \
                    mock.patch.object(recovery_wizard.subprocess, 'run', run), \
                    mock.patch.object(neko_common.subprocess, 'run', run):
                with self.assertRaisesRegex(RuntimeError, 'other accounts'):
                    with recovery_wizard.Usb(config, writable=True):
                        pass

            def private(argv, **kw):
                out = 'AB-CD rw,relatime,uid=0,gid=0,fmask=0177,dmask=0077' if 'UUID,OPTIONS' in argv else ''
                return mock.Mock(returncode=0, stdout=out)
            with mock.patch.object(recovery_wizard, 'USB_LOCK', Path(d) / 'lock'), \
                    mock.patch.object(recovery_wizard.subprocess, 'run', private):
                with recovery_wizard.Usb(config) as usb:
                    self.assertEqual(usb, Path(d))


class SonarRefactorTests(unittest.TestCase):
    """Helpers split out for Sonar keep their behaviour."""
    def test_docker_rows_before_and_after_install(self):
        import capabilities
        rows = {r['name']: r['found'] for r in capabilities.docker_rows('', '', False)}
        self.assertEqual(rows, {'docker': 'not installed yet (setup installs it)',
                                'compose': 'not installed yet (setup installs it)'})
        rows = {r['name']: r['found'] for r in capabilities.docker_rows('', '', True)}
        self.assertIn('not started yet', rows['docker'])
        self.assertEqual(rows['compose'], 'checked once Docker runs')
        rows = capabilities.docker_rows('27.1', '2.29', True)
        self.assertEqual([(r['found'], r['ok']) for r in rows], [('27.1', True), ('2.29', True)])

    def test_run_jobs_lists_only_backups_that_did_not_pass(self):
        import first_checkup
        jobs = [('a', 'A', 'a.json'), ('b', 'B', 'b.json'), ('c', 'C', 'c.json')]
        results = {'a.json': ('pass', ''), 'b.json': ('partial', 'one folder skipped')}
        lines = []
        with mock.patch.object(first_checkup, 'JOBS', jobs), \
                mock.patch.object(first_checkup, 'scheduled', return_value=True), \
                mock.patch.object(first_checkup, 'start', side_effect=lambda unit, run: unit != 'c'), \
                mock.patch.object(first_checkup, 'outcome', side_effect=lambda config, report: results[report]):
            failed = first_checkup.run_jobs({}, lines.append, None)
        self.assertEqual(failed, ['b', 'c'])
        self.assertIn('    done in part: one folder skipped', lines)
        self.assertTrue(any('journalctl -u c.service' in line for line in lines))


class ConnectorQbittorrentLoginTests(unittest.TestCase):
    """The connector's qBittorrent client accepts qBittorrent 5's empty reply and
    QBT_SID cookie as a good login, and still refuses "Fails."."""
    def client_answering(self, reply, cookie=None):
        import qbit_setup
        client = qbit_setup.Client()

        def call(endpoint, form=None, json_result=False):
            if cookie:
                client.jar.set_cookie(http.cookiejar.Cookie(0, cookie, 'x', None, False, '127.0.0.1', False, False, '/', True,
                                                            False, None, False, None, None, {}))
            return reply
        client.call = call
        return client

    def test_qbittorrent_5_login(self):
        self.client_answering('', 'QBT_SID_8080').login('u', 'p')
        self.client_answering('Ok.', 'SID').login('u', 'p')

    def test_a_refused_login_still_fails(self):
        with self.assertRaises(RuntimeError):
            self.client_answering('Fails.').login('u', 'p')
        with self.assertRaises(RuntimeError):
            self.client_answering('', None).login('u', 'p')


def load_connector():
    import importlib.util
    spec = importlib.util.spec_from_file_location('media_connect', ROOT / 'media-connect.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConnectorRerunTests(unittest.TestCase):
    """Setup run again: a Prowlarr link made earlier is kept although Prowlarr masks
    the stored key in its answers, and the download client is set with each app's
    own category field."""
    def test_a_link_with_a_masked_key_is_verified_by_its_addresses(self):
        connect = load_connector()
        existing = [{'name': 'neko-sonarr', 'syncLevel': 'addOnly', 'fields': [
            {'name': 'prowlarrUrl', 'value': 'http://prowlarr:9696'}, {'name': 'baseUrl', 'value': 'http://sonarr:8989'},
            {'name': 'apiKey', 'value': '********'}]}]
        answers = {('prowlarr', '/applications/schema'): [], ('prowlarr', '/applications'): existing, ('sonarr', '/system/status'): {}}
        with mock.patch.object(connect, 'app_key', side_effect=lambda app, root: 'k-' + app), \
                mock.patch.object(connect, 'request', side_effect=lambda app, key, endpoint, *a, **k: answers[(app, endpoint)]), \
                mock.patch('builtins.print') as shown:
            self.assertIsNone(connect.configure_applications(Path('/x'), True, apps=('sonarr',)))
        self.assertIn('existing connection fields verified', str(shown.call_args_list))

    def test_a_link_to_another_address_is_still_reported(self):
        connect = load_connector()
        existing = [{'name': 'neko-sonarr', 'syncLevel': 'addOnly', 'fields': [
            {'name': 'prowlarrUrl', 'value': 'http://prowlarr:9696'}, {'name': 'baseUrl', 'value': 'http://elsewhere:8989'},
            {'name': 'apiKey', 'value': '********'}]}]
        answers = {('prowlarr', '/applications/schema'): [], ('prowlarr', '/applications'): existing, ('sonarr', '/system/status'): {}}
        with mock.patch.object(connect, 'app_key', side_effect=lambda app, root: 'k-' + app), \
                mock.patch.object(connect, 'request', side_effect=lambda app, key, endpoint, *a, **k: answers[(app, endpoint)]), \
                mock.patch('builtins.print'):
            with self.assertRaises(RuntimeError) as caught:
                connect.configure_applications(Path('/x'), True, apps=('sonarr',))
        self.assertIn('sonarr', str(caught.exception))

    def test_each_app_gets_its_own_category_field(self):
        connect = load_connector()
        self.assertEqual(connect.CATEGORY_FIELDS, {'sonarr': 'tvCategory', 'radarr': 'movieCategory', 'lidarr': 'musicCategory'})
        schema = {'implementation': 'QBittorrent', 'fields': [{'name': n} for n in (
            'host', 'port', 'useSsl', 'username', 'password', 'tvCategory', 'tvImportedCategory')]}
        posted = []

        def request(app, key, endpoint, method='GET', payload=None, **k):
            if method == 'POST':
                posted.append(payload)
                return payload
            return {'/downloadclient': [], '/downloadclient/schema': [schema]}[endpoint]
        with mock.patch.object(connect.account_store, 'get', return_value={'username': 'u', 'password': 'p'}), \
                mock.patch.object(connect, 'app_key', return_value='k'), \
                mock.patch.object(connect, 'request', side_effect=request), \
                mock.patch('builtins.print'):
            self.assertIsNone(connect.configure_download_clients(Path('/x'), True, apps=('sonarr',), verify_qbit=False))
        fields = {f['name']: f.get('value') for f in posted[0]['fields']}
        self.assertEqual((fields['tvCategory'], fields['host'], fields['username']), ('tv', 'qbittorrent', 'u'))


class JellyfinKeyHeaderTests(unittest.TestCase):
    """Jellyfin 12 wants the key in the MediaBrowser Authorization header (Token="..."),
    not in X-Emby-Token alone."""
    def test_a_key_set_as_x_emby_token_is_also_sent_as_the_authorization_token(self):
        import media_libraries
        client = media_libraries.Client('jellyfin')
        client.headers['X-Emby-Token'] = 'key123'
        sent = media_libraries.jellyfin_headers('jellyfin', client.headers)
        self.assertTrue(sent['Authorization'].startswith('MediaBrowser Client="NekoServer"'))
        self.assertIn(', Token="key123"', sent['Authorization'])
        self.assertNotIn('Token=', client.headers['Authorization'])   # the client's own headers are untouched

    def test_a_token_already_in_the_authorization_header_is_kept_once(self):
        import media_libraries
        headers = {'Authorization': 'MediaBrowser Client="x", Token="abc"', 'X-Emby-Token': 'abc'}
        self.assertEqual(media_libraries.jellyfin_headers('jellyfin', headers)['Authorization'].count('Token='), 1)
        self.assertEqual(media_libraries.jellyfin_headers('seerr', {'X-Emby-Token': 'x'}), {'X-Emby-Token': 'x'})


class ConnectorAnswerSizeTests(unittest.TestCase):
    """The connector reads Prowlarr's multi-MB indexer catalogue whole and names an
    answer that is larger than its limit instead of failing to parse a cut-off one."""
    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('media_connect', ROOT / 'media-connect.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_catalogue_of_several_megabytes_is_parsed(self):
        connect = self.load()
        payload = json.dumps([{'name': 'x' * 100}] * 40000).encode()   # ~4.4 MB
        self.assertGreater(len(payload), 3_000_000)

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return payload[:n]
        with mock.patch.object(connect.OPENER, 'open', return_value=Response()):
            self.assertEqual(len(connect.request('prowlarr', 'k', '/indexer/schema')), 40000)

    def test_an_oversized_answer_is_named(self):
        connect = self.load()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'[' + b'1,' * (n // 2)
        with mock.patch.object(connect.OPENER, 'open', return_value=Response()):
            with self.assertRaises(RuntimeError) as caught:
                connect.request('prowlarr', 'k', '/indexer/schema')
        self.assertIn('larger than', str(caught.exception))


class DriveReturnTests(unittest.TestCase):
    """When the SSD or HDD comes back after it disappeared, systemd runs the runtime's
    drive-return, which renews the mount and starts the apps again."""
    config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media',
              'storage': {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}}

    def test_the_unit_runs_when_either_drive_appears_after_boot_mounts(self):
        from modules import server_os
        escaped = lambda argv, **_: mock.Mock(stdout='dev-disk-by\\x2duuid-' + argv[-1].rsplit('/', 1)[1].replace('-', '\\x2d') + '.device\n')
        devices = server_os.drive_devices(self.config, run=escaped)
        self.assertEqual(devices, ['dev-disk-by\\x2duuid-aaaa\\x2d1111.device', 'dev-disk-by\\x2duuid-bbbb\\x2d2222.device'])
        text = server_os.drive_unit(Path('/usr/local/lib/nekoserver-os/v1/nekoserver-runtime'), devices)
        self.assertTrue(text.startswith('[Unit]\nDescription=NekoServer '))   # recognised as NekoServer's own
        self.assertIn('After=local-fs.target\n', text)
        self.assertIn('ExecStart=/usr/local/lib/nekoserver-os/v1/nekoserver-runtime drive-return --config /etc/nekoserver-os/drives.json\n', text)
        self.assertIn('WantedBy=' + ' '.join(devices) + '\n', text)

    def test_install_writes_the_settings_and_enables_the_unit(self):
        from modules import server_os
        calls, written = [], {}
        run = lambda argv, **_: calls.append(argv) or mock.Mock(stdout='dev-x.device\n', returncode=0)
        with mock.patch.object(server_os.native_runtime, 'deploy', return_value=Path('/rt/nekoserver-runtime')), \
                mock.patch.object(server_os, 'check_units'), \
                mock.patch.object(server_os, 'safe_path', side_effect=lambda p: Path(p)), \
                mock.patch.object(server_os, 'atomic_json', side_effect=lambda path, value: written.update({str(path): value})), \
                mock.patch.object(server_os, 'write_units', side_effect=lambda units: written.update(units)):
            server_os.install_drive_return(self.config, Path('/project'), run=run)
        self.assertEqual(written[str(server_os.DRIVE_SETTINGS)]['storage'], self.config['storage'])
        self.assertIn('drive-return', written[server_os.DRIVE_UNIT])
        self.assertIn(['systemctl', 'reenable', server_os.DRIVE_UNIT], calls)


class HddPolicyTests(unittest.TestCase):
    """A media drive that is not an internal spinning SATA disk (a USB case, an SSD)
    keeps its own power settings: setup says so and goes on, instead of "needs you" for
    good with a red alert on the dashboard."""
    def said(self, journal, marker):
        from modules import server_os
        return server_os.hdd_service_said(marker, run=lambda argv, **_: mock.Mock(stdout=journal))

    def test_what_the_service_did_is_shown(self):
        journal = ('Starting NekoServer verified media HDD standby policy...\n'
                   'Drive power settings: left to the drive itself, as it is connected by USB\n')
        self.assertEqual(self.said(journal, 'Drive power settings:'), 'left to the drive itself, as it is connected by USB')
        self.assertEqual(self.said(journal, 'runtime failed'), '')
        # Failures keep their reason, with or without a stage named.
        self.assertEqual(self.said('NekoServer runtime failed: HDD standby command refused. No success recorded.\n', 'runtime failed'),
                         'HDD standby command refused. No success recorded')
        self.assertEqual(self.said('NekoServer runtime failed while checking: Media device changed.\n', 'runtime failed'),
                         'while checking: Media device changed')
        from modules import server_os
        def broken(argv, **_):
            raise OSError('no journalctl')
        self.assertEqual(server_os.hdd_service_said('runtime failed', run=broken), '')


class GatewayStateOwnerTests(unittest.TestCase):
    """After Linux is reinstalled, its caddy account gets another number: the gateway's
    certificates on the SSD are handed over when NekoServer itself gave them the old one."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state, self.ledger = root / 'caddy-state', root / 'lan-managed.json'
        (self.state / 'caddy/pki').mkdir(parents=True)
        (self.state / 'caddy/pki/root.crt').write_text('ca')
        self.me = os.stat(self.state)
        self.caddy = mock.Mock(pw_uid=995, pw_gid=989)

    def test_an_earlier_installs_files_are_taken_over(self):
        import gateway_tools
        self.ledger.write_text(json.dumps({'uid': self.me.st_uid, 'gid': self.me.st_gid}))
        changed = []
        with mock.patch.object(gateway_tools.pwd, 'getpwuid', side_effect=KeyError), \
                mock.patch.object(gateway_tools.os, 'chown', side_effect=lambda p, u, g, follow_symlinks=True: changed.append((Path(p).name, u, g))):
            said = gateway_tools.adopt_state(self.state, self.caddy, self.ledger)
        self.assertIn('caddy account of an earlier install', said)
        self.assertEqual({name for name, _, _ in changed}, {'caddy-state', 'caddy', 'pki', 'root.crt'})
        self.assertEqual({(u, g) for _, u, g in changed}, {(995, 989)})

    def test_files_of_an_account_here_or_an_owner_never_recorded_stay(self):
        import gateway_tools
        with mock.patch.object(gateway_tools.os, 'chown') as chown:
            # The account owning them exists on this PC (here: whoever runs the test).
            self.ledger.write_text(json.dumps({'uid': self.me.st_uid, 'gid': self.me.st_gid}))
            with mock.patch.object(gateway_tools.pwd, 'getpwuid', return_value=mock.Mock(pw_name='alice')), \
                    self.assertRaisesRegex(RuntimeError, 'the account "alice".*chown -R caddy:caddy'):
                gateway_tools.adopt_state(self.state, self.caddy, self.ledger)
            # No such account, but NekoServer never gave them that owner.
            self.ledger.write_text(json.dumps({'uid': self.me.st_uid + 1, 'gid': self.me.st_gid}))
            with mock.patch.object(gateway_tools.pwd, 'getpwuid', side_effect=KeyError), \
                    self.assertRaisesRegex(RuntimeError, 'NekoServer did not leave them that way'):
                gateway_tools.adopt_state(self.state, self.caddy, self.ledger)
        chown.assert_not_called()


class NextcloudFirstInstallTests(unittest.TestCase):
    """Nextcloud's image installs it on its first start only; when its database was out of
    reach then, setup finishes the install instead of leaving it "not installed" for good."""
    BASE = ['docker', 'compose', '-p', 'neko-nextcloud']

    def fake(self, installed, logs):
        calls = []
        state = {'installed': installed}

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[-3:] == ['occ', 'status', '--output=json']:
                return mock.Mock(returncode=0, stdout='{"installed":' + ('true' if state['installed'] else 'false') + '}')
            if 'logs' in argv:
                return mock.Mock(returncode=0, stdout=logs)
            return mock.Mock(returncode=0, stdout='')
        return run, calls, state

    def test_an_installed_nextcloud_is_left_alone(self):
        from modules import nextcloud
        run, calls, _ = self.fake(True, '')
        with mock.patch.object(nextcloud, 'checked_run') as checked:
            self.assertFalse(nextcloud.finish_install(self.BASE, lambda m: None, run=run))
        checked.assert_not_called()

    def test_a_given_up_first_install_is_finished_with_the_images_own_settings(self):
        from modules import nextcloud
        run, calls, state = self.fake(False, 'Retrying install...\nInstalling of nextcloud failed!\n')
        logged = []
        with mock.patch.object(nextcloud, 'checked_run', side_effect=lambda argv, *a: state.update(installed=True)) as checked:
            self.assertTrue(nextcloud.finish_install(self.BASE, logged.append, run=run, sleep=lambda s: None))
        argv = checked.call_args[0][0]
        self.assertEqual(argv[:9], self.BASE + ['exec', '-T', '-u', 'www-data', 'nextcloud'])
        self.assertIn('maintenance:install', argv[-1])
        self.assertIn('"$NEXTCLOUD_ADMIN_PASSWORD"', argv[-1])   # from the container, never on our command line
        self.assertIn('trusted_domains', argv[-1])
        self.assertIn('finishing it now', logged[0])

    def test_an_install_still_running_is_waited_for_then_named(self):
        from modules import nextcloud
        clock = iter(range(0, 10000, 100))
        run, _, _ = self.fake(False, 'Initializing nextcloud 33.0.10.1 ...\n')   # the image is still installing
        with mock.patch.object(nextcloud, 'checked_run') as checked, \
                self.assertRaisesRegex(RuntimeError, 'did not finish installing within 15 minutes.*docker logs'):
            nextcloud.finish_install(self.BASE, lambda m: None, run=run, sleep=lambda s: None, now=lambda: next(clock))
        checked.assert_not_called()


class NextcloudUpgradeTests(unittest.TestCase):
    """A new release may change how Nextcloud's containers are run; with the same settings
    in my-config.json that is applied, not taken for a changed setting."""
    def test_settings_are_read_back_from_an_older_install(self):
        from modules import nextcloud
        config = {'data_mount': '/srv/x', 'media_mount': '/mnt/x', 'nextcloud': {'trusted_hosts': ['localhost', 'nas.home'], 'public_url': 'https://nas.home:8447'}}
        older = {'format': 'neko-nextcloud-v1', 'image': 'nextcloud:31-apache', 'model': nextcloud.model(config, 'nextcloud:31-apache')}
        older['model']['services']['cron'].pop('init')   # as an earlier release wrote it
        self.assertEqual(nextcloud.settings_of(older), nextcloud.settings(config, 'nextcloud:31-apache'))
        changed = dict(config, nextcloud={'trusted_hosts': ['localhost']})
        self.assertNotEqual(nextcloud.settings_of(older), nextcloud.settings(changed, 'nextcloud:31-apache'))
        recorded = dict(older, settings=nextcloud.settings(config, 'nextcloud:31-apache'))
        self.assertEqual(nextcloud.settings_of(recorded), nextcloud.settings(config, 'nextcloud:31-apache'))


class NextcloudCronStopTests(unittest.TestCase):
    """Nextcloud's cron container runs behind an init process, so a stop signal reaches
    crond (as PID 1 it would ignore it and be killed after 60 s, exit 137)."""
    def test_the_cron_service_has_an_init_process(self):
        from modules import nextcloud
        config = {'data_mount': '/srv/x', 'media_mount': '/mnt/x', 'nextcloud': {}}
        services = nextcloud.model(config, 'nextcloud:31')['services']
        self.assertIs(services['cron']['init'], True)
        self.assertEqual(services['cron']['stop_signal'], 'SIGTERM')   # the Apache image's own is SIGWINCH
        self.assertEqual(services['cron']['entrypoint'], '/cron.sh')


class IsolatedRestoreNetworkTests(unittest.TestCase):
    """The restore test's network must let this PC reach the published port (Docker
    publishes nothing on an "internal" network) while the restored app has no way out."""
    def test_the_test_network_publishes_but_does_not_masquerade(self):
        import recovery_tools
        model = {'services': {'immich-server': {'image': 'immich', 'environment': {'IMMICH_VERSION': 'v3'}},
                              'database': {'image': 'pg', 'environment': {}}}}
        meta = {'source_upload': '/x/upload', 'source_database': '/x/db'}
        with mock.patch.object(recovery_tools, 'validate_model'):
            isolated = recovery_tools.isolated_model(model, meta, Path('/w'), 2284)
        network = isolated['networks']['default']
        self.assertNotIn('internal', network)
        self.assertEqual(network['driver_opts'], {'com.docker.network.bridge.enable_ip_masquerade': 'false'})
        self.assertEqual(isolated['services']['immich-server']['ports'],
                         [{'target': 2283, 'published': '2284', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}])


class RestoreTestPortTests(unittest.TestCase):
    """A restore test is left running for a person to check; a second one on the same port
    names the first and the exact command that stops it."""
    def test_an_earlier_restore_test_is_named_with_its_stop_command(self):
        import recovery_tools
        with tempfile.TemporaryDirectory() as d:
            work = Path(d) / 'recovery-tests' / 'first'
            work.mkdir(parents=True)
            (work / 'restore-report.json').write_text(json.dumps({'project': 'neko-restore-0123456789ab'}))
            shown = mock.Mock(stdout='neko-restore-0123456789ab\n')
            message = recovery_tools.port_in_use({'data_mount': d, 'media_mount': '/nonexistent'}, 2284, run=lambda *a, **k: shown)
        self.assertIn('earlier restore test (neko-restore-0123456789ab)', message)
        self.assertIn(f'-f {work}/compose.json stop', message)
        self.assertIn('--port', message)

    def test_another_program_on_the_port_is_said_plainly(self):
        import recovery_tools
        message = recovery_tools.port_in_use({'data_mount': '/x', 'media_mount': '/y'}, 2284, run=lambda *a, **k: mock.Mock(stdout=''))
        self.assertIn('already in use by another program', message)


class RestoreTestWaitTests(unittest.TestCase):
    """The isolated restore's HTTP wait is bounded by time (10 minutes), not by a count of
    tries, and ends as soon as the app answers."""
    def opener(self, answers):
        import urllib.error
        replies = iter(answers)

        class Response:
            def __init__(self, status): self.status = status
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Opener:
            def open(self, url, timeout=0):
                code = next(replies)
                if code is None:
                    raise OSError('refused')
                if code != 200:
                    raise urllib.error.HTTPError(url, code, 'x', {}, None)
                return Response(200)
        return Opener()

    def test_an_app_that_answers_late_still_passes_within_the_deadline(self):
        import recovery_tools
        now = {'t': 0}
        with mock.patch.object(recovery_tools, 'local_opener', return_value=self.opener([None, 503, 200])):
            self.assertTrue(recovery_tools.http_ready(2284, seconds=600, clock=lambda: now['t'], sleep=lambda s: now.update(t=now['t'] + 100)))

    def test_the_wait_gives_up_after_the_deadline(self):
        import recovery_tools
        now = {'t': 0}
        tries = []
        with mock.patch.object(recovery_tools, 'local_opener', return_value=self.opener([None] * 1000)):
            self.assertFalse(recovery_tools.http_ready(2284, seconds=600, clock=lambda: now['t'],
                                                       sleep=lambda s: (tries.append(s), now.update(t=now['t'] + 150))))
        self.assertEqual(len(tries), 4)


class OnboardingProbeTests(unittest.TestCase):
    """The onboarding probes follow no redirects: an app that sends its front page to
    its sign-in page (302) or asks for a login (401) is up, not "unavailable"."""
    def answers(self, code):
        import io, urllib.error
        def opener(url, timeout=0):
            raise urllib.error.HTTPError(url, code, 'x', {}, io.BytesIO(b''))
        return opener

    def test_a_sign_in_redirect_counts_as_the_app_answering(self):
        import onboard
        with mock.patch.object(onboard.OPENER, 'open', side_effect=self.answers(302)):
            self.assertEqual(onboard.wait_ready(['sonarr', 'jellyfin'], 1, clock=lambda: 0, sleep=lambda s: None), [])
        with mock.patch.object(onboard.OPENER, 'open', side_effect=self.answers(503)):
            self.assertEqual(onboard.wait_ready(['sonarr'], 0, clock=lambda: 0, sleep=lambda s: None), ['sonarr'])

    def test_the_report_marks_a_redirecting_app_as_up(self):
        import onboard
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'appdata' / 'sonarr').mkdir(parents=True)
            with mock.patch.object(onboard.OPENER, 'open', side_effect=self.answers(302)):
                rows = {r['check']: r['status'] for r in onboard.report(root, root, apps=('sonarr',))}
            self.assertEqual(rows['http:sonarr'], 'pass')
            with mock.patch.object(onboard.OPENER, 'open', side_effect=self.answers(502)):
                rows = {r['check']: r['status'] for r in onboard.report(root, root, apps=('sonarr',))}
            self.assertEqual(rows['http:sonarr'], 'fail')


class FinishingScreenPinTests(unittest.TestCase):
    """The PIN file lives in a root-only folder: without root, the finishing screen says
    the answer is unknown instead of crashing with PermissionError."""
    def test_an_unreadable_pin_folder_does_not_stop_the_finishing_screen(self):
        import wizard

        class Unreadable:
            def exists(self):
                raise PermissionError(13, 'Permission denied')
        lines = []
        with mock.patch.object(wizard, 'dashboard_port', return_value=8787):
            wizard.show_dashboard({}, lines.append, pin_file=Unreadable())
        self.assertTrue(any(line.startswith('Dashboard PIN:') and 'unknown' in line for line in lines))


class FakeCookie:
    def __init__(self, name):
        self.name = name


class FakeClient:
    """Answers each endpoint from a table; records every call. `jar` holds the cookies
    a sign-in sets, as the real cookie jar would."""
    def __init__(self, answers, cookie_on_login=None):
        self.answers, self.cookie_on_login = answers, cookie_on_login
        self.headers, self.jar, self.calls = {}, [], []

    def call(self, endpoint, method='GET', payload=None, form=None, raw=False, headers=None):
        import account_setup
        self.calls.append((method, endpoint))
        if endpoint in ('/login', '/api/v2/auth/login') and self.cookie_on_login:
            self.jar.append(FakeCookie(self.cookie_on_login))
        answer = self.answers.get((method, endpoint), self.answers.get(endpoint))
        if callable(answer):
            answer = answer(payload or form)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            raise account_setup.HTTPFailure(answer)
        return answer


class MediaManagerLoginTests(unittest.TestCase):
    """Sonarr, Radarr, Lidarr and Prowlarr: the login is proved on the page the app loads
    after sign-in (the API wants its key even with a session, since Sonarr v4), and a
    freshly set login is applied only after the app restarts."""
    def setUp(self):
        import account_setup
        self.account_setup = account_setup
        self.tmp = tempfile.TemporaryDirectory()
        appdata = Path(self.tmp.name) / 'appdata' / 'sonarr'
        appdata.mkdir(parents=True)
        (appdata / 'config.xml').write_text('<Config><ApiKey>key123</ApiKey></Config>')
        self.config = {'data_mount': self.tmp.name}
        self.entry = {'username': 'neko_a', 'password': 'pw', 'allow_create': True}

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_app_is_restarted_after_its_login_is_set_and_the_session_is_proved(self):
        host = {'authenticationMethod': 'none', 'authenticationRequired': 'disabledForLocalAddresses', 'username': '', 'password': ''}
        state = {'host': dict(host)}

        def put(payload):
            state['host'] = dict(payload)
            return {}
        client = FakeClient({('GET', '/api/v3/config/host'): lambda _: state['host'], ('PUT', '/api/v3/config/host'): put,
                             ('POST', '/api/v3/system/restart'): {}, '/api/v3/system/status': b'{}',
                             '/login': b'<html>', '/initialize.json': b'{"apiRoot": "/api/v3", "apiKey": "key123"}'},
                            cookie_on_login='SonarrAuth')
        with mock.patch.object(self.account_setup.time, 'sleep'):
            self.account_setup.arr(client, self.entry, self.config, 'sonarr')
        methods = [c for c in client.calls]
        put_at = methods.index(('PUT', '/api/v3/config/host'))
        restart_at = methods.index(('POST', '/api/v3/system/restart'))
        login_at = methods.index(('POST', '/login'))
        self.assertLess(put_at, restart_at)
        self.assertLess(restart_at, login_at)
        self.assertEqual(methods[-1], ('GET', '/initialize.json'))
        self.assertEqual(state['host']['username'], 'neko_a')
        self.assertNotIn('X-Api-Key', client.headers)

    def test_an_app_that_sends_the_session_to_its_sign_in_page_is_not_verified(self):
        host = {'authenticationMethod': 'forms', 'authenticationRequired': 'enabled', 'username': 'neko_a', 'password': 'x'}
        client = FakeClient({'/api/v3/config/host': host, '/login': b'<html>', '/initialize.json': b'<html>sign in</html>'},
                            cookie_on_login='SonarrAuth')
        with self.assertRaises(RuntimeError):
            self.account_setup.arr(client, self.entry, self.config, 'sonarr')
        self.assertNotIn(('POST', '/api/v3/system/restart'), client.calls)

    def test_a_page_with_another_apps_key_is_not_verified(self):
        host = {'authenticationMethod': 'forms', 'authenticationRequired': 'enabled', 'username': 'neko_a', 'password': 'x'}
        client = FakeClient({'/api/v3/config/host': host, '/login': b'<html>', '/initialize.json': b'{"apiKey": "other"}'},
                            cookie_on_login='SonarrAuth')
        with self.assertRaises(RuntimeError):
            self.account_setup.arr(client, self.entry, self.config, 'sonarr')


class SeerrLoginTests(unittest.TestCase):
    """Seerr linked to Jellyfin by an earlier run refuses the Jellyfin address again
    ("Jellyfin hostname already configured", HTTP 500): the Jellyfin login alone signs
    that user in, and the rest of the account setup goes on."""
    def test_a_linked_seerr_is_signed_into_without_the_address(self):
        import account_setup
        entry = {'username': 'neko_s@server.invalid', 'password': 'pw', 'allow_create': True}
        jellyfin = {'username': 'neko_j', 'password': 'jpw', 'state': 'verified'}
        bodies = []

        def jellyfin_login(body):
            bodies.append(body)
            if 'hostname' in body:
                return account_setup.HTTPFailure(500)
            # Seerr 3.5: no email in the answer, the linked Jellyfin user instead.
            return {'id': 1, 'jellyfinUsername': 'neko_j', 'username': None}
        state = {'has_password': False, 'initialized': False}
        client = FakeClient({
            ('POST', '/api/v1/auth/local'): lambda _: account_setup.HTTPFailure(403) if not state['has_password'] else {'id': 1, 'email': 'neko_s@server.invalid'},
            ('GET', '/api/v1/settings/public'): lambda _: {'initialized': state['initialized']},
            ('POST', '/api/v1/auth/jellyfin'): jellyfin_login,
            ('GET', '/api/v1/auth/me'): {'id': 1, 'email': 'Neko_S@server.invalid', 'jellyfinUsername': 'neko_j'},
            ('GET', '/api/v1/user/1/settings/password'): lambda _: {'hasPassword': state['has_password']},
            ('POST', '/api/v1/user/1/settings/password'): lambda _: state.update(has_password=True) or {},
            ('POST', '/api/v1/auth/logout'): {},
            ('POST', '/api/v1/settings/initialize'): lambda _: state.update(initialized=True) or {}})
        account_setup.seerr(client, entry, jellyfin)
        self.assertEqual([('hostname' in b) for b in bodies], [True, False])
        self.assertNotIn('hostname', bodies[1])
        self.assertTrue(state['has_password'] and state['initialized'])
        self.assertIn(('GET', '/api/v1/auth/me'), client.calls)

    def test_a_session_for_another_user_is_not_verified(self):
        import account_setup
        entry = {'username': 'neko_s@server.invalid', 'password': 'pw', 'allow_create': False}
        client = FakeClient({('POST', '/api/v1/auth/local'): {'id': 1, 'displayName': 'x'},
                             ('GET', '/api/v1/auth/me'): {'id': 1, 'email': 'someone@else.invalid'}})
        with self.assertRaises(RuntimeError):
            account_setup.seerr(client, entry, None)

    def test_another_jellyfin_sign_in_failure_is_not_retried(self):
        import account_setup
        entry = {'username': 'neko_s@server.invalid', 'password': 'pw', 'allow_create': True}
        jellyfin = {'username': 'neko_j', 'password': 'jpw', 'state': 'verified'}
        client = FakeClient({('POST', '/api/v1/auth/local'): account_setup.HTTPFailure(403),
                             ('GET', '/api/v1/settings/public'): {'initialized': False},
                             ('POST', '/api/v1/auth/jellyfin'): account_setup.HTTPFailure(401)})
        with self.assertRaises(account_setup.HTTPFailure):
            account_setup.seerr(client, entry, jellyfin)
        self.assertEqual(sum(1 for c in client.calls if c == ('POST', '/api/v1/auth/jellyfin')), 1)


class QbittorrentLoginTests(unittest.TestCase):
    """qBittorrent 5 answers a good login with an empty 204 (4 said "Ok."); the SID cookie
    is the proof either way."""
    def setUp(self):
        import account_setup
        self.account_setup = account_setup
        self.entry = {'username': 'neko_q', 'password': 'pw', 'allow_create': True}
        self.preferences = {'web_ui_username': 'neko_q', 'bypass_local_auth': False, 'bypass_auth_subnet_whitelist_enabled': False}

    def test_an_empty_reply_with_a_session_cookie_is_a_good_login(self):
        client = FakeClient({'/api/v2/auth/login': b'', '/api/v2/app/preferences': self.preferences, '/api/v2/auth/logout': b''},
                            cookie_on_login='QBT_SID_8080')
        self.account_setup.qbittorrent(client, self.entry)
        self.assertEqual(client.calls[0], ('POST', '/api/v2/auth/login'))
        self.assertNotIn(('POST', '/api/v2/app/setPreferences'), client.calls)

    def test_ok_with_a_session_cookie_is_still_a_good_login(self):
        client = FakeClient({'/api/v2/auth/login': b'Ok.', '/api/v2/app/preferences': self.preferences, '/api/v2/auth/logout': b''},
                            cookie_on_login='SID')
        self.account_setup.qbittorrent(client, self.entry)

    def test_a_refused_login_falls_back_to_the_temporary_password(self):
        answers = iter([b'Fails.', b'', b''])   # saved login refused; admin+temporary accepted; new login accepted
        client = FakeClient({'/api/v2/auth/login': lambda _: next(answers),
                             '/api/v2/app/preferences': lambda _: {'web_ui_username': 'admin'} if len(client.calls) < 5 else self.preferences,
                             '/api/v2/app/setPreferences': b'', '/api/v2/auth/logout': b''}, cookie_on_login='SID')
        logs = 'The WebUI administrator password was not set. A temporary password is provided for this session: tmpPass1\n'
        with mock.patch.object(self.account_setup, 'docker', side_effect=lambda args, *_: 'abcdef123456\n' if args[0] == 'ps' else logs):
            self.account_setup.qbittorrent(client, self.entry)
        self.assertIn(('POST', '/api/v2/app/setPreferences'), client.calls)
        self.assertEqual([c for c in client.calls if c == ('POST', '/api/v2/auth/login')], [('POST', '/api/v2/auth/login')] * 3)


if __name__ == '__main__':
    unittest.main()
