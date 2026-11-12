"""Gate runs C9 and C16: with a group's own drive away, its apps stay stopped and nothing
is made in the drive's place. The Compose files bind every folder of your files in the
long form with create_host_path false (Docker then refuses to start the app instead of
making the folder on the disk Linux runs from), setup makes the folders those binds need
while the drive is there, and the drive-return service also runs when such a drive
appears, to start the apps Docker could not start meanwhile (drive_return.rs)."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules import media, server_os  # noqa: E402

try:
    import yaml  # only these tests and a few others read YAML; CI installs it
except ImportError:  # pragma: no cover
    yaml = None

DATA, FILMS, MUSIC = '/srv/nekoserver', '/mnt/nekoserver-media', '/mnt/nekoserver-drives/d3'
APPS = ('jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'qbittorrent')


def volumes(app):
    """The app's volumes as Compose reads them, rendered with the music on a drive of
    its own (d3) and the films on the media drive."""
    template = (ROOT / 'compose' / f'{app}.yaml').read_text(encoding='utf-8')
    rendered = media.render(template, DATA, FILMS, 1000, 1000, 'UTC', '127.0.0.1', MUSIC)
    return yaml.safe_load(rendered)['services'][app]['volumes']


def on_a_drive(source):
    return source.startswith((FILMS + '/', MUSIC + '/'))


@unittest.skipUnless(yaml, 'PyYAML is not installed')
class ComposeBindTests(unittest.TestCase):
    def test_every_bind_of_your_files_never_lets_docker_make_the_folder(self):
        for app in APPS:
            found = volumes(app)
            short = [v for v in found if isinstance(v, str) and on_a_drive(v.split(':')[0])]
            self.assertEqual(short, [], f'{app}: a short bind makes a missing folder')
            long = [v for v in found if isinstance(v, dict) and on_a_drive(v['source'])]
            self.assertTrue(long, app)
            for volume in long:
                self.assertEqual(volume['type'], 'bind', app)
                self.assertIs(volume['bind']['create_host_path'], False, f"{app}: {volume['source']}")

    def test_each_app_binds_the_same_folders_as_before(self):
        """The long form changes how a missing folder is treated, not what is bound."""
        expected = {
            'jellyfin': {
                (f'{FILMS}/library/movies', '/data/movies', True),
                (f'{FILMS}/library/tv', '/data/tv', True),
                (f'{FILMS}/library/existing', '/data/existing', True),
            },
            'navidrome': {
                (f'{MUSIC}/library/music', '/music', True),
                (f'{MUSIC}/library/existing', '/data/existing', True),
            },
            'sonarr': {(f'{FILMS}/library', '/data', False), (f'{FILMS}/library/existing', '/data/existing', False)},
            'radarr': {(f'{FILMS}/library', '/data', False), (f'{FILMS}/library/existing', '/data/existing', False)},
            'lidarr': {(f'{MUSIC}/library', '/data', False), (f'{MUSIC}/library/existing', '/data/existing', False)},
            'qbittorrent': {
                (f'{FILMS}/library', '/data', False),
                (f'{MUSIC}/library/downloads/music', '/data/downloads/music', False),
                (f'{FILMS}/library/existing', '/data/existing', False),
            },
        }
        for app, binds in expected.items():
            found = {
                (v['source'], v['target'], v.get('read_only', False))
                for v in volumes(app)
                if isinstance(v, dict) and on_a_drive(v['source'])
            }
            self.assertEqual(found, binds, app)
            # The app's own settings stay where every other part of NekoServer looks.
            self.assertIn(f'{DATA}/appdata/{app}:{"/data" if app == "navidrome" else "/config"}', volumes(app))

    def test_the_folders_of_the_person_own_media_are_made_before_the_apps_start(self):
        source = (ROOT / 'modules' / 'media.py').read_text(encoding='utf-8')
        made = source.index("ensure_directory(drive / 'library' / 'existing', 0o755)")
        self.assertLess(source.index('for drive in dict.fromkeys((films, music)):'), made)
        self.assertLess(made, source.index('            compose_up('))


class DriveReturnUnitTests(unittest.TestCase):
    def config(self, **locations):
        return {
            'data_mount': DATA,
            'media_mount': FILMS,
            'storage': {
                'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'},
                'd3': {'uuid': 'cccc-3333', 'fstype': 'ext4'},
                'nas1': {
                    'uuid': 'nfs-0123',
                    'fstype': 'nfs4',
                    'network': {'transport': 'nfs', 'export': 'nas:/music'},
                },
            },
            'locations': locations,
        }

    @staticmethod
    def escaped(argv, **_):
        return mock.Mock(stdout='device-' + argv[-1].rsplit('/', 1)[1] + '\n')

    def test_it_also_runs_when_a_drive_of_a_group_appears(self):
        devices = server_os.drive_devices(self.config(music='d3'), run=self.escaped)
        self.assertEqual(devices, ['device-aaaa-1111', 'device-bbbb-2222', 'device-cccc-3333'])

    def test_a_drive_nothing_is_placed_on_or_a_shared_folder_adds_no_device(self):
        self.assertEqual(
            server_os.drive_devices(self.config(), run=self.escaped), ['device-aaaa-1111', 'device-bbbb-2222']
        )
        # A shared folder has no device to appear: the NAS check runs drive-return for it.
        devices = server_os.drive_devices(self.config(music='nas1'), run=self.escaped)
        self.assertEqual(devices, ['device-aaaa-1111', 'device-bbbb-2222'])


class SharedFolderTests(unittest.TestCase):
    """Gate run C8: a folder of yours on a NAS, used where it is. The runtime is told which
    one, where it is mounted into the library and which apps see it, so those stay stopped
    while the NAS does not answer (shared_folders.rs); the every-minute check runs then."""

    def config(self, fstype='nfs4', services=('jellyfin', 'radarr', 'sonarr', 'prowlarr', 'qbittorrent')):
        entry = {
            'path': '/mnt/tank/films',
            'holds': 'movies',
            'use': 'in_place',
            'new_files': False,
            'mount': '/mnt/tank',
            'share': 'nas:/tank' if fstype.startswith('nfs') else '/dev/sdd1',
            'fstype': fstype,
        }
        return {
            'data_mount': DATA,
            'media_mount': FILMS,
            'storage': {
                'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'},
            },
            'media': {'services': list(services)},
            'placement': {'movies': {'install': 'ssd', 'data': 'media', 'extra': [entry]}},
        }

    def test_the_runtime_is_told_the_share_its_library_folder_and_the_apps_that_see_it(self):
        import native_runtime

        self.assertEqual(
            native_runtime.shared_folders(self.config()),
            [
                {
                    'holds': 'movies',
                    'path': '/mnt/tank/films',
                    'mount': '/mnt/tank',
                    'share': 'nas:/tank',
                    'point': FILMS + '/library/existing/movies',
                    'apps': ['jellyfin', 'radarr', 'sonarr', 'qbittorrent'],
                }
            ],
        )
        # Only the apps this server has.
        only = native_runtime.shared_folders(self.config(services=('jellyfin',)))
        self.assertEqual(only[0]['apps'], ['jellyfin'])
        # A folder on a drive of this PC is no share: its drive's own checks cover it.
        self.assertEqual(native_runtime.shared_folders(self.config(fstype='ext4')), [])
        with mock.patch('native_runtime.saved_paths', return_value={}):
            self.assertIn('shared_folders', native_runtime.storage_config(self.config()))
            self.assertNotIn('shared_folders', native_runtime.storage_config(self.config(fstype='ext4')))

    def test_the_every_minute_check_runs_on_a_server_with_one(self):
        self.assertTrue(server_os.on_a_nas(self.config()))
        self.assertFalse(server_os.on_a_nas(self.config(fstype='ext4')))


if __name__ == '__main__':
    unittest.main()
