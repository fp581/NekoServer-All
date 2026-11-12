"""Media a person already has, used where it is (existing_media.py; wizard page P03,
checks T12 to T14). Everything here runs on folders of its own with stand-ins for
findmnt, the apps' account, mount and the apps' APIs: no Docker, no network."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import existing_media  # noqa: E402
import media_libraries  # noqa: E402
import placement  # noqa: E402
import source_import  # noqa: E402
from existing_media import Away, Refused  # noqa: E402


class Server:
    """A stand-in PC: an app SSD, a media drive, and a NAS share mounted at <top>/nas
    that holds films. `mounts` is what findmnt would say: {mount folder: (what, type)}."""

    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.top = Path(os.path.realpath(tmp.name))
        self.ssd, self.media, self.nas = self.top / 'ssd', self.top / 'media', self.top / 'nas'
        self.films = self.nas / 'films'
        (self.films / 'Alien (1979)').mkdir(parents=True)
        (self.films / 'Alien (1979)' / 'Alien.mkv').write_bytes(b'film' * 100)
        (self.films / 'Brazil (1985)').mkdir()
        (self.films / 'Brazil (1985)' / 'Brazil.mkv').write_bytes(b'film' * 50)
        for folder in (self.ssd, self.media / 'library' / 'movies'):
            folder.mkdir(parents=True)
        self.fstab = self.top / 'fstab'
        self.fstab.write_text(f'UUID=1 / ext4 defaults 0 1\nnas:/tank {self.nas} nfs4 hard,nofail 0 0\n')
        self.mounts = {
            '/': ('/dev/sda1', 'ext4'),
            str(self.nas): ('nas:/tank', 'nfs4'),
            str(self.media): ('/dev/sdb1', 'ext4'),
            str(self.ssd): ('/dev/sdc1', 'ext4'),
        }
        self.bound = {}  # mount points bound by "mount", as the kernel's table would have them
        self.config = {
            'data_mount': str(self.ssd),
            'media_mount': str(self.media),
            'media': {'uid': 1000, 'gid': 1000},
            'storage': {
                'data_ssd': {'uuid': 'ssd-uuid', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'hdd-uuid', 'fstype': 'ext4'},
            },
        }

    def holding(self, path):
        return max((m for m in self.mounts if existing_media.within(str(path), m)), key=len)

    def find(self, column, path, by='--mountpoint'):
        mount = self.holding(path)
        return {'TARGET': mount, 'SOURCE': self.mounts[mount][0], 'FSTYPE': self.mounts[mount][1]}[column]

    def kind(self, path):
        return self.find('FSTYPE', path)

    def how(self):
        return {'find': self.find, 'kind': self.kind, 'mounted': lambda point: self.bound.get(str(point))}

    def check(self, folder=None, new_files=False, said=None, flags=0, category='movies'):
        """check() with the apps' account answering `said` ({'read': .., 'write': ..})."""
        asked = []

        def as_user(uid, gid, path, write=False):
            asked.append((uid, gid, path, write))
            return (said or {}).get('write' if write else 'read', '')

        found = existing_media.check(
            self.config,
            category,
            str(folder or self.films),
            new_files,
            as_user=as_user,
            find=self.find,
            kind=self.kind,
            fstab=self.fstab,
            statvfs=lambda path: SimpleNamespace(f_flag=flags),
        )
        return found, asked

    def chosen(self, new_files=False, category='movies'):
        """The settings after the films folder was chosen to be used where it is."""
        found, _ = self.check(new_files=new_files, category=category)
        return existing_media.with_choice(self.config, existing_media.entry_for(category, found, new_files))


