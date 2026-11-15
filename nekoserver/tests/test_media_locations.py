"""The dashboard's Media folders page, as root does it (media_locations.py): a request
the control service wrote is read, the change is made by setup's own code
(existing_media.py, existing_page.py), and the answer is written for the page.

End to end on folders of its own: a stand-in PC with an app SSD, a media drive and a
NAS share, and stand-ins on PATH for findmnt, mount, umount, systemctl and docker (they
keep what is mounted in a small state file and a mount table of their own). The apps'
APIs (Jellyfin, Navidrome, Sonarr/Radarr/Lidarr, Seerr) are stand-in steps that note
what they were asked. No root, no real mounts, no Docker, no network."""

import contextlib
import fcntl
import functools
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import existing_media  # noqa: E402
import existing_page  # noqa: E402
import media_locations  # noqa: E402
import source_import  # noqa: E402

# One stand-in for findmnt, mount, umount, systemctl and docker: what it is called as
# decides what it does. State: {"mounts": {folder: [source, type]}, "bound": {point:
# source}, "busy": [points an app holds open]}; every call is noted in calls.log.
STAND_IN = r'''#!/usr/bin/env python3
import json, os, re, sys
state_path = os.environ['NEKO_STAND_IN']
state = json.load(open(state_path))
me, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(os.path.dirname(state_path), 'calls.log'), 'a') as log:
    log.write(' '.join([me, *args]) + '\n')
def save():
    json.dump(state, open(state_path, 'w'))
    with open(state['table'], 'w') as table:
        for n, (point, source) in enumerate(sorted(state['bound'].items())):
            table.write('%d 1 0:%d / %s rw - none %s rw\n' % (100 + n, n, point.replace(' ', '\\040'), source))
def unescaped(field):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), field)
if me == 'findmnt':
    columns, by, path = args[2], args[3], args[4]
    mounts = state['mounts']
    if by == '--target':
        held = [m for m in mounts if path == m or path.startswith(m.rstrip('/') + '/')]
        if not held:
            sys.exit(1)
        point = max(held, key=len)
    elif path in mounts:
        point = path
    else:
        sys.exit(1)
    said = {'TARGET': point, 'SOURCE': mounts[point][0], 'FSTYPE': mounts[point][1]}
    print(' '.join(said[c] for c in columns.split(',')))
elif me == 'mount':
    point = args[-1]
    if args[0] == '-o':
        state.setdefault('remounts', []).append([point, args[1]])
    else:
        for line in open(state['fstab']):
            fields = line.split()
            if len(fields) > 3 and not line.startswith('#') and unescaped(fields[1]) == point:
                state['bound'][point] = unescaped(fields[0])
                break
        else:
            sys.exit('mount: %s: not in fstab' % point)
    save()
elif me == 'umount':
    if args[-1] in state['busy']:
        sys.exit('umount: %s: target is busy.' % args[-1])
    state['bound'].pop(args[-1], None)
    save()
elif me == 'systemctl':
    pass
elif me == 'docker':
    pass
'''


