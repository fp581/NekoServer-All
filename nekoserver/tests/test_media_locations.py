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
        self.jobs = []  # the music folders the jobs' settings were brought up to date with
        original = existing_media.check
        patches = [
            mock.patch.dict(
                os.environ, {'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'NEKO_STAND_IN': str(self.state_file)}
            ),
            mock.patch.object(existing_media, 'FSTAB', self.fstab),
            mock.patch.object(source_import, 'MOUNT_TABLE', self.table),
            mock.patch.object(existing_media, 'same_folder', self.same_folder),
            mock.patch.object(existing_media, 'check', functools.partial(self.checked, original)),
            mock.patch.object(media_locations, 'verify_storage', return_value=None),
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
            'jobs': lambda config, out: self.jobs.append(existing_media.backed_up(config)),
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


class CopyProgressTests(unittest.TestCase):
    """The owner's decision 3 on PR 89: "Copy into the library" shows how far it is."""

    def test_the_import_says_files_and_bytes_done_after_each_file(self):
        with tempfile.TemporaryDirectory() as top:
            source, media = Path(top) / 'old-tv', Path(top) / 'hdd'
            (source / 'Show A').mkdir(parents=True)
            (source / 'Show A' / 'e1.mkv').write_bytes(b'x' * 300)
            (source / 'Show A' / 'e2.mkv').write_bytes(b'y' * 700)
            media.mkdir()
            told = []
            entry = {'type': 'local', 'path': str(source), 'category': 'tv'}
            result = source_import.copy_source(entry, media, apply=True, progress=lambda *now: told.append(now))
        self.assertEqual(result['copied'], 2)
        self.assertEqual(told[0], (0, 0, 2, 1000))
        self.assertEqual(told[-1], (2, 1000, 2, 1000))
        self.assertEqual(len(told), 3)

    def test_progress_is_written_beside_the_result_at_most_once_a_second(self):
        with tempfile.TemporaryDirectory() as folder:
            now, said = [100.0], []
            progress = media_locations.CopyProgress(folder, '0123456789abcdef', said.append, clock=lambda: now[0])
            told = progress('tv', '/mnt/nas/shows')
            self.assertEqual(said, [media_locations.COPYING.format(source='/mnt/nas/shows')])
            self.assertIn('Backups and monthly updates wait until the copy is done', said[0])
            path = Path(folder) / 'progress.json'
            told(0, 0, 10, 1000)
            written = json.loads(path.read_text())
            self.assertEqual(
                written,
                {
                    'format': 'neko-media-folders-v1',
                    'id': '0123456789abcdef',
                    'kind': 'tv',
                    'source': '/mnt/nas/shows',
                    'files_done': 0,
                    'files_total': 10,
                    'bytes_done': 0,
                    'bytes_total': 1000,
                },
            )
            told(1, 100, 10, 1000)  # within the second: not written
            self.assertEqual(json.loads(path.read_text())['files_done'], 0)
            now[0] += 1.5
            told(2, 200, 10, 1000)
            self.assertEqual(json.loads(path.read_text())['files_done'], 2)
            told(10, 1000, 10, 1000)  # the end is always written
            self.assertEqual(json.loads(path.read_text())['files_done'], 10)
            progress.done()
            self.assertFalse(path.exists())


class GuideTests(unittest.TestCase):
    def test_the_guides_say_the_limit_shared_json_sets(self):
        cap = existing_media.FOLDERS_PER_KIND
        for guide, said in (
            ('DASHBOARD.md', f'Each kind can have up to {cap} folders of yours connected'),
            ('STORAGE.md', f'Each kind (films, shows, music) can have up to {cap} such folders'),
            ('GUIDED-SETUP.md', f'up to {cap}, until you answer **That is all**'),
        ):
            self.assertIn(said, (ROOT / guide).read_text(encoding='utf-8'), guide)
        self.assertNotIn('one folder used where it is, and one more', (ROOT / 'DASHBOARD.md').read_text())


class RequestPathTests(unittest.TestCase):
    def test_only_a_json_request_is_read_and_its_folder_is_where_it_really_is(self):
        """SonarCloud on PR 89: the request's path comes from the command line, and the
        lock and the result are written beside it."""
        with self.assertRaises(SystemExit) as caught:
            media_locations.run('/nowhere/my-config.json', '/etc/passwd')
        self.assertIn('must be a .json file', str(caught.exception))


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
        self.assertEqual(films['target'], 'movies')
        self.assertEqual(
            films['folders'],
            [
                {
                    'slot': 'movies',
                    'use': 'in_place',
                    'path': str(self.pc.films),
                    'new_files': True,
                    'ready': True,
                    'reason': None,
                    'backup': None,
                    'backup_said': None,
                }
            ],
        )
        self.assertEqual(films['library'], str(self.pc.media / 'library' / 'movies'))
        self.assertEqual((films['room'], films['full']), (existing_media.FOLDERS_PER_KIND - 1, None))
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

    def test_settings_that_could_not_be_saved_in_one_copy_are_changed_in_neither(self):
        """Review of 0.52: setup's own copy was saved first; when the copy on the SSD then
        could not be, the page said "nothing was changed", and the next setup run made the
        change from the copy that had it."""
        import installer

        saved = installer.atomic_json
        before = {which: which.read_bytes() for which in (self.pc.kept, self.pc.own)}

        def ssd_full(path, payload):
            if Path(path) == self.pc.kept:
                raise OSError(28, 'No space left on device')
            saved(path, payload)

        with mock.patch.object(installer, 'atomic_json', ssd_full):
            answer = self.add()
        self.assertEqual(answer['state'], 'refused', answer)
        self.assertIn('nothing was changed', answer['message'])
        for which, content in before.items():
            self.assertEqual(which.read_bytes(), content, which)

    def test_a_folder_inside_or_around_a_connected_one_is_refused(self):
        # Deep check 836: the NAS share connected after its films folder (or a film's
        # folder after the films) was bound on its own too, so every film showed twice.
        self.add(downloads=False)
        before = self.pc.settings()
        words = {'other': str(self.pc.films), 'other_words': 'films'}
        for path, kind, said in (
            (self.pc.nas, 'movies', media_locations.HAS_INSIDE),
            (self.pc.films / 'Alien (1979)', 'movies', media_locations.INSIDE),
            (self.pc.films / 'Alien (1979)', 'tv', media_locations.INSIDE),
            (self.pc.films, 'tv', media_locations.OTHER_KIND),
        ):
            answer = self.add(path, kind=kind, downloads=False)
            wanted = said.format(path=path, words=media_locations.WORDS[kind], **words)
            self.assertEqual((answer['state'], answer['message']), ('refused', wanted), path)
            self.assertEqual(self.pc.settings(), before, path)
        # A folder beside it is fine.
        self.assertEqual(self.add(self.pc.shows, kind='tv', downloads=False)['state'], 'done')

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
                {'change': 'target', 'kind': 'movies', 'target': 'movies'},
            ):
                with self.subTest(asked=asked):
                    answer = self.pc.ask(**asked)
                    self.assertEqual((answer['state'], answer['message']), ('refused', media_locations.AWAY))
                    self.assertEqual(self.pc.settings(), before)
        self.assertEqual(looked[-1], (['test', '-d', f'{self.pc.films}/.'], media_locations.LOOK_SECONDS))
        self.assertEqual(len(looked), 3)

    def big_drive(self, *names):
        """Folders of films on another drive of this PC (mounted, in /etc/fstab)."""
        big = self.pc.top / 'big'
        if str(big) not in self.pc.load_state()['mounts']:
            self.pc.state['mounts'][str(big)] = ['/dev/sdd1', 'ext4']
            self.pc.save_state()
            with open(self.pc.fstab, 'a') as fstab:
                fstab.write(f'UUID=4 {big} ext4 defaults,nofail 0 2\n')
        for name in names:
            (big / name).mkdir(parents=True)
        return [big / name for name in names]

    def settings_of_0_51_3(self):
        """The films used in place (read-only) and a folder only for new films, saved as
        0.51.3 saved them (no "slot"), and connected. Returns that folder."""
        (new,) = self.big_drive('new-films')
        self.add(downloads=False)
        old = {'mount': str(self.pc.top / 'big'), 'share': '/dev/sdd1', 'fstype': 'ext4'}
        for which in (self.pc.kept, self.pc.own):
            saved = self.pc.settings(which)
            films = saved['placement']['movies']['extra']
            films[0].pop('slot')
            films.append({'path': str(new), 'holds': 'movies', 'use': 'new_files', 'new_files': True, **old})
            which.write_text(json.dumps(saved))
        return new

    def test_settings_saved_by_0_51_3_are_shown_and_kept_as_they_were(self):
        # 0.51.3 saved one folder used where it is and one only for new downloads, with no
        # "slot": they are where 0.51.3 put them, and a third folder changes neither.
        new = self.settings_of_0_51_3()
        before = self.pc.settings()
        look = self.pc.ask(change='look')
        self.assertEqual(
            [(f['slot'], f['use'], f['new_files']) for f in self.kind(look)['folders']],
            [('movies', 'in_place', False), ('movies-new', 'new_files', True)],
        )
        self.assertEqual(self.kind(look)['target'], 'movies-new')
        self.assertEqual(self.pc.settings(), before, 'a look changes nothing')
        (third,) = self.big_drive('more-films')
        added = self.add(third, downloads=False)
        self.assertEqual(added['state'], 'done', added)
        extra = self.pc.settings()['placement']['movies']['extra']
        self.assertEqual(extra[:2], before['placement']['movies']['extra'], 'the two of 0.51.3 are as they were')
        self.assertEqual(extra[2]['slot'], 'movies-2')
        self.assertEqual(self.pc.load_state()['bound'][self.pc.point(slot='movies-2')], str(third))
        fstab = self.pc.fstab.read_text()
        self.assertIn(f'{self.pc.films} {self.pc.point()} none bind,ro,nofail', fstab)
        self.assertIn(f'{new} {self.pc.point(slot="movies-new")} none bind,rw,nofail', fstab)

    def test_a_folder_only_for_new_downloads_stays_connected_when_they_go_elsewhere(self):
        # The owner's decision 2 on PR 89: 0.51 disconnected it, so the films the apps had
        # saved there left the library. It stays, used where it is and read-only.
        new = self.settings_of_0_51_3()
        self.pc.load_state()['bound'][self.pc.point(slot='movies-new')] = str(new)  # connected by 0.51.3
        self.pc.save_state()
        to_library = self.pc.ask(change='target', kind='movies', target='library')
        self.assertEqual(to_library['state'], 'done', to_library)
        films = self.kind(to_library)
        self.assertEqual(
            [(f['slot'], f['use'], f['new_files']) for f in films['folders']],
            [('movies', 'in_place', False), ('movies-new', 'in_place', False)],
        )
        self.assertEqual(films['target'], 'library')
        stays = existing_media.STAYS.format(path=new, what='movies')
        self.assertIn(stays, to_library['notes'])
        self.assertIn(f'{new} {self.pc.point(slot="movies-new")} none bind,ro,nofail', self.pc.fstab.read_text())
        self.assertEqual(self.pc.load_state()['bound'][self.pc.point(slot='movies-new')], str(new))
        self.assertNotIn(f'umount {self.pc.point(slot="movies-new")}', self.pc.calls())
        self.assertIn([self.pc.point(slot='movies-new'), 'remount,bind,ro'], self.pc.load_state()['remounts'])
        # It is an ordinary folder now: new downloads can be sent back to it, or taken away.
        back = self.pc.ask(change='target', kind='movies', target='movies-new')
        self.assertEqual(back['state'], 'done', back)
        self.assertEqual(self.kind(back)['target'], 'movies-new')

    def test_a_folder_only_for_new_downloads_stays_when_another_folder_takes_them(self):
        new = self.settings_of_0_51_3()
        moved = self.pc.ask(change='target', kind='movies', target='movies')
        self.assertEqual(moved['state'], 'done', moved)
        self.assertEqual(
            [(f['slot'], f['use'], f['new_files']) for f in self.kind(moved)['folders']],
            [('movies', 'in_place', True), ('movies-new', 'in_place', False)],
        )
        self.assertIn(existing_media.STAYS.format(path=new, what='movies'), moved['notes'])
        # A folder added with new downloads does the same.
        (more,) = self.big_drive('more-films')
        self.pc.ask(change='target', kind='movies', target='library')
        added = self.add(more, downloads=True)
        self.assertEqual([f['new_files'] for f in self.kind(added)['folders']], [False, False, True])

    def test_music_on_a_nas_is_asked_about_the_backup_as_setup_asks_and_saved_the_same(self):
        # The owner's decision 4 on PR 89: before, music added on the page was never
        # backed up, and setup's question was not asked.
        music = self.pc.nas / 'music'
        look = self.pc.ask(change='look')
        self.assertEqual(self.kind(look, 'music')['backup_question'], existing_page.backup_question())
        self.assertIsNone(self.kind(look)['backup_question'])
        no = self.pc.ask(change='add', kind='music', path=str(music), how='in_place', downloads=False, backup=False)
        self.assertEqual(no['state'], 'done', no)
        (entry,) = self.pc.settings()['placement']['music']['extra']
        self.assertIs(entry['backup'], False)
        self.assertIn('Your music: not backed up by NekoServer.', no['notes'])
        self.assertEqual(self.kind(no, 'music')['folders'][0]['backup_said'], 'not backed up by NekoServer.')
        self.assertEqual(self.pc.jobs[-1], [])
        self.pc.ask(change='remove', kind='music', slot='music')
        # Yes (as Enter in setup): saved as setup saves it, and the music backup's own
        # settings are brought up to date to copy it from the next night.
        yes = self.pc.ask(change='add', kind='music', path=str(music), how='in_place', downloads=False, backup=True)
        (entry,) = self.pc.settings()['placement']['music']['extra']
        self.assertIs(entry['backup'], True)
        self.assertIn('Your music: copied each night with the music backup.', yes['notes'])
        self.assertEqual([f['slot'] for f in self.pc.jobs[-1]], ['music'])
        # A music folder on a drive of this PC is not a NAS's: not asked, not backed up.
        (local,) = self.big_drive('music')
        self.pc.ask(change='add', kind='music', path=str(local), how='in_place', downloads=False, backup=True)
        self.assertNotIn('backup', self.pc.settings()['placement']['music']['extra'][1])
        # A changed "where new downloads go" keeps the answer.
        self.pc.ask(change='target', kind='music', target='music')
        self.assertIs(self.pc.settings()['placement']['music']['extra'][0]['backup'], True)

    def test_the_jobs_settings_are_brought_up_to_date_only_where_installed(self):
        import native_runtime
        from modules import app_backup, host_backup, server_os

        top = self.pc.top / 'etc'
        storage, drives, hdd = top / 'storage.json', top / 'drives.json', top / 'hdd.json'
        top.mkdir()
        storage.write_text('{}')
        config = self.pc.config
        out = {'log': []}
        with (
            mock.patch.object(native_runtime, 'BACKUP_GUARD', storage),
            mock.patch.object(server_os, 'DRIVE_SETTINGS', drives),
            mock.patch.object(server_os, 'HDD_GUARD', hdd),
            mock.patch.object(app_backup, 'refresh') as apps,
            mock.patch.object(host_backup, 'refresh') as host,
        ):
            media_locations.jobs(config, out)
        self.assertEqual(json.loads(storage.read_text()), native_runtime.storage_config(config))
        self.assertFalse(drives.exists() or hdd.exists(), 'a job that is not installed gets no settings')
        apps.assert_called_once()
        host.assert_called_once()
        self.assertIn('Backups: settings brought up to date', out['log'])

    def test_the_hourly_media_check_gets_the_new_folders_at_once(self):
        """Review of 0.52: the hourly check of the apps kept its own copy of the settings
        until setup ran again, so a download folder changed on the page (Seerr's, say)
        was reported as wrong though it was right."""
        import native_runtime
        from modules import app_backup, host_backup, server_os

        config = self.pc.config
        monitor = Path(config['data_mount']) / 'scripts' / 'config.json'
        monitor.parent.mkdir(parents=True, exist_ok=True)
        monitor.write_text('{"placement": "as it was before the change"}')  # the check is installed
        out = {'log': []}
        missing = self.pc.top / 'not-installed.json'
        with (
            mock.patch.object(native_runtime, 'BACKUP_GUARD', missing),
            mock.patch.object(server_os, 'DRIVE_SETTINGS', missing),
            mock.patch.object(server_os, 'HDD_GUARD', missing),
            mock.patch.object(app_backup, 'refresh'),
            mock.patch.object(host_backup, 'refresh'),
        ):
            media_locations.jobs(config, out)
        self.assertEqual(json.loads(monitor.read_text()), config)
        self.assertIn('Media monitor: settings brought up to date', out['log'])

    def test_a_change_that_half_fails_names_the_step_and_what_to_do(self):
        # The owner's decision 5 on PR 89: it said only "Run setup again" (in a note), and
        # a mount step that failed after the settings were saved said "Not changed".
        def broken(why):
            def step(*args):
                raise RuntimeError(why)

            return step

        steps = self.pc.steps()
        self.pc.steps = lambda: {**steps, 'jellyfin': broken('Jellyfin did not answer (HTTP 503).')}
        answer = self.add(downloads=False)
        self.assertEqual(answer['state'], 'partial', answer)
        self.assertIn('are connected, used where they are', answer['message'])
        self.assertIn(media_locations.PARTLY, answer['message'])
        self.assertEqual(
            answer['next'],
            [
                "Jellyfin's library was not updated: Jellyfin did not answer (HTTP 503). Check that Jellyfin runs "
                '(Your apps), then run setup again.'
            ],
        )
        self.assertEqual(answer['failed'], ['jellyfin'])
        # The others were still told, and nothing was undone.
        self.assertEqual([app for app, _ in self.pc.told], ['root folders', 'seerr'])
        self.assertEqual(len(self.pc.settings()['placement']['movies']['extra']), 1)
        # The folders not connected: said as such, never as "Not changed".
        self.pc.steps = lambda: {**steps, 'mount': broken('mount: /x: special device does not exist')}
        (other,) = self.big_drive('other-films')
        mounted = self.add(other, downloads=False)
        self.assertEqual(mounted['state'], 'partial', mounted)
        self.assertIn('Your folders were not connected to the library: mount: /x', mounted['next'][0])
        self.assertIn('Run setup again to connect them', mounted['next'][0])
        self.assertEqual(len(self.pc.settings()['placement']['movies']['extra']), 2)

    def test_the_apps_that_answer_a_failure_are_named_with_their_reason(self):
        out = {'next': [], 'failed': []}
        rows = [
            {'check': 'jellyfin:existing:movies', 'status': 'fail', 'reason': 'Library API/path check failed'},
            {'check': 'jellyfin:existing:tv', 'status': 'needs_setup', 'reason': 'Remove "Shows (your folder)"'},
        ]
        media_locations.app_rows(rows, out, 'Jellyfin', 'jellyfin')
        self.assertEqual(
            out['next'],
            [
                "Jellyfin's library was not updated: Library API/path check failed. Check that Jellyfin runs "
                '(Your apps), then run setup again.',
                'Jellyfin: Remove "Shows (your folder)"',
            ],
        )
        done = mock.Mock(returncode=1, stdout='radarr: root folder incomplete (Radarr did not answer)\n')
        media_locations.root_folders({}, '/x/my-config.json', out, run=lambda *a, **k: done)
        self.assertIn(
            'The folder for new downloads in Sonarr, Radarr or Lidarr was not updated: radarr: root folder '
            'incomplete (Radarr did not answer). Check that they run',
            out['next'][-1],
        )
        self.assertEqual(out['failed'], ['jellyfin', 'root folders'])
        self.assertEqual(
            set(media_locations.STEP_FAILED), {*media_locations.STEPS} - {'root_folders'} | {'root folders'}
        )

    def test_a_kind_takes_more_folders_used_where_they_are_up_to_its_limit(self):
        self.add(downloads=False)
        cap = existing_media.FOLDERS_PER_KIND
        others = self.big_drive(*(f'films-{n}' for n in range(2, cap + 2)))
        added = self.add(others[0], downloads=False)
        self.assertEqual(added['state'], 'done', added)
        films = self.kind(added)
        self.assertEqual(
            [(f['slot'], f['use'], f['new_files']) for f in films['folders']],
            [('movies', 'in_place', False), ('movies-2', 'in_place', False)],
        )
        self.assertEqual(self.pc.load_state()['bound'][self.pc.point(slot='movies-2')], str(others[0]))
        self.assertIn(f'{others[0]} {self.pc.point(slot="movies-2")} none bind,ro,nofail', self.pc.fstab.read_text())
        # New downloads to the second: the first stays as it is, read-only.
        downloads = self.pc.ask(change='target', kind='movies', target='movies-2')
        self.assertEqual(downloads['state'], 'done', downloads)
        self.assertEqual(self.kind(downloads)['target'], 'movies-2')
        self.assertIn(('root folders', '/data/existing/movies-2'), self.pc.told)
        for other in others[1 : cap - 1]:
            self.assertEqual(self.add(other, downloads=False)['state'], 'done')
        films = self.kind(self.pc.ask(change='look'))
        self.assertEqual((len(films['folders']), films['room']), (cap, 0))
        self.assertEqual(films['full'], existing_media.full_words('movies'))
        # One more is refused in the words setup says too, and nothing changes.
        before = self.pc.settings()
        full = self.add(others[-1], downloads=False)
        self.assertEqual((full['state'], full['message']), ('refused', existing_media.full_words('movies')))
        self.assertEqual(self.pc.settings(), before)
        # Taking one away moves no other folder: each keeps its slot, and its place in the apps.
        taken = self.pc.ask(change='remove', kind='movies', slot='movies')
        self.assertEqual(taken['state'], 'done', taken)
        slots = [f['slot'] for f in self.kind(taken)['folders']]
        self.assertEqual(slots, [f'movies-{n}' for n in range(2, cap + 1)])
        self.assertEqual(self.kind(taken)['target'], 'movies-2')

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
        refused = self.pc.ask(change='target', kind='movies', target='movies')
        self.assertEqual(refused['state'], 'refused')
        self.assertIn('read-only here', refused['message'])
        self.pc.refusals.clear()
        back = self.pc.ask(change='target', kind='movies', target='movies')
        self.assertEqual(back['state'], 'done', back)
        self.assertEqual(self.kind(back)['target'], 'movies')
        self.assertIn([self.pc.point(), 'remount,bind,rw'], self.pc.load_state()['remounts'])
        same = self.pc.ask(change='target', kind='movies', target='movies')
        self.assertIn('already go there', same['message'])

    def test_downloads_sent_elsewhere_say_what_the_apps_already_have_keeps_its_folder(self):
        # Deep checks 831-832: downloads moved to a new HDD because the old one was full.
        # The page said "New shows are saved in ... now", and Sonarr went on saving each
        # new episode of a show it had into the show's old folder.
        added = self.add(self.pc.shows, kind='tv', downloads=True)
        self.assertEqual(added['state'], 'done', added)
        moved = [n for n in added['notes'] if n.startswith('Sonarr keeps')]
        self.assertEqual(
            moved,
            [
                'Sonarr keeps the shows it has already in their folders in /data/tv: a new episode of a show you '
                'follow still goes there. To move them to /data/existing/tv too, open Sonarr, press Select, tick '
                'them, press Edit, choose /data/existing/tv as the Root Folder and answer yes to moving the files.'
            ],
        )
        # Back to the library: the folder they went to is read-only now, and that is said.
        back = self.pc.ask(change='target', kind='tv', target='library')
        (said,) = [n for n in back['notes'] if n.startswith('Sonarr keeps')]
        self.assertIn('in /data/existing/tv: a new episode', said)
        self.assertIn('but that folder is read-only now, so Sonarr cannot save it there until they are moved', said)
        self.assertIn('choose /data/tv as the Root Folder', said)
        # A folder connected without new downloads changes nothing in Radarr: nothing said.
        films = self.add(downloads=False)
        self.assertFalse([n for n in films['notes'] if 'keeps the' in n], films['notes'])

    def test_a_page_left_open_on_another_device_never_changes_the_folder_added_since(self):
        # Deep check 842: the films folder was taken away on a phone and a new drive's
        # films added, which got the same slot; the laptop, still showing the old one,
        # pressed "Take away" on it and disconnected the new drive's films instead.
        self.add(downloads=False)
        self.pc.ask(change='remove', kind='movies', slot='movies')
        (new,) = self.big_drive('films')
        self.assertEqual(self.add(new, downloads=False)['state'], 'done')
        before = self.pc.settings()
        for request in (
            {'change': 'remove', 'kind': 'movies', 'slot': 'movies', 'path': str(self.pc.films)},
            {'change': 'target', 'kind': 'movies', 'target': 'movies', 'path': str(self.pc.films)},
        ):
            stale = self.pc.ask(**request)
            self.assertEqual(stale['state'], 'refused', stale)
            self.assertEqual(stale['message'], media_locations.CHANGED.format(path=self.pc.films))
            self.assertEqual(self.pc.settings(), before)
        # The folder the page shows is the one changed.
        taken = self.pc.ask(change='remove', kind='movies', slot='movies', path=str(new))
        self.assertEqual(taken['state'], 'done', taken)
        odd = self.pc.ask(change='remove', kind='movies', slot='movies', path='/mnt/../etc')
        self.assertIn('Which folder?', odd['message'])

    def test_a_folder_taken_away_is_disconnected_and_its_files_are_never_touched(self):
        self.add(downloads=True)
        before = tree(self.pc.nas)
        answer = self.pc.ask(change='remove', kind='movies', slot='movies')
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
        again = self.pc.ask(change='remove', kind='movies', slot='movies')
        self.assertIn('not connected for your films', again['message'])

    def test_a_folder_an_app_holds_open_says_a_restart_is_needed(self):
        self.add(downloads=True)
        self.pc.load_state()
        self.pc.state['busy'] = [self.pc.point()]
        self.pc.save_state()
        answer = self.pc.ask(change='remove', kind='movies', slot='movies')
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

    def test_the_apps_are_restarted_only_when_a_folder_they_see_changed(self):
        # Seen with real mounts: shows copied into the library restarted every media app
        # (Jellyfin in the middle of a film), because the films folder, left as it was,
        # was remounted as it already was and that counted as a change.
        self.add(downloads=True)
        restarts = [c for c in self.pc.calls() if c.startswith('docker compose')]
        self.assertTrue(restarts, 'connecting a folder restarts the apps that see it')
        with mock.patch.object(existing_media, 'copy_now', lambda *a, **k: {'status': 'copied', 'copied': 1}):
            answer = self.add(self.pc.shows, kind='tv', downloads=False, how='copy')
        self.assertEqual(answer['state'], 'done', answer)
        self.assertIn(f'mount -o remount,bind,rw {self.pc.point()}', self.pc.calls())
        self.assertEqual([c for c in self.pc.calls() if c.startswith('docker compose')], restarts)
        # Switched to read-only is a change the apps must be restarted for.
        self.pc.ask(change='target', kind='movies', target='library')
        self.assertGreater(len([c for c in self.pc.calls() if c.startswith('docker compose')]), len(restarts))

    def test_an_app_that_could_not_be_restarted_is_said_not_done(self):
        """Review of 0.52: a folder switched to read-only stays writable for an app that
        runs until it is restarted; a restart that failed was only a line in the log, and
        the page said Done."""
        from modules import media

        def refused(command, **_):
            raise RuntimeError('docker: the daemon did not answer')

        def restart_refused(found, failed, log):
            return media.restart_for_own_folders(found, failed, log, refused)

        own = self.pc.steps

        def steps():
            return {**own(), 'restart': restart_refused}

        with mock.patch.object(self.pc, 'steps', steps):
            answer = self.add(downloads=False)
        self.assertEqual(answer['state'], 'partial', answer)
        self.assertIn('restart', answer['failed'])
        said = ' '.join(answer['next'])
        self.assertIn('were not restarted', said)
        self.assertIn('the daemon did not answer', said)
        self.assertIn('Restart the server, or run setup again.', said)

    def test_copy_into_the_library_is_setups_copy_with_the_folder_kept(self):
        copied, seen = [], []

        def copy_now(config, kind, path, **how):
            copied.append((kind, path))
            # While it copies: how far it is beside the result, and the page told backups wait.
            how['progress'](1, 4, 2, 10)
            control = self.pc.control
            seen.append(
                (json.loads((control / 'progress.json').read_text()), json.loads((control / 'result.json').read_text()))
            )
            return {'status': 'copied', 'copied': 1}

        with mock.patch.object(existing_media, 'copy_now', copy_now):
            answer = self.add(self.pc.shows, kind='tv', downloads=False, how='copy')
        self.assertEqual(answer['state'], 'done', answer)
        self.assertIn('are copied into the library', answer['message'])
        self.assertEqual(copied, [('tv', str(self.pc.shows))])
        ((progress, result),) = seen
        self.assertEqual((progress['id'], progress['files_done'], progress['bytes_total']), (answer['id'], 1, 10))
        self.assertEqual(result['state'], 'working')
        self.assertIn('Backups and monthly updates wait until the copy is done', result['message'])
        self.assertFalse((self.pc.control / 'progress.json').exists(), 'gone once the copy ended')
        self.assertNotIn(existing_page.COPIES, self.pc.settings(), 'copied: off the list')
        self.assertNotIn(existing_media.BEGIN, self.pc.fstab.read_text(), 'a copy connects nothing')


if __name__ == '__main__':
    unittest.main()
