"""Setup's links from Prowlarr to Sonarr, Radarr and Lidarr, and qBittorrent added to an
app as its download client (media-connect.py, split in 0.52.2; mutation test T6).

The apps' API is a small fake that answers like Prowlarr and the Arr apps, and refuses a
request made with another app's key."""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location('media_connect_links', ROOT / 'media-connect.py')
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)

KEYS = {'prowlarr': 'key-p', 'sonarr': 'key-s', 'radarr': 'key-r', 'lidarr': 'key-l'}


def definition(implementation):
    """One of Prowlarr's application definitions (GET /applications/schema)."""
    return {
        'implementation': implementation,
        'fields': [{'name': 'prowlarrUrl'}, {'name': 'baseUrl'}, {'name': 'apiKey'}],
    }


DEFINITIONS = [definition('Sonarr'), definition('Radarr'), definition('Lidarr')]


def link(app, base=None, sync='addOnly'):
    """A link Prowlarr has already, as it answers it (the key masked)."""
    return {
        'id': 4,
        'name': f'neko-{app}',
        'syncLevel': sync,
        'fields': [
            {'name': 'prowlarrUrl', 'value': connect.PROWLARR_URL},
            {'name': 'baseUrl', 'value': base or connect.in_docker(app)},
            {'name': 'apiKey', 'value': '********'},
        ],
    }


class FakeApps:
    def __init__(self, existing=(), definitions=DEFINITIONS, refuse_post=(), down=()):
        self.existing = list(existing)
        self.definitions = definitions
        self.refuse_post = refuse_post  # apps whose new link Prowlarr refuses
        self.down = down  # apps that do not answer
        self.posted = []

    def __call__(self, app, key, endpoint, method='GET', payload=None, timeout=12):
        if key != KEYS[app]:
            raise RuntimeError(f'{app}: HTTP 401')
        if app in self.down:
            raise RuntimeError(f'{app}: connection refused')
        if (app, endpoint, method) == ('prowlarr', '/applications/schema', 'GET'):
            return self.definitions
        if (app, endpoint, method) == ('prowlarr', '/applications', 'GET'):
            return self.existing
        if (app, endpoint, method) == ('prowlarr', '/applications', 'POST'):
            name = payload['name'].removeprefix('neko-')
            if name in self.refuse_post:
                raise RuntimeError('prowlarr: HTTP 400')
            self.posted.append(payload)
            return dict(payload, id=len(self.posted))
        return {}  # /system/status, /applications/test


def link_apps(apps, apply):
    """(what setup printed, the error it stopped with or None)."""
    said = io.StringIO()
    stopped = None
    with (
        mock.patch.object(connect, 'request', apps),
        mock.patch.object(connect, 'app_key', side_effect=lambda app, root: KEYS[app]),
        contextlib.redirect_stdout(said),
    ):
        try:
            connect.configure_applications('/srv/nekoserver', apply)
        except RuntimeError as exc:
            stopped = str(exc)
    return said.getvalue(), stopped


class ProwlarrLinkTests(unittest.TestCase):
    def test_a_preview_says_what_it_would_link_and_links_nothing(self):
        apps = FakeApps()
        said, stopped = link_apps(apps, apply=False)
        self.assertIsNone(stopped)
        self.assertEqual(apps.posted, [])
        for app in ('sonarr', 'radarr', 'lidarr'):
            self.assertIn(f'{app}: will link', said.splitlines())

    def test_each_app_is_linked_by_its_container_name_with_its_own_key(self):
        apps = FakeApps()
        said, stopped = link_apps(apps, apply=True)
        self.assertIsNone(stopped)
        self.assertEqual([p['name'] for p in apps.posted], ['neko-sonarr', 'neko-radarr', 'neko-lidarr'])
        sonarr = {f['name']: f['value'] for f in apps.posted[0]['fields']}
        self.assertEqual(
            sonarr, {'prowlarrUrl': connect.PROWLARR_URL, 'baseUrl': connect.in_docker('sonarr'), 'apiKey': 'key-s'}
        )
        self.assertEqual(apps.posted[0]['syncLevel'], 'addOnly')  # never removes the apps' own indexers
        self.assertIn('sonarr: linked', said.splitlines())

    def test_a_link_prowlarr_refuses_is_named_and_the_others_are_still_made(self):
        apps = FakeApps(refuse_post=('radarr',))
        said, stopped = link_apps(apps, apply=True)
        self.assertEqual(stopped, 'Prowlarr application connections incomplete: radarr')
        self.assertIn('radarr: could not link (prowlarr: HTTP 400); check Prowlarr UI', said.splitlines())
        self.assertEqual([p['name'] for p in apps.posted], ['neko-sonarr', 'neko-lidarr'])

    def test_an_app_that_does_not_answer_is_said_not_ready(self):
        said, stopped = link_apps(FakeApps(down=('lidarr',)), apply=True)
        self.assertEqual(stopped, 'Prowlarr application connections incomplete: lidarr')
        self.assertIn('lidarr: not ready (lidarr: connection refused)', said.splitlines())

    def test_an_app_prowlarr_has_no_definition_for_needs_a_person(self):
        said, stopped = link_apps(FakeApps(definitions=DEFINITIONS[:2]), apply=True)
        self.assertEqual(stopped, 'Prowlarr application connections incomplete: lidarr')
        self.assertIn('lidarr: no matching Prowlarr application definition; manual setup needed', said.splitlines())

    def test_a_definition_without_a_key_field_needs_a_person(self):
        keyless = dict(definition('Lidarr'), fields=[{'name': 'prowlarrUrl'}, {'name': 'baseUrl'}])
        said, stopped = link_apps(FakeApps(definitions=DEFINITIONS[:2] + [keyless]), apply=True)
        self.assertEqual(stopped, 'Prowlarr application connections incomplete: lidarr')
        self.assertIn('lidarr: missing provider field apiKey; manual setup needed', said.splitlines())

    def test_links_made_before_are_checked_and_kept(self):
        """A full sync, set in Prowlarr by hand, is a link that works too."""
        apps = FakeApps(existing=[link('sonarr'), link('radarr', sync='fullSync'), link('lidarr')])
        said, stopped = link_apps(apps, apply=True)
        self.assertIsNone(stopped)
        self.assertEqual(apps.posted, [])
        self.assertIn('radarr: existing connection fields verified', said.splitlines())

    def test_a_link_pointed_elsewhere_by_hand_is_left_for_a_person(self):
        apps = FakeApps(existing=[link('sonarr', base='http://192.168.1.5:8989'), link('radarr'), link('lidarr')])
        said, stopped = link_apps(apps, apply=True)
        self.assertEqual(stopped, 'Prowlarr application connections incomplete: sonarr')
        self.assertIn('sonarr: existing connection differs; review in Prowlarr', said.splitlines())
        self.assertEqual(apps.posted, [])


