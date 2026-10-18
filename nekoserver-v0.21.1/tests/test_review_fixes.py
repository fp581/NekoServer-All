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
