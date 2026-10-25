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
                mock.patch.object(tv, 'has_desktop', return_value=True), \
                mock.patch('shutil.which', return_value=None):
            return desktop.run(ctx)

    def test_a_desktop_set_to_the_home_folder_gets_menu_entries_only(self):
        # xdg-user-dirs points DESKTOP at the home folder once ~/Desktop is deleted; the
        # step failed ("must be a non-symlink directory inside the user home").
        home = self.root / 'home'
        home.mkdir()
        me = pwd.getpwuid(os.getuid())
        user = mock.Mock(pw_uid=me.pw_uid, pw_gid=me.pw_gid, pw_name=me.pw_name)
        with mock.patch('shutil.which', return_value='/usr/bin/xdg-user-dir'), \
                mock.patch.object(tv, 'command', return_value=mock.Mock(returncode=0, stdout=str(home) + '\n')):
            self.assertIsNone(tv.desktop_folder(user, home))
        with mock.patch('os.geteuid', return_value=0), \
                mock.patch.object(tv, 'desktop_user', return_value=(user, home)), \
                mock.patch.object(tv, 'desktop_folder', return_value=None), \
                mock.patch.object(tv, 'has_desktop', return_value=True), \
                mock.patch('shutil.which', return_value=None):
            desktop.run(mock.Mock(apply=True, config=self.config, log=lambda message: None))
        self.assertTrue((home / '.local/share/applications/nekoserver-navidrome.desktop').is_file())
        self.assertFalse(any(p.suffix == '.desktop' for p in home.iterdir()))

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


class TvSleepTests(unittest.TestCase):
    """KDE or XFCE on the TV: no GNOME setting to change, and none needed once the
    server-OS step has blocked sleep system-wide."""
    def test_blocked_system_wide_passes_on_any_desktop(self):
        masked = mock.Mock(stdout='masked\n' * 5, returncode=0)
        with mock.patch('shutil.which', return_value=None):
            result = tv.power_setup(mock.Mock(), Path('/home/u'), run=lambda argv: masked)
        self.assertEqual(result['status'], 'pass')

    def test_not_blocked_and_no_gnome_setting_still_needs_you(self):
        with mock.patch('shutil.which', return_value=None):
            result = tv.power_setup(mock.Mock(), Path('/home/u'), blocked=lambda: False)
        self.assertEqual(result['status'], 'needs_setup')

    def test_sleep_blocked_needs_every_target_masked(self):
        self.assertTrue(tv.sleep_blocked(lambda argv: mock.Mock(stdout='masked\n' * 5)))
        self.assertFalse(tv.sleep_blocked(lambda argv: mock.Mock(stdout='masked\nmasked\nenabled\nmasked\nmasked\n')))
        self.assertFalse(tv.sleep_blocked(lambda argv: mock.Mock(stdout='')))


if __name__ == '__main__':
    unittest.main()


class TvLauncherUpdateTests(unittest.TestCase):
    """The reported case: the TV step's YouTube launcher named Chromium; after Firefox was
    installed every later run stopped with "custom shortcut preserved", although the
    launcher was NekoServer's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.user = pwd.struct_passwd(('neko', 'x', os.getuid(), os.getgid(), '', str(self.home), '/bin/bash'))
        self.path = self.home / '.local/share/applications/nekoserver-youtube.desktop'
        self.path.parent.mkdir(parents=True)

    def place(self, browser):
        return {self.path: tv.launchers('/usr/bin/steam', browser)['nekoserver-youtube.desktop']}

    def test_its_own_out_of_date_launcher_is_brought_up_to_date(self):
        old = self.place('/usr/bin/chromium')
        self.path.write_text(old[self.path])
        self.path.chmod(0o755)
        new = self.place('/usr/bin/firefox')
        with mock.patch.object(os, 'chown'):
            tv.check_launchers(new, self.user, self.home, {})
            tv.write_launchers(new, self.user, self.home, {})
        self.assertIn('Exec=/usr/bin/firefox ', self.path.read_text())

    def test_a_launcher_someone_changed_is_kept(self):
        self.path.write_text('[Desktop Entry]\nName=My own YouTube\nExec=/usr/bin/chromium\n')
        self.path.chmod(0o755)
        browser = self.place('/usr/bin/firefox')
        with self.assertRaisesRegex(RuntimeError, 'custom shortcut preserved'):
            tv.check_launchers(browser, self.user, self.home, {})

    def test_the_recorded_checksum_also_makes_it_ours(self):
        import hashlib
        text = '[Desktop Entry]\nName=NekoServer YouTube\nExec=/usr/bin/chromium\n'
        self.path.write_text(text)
        self.path.chmod(0o755)
        recorded = {'.local/share/applications/nekoserver-youtube.desktop': hashlib.sha256(text.encode()).hexdigest()}
        tv.check_launchers(self.place('/usr/bin/firefox'), self.user, self.home, recorded)


class DebianFirefoxTests(unittest.TestCase):
    def test_debians_firefox_esr_counts_as_the_browser(self):
        found = {'firefox-esr': '/usr/bin/firefox-esr'}
        files = desktop.shortcuts({'data_mount': '/srv/nekoserver', 'dashboard': {'port': 8787}, 'media': {'services': []}},
                                  which=found.get, launcher=Path('/nonexistent'))
        self.assertIn('Exec=/usr/bin/firefox-esr\n', files['nekoserver-browser.desktop'])
