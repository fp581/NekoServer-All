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


if __name__ == '__main__':
    unittest.main()
