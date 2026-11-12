"""An installation drive for a group (0.50.0): the apps of Photos, Movies and shows, Music
or Nextcloud keep their settings and databases on another drive than the apps' own. The
folders stay where they are for every other part of NekoServer: each app's folder on its
drive is mounted at its usual place."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import native_runtime  # noqa: E402
import neko_common  # noqa: E402
import placement  # noqa: E402
from modules import docker, storage  # noqa: E402

DRIVES = {
    'data_ssd': {'uuid': 'ssd-00001', 'fstype': 'ext4'},
    'media_hdd': {'uuid': 'hdd-00001', 'fstype': 'ext4'},
    'd3': {'uuid': 'fast-0003', 'fstype': 'ext4'},
}


def settings(**more):
    return {
        'data_mount': '/srv/nekoserver',
        'media_mount': '/mnt/nekoserver-media',
        'storage': {key: dict(value) for key, value in DRIVES.items()},
        **more,
    }


class TableTests(unittest.TestCase):
    def test_a_group_placed_on_another_drive_takes_its_apps_there(self):
        config = settings(install={'photos': 'd3'})
        self.assertEqual(placement.install_of(config, 'photos'), 'd3')
        self.assertEqual(placement.install_of(config, 'movies'), 'ssd')
        self.assertEqual(placement.apps_elsewhere(config), {'immich': 'd3'})
        self.assertEqual(placement.table(config)['photos']['install'], 'd3')
        # The drive is one setup mounts and every job checks.
        self.assertEqual(neko_common.drives_in_use(config), ['d3'])
        # Movies and shows takes the apps that find and download with it.
        elsewhere = placement.apps_elsewhere(settings(install={'movies': 'd3'}))
        self.assertEqual(sorted(elsewhere), ['jellyfin', 'prowlarr', 'qbittorrent', 'radarr', 'seerr', 'sonarr'])
        self.assertEqual(sorted(placement.apps_elsewhere(settings(install={'music': 'd3'}))), ['lidarr', 'navidrome'])

    def test_nothing_says_so_on_every_other_server(self):
        config = settings()
        self.assertEqual(placement.apps_elsewhere(config), {})
        self.assertEqual(neko_common.drives_in_use(config), [])
        self.assertNotIn('installed', native_runtime.storage_config(config))
        self.assertEqual(placement.apps_elsewhere(settings(install={'photos': 'ssd'})), {})
        # A group that is switched off has no apps anywhere.
        off = settings(install={'photos': 'd3'}, media_apps={'photos': False})
        self.assertEqual(placement.apps_elsewhere(off), {})

    def test_the_settings_file_is_not_trusted(self):
        for install in ({'photos': 'd9'}, {'photos': 'media'}, {'core': 'd3'}, {'photos': ['d3']}, {'films': 'd3'}):
            with self.subTest(install=install), self.assertRaisesRegex(RuntimeError, 'under "install"'):
                placement.apps_elsewhere(settings(install=install))
        # Something that is no table at all places nothing.
        self.assertEqual(placement.apps_elsewhere(settings(install='d3')), {})

    def test_the_jobs_are_told_which_apps_and_which_drive(self):
        config = settings(install={'cloud': 'd3'})
        self.assertEqual(native_runtime.storage_config(config)['installed'], {'nextcloud': 'd3'})


class FstabTests(unittest.TestCase):
    def test_the_line_waits_for_both_drives_and_never_stops_the_start(self):
        binds = storage.installed_binds(settings(install={'photos': 'd3'}))
        self.assertEqual(
            [(app, str(source), str(target), str(drive)) for app, source, target, drive in binds],
            [
                (
                    'immich',
                    '/mnt/nekoserver-drives/d3/nekoserver/appdata/immich',
                    '/srv/nekoserver/appdata/immich',
                    '/mnt/nekoserver-drives/d3',
                )
            ],
        )
        _, source, target, drive = binds[0]
        self.assertEqual(
            storage.installed_line(source, target, drive, Path('/srv/nekoserver')),
            '/mnt/nekoserver-drives/d3/nekoserver/appdata/immich /srv/nekoserver/appdata/immich none '
            'bind,nofail,x-systemd.requires-mounts-for=/mnt/nekoserver-drives/d3,'
            'x-systemd.requires-mounts-for=/srv/nekoserver 0 0',
        )
        with self.assertRaisesRegex(RuntimeError, 'spaces'):
            storage.installed_line(Path('/mnt/a b'), target, drive, Path('/srv/nekoserver'))

    def test_the_block_is_setups_own_and_the_rest_of_the_file_is_as_it_was(self):
        theirs = 'UUID=abc / ext4 defaults 0 1\n# a note of the person\n'
        begin, end = storage.INSTALL_BEGIN, storage.INSTALL_END
        once = storage.with_block(theirs, begin, end, ['line one', 'line two'])
        self.assertEqual(once, theirs + f'{begin}\nline one\nline two\n{end}\n')
        self.assertEqual(storage.with_block(once, begin, end, ['line one', 'line two']), once)  # no change, no growth
        self.assertEqual(storage.with_block(once, begin, end, ['other']), theirs + f'{begin}\nother\n{end}\n')
        self.assertEqual(storage.with_block(once, begin, end, []), theirs)
        with self.assertRaisesRegex(RuntimeError, 'without its end line'):
            storage.with_block(theirs + begin + '\nline\n', begin, end, [])


class MountTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.data, self.drive = self.root / 'apps', self.root / 'd3'
        self.target = self.data / 'appdata' / 'immich'
        self.source = self.drive / 'nekoserver' / 'appdata' / 'immich'
        self.fstab = self.root / 'fstab'
        self.fstab.write_text('UUID=abc / ext4 defaults 0 1\n')
        self.logs, self.calls, self.mounted = [], [], set()

    def mount(self, *more):
        binds = [('immich', self.source, self.target, self.drive)]
        context = SimpleNamespace(config=settings(data_mount=str(self.data)), log=self.logs.append)

        def call(argv, timeout=30):
            self.calls.append(argv)
            if argv[0] == 'mount':
                self.mounted.add(argv[1])
            return ''

        with (
            mock.patch.object(storage, 'installed_binds', return_value=binds),
            mock.patch.object(storage, 'call', side_effect=call),
            mock.patch.object(storage, 'on_its_drive', side_effect=lambda target, drive: str(target) in self.mounted),
            mock.patch.object(storage, 'unwritable') as closed,
        ):
            storage.mount_installed(context, self.fstab)
        return closed

    def test_the_folder_is_made_on_the_drive_closed_underneath_and_mounted(self):
        closed = self.mount()
        self.assertTrue(self.source.is_dir() and self.target.is_dir())
        closed.assert_called_once_with(self.target)  # before the mount: nothing is written there without the drive
        self.assertEqual(self.calls, [['systemctl', 'daemon-reload'], ['mount', str(self.target)]])
        text = self.fstab.read_text()
        self.assertTrue(text.startswith('UUID=abc / ext4 defaults 0 1\n' + storage.INSTALL_BEGIN))
        self.assertIn(f'{self.source} {self.target} none bind,nofail,', text)
        self.assertIn('immich: its settings are kept on the drive at', self.logs[0])
        # Again: nothing is written, mounted or said twice.
        self.calls.clear()
        self.mount().assert_not_called()
        self.assertEqual((self.calls, self.fstab.read_text()), ([], text))

    def test_an_app_that_is_set_up_on_the_apps_drive_is_never_moved(self):
        self.target.mkdir(parents=True)
        (self.target / 'library.db').write_text('x')
        with self.assertRaisesRegex(RuntimeError, 'immich already keeps its settings on the drive of the apps'):
            self.mount()
        self.assertEqual(self.calls, [])
        self.assertNotIn(storage.INSTALL_BEGIN, self.fstab.read_text())
        self.assertFalse(self.source.exists())

    def test_a_server_with_nothing_placed_touches_nothing(self):
        context = SimpleNamespace(config=settings(), log=self.logs.append)
        before = self.fstab.read_text()
        with mock.patch.object(storage, 'call', side_effect=AssertionError('ran')):
            storage.mount_installed(context, self.fstab)
        self.assertEqual(self.fstab.read_text(), before)

    def test_closed_means_immutable_or_nobodys(self):
        folder = self.root / 'under'
        folder.mkdir()
        ran = mock.Mock(return_value=SimpleNamespace(returncode=0))
        with mock.patch.object(storage.os, 'chmod') as chmod:
            storage.unwritable(folder, ran)
        self.assertEqual(ran.call_args[0][0], ['chattr', '+i', str(folder)])
        chmod.assert_not_called()
        # A filesystem that has no such mark: closed by its mode instead.
        refused = mock.Mock(return_value=SimpleNamespace(returncode=1))
        with mock.patch.object(storage.os, 'chmod') as chmod, mock.patch.object(storage.os, 'chown') as chown:
            storage.unwritable(folder, refused)
        chmod.assert_called_once_with(folder, 0o000)
        chown.assert_called_once_with(folder, 0, 0, follow_symlinks=False)


class DockerTests(unittest.TestCase):
    def test_docker_starts_after_those_folders_and_does_not_need_them(self):
        def escaped(argv):
            return argv[-1].strip('/').replace('/', '-') + '.mount'

        with mock.patch.object(docker, 'call', side_effect=escaped):
            plain = docker.guard_contents(settings(), Path('/srv/nekoserver/installer/guards/v1'))
            placed = docker.guard_contents(
                settings(install={'photos': 'd3'}), Path('/srv/nekoserver/installer/guards/v1')
            )
        self.assertIn('After=srv-nekoserver.mount mnt-nekoserver-media.mount\n', plain)
        self.assertIn(
            'After=srv-nekoserver.mount mnt-nekoserver-media.mount srv-nekoserver-appdata-immich.mount\n', placed
        )
        # Needed and bound to: the two drives only, as before.
        for text in (plain, placed):
            self.assertIn('RequiresMountsFor=/srv/nekoserver /mnt/nekoserver-media\n', text)
            self.assertIn('BindsTo=srv-nekoserver.mount mnt-nekoserver-media.mount\n', text)


ROW = {'number': 4, 'uuid': 'fast-0003', 'name': 'sdd1', 'size': '238.5G', 'type': 'SSD', 'model': 'Fast', 'label': ''}


class PageTests(unittest.TestCase):
    """The storage page asks where a group's apps are installed, only where there is a
    drive for it; the answer is kept for the run and written once the apps' drive is open."""

    def setUp(self):
        import drive_table

        self.drive_table = drive_table
        self.addCleanup(drive_table.INSTALL_AHEAD.clear)
        self.said = []
        self.context = SimpleNamespace(config=settings(), log=self.said.append)

    def ask(self, *answers, others=(ROW,)):
        given = iter(answers)
        with (
            mock.patch.object(storage, 'install_drives', return_value=list(others)),
            mock.patch.object(storage, 'page_fields', return_value={'photos': '', 'cloud': ''}),
            mock.patch.object(self.drive_table, 'field', side_effect=lambda key, ask, prompt: next(given)),
            mock.patch.object(self.drive_table, 'brief', return_value='drive 4 (sdd1: 238 GB SSD, Fast)'),
        ):
            return storage.wished_installs(self.context, {'blockdevices': []})

    def test_enter_keeps_the_apps_drive_and_a_number_or_the_windows_answer_takes_another(self):
        self.assertEqual(self.ask('', ''), {})
        self.assertIn('Where should Immich be installed (the settings and the database)?', self.said)
        self.assertIn('  1) the same drive as NekoServer and your apps  (recommended)', self.said)
        self.assertIn('  2) a drive of its own: drive 4 (sdd1: 238 GB SSD, Fast)', self.said)
        self.assertEqual(self.ask('2', '1'), {'photos': ROW})
        self.assertEqual(self.ask('', 'drive:4'), {'cloud': ROW})
        # Not a number of the list: said, asked again, and the apps' drive in the end.
        self.assertEqual(self.ask('9', 'x', 'drive:7', ''), {})
        self.assertIn('Type a number from 1 to 2, or press Enter.', self.said)

    def test_nothing_is_asked_without_a_drive_for_it_or_for_a_group_already_placed(self):
        never = mock.Mock(side_effect=AssertionError('asked'))
        with mock.patch.object(self.drive_table, 'field', never):
            self.assertEqual(self.ask(others=()), {})
        with mock.patch.object(storage, 'page_fields', return_value={'photos': '', 'cloud': ''}):
            self.assertEqual(storage.installs_asked(settings()), ['photos', 'cloud'])
            self.assertEqual(storage.installs_asked(settings(install={'photos': 'd3'})), ['cloud'])

    def test_the_choice_is_written_once_the_apps_drive_is_open_and_never_moves_an_app(self):
        with tempfile.TemporaryDirectory() as folder:
            self.context.config['data_mount'] = folder
            (Path(folder) / 'appdata' / 'nextcloud').mkdir(parents=True)
            (Path(folder) / 'appdata' / 'nextcloud' / 'config.php').write_text('x')
            self.drive_table.INSTALL_AHEAD.update(photos=ROW, cloud=ROW)
            with (
                mock.patch.object(storage, 'placed_on', return_value='d3') as placed,
                mock.patch.object(self.drive_table, 'brief', return_value='drive 4 (sdd1: 238 GB SSD, Fast)'),
            ):
                storage.settle_installs(self.context)
        placed.assert_called_once_with(self.context.config, ROW)
        self.assertEqual(self.context.config['install'], {'photos': 'd3'})
        self.assertIn('Immich: installed on drive 4 (sdd1: 238 GB SSD, Fast), a drive of its own', self.said)
        self.assertIn(
            'Nextcloud: kept on the drive of NekoServer and your apps, where its settings already are', self.said
        )
        self.assertEqual(self.drive_table.INSTALL_AHEAD, {})

    def test_the_review_names_the_drive(self):
        with mock.patch.object(self.drive_table, 'brief', return_value='drive 4 (sdd1: 238 GB SSD, Fast)'):
            lines = storage.ahead_lines(settings(storage={}), {'blockdevices': []}, {}, {'photos': ROW})
        self.assertIn('Immich: installed on drive 4 (sdd1: 238 GB SSD, Fast), a drive of its own', lines)