def tree(folder):
    """Every name under a folder with its size, date, owner and permissions."""
    seen = {}
    for path in [Path(folder), *sorted(Path(folder).rglob('*'))]:
        info = path.lstat()
        seen[str(path)] = (info.st_size if path.is_file() else None, info.st_mtime_ns, info.st_uid, info.st_mode)
    return seen


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.pc = Server(self)

    def refusal(self, **how):
        with self.assertRaises(Refused) as caught:
            self.pc.check(**how)
        return caught.exception

    def test_a_folder_on_a_mounted_share_is_read_as_the_apps_account(self):
        found, asked = self.pc.check()
        self.assertEqual(found['path'], str(self.pc.films))
        self.assertEqual((found['mount'], found['share'], found['fstype']), (str(self.pc.nas), 'nas:/tank', 'nfs4'))
        self.assertTrue(found['persistent'])
        self.assertIsNone(found['add_to_fstab'])
        self.assertIsNone(found['new_files'], 'not asked, so not tried')
        # Read as the account the apps' containers run as (media.uid), and nothing written.
        self.assertEqual(asked, [(1000, 1000, str(self.pc.films), False)])

    def test_a_share_that_is_not_mounted_is_an_empty_folder_not_the_films(self):
        del self.pc.mounts[str(self.pc.nas)]  # the NAS is off: its folder is on the system disk
        refused = self.refusal()
        self.assertEqual(refused.code, 'not_mounted')
        self.assertIn(f'Nothing is mounted at {self.pc.nas} now', str(refused))
        self.assertIn('not your movies', str(refused))
        # A share mounted on first use that did not connect.
        self.pc.mounts[str(self.pc.nas)] = ('systemd-1', 'autofs')
        self.assertEqual(self.refusal().code, 'not_mounted')

    def test_a_bare_folder_where_drives_are_mounted_is_refused(self):
        self.pc.fstab.write_text('UUID=1 / ext4 defaults 0 1\n')
        del self.pc.mounts[str(self.pc.nas)]
        with mock.patch.object(existing_media, 'MOUNT_PARENTS', (str(self.pc.top),)):
            self.assertEqual(self.refusal().code, 'bare_mount_folder')

    def test_a_mount_that_would_be_gone_after_a_restart(self):
        self.pc.fstab.write_text('UUID=1 / ext4 defaults 0 1\n')  # mounted by hand
        # An NFS share: setup can make it permanent itself, with the line it would write.
        found, _ = self.pc.check()
        self.assertFalse(found['persistent'])
        self.assertEqual(found['add_to_fstab'].split()[:3], ['nas:/tank', str(self.pc.nas), 'nfs'])
        self.assertIn('nofail', found['add_to_fstab'])
        # A USB drive the desktop mounted: said, with what to do.
        self.pc.mounts[str(self.pc.nas)] = ('/dev/sdd1', 'ext4')
        refused = self.refusal()
        self.assertEqual(refused.code, 'not_permanent')
        self.assertIn('only until the next restart', str(refused))

    def test_a_folder_the_apps_account_cannot_read_says_who_and_where_to_fix_it(self):
        refused = self.refusal(said={'read': 'read Permission denied'})
        self.assertEqual(refused.code, 'unreadable')
        self.assertIn("The apps' account (1000:1000) cannot read", str(refused))
        self.assertIn('Permission denied', str(refused))
        self.assertIn('never changes the permissions of your folder', str(refused))
        self.assertIn('On the NAS', str(refused))

    def test_new_files_need_a_write_by_the_apps_account(self):
        found, asked = self.pc.check(new_files=True)
        self.assertEqual(found['new_files'], {'ok': True})
        self.assertEqual([write for *_, write in asked], [False, True])

    def test_a_read_only_folder_is_still_used_but_takes_no_new_files(self):
        found, asked = self.pc.check(new_files=True, flags=os.ST_RDONLY)
        self.assertTrue(found['read_only'])
        self.assertEqual(found['new_files']['code'], 'read_only')
        self.assertIn('is read-only here (nas:/tank', found['new_files']['reason'])
        self.assertIn('Choose another folder for new movies', found['new_files']['reason'])
        self.assertEqual([write for *_, write in asked], [False], 'no write is tried on a read-only mount')
        # Exported read-only by the NAS though mounted for writing: the write says so.
        found, _ = self.pc.check(new_files=True, said={'write': 'write Read-only file system'})
        self.assertEqual(found['new_files']['code'], 'read_only')
        with self.assertRaises(RuntimeError):
            existing_media.entry_for('movies', found, new_files=True)
        self.assertFalse(existing_media.entry_for('movies', found)['new_files'])

    def test_no_write_access_and_a_squashed_owner_are_told_apart(self):
        found, _ = self.pc.check(new_files=True, said={'write': 'write Permission denied'}, category='tv')
        self.assertEqual(found['new_files']['code'], 'no_write')
        self.assertIn('may read', found['new_files']['reason'])
        self.assertIn('choose another folder for new shows', found['new_files']['reason'])
        found, _ = self.pc.check(new_files=True, said={'write': 'owner 65534:65534'})
        self.assertEqual(found['new_files']['code'], 'owner')
        self.assertIn('belongs to 65534:65534', found['new_files']['reason'])

    def test_the_systems_and_nekoservers_own_folders_are_never_media(self):
        for folder, code in (
            ('films', 'not_a_folder'),
            ('/etc', 'unsafe'),
            ('/', 'unsafe'),
            (self.pc.media / 'library' / 'movies', 'own'),
            (self.pc.top, 'own'),  # holds NekoServer's drives
            (self.pc.nas / 'nothing-here', 'not_a_folder'),
            (str(self.pc.films) + '\n/etc', 'not_a_folder'),
        ):
            self.assertEqual(self.refusal(folder=folder).code, code, folder)

    def test_a_link_is_checked_as_the_folder_it_names(self):
        link = self.pc.top / 'my-films'
        link.symlink_to(self.pc.films)
        self.assertEqual(self.pc.check(folder=link)[0]['path'], str(self.pc.films))
        (self.pc.top / 'to-etc').symlink_to('/etc')
        self.assertEqual(self.refusal(folder=self.pc.top / 'to-etc').code, 'unsafe')

    @unittest.skipUnless(os.geteuid() == 0, 'switching to the apps\' account needs root')
    def test_the_apps_account_really_tries_and_leaves_the_folder_as_it_was(self):
        for folder in (self.pc.top, self.pc.nas):
            os.chmod(folder, 0o755)
        os.chmod(self.pc.films, 0o700)  # root's own: the apps' account may not enter
        self.assertTrue(existing_media.as_app(1000, 1000, self.pc.films).startswith('read '))
        os.chmod(self.pc.films, 0o755)
        for inside in self.pc.films.rglob('*'):
            os.chmod(inside, 0o755 if inside.is_dir() else 0o644)
        before = tree(self.pc.films)
        self.assertEqual(existing_media.as_app(1000, 1000, self.pc.films), '')
        # Readable, not writable: said as a write that was refused; nothing left behind.
        self.assertTrue(existing_media.as_app(1000, 1000, self.pc.films, write=True).startswith('write '))
        self.assertEqual(tree(self.pc.films), before)
        # Writable by the apps' account: its own test file is written and removed again.
        os.chown(self.pc.films, 1000, 1000)
        owned = tree(self.pc.films)
        self.assertEqual(existing_media.as_app(1000, 1000, self.pc.films, write=True), '')
        after = tree(self.pc.films)
        self.assertEqual(sorted(after), sorted(owned), 'no test file left')
        self.assertEqual(
            {path: seen[2:] for path, seen in after.items()}, {path: seen[2:] for path, seen in owned.items()}
        )


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.pc = Server(self)

    def test_the_choice_is_saved_in_the_groups_row_of_the_placement_table(self):
        changed = self.pc.chosen()
        self.assertEqual(
            changed['placement']['movies']['extra'],
            [
                {
                    'path': str(self.pc.films),
                    'holds': 'movies',
                    'use': 'in_place',
                    'new_files': False,
                    'mount': str(self.pc.nas),
                    'share': 'nas:/tank',
                    'fstype': 'nfs4',
                }
            ],
        )
        self.assertNotIn('placement', self.pc.config, 'the settings given are not changed')
        # The table is still one placement.py accepts, with every row it had.
        self.assertEqual(placement.table(changed)['movies']['data'], placement.table(self.pc.config)['movies']['data'])
        self.assertEqual(existing_media.extras(changed)[0]['path'], str(self.pc.films))
        self.assertEqual(existing_media.extras(self.pc.config), [])

    def test_one_folder_for_each_kind_and_a_new_choice_takes_the_olds_place(self):
        (self.pc.nas / 'shows').mkdir()
        self.pc.config = self.pc.chosen()
        found, _ = self.pc.check(folder=self.pc.nas / 'shows', new_files=True, category='tv')
        both = existing_media.with_choice(self.pc.config, existing_media.entry_for('tv', found, True))
        self.assertEqual([e['holds'] for e in existing_media.extras(both)], ['movies', 'tv'])
        (self.pc.nas / 'more-films').mkdir()
        found, _ = self.pc.check(folder=self.pc.nas / 'more-films')
        again = existing_media.with_choice(both, existing_media.entry_for('movies', found))
        self.assertEqual(
            sorted((e['holds'], Path(e['path']).name) for e in existing_media.extras(again)),
            [('movies', 'more-films'), ('tv', 'shows')],
        )
        fewer = existing_media.without_choice(again, 'tv')
        self.assertEqual([e['holds'] for e in existing_media.extras(fewer)], ['movies'])
        # Where new files go, as the apps see it: the existing folder only when chosen.
        self.assertEqual(existing_media.new_files_folder(again, 'tv'), '/data/existing/tv')
        self.assertEqual(existing_media.new_files_folder(again, 'movies'), '/data/movies')
        self.assertEqual(existing_media.new_files_folder(self.pc.config, 'music'), '/data/music')
        left_out = existing_media.not_backed_up(self.pc.config)
        self.assertEqual([row[:2] for row in left_out], [('movies', str(self.pc.films))])

    def test_what_the_settings_file_says_is_not_trusted(self):
        good = self.pc.chosen()
        link = self.pc.top / 'link'
        link.symlink_to('/etc')

        def with_entry(**changes):
            bad = existing_media.without_choice(good, 'movies')
            bad['placement']['movies']['extra'] = [{**good['placement']['movies']['extra'][0], **changes}]
            return bad

        for changes in (
            {'path': '/etc/cron.d'},
            {'path': str(link)},  # not the real folder it names
            {'path': str(self.pc.films) + '/../../ssd'},
            {'path': str(self.pc.ssd / 'secrets')},
            {'path': 'films'},
            {'holds': 'music'},  # not this row's
            {'holds': 'photos'},
            {'use': 'copy'},
            {'new_files': 'yes'},
            {'mount': None},
        ):
            with self.assertRaises(RuntimeError, msg=changes):
                existing_media.extras(with_entry(**changes))
            with self.assertRaises(RuntimeError, msg=changes):
                existing_media.fstab_lines(with_entry(**changes))
        twice = existing_media.without_choice(good, 'movies')
        twice['placement']['movies']['extra'] = good['placement']['movies']['extra'] * 2
        with self.assertRaises(RuntimeError):
            existing_media.extras(twice)

    def test_the_mount_point_is_under_the_groups_own_drive(self):
        self.assertEqual(
            existing_media.mount_point(self.pc.config, 'tv'), self.pc.media / 'library' / 'existing' / 'tv'
        )
        self.assertEqual(
            existing_media.mount_point(self.pc.config, 'music'), self.pc.ssd / 'library' / 'existing' / 'music'
        )
        self.assertEqual(existing_media.container_path('movies'), '/data/existing/movies')
        # A link planted where the mount point goes cannot lead it elsewhere.
        (self.pc.media / 'library' / 'existing').mkdir()
        (self.pc.media / 'library' / 'existing' / 'movies').symlink_to('/etc')
        with self.assertRaises(RuntimeError):
            existing_media.mount_point(self.pc.config, 'movies')


