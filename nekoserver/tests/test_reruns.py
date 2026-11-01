"""v0.23.0: re-runs do not repeat finished steps, optional extras can be skipped,
and recovery never shares the USB drive with a running backup."""

import fcntl
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import host  # noqa: E402
import phone_guide  # noqa: E402
import recovery_wizard  # noqa: E402
from modules import capacity, gateway_tailnet, tv  # noqa: E402
from neko_common import find_program  # noqa: E402


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class Ctx:
    def __init__(self, config, apply=True):
        self.config, self.apply, self.logs, self.saved = config, apply, [], 0

    def log(self, message):
        self.logs.append(message)

    def persist_config(self):
        self.saved += 1


class OptionalExtrasTests(unittest.TestCase):
    def test_asked_once_and_remembered(self):
        ctx = Ctx({})
        with mock.patch.object(guided_setup, 'interactive', return_value=True):
            out = []
            self.assertFalse(
                guided_setup.wanted(ctx, 'tv', 'Use a TV', prompt=answers('maybe', 'no'), output=out.append)
            )
            self.assertFalse(ctx.config['tv']['wanted'])
            self.assertEqual(ctx.saved, 1)
            self.assertTrue(any('"wanted" to true' in line for line in out))
            # Second run: no question at all.
            self.assertFalse(guided_setup.wanted(ctx, 'tv', 'Use a TV', prompt=answers()))

    def test_nobody_to_ask_changes_nothing(self):
        ctx = Ctx({})
        with mock.patch.object(guided_setup, 'interactive', return_value=False):
            self.assertIsNone(guided_setup.wanted(ctx, 'notifications', 'Alerts'))
        self.assertNotIn('wanted', ctx.config.get('notifications', {}))
        self.assertEqual(ctx.saved, 0)


