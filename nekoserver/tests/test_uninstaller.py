"""The uninstaller (uninstall.sh, uninstaller.py): what it finds, what it asks, what it
removes, and what it never touches."""
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
import uninstaller  # noqa: E402

REAL_REMOVE = uninstaller.remove   # main() calls a stand-in in some tests
RealPlan = uninstaller.Plan   # the tests below stand in for Plan in main(); the fake system still builds a real one

SETTINGS = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media', 'external_backup_mount': '/mnt/nekoserver-photos',
            'storage': {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}},
            'photos': {'external_backup_uuid': 'dddd-4444'}}
HOST = 'neko.tail1234.ts.net'
MINE = {'TCP': {'8443': {'HTTPS': True}, '8448': {'HTTPS': True}},
        'Web': {HOST + ':8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8096'}}},
                HOST + ':8448': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8787'}}}}}
DRIVES = {'/srv/nekoserver': 'aaaa-1111', '/mnt/nekoserver-media': 'bbbb-2222', '/mnt/nekoserver-photos': 'dddd-4444'}


class FakeSystem:
    """A PC with NekoServer on it, under a temporary root; commands are recorded."""
    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.projects = ['neko-jellyfin', 'neko-immich']
        self.drives = dict(DRIVES)   # mount folder -> UUID of what is mounted there, or (UUID, filesystem, subvolume)
        self.removed = False         # the apps' containers
        self.failing = {}            # argv prefix (tuple) -> (exit code, what it says): a command that fails
        self.running = set()         # units still running after a stop
        self.keeps_fans = False      # the control service stops but leaves its fan records
        # Tailscale Serve as it is now: NekoServer's two routes, and one of your own.
        self.serve = {'TCP': {**MINE['TCP'], '443': {'HTTPS': True}},
                      'Web': {**MINE['Web'], HOST + ':443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3000'}}}}}
        self.networks = ['neko-jellyfin_default', 'bridge']
        self.units_on = {'smbd.service'}   # Samba: switched on (by NekoServer's setup)

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
        w('/srv/nekoserver/gateway/tailnet-owned.json', json.dumps({'format': 'neko-tailnet-owned-v1', 'host': HOST, 'owned': MINE,
                                                                      'status': 'configured'}))
        w('/usr/local/lib/nekoserver-dashboard/v0.38.1/nekoserver-runtime')
        w('/usr/local/lib/nekoserver-os/v0.38.1/nekoserver-runtime')
        w('/usr/local/bin/nekoserver-dashboard', '#!/bin/sh\n# Managed by NekoServer\n')
        w('/usr/share/applications/nekoserver-dashboard.desktop', '[Desktop Entry]\nName=NekoServer Dashboard\n')
        w('/home/neko/.local/share/applications/nekoserver-jellyfin.desktop', '[Desktop Entry]\nName=Jellyfin (NekoServer)\n')
        w('/home/neko/Desktop/nekoserver-steam.desktop', '[Desktop Entry]\nName=Steam Big Picture\nComment=NekoServer\n')
        w('/home/neko/Desktop/nekoserver-mine.desktop', '[Desktop Entry]\nName=Something of my own\n')
        w('/run/nekoserver-control/originals.json', '{}')
        w('/run/nekoserver-setup.lock', '')
        w('/run/nekoserver-install.lock', '')
        w('/var/lib/systemd/timers/stamp-nekoserver-maintenance.timer', '')
        w('/var/lib/systemd/timers/stamp-other.timer', '')
        w('/var/lib/nekoserver-openrgb/OpenRGB/OpenRGB.json', '{}')
        w('/etc/modules-load.d/nekoserver-i2c.conf', '# Managed by NekoServer\ni2c-dev\n')
        for dropin in uninstaller.OS_DROPINS:
            w(dropin, uninstaller.OS_MARKER + '\n[Sleep]\n')
        w('/etc/systemd/logind.conf.d/50-mine.conf', '[Login]\nHandleLidSwitch=ignore\n')     # someone's own
        for target in uninstaller.SLEEP_TARGETS:
            link = self.root / 'etc/systemd/system' / target
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to('/dev/null')
        # hibernate was masked before NekoServer (this PC is never meant to hibernate)
        w('/etc/nekoserver-os/sleep-masks-before.json', json.dumps({'format': 'neko-sleep-masks-v1', 'masked_before': ['hibernate.target']}))
        w('/etc/samba/nekoserver.conf', '# Managed by NekoServer\n[Media]\n   path = /mnt/nekoserver-media/library\n   valid users = neko\n')
        w('/etc/samba/smb.conf', uninstaller.OWN_SMB_CONF)
        w('/etc/samba/nekoserver-accounts', '# Managed by NekoServer\nneko\n')   # setup created the account
        w('/etc/samba/smb.conf.packaged', '# Sample configuration file for the Samba suite for Debian GNU/Linux.\n[global]\n')
        w('/etc/docker/daemon.json', json.dumps({'data-root': '/srv/nekoserver/docker-engine', 'log-driver': 'json-file'}))
        w('/etc/fstab', '# static\nUUID=cccc-3333 / ext4 defaults 0 1\nUUID=aaaa-1111 /srv/nekoserver ext4 defaults,nofail 0 2\n'
                        'UUID=bbbb-2222 /mnt/nekoserver-media ext4 defaults,nofail 0 2\nUUID=dddd-4444 /mnt/nekoserver-photos ext4 defaults,nofail 0 2\n')
        for path in ('/srv/nekoserver/appdata/jellyfin/config.xml', '/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt',
                     '/srv/nekoserver/docker-engine/overlay2/x', '/srv/nekoserver/installer/releases/v0.38.1/install.sh',
                     '/srv/nekoserver/photos/immich-library/upload/a.jpg', '/srv/nekoserver/library/downloads/music/song.mp3',
                     '/mnt/nekoserver-media/library/movies/film.mkv', '/mnt/nekoserver-media/photos/archive/2026/a.jpg',
                     '/mnt/nekoserver-media/cloud/files/neko/doc.txt', '/mnt/nekoserver-media/backups/apps/x.tar',
                     '/mnt/nekoserver-media/recovery-snapshots/first-test/manifest.json', '/mnt/nekoserver-photos/upload/2026/a.jpg',
                     '/mnt/nekoserver-photos/.neko-ledger.json',
                     '/mnt/nekoserver-photos/lost+found/keep'):
            w(path)
        return self

    def run(self, argv, timeout=0):
        self.calls.append(argv)
        if argv[:3] == ['docker', 'ps', '-a']:
            return '\n'.join(self.projects + ['', 'something-else']) + '\n'
        if argv[:3] == ['docker', 'ps', '-aq']:
            return '' if self.removed else 'c1\nc2\n'
        if argv[:3] == ['docker', 'network', 'ls']:
            return '\n'.join(self.networks) + '\n'
        if argv[0] == 'du':
            return '12M\t' + argv[-1] + '\n'
        if argv[:3] == ['tailscale', 'serve', 'status']:
            return json.dumps(self.serve)
        if argv[0] == 'findmnt':
            mountpoint = '/' + str(Path(argv[-1]).relative_to(self.root))
            return self.drives.get(mountpoint, '') + '\n'
        if argv[0] == 'pdbedit':
            return 'neko:1000:\n'
        if argv[:2] == ['systemctl', 'show']:
            on = argv[2] in self.units_on
            return ('LoadState=loaded\nUnitFileState=' + ('enabled' if on else 'disabled') + '\nActiveState='
                    + ('active' if on else 'inactive') + '\n')
        return ''

    def call(self, argv, timeout=0):
        """A command that changes something: (exit code, what it said)."""
        self.calls.append(argv)
        for prefix, answer in self.failing.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        if argv[:2] == ['systemctl', 'is-active']:
            return (0, 'active') if argv[-1] in self.running else (3, 'inactive')
        if argv[:3] == ['systemctl', 'disable', '--now'] and argv[-1] == 'nekoserver-control.service' and not self.keeps_fans:
            for record in ('run/nekoserver-control/originals.json', 'run/nekoserver-control/originals-detect.json'):
                (self.root / record).unlink(missing_ok=True)
        if argv[:2] == ['docker', 'rm']:
            self.removed = True
        return 0, ''

    def identity(self, path):
        """neko_common.mounted_identity on this PC: (UUID, filesystem, subvolume), or None."""
        found = self.drives.get('/' + str(Path(path).relative_to(self.root)))
        return (found, 'ext4', '') if isinstance(found, str) else found

    def plan(self, group_exists=lambda name: name == 'nekoserver-control', settings=SETTINGS):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return RealPlan(settings, self.root, run=self.run, group_exists=group_exists, identity=self.identity)