class MountTests(unittest.TestCase):
    def setUp(self):
        self.pc = Server(self)
        self.ran = []

    def run_tool(self, argv, timeout):
        self.ran.append(argv)
        if argv[0] == 'mount' and '-o' not in argv:
            self.pc.bound[argv[-1]] = '77'

    def use(self, config, permanent=()):
        how = self.pc.how()
        return existing_media.use_in_place(
            config, permanent, run=self.run_tool, fstab=self.pc.fstab, log=lambda line: None, **how
        )

    def test_the_line_binds_read_only_and_waits_for_the_share(self):
        point = self.pc.media / 'library' / 'existing' / 'movies'
        (line,) = existing_media.fstab_lines(self.pc.chosen())
        fields = line.split()
        self.assertEqual(fields[:3], [str(self.pc.films), str(point), 'none'])
        options = fields[3].split(',')
        self.assertEqual(options[:3], ['bind', 'ro', 'nofail'])
        self.assertIn(f'x-systemd.requires-mounts-for={self.pc.films}', options)
        self.assertIn(f'x-systemd.requires-mounts-for={self.pc.media}', options)
        # New files go there: writable, and only then.
        (line,) = existing_media.fstab_lines(self.pc.chosen(new_files=True))
        self.assertEqual(line.split()[3].split(',')[:3], ['bind', 'rw', 'nofail'])

    def test_a_folder_with_a_space_in_its_name(self):
        spaced = self.pc.nas / 'TV Shows'
        spaced.mkdir()
        found, _ = self.pc.check(folder=spaced, category='tv')
        config = existing_media.with_choice(self.pc.config, existing_media.entry_for('tv', found))
        (line,) = existing_media.fstab_lines(config)
        self.assertEqual(len(line.split()), 6)
        self.assertEqual(existing_media.unescaped(line.split()[0]), str(spaced))
        self.assertEqual(existing_media.fstab_mounts(line)[str(self.pc.media / 'library/existing/tv')], str(spaced))

    def test_setup_mounts_it_now_and_at_every_start_and_touches_nothing_of_the_persons(self):
        config = self.pc.chosen()
        before = tree(self.pc.films)
        earlier = self.pc.fstab.read_text()
        rows = self.use(config)
        point = self.pc.media / 'library' / 'existing' / 'movies'
        self.assertEqual(rows, [{'holds': 'movies', 'path': str(self.pc.films), 'mounted': True, 'reason': None}])
        self.assertEqual(self.ran, [['systemctl', 'daemon-reload'], ['mount', str(point)]])
        written = self.pc.fstab.read_text()
        self.assertTrue(written.startswith(earlier), 'the lines that were there stay, in order')
        self.assertEqual(
            written[len(earlier) :].splitlines(),
            [existing_media.BEGIN, *existing_media.fstab_lines(config), existing_media.END],
        )
        # The mount point is root's and closed: nothing can be written into the bare folder.
        self.assertEqual(point.stat().st_mode & 0o777, 0o555)
        self.assertEqual(list(point.iterdir()), [])
        # Nothing of the person's was copied, moved, renamed, chowned or chmodded.
        self.assertEqual(tree(self.pc.films), before)
        self.assertEqual(sorted(p.name for p in (self.pc.media / 'library' / 'movies').iterdir()), [])
        # Run again: nothing to write; an option that changed is applied to the mount.
        self.ran.clear()
        self.use(config)
        self.assertEqual(self.ran, [['mount', '-o', 'remount,bind,ro', str(point)]])
        self.assertEqual(self.pc.fstab.read_text(), written)
        self.ran.clear()
        self.use(self.pc.chosen(new_files=True))
        self.assertEqual(self.ran, [['systemctl', 'daemon-reload'], ['mount', '-o', 'remount,bind,rw', str(point)]])
        # The choice taken back: the block goes, the other lines stay.
        self.use(self.pc.config)
        self.assertEqual(self.pc.fstab.read_text(), earlier)

    def test_a_share_made_permanent_is_added_once_outside_the_block(self):
        self.pc.fstab.write_text('UUID=1 / ext4 defaults 0 1\n')
        found, _ = self.pc.check()
        config = existing_media.with_choice(self.pc.config, existing_media.entry_for('movies', found))
        for _ in range(2):
            self.use(config, [found['add_to_fstab']])
        lines = self.pc.fstab.read_text().splitlines()
        self.assertEqual(lines.count(found['add_to_fstab']), 1)
        self.assertLess(lines.index(found['add_to_fstab']), lines.index(existing_media.BEGIN))
        self.use(self.pc.config)
        self.assertIn(found['add_to_fstab'], self.pc.fstab.read_text(), 'the share stays mounted at start')

    def test_a_share_that_is_away_is_not_mounted_and_nothing_is_written_in_its_place(self):
        config = self.pc.chosen(new_files=True)
        del self.pc.mounts[str(self.pc.nas)]  # the NAS is off: its folder is the bare one
        rows = self.use(config)
        self.assertEqual(rows[0]['mounted'], False)
        self.assertIn('are not there now (nas:/tank is not connected)', rows[0]['reason'])
        self.assertNotIn(['mount', str(self.pc.media / 'library/existing/movies')], self.ran)
        # Another share mounted at the same place is not the one that was chosen either.
        self.pc.mounts[str(self.pc.nas)] = ('other:/films', 'nfs4')
        self.assertFalse(self.use(config)[0]['mounted'])
        (state,) = existing_media.available(config, **self.pc.how())
        self.assertFalse(state['ready'])
        with self.assertRaises(Away):
            existing_media.guard(config, 'movies', **self.pc.how())
        existing_media.guard(config, 'tv', **self.pc.how())  # no existing folder for shows

    def test_ready_only_when_the_share_is_there_and_bound_into_the_library(self):
        config = self.pc.chosen()
        (state,) = existing_media.available(config, **self.pc.how())
        self.assertEqual((state['ready'], state['container']), (False, '/data/existing/movies'))
        self.assertIn('not connected to the library yet', state['reason'])
        self.use(config)
        (state,) = existing_media.available(config, **self.pc.how())
        self.assertEqual((state['ready'], state['reason']), (True, None))
        existing_media.guard(config, 'movies', **self.pc.how())