class TvTests(unittest.TestCase):
    LSPCI = (
        '00:02.0 "VGA compatible controller" "Intel Corporation" "UHD Graphics 630"\n'
        '03:00.0 "VGA compatible controller" "Advanced Micro Devices, Inc. [AMD/ATI]" "Navi 23"\n'
        '00:1f.3 "Audio device" "Intel Corporation" "Cannon Lake PCH cAVS"\n'
    )

    def test_vulkan_driver_matches_the_graphics(self):
        self.assertEqual(tv.graphics(self.LSPCI), ['intel', 'amd'])
        self.assertEqual(tv.vulkan_packages(['amd']), ['vulkan-radeon', 'lib32-vulkan-radeon'])
        self.assertEqual(tv.vulkan_packages(['nvidia']), [])
        self.assertEqual(tv.graphics('01:00.0 "3D controller" "NVIDIA Corporation" "GA107"'), ['nvidia'])

    def test_confirmed_tests_are_remembered(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'reports/tv-setup.json'
            report.parent.mkdir()
            report.write_text(
                json.dumps(
                    {
                        'checks': [
                            {'check': 'tv:controller', 'status': 'user_confirmed'},
                            {'check': 'tv:display_audio_playback', 'status': 'needs_setup'},
                        ]
                    }
                )
            )
            self.assertEqual(tv.previous_checks(folder), {'tv:controller'})
            self.assertEqual(tv.previous_checks(Path(folder) / 'missing'), set())


class TvOnEveryFamilyTests(unittest.TestCase):
    """The TV step on Debian/Ubuntu, Fedora and openSUSE Tumbleweed, not Arch alone:
    each family's own package names, what to switch on for Steam, and the two systems
    without a Steam package."""

    @staticmethod
    def family(kind, **release):
        return kind(run=lambda *args, **kwargs: None, release=release)

    def test_where_steam_comes_from_on_each_family(self):
        self.assertIn('multilib', tv.steam_source(self.family(host.Arch, ID='arch')))
        self.assertIn('contrib non-free', tv.steam_source(self.family(host.Debian, ID='debian')))
        ubuntu = tv.steam_source(self.family(host.Debian, ID='ubuntu', ID_LIKE='debian'))
        self.assertIn('multiverse', ubuntu)
        self.assertIn('dpkg --add-architecture i386', ubuntu)
        self.assertIn('rpmfusion-nonfree-steam', tv.steam_source(self.family(host.Fedora, ID='fedora')))
        self.assertIn('non-oss', tv.steam_source(self.family(host.Suse, ID='opensuse-tumbleweed')))

    def test_systems_without_a_steam_package_are_unavailable_with_the_reason(self):
        for kind, release in (
            (host.Arch, {'ID': 'arch'}),
            (host.Debian, {'ID': 'debian'}),
            (host.Fedora, {'ID': 'fedora'}),
            (host.Suse, {'ID': 'opensuse-tumbleweed'}),
            (host.Suse, {'ID': 'opensuse-slowroll'}),
        ):
            self.assertIsNone(tv.unavailable_reason(self.family(kind, **release)), release)
        rhel = tv.unavailable_reason(self.family(host.Rhel, ID='almalinux', ID_LIKE='rhel', VERSION_ID='10.2'))
        self.assertIn('AlmaLinux', rhel)
        self.assertIn('Flatpak', rhel)
        self.assertIn(
            'openSUSE Leap has no Steam package',
            tv.unavailable_reason(self.family(host.Suse, ID='opensuse-leap', VERSION_ID='16.0')),
        )

    def test_each_family_names_the_tv_packages_its_own_way(self):
        debian = self.family(host.Debian, ID='debian')
        self.assertEqual(debian.names('steam'), ['steam-installer'])
        self.assertEqual(debian.names('lib32-vulkan-intel'), ['mesa-vulkan-drivers:i386'])
        self.assertEqual(debian.names('firefox'), ['firefox-esr'])
        self.assertEqual(self.family(host.Debian, ID='ubuntu', ID_LIKE='debian').names('firefox'), ['firefox'])
        self.assertEqual(
            self.family(host.Fedora, ID='fedora').names('lib32-vulkan-radeon'), ['mesa-vulkan-drivers.i686']
        )
        suse = self.family(host.Suse, ID='opensuse-tumbleweed')
        self.assertEqual(suse.names('firefox'), ['MozillaFirefox'])
        self.assertEqual(suse.names('lib32-vulkan-intel'), ['libvulkan_intel-32bit'])
        for kind, release in (
            (host.Debian, {'ID': 'debian'}),
            (host.Fedora, {'ID': 'fedora'}),
            (host.Suse, {'ID': 'opensuse-tumbleweed'}),
        ):
            self.assertEqual(
                self.family(kind, **release).names('bluez-utils'), ['bluez'], release
            )  # bluetoothctl is inside bluez
        self.assertEqual(self.family(host.Arch, ID='arch').names('bluez-utils'), ['bluez-utils'])

    def test_debian_needs_the_i386_architecture_before_steam_counts_as_offered(self):
        # apt lists steam-installer without i386, then fails on its 32-bit libraries.
        def dpkg(architectures):
            return lambda argv, **kwargs: mock.Mock(
                returncode=0, stdout=architectures if argv[:2] == ['dpkg', '--print-foreign-architectures'] else ''
            )

        self.assertFalse(tv.steam_ready(host.Debian(dpkg(''), {'ID': 'ubuntu'}, fetch=lambda url: b'')))
        self.assertTrue(tv.steam_ready(host.Debian(dpkg('i386\n'), {'ID': 'ubuntu'}, fetch=lambda url: b'')))
        self.assertTrue(tv.steam_ready(self.family(host.Fedora, ID='fedora')))  # one package for both sides there

    def test_launchers_name_the_programs_where_this_linux_keeps_them(self):
        files = tv.launchers('/usr/games/steam', '/usr/bin/firefox-esr')  # Debian
        self.assertIn('Exec=/usr/games/steam -gamepadui\n', files['nekoserver-steam.desktop'])
        self.assertIn(
            'Exec=/usr/bin/firefox-esr --new-window https://www.youtube.com/\n', files['nekoserver-youtube.desktop']
        )
        self.assertIn('Exec=/usr/bin/steam -gamepadui\n', tv.launchers()['nekoserver-steam.desktop'])  # Arch, as before

    def test_steam_is_found_in_the_games_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            games = Path(folder) / 'usr/games'
            games.mkdir(parents=True)
            (Path(folder) / 'usr/sbin').symlink_to('bin')  # Fedora: root's PATH says sbin, the real folder is bin
            (Path(folder) / 'usr/bin').mkdir()

            def which(name, path=None):
                return {
                    ('steam', '/usr/games'): str(games / 'steam'),
                    ('firefox', None): str(Path(folder) / 'usr/sbin/firefox'),
                }.get((name, path))

            self.assertEqual(find_program('steam', which), str(games / 'steam'))
            self.assertEqual(
                find_program('firefox', which), str(Path(folder) / 'usr/bin/firefox')
            )  # named by its real folder
            self.assertIsNone(find_program('chromium', which))


class TailnetRerunTests(unittest.TestCase):
    def test_earlier_phone_confirmation_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {'data_mount': folder}
            self.assertIsNone(gateway_tailnet.confirmed_links(config))
            report = Path(folder) / 'reports/gateway-tailnet.json'
            report.parent.mkdir()
            links = {'jellyfin': 'https://neko.tail1.ts.net:8443/'}
            report.write_text(json.dumps({'client_acceptance': 'needs_setup', 'links': links}))
            self.assertIsNone(gateway_tailnet.confirmed_links(config))
            report.write_text(json.dumps({'client_acceptance': 'user_confirmed', 'links': links}))
            self.assertEqual(gateway_tailnet.confirmed_links(config), links)


class CapacityRerunTests(unittest.TestCase):
    def test_existing_install_is_never_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'docker-engine').mkdir()
            ctx = Ctx({'data_mount': folder})
            with mock.patch.object(capacity, 'assess', return_value=(['SSD too small'], ['low'])):
                self.assertIsNone(capacity.run(ctx, prompt=answers()))
            self.assertIn('Drive space: SSD too small', ctx.logs)


