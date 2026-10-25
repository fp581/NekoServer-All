"""The apps follow-ups (0.39.6): imports that a container can't redirect with a symlink,
connections to a removed app taken away, a changed qBittorrent password passed on to the
download apps, and qBittorrent's automatic mode switched on only for Lidarr."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source_import


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def roomy(path):
    return SimpleNamespace(free=1 << 40)


class SymlinkSafeImportTests(unittest.TestCase):
    """A download client's folder (a source) and the library folders can be changed by the
    apps' containers while a copy runs. A file or folder swapped for a symlink after the
    checks must not make the copy read outside the source or write outside the library."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.source, self.media, self.outside = base / 'downloads', base / 'hdd', base / 'private'
        (self.source / 'Album').mkdir(parents=True)
        (self.source / 'Album/track.flac').write_bytes(b'track')
        self.outside.mkdir()
        # As long as the track and dated the same: only the symlink tells them apart.
        (self.outside / 'secret.txt').write_bytes(b'SECRT')
        for name in ('Album/track.flac',):
            os.utime(self.source / name, ns=(1_000_000_000, 2_000_000_000))
        os.utime(self.outside / 'secret.txt', ns=(1_000_000_000, 2_000_000_000))
        self.media.mkdir()
        self.entry = {'type': 'local', 'path': str(self.source), 'category': 'music'}

    def tearDown(self):
        self.tmp.cleanup()

    def copy_with_swap(self, swap):
        """Copy, with `swap` run after the files were checked and before they are copied."""
        def checked_then_swapped(path):
            swap()
            return roomy(path)
        with mock.patch.object(source_import.shutil, 'disk_usage', side_effect=checked_then_swapped):
            return source_import.copy_source(self.entry, self.media, apply=True)

    def copied(self):
        library = self.media / 'library'
        return sorted(p.read_bytes() for p in library.rglob('*') if p.is_file() and not p.is_symlink())

    def test_a_file_swapped_for_a_symlink_is_not_copied(self):
        def swap():
            (self.source / 'Album/track.flac').unlink()
            (self.source / 'Album/track.flac').symlink_to(self.outside / 'secret.txt')
        with self.assertRaises((RuntimeError, OSError)):
            self.copy_with_swap(swap)
        self.assertNotIn(b'SECRT', self.copied())

    def test_a_source_folder_swapped_for_a_symlink_is_not_followed(self):
        (self.outside / 'track.flac').write_bytes(b'SECRT')
        os.utime(self.outside / 'track.flac', ns=(1_000_000_000, 2_000_000_000))

        def swap():
            (self.source / 'Album').rename(self.source / 'Album.old')
            (self.source / 'Album').symlink_to(self.outside)
        with self.assertRaises((RuntimeError, OSError)):
            self.copy_with_swap(swap)
        self.assertNotIn(b'SECRT', self.copied())

    def test_a_library_folder_swapped_for_a_symlink_is_not_written_through(self):
        """Lidarr (or anything with the library folder) swaps the album's folder in the
        library for a symlink right after NekoServer opened it: the copy still lands in
        the folder that was opened, never in the symlink's target."""
        album = self.media / 'library/music/Album'
        album.mkdir(parents=True)
        swapped, outside = [], self.outside

        class Guard:
            """The drive check, used here as the moment between opening the folder and
            writing into it (the second check, by name or by handle)."""
            calls = 0

            def check(self, path):
                self.swap_on_second()

            def check_fd(self, fd):
                self.swap_on_second()

            def swap_on_second(self):
                Guard.calls += 1
                if Guard.calls == 2 and not swapped:
                    album.rename(album.with_name('Album.moved'))
                    album.symlink_to(outside)
                    swapped.append(True)
        entries = source_import.planned_copies(self.source, self.media / 'library/music')
        for current, dest, stat in entries:
            source_import.import_file(current, dest, stat, None, None, Guard())
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['secret.txt'])
        self.assertEqual((album.with_name('Album.moved') / 'track.flac').read_bytes(), b'track')

    def test_a_missing_library_folder_is_made_without_following_symlinks(self):
        (self.media / 'library/music').mkdir(parents=True)
        (self.media / 'library/music/Album').symlink_to(self.outside)
        current = self.source / 'Album/track.flac'
        target, checked = self.media / 'library/music/Album/track.flac', current.lstat()
        with self.assertRaises((RuntimeError, OSError)):
            source_import.import_file(current, target, checked, None, None)
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ['secret.txt'])

    def test_a_plain_import_still_copies_and_keeps_the_date(self):
        result = source_import.copy_source(self.entry, self.media, apply=True)
        self.assertEqual(result['copied'], 1)
        dest = self.media / 'library/music/Album/track.flac'
        self.assertEqual(dest.read_bytes(), b'track')
        self.assertEqual(dest.stat().st_mtime_ns, 2_000_000_000)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), oct(0o664))
        self.assertFalse(any(p.name.endswith('.neko-part') for p in self.media.rglob('*')))
        # A second run finds the copy and makes none.
        self.assertEqual(source_import.copy_source(self.entry, self.media, apply=True)['copied'], 0)


