"""Media a person already has, used where it is (existing_media.py; wizard page P03,
checks T12 to T14). Everything here runs on folders of its own with stand-ins for
findmnt, the apps' account, mount and the apps' APIs: no Docker, no network."""

import os
import sys
import tempfile
import unittest
import urllib.parse
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
        self.assertTrue(found['new_files']['ok'])
        # No hard link is promised: the download is copied in, and needs room twice meanwhile.
        self.assertFalse(found['hardlinks'])
        note = found['new_files']['note']
        self.assertIn(f"downloaded to {self.pc.media / 'library' / 'downloads'} first", note)
        self.assertIn(f'then copied into {self.pc.films}', note)
        self.assertIn('needs room in both places', note)
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
            wrong = with_entry(**changes)
            with self.assertRaises(RuntimeError, msg=changes):
                existing_media.extras(wrong)
            with self.assertRaises(RuntimeError, msg=changes):
                existing_media.fstab_lines(wrong)
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
        mounted = {'holds': 'movies', 'use': 'in_place', 'path': str(self.pc.films), 'mounted': True, 'reason': None}
        self.assertEqual(rows, [mounted])
        self.assertEqual(self.ran, [['systemctl', 'daemon-reload'], ['mount', str(point)]])
        written = self.pc.fstab.read_text()
        self.assertTrue(written.startswith(earlier), 'the lines that were there stay, in order')
        self.assertEqual(
            written[len(earlier) :].splitlines(),
            [existing_media.BEGIN, *existing_media.fstab_lines(config), existing_media.END],
        )
        # The mount point is root's and closed: nothing can be written into the bare folder.
        self.assertEqual(point.stat().st_mode & 0o777, 0o500)
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

    def test_a_block_without_its_end_line_stops_before_any_line_is_lost(self):
        mine = 'UUID=7 /mnt/my-backups ext4 defaults 0 2'
        broken = '\n'.join(['UUID=1 / ext4 defaults 0 1', existing_media.BEGIN, 'old line', mine]) + '\n'
        self.pc.fstab.write_text(broken)
        chosen = self.pc.chosen()
        with self.assertRaisesRegex(RuntimeError, 'Nothing was changed'):
            self.use(chosen)
        self.assertEqual(self.pc.fstab.read_text(), broken, "the person's own mount is still there")
        self.assertEqual(self.ran, [])

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
        self.assertIs(rows[0]['mounted'], False)
        self.assertIn('are not there now (nas:/tank is not connected)', rows[0]['reason'])
        self.assertNotIn(['mount', str(self.pc.media / 'library/existing/movies')], self.ran)
        # Another share mounted at the same place is not the one that was chosen either.
        self.pc.mounts[str(self.pc.nas)] = ('other:/films', 'nfs4')
        self.assertFalse(self.use(config)[0]['mounted'])
        (state,) = existing_media.available(config, **self.pc.how())
        self.assertFalse(state['ready'])
        how = self.pc.how()
        with self.assertRaises(Away):
            existing_media.guard(config, 'movies', **how)
        existing_media.guard(config, 'tv', **self.pc.how())  # no existing folder for shows

    def test_a_share_that_does_not_answer_is_away_and_never_looked_at_here(self):
        config = self.pc.chosen()
        self.use(config)
        looked = []
        with (
            mock.patch.object(existing_media, 'folder_answers', return_value=False),
            mock.patch.object(existing_media.os.path, 'isdir', side_effect=looked.append),
            mock.patch.object(existing_media.Path, 'is_dir', side_effect=lambda *a: looked.append(a)),
        ):
            (state,) = existing_media.available(config, **self.pc.how())
            rows = self.use(config)
        self.assertFalse(state['ready'])
        self.assertIn('are not there now (nas:/tank is not connected)', state['reason'])
        self.assertFalse(rows[0]['mounted'])
        self.assertEqual(looked, [], 'nothing of the share, or of the folder it is bound to, is looked at')

    def test_a_look_that_does_not_end_is_given_up_and_its_child_is_not_waited_for(self):
        import neko_common

        class Stuck:
            killed = False

            def poll(self):
                return None

            def kill(self):
                Stuck.killed = True

        clock = iter(range(100))
        got = neko_common.answered_within(['x'], 15, start=lambda *a, **k: Stuck(), now=lambda: next(clock), sleep=id)
        self.assertIsNone(got)
        self.assertTrue(Stuck.killed)
        self.assertTrue(neko_common.folder_answers(self.pc.films))
        self.assertFalse(neko_common.folder_answers(self.pc.films / 'not-there'))

    def test_a_saved_folder_whose_share_does_not_answer_is_still_listed_by_its_words(self):
        config = self.pc.chosen()
        with mock.patch.object(existing_media, 'real_path', return_value=None):
            (entry,) = existing_media.extras(config)
            self.assertEqual(entry['path'], str(self.pc.films))
            # Its words are still checked: a path that leaves its folder is refused.
            config['placement']['movies']['extra'][0]['path'] = str(self.pc.films) + '/../../ssd'
            with self.assertRaises(RuntimeError):
                existing_media.extras(config)
        import neko_common

        self.assertEqual(neko_common.real_path(self.pc.films), os.path.realpath(self.pc.films))

    def test_only_a_path_on_a_share_is_asked_aside_and_its_answer_is_kept(self):
        import neko_common

        table = self.pc.top / 'mountinfo'
        table.write_text(
            '22 1 8:1 / / rw - ext4 /dev/sda1 rw\n'
            '40 22 0:50 / /mnt/my\\040tank rw - nfs4 nas:/tank rw\n'
            '41 22 0:51 / /mnt/auto rw - autofs systemd-1 rw\n'
        )
        self.assertEqual(neko_common.network_mounts(table), ['/mnt/my tank', '/mnt/auto'])
        self.assertTrue(neko_common.on_a_share('/mnt/my tank/films', table))
        self.assertTrue(neko_common.on_a_share('/mnt/auto', table))
        self.assertFalse(neko_common.on_a_share('/mnt/my tanker', table))
        self.assertTrue(neko_common.on_a_share('/x', self.pc.top / 'no-such-table'))  # can't be told: aside
        asked = []

        def ask(argv, seconds):
            asked.append(argv)
            return None  # the share does not answer

        self.addCleanup(neko_common._ASKED.clear)
        neko_common._ASKED.clear()
        local = neko_common.real_path(self.pc.films, table=table, ask=ask)
        self.assertEqual((local, asked), (os.path.realpath(self.pc.films), []), 'a folder of this PC: no child')
        self.assertIsNone(neko_common.real_path('/mnt/my tank/films', table=table, ask=ask))
        self.assertIsNone(neko_common.real_path('/mnt/my tank/films', table=table, ask=ask))
        self.assertEqual(len(asked), 1, 'away: not asked again at once')
        got = neko_common.real_path('/mnt/my tank/music', table=table, ask=lambda argv, s: (0, '/mnt/tank2\n'))
        self.assertEqual(got, '/mnt/tank2')
        self.assertEqual(neko_common.real_path('/mnt/my tank/music', table=table, ask=ask), got, 'kept for the run')
        self.assertEqual(len(asked), 1)
        self.assertFalse(neko_common.folder_answers('/mnt/my tank/films', table=table), 'still away')

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
        query = {name: values[0] for name, values in urllib.parse.parse_qs(endpoint.split('?')[1]).items()}
        self.libraries.append({'Name': query['name'], 'Locations': [query['paths']], 'ItemId': '9'})
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