class PhoneGuidePathTests(unittest.TestCase):
    def test_password_command_follows_the_data_drive(self):
        config = {'data_mount': '/mnt/fastssd'}
        tool = '/mnt/fastssd/installer/releases/v' + phone_guide.VERSION + '/account-setup.py show jellyfin'
        with mock.patch.object(phone_guide, 'username', return_value=None):
            steps = phone_guide.instructions(config, 'jellyfin', 'https://neko.tail1.ts.net:8443/')
            page = phone_guide.page_html(config, {'jellyfin': 'https://neko.tail1.ts.net:8443/'}, qr=lambda text: '')
        self.assertTrue(any(tool in s for s in steps))
        self.assertIn(tool, page)
        self.assertNotIn('/srv/nekoserver', page)


class RecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_restore_refuses_when_the_drive_would_fill(self):
        source = self.root / 'src'
        source.mkdir()
        (source / 'a.jpg').write_bytes(b'x' * 1000)
        target = self.root / 'new'
        full = mock.Mock(total=100 * 1024**3, free=1024**3)
        with mock.patch.object(recovery_wizard.shutil, 'disk_usage', return_value=full):
            with self.assertRaisesRegex(RuntimeError, 'not enough free space'):
                recovery_wizard.copy_new([('upload', source)], target)
        self.assertFalse(target.exists())
        roomy = mock.Mock(total=100 * 1024**3, free=50 * 1024**3)
        with mock.patch.object(recovery_wizard.shutil, 'disk_usage', return_value=roomy):
            self.assertEqual(recovery_wizard.copy_new([('upload', source)], target), 1)

    def test_usb_is_not_used_while_the_weekly_backup_runs(self):
        lock = self.root / 'photo-backup.lock'
        holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        usb = recovery_wizard.Usb({'photos': {'external_backup_uuid': '1234-ABCD'}})
        with mock.patch.object(recovery_wizard, 'USB_LOCK', lock):
            with self.assertRaisesRegex(RuntimeError, 'weekly backup is using the USB drive'):
                usb.__enter__()
            fcntl.flock(holder, fcntl.LOCK_UN)
            with (
                mock.patch.object(recovery_wizard.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='')),
                mock.patch.object(Path, 'exists', return_value=False),
            ):
                with usb as mounted:
                    self.assertIsNone(mounted)
            # Released afterwards: the weekly job can take it again.
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == '__main__':
    unittest.main()