class FakeArr:
    """Prowlarr and the Arr apps' API, as media-connect.py's request() sees it."""
    def __init__(self, data, test_ok=True):
        self.data = data          # {(app, endpoint): rows}
        self.calls = []
        self.test_ok = test_ok

    def __call__(self, app, key, endpoint, method='GET', payload=None, timeout=12):
        self.calls.append((app, endpoint, method, payload))
        if method == 'GET':
            return self.data[(app, endpoint)]
        if endpoint == '/downloadclient/test' and not self.test_ok:
            raise RuntimeError(f'{app} {endpoint}: HTTP 400')
        return {}

    def sent(self, method):
        return [(app, endpoint) for app, endpoint, m, _ in self.calls if m == method]


def neko_link(app, port, name=None, base=None):
    return {'id': 7, 'name': name or f'neko-{app}', 'fields': [
        {'name': 'prowlarrUrl', 'value': 'http://prowlarr:9696'},
        {'name': 'baseUrl', 'value': base or f'http://{app}:{port}'}, {'name': 'apiKey', 'value': '********'}]}


def neko_client(category_field, category, host='qbittorrent', username='old'):
    return {'id': 3, 'name': 'Neko-qBittorrent', 'enable': True, 'fields': [
        {'name': 'host', 'value': host}, {'name': 'port', 'value': 8080},
        {'name': 'username', 'value': username}, {'name': 'password', 'value': '********'},
        {'name': category_field, 'value': category}]}


