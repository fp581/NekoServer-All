"""Desktop shortcuts: the dashboard, the browser, Steam and the installed apps, and no
Steam Big Picture at login."""
import os
import pwd
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules import desktop, tv


class DesktopShortcutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'ssd'
        self.config = {'data_mount': str(self.data), 'dashboard': {'port': 8787},
                       'media': {'services': ['jellyfin', 'navidrome', 'sonarr']}}

    def files(self, which=lambda name: None, launcher=None):
        return desktop.shortcuts(self.config, which, launcher or self.root / 'no-launcher')

    def test_every_installed_app_gets_one(self):
        (self.data / 'compose/immich').mkdir(parents=True)
        (self.data / 'compose/immich/compose.yaml').write_text('services: {}\n')
        names = self.files()
        self.assertEqual(sorted(names), ['nekoserver-dashboard.desktop', 'nekoserver-immich.desktop',
                                         'nekoserver-jellyfin.desktop', 'nekoserver-navidrome.desktop',
                                         'nekoserver-sonarr.desktop'])
        self.assertIn('Exec=xdg-open http://127.0.0.1:8096/', names['nekoserver-jellyfin.desktop'])
        self.assertIn('Name=Immich', names['nekoserver-immich.desktop'])
        # Nextcloud was not set up here: no shortcut that leads nowhere.
        self.assertNotIn('nekoserver-nextcloud.desktop', names)

    def test_the_browser_steam_and_the_dashboard_window(self):
        launcher = self.root / 'nekoserver-dashboard'
        launcher.write_text('#!/bin/sh\n')
        found = {'chromium': '/usr/bin/chromium', 'steam': '/usr/bin/steam'}
        names = self.files(found.get, launcher)
        self.assertIn('Exec=/usr/bin/chromium\n', names['nekoserver-browser.desktop'])
        # Steam as a normal window, not Big Picture.
        self.assertIn('Exec=/usr/bin/steam\n', names['nekoserver-steam-desktop.desktop'])
        self.assertIn('Exec=' + str(launcher) + '\n', names['nekoserver-dashboard.desktop'])

    def test_music_shortcut_can_be_switched_off(self):
        self.config['music'] = {'create_desktop_shortcut': False}
        self.assertNotIn('nekoserver-navidrome.desktop', self.files())

    def run_step(self, home):
        me = pwd.getpwuid(os.getuid())
        user = mock.Mock(pw_uid=me.pw_uid, pw_gid=me.pw_gid, pw_name=me.pw_name)
        ctx = mock.Mock(apply=True, config=self.config, log=lambda message: None)
        with mock.patch('os.geteuid', return_value=0), \
                mock.patch.object(tv, 'desktop_user', return_value=(user, home)), \
                mock.patch.object(tv, 'desktop_folder', return_value=home / 'Desktop'), \
                mock.patch('shutil.which', return_value=None):
            return desktop.run(ctx)

    def test_old_shortcuts_are_updated_and_big_picture_no_longer_starts_at_login(self):
        home = self.root / 'home'
        autostart = home / desktop.OLD_AUTOSTART
        autostart.parent.mkdir(parents=True)
        autostart.write_text(tv.desktop_entry('Steam Big Picture', '/usr/bin/steam -gamepadui', 'steam'))
        (home / 'Desktop').mkdir()
        # Written by the TV step before 0.28.7, and one the user made.
        (home / 'Desktop/nekoserver-jellyfin.desktop').write_text(
            tv.desktop_entry('Jellyfin', '/usr/bin/firefox --new-window http://127.0.0.1:8096/', 'x'))
        (home / 'Desktop/nekoserver-sonarr.desktop').write_text('[Desktop Entry]\nName=My Sonarr\n')
        self.run_step(home)
        self.assertFalse(autostart.exists())
        self.assertIn('xdg-open', (home / 'Desktop/nekoserver-jellyfin.desktop').read_text())
        self.assertEqual((home / 'Desktop/nekoserver-sonarr.desktop').read_text(), '[Desktop Entry]\nName=My Sonarr\n')
        self.assertTrue((home / '.local/share/applications/nekoserver-navidrome.desktop').is_file())
        self.assertTrue(os.access(home / 'Desktop/nekoserver-dashboard.desktop', os.X_OK))

    def test_an_autostart_someone_else_made_is_kept(self):
        home = self.root / 'home'
        autostart = home / desktop.OLD_AUTOSTART
        autostart.parent.mkdir(parents=True)
        autostart.write_text('[Desktop Entry]\nExec=steam -bigpicture\n')
        self.run_step(home)
        self.assertTrue(autostart.exists())


if __name__ == '__main__':
    unittest.main()
