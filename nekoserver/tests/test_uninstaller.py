"""The uninstaller (uninstall.sh, uninstaller.py): what it finds, what it asks, what it
removes, and what it never touches."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import uninstaller  # noqa: E402

RealPlan = uninstaller.Plan   # the tests below stand in for Plan in main(); the fake system still builds a real one

SETTINGS = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media', 'external_backup_mount': '/mnt/nekoserver-photos',
            'storage': {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}}


class FakeSystem:
    """A PC with NekoServer on it, under a temporary root; commands are recorded."""
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.projects = ['neko-jellyfin', 'neko-immich']
        self.networks = ['neko-jellyfin_default', 'bridge']

    def write(self, path, text='x\n'):
        full = self.root / path.lstrip('/')
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)
        return full

    def populate(self):
        w = self.write
        for unit in ('nekoserver-dashboard.service', 'nekoserver-maintenance.service', 'nekoserver-maintenance.timer', 'nekoserver-control.service'):
            w('/etc/systemd/system/' + unit, '[Unit]\n')
        w('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf', '# Managed by NekoServer\n')
        w('/etc/systemd/system/smb.service.d/10-nekoserver-storage.conf', '# Managed by NekoServer\n')
        w('/etc/systemd/system/other.service', '[Unit]\n')
        w('/etc/nekoserver-dashboard/settings.json', '{}')
        w('/etc/nekoserver-backups/storage.json', '{}')
        w('/etc/nekoserver-gateway/lan.json', '{}')
        w('/usr/local/lib/nekoserver-dashboard/v0.38.1/nekoserver-runtime')
        w('/usr/local/lib/nekoserver-os/v0.38.1/nekoserver-runtime')
        w('/usr/local/bin/nekoserver-dashboard', '#!/bin/sh\n# Managed by NekoServer\n')
        w('/usr/share/applications/nekoserver-dashboard.desktop', '[Desktop Entry]\nName=NekoServer Dashboard\n')
        w('/home/neko/.local/share/applications/nekoserver-jellyfin.desktop', '[Desktop Entry]\nName=Jellyfin (NekoServer)\n')
        w('/home/neko/Desktop/nekoserver-steam.desktop', '[Desktop Entry]\nName=Steam Big Picture\nComment=NekoServer\n')
        w('/home/neko/Desktop/nekoserver-mine.desktop', '[Desktop Entry]\nName=Something of my own\n')
        w('/run/nekoserver-control/originals.json', '{}')
        w('/var/lib/nekoserver-openrgb/OpenRGB/OpenRGB.json', '{}')
        w('/etc/modules-load.d/nekoserver-i2c.conf', '# Managed by NekoServer\ni2c-dev\n')
        w('/etc/samba/nekoserver.conf', '# Managed by NekoServer\n[Media]\n   path = /mnt/nekoserver-media/library\n   valid users = neko\n')
        w('/etc/samba/smb.conf', uninstaller.OWN_SMB_CONF)
        w('/etc/docker/daemon.json', json.dumps({'data-root': '/srv/nekoserver/docker-engine', 'log-driver': 'json-file'}))
        w('/etc/fstab', '# static\nUUID=cccc-3333 / ext4 defaults 0 1\nUUID=aaaa-1111 /srv/nekoserver ext4 defaults,nofail 0 2\n'
                        'UUID=bbbb-2222 /mnt/nekoserver-media ext4 defaults,nofail 0 2\nUUID=dddd-4444 /mnt/nekoserver-photos ext4 defaults,nofail 0 2\n')
        for path in ('/srv/nekoserver/appdata/jellyfin/config.xml', '/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt',
                     '/srv/nekoserver/docker-engine/overlay2/x', '/srv/nekoserver/installer/releases/v0.38.1/install.sh',
                     '/srv/nekoserver/photos/immich-library/upload/a.jpg', '/srv/nekoserver/library/downloads/music/song.mp3',
                     '/mnt/nekoserver-media/library/movies/film.mkv', '/mnt/nekoserver-media/photos/archive/2026/a.jpg',
                     '/mnt/nekoserver-media/cloud/files/neko/doc.txt', '/mnt/nekoserver-media/backups/apps/x.tar',
                     '/mnt/nekoserver-media/recovery-snapshots/first-test/manifest.json', '/mnt/nekoserver-photos/photos/2026/a.jpg',
                     '/mnt/nekoserver-photos/lost+found/keep'):
            w(path)
        return self

    def run(self, argv, timeout=0):
        self.calls.append(argv)
        if argv[:3] == ['docker', 'ps', '-a']:
            return '\n'.join(self.projects + ['', 'something-else']) + '\n'
        if argv[:3] == ['docker', 'ps', '-aq']:
            return 'c1\nc2\n'
        if argv[:3] == ['docker', 'network', 'ls']:
            return '\n'.join(self.networks) + '\n'
        if argv[0] == 'du':
            return '12M\t' + argv[-1] + '\n'
        if argv[0] == 'pdbedit':
            return 'neko:1000:\n'
        return ''

    def plan(self, group_exists=lambda name: name == 'nekoserver-control'):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return RealPlan(SETTINGS, self.root, run=self.run, group_exists=group_exists)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem(self.tmp.name).populate()
        self.plan = self.system.plan()

    def test_the_server_choice_lists_only_nekoservers_own_things(self):
        group = self.plan.groups['server']
        names = {str(p.relative_to(self.system.root)) for p in group['paths']}
        self.assertIn('etc/systemd/system/nekoserver-dashboard.service', names)
        self.assertIn('etc/systemd/system/docker.service.d/10-nekoserver-storage.conf', names)
        self.assertIn('etc/systemd/system/smb.service.d/10-nekoserver-storage.conf', names)
        self.assertIn('usr/local/lib/nekoserver-dashboard', names)
        self.assertIn('home/neko/.local/share/applications/nekoserver-jellyfin.desktop', names)
        self.assertIn('home/neko/Desktop/nekoserver-steam.desktop', names)
        self.assertIn('etc/samba/nekoserver.conf', names)
        self.assertIn('etc/samba/smb.conf', names)          # the include NekoServer wrote, nothing else in it
        self.assertIn('run/nekoserver-control', names)
        self.assertNotIn('etc/systemd/system/other.service', names)
        self.assertNotIn('home/neko/Desktop/nekoserver-mine.desktop', names)   # not NekoServer's
        self.assertFalse(any(name.startswith(('srv/', 'mnt/')) for name in names))
        self.assertEqual(self.plan.units(), ['nekoserver-control.service', 'nekoserver-dashboard.service',
                                             'nekoserver-maintenance.service', 'nekoserver-maintenance.timer'])
        notes = '\n'.join(group['notes'])
        self.assertIn('4 services and timers', notes)
        self.assertIn('with the account neko', notes)
        self.assertIn('smb.conf (only the include NekoServer wrote) goes too', notes)
        self.assertIn('tailscale serve reset', notes)
        self.assertIn('nekoserver-control group goes', notes)

    def test_someones_own_smb_conf_stays(self):
        self.system.write('/etc/samba/smb.conf', '[global]\n   workgroup = HOME\n   include = /etc/samba/nekoserver.conf\n')
        plan = self.system.plan()
        self.assertNotIn(self.system.root / 'etc/samba/smb.conf', plan.groups['server']['paths'])
        self.assertIn('your own smb.conf stays', '\n'.join(plan.groups['server']['notes']))

    def test_the_drives_are_split_into_what_is_whose(self):
        rel = lambda key: sorted(str(p.relative_to(self.system.root)) for p in self.plan.groups[key]['paths'])  # noqa: E731
        self.assertEqual(rel('data'), ['srv/nekoserver/appdata', 'srv/nekoserver/docker-engine', 'srv/nekoserver/installer', 'srv/nekoserver/secrets'])
        self.assertEqual(rel('photos'), ['mnt/nekoserver-media/photos', 'srv/nekoserver/photos'])
        self.assertEqual(rel('media'), ['mnt/nekoserver-media/library', 'srv/nekoserver/library'])
        self.assertEqual(rel('cloud'), ['mnt/nekoserver-media/cloud'])
        self.assertEqual(rel('backups'), ['mnt/nekoserver-media/backups', 'mnt/nekoserver-media/recovery-snapshots', 'mnt/nekoserver-photos/photos'])

    def test_apps_and_mounts_say_what_they_do(self):
        self.assertEqual(self.plan.groups['apps']['notes'], ['Remove the containers of neko-immich, neko-jellyfin',
                                                            'Remove the Docker networks neko-jellyfin_default'])
        notes = '\n'.join(self.plan.groups['mounts']['notes'])
        self.assertIn('UUID=aaaa-1111 /srv/nekoserver', notes)
        self.assertIn('UUID=dddd-4444 /mnt/nekoserver-photos', notes)     # by folder: the USB has no UUID in the settings
        self.assertNotIn('cccc-3333', notes)
        self.assertIn("Docker's data-root setting (/srv/nekoserver/docker-engine) is taken out", notes)
        self.assertEqual(len(self.plan.fstab_lines()), 3)

    def test_the_plan_shows_sizes_and_the_phrase_names_the_personal_data(self):
        text = self.plan.describe({'server', 'photos'})
        self.assertIn('== NekoServer itself', text)
        self.assertIn('remove ' + str(self.system.root / 'srv/nekoserver/photos') + '  (12M)', text)
        self.assertNotIn('== Your movies', text)
        self.assertEqual(self.plan.phrase({'server', 'apps'}), 'remove NekoServer')
        self.assertEqual(self.plan.phrase({'server', 'photos', 'backups', 'media'}),
                         'remove NekoServer and delete my photos, movies and music, backups')

    @unittest.skipIf(os.name != 'posix' or os.geteuid() == 0, 'needs a folder this account may not read')
    def test_a_folder_it_may_not_read_is_noted_not_fatal(self):
        # A dry run without sudo cannot look into /root: the plan says so and goes on.
        locked = self.system.root / 'root/.local'
        locked.mkdir(parents=True)
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        plan = self.system.plan()
        self.assertTrue(any('Could not look in' in w and 'run with sudo' in w for w in plan.warnings), plan.warnings)
        self.assertIn('!! Could not look in', plan.describe({'server'}))

    def test_every_path_stays_under_the_plans_root(self):
        self.assertEqual(self.plan.at('/etc/fstab'), self.system.root / 'etc/fstab')
        self.assertEqual(self.plan.at('/'), Path(os.path.normpath(str(self.system.root))))
        with self.assertRaises(RuntimeError):
            self.plan.at('/../outside')

    def test_a_pc_without_nekoserver_has_an_honest_plan(self):
        with tempfile.TemporaryDirectory() as empty:
            plan = FakeSystem(empty).plan(group_exists=lambda name: False)
            plan.run = lambda argv, timeout=0: ''
            for key in ('server', 'data', 'photos', 'mounts'):
                self.assertEqual(plan.groups[key]['paths'], [])
            self.assertIn('nothing of this is on this PC', plan.describe({'server'}))


class SettingsGateTests(unittest.TestCase):
    """A settings file names the drives' mount folders; it can never point the removal
    anywhere else."""
    def test_only_a_plain_folder_of_its_own_is_a_drive(self):
        for value in ('/', '/etc', '/usr/local', '/home/neko', '/var/lib/docker', '/srv/../etc', 'srv/nekoserver', 42, None, '/srv/nekoserver/'):
            self.assertEqual(uninstaller.mount_folder(value, '/srv/nekoserver'), '/srv/nekoserver', value)
        for value in ('/srv/nekoserver', '/mnt/nekoserver-media', '/mnt/usb', '/data/neko'):
            self.assertEqual(uninstaller.mount_folder(value, '/x'), value)

    def test_a_refused_folder_is_named_in_the_plan_and_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = FakeSystem(tmp).populate()
            system.write('/etc/passwd', 'root:x:0:0')
            settings = {**SETTINGS, 'data_mount': '/etc', 'media_mount': '/srv/../etc'}
            with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
                plan = RealPlan(settings, system.root, run=system.run, group_exists=lambda name: False)
            self.assertEqual((plan.data, plan.media), ('/srv/nekoserver', '/mnt/nekoserver-media'))
            text = plan.describe({'server', 'data'})
            self.assertIn("!! The settings name '/etc' as data_mount, which cannot be a drive of NekoServer's; /srv/nekoserver is used instead", text)
            self.assertFalse(any('etc/passwd' in str(p) for group in plan.groups.values() for p in group['paths']))
            self.assertFalse(plan.mounted('/etc'))


class ChoiceTests(unittest.TestCase):
    def test_numbers_toggle_all_takes_everything_and_the_server_is_always_on(self):
        shown = []
        answers = iter(['1 3', '3,4', 'all', '7', ''])
        chosen = uninstaller.choose(ask=lambda prompt: next(answers), out=shown.append)
        self.assertEqual(chosen, {'server', 'apps', 'data', 'photos', 'media', 'cloud', 'mounts'})
        self.assertIn(' NekoServer itself is always removed.', shown)
        self.assertTrue(any(line.startswith(' [x] 1. NekoServer itself') for line in shown))

    def test_two_confirmations_and_the_exact_phrase(self):
        plan = mock.Mock()
        plan.describe.return_value = 'the plan'
        plan.phrase.return_value = 'remove NekoServer and delete my photos'
        chosen = {'server', 'photos'}
        self.assertFalse(uninstaller.confirm(plan, chosen, ask=lambda p: 'y', out=lambda m: None))
        answers = iter(['yes', 'remove NekoServer'])
        self.assertFalse(uninstaller.confirm(plan, chosen, ask=lambda p: next(answers), out=lambda m: None))
        answers = iter(['YES', ' remove NekoServer and delete my photos '])
        shown = []
        self.assertTrue(uninstaller.confirm(plan, chosen, ask=lambda p: next(answers), out=shown.append))
        self.assertIn('the plan', shown)
        self.assertIn('Last check: nothing listed can be brought back afterwards.', shown)


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem(self.tmp.name).populate()
        self.plan = self.system.plan()
        self.shown = []

    def exists(self, path):
        return (self.system.root / path.lstrip('/')).exists()

    def remove(self, chosen):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            uninstaller.remove(self.plan, chosen, run=self.system.run, out=self.shown.append, root=self.system.root)

    def test_the_server_alone_leaves_every_drive_and_the_apps_data(self):
        self.remove({'server'})
        for gone in ('/etc/systemd/system/nekoserver-dashboard.service', '/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf',
                     '/usr/local/lib/nekoserver-dashboard', '/etc/nekoserver-gateway', '/home/neko/Desktop/nekoserver-steam.desktop',
                     '/etc/samba/nekoserver.conf', '/etc/samba/smb.conf', '/var/lib/nekoserver-openrgb', '/run/nekoserver-control'):
            self.assertFalse(self.exists(gone), gone)
        for kept in ('/etc/systemd/system/other.service', '/home/neko/Desktop/nekoserver-mine.desktop', '/srv/nekoserver/appdata/jellyfin/config.xml',
                     '/srv/nekoserver/photos/immich-library/upload/a.jpg', '/mnt/nekoserver-media/library/movies/film.mkv',
                     '/mnt/nekoserver-photos/photos/2026/a.jpg', '/etc/fstab', '/etc/docker/daemon.json'):
            self.assertTrue(self.exists(kept), kept)
        calls = self.system.calls
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-maintenance.timer'], calls)
        self.assertLess(calls.index(['systemctl', 'disable', '--now', 'nekoserver-maintenance.timer']),
                        calls.index(['systemctl', 'disable', '--now', 'nekoserver-maintenance.service']))   # timers first
        self.assertIn(['tailscale', 'serve', 'reset'], calls)
        self.assertIn(['groupdel', 'nekoserver-control'], calls)
        self.assertIn(['systemctl', 'daemon-reload'], calls)
        self.assertFalse(any(c[:2] == ['docker', 'rm'] or c[:3] == ['docker', 'network', 'rm'] for c in calls))
        self.assertFalse(any(c[0] == 'umount' for c in calls))
        self.assertEqual(json.loads((self.system.root / 'etc/docker/daemon.json').read_text())['data-root'], '/srv/nekoserver/docker-engine')
        self.assertFalse(any(c[0] in ('smbpasswd', 'pdbedit') for c in calls))   # a test root: no real Samba accounts touched

    def test_everything_goes_in_the_right_order_and_the_drives_are_only_unmounted(self):
        self.remove({key for key, *_ in uninstaller.CHOICES})
        for gone in ('/srv/nekoserver/appdata', '/srv/nekoserver/secrets', '/srv/nekoserver/docker-engine', '/srv/nekoserver/photos',
                     '/mnt/nekoserver-media/library', '/mnt/nekoserver-media/photos', '/mnt/nekoserver-media/cloud',
                     '/mnt/nekoserver-media/backups', '/mnt/nekoserver-photos/photos'):
            self.assertFalse(self.exists(gone), gone)
        self.assertTrue(self.exists('/mnt/nekoserver-photos/lost+found/keep'))
        calls = self.system.calls
        self.assertIn(['docker', 'rm', '-f', 'c1', 'c2'], calls)
        self.assertIn(['docker', 'network', 'rm', 'neko-jellyfin_default'], calls)
        self.assertNotIn(['docker', 'network', 'rm', 'bridge'], calls)
        self.assertIn(['systemctl', 'stop', 'docker.service', 'docker.socket'], calls)
        self.assertNotIn('data-root', json.loads((self.system.root / 'etc/docker/daemon.json').read_text()))
        fstab = (self.system.root / 'etc/fstab').read_text()
        self.assertEqual(fstab, '# static\nUUID=cccc-3333 / ext4 defaults 0 1\n')
        self.assertEqual([c for c in calls if c[0] == 'umount'],
                         [['umount', '/mnt/nekoserver-photos'], ['umount', '/mnt/nekoserver-media'], ['umount', '/srv/nekoserver']])
        # Docker's containers went before Docker was stopped, and both before any file.
        self.assertLess(calls.index(['docker', 'rm', '-f', 'c1', 'c2']), calls.index(['systemctl', 'stop', 'docker.service', 'docker.socket']))
        self.assertLess(calls.index(['systemctl', 'stop', 'docker.service', 'docker.socket']),
                        next(i for i, line in enumerate(self.shown) if line.startswith('  removing')) + len(calls))


class EntryTests(unittest.TestCase):
    def test_the_dry_run_shows_everything_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = FakeSystem(tmp).populate()
            shown = []
            with mock.patch.object(uninstaller, 'Plan', return_value=system.plan()), \
                    mock.patch('builtins.print', shown.append):
                self.assertEqual(uninstaller.main(['--dry-run']), 0)
            text = '\n'.join(shown)
            self.assertIn('Dry run', text)
            self.assertIn('== Your photos and videos', text)
            self.assertIn('packages of your Linux: they stay installed', text)
            self.assertTrue((system.root / 'etc/systemd/system/nekoserver-dashboard.service').exists())
            # Only looking: Docker was asked what runs, nothing was stopped or removed.
            self.assertFalse(any(c[0] in ('systemctl', 'umount') or c[:2] == ['docker', 'rm'] or c[:3] == ['docker', 'network', 'rm'] for c in system.calls))

    def test_the_shell_entry_point_hands_over_to_the_python(self):
        script = (ROOT / 'uninstall.sh').read_text()
        self.assertIn('exec python3 "$ROOT/uninstaller.py" "$@"', script)
        self.assertIn('--dry-run', script)
        self.assertIn('Run with sudo', script)

    def test_a_declined_confirmation_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = FakeSystem(tmp).populate()
            with mock.patch.object(uninstaller, 'Plan', return_value=system.plan()), \
                    mock.patch.object(uninstaller.os, 'geteuid', return_value=0), \
                    mock.patch('builtins.input', side_effect=['', 'no']), \
                    mock.patch('builtins.print'):
                self.assertEqual(uninstaller.main([]), 2)
            self.assertTrue((system.root / 'etc/systemd/system/nekoserver-dashboard.service').exists())
            self.assertFalse(any(c[0] == 'systemctl' for c in system.calls))


if __name__ == '__main__':
    unittest.main()