class FakeSubvolume:
    """os as it answers for a Btrfs subvolume named `name`: its folder and everything in
    it on a device number of its own, the folder's inode 256. Its mount is the one of the
    folder around it (same_mount) or another (a drive mounted there). read_only: nothing
    in it can be deleted (a read-only snapshot)."""
    def __init__(self, name, same_mount=True, read_only=False):
        self.name, self.same_mount, self.read_only = name, same_mount, read_only
        self.inside = set()   # the folders opened in it
        self.real = {key: getattr(os, key) for key in ('stat', 'fstat', 'open', 'unlink')}

    def moved(self, found, inode=None):
        values = list(found)
        values[2] += 1
        if inode is not None:
            values[1] = inode
        return os.stat_result(values)

    def stat(self, path, *args, dir_fd=None, **kwargs):
        found = self.real['stat'](path, *args, dir_fd=dir_fd, **kwargs)
        if path == self.name:
            return self.moved(found, uninstaller.BTRFS_SUBVOLUME_ROOT)
        return self.moved(found) if dir_fd in self.inside else found

    def fstat(self, fd):
        found = self.real['fstat'](fd)
        return self.moved(found) if fd in self.inside else found

    def open(self, path, flags, *args, dir_fd=None, **kwargs):
        fd = self.real['open'](path, flags, *args, dir_fd=dir_fd, **kwargs)
        if path == self.name or dir_fd in self.inside:
            self.inside.add(fd)
        return fd

    def unlink(self, path, *args, dir_fd=None, **kwargs):
        if self.read_only and dir_fd in self.inside:
            raise OSError(30, 'Read-only file system')
        return self.real['unlink'](path, *args, dir_fd=dir_fd, **kwargs)

    def mount_id(self, fd):
        return 'other' if fd in self.inside and not self.same_mount else 'drive'

    def patched(self):
        from contextlib import ExitStack
        stack = ExitStack()
        for key in self.real:
            stack.enter_context(mock.patch.object(uninstaller.os, key, side_effect=getattr(self, key)))
        stack.enter_context(mock.patch.object(uninstaller, '_mount_id', side_effect=self.mount_id))
        return stack


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
        # The setup locks the uninstall holds stay (deleted, a setup could take a new one meanwhile).
        self.assertNotIn('run/nekoserver-setup.lock', names)
        self.assertNotIn('run/nekoserver-install.lock', names)
        # When each timer last ran: left behind, a later setup would run every backup at once.
        self.assertIn('var/lib/systemd/timers/stamp-nekoserver-maintenance.timer', names)
        self.assertNotIn('var/lib/systemd/timers/stamp-other.timer', names)
        self.assertNotIn('etc/systemd/system/other.service', names)
        self.assertNotIn('home/neko/Desktop/nekoserver-mine.desktop', names)   # not NekoServer's
        self.assertFalse(any(name.startswith(('srv/', 'mnt/')) for name in names))
        self.assertEqual(self.plan.units(), ['nekoserver-control.service', 'nekoserver-dashboard.service',
                                             'nekoserver-maintenance.service', 'nekoserver-maintenance.timer'])
        notes = '\n'.join(group['notes'])
        self.assertIn('4 services and timers', notes)
        self.assertIn('with the account neko it created', notes)
        self.assertIn('smb.conf (only the include NekoServer wrote) goes too', notes)
        self.assertIn("the package's example smb.conf, set aside by setup, comes back", notes)
        self.assertIn('switched off (ports 8443, 8448); any of your own stay', notes)
        self.assertIn('nekoserver-control group goes', notes)

    def test_someones_own_smb_conf_stays(self):
        self.system.write('/etc/samba/smb.conf', '[global]\n   workgroup = HOME\n   include = /etc/samba/nekoserver.conf\n')
        plan = self.system.plan()
        self.assertNotIn(self.system.root / 'etc/samba/smb.conf', plan.groups['server']['paths'])
        self.assertIn('your own smb.conf stays', '\n'.join(plan.groups['server']['notes']))

    def test_the_drives_are_split_into_what_is_whose(self):
        rel = lambda key: sorted(str(p.relative_to(self.system.root)) for p in self.plan.groups[key]['paths'])  # noqa: E731
        self.assertEqual(rel('data'), ['srv/nekoserver/appdata', 'srv/nekoserver/docker-engine', 'srv/nekoserver/gateway', 'srv/nekoserver/installer', 'srv/nekoserver/secrets'])
        self.assertEqual(rel('photos'), ['mnt/nekoserver-media/photos', 'srv/nekoserver/photos'])
        self.assertEqual(rel('media'), ['mnt/nekoserver-media/library', 'srv/nekoserver/library'])
        self.assertEqual(rel('cloud'), ['mnt/nekoserver-media/cloud'])
        self.assertEqual(rel('backups'), ['mnt/nekoserver-media/backups', 'mnt/nekoserver-media/recovery-snapshots', 'mnt/nekoserver-photos/.neko-ledger.json', 'mnt/nekoserver-photos/upload'])

    def test_your_own_folders_on_the_drives_stay_whatever_is_chosen(self):
        """The reported case: a folder of your own at the top of the SSD or the USB drive was
        taken for the apps' data (or the backups) and deleted."""
        for path in ('/srv/nekoserver/PersonalDocuments/taxes.pdf', '/mnt/nekoserver-photos/Holiday 2019/a.jpg',
                     '/mnt/nekoserver-media/MyStuff/notes.txt'):
            self.system.write(path)
        plan = self.system.plan()
        chosen = {key for key, *_ in uninstaller.CHOICES}
        planned = {str(p) for key in chosen for p in plan.groups[key]['paths']}
        for name in ('srv/nekoserver/PersonalDocuments', 'mnt/nekoserver-photos/Holiday 2019', 'mnt/nekoserver-media/MyStuff'):
            self.assertNotIn(str(self.system.root / name), planned)
        description = plan.describe(chosen)
        self.assertIn("Not NekoServer's, so these stay", description)
        self.assertIn('PersonalDocuments', description)
        self.assertIn('Holiday 2019', description)
        self.assertNotIn('lost+found', description)

    def test_a_drive_that_is_not_the_one_in_the_settings_loses_nothing(self):
        self.system.drives['/srv/nekoserver'] = 'ffff-9999'    # another drive mounted at the SSD's folder
        del self.system.drives['/mnt/nekoserver-photos']        # the USB drive not mounted
        plan = self.system.plan()
        self.assertEqual(plan.groups['data']['paths'], [])
        self.assertFalse(any('srv/nekoserver' in str(p) for key in ('photos', 'media', 'backups') for p in plan.groups[key]['paths']))
        self.assertFalse(any('nekoserver-photos' in str(p) for p in plan.groups['backups']['paths']))
        self.assertTrue(plan.groups['cloud']['paths'])          # the HDD is the right one: its choices still work
        warnings = '\n'.join(plan.warnings)
        self.assertIn('/srv/nekoserver is not the one the settings name (UUID ffff-9999, not aaaa-1111)', warnings)
        self.assertIn('/mnt/nekoserver-photos is not mounted', warnings)

    def test_another_btrfs_subvolume_at_the_folder_is_not_nekoservers_drive(self):
        # The SSD's own filesystem, but its top level (or another subvolume) mounted at the
        # folder: another tree, so nothing on it is NekoServer's to remove.
        settings = json.loads(json.dumps(SETTINGS))
        settings['storage']['data_ssd'].update(fstype='btrfs', subvolume='/@data')
        self.system.drives['/srv/nekoserver'] = ('aaaa-1111', 'btrfs', '')
        plan = self.system.plan(settings=settings)
        self.assertEqual(plan.groups['data']['paths'], [])
        self.assertIn('/srv/nekoserver is the right drive, but another part of it is mounted there (its top level, not /@data)',
                      '\n'.join(plan.warnings))
        self.system.drives['/srv/nekoserver'] = ('aaaa-1111', 'btrfs', '/@data')
        self.assertTrue(self.system.plan(settings=settings).groups['data']['paths'])   # its own subvolume: identified

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

    def test_without_sudo_a_size_it_cannot_count_is_said_not_understated(self):
        """IT-07: as the user, du counts only the private installer folder itself (4.0K)."""
        installer = self.system.root / 'srv/nekoserver/installer'

        def run(argv, timeout=0):
            if argv[0] == 'find':   # find names the first thing it may not read
                return str(installer) + '\n' if argv[1] == str(installer) else ''
            return self.system.run(argv, timeout)
        self.plan.run = run
        with mock.patch.object(uninstaller.os, 'geteuid', return_value=1000):
            text = self.plan.describe({'server', 'data'})
        self.assertIn('remove ' + str(installer) + '  (size unknown without sudo)', text)
        self.assertIn('remove ' + str(self.system.root / 'srv/nekoserver/appdata') + '  (12M)', text)
        with mock.patch.object(uninstaller.os, 'geteuid', return_value=0):   # with sudo: du alone, nothing to look up
            self.assertEqual(self.plan.size(installer), '12M')

    @unittest.skipIf(os.name != 'posix' or os.geteuid() == 0, 'needs a folder this account may not read')
    def test_the_real_lookup_finds_a_folder_it_may_not_read(self):
        locked = self.system.root / 'srv/nekoserver/installer/state'
        locked.mkdir(parents=True)
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        plan = self.system.plan()
        plan.run = uninstaller.run
        self.assertEqual(plan.size(locked.parent), uninstaller.SIZE_UNKNOWN)
        self.assertNotEqual(plan.size(self.system.root / 'srv/nekoserver/appdata'), uninstaller.SIZE_UNKNOWN)

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

    def test_permission_denied_is_not_taken_for_no_folder(self):
        # Since Python 3.14 Path.is_dir() says False for "permission denied" too; the plan
        # must still notice the folder it could not look into (this runs as root as well).
        folder = mock.Mock()
        folder.stat.side_effect = PermissionError(13, 'Permission denied')
        with self.assertRaises(PermissionError):
            uninstaller.Plan.folder_here(folder)
        self.assertFalse(uninstaller.Plan.folder_here(self.system.root / 'not-there'))
        self.assertTrue(uninstaller.Plan.folder_here(self.system.root))

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
            plan = system.plan(group_exists=lambda name: False, settings=settings)
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
            uninstaller.remove(self.plan, chosen, run=self.system.run, out=self.shown.append, root=self.system.root,
                               call=self.system.call, sleep=lambda seconds: None)

    def unfinished_install(self):
        """What a package installation cut off on Debian leaves: its record, NekoServer's
        policy-rc.d, and the masks on the services it brings (one masked by its package)."""
        record = self.system.root / 'var/lib/nekoserver-host/package-install.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'format': 'neko-package-install-v1', 'packages': ['docker', 'samba'],
                                      'before': {'new_units': ['docker.service', 'docker.socket', 'smbd.service', 'x; rm -rf /']}}))
        policy = self.system.root / 'usr/sbin/policy-rc.d'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(uninstaller.host.OUR_POLICY)
        package_mask = self.system.root / uninstaller.host.PACKAGE_MASKS.lstrip('/') / 'smbd.service'
        package_mask.parent.mkdir(parents=True, exist_ok=True)
        package_mask.write_text('')
        self.plan = self.system.plan()
        return record, policy

    def test_an_unfinished_package_installation_is_finished_before_its_record_goes(self):
        record, policy = self.unfinished_install()
        self.assertIn('package installation setup left unfinished', self.plan.describe({'server'}))
        finished = []

        def reconcile():
            finished.append(record.exists())
            record.unlink()
            policy.unlink()
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            uninstaller.remove(self.plan, {'server'}, run=self.system.run, out=self.shown.append, root=self.system.root,
                               call=self.system.call, sleep=lambda seconds: None, reconcile=reconcile)
        self.assertEqual(finished, [True])          # while its record was still there
        self.assertFalse(self.exists('/var/lib/nekoserver-host'))

    def test_when_it_cannot_be_finished_its_protection_is_lifted(self):
        _, policy = self.unfinished_install()

        def reconcile():
            raise RuntimeError('dpkg could not finish setting up docker-ce')
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            uninstaller.remove(self.plan, {'server'}, run=self.system.run, out=self.shown.append, root=self.system.root,
                               call=self.system.call, sleep=lambda seconds: None, reconcile=reconcile)
        self.assertFalse(policy.exists())
        self.assertIn(['systemctl', 'unmask', 'docker.service', 'docker.socket'], self.system.calls)   # not the package's own mask
        self.assertTrue(any('could not finish it (dpkg could not finish' in line for line in self.shown))
        self.assertFalse(self.exists('/var/lib/nekoserver-host'))

    def test_someone_elses_policy_is_left_alone(self):
        policy = self.system.root / 'usr/sbin/policy-rc.d'
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text('#!/bin/sh\nexit 101\n')
        self.plan = self.system.plan()
        self.remove({'server'})
        self.assertTrue(policy.exists())
        self.assertFalse(any('unfinished' in line for line in self.shown))

    def test_setups_records_on_the_system_disk_go_and_the_fstab_copy_is_said_to_stay(self):
        """IT-09: setup's state from a run without the SSD, and its first copy of /etc/fstab."""
        for name in ('install.log', 'state.json', 'state.json.moved-to-ssd'):
            self.system.write('/root/.local/state/nekoserver/' + name, '{}')
        self.system.write('/etc/fstab.nekoserver.bak', '# static\n')
        self.plan = self.system.plan()
        state = self.system.root / 'root/.local/state/nekoserver'
        text = self.plan.describe({'server'})
        self.assertIn('remove ' + str(state) + '\n', text + '\n')
        self.assertIn('Kept: /etc/fstab.nekoserver.bak, your /etc/fstab as it was before NekoServer first changed it', text)
        self.remove({'server'})
        self.assertFalse(state.exists())
        self.assertTrue(self.exists('/root/.local'))   # only NekoServer's own folder goes
        self.assertTrue(self.exists('/etc/fstab.nekoserver.bak'))

    def test_a_socket_unit_is_stopped_before_its_service_and_removed(self):
        # 0.40.1's OpenRGB proxy starts from a socket: left on, it would start the service again.
        for unit in ('nekoserver-openrgb-proxy.socket', 'nekoserver-openrgb-proxy.service'):
            self.system.write('/etc/systemd/system/' + unit, '[Unit]\n')
        self.plan = self.system.plan()
        self.remove({'server'})
        calls = self.system.calls
        stop = ['systemctl', 'disable', '--now']
        self.assertLess(calls.index(stop + ['nekoserver-openrgb-proxy.socket']), calls.index(stop + ['nekoserver-openrgb-proxy.service']))
        for unit in ('nekoserver-openrgb-proxy.socket', 'nekoserver-openrgb-proxy.service'):
            self.assertFalse(self.exists('/etc/systemd/system/' + unit), unit)

    def test_the_server_alone_leaves_every_drive_and_the_apps_data(self):
        self.remove({'server'})
        for gone in ('/etc/systemd/system/nekoserver-dashboard.service', '/etc/systemd/system/smb.service.d/10-nekoserver-storage.conf',
                     '/usr/local/lib/nekoserver-dashboard', '/etc/nekoserver-gateway', '/home/neko/Desktop/nekoserver-steam.desktop',
                     '/etc/samba/nekoserver.conf', '/etc/samba/smb.conf.packaged', '/var/lib/nekoserver-openrgb', '/run/nekoserver-control',
                     '/var/lib/systemd/timers/stamp-nekoserver-maintenance.timer'):
            self.assertFalse(self.exists(gone), gone)
        self.assertTrue(self.exists('/var/lib/systemd/timers/stamp-other.timer'))
        # The package's example smb.conf, which setup had set aside, is smb.conf again.
        self.assertTrue((self.system.root / 'etc/samba/smb.conf').read_text().startswith('# Sample configuration file'))
        for kept in ('/etc/systemd/system/other.service', '/home/neko/Desktop/nekoserver-mine.desktop', '/srv/nekoserver/appdata/jellyfin/config.xml',
                     '/srv/nekoserver/photos/immich-library/upload/a.jpg', '/mnt/nekoserver-media/library/movies/film.mkv',
                     '/mnt/nekoserver-photos/upload/2026/a.jpg', '/etc/fstab', '/etc/docker/daemon.json',
                     # Docker keeps its data-root on the SSD, so its start guard stays with it.
                     '/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf'):
            self.assertTrue(self.exists(kept), kept)
        calls = self.system.calls
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-maintenance.timer'], calls)
        self.assertLess(calls.index(['systemctl', 'disable', '--now', 'nekoserver-maintenance.timer']),
                        calls.index(['systemctl', 'disable', '--now', 'nekoserver-maintenance.service']))   # timers first
        self.assertIn(['tailscale', 'serve', '--bg', '--https=8443', 'off'], calls)
        self.assertIn(['tailscale', 'serve', '--bg', '--https=8448', 'off'], calls)
        self.assertFalse(any(c[:3] == ['tailscale', 'serve', 'reset'] or '--https=443' in c for c in calls))   # your own route stays
        self.assertIn(['groupdel', 'nekoserver-control'], calls)
        self.assertIn(['systemctl', 'daemon-reload'], calls)
        self.assertFalse(any(c[:2] == ['docker', 'rm'] or c[:3] == ['docker', 'network', 'rm'] for c in calls))
        self.assertFalse(any(c[0] == 'umount' for c in calls))
        self.assertEqual(json.loads((self.system.root / 'etc/docker/daemon.json').read_text())['data-root'], '/srv/nekoserver/docker-engine')
        self.assertIn(['smbpasswd', '-x', 'neko'], calls)        # the account setup created
        self.assertFalse(self.exists('/etc/samba/nekoserver-accounts'))

    def test_a_samba_account_that_was_there_before_stays(self):
        """The reported case: setup reused an existing Samba account, and uninstall deleted it
        (shares of your own that use it would then refuse you)."""
        (self.system.root / 'etc/samba/nekoserver-accounts').unlink()   # setup reused 'neko', so it never wrote it down
        self.plan = self.system.plan()
        notes = '\n'.join(self.plan.groups['server']['notes'])
        self.assertIn('the account neko was there before NekoServer and stays', notes)
        self.remove({'server'})
        self.assertFalse(any(c[0] == 'smbpasswd' for c in self.system.calls))
        self.assertFalse(self.exists('/etc/samba/nekoserver.conf'))     # NekoServer's shares still go

    def test_tailscale_is_left_alone_without_a_record_of_nekoservers_routes(self):
        """The reported case: home-network access only (no Tailscale routes of NekoServer's), and
        uninstall cleared every Serve route with  tailscale serve reset."""
        (self.system.root / 'srv/nekoserver/gateway/tailnet-owned.json').unlink()
        self.plan = self.system.plan()
        self.remove({'server'})
        self.assertFalse(any(c[0] == 'tailscale' and c[:3] != ['tailscale', 'serve', 'status'] for c in self.system.calls))

    def test_a_route_changed_since_setup_stays(self):
        self.system.serve['Web'][HOST + ':8443'] = {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}   # changed by you
        self.plan = self.system.plan()
        self.assertEqual(self.plan.served, ['8448'])

    def test_the_pc_may_sleep_again_but_masks_from_before_stay(self):
        """The reported case: after uninstall the PC still could not suspend or hibernate, with
        NekoServer's log limits still on."""
        notes = '\n'.join(self.plan.groups['server']['notes'])
        self.assertIn('The PC may sleep again', notes)
        self.assertIn('unmasked: sleep.target, suspend.target, hybrid-sleep.target, suspend-then-hibernate.target', notes)
        self.remove({'server'})
        for dropin in uninstaller.OS_DROPINS:
            self.assertFalse(self.exists(dropin), dropin)
        self.assertTrue(self.exists('/etc/systemd/logind.conf.d/50-mine.conf'))
        self.assertIn(['systemctl', 'unmask', 'sleep.target', 'suspend.target', 'hybrid-sleep.target', 'suspend-then-hibernate.target'],
                      self.system.calls)
        self.assertIn(['systemctl', 'restart', 'systemd-journald.service'], self.system.calls)

    def test_without_a_record_the_masks_are_nekoservers_only_when_its_settings_are_there(self):
        (self.system.root / 'etc/nekoserver-os/sleep-masks-before.json').unlink()
        self.assertEqual(self.system.plan().sleep_masks(), list(uninstaller.SLEEP_TARGETS))
        for dropin in uninstaller.OS_DROPINS:
            (self.system.root / dropin.lstrip('/')).unlink()
        self.assertEqual(self.system.plan().sleep_masks(), [])   # masked by someone else: left alone

    def assert_nothing_deleted(self):
        for kept in ('/etc/systemd/system/nekoserver-control.service', '/run/nekoserver-control', '/srv/nekoserver/appdata',
                     '/srv/nekoserver/docker-engine', '/etc/samba/nekoserver.conf', '/mnt/nekoserver-media/library'):
            self.assertTrue(self.exists(kept), kept)

    def test_a_service_that_does_not_stop_ends_the_uninstall_before_any_deletion(self):
        """The reported case: the control service failed to stop and the uninstall went on to
        delete its definition and the fan records."""
        self.system.failing[('systemctl', 'disable', '--now', 'nekoserver-control.service')] = (1, 'Job for nekoserver-control.service failed')
        self.system.running.add('nekoserver-control.service')
        with self.assertRaisesRegex(uninstaller.Stopped, r'nekoserver-control.service \(Job for nekoserver-control.service failed\).*Nothing has been deleted'):
            self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assert_nothing_deleted()
        self.assertFalse(any(c[:2] == ['docker', 'rm'] for c in self.system.calls))

    def test_a_stop_that_times_out_counts_as_not_stopped(self):
        self.system.failing[('systemctl', 'disable', '--now', 'nekoserver-maintenance.service')] = (None, 'did not finish within 120 seconds')
        with self.assertRaisesRegex(uninstaller.Stopped, 'nekoserver-maintenance.service \\(did not finish within 120 seconds\\)'):
            self.remove({'server'})
        self.assert_nothing_deleted()

    def test_fans_not_handed_back_end_the_uninstall(self):
        self.system.keeps_fans = True
        with self.assertRaisesRegex(uninstaller.Stopped, 'not handed back to the motherboard'):
            self.remove({'server'})
        self.assert_nothing_deleted()

    def test_containers_or_docker_that_do_not_go_keep_the_data(self):
        self.system.failing[('docker', 'rm')] = (1, 'Error response from daemon: container is busy')
        with self.assertRaisesRegex(uninstaller.Stopped, 'containers of neko-immich could not be removed \\(Error response from daemon: container is busy\\)'):
            self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assert_nothing_deleted()
        self.system.failing = {('systemctl', 'stop', 'docker.service'): (1, 'Failed to stop docker.service')}
        with self.assertRaisesRegex(uninstaller.Stopped, 'Docker did not stop \\(Failed to stop docker.service\\)'):
            self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assert_nothing_deleted()
        self.assertEqual(json.loads((self.system.root / 'etc/docker/daemon.json').read_text())['data-root'], '/srv/nekoserver/docker-engine')

    def test_everything_goes_in_the_right_order_and_the_drives_are_only_unmounted(self):
        self.remove({key for key, *_ in uninstaller.CHOICES})
        for gone in ('/srv/nekoserver/appdata', '/srv/nekoserver/secrets', '/srv/nekoserver/docker-engine', '/srv/nekoserver/photos',
                     '/mnt/nekoserver-media/library', '/mnt/nekoserver-media/photos', '/mnt/nekoserver-media/cloud',
                     '/mnt/nekoserver-media/backups', '/mnt/nekoserver-photos/upload', '/mnt/nekoserver-photos/.neko-ledger.json'):
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