class NewFilesTests(unittest.TestCase):
    """ "Save new ones here too": the place new films, shows and music are saved."""

    def setUp(self):
        self.pc = Server(self)
        self.new = self.pc.nas / 'new-films'
        self.new.mkdir()

    def both(self):
        """The films used in place (read-only), and a folder of its own for the new ones."""
        config = self.pc.chosen()
        found, _ = self.pc.check(folder=self.new, new_files=True)
        entry = existing_media.entry_for('movies', found, use=existing_media.NEW)
        return existing_media.with_choice(config, entry)

    def test_a_read_only_folder_is_kept_and_new_files_get_a_folder_of_their_own(self):
        config = self.both()
        self.assertEqual(
            [(e['use'], Path(e['path']).name, e['new_files']) for e in existing_media.extras(config)],
            [('in_place', 'films', False), ('new_files', 'new-films', True)],
        )
        # The apps are given the folder for new files; the films' own stays read-only.
        self.assertEqual(existing_media.new_files_folder(config, 'movies'), '/data/existing/movies-new')
        self.assertEqual(existing_media.new_files_folder(config, 'tv'), '/data/tv')
        library = self.pc.media / 'library' / 'existing'
        first, second = (line.split() for line in existing_media.fstab_lines(config))
        self.assertEqual((first[1], first[3].split(',')[1]), (str(library / 'movies'), 'ro'))
        self.assertEqual((second[:2], second[3].split(',')[1]), ([str(self.new), str(library / 'movies-new')], 'rw'))

    def test_there_is_one_place_for_new_files(self):
        config = self.both()
        # The films' folder made writable after all: it takes the new files, the other goes.
        again = self.pc.chosen(new_files=True)['placement']['movies']['extra'][0]
        one = existing_media.with_choice(config, again)
        self.assertEqual([(e['use'], e['new_files']) for e in existing_media.extras(one)], [('in_place', True)])
        self.assertEqual(existing_media.new_files_folder(one, 'movies'), '/data/existing/movies')
        # And back: a folder of their own takes them from the films' folder.
        found, _ = self.pc.check(folder=self.new, new_files=True)
        back = existing_media.with_choice(one, existing_media.entry_for('movies', found, use=existing_media.NEW))
        self.assertEqual(
            [(e['use'], e['new_files']) for e in existing_media.extras(back)],
            [('in_place', False), ('new_files', True)],
        )
        only = existing_media.without_choice(back, 'movies', existing_media.NEW)
        self.assertEqual([e['use'] for e in existing_media.extras(only)], ['in_place'])
        self.assertEqual(existing_media.new_files_folder(only, 'movies'), '/data/movies')

    def test_a_folder_that_cannot_be_written_never_becomes_the_place_for_new_files(self):
        found, _ = self.pc.check(folder=self.new, new_files=True, flags=os.ST_RDONLY)
        with self.assertRaises(RuntimeError):
            existing_media.entry_for('movies', found, use=existing_media.NEW)
        found, _ = self.pc.check(folder=self.new)  # not even tried
        with self.assertRaises(RuntimeError):
            existing_media.entry_for('movies', found, use=existing_media.NEW)
        # Nor by a settings file changed by hand.
        config = self.both()
        entries = config['placement']['movies']['extra']
        for changed in (
            [{**entries[0], 'new_files': True}, entries[1]],  # two places
            [entries[0], {**entries[1], 'new_files': False}],
            [entries[0], {**entries[1], 'use': 'anything'}],
            [entries[1], entries[1]],
        ):
            bad = existing_media.without_choice(config, 'movies')
            bad['placement']['movies']['extra'] = changed
            with self.assertRaises(RuntimeError, msg=changed):
                existing_media.extras(bad)

    def test_the_apps_are_told_only_while_the_folder_is_there(self):
        config = self.both()
        how = self.pc.how()
        self.assertIsNone(existing_media.new_files_ready(self.pc.config, 'movies', **how))
        with self.assertRaises(Away):  # chosen, not mounted into the library yet
            existing_media.new_files_ready(config, 'movies', **how)
        for use in existing_media.USES:
            self.pc.bound[str(existing_media.mount_point(config, 'movies', use))] = '80'
        self.assertEqual(existing_media.new_files_ready(config, 'movies', **how), '/data/existing/movies-new')
        del self.pc.mounts[str(self.pc.nas)]  # the share goes away
        with self.assertRaises(Away) as caught:
            existing_media.new_files_ready(config, 'movies', **how)
        self.assertIn('Nothing is written in their place', str(caught.exception))

    def test_jellyfin_gets_a_library_for_the_new_ones_too(self):
        config = self.both()
        for use in existing_media.USES:
            self.pc.bound[str(existing_media.mount_point(config, 'movies', use))] = '80'
        client = Jellyfin()
        rows = existing_media.jellyfin_libraries(client, config, True, **self.pc.how())
        self.assertEqual(
            [(row['check'], row['status']) for row in rows],
            [('jellyfin:existing:movies', 'pass'), ('jellyfin:existing:movies-new', 'pass')],
        )
        self.assertEqual(
            sorted((x['Name'], x['Locations'][0]) for x in client.libraries),
            [
                ('Movies (new, your folder)', '/data/existing/movies-new'),
                ('Movies (your folder)', '/data/existing/movies'),
            ],
        )

    def test_the_download_and_import_apps_see_the_same_path(self):
        for app, drive in (('sonarr', 'MEDIA'), ('radarr', 'MEDIA'), ('lidarr', 'MUSIC'), ('qbittorrent', 'MEDIA')):
            text = (ROOT / 'compose' / f'{app}.yaml').read_text()
            block = text[text.index('      - type: bind') :]
            self.assertIn(f'        source: "${{NEKO_{drive}}}/library/existing"\n', block, app)
            self.assertIn('        target: /data/existing\n', block, app)
            self.assertIn('          propagation: rslave\n', block, app)
            # They write there when the mount allows it: not read-only in the app itself.
            self.assertNotIn('read_only', block, app)