class RemovedAppConnectionTests(unittest.TestCase):
    """An app taken out of media.services: the connections setup made to it go, the
    ones a person made or changed stay."""
    def setUp(self):
        self.connect = load('media_connect_removed', 'media-connect.py')

    def test_prowlarrs_link_to_a_removed_app_is_taken_away_and_a_changed_one_kept(self):
        fake = FakeArr({('prowlarr', '/applications'): [
            neko_link('sonarr', 8989),                                         # setup's, Sonarr removed
            dict(neko_link('lidarr', 8686, base='http://192.168.1.5:8686'), id=8),  # re-pointed by hand
            dict(neko_link('radarr', 7878), id=9),                             # Radarr still selected
            {'id': 10, 'name': 'My Sonarr', 'fields': []}]})                   # the user's own
        with mock.patch.object(self.connect, 'request', fake), \
                mock.patch.object(self.connect, 'app_key', return_value='k'), mock.patch('builtins.print') as said:
            self.connect.stale_applications('/srv', ['prowlarr', 'radarr'], apply=True)
        self.assertEqual(fake.sent('DELETE'), [('prowlarr', '/applications/7')])
        self.assertIn('changed by hand', ' '.join(str(c.args[0]) for c in said.call_args_list))

    def test_a_preview_removes_nothing(self):
        fake = FakeArr({('prowlarr', '/applications'): [neko_link('sonarr', 8989)]})
        with mock.patch.object(self.connect, 'request', fake), \
                mock.patch.object(self.connect, 'app_key', return_value='k'), mock.patch('builtins.print'):
            self.connect.stale_applications('/srv', ['prowlarr'], apply=False)
        self.assertEqual(fake.sent('DELETE'), [])

    def test_the_arr_download_clients_go_with_qbittorrent(self):
        fake = FakeArr({('sonarr', '/downloadclient'): [neko_client('tvCategory', 'tv'),
                                                        {'id': 4, 'name': 'My Transmission', 'fields': []}],
                        ('radarr', '/downloadclient'): [neko_client('movieCategory', 'movies', host='10.0.0.2')]})
        with mock.patch.object(self.connect, 'request', fake), \
                mock.patch.object(self.connect, 'app_key', return_value='k'), mock.patch('builtins.print'):
            self.connect.stale_download_clients('/srv', ['sonarr', 'radarr'], apply=True)
            self.assertEqual(fake.sent('DELETE'), [('sonarr', '/downloadclient/3')])
            fake.calls.clear()
            self.connect.stale_download_clients('/srv', ['sonarr', 'qbittorrent'], apply=True)
        self.assertEqual(fake.calls, [])     # qBittorrent still selected: nothing looked at

    def test_seerrs_provider_for_a_removed_app_is_taken_away(self):
        import seerr_providers as sp
        seerr = mock.Mock(headers={})
        ours = {'id': 0, 'name': 'NekoServer Sonarr', 'hostname': 'sonarr', 'port': sp.SPECS['sonarr'][0]}
        mine = {'id': 1, 'name': 'Sonarr 4K', 'hostname': 'sonarr', 'port': sp.SPECS['sonarr'][0]}
        seerr.call.side_effect = lambda endpoint, method='GET', payload=None: [ours, mine] if endpoint == '/settings/sonarr' else {}
        with mock.patch.object(sp, 'open_seerr'):
            rows = sp.remove_stale('/srv', ('seerr', 'radarr'), apply=True, factory=lambda app: seerr)
        self.assertIn(mock.call('/settings/sonarr/0', 'DELETE'), seerr.call.call_args_list)
        self.assertNotIn(mock.call('/settings/sonarr/1', 'DELETE'), seerr.call.call_args_list)
        self.assertEqual(rows[0]['status'], 'pass')
        self.assertIn('1 other Sonarr provider', rows[0]['reason'])
        self.assertEqual(sp.remove_stale('/srv', ('seerr', 'sonarr', 'radarr'), True, factory=lambda app: seerr), [])

    def test_setup_takes_them_away_only_for_an_app_it_had_set_up(self):
        from modules import onboarding
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'compose/sonarr').mkdir(parents=True)
            (Path(d) / 'compose/sonarr/compose.yaml').write_text('services: {}\n')
            ctx = mock.Mock(config={'data_mount': d, 'media': {'services': ['jellyfin', 'radarr', 'qbittorrent']}})
            with mock.patch.object(onboarding.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
                onboarding.remove_stale(ctx, Path(d) / 'release', Path(d) / 'config.json')
                self.assertIn('--remove-stale', run.call_args.args[0])
                (Path(d) / 'compose/sonarr/compose.yaml').unlink()
                run.reset_mock()
                onboarding.remove_stale(ctx, Path(d) / 'release', Path(d) / 'config.json')
                run.assert_not_called()


class QbittorrentLoginPassedOnTests(unittest.TestCase):
    """The qBittorrent login changed with account-setup.py (c): the download clients setup
    made in the Arr apps get the new one, instead of failing every download."""
    def setUp(self):
        self.connect = load('media_connect_login', 'media-connect.py')
        self.stored = {'username': 'admin2', 'password': 'new-secret', 'state': 'verified'}

    def run_pass_on(self, fake):
        qbit = mock.Mock()
        with mock.patch.object(self.connect, 'request', fake), \
                mock.patch.object(self.connect, 'app_key', return_value='k'), \
                mock.patch.object(self.connect.account_store, 'get', return_value=self.stored), \
                mock.patch('builtins.print'):
            self.connect.pass_on_login('/srv', ('sonarr',), client_factory=lambda: qbit)
        qbit.login.assert_called_once_with('admin2', 'new-secret')
        return fake

    def test_a_client_with_the_old_login_gets_the_new_one(self):
        fake = self.run_pass_on(FakeArr({('sonarr', '/downloadclient'): [neko_client('tvCategory', 'tv')]}, test_ok=False))
        puts = [payload for app, endpoint, method, payload in fake.calls if method == 'PUT']
        self.assertEqual(fake.sent('PUT'), [('sonarr', '/downloadclient/3')])
        fields = {f['name']: f['value'] for f in puts[0]['fields']}
        self.assertEqual((fields['username'], fields['password'], fields['tvCategory']), ('admin2', 'new-secret', 'tv'))

    def test_a_client_that_still_signs_in_is_left_as_it_is(self):
        fake = self.run_pass_on(FakeArr({('sonarr', '/downloadclient'): [neko_client('tvCategory', 'tv')]}))
        self.assertEqual(fake.sent('PUT'), [])

    def test_a_client_pointed_elsewhere_is_not_touched(self):
        fake = FakeArr({('sonarr', '/downloadclient'): [neko_client('tvCategory', 'tv', host='10.0.0.2')]}, test_ok=False)
        with self.assertRaises(RuntimeError):
            self.run_pass_on(fake)
        self.assertEqual(fake.sent('PUT'), [])

    def test_the_normal_setup_run_passes_it_on_too(self):
        fake = FakeArr({('sonarr', '/downloadclient'): [neko_client('tvCategory', 'tv')]}, test_ok=False)
        with mock.patch.object(self.connect, 'request', fake), \
                mock.patch.object(self.connect, 'app_key', return_value='k'), \
                mock.patch.object(self.connect.account_store, 'get', return_value=self.stored), \
                mock.patch.object(self.connect.qbit_setup, 'configure'), mock.patch('builtins.print'):
            self.connect.configure_download_clients('/srv', True, ('sonarr',))
        self.assertEqual(fake.sent('PUT'), [('sonarr', '/downloadclient/3')])

    def test_account_setup_passes_a_changed_login_on(self):
        setup = load('account_setup_cli', 'account-setup.py')
        config = {'media': {'services': ['sonarr', 'qbittorrent']}}
        before = {'username': 'admin2', 'password': 'old-secret'}
        run = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(setup.account_store, 'get', return_value=self.stored), mock.patch('builtins.print'):
            self.assertTrue(setup.pass_on_qbittorrent('/c.json', config, ['qbittorrent'], before, run=run))
            self.assertIn('--pass-on-login', run.call_args.args[0])
            run.reset_mock()
            same = {'username': 'admin2', 'password': 'new-secret'}
            self.assertTrue(setup.pass_on_qbittorrent('/c.json', config, ['qbittorrent'], same, run=run))
            run.assert_not_called()
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(setup.pass_on_qbittorrent('/c.json', config, ['qbittorrent'], before, run=run))


class QbitAutomaticModeOnlyForLidarrTests(unittest.TestCase):
    """Automatic mode is needed only for Lidarr's music; without Lidarr a choice of
    manual mode in qBittorrent stays."""
    def configure(self, categories, automatic):
        import qbit_setup
        prefs = {'save_path': '/data/downloads', 'auto_tmm_enabled': automatic}
        saved, said = [], []

        def call(endpoint, form=None, json_result=False):
            if endpoint == '/app/preferences':
                return dict(prefs)
            if endpoint == '/app/setPreferences':
                saved.append(json.loads(form['json']))
                prefs.update(saved[-1])
            if endpoint == '/torrents/categories':
                return {c: {'savePath': '/data/downloads/' + c} for c in categories}
            return {}
        fake = mock.Mock(call=call)
        qbit_setup.configure('u', 'p', apply=True, categories=categories, client=fake, output=said.append)
        return saved, said

    def test_manual_mode_stays_without_lidarr(self):
        saved, said = self.configure(('tv', 'movies'), False)
        self.assertEqual(saved, [])
        self.assertFalse(any('automatic mode' in line for line in said))

    def test_with_lidarr_it_is_switched_on_and_said(self):
        saved, said = self.configure(('tv', 'music'), False)
        self.assertEqual(saved, [{'auto_tmm_enabled': True}])
        self.assertTrue(any('Lidarr needs it' in line and 'manual mode' in line for line in said))


if __name__ == '__main__':
    unittest.main()