class PC:
    """A stand-in PC: the app SSD, the media drive, and a NAS share (mounted, in
    /etc/fstab) with films, shows and music on it; NekoServer's settings in both places
    setup keeps them (the copy on the SSD, and the folder setup was run from)."""

    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory(prefix='neko-media-locations-')
        case.addCleanup(tmp.cleanup)
        self.case = case
        self.top = top = Path(os.path.realpath(tmp.name))
        self.ssd, self.media, self.nas = top / 'ssd', top / 'media', top / 'nas'
        self.films, self.shows = self.nas / 'films', self.nas / 'shows'
        for folder in (
            self.films / 'Alien (1979)',
            self.shows / 'Bluey' / 'Season 1',
            self.nas / 'music' / 'Abba',
            self.ssd / 'installer',
            self.media / 'library' / 'movies',
            self.media / 'library' / 'tv',
            self.ssd / 'library' / 'music',
        ):
            folder.mkdir(parents=True)
        (self.films / 'Alien (1979)' / 'Alien.mkv').write_bytes(b'film' * 100)
        (self.shows / 'Bluey' / 'Season 1' / 'Bluey S01E01.mkv').write_bytes(b'show' * 50)
        for app in ('jellyfin', 'radarr', 'navidrome'):
            compose = self.ssd / 'compose' / app / 'compose.yaml'
            compose.parent.mkdir(parents=True)
            compose.write_text(f'services:\n  {app}:\n    volumes:\n      - /x/library/existing:/data/existing\n')
        self.fstab = top / 'fstab'
        self.fstab.write_text(f'UUID=1 / ext4 defaults 0 1\nnas:/tank {self.nas} nfs4 hard,nofail 0 0\n')
        self.table = top / 'mountinfo'
        self.table.write_text('')
        self.state_file = top / 'stand-in.json'
        self.state = {
            'mounts': {
                '/': ['/dev/sda1', 'ext4'],
                str(self.nas): ['nas:/tank', 'nfs4'],
                str(self.media): ['/dev/sdb1', 'ext4'],
                str(self.ssd): ['/dev/sdc1', 'ext4'],
            },
            'bound': {},
            'busy': [],
            'fstab': str(self.fstab),
            'table': str(self.table),
        }
        self.save_state()
        bin_dir = top / 'bin'
        bin_dir.mkdir()
        script = bin_dir / 'stand-in'
        script.write_text(STAND_IN)
        script.chmod(0o755)
        for name in ('findmnt', 'mount', 'umount', 'systemctl', 'docker'):
            (bin_dir / name).symlink_to(script)
        uid, gid = os.getuid() or 1000, os.getgid() or 1000
        self.config = {
            'data_mount': str(self.ssd),
            'media_mount': str(self.media),
            'media': {
                'uid': uid,
                'gid': gid,
                'services': ['jellyfin', 'navidrome', 'radarr', 'sonarr', 'lidarr', 'seerr', 'qbittorrent'],
            },
            'storage': {
                'data_ssd': {'uuid': 'ssd-uuid', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'hdd-uuid', 'fstype': 'ext4'},
            },
        }
        self.kept = self.ssd / 'installer' / 'releases' / 'v1' / 'config.json'
        self.own = top / 'project' / 'my-config.json'
        for path in (self.kept, self.own):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(self.config))
        self.control = top / 'control' / 'media-folders'
        self.control.mkdir(parents=True, mode=0o700)
        self.locks = top / 'locks'
        self.locks.mkdir()
        # What the apps' account could not do, by folder: {'read': .., 'write': ..}.
        self.refusals = {}
        self.told = []  # the apps' steps, as they were asked
        original = existing_media.check
        patches = [
            mock.patch.dict(
                os.environ, {'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'NEKO_STAND_IN': str(self.state_file)}
            ),
            mock.patch.object(existing_media, 'FSTAB', self.fstab),
            mock.patch.object(source_import, 'MOUNT_TABLE', self.table),
            mock.patch.object(existing_media, 'same_folder', self.same_folder),
            mock.patch.object(existing_media, 'check', functools.partial(self.checked, original)),
            mock.patch.object(media_locations, 'verify_storage', lambda config: None),
            mock.patch.object(media_locations, 'SETUP_LOCK', self.locks / 'setup.lock'),
            mock.patch.object(media_locations, 'OPERATION_LOCK', self.locks / 'operation.lock'),
        ]
        for patch in patches:
            patch.start()
            case.addCleanup(patch.stop)

    def save_state(self):
        self.state_file.write_text(json.dumps(self.state))

    def load_state(self):
        self.state = json.loads(self.state_file.read_text())
        return self.state

    def same_folder(self, point, path):
        return self.load_state()['bound'].get(str(point)) == str(path)

    def checked(self, original, config, category, folder, new_files=False, **how):
        """existing_media.check, with the apps' account answering from `refusals`."""

        def as_user(uid, gid, path, write=False):
            return self.refusals.get(str(path), {}).get('write' if write else 'read', '')

        return original(config, category, folder, new_files, as_user=as_user, **how)

    def steps(self):
        """setup's own steps for the folders, the apps' API steps as stand-ins."""

        def noted(name):
            return lambda config, *rest: self.told.append((name, existing_media.new_files_folder(config, 'movies')))

        return {
            **media_locations.STEPS,
            'jellyfin': noted('jellyfin'),
            'navidrome': noted('navidrome'),
            'root_folders': noted('root folders'),
            'seerr': noted('seerr'),
        }

    def ask(self, **request):
        """The control service's request (root's file, 0600), and one run of the unit."""
        path = self.control / 'request.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'id': '0123456789abcdef', **request}, stream)
        code = media_locations.run(self.kept, path, self.own, steps=self.steps(), lock=self.no_backup)
        self.case.assertEqual(code, 0)
        return json.loads((self.control / 'result.json').read_text())

    @contextlib.contextmanager
    def no_backup(self, config):
        yield

    def settings(self, which=None):
        return json.loads((which or self.kept).read_text())

    def calls(self):
        log = self.top / 'calls.log'
        return log.read_text().splitlines() if log.exists() else []

    def point(self, kind='movies', slot=None):
        return str(self.media / 'library' / 'existing' / (slot or kind))


def tree(folder):
    """Every name under a folder with its size, date and permissions."""
    return {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns, stat.S_IMODE(p.stat().st_mode))
        for p in [Path(folder), *sorted(Path(folder).rglob('*'))]
    }