def load(name, file):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RootFolderTests(unittest.TestCase):
    """media-connect.py --root-folders: Sonarr, Radarr and Lidarr are given the person's
    folder for new files as a library folder of theirs, through their API."""

    def setUp(self):
        self.pc = Server(self)
        self.connect = load('media_connect_existing', 'media-connect.py')
        (self.pc.media / 'library' / 'tv').mkdir()
        self.music = self.pc.ssd / 'library' / 'music'
        self.music.mkdir(parents=True)
        self.folders = {'sonarr': [], 'radarr': [], 'lidarr': []}
        self.posted = []

    def request(self, app, key, endpoint, method='GET', payload=None):
        if endpoint != '/rootfolder':
            return [{'id': 1, 'name': 'Any'}]  # Lidarr's profiles
        if method == 'POST':
            self.posted.append((app, payload['path'], payload.get('name')))
            self.folders[app].append({**payload, 'accessible': True})
        return self.folders[app]

    def configure(self, config, apps=('sonarr', 'radarr', 'lidarr')):
        real = existing_media.available

        def available(settings, **how):
            return real(settings, **self.pc.how())

        with (
            mock.patch.object(self.connect, 'app_key', return_value='k'),
            mock.patch.object(self.connect, 'request', self.request),
            mock.patch.object(existing_media, 'available', available),
            mock.patch('builtins.print'),
        ):
            self.connect.configure_root_folders(self.pc.ssd, self.pc.media, True, apps, self.music, config)

    def chosen(self):
        """New films in the films' own folder, new music in a folder of the person's."""
        config = self.pc.chosen(new_files=True)
        (self.pc.nas / 'music').mkdir(exist_ok=True)
        found, _ = self.pc.check(folder=self.pc.nas / 'music', new_files=True, category='music')
        config = existing_media.with_choice(config, existing_media.entry_for('music', found, True))
        for entry in existing_media.extras(config):
            self.pc.bound[str(existing_media.mount_point(config, entry['holds'], entry['use']))] = '81'
        return config

    def test_each_app_gets_the_persons_folder_beside_nekoservers_own(self):
        self.configure(self.chosen())
        self.assertEqual(
            self.posted,
            [
                ('sonarr', '/data/tv', None),  # nothing chosen for shows
                ('radarr', '/data/movies', None),
                ('radarr', '/data/existing/movies', None),
                ('lidarr', '/data/music', 'Neko Music'),
                ('lidarr', '/data/existing/music', 'Neko Music (your folder)'),
            ],
        )
        # Run again: both are there, nothing is added twice.
        self.posted.clear()
        self.configure(self.chosen())
        self.assertEqual(self.posted, [])

    def test_without_a_folder_of_the_persons_it_is_as_before(self):
        self.configure(self.pc.config)
        self.assertEqual([path for _, path, _ in self.posted], ['/data/tv', '/data/movies', '/data/music'])

    def test_a_share_that_is_away_is_not_handed_to_the_app(self):
        config = self.chosen()
        del self.pc.mounts[str(self.pc.nas)]
        with self.assertRaises(RuntimeError) as caught:
            self.configure(config)
        self.assertIn('are not there now', str(caught.exception))
        self.assertIn('radarr', str(caught.exception))
        # NekoServer's own folders are still given; the person's are not, so no app makes
        # a folder in the bare mount point.
        self.assertEqual([path for _, path, _ in self.posted], ['/data/tv', '/data/movies', '/data/music'])