class ReviewFixTests(unittest.TestCase):
    """The uninstaller review of 0.39.5: a stopped uninstall puts the services back, system
    files are replaced in one step, it waits for a monthly update or a setup, and it says
    what it could not remove."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem(self.tmp.name).populate()
        self.plan = self.system.plan()
        self.shown = []

    def remove(self, chosen):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return uninstaller.remove(self.plan, chosen, run=self.system.run, out=self.shown.append, root=self.system.root,
                                      call=self.system.call, sleep=lambda seconds: None)

    def test_a_stopped_uninstall_switches_the_services_back_on(self):
        """The reported case: every service and timer was disabled, then the containers could
        not be removed, and the message said nothing had been changed."""
        for unit in ('nekoserver-dashboard.service', 'nekoserver-maintenance.timer'):
            self.system.failing[('systemctl', 'is-enabled', unit)] = (0, 'enabled')
        self.system.failing[('systemctl', 'is-active', 'nekoserver-dashboard.service')] = (0, 'active')
        self.system.failing[('docker', 'rm')] = (1, 'container is busy')
        with self.assertRaisesRegex(uninstaller.Stopped, 'switched back on as they were'):
            self.remove({key for key, *_ in uninstaller.CHOICES})
        calls = self.system.calls
        self.assertIn(['systemctl', 'enable', 'nekoserver-dashboard.service'], calls)
        self.assertIn(['systemctl', 'enable', 'nekoserver-maintenance.timer'], calls)
        self.assertIn(['systemctl', 'start', 'nekoserver-dashboard.service'], calls)
        self.assertNotIn(['systemctl', 'enable', 'nekoserver-control.service'], calls)   # it was not on

    def test_one_that_cannot_be_switched_back_on_is_named(self):
        self.system.failing[('systemctl', 'is-enabled', 'nekoserver-dashboard.service')] = (0, 'enabled')
        self.system.failing[('systemctl', 'enable', 'nekoserver-dashboard.service')] = (1, 'Failed')
        self.system.failing[('docker', 'rm')] = (1, 'container is busy')
        with self.assertRaisesRegex(uninstaller.Stopped, 'could not be switched back on: nekoserver-dashboard.service.*sudo bash install.sh'):
            self.remove({key for key, *_ in uninstaller.CHOICES})

    def test_fstab_and_daemon_json_are_replaced_in_one_step_with_a_copy_kept(self):
        fstab = self.system.root / 'etc/fstab'
        before = fstab.read_text()
        fstab.chmod(0o644)
        with mock.patch('neko_common.os.replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assertEqual(fstab.read_text(), before)
        daemon = json.loads((self.system.root / 'etc/docker/daemon.json').read_text())
        self.assertEqual(daemon['data-root'], '/srv/nekoserver/docker-engine')   # never half written
        self.system = FakeSystem(tempfile.mkdtemp(dir=self.tmp.name)).populate()
        self.plan = self.system.plan()
        fstab = self.system.root / 'etc/fstab'
        fstab.chmod(0o644)
        self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assertEqual(fstab.read_text(), '# static\nUUID=cccc-3333 / ext4 defaults 0 1\n')
        self.assertEqual(fstab.stat().st_mode & 0o777, 0o644)
        [copy] = fstab.parent.glob('fstab.before-nekoserver-uninstall-*')
        self.assertEqual(copy.read_text(), before)
        self.assertTrue(list(fstab.parent.glob('docker/daemon.json.before-nekoserver-uninstall-*')))

    def test_a_drive_that_cannot_be_unmounted_is_said_and_listed(self):
        self.system.failing[('umount', '/srv/nekoserver')] = (32, 'target is busy')
        left = self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assertIn('/srv/nekoserver (still mounted)', left)
        self.assertTrue(any('could not unmount (busy: target is busy): sudo umount /srv/nekoserver' in line for line in self.shown), self.shown)

    def test_what_could_not_be_removed_is_listed(self):
        stubborn = self.system.root / 'etc/nekoserver-gateway'
        left = self.remove_with(lambda path, **handlers: None, {'server'})
        self.assertIn(str(stubborn), left)

    def remove_with(self, rmtree, chosen):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return uninstaller.remove(self.plan, chosen, run=self.system.run, out=self.shown.append, root=self.system.root,
                                      call=self.system.call, sleep=lambda seconds: None, rmtree=rmtree)

    def test_dockers_start_guard_goes_only_with_its_ssd_data_root(self):
        guard = self.system.root / 'etc/systemd/system/docker.service.d/10-nekoserver-storage.conf'
        self.assertNotIn('remove ' + str(guard), self.plan.describe({'server', 'apps'}))
        self.assertIn('remove ' + str(guard), self.plan.describe({'server', 'data'}))
        self.remove({'server', 'apps'})
        self.assertTrue(guard.exists())

    def test_it_waits_for_a_monthly_update_or_a_running_setup(self):
        journal = Path(self.tmp.name) / 'maintenance-resume.json'
        journal.write_text('{"version": "0.39.5"}')
        with mock.patch('neko_common.maintenance_running', return_value=False):
            blocked, held = uninstaller.busy(locks=(), journal=journal)
        self.assertIn('did not finish', blocked)
        self.assertIn('Nothing was removed', blocked)
        with mock.patch('neko_common.maintenance_running', return_value=True):
            blocked, _ = uninstaller.busy(locks=(), journal=Path(self.tmp.name) / 'none.json')
        self.assertIn('running right now', blocked)
        lock = Path(self.tmp.name) / 'setup.lock'
        lock.write_text('')
        import fcntl
        with lock.open() as setup:
            fcntl.flock(setup, fcntl.LOCK_EX)     # a setup holds it
            with mock.patch('neko_common.maintenance_running', return_value=False):
                blocked, _ = uninstaller.busy(locks=(str(lock),), journal=Path(self.tmp.name) / 'none.json')
            self.assertIn('setup is running', blocked)
        with mock.patch('neko_common.maintenance_running', return_value=False):
            blocked, held = uninstaller.busy(locks=(str(lock), str(lock) + '.missing'), journal=Path(self.tmp.name) / 'none.json')
        self.assertIsNone(blocked)
        self.assertEqual(len(held), 1)

    def test_main_refuses_before_any_change(self):
        with mock.patch.object(uninstaller.os, 'geteuid', return_value=0), \
                mock.patch.object(uninstaller, 'busy', return_value=('A monthly update did not finish.', [])), \
                mock.patch.object(uninstaller, 'Plan') as plan, mock.patch('builtins.print'):
            self.assertEqual(uninstaller.main([]), 1)
        plan.assert_not_called()

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
            self.assertFalse(any(c[:2] not in (['systemctl', 'show'],) and c[0] in ('systemctl', 'umount') or c[:2] == ['docker', 'rm']
                                 or c[:3] == ['docker', 'network', 'rm'] for c in system.calls))

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
                    mock.patch.object(uninstaller, 'busy', return_value=(None, [])), \
                    mock.patch('builtins.input', side_effect=['', 'no']), \
                    mock.patch('builtins.print'):
                self.assertEqual(uninstaller.main([]), 2)
            self.assertTrue((system.root / 'etc/systemd/system/nekoserver-dashboard.service').exists())
            self.assertFalse(any(c[0] == 'systemctl' and c[1] != 'show' for c in system.calls))   # only looked


class FollowUpTests(unittest.TestCase):
    """0.39.6: Samba is left as it was before NekoServer, failures are said and listed, and
    a fresh download finds the drives setup recorded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem(self.tmp.name).populate()
        self.shown = []

    def remove(self, chosen, rmtree=uninstaller.shutil.rmtree):
        self.plan = self.system.plan()
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return uninstaller.remove(self.plan, chosen, run=self.system.run, out=self.shown.append, root=self.system.root,
                                      call=self.system.call, sleep=lambda seconds: None, rmtree=rmtree)

    def samba_before(self, record):
        self.system.write(uninstaller.SAMBA_BEFORE, record if isinstance(record, str) else json.dumps(record))

    def notes(self):
        return '\n'.join(self.system.plan().groups['server']['notes'])

    # ---- C2: Samba
    def test_samba_that_nekoserver_switched_on_is_switched_off(self):
        """The reported case: Samba stayed on after the uninstall, on Debian with the package's
        example smb.conf back, which shares on every network."""
        self.assertIn('Samba (smbd.service) is switched off: NekoServer switched it on', self.notes())
        self.remove({'server'})
        self.assertIn(['systemctl', 'disable', '--now', 'smbd.service'], self.system.calls)
        self.assertFalse(any(c[0] == 'smbcontrol' for c in self.system.calls))   # nothing left to reload

    def test_samba_that_was_on_before_stays_on(self):
        self.samba_before({'format': 'neko-samba-before-v1', 'unit': 'smbd.service', 'enabled': True, 'active': True})
        self.assertIn('Samba (smbd.service) stays on: it was on before NekoServer', self.notes())
        self.remove({'server'})
        self.assertNotIn(['systemctl', 'disable', '--now', 'smbd.service'], self.system.calls)
        self.assertFalse(self.exists(uninstaller.SAMBA_BEFORE))   # the record goes with NekoServer

    def test_the_record_decides_over_smb_conf(self):
        self.system.write('/etc/samba/smb.conf', '[global]\n   include = /etc/samba/nekoserver.conf\n   workgroup = HOME\n')
        self.assertIn('stays on: your own smb.conf uses it', self.notes())
        self.samba_before({'format': 'neko-samba-before-v1', 'unit': 'smb.service', 'enabled': False, 'active': False})
        self.system.units_on = {'smb.service'}
        self.assertIn('Samba (smb.service) is switched off: NekoServer switched it on (it was off before)', self.notes())

    def test_a_broken_samba_record_counts_as_none(self):
        for broken in ('{not json', json.dumps(['x']), json.dumps({'format': 'neko-samba-before-v1', 'unit': 'sshd.service', 'enabled': False})):
            self.samba_before(broken)
            self.assertIn('is switched off: NekoServer switched it on (smb.conf is only its own include)', self.notes())

    def test_samba_already_off_needs_nothing(self):
        self.system.units_on = set()
        self.assertNotIn('Samba (', self.notes())
        self.remove({'server'})
        self.assertFalse(any(c[:3] == ['systemctl', 'disable', '--now'] and 'smb' in c[-1] for c in self.system.calls))

    # ---- C3: failures said and listed
    def exists(self, path):
        return (self.system.root / path.lstrip('/')).exists()

    def test_files_that_could_not_be_deleted_are_named(self):
        def stubborn(path, onexc=None, onerror=None):
            handler = onexc or onerror
            for name in ('a', 'b'):
                error = OSError(16, 'Device or resource busy')
                handler(os.unlink, str(path / name), error if onexc else (OSError, error, None))
        left = self.remove({'server'}, rmtree=stubborn)
        self.assertTrue(any(line.startswith('  could not delete ') and 'Device or resource busy' in line for line in self.shown), self.shown)
        self.assertIn(str(self.system.root / 'etc/nekoserver-gateway'), left)

    def test_commands_that_failed_are_listed_at_the_end(self):
        self.system.failing[('systemctl', 'unmask')] = (1, 'Access denied')
        self.system.failing[('smbpasswd',)] = (1, 'Failed to delete entry')
        left = self.remove({'server'})
        text = '\n'.join(left)
        self.assertIn('the masks on sleep.target', text)
        self.assertIn('the Samba account neko', text)

    def test_a_mount_folder_that_cannot_go_says_why(self):
        busy = OSError(16, 'Device or resource busy')
        busy.strerror = 'Device or resource busy'
        with mock.patch.object(uninstaller.Path, 'rmdir', side_effect=busy):
            left = self.remove({key for key, *_ in uninstaller.CHOICES})
        self.assertTrue(any('could not remove the folder /srv/nekoserver (Device or resource busy)' in line for line in self.shown), self.shown)
        self.assertIn('/srv/nekoserver (the mount folder: Device or resource busy)', left)
        self.assertFalse(any('is not empty' in line for line in self.shown))

    def test_main_does_not_say_done_when_something_is_left(self):
        shown = []
        with mock.patch.object(uninstaller, 'Plan', return_value=self.system.plan()), \
                mock.patch.object(uninstaller.os, 'geteuid', return_value=0), \
                mock.patch.object(uninstaller, 'busy', return_value=(None, [])), \
                mock.patch.object(uninstaller, 'remove', return_value=['/etc/nekoserver-gateway']), \
                mock.patch('builtins.input', side_effect=['', 'yes', 'remove NekoServer']), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: shown.append(' '.join(map(str, a)))):
            self.assertEqual(uninstaller.main([]), 0)
        text = '\n'.join(shown)
        self.assertIn('could not be removed or switched off', text)
        self.assertIn('Finished, but not everything went', text)
        self.assertNotIn('Done.', text)

    # ---- C4: settings from a fresh download
    def test_a_fresh_download_uses_the_drives_setup_recorded(self):
        template = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media',
                    'storage': {'data_ssd': {'uuid': ''}, 'media_hdd': {'uuid': ''}}, 'profiles': {}}
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (folder / 'config.json').write_text(json.dumps(template))
        record = {'data_mount': '/data/neko', 'media_mount': '/mnt/hdd', 'storage': SETTINGS['storage']}
        older = self.system.write('/etc/nekoserver-backups/storage.json', json.dumps({**record, 'data_mount': '/srv/old'}))
        os.utime(older, (1, 1))
        self.system.write('/etc/nekoserver-os/drives.json', json.dumps(record))
        settings, source = uninstaller.find_settings(folder, self.system.root)
        self.assertEqual((settings['data_mount'], settings['media_mount']), ('/data/neko', '/mnt/hdd'))   # the newer one
        self.assertEqual(settings['storage']['data_ssd']['uuid'], 'aaaa-1111')
        self.assertIn('profiles', settings)
        self.assertIn('/etc/nekoserver-os/drives.json (the drives setup recorded', source)
        # my-config.json in the folder always comes first.
        (folder / 'my-config.json').write_text(json.dumps(SETTINGS))
        settings, source = uninstaller.find_settings(folder, self.system.root)
        self.assertEqual((settings['data_mount'], source), ('/srv/nekoserver', str(folder / 'my-config.json')))

    def test_a_broken_record_of_the_drives_is_not_used(self):
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (folder / 'config.json').write_text(json.dumps({'data_mount': '/srv/nekoserver'}))
        for broken in ('{"data_mount": 5', '[]', json.dumps({'data_mount': '/x', 'media_mount': '/y', 'storage': {'data_ssd': {'uuid': ''}}})):
            self.system.write('/etc/nekoserver-os/drives.json', broken)
            self.system.write('/etc/nekoserver-backups/storage.json', '{}')
            settings, source = uninstaller.find_settings(folder, self.system.root)
            self.assertEqual(settings, {'data_mount': '/srv/nekoserver'})
            self.assertIn('the template', source)
            self.assertIn('nothing on the drives is removed', source)

    def test_main_says_which_settings_it_used(self):
        shown = []
        with mock.patch.object(uninstaller, 'Plan', return_value=self.system.plan()), \
                mock.patch.object(uninstaller, 'find_settings', return_value=({}, '/etc/nekoserver-os/drives.json (...)')), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: shown.append(' '.join(map(str, a)))):
            self.assertEqual(uninstaller.main(['--dry-run']), 0)
        self.assertIn('Settings: /etc/nekoserver-os/drives.json (...)', shown)