class Jellyfin:
    """Jellyfin's library API, as far as it is used."""

    def __init__(self, libraries=()):
        self.libraries, self.calls = list(libraries), []

    def call(self, endpoint, method='GET', payload=None):
        self.calls.append((method, endpoint))
        if method == 'GET':
            return self.libraries
        query = dict(part.split('=') for part in endpoint.split('?')[1].split('&'))
        self.libraries.append(
            {'Name': query['name'].replace('+', ' '), 'Locations': [query['paths'].replace('%2F', '/')], 'ItemId': '9'}
        )
        return {}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.pc = Server(self)
        self.config = self.pc.chosen()
        self.pc.bound[str(self.pc.media / 'library/existing/movies')] = '77'

    def libraries(self, client, apply=True, config=None):
        return existing_media.jellyfin_libraries(client, config or self.config, apply, **self.pc.how())

    def test_jellyfin_gets_a_library_at_the_folder_as_it_sees_it(self):
        ours = {'Name': 'Movies', 'Locations': ['/data/movies'], 'CollectionType': 'movies', 'ItemId': '1'}
        client = Jellyfin([ours])
        self.assertEqual(self.libraries(client), [{'check': 'jellyfin:existing:movies', 'status': 'pass'}])
        (posted,) = [endpoint for method, endpoint in client.calls if method == 'POST']
        self.assertIn('paths=%2Fdata%2Fexisting%2Fmovies', posted)
        self.assertIn('collectionType=movies', posted)
        self.assertIn('name=Movies+%28your+folder%29', posted)
        # NekoServer's own library is as it was, and still found by its own check.
        self.assertEqual(media_libraries.matching(client.libraries, 'movies', 'Movies', 'movies'), ours)
        # Run again: there already, nothing made twice.
        client.calls.clear()
        self.assertEqual(self.libraries(client)[0]['status'], 'pass')
        self.assertEqual([method for method, _ in client.calls], ['GET'])

    def test_a_preview_and_a_share_that_is_away_make_nothing(self):
        client = Jellyfin()
        self.assertEqual(self.libraries(client, apply=False)[0]['status'], 'needs_setup')
        del self.pc.mounts[str(self.pc.nas)]
        (row,) = self.libraries(client)
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('are not there now', row['reason'])
        self.assertEqual(client.libraries, [])
        self.assertNotIn('POST', [method for method, _ in client.calls])

    def test_a_library_of_that_name_for_another_folder_is_left_alone(self):
        client = Jellyfin([{'Name': 'movies (your folder)', 'Locations': ['/somewhere'], 'ItemId': '3'}])
        (row,) = self.libraries(client)
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('already has a library called', row['reason'])
        self.assertEqual(len(client.libraries), 1)

    def test_navidrome_gets_a_second_music_folder(self):
        (self.pc.nas / 'music').mkdir()
        found, _ = self.pc.check(folder=self.pc.nas / 'music', category='music')
        config = existing_media.with_choice(self.config, existing_media.entry_for('music', found))
        self.pc.bound[str(self.pc.ssd / 'library/existing/music')] = '78'
        have, calls = [{'id': 1, 'name': 'Music Library', 'path': '/music'}], []

        def call(method, endpoint, payload):
            calls.append((method, endpoint, payload))
            if method == 'POST':
                have.append({'id': 2, **payload})
            return have

        rows = existing_media.navidrome_library(call, config, True, **self.pc.how())
        self.assertEqual(rows, [{'check': 'navidrome:existing:music', 'status': 'pass'}])
        self.assertIn(('POST', '/api/library', {'name': 'Music (your folder)', 'path': '/data/existing/music'}), calls)
        calls.clear()
        existing_media.navidrome_library(call, config, True, **self.pc.how())
        self.assertEqual([method for method, *_ in calls], ['GET'])
        # No music folder chosen: Navidrome is not asked anything.
        self.assertEqual(existing_media.navidrome_library(call, self.config, True, **self.pc.how()), [])

        # A Navidrome too old for a second folder: a step left, said plainly.
        def old(method, endpoint, payload):
            raise LookupError('404')

        (row,) = existing_media.navidrome_library(old, config, True, **self.pc.how())
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('0.58', row['reason'])

    def test_the_apps_compose_files_mount_the_place_the_folders_are_bound_to(self):
        for app, drive in (('jellyfin', 'MEDIA'), ('navidrome', 'MUSIC')):
            text = (ROOT / 'compose' / f'{app}.yaml').read_text()
            block = text[text.index('      - type: bind') :]
            self.assertIn(f'        source: "${{NEKO_{drive}}}/library/existing"\n', block, app)
            self.assertIn('        target: /data/existing\n', block, app)
            self.assertIn('        read_only: true\n', block, app)
            # A share that comes back after the app started is seen without a restart.
            self.assertIn('          propagation: rslave\n', block, app)


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.pc = Server(self)

    def preview(self, free=10**9):
        return existing_media.copy_preview(
            self.pc.config,
            'movies',
            str(self.pc.films),
            find=self.pc.find,
            kind=self.pc.kind,
            fstab=self.pc.fstab,
            usage=lambda root: SimpleNamespace(free=free),
        )

    def test_the_preview_says_size_room_and_conflicts_and_copies_nothing(self):
        library = self.pc.media / 'library' / 'movies'
        (library / 'Brazil (1985)').mkdir()
        (library / 'Brazil (1985)' / 'Brazil.mkv').write_bytes(b'another cut')
        before, had = tree(self.pc.films), tree(library)
        shown = self.preview()
        self.assertEqual((shown['files'], shown['bytes']), (1, 400))
        self.assertEqual(shown['conflicts'], ['Brazil (1985)/Brazil.mkv'])
        self.assertEqual((shown['source'], shown['destination']), (str(self.pc.films), str(library)))
        self.assertEqual((shown['free'], shown['fits'], shown['source_kept']), (10**9, True, True))
        self.assertEqual((tree(self.pc.films), tree(library)), (before, had), 'a preview writes nothing')
        # Too little room is said in the preview, not found out half-way.
        self.assertFalse(self.preview(free=399)['fits'])
        # What goes into the source list for source_import (which keeps the source).
        self.assertEqual(
            shown['entry'],
            {'type': 'remote', 'category': 'movies', 'copy_to_library': True, 'mounted_path': str(self.pc.films)},
        )
        self.assertEqual(source_import.configured_folder(shown['entry']), self.pc.films)

    def test_a_share_that_is_away_is_refused_before_any_preview(self):
        del self.pc.mounts[str(self.pc.nas)]
        with self.assertRaises(Refused) as caught:
            self.preview()
        self.assertEqual(caught.exception.code, 'not_mounted')


if __name__ == '__main__':
    unittest.main()
