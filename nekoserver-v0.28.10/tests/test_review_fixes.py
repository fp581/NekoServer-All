"""Regression tests for the compose-path, Seerr UID and timezone fixes."""
import os
import sys
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

    def test_versions_sort_by_number_not_text(self):
        import native_runtime
        self.assertLess(native_runtime.release_number('v0.28.9'), native_runtime.release_number('v0.28.10'))
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
                    mock.patch.object(recovery_wizard.subprocess, 'run', run):
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


if __name__ == '__main__':
    unittest.main()
