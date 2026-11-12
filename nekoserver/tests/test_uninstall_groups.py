"""Gate run C18 (VM3): with "all" chosen, the uninstaller never named a group's own drive
(/mnt/nekoserver-drives/d3) and left that group's settings and library on it. Now every
drive and folder NekoServer uses is listed with what is on it; a group's drive is cleaned
like the SSD and the HDD, and the person's own folders and shares are kept."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uninstaller  # noqa: E402
from test_uninstaller import SETTINGS, FakeSystem  # noqa: E402

D3 = '/mnt/nekoserver-drives/d3'
# Bound into the library from the same drive, a folder of the person's is that very folder
# (same device, same inode) inside the library: here it simply is the folder there.
OWN_ON_D3 = D3 + '/library/existing/music'
SHARED_FILMS = '/mnt/tank/films'
GROUPS = {
    **SETTINGS,
    'storage': {**SETTINGS['storage'], 'd3': {'uuid': 'eeee-5555', 'fstype': 'ext4'}},
    'locations': {'music': 'd3'},
    'install': {'music': 'd3'},
    'placement': {
        'music': {
            'extra': [{'path': OWN_ON_D3, 'holds': 'music', 'use': 'in_place', 'share': '/dev/sdc1', 'mount': D3}]
        },
        'movies': {
            'extra': [
                {'path': SHARED_FILMS, 'holds': 'movies', 'use': 'in_place', 'share': 'nas:/tank', 'mount': '/mnt/tank'}
            ]
        },
    },
}
ALL = {key for key, *_ in uninstaller.CHOICES}


class GroupDriveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.system = FakeSystem(tmp.name).populate()
        self.system.drives[D3] = 'eeee-5555'
        w = self.system.write
        w(D3 + '/nekoserver/appdata/navidrome/navidrome.db')
        w(D3 + '/nekoserver/appdata/lidarr/config.xml')
        w(D3 + '/library/music/album/song.flac')
        w(D3 + '/library/downloads/music/new.flac')
        w(OWN_ON_D3 + '/mine/old-song.flac')
        w(D3 + '/my-own-notes.txt')  # the person's, beside NekoServer's folders
        w(SHARED_FILMS + '/film.mkv')
        fstab = self.system.root / 'etc/fstab'
        fstab.write_text(
            fstab.read_text()
            + 'UUID=eeee-5555 '
            + D3
            + ' ext4 defaults,nofail 0 2\n'
            + 'nas:/tank /mnt/tank nfs4 defaults,nofail 0 0\n'
        )
        self.plan = self.system.plan(settings=GROUPS)
        self.shown = []

    def there(self, path):
        return (self.system.root / path.lstrip('/')).exists()

    def test_every_drive_and_folder_is_listed_with_what_is_on_it(self):
        said = self.plan.describe(ALL)
        self.assertIn(
            D3 + ' (a drive of its own: the settings and databases of the apps installed there, Music\'s files)', said
        )
        self.assertIn(OWN_ON_D3 + ' (your music): your own folder: kept, never removed', said)
        self.assertIn(SHARED_FILMS + ' (your movies, on the shared folder nas:/tank): your own folder: kept', said)
        self.assertIn('nas:/tank at /mnt/tank (your own shared folder: kept, with its /etc/fstab line)', said)
        # What goes from it is listed under its choice, as for the SSD and the HDD.
        root = str(self.system.root)
        self.assertIn('remove ' + root + D3 + '/nekoserver', said)
        self.assertIn('remove ' + root + D3 + '/library', said)
        self.assertIn('Unmount ' + D3 + ' (a drive of its own)', said)
        # Not NekoServer's: listed as staying.
        self.assertIn(root + D3 + '/my-own-notes.txt', said.split('== The apps')[0])

    def test_all_cleans_the_group_drive_and_keeps_the_person_s_folders(self):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            uninstaller.remove(
                self.plan,
                ALL - {'mounts'},
                run=self.system.run,
                out=self.shown.append,
                root=self.system.root,
                call=self.system.call,
                sleep=lambda seconds: None,
            )
        for gone in (D3 + '/nekoserver', D3 + '/library/music', D3 + '/library/downloads'):
            self.assertFalse(self.there(gone), gone)
        self.assertTrue(self.there(OWN_ON_D3 + '/mine/old-song.flac'))
        self.assertIn('  kept ' + str(self.system.root) + OWN_ON_D3 + ': your own folder, never removed', self.shown)
        self.assertTrue(self.there(D3 + '/my-own-notes.txt'))
        self.assertTrue(self.there(SHARED_FILMS + '/film.mkv'))

    def test_the_mounts_go_but_the_share_and_its_line_stay(self):
        with mock.patch.object(uninstaller.shutil, 'which', return_value='/usr/bin/x'):
            uninstaller.remove(
                self.plan,
                {'server', 'mounts'},
                run=self.system.run,
                out=self.shown.append,
                root=self.system.root,
                call=self.system.call,
                sleep=lambda seconds: None,
            )
        fstab = (self.system.root / 'etc/fstab').read_text()
        self.assertNotIn('UUID=eeee-5555', fstab)
        self.assertIn('nas:/tank /mnt/tank nfs4 defaults,nofail 0 0', fstab)
        self.assertTrue(self.there(SHARED_FILMS + '/film.mkv'))

    def test_a_drive_that_is_not_the_one_named_loses_nothing(self):
        self.system.drives[D3] = 'ffff-6666'  # another drive is mounted there now
        plan = self.system.plan(settings=GROUPS)
        self.assertFalse(plan.identified[uninstaller.EXTRA + 'd3'])
        self.assertNotIn(str(self.system.root) + D3 + '/nekoserver', [str(p) for p in plan.groups['data']['paths']])
        self.assertIn('The drive at ' + D3 + ' is ', ' '.join(plan.warnings))

    def test_only_drives_a_group_uses_are_group_drives(self):
        settings = {
            'storage': {
                'd3': {'uuid': 'eeee-5555'},
                'd4': {'uuid': 'f-1'},  # named by nothing
                'nas': {'uuid': 'n-1', 'network': {'export': 'nas:/media'}},
                'one': {'uuid': 'o-1'},
                'sys': {'uuid': 's-1', 'folder': '/srv/x'},
            },
            'locations': {'music': 'd3', 'movies': 'nas', 'photos': 'one', 'cloud': ['not', 'a', 'name']},
            'install': {'photos': 'sys', 'music': 'd3'},
        }
        self.assertEqual(uninstaller.group_drives(settings), {'d3': ({'uuid': 'eeee-5555'}, ['install', 'music'])})
        self.assertEqual(uninstaller.group_drives({}), {})


if __name__ == '__main__':
    unittest.main()