class GivenUpTests(unittest.TestCase):
    """A group taken out of "install", and a drive that is lost."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.target = self.root / 'apps/appdata/immich'
        self.target.mkdir(parents=True)
        self.text = '\n'.join(
            [storage.INSTALL_BEGIN, f'/mnt/nekoserver-drives/d3/nekoserver/appdata/immich {self.target} none bind 0 0']
            + [storage.INSTALL_END]
        )
        self.said = []

    def test_an_app_still_on_its_drive_is_not_moved_back_and_setup_says_why(self):
        with (
            mock.patch.object(storage.os.path, 'ismount', return_value=True),
            self.assertRaisesRegex(RuntimeError, 'does not move an app that is set up back'),
        ):
            storage.given_up(self.text, [], self.said.append)

    def test_with_the_drive_gone_the_closed_folder_is_opened_for_the_app_to_start_anew(self):
        os.chmod(self.target, 0o000)
        ran = mock.Mock()
        storage.given_up(self.text, [], self.said.append, ran)
        self.assertEqual(ran.call_args[0][0], ['chattr', '-i', str(self.target)])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)
        self.assertIn('sudo bash install.sh --recover', self.said[0])
        # A group that is still placed is left as it is.
        os.chmod(self.target, 0o000)
        still = [('immich', Path('/x'), self.target, Path('/mnt/nekoserver-drives/d3'))]
        storage.given_up(self.text, still, self.said.append, mock.Mock(side_effect=AssertionError('opened')))
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o000)
        os.chmod(self.target, 0o755)

    def test_a_lost_drive_stops_setup_with_what_to_do(self):
        config = settings(install={'photos': 'd3'})
        with self.assertRaises(RuntimeError) as stopped:
            storage.installation_drives_connected(config, str(self.root) + '/no-such-')
        said = str(stopped.exception)
        self.assertIn('The drive that Immich are installed on is not connected (UUID fast-0003).', said)
        self.assertIn('take "photos" out of "install" and "d3" out of "storage"', said)
        self.assertIn('it restores into a new folder, never over an app', said)
        # Connected, or nothing placed: setup goes on.
        (self.root / 'by-fast-0003').write_text('')
        storage.installation_drives_connected(config, str(self.root) + '/by-')
        storage.installation_drives_connected(settings(), str(self.root) + '/no-such-')


class SpaceTests(unittest.TestCase):
    def test_the_space_check_counts_the_drive_the_apps_are_installed_on(self):
        from modules import capacity

        config = settings(install={'movies': 'd3'})
        room = {'/mnt/nekoserver-drives/d3': 4 * capacity.GIB}
        problems = capacity.installed_elsewhere(config, lambda path: room[str(path)])
        self.assertEqual(len(problems), 1)
        self.assertIn('Jellyfin', problems[0])
        self.assertIn('has only 4 GB free. Their settings and databases need at least 10 GB there.', problems[0])
        room['/mnt/nekoserver-drives/d3'] = 40 * capacity.GIB
        self.assertEqual(capacity.installed_elsewhere(config, lambda path: room[str(path)]), [])
        # Nothing placed: no drive is looked at.
        never = mock.Mock(side_effect=AssertionError('measured'))
        self.assertEqual(capacity.installed_elsewhere(settings(), never), [])


class UninstallTests(unittest.TestCase):
    """The uninstaller takes setup's own blocks out of /etc/fstab, unmounts what is mounted
    inside the drives before the drives, and opens the folders setup closed."""

    def setUp(self):
        import uninstaller

        self.uninstaller = uninstaller
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.plan = SimpleNamespace(
            data='/srv/nekoserver',
            media='/mnt/nekoserver-media',
            at=lambda path: self.root / str(path).lstrip('/'),
        )
        self.calls, self.said = [], []

    def call(self, argv, timeout=0):
        self.calls.append(argv)
        return 0, ''

    def test_the_blocks_are_the_ones_setup_writes(self):
        import existing_media

        self.assertEqual(
            self.uninstaller.FSTAB_BLOCKS,
            ((existing_media.BEGIN, existing_media.END), (storage.INSTALL_BEGIN, storage.INSTALL_END)),
        )
        self.assertEqual(self.uninstaller.EXTRA_MOUNTS, str(neko_common.EXTRA_MOUNTS))

    def test_a_whole_block_is_taken_and_nothing_else(self):
        theirs = [
            'UUID=abc / ext4 defaults 0 1',
            '# NekoServer: a note someone wrote',
            'UUID=def /home ext4 defaults 0 2',
        ]
        block = [
            storage.INSTALL_BEGIN,
            '/mnt/nekoserver-drives/d3/nekoserver/appdata/immich /srv/nekoserver/appdata/immich none bind,nofail 0 0',
            storage.INSTALL_END,
        ]
        lines, folders = self.uninstaller.fstab_blocks('\n'.join(theirs[:2] + block + theirs[2:]) + '\n')
        self.assertEqual(lines, block)
        self.assertEqual(folders, ['/srv/nekoserver/appdata/immich'])
        # A block that never ends is not setup's for certain: nothing of it is taken.
        self.assertEqual(self.uninstaller.fstab_blocks('\n'.join(theirs + block[:2]) + '\n'), ([], []))
        self.assertEqual(self.uninstaller.fstab_blocks(''), ([], []))

    def test_the_closed_folder_is_opened_and_goes_and_other_folders_are_left_alone(self):
        closed = self.root / 'srv/nekoserver/appdata/immich'
        closed.mkdir(parents=True)
        elsewhere = self.root / 'home/someone/films'
        elsewhere.mkdir(parents=True)
        left = []
        self.uninstaller._unmount_inside(
            self.plan, ['/srv/nekoserver/appdata/immich', '/home/someone/films'], self.call, self.said.append, left
        )
        self.assertEqual(self.calls, [['chattr', '-i', str(closed)]])
        self.assertFalse(closed.exists())
        self.assertTrue(elsewhere.is_dir())
        self.assertEqual(left, [])

    def test_the_drives_beyond_the_two_are_unmounted_and_their_folders_go(self):
        drive = self.root / 'mnt/nekoserver-drives/d3'
        drive.mkdir(parents=True)
        left = []
        with mock.patch.object(self.uninstaller.os.path, 'ismount', side_effect=lambda path: path == str(drive)):
            self.uninstaller._unmount_extra(self.plan, self.call, self.said.append, left)
        # The folder setup closed before mounting the drive is opened, then removed.
        self.assertEqual(self.calls, [['umount', '/mnt/nekoserver-drives/d3'], ['chattr', '-i', str(drive)]])
        self.assertEqual(self.said, ['Unmounting /mnt/nekoserver-drives/d3'])
        self.assertFalse((self.root / 'mnt/nekoserver-drives').exists())
        # One that stays busy is said and listed, and its folder stays.
        drive.mkdir(parents=True)
        busy = lambda argv, timeout=0: (32, 'target is busy')  # noqa: E731
        with mock.patch.object(self.uninstaller.os.path, 'ismount', side_effect=lambda path: path == str(drive)):
            self.uninstaller._unmount_extra(self.plan, busy, self.said.append, left)
        self.assertEqual(left, ['/mnt/nekoserver-drives/d3 (still mounted)'])
        self.assertTrue(drive.is_dir())



class SealedMountFolderTests(unittest.TestCase):
    """Gate runs C9 and C16: with the drive of a group away at the start, Docker made the
    apps' folders on the system disk under its mount folder, and Lidarr started there."""

    def test_the_empty_folder_of_a_drive_beyond_the_two_is_closed_before_mounting(self):
        calls = []
        run = lambda argv, **how: calls.append(argv) or mock.Mock(returncode=0)  # noqa: E731
        with tempfile.TemporaryDirectory() as root:
            drives = Path(root) / 'mnt/nekoserver-drives'
            (drives / 'd3').mkdir(parents=True)
            with mock.patch.object(neko_common, 'EXTRA_MOUNTS', drives):
                storage.seal_extra(str(drives / 'd3'), run)
                self.assertEqual(calls, [['chattr', '+i', str(drives / 'd3')]])
                # Not one with something in it, and not the two drives' own folders.
                (drives / 'd4').mkdir()
                (drives / 'd4' / 'left').write_text('x')
                storage.seal_extra(str(drives / 'd4'), run)
                (Path(root) / 'srv/nekoserver').mkdir(parents=True)
                storage.seal_extra(str(Path(root) / 'srv/nekoserver'), run)
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
