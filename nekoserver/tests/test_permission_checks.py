"""The read-only folder checks of the setup report (permission_checks.py): which bits of
a folder's mode an account gets, whether it may reach and use a folder, and which
folders are checked for the media apps and the shares."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import permission_checks

ME = os.getuid()
SOMEONE_ELSE = ME + 4242
A_GROUP = 4243


class ModeBitsTests(unittest.TestCase):
    def test_the_owner_reads_the_owners_bits_a_member_the_groups_and_anyone_else_the_rest(self):
        folder = SimpleNamespace(st_uid=ME, st_gid=A_GROUP)
        self.assertEqual(permission_checks.mode_shift(folder, ME, set()), 6)
        self.assertEqual(permission_checks.mode_shift(folder, SOMEONE_ELSE, {A_GROUP}), 3)
        self.assertEqual(permission_checks.mode_shift(folder, SOMEONE_ELSE, {A_GROUP + 1}), 0)


class AccessTests(unittest.TestCase):
    """Folders made in a temporary folder: their owner is the account running the tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name)
        os.chmod(self.top, 0o755)
        self.folder = self.top / 'library'
        self.folder.mkdir()
        self.mine = os.getgid()

    def access(self, uid, groups, mask, **more):
        return permission_checks.access(self.folder, uid, groups, mask, **more)

    def test_the_owner_may_use_a_folder_only_for_them(self):
        os.chmod(self.folder, 0o700)
        self.assertTrue(self.access(ME, set(), 7))
        self.assertFalse(self.access(SOMEONE_ELSE, set(), 5))

    def test_a_member_of_the_group_gets_what_the_group_bits_give(self):
        os.chmod(self.folder, 0o750)
        self.assertTrue(self.access(SOMEONE_ELSE, {self.mine}, 5))
        self.assertFalse(self.access(SOMEONE_ELSE, {self.mine}, 7))
        self.assertFalse(self.access(SOMEONE_ELSE, set(), 5))

    def test_a_folder_on_a_path_the_account_cannot_pass_through_is_out_of_reach(self):
        os.chmod(self.folder, 0o777)
        os.chmod(self.top, 0o700)
        self.addCleanup(os.chmod, self.top, 0o755)
        self.assertFalse(self.access(SOMEONE_ELSE, set(), 7))
        # An app's own folder is bound into its container: only the folder itself counts.
        self.assertTrue(self.access(SOMEONE_ELSE, set(), 7, bind_root=True))

    def test_a_file_where_the_folder_should_be_is_no_folder(self):
        self.folder.rmdir()
        self.folder.write_text('')
        os.chmod(self.folder, 0o777)
        self.assertFalse(self.access(ME, set(), 7))

    def test_a_folder_reached_through_a_link_is_refused(self):
        os.chmod(self.folder, 0o777)
        link = self.top / 'link'
        link.symlink_to(self.folder)
        with self.assertRaisesRegex(RuntimeError, 'Refusing symlink path: '):
            permission_checks.access(link, ME, set(), 7)
        row = permission_checks.inspected('media:movies', link, ME, set(), True)
        self.assertEqual(row['status'], 'needs_setup')

    def test_a_row_says_pass_or_needs_setup_with_the_folders_name(self):
        os.chmod(self.folder, 0o750)
        row = permission_checks.inspected('media:movies', self.folder, SOMEONE_ELSE, {self.mine}, False)
        self.assertEqual(
            (row['id'], row['name'], row['status']),
            ('permission:media:movies', 'media:movies filesystem access', 'pass'),
        )
        row = permission_checks.inspected('media:movies', self.folder, SOMEONE_ELSE, {self.mine}, True)
        self.assertEqual(row['status'], 'needs_setup')
        # A folder in a path the account cannot pass through, unless it is bound.
        os.chmod(self.folder, 0o777)
        os.chmod(self.top, 0o700)
        self.addCleanup(os.chmod, self.top, 0o755)
        self.assertEqual(
            permission_checks.inspected('x', self.folder, SOMEONE_ELSE, set(), True)['status'], 'needs_setup'
        )


def drive(_config, what):
    return Path('/drive-for-' + what)