class Answer:
    def __init__(self, value):
        self.data = __import__('json').dumps(value).encode()

    def read(self, limit=None):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *left):
        return False


class Navidrome:
    """Navidrome's sign-in and library API, as a stand-in opener."""

    def __init__(self, admin=True, libraries=True):
        self.admin, self.libraries, self.seen = admin, libraries, []
        self.have = [{'id': 1, 'name': 'Music Library', 'path': '/music'}]

    def open(self, asked, timeout=None):
        import json
        import urllib.error

        path = asked.full_url.split('4533', 1)[1]
        body = json.loads(asked.data) if asked.data else None
        self.seen.append((asked.get_method(), path, body, asked.get_header('X-nd-authorization')))
        if path == '/auth/login':
            if body['password'] != 'right':
                raise urllib.error.HTTPError(asked.full_url, 401, 'no', {}, None)
            return Answer({'token': 'tok', 'isAdmin': self.admin})
        if not self.libraries:
            raise urllib.error.HTTPError(asked.full_url, 404, 'no', {}, None)
        if asked.get_method() == 'POST':
            self.have.append({'id': 2, **body})
        return Answer(self.have)


class MusicTests(unittest.TestCase):
    """An existing music folder, played by Navidrome from where it is."""

    def setUp(self):
        self.pc = Server(self)
        (self.pc.nas / 'music').mkdir()
        found, _ = self.pc.check(folder=self.pc.nas / 'music', category='music')
        self.config = existing_media.with_choice(self.pc.config, existing_media.entry_for('music', found))
        self.pc.bound[str(self.pc.ssd / 'library/existing/music')] = '78'

    def libraries(self, navidrome, saved, config=None):
        def api(username, password):
            return existing_media.navidrome_api(username, password, 'http://127.0.0.1:4533', navidrome)

        with mock.patch.object(existing_media.account_store, 'get', return_value=saved) as asked:
            rows = existing_media.music_libraries(config or self.config, True, api, **self.pc.how())
        return rows, asked

    def test_navidrome_is_signed_in_to_as_its_administrator_and_given_the_folder(self):
        navidrome = Navidrome()
        rows, asked = self.libraries(navidrome, {'username': 'neko', 'password': 'right'})
        self.assertEqual(rows, [{'check': 'navidrome:existing:music', 'status': 'pass'}])
        asked.assert_called_once_with(self.config, 'navidrome', verified=True)
        self.assertEqual(navidrome.seen[0], ('POST', '/auth/login', {'username': 'neko', 'password': 'right'}, None))
        added = {'name': 'Music (your folder)', 'path': '/data/existing/music'}
        self.assertIn(('POST', '/api/library', added, 'Bearer tok'), navidrome.seen)
        # Every request after the sign-in carries the token, never the password.
        self.assertTrue(all(token == 'Bearer tok' for *_, token in navidrome.seen[1:]))
        self.assertEqual([x['path'] for x in navidrome.have], ['/music', '/data/existing/music'])
        # The folder is mounted read-only for Navidrome: one line, at the music drive.
        (line,) = existing_media.fstab_lines(self.config)
        self.assertEqual(line.split()[1], str(self.pc.ssd / 'library/existing/music'))
        self.assertEqual(line.split()[3].split(',')[1], 'ro')

    def test_what_is_in_the_way_is_a_step_left_with_its_reason(self):
        for navidrome, saved, said in (
            (Navidrome(), None, 'administrator login is not set up yet'),
            (Navidrome(), {'username': 'neko', 'password': 'wrong'}, 'did not accept its administrator login'),
            (Navidrome(admin=False), {'username': 'neko', 'password': 'right'}, 'not an administrator'),
            (Navidrome(libraries=False), {'username': 'neko', 'password': 'right'}, 'version 0.58'),
        ):
            (row,), _ = self.libraries(navidrome, saved)
            self.assertEqual(row['status'], 'needs_setup', said)
            self.assertIn(said, row['reason'])
            self.assertEqual(len(navidrome.have), 1, 'nothing was added')

    def test_no_music_folder_and_navidrome_is_not_asked_anything(self):
        navidrome = Navidrome()
        rows, asked = self.libraries(navidrome, {'username': 'neko', 'password': 'right'}, self.pc.config)
        self.assertEqual((rows, navidrome.seen), ([], []))
        asked.assert_not_called()

    def test_a_share_that_is_away_makes_no_library(self):
        del self.pc.mounts[str(self.pc.nas)]
        navidrome = Navidrome()
        (row,), _ = self.libraries(navidrome, {'username': 'neko', 'password': 'right'})
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('are not there now', row['reason'])
        self.assertEqual(len(navidrome.have), 1)