class MediaFoldersTests(unittest.TestCase):
    def setUp(self):
        self.pc = PC(self)

    def add(self, folder=None, kind='movies', downloads=True, how='in_place'):
        return self.pc.ask(change='add', kind=kind, path=str(folder or self.pc.films), how=how, downloads=downloads)

    def kind(self, answer, kind='movies'):
        return next(k for k in answer['kinds'] if k['kind'] == kind)

    def test_a_folder_added_on_the_page_is_connected_as_setup_connects_it(self):
        before = tree(self.pc.nas)
        answer = self.add()
        self.assertEqual(answer['state'], 'done', answer)
        self.assertEqual(answer['id'], '0123456789abcdef')
        self.assertIn(f'Your films in {self.pc.films} are connected', answer['message'])
        self.assertIn('New films are saved there from now on', answer['message'])
        # Saved as setup saves the choice, in both copies of the settings.
        for which in (self.pc.kept, self.pc.own):
            saved = self.pc.settings(which)
            extra = saved['placement']['movies']['extra']
            self.assertEqual(
                [(e['path'], e['use'], e['new_files'], e['share']) for e in extra],
                [(str(self.pc.films), 'in_place', True, 'nas:/tank')],
            )
            self.assertIn('movies', saved[existing_page.ANSWERED])  # setup won't ask about films again
        # Bound into the library read-write (new files go there), now and at every start.
        fstab = self.pc.fstab.read_text()
        self.assertIn(existing_media.BEGIN, fstab)
        self.assertIn(f'{self.pc.films} {self.pc.point()} none bind,rw,nofail', fstab)
        self.assertEqual(self.pc.load_state()['bound'], {self.pc.point(): str(self.pc.films)})
        # The apps that see the folders were restarted, and told about the folder.
        calls = self.pc.calls()
        self.assertIn('systemctl daemon-reload', calls)
        restarted = sorted(c.split(' -p ')[1].split()[0] for c in calls if c.startswith('docker compose'))
        self.assertEqual(restarted, ['neko-jellyfin', 'neko-navidrome', 'neko-radarr'])
        self.assertEqual(
            self.pc.told, [(app, '/data/existing/movies') for app in ('jellyfin', 'root folders', 'seerr')]
        )
        # The page shows it ready, and the place for new films.
        films = self.kind(answer)
        self.assertEqual(films['target'], 'in_place')
        self.assertEqual(
            films['folders'],
            [{'use': 'in_place', 'path': str(self.pc.films), 'new_files': True, 'ready': True, 'reason': None}],
        )
        self.assertEqual(films['library'], str(self.pc.media / 'library' / 'movies'))
        self.assertEqual(films['room'], {'in_place': False, 'new_files': True})
        self.assertEqual(tree(self.pc.nas), before, 'nothing of the person was changed')
        self.assertFalse((self.pc.control / 'request.json').exists(), 'the request was taken')

    def test_a_folder_that_cannot_be_used_is_refused_in_its_own_words_and_nothing_changes(self):
        self.pc.refusals[str(self.pc.films)] = {'read': 'read Permission denied'}
        answer = self.add()
        self.assertEqual(answer['state'], 'refused')
        self.assertIn("The apps' account", answer['message'])
        self.assertIn('cannot read', answer['message'])
        self.assertIn('On the NAS, give this PC', answer['message'])
        self.assertNotIn('placement', self.pc.settings())
        self.assertNotIn(existing_media.BEGIN, self.pc.fstab.read_text())
        self.assertEqual(self.pc.calls(), [c for c in self.pc.calls() if c.startswith('findmnt')])
        # The page still shows the kinds as they are.
        self.assertEqual(self.kind(answer)['folders'], [])
        # A folder that is not there at all, and a request field that is not plain.
        missing = self.add(self.pc.top / 'nowhere')
        self.assertIn('is not a folder on this PC', missing['message'])
        odd = self.pc.ask(change='add', kind='movies', path='/mnt/../etc', how='in_place', downloads=False)
        self.assertEqual(odd['state'], 'refused')
        self.assertIn('full path', odd['message'])

    def test_a_share_that_does_not_answer_is_refused_without_waiting_for_it(self):
        # A share that stopped answering holds every look at its folders. Seen for real
        # (a FUSE share whose server never answered): the run waited in the kernel for
        # good, holding the lock the nightly backups take, and the page said "Working on
        # it" until the PC was restarted. Each look is asked of a child with a time limit.
        self.add(downloads=False)
        looked = []

        def no_answer(argv, seconds):
            looked.append((argv, seconds))
            return None  # the child did not end in time

        on_nas = functools.partial(lambda nas, path: str(path).startswith(str(nas)), self.pc.nas)
        with (
            mock.patch.object(media_locations, 'on_a_share', on_nas),
            mock.patch.object(media_locations, 'answered_within', no_answer),
        ):
            before = self.pc.settings()
            for asked in (
                {'change': 'add', 'kind': 'tv', 'path': str(self.pc.shows), 'how': 'in_place', 'downloads': False},
                {'change': 'add', 'kind': 'tv', 'path': str(self.pc.shows), 'how': 'copy', 'downloads': False},
                {'change': 'target', 'kind': 'movies', 'target': 'in_place'},
            ):
                with self.subTest(asked=asked):
                    answer = self.pc.ask(**asked)
                    self.assertEqual((answer['state'], answer['message']), ('refused', media_locations.AWAY))
                    self.assertEqual(self.pc.settings(), before)
        self.assertEqual(looked[-1], (['test', '-d', f'{self.pc.films}/.'], media_locations.LOOK_SECONDS))
        self.assertEqual(len(looked), 3)

    def test_one_folder_is_used_where_it_is_and_one_more_only_for_new_downloads(self):
        self.add(downloads=False)
        other = self.pc.top / 'big' / 'films'
        other.mkdir(parents=True)
        self.pc.state['mounts'][str(self.pc.top / 'big')] = ['/dev/sdd1', 'ext4']
        self.pc.save_state()
        with open(self.pc.fstab, 'a') as fstab:
            fstab.write(f'UUID=4 {self.pc.top / "big"} ext4 defaults,nofail 0 2\n')
        refused = self.add(other, downloads=False)
        self.assertEqual(refused['state'], 'refused')
        self.assertIn('NekoServer connects one folder of films where it is', refused['message'])
        added = self.add(other, downloads=True)
        self.assertEqual(added['state'], 'done', added)
        films = self.kind(added)
        self.assertEqual(
            [(f['use'], f['new_files']) for f in films['folders']], [('in_place', False), ('new_files', True)]
        )
        self.assertEqual(films['target'], 'new_files')
        self.assertEqual(self.pc.load_state()['bound'][self.pc.point(slot='movies-new')], str(other))
        self.assertIn(('jellyfin', '/data/existing/movies-new'), self.pc.told)
        full = self.add(self.pc.shows, downloads=True)
        self.assertIn('Take one of them away first', full['message'])

    def test_where_new_downloads_go_is_changed_and_the_folder_follows(self):
        self.add(downloads=True)
        to_library = self.pc.ask(change='target', kind='movies', target='library')
        self.assertEqual(to_library['state'], 'done', to_library)
        self.assertIn('saved in the NekoServer library', to_library['message'])
        self.assertFalse(self.pc.settings()['placement']['movies']['extra'][0]['new_files'])
        self.assertIn(f'{self.pc.films} {self.pc.point()} none bind,ro,nofail', self.pc.fstab.read_text())
        # Switched to read-only where it is connected, and the apps restarted to see it so.
        self.assertIn([self.pc.point(), 'remount,bind,ro'], self.pc.load_state()['remounts'])
        self.assertEqual(self.kind(to_library)['target'], 'library')
        self.assertEqual(self.pc.told[-2:], [('root folders', '/data/movies'), ('seerr', '/data/movies')])
        # Back: checked for writing as the apps' account first.
        self.pc.refusals[str(self.pc.films)] = {'write': 'write Read-only file system'}
        refused = self.pc.ask(change='target', kind='movies', target='in_place')
        self.assertEqual(refused['state'], 'refused')
        self.assertIn('read-only here', refused['message'])
        self.pc.refusals.clear()
        back = self.pc.ask(change='target', kind='movies', target='in_place')
        self.assertEqual(back['state'], 'done', back)
        self.assertEqual(self.kind(back)['target'], 'in_place')
        self.assertIn([self.pc.point(), 'remount,bind,rw'], self.pc.load_state()['remounts'])
        same = self.pc.ask(change='target', kind='movies', target='in_place')
        self.assertIn('already go there', same['message'])

    def test_a_folder_taken_away_is_disconnected_and_its_files_are_never_touched(self):
        self.add(downloads=True)
        before = tree(self.pc.nas)
        answer = self.pc.ask(change='remove', kind='movies', use='in_place')
        self.assertEqual(answer['state'], 'done', answer)
        self.assertIn('is taken away from your films', answer['message'])
        self.assertIn('were not touched: nothing was deleted', answer['message'])
        self.assertIn('New films are saved in the NekoServer library', ' '.join(answer['notes']))
        self.assertEqual(self.pc.settings()['placement']['movies']['extra'], [])
        self.assertEqual(self.pc.settings(self.pc.own)['placement']['movies']['extra'], [])
        # The last folder: NekoServer's lines go, and what they connected is disconnected.
        self.assertNotIn(existing_media.BEGIN, self.pc.fstab.read_text())
        self.assertIn(f'umount {self.pc.point()}', self.pc.calls())
        self.assertEqual(self.pc.load_state()['bound'], {})
        self.assertEqual(tree(self.pc.nas), before)
        self.assertEqual(self.kind(answer)['folders'], [])
        again = self.pc.ask(change='remove', kind='movies', use='in_place')
        self.assertIn('not connected for your films', again['message'])

    def test_a_folder_an_app_holds_open_says_a_restart_is_needed(self):
        self.add(downloads=True)
        self.pc.load_state()
        self.pc.state['busy'] = [self.pc.point()]
        self.pc.save_state()
        answer = self.pc.ask(change='remove', kind='movies', use='in_place')
        self.assertEqual(answer['state'], 'done')
        # Said where the page shows the next steps, not only in the log.
        (step,) = answer['next']
        self.assertIn('still connected', step)
        self.assertIn('Restart the PC', step)
        # Closed to new files meanwhile: nothing more is saved in a folder taken away.
        self.assertIn([self.pc.point(), 'remount,bind,ro'], self.pc.load_state()['remounts'])

    def test_a_second_change_while_one_is_at_work_is_already_working_on_it(self):
        with open(self.pc.control / 'lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            path = self.pc.control / 'request.json'
            path.write_text(json.dumps({'id': 'abcdef0123456789', 'change': 'look'}))
            path.chmod(0o600)
            self.assertEqual(media_locations.run(self.pc.kept, path, self.pc.own, steps=self.pc.steps()), 0)
        # The run at work answers; this one wrote nothing and left the request.
        self.assertFalse((self.pc.control / 'result.json').exists())
        self.assertTrue(path.exists())
        # Setup running: no change is made, and the page says why.
        with open(self.pc.locks / 'setup.lock', 'a') as setup:
            fcntl.flock(setup, fcntl.LOCK_EX)
            answer = self.add()
        self.assertEqual(answer['state'], 'refused')
        self.assertEqual(answer['message'], media_locations.SETUP_RUNNING)
        self.assertNotIn('placement', self.pc.settings())
        # A backup at work holds the apps: refused in the page's words.
        busy = mock.Mock(side_effect=RuntimeError('A NekoServer backup or monthly update is running right now'))
        path.write_text(
            json.dumps(
                {
                    'id': 'abcdef0123456789',
                    'change': 'add',
                    'kind': 'movies',
                    'path': str(self.pc.films),
                    'how': 'in_place',
                    'downloads': False,
                }
            )
        )
        media_locations.run(self.pc.kept, path, self.pc.own, steps=self.pc.steps(), lock=busy)
        said = json.loads((self.pc.control / 'result.json').read_text())
        self.assertEqual(said['message'], media_locations.BACKUP_RUNNING)

    def test_a_request_not_written_by_the_control_service_is_not_used(self):
        path = self.pc.control / 'request.json'
        path.write_text(json.dumps({'id': '0123456789abcdef', 'change': 'look'}))
        path.chmod(0o666)
        media_locations.run(self.pc.kept, path, self.pc.own, steps=self.pc.steps())
        said = json.loads((self.pc.control / 'result.json').read_text())
        self.assertEqual(said['state'], 'refused')
        self.assertIn('not written by the control service', said['message'])

    def test_a_look_shows_every_kind_with_its_library(self):
        answer = self.pc.ask(change='look')
        self.assertEqual(answer['state'], 'done')
        self.assertEqual([k['kind'] for k in answer['kinds']], ['movies', 'tv', 'music'])
        music = self.kind(answer, 'music')
        self.assertEqual(music['library'], str(self.pc.ssd / 'library' / 'music'))
        self.assertEqual((music['target'], music['downloads']), ('library', True))
        self.assertEqual(self.pc.told, [])  # nothing changed, so no app is told anything

    def test_copy_into_the_library_is_setups_copy_with_the_folder_kept(self):
        copied = []

        def copy_now(config, kind, path, **how):
            copied.append((kind, path))
            return {'status': 'copied', 'copied': 1}

        with mock.patch.object(existing_media, 'copy_now', copy_now):
            answer = self.add(self.pc.shows, kind='tv', downloads=False, how='copy')
        self.assertEqual(answer['state'], 'done', answer)
        self.assertIn('are copied into the library', answer['message'])
        self.assertEqual(copied, [('tv', str(self.pc.shows))])
        self.assertNotIn(existing_page.COPIES, self.pc.settings(), 'copied: off the list')
        self.assertNotIn(existing_media.BEGIN, self.pc.fstab.read_text(), 'a copy connects nothing')


if __name__ == '__main__':
    unittest.main()