class FoldersCheckedTests(unittest.TestCase):
    """Which folders are checked, for which account, and how (inspected is recorded)."""

    def setUp(self):
        self.calls = []

        def inspected(name, path, owner, groups, writable, bind_root=False):
            self.calls.append((name, path, owner, groups, writable, bind_root))
            return {'id': 'permission:' + name}

        for name, value in (
            ('inspected', inspected),
            ('files_root', drive),
            ('music_downloads', lambda _config: Path('/drive-for-music/downloads')),
            ('media_services', lambda config: config.get('media', {}).get('services', [])),
        ):
            patch = mock.patch.object(permission_checks, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_each_media_app_and_library_folder_is_checked_for_the_media_account(self):
        config = {'data_mount': '/srv/nekoserver', 'media': {'services': ['jellyfin', 'sonarr']}}
        permission_checks.media_rows(config)
        libraries = [
            ('media:movies', Path('/drive-for-movies/library/movies')),
            ('media:tv', Path('/drive-for-movies/library/tv')),
            ('media:music', Path('/drive-for-music/library/music')),
            ('media:downloads', Path('/drive-for-movies/library/downloads')),
            ('media:music-downloads', Path('/drive-for-music/downloads')),
        ]
        self.assertEqual(
            self.calls,
            [
                ('jellyfin:appdata', Path('/srv/nekoserver/appdata/jellyfin'), 1000, {1000}, True, True),
                ('sonarr:appdata', Path('/srv/nekoserver/appdata/sonarr'), 1000, {1000}, True, True),
                *[(name, path, 1000, {1000}, True, False) for name, path in libraries],
            ],
        )

    def test_the_media_account_in_the_settings_is_the_one_checked(self):
        config = {'data_mount': '/srv/nekoserver', 'media': {'services': ['jellyfin'], 'uid': 1500, 'gid': 1600}}
        permission_checks.media_rows(config)
        self.assertEqual({(owner, frozenset(groups)) for _, _, owner, groups, _, _ in self.calls}, {(1500, frozenset({1600}))})

    def samba(self, config, user='alice'):
        record = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        with (
            mock.patch('modules.samba.samba_account', return_value=user),
            mock.patch.object(permission_checks.pwd, 'getpwnam', return_value=record) as getpwnam,
            mock.patch.object(permission_checks.os, 'getgrouplist', return_value=[1001, 1000]),
            mock.patch('modules.samba.wanted_shares', return_value={'Media': 'library', 'Photos': 'photos/archive'}),
            mock.patch('modules.samba.placed_shares', return_value={'Media': Path('/elsewhere/films')}),
            mock.patch('modules.samba.writable', side_effect=lambda name, settings: name == 'Media'),
        ):
            rows = permission_checks.samba_rows(config)
        getpwnam.assert_called_once_with(user)
        return rows

    def test_each_share_is_checked_for_the_samba_account_where_its_folder_is(self):
        config = {'media_mount': '/mnt/nekoserver-media', 'networking': {}}
        rows = self.samba(config)
        self.assertEqual(rows, [{'id': 'permission:samba:Media'}, {'id': 'permission:samba:Photos'}])
        self.assertEqual(
            self.calls,
            [
                ('samba:Media', Path('/elsewhere/films'), 1001, {1001, 1000}, True, False),
                ('samba:Photos', Path('/mnt/nekoserver-media/photos/archive'), 1001, {1001, 1000}, False, False),
            ],
        )

    def test_a_samba_account_that_does_not_exist_is_one_row_to_set_up(self):
        with (
            mock.patch('modules.samba.samba_account', return_value='nobody-here'),
            mock.patch.object(permission_checks.pwd, 'getpwnam', side_effect=KeyError('nobody-here')),
        ):
            rows = permission_checks.samba_rows({'networking': {}})
        self.assertEqual([(row['id'], row['status']) for row in rows], [('permission:samba:user', 'needs_setup')])
        self.assertEqual(self.calls, [])

    def test_media_folders_are_checked_only_with_the_media_step_and_an_app_and_shares_only_with_samba(self):
        with (
            mock.patch.object(permission_checks, 'media_rows', return_value=['media']),
            mock.patch.object(permission_checks, 'samba_rows', return_value=['samba']),
        ):
            with_apps = {'media': {'services': ['jellyfin']}}
            self.assertEqual(permission_checks.check(with_apps, ['media', 'samba']), ['media', 'samba'])
            self.assertEqual(permission_checks.check(with_apps, ['media']), ['media'])
            self.assertEqual(permission_checks.check(with_apps, ['samba']), ['samba'])
            self.assertEqual(permission_checks.check({'media': {'services': []}}, ['media', 'samba']), ['samba'])


if __name__ == '__main__':
    unittest.main()