class CopyNowTests(unittest.TestCase):
    """Copy instead of use in place: after the preview, the copy, the source kept."""

    def setUp(self):
        self.pc = Server(self)
        self.runs = []

    def run_import(self, config, sources, apply=False, verify=None, again=False):
        import json

        self.runs.append((sources, apply, again))
        verify()
        report = self.pc.ssd / 'reports'
        report.mkdir(exist_ok=True)
        row = {'source_number': 1, 'category': 'movies', 'status': 'copied', 'copied': 2, 'bytes': 600}
        (report / 'source-import.json').write_text(json.dumps({'apply': apply, 'sources': [row]}))
        return 0

    def copy(self, free=10**9):
        verified = []
        result = existing_media.copy_now(
            self.pc.config,
            'movies',
            str(self.pc.films),
            run=self.run_import,
            verify=verified.append,
            find=self.pc.find,
            kind=self.pc.kind,
            fstab=self.pc.fstab,
            usage=lambda root: SimpleNamespace(free=free),
        )
        return result, verified

    def test_the_copy_is_the_imports_own_with_this_one_folder(self):
        import json

        result, verified = self.copy()
        entry = {'type': 'remote', 'category': 'movies', 'copy_to_library': True, 'mounted_path': str(self.pc.films)}
        self.assertEqual(self.runs, [([entry], True, False)])
        self.assertEqual(verified, [self.pc.config], 'the drives are checked as the import asks')
        self.assertEqual((result['status'], result['copied']), ('copied', 2))
        self.assertEqual((result['source'], result['source_kept']), (str(self.pc.films), True))
        # The folder is in the source list, once, however often it is copied.
        self.copy()
        saved = json.loads((self.pc.ssd / 'config' / 'media-sources.json').read_text())
        self.assertEqual(saved, {'schema_version': 1, 'sources': [entry]})

    def test_too_little_room_is_said_with_the_sizes_before_anything_is_copied(self):
        with self.assertRaises(RuntimeError) as caught:
            self.copy(free=599)
        self.assertIn('needs 600 bytes', str(caught.exception))
        self.assertIn('has 599 bytes free, so nothing was copied', str(caught.exception))
        self.assertEqual(self.runs, [])
        self.assertFalse((self.pc.ssd / 'config').exists())
        self.assertEqual(existing_media.size_words(1_500_000_000), '1.5 GB')

    def test_a_share_that_is_away_is_refused_and_nothing_runs(self):
        del self.pc.mounts[str(self.pc.nas)]
        with self.assertRaises(Refused):
            self.copy()
        self.assertEqual(self.runs, [])

    @unittest.skipUnless(os.geteuid() == 0, 'the copy hands the files to the apps\' account: root')
    def test_the_real_copy_keeps_the_source_and_the_librarys_own_files(self):
        self.pc.mounts[str(self.pc.nas)] = ('/dev/sdd1', 'ext4')  # a local drive: read as a folder
        self.pc.fstab.write_text(f'UUID=9 {self.pc.nas} ext4 defaults 0 2\n')
        library = self.pc.media / 'library' / 'movies'
        (library / 'Brazil (1985)').mkdir()
        (library / 'Brazil (1985)' / 'Brazil.mkv').write_bytes(b'another cut')
        before = tree(self.pc.films)
        shown = existing_media.copy_preview(
            self.pc.config, 'movies', str(self.pc.films), find=self.pc.find, kind=self.pc.kind, fstab=self.pc.fstab
        )
        row = source_import.copy_source(shown['entry'], self.pc.media, True, 1000, 1000)
        self.assertEqual((row['status'], row['copied'], row['bytes']), ('copied', shown['files'], shown['bytes']))
        self.assertEqual(row['kept_library_version'], 1)
        self.assertEqual((library / 'Alien (1979)' / 'Alien.mkv').read_bytes(), b'film' * 100)
        self.assertEqual((library / 'Brazil (1985)' / 'Brazil.mkv').read_bytes(), b'another cut')
        self.assertEqual(tree(self.pc.films), before, 'the source is as it was')


if __name__ == '__main__':
    unittest.main()
