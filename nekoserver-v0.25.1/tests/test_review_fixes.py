"""Regression tests for the compose-path, Seerr UID and timezone fixes."""
import os
import sys
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
        with mock.patch.object(os, 'readlink', side_effect=OSError):
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


if __name__ == '__main__':
    unittest.main()


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