class DownloadClientTests(unittest.TestCase):
    """new_client: qBittorrent added to one app (here Sonarr) as its download client."""

    WANTED = {'host': 'qbittorrent', 'port': 8080, 'tvCategory': 'tv'}

    def add(self, schemas, apply=True, refuse=False):
        posted = []

        def request(app, key, endpoint, method='GET', payload=None, timeout=12):
            if method == 'GET':
                return schemas
            if refuse:
                raise RuntimeError('sonarr: HTTP 400')
            posted.append(payload)
            return {}

        said = io.StringIO()
        with mock.patch.object(connect, 'request', request), contextlib.redirect_stdout(said):
            done = connect.new_client('sonarr', 'key-s', dict(self.WANTED), apply)
        return done, said.getvalue(), posted

    def qbittorrent(self, fields=('host', 'port', 'tvCategory')):
        return {'implementation': 'QBittorrent', 'fields': [{'name': n} for n in fields]}

    def test_qbittorrent_is_added_switched_on_under_setups_name(self):
        done, said, posted = self.add([{'implementation': 'Transmission', 'fields': []}, self.qbittorrent()])
        self.assertTrue(done)
        self.assertEqual(len(posted), 1)
        self.assertEqual((posted[0]['name'], posted[0]['enable']), (connect.CLIENT, True))
        self.assertEqual({f['name']: f['value'] for f in posted[0]['fields']}, self.WANTED)
        self.assertIn('sonarr: configured', said.splitlines())

    def test_a_preview_adds_nothing(self):
        done, said, posted = self.add([self.qbittorrent()], apply=False)
        self.assertEqual((done, posted), (True, []))
        self.assertIn('sonarr: will configure qBittorrent', said.splitlines())

    def test_an_app_without_qbittorrent_says_so(self):
        done, said, _ = self.add([{'implementation': 'Transmission', 'fields': []}])
        self.assertFalse(done)
        self.assertIn('sonarr: no qBittorrent provider schema', said.splitlines())

    def test_a_field_the_app_does_not_have_needs_a_person(self):
        done, said, posted = self.add([self.qbittorrent(fields=('host', 'port'))])
        self.assertEqual((done, posted), (False, []))
        self.assertIn('sonarr: unsupported schema field tvCategory; needs manual configuration', said.splitlines())

    def test_a_client_the_app_refuses_is_said(self):
        done, said, _ = self.add([self.qbittorrent()], refuse=True)
        self.assertFalse(done)
        self.assertIn('sonarr: rejected (sonarr: HTTP 400); check credentials and WebUI settings', said.splitlines())


class DownloadClientStepTests(unittest.TestCase):
    """configure_download_clients: each app gets qBittorrent with its own category."""

    def test_each_app_gets_its_own_category_in_qbittorrent_and_in_the_app(self):
        made, categories = [], []
        with (
            mock.patch.object(connect, 'qbittorrent_login', return_value=('admin', 'pw')),
            mock.patch.object(connect.qbit_setup, 'configure', side_effect=lambda *a: categories.extend(a[-1])),
            mock.patch.object(
                connect,
                'download_client',
                side_effect=lambda app, category, *rest: made.append((app, category)) or True,
            ),
        ):
            connect.configure_download_clients('/srv/nekoserver', True)
        self.assertEqual(categories, ['tv', 'movies', 'music'])
        self.assertEqual(made, [('sonarr', 'tv'), ('radarr', 'movies'), ('lidarr', 'music')])


class DataDriveTests(unittest.TestCase):
    def test_a_data_root_reached_through_a_link_is_not_taken_for_the_drive(self):
        """`--data-root /srv/ssd` where /srv/ssd is a link to the drive: refused, the
        folder must be named by its real path."""
        with tempfile.TemporaryDirectory() as tmp:
            drive = Path(tmp) / 'drive'
            drive.mkdir()
            link = Path(tmp) / 'ssd'
            link.symlink_to(drive)
            parser = mock.Mock()
            parser.error.side_effect = SystemExit(2)
            args = mock.Mock(data_root=str(link))
            with mock.patch.object(connect.os.path, 'ismount', return_value=True):
                with self.assertRaises(SystemExit):
                    connect.mounted_ssd(parser, args, {})
                parser.error.assert_called_once_with(
                    'Data SSD must be mounted at --data-root (its real path: no links, no //)'
                )
                self.assertEqual(connect.mounted_ssd(parser, mock.Mock(data_root=str(drive)), {}), drive)


if __name__ == '__main__':
    unittest.main()