class ExclusionTests(unittest.TestCase):
    """The review of 3130211: the drives are checked again right before anything on them is
    deleted, and deleted only through their checked folders; Samba must really be off
    first; and nothing may be running or unfinished, with the jobs' lock held, while
    anything is deleted."""
    ALL = frozenset(key for key, *_ in uninstaller.CHOICES)
    FILM = '/mnt/nekoserver-media/library/movies/film.mkv'
    MAINTENANCE = '/etc/nekoserver-backups/maintenance-resume.json'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system = FakeSystem(self.tmp.name).populate()
        self.plan = self.system.plan()
        self.shown = []

    def at(self, path):
        return self.system.root / path.lstrip('/')

    def remove(self, chosen=None, call=None):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            return REAL_REMOVE(self.plan, self.ALL if chosen is None else chosen, run=self.system.run, out=self.shown.append,
                                      root=self.system.root, call=call or self.system.call, sleep=lambda seconds: None)

    def assert_nothing_deleted(self):
        for kept in (self.FILM, '/srv/nekoserver/appdata/jellyfin/config.xml', '/etc/samba/nekoserver.conf',
                     '/etc/samba/nekoserver-accounts', '/etc/nekoserver-backups/storage.json'):
            self.assertTrue(self.at(kept).exists(), kept)
        self.assertFalse(any(argv[0] == 'smbpasswd' for argv in self.system.calls))

    # ---- 2: the drive replaced after the plan was made
    def test_a_drive_replaced_after_the_plan_loses_nothing(self):
        self.system.drives['/mnt/nekoserver-media'] = 'ffff-9999'      # someone else's HDD mounted there now
        self.remove()
        self.assertTrue(self.at(self.FILM).exists())
        self.assertIn('Nothing on ' + str(self.at('/mnt/nekoserver-media')) + ' is removed: the drive there now is not the one the '
                      'settings name (UUID ffff-9999, not bbbb-2222).', self.shown)
        self.assertFalse(self.at('/srv/nekoserver/appdata').exists())   # the SSD is still the right one

    def test_the_paths_of_a_replaced_drive_are_never_left_to_delete_by_hand(self):
        """IT-06: the swapped drive's planned folders are listed apart, with a warning not to
        delete them by hand (they would be the other drive's), and the reason is said once."""
        self.system.drives['/mnt/nekoserver-media'] = 'ffff-9999'
        left = self.remove({'server', 'media', 'backups'})
        lines = uninstaller.not_removed(left)
        text = '\n'.join(lines + self.shown)
        self.assertEqual(text.count('not the one the settings name'), 1, text)   # no "not the one ... (not the one ...)"
        self.assertNotIn('do it yourself', text)
        self.assertIn('Do not delete them by hand', lines[0])
        self.assertIn('   - ' + str(self.at(self.FILM).parents[1]), lines)
        # A path that really failed is still listed to do by hand, apart from the skipped ones.
        mixed = uninstaller.not_removed(['/etc/x', uninstaller.Skipped('/mnt/nekoserver-media/library')])
        self.assertEqual(mixed[:2], [mixed[0], '   - /etc/x'])
        self.assertIn('do it yourself', mixed[0])
        self.assertIn('Do not delete them by hand', mixed[2])

    def test_a_drive_replaced_during_the_deletion_is_never_touched(self):
        real = uninstaller.open_drives
        media = self.at('/mnt/nekoserver-media')

        def then_replaced(plan, out):
            drives = real(plan, out)
            media.rename(media.with_name('nekoserver-media.original'))   # the checked drive moves away...
            (media / 'library').mkdir(parents=True)                       # ...and another one takes its folder
            (media / 'library/foreign video.mkv').write_text('not NekoServer\'s')
            return drives
        with mock.patch.object(uninstaller, 'open_drives', side_effect=then_replaced):
            self.remove({'server', 'media'})
        self.assertTrue((media / 'library/foreign video.mkv').exists())
        self.assertFalse(media.with_name('nekoserver-media.original').joinpath('library').exists())   # the checked one's went

    def test_the_deletion_never_follows_a_symlink_nor_enters_another_filesystem(self):
        outside = self.at('/home/neko/keep.txt')
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text('mine')
        tree = self.at('/mnt/nekoserver-media/library')
        (tree / 'link').symlink_to(outside.parent)
        (tree / 'mounted').mkdir()
        (tree / 'mounted/other.mkv').write_text('another drive')
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            found = real_stat(path, *args, **kwargs)
            if path == 'mounted':   # as if a drive were mounted on this folder
                return os.stat_result((found.st_mode, found.st_ino, found.st_dev + 1, *tuple(found)[3:]))
            return found
        failed, held = [], []
        fd = os.open(tree.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(uninstaller.os, 'stat', side_effect=stat):
                uninstaller._delete_at(fd, 'library', real_stat(tree).st_dev, (failed, str(tree), set(), held))
        finally:
            os.close(fd)
        self.assertTrue(outside.exists())
        self.assertTrue((tree / 'mounted/other.mkv').exists())
        self.assertFalse((tree / 'movies').exists())
        self.assertTrue(any(uninstaller.OTHER_DRIVE in line for line in failed), failed)

    # ---- QA-05: a Btrfs subvolume inside a folder that goes is part of the same drive
    def delete_with_subvolume(self, same_mount=True, read_only=False):
        """docker/btrfs/subvolumes/abc123 made a Btrfs subvolume (its own device number, inode
        256, on the folder's mount unless same_mount is False), then docker deleted."""
        tree = self.at('/srv/nekoserver/docker')
        (tree / 'btrfs/subvolumes/abc123/etc').mkdir(parents=True)
        (tree / 'btrfs/subvolumes/abc123/etc/layer.txt').write_text('a layer')
        (tree / 'appdata.txt').write_text('x')
        fake = FakeSubvolume('abc123', same_mount, read_only)
        failed, held = [], []
        fd = os.open(tree.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with fake.patched():
                uninstaller._delete_at(fd, 'docker', os.stat(tree).st_dev, (failed, str(tree), set(), held))
        finally:
            os.close(fd)
        return tree, failed

    @unittest.skipUnless(Path('/proc/self/fdinfo').is_dir(), 'needs Linux /proc')
    def test_the_mount_of_an_open_folder_is_read_from_proc(self):
        first, second = (os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in (self.at('/srv'), self.at('/etc')))
        try:
            self.assertIsNotNone(uninstaller._mount_id(first))
            self.assertEqual(uninstaller._mount_id(first), uninstaller._mount_id(second))   # one temporary folder, one mount
        finally:
            os.close(first)
            os.close(second)
        self.assertIsNone(uninstaller._mount_id(10 ** 6))   # not an open file

    def test_a_nested_btrfs_subvolume_goes_with_its_folder(self):
        tree, failed = self.delete_with_subvolume()
        self.assertEqual(failed, [])
        self.assertFalse(tree.exists())

    def test_a_drive_mounted_on_a_subvolume_shaped_folder_is_still_another_drive(self):
        tree, failed = self.delete_with_subvolume(same_mount=False)
        self.assertTrue((tree / 'btrfs/subvolumes/abc123/etc/layer.txt').exists())
        self.assertEqual(failed, [str(tree / 'btrfs/subvolumes/abc123') + ': ' + uninstaller.OTHER_DRIVE])
        self.assertFalse((tree / 'appdata.txt').exists())

    def test_a_subvolume_that_cannot_be_emptied_is_named_once_with_the_command(self):
        tree, failed = self.delete_with_subvolume(read_only=True)
        where = str(tree / 'btrfs/subvolumes/abc123')
        self.assertEqual(len(failed), 1, failed)
        self.assertTrue(failed[0].startswith(where + ': it is a Btrfs subvolume of this drive'), failed)
        self.assertIn('sudo btrfs subvolume delete ' + where + '  (' + where + '/etc/layer.txt: Read-only file system)', failed[0])
        self.assertNotIn(uninstaller.OTHER_DRIVE, failed[0])

    # ---- 3: Samba must be off before its accounts, its shares or any file go
    def test_samba_that_does_not_switch_off_stops_everything(self):
        for unit, setup in (('smbd.service', None), ('smb.service', 'arch')):
            for case, answer in (('failed', (1, 'Failed to stop')), ('timeout', (None, 'did not finish within 120 seconds')),
                                 ('unknown', None)):
                with self.subTest(unit=unit, case=case):
                    self.setUp()
                    if setup:
                        self.system.write(uninstaller.SAMBA_BEFORE, json.dumps(
                            {'format': 'neko-samba-before-v1', 'unit': unit, 'enabled': False, 'active': False}))
                        self.system.units_on = {unit}
                        self.plan = self.system.plan()
                    self.assertEqual(self.plan.samba_off, unit)
                    self.system.failing[('systemctl', 'is-enabled', unit)] = (0, 'enabled')
                    if answer is None:
                        self.system.failing[('systemctl', 'is-active', unit)] = (4, '')   # systemd cannot say
                    else:
                        self.system.failing[('systemctl', 'disable', '--now', unit)] = answer
                    with self.assertRaisesRegex(uninstaller.Stopped, r'Samba \(' + unit + r'\) did not switch off'):
                        self.remove()
                    self.assert_nothing_deleted()
                    self.assertIn(['systemctl', 'enable', unit], self.system.calls)   # back on, as it was

    # ---- 4: nothing running or unfinished, the jobs' lock held, the locks kept
    def test_unfinished_maintenance_found_after_the_confirmation_stops_it(self):
        def confirmed(*_args):
            self.system.write(self.MAINTENANCE, json.dumps({'version': '0.40.1', 'phase': 'restoring'}))   # began meanwhile
            return True
        shown = []
        with mock.patch.object(uninstaller.os, 'geteuid', return_value=0), \
                mock.patch.object(uninstaller, 'busy', return_value=(None, [])), \
                mock.patch.object(uninstaller, 'find_settings', return_value=(SETTINGS, 'my-config.json')), \
                mock.patch.object(uninstaller, 'Plan', return_value=self.plan), \
                mock.patch.object(uninstaller, 'choose', return_value=self.ALL), \
                mock.patch.object(uninstaller, 'confirm', side_effect=confirmed), \
                mock.patch.object(uninstaller, 'remove', side_effect=lambda plan, chosen, out: self.remove(chosen)), \
                mock.patch('builtins.print', side_effect=lambda *a, **k: shown.append(' '.join(map(str, a)))):
            self.assertEqual(uninstaller.main([]), 1)
        self.assertIn('did not finish', '\n'.join(shown))
        self.assert_nothing_deleted()
        self.assertTrue(self.at(self.MAINTENANCE).exists())
        self.assertFalse(any(argv[:2] == ['systemctl', 'disable'] for argv in self.system.calls))   # nothing even stopped

    def test_a_monthly_update_that_starts_while_the_timers_go_is_never_stopped(self):
        timers_off = []

        def call(argv, timeout=0):
            if argv[:3] == ['systemctl', 'disable', '--now'] and argv[-1].endswith('.timer'):
                timers_off.append(argv[-1])
            if argv[:2] == ['systemctl', 'is-active'] and 'nekoserver-maintenance.service' in argv and timers_off:
                self.system.calls.append(argv)
                return 0, 'activating\ninactive'
            return self.system.call(argv, timeout)
        with self.assertRaisesRegex(uninstaller.Stopped, 'monthly update is running'):
            self.remove(call=call)
        self.assertNotIn(['systemctl', 'disable', '--now', 'nekoserver-maintenance.service'], self.system.calls)
        self.assert_nothing_deleted()

    def test_a_cut_off_backup_left_unfinished_stops_it(self):
        self.system.write('/etc/nekoserver-backups/app-resume.json', '{}')
        with self.assertRaisesRegex(uninstaller.Stopped, 'app-resume.json is still there.*nekoserver-app-resume.service'):
            self.remove()
        self.assert_nothing_deleted()

    def test_a_job_cut_off_by_the_stop_itself_is_found_with_the_lock_held(self):
        # A backup stopped part-way whose restart of the apps did not finish keeps its record.
        def call(argv, timeout=0):
            if argv[:3] == ['systemctl', 'disable', '--now'] and argv[-1] == 'nekoserver-dashboard.service':
                self.system.write('/etc/nekoserver-backups/host-resume.json', '{}')
            return self.system.call(argv, timeout)
        with self.assertRaisesRegex(uninstaller.Stopped, 'host-resume.json is still there'):
            self.remove(call=call)
        self.assert_nothing_deleted()

    def test_the_jobs_lock_is_held_while_files_go_and_a_job_holding_it_stops_it(self):
        lock = self.at('/srv/nekoserver/installer/recovery.lock')
        real = uninstaller._remove_files
        seen = []

        def removing(*args, **kwargs):
            with lock.open() as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append('free')
                except BlockingIOError:
                    seen.append('held')
            return real(*args, **kwargs)
        with mock.patch.object(uninstaller, '_remove_files', side_effect=removing):
            left = self.remove()
        self.assertEqual(seen, ['held'])
        self.assertFalse(self.at('/srv/nekoserver/installer').exists())   # the lock went last, with its folder
        self.assertNotIn(str(self.at('/srv/nekoserver/installer')), left)
        # A job that still holds it: nothing goes.
        self.setUp()
        lock = self.at('/srv/nekoserver/installer/recovery.lock')
        lock.write_text('')
        with lock.open() as job:
            fcntl.flock(job, fcntl.LOCK_EX)
            with self.assertRaisesRegex(uninstaller.Stopped, 'job is still working'):
                self.remove()
        self.assert_nothing_deleted()

    def test_lock_files_in_run_stay_so_a_setup_started_meanwhile_still_waits(self):
        for path in ('/run/nekoserver-setup.lock', '/run/nekoserver-install.lock', '/run/nekoserver-packages.lock',
                     '/run/nekoserver-heartbeat/worker.lock', '/run/nekoserver-heartbeat/status.json'):
            self.system.write(path, '')
        setup = self.at('/run/nekoserver-setup.lock')
        held = uninstaller.hold_setup_locks((str(setup),))
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        self.plan = self.system.plan()
        self.remove({'server'})
        for kept in ('/run/nekoserver-setup.lock', '/run/nekoserver-install.lock', '/run/nekoserver-packages.lock',
                     '/run/nekoserver-heartbeat/worker.lock'):
            self.assertTrue(self.at(kept).exists(), kept)
        self.assertFalse(self.at('/run/nekoserver-heartbeat/status.json').exists())
        self.assertFalse(self.at('/run/nekoserver-control').exists())
        with setup.open() as installer, self.assertRaises(BlockingIOError):   # install.sh started now: it must wait
            fcntl.flock(installer, fcntl.LOCK_EX | fcntl.LOCK_NB)



class NoContainersHintTests(unittest.TestCase):
    """IT-08: "run with sudo" only when it could help."""
    def test_the_hint_fits_why_no_container_was_listed(self):
        why = uninstaller.no_containers_why
        self.assertEqual(why(which=lambda _: None, euid=lambda: 1000), 'Docker is not installed')
        self.assertIn('run with sudo', why(which=lambda _: '/usr/bin/docker', euid=lambda: 1000))
        self.assertNotIn('sudo', why(which=lambda _: '/usr/bin/docker', euid=lambda: 0))


if __name__ == '__main__':
    unittest.main()
