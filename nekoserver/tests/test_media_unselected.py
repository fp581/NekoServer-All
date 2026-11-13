"""A media app taken out of media.services is stopped on the next setup run (its
containers removed with `docker compose down`, never its volumes); its settings, data
and Compose file stay, so adding it back starts it as it was."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import installer  # noqa: E402
from modules import media, media_monitor  # noqa: E402


class UnselectedAppTests(unittest.TestCase):
    def test_an_app_taken_out_of_the_list_is_stopped_and_keeps_its_files(self):
        with tempfile.TemporaryDirectory() as d:
            compose = Path(d) / 'compose'
            for name in ('jellyfin', 'qbittorrent', 'sonarr'):
                (compose / name).mkdir(parents=True)
                (compose / name / 'compose.yaml').write_text('services: {}\n')
            ran, logged = [], []
            media.stop_unselected(
                compose, ['jellyfin', 'sonarr'], logged.append, run=lambda argv, timeout: ran.append(argv)
            )
            self.assertEqual(
                ran,
                [
                    [
                        'docker',
                        'compose',
                        '-f',
                        str(compose / 'qbittorrent/compose.yaml'),
                        '-p',
                        'neko-qbittorrent',
                        'down',
                    ]
                ],
            )
            self.assertNotIn('-v', ran[0])
            self.assertTrue((compose / 'qbittorrent/compose.yaml').is_file())
            self.assertIn('qbittorrent: not among your apps any more', logged[0])

    def test_nothing_to_stop_when_every_installed_app_is_selected(self):
        with tempfile.TemporaryDirectory() as d:
            compose = Path(d) / 'compose'
            (compose / 'jellyfin').mkdir(parents=True)
            (compose / 'jellyfin/compose.yaml').write_text('services: {}\n')
            ran = []
            media.stop_unselected(compose, ['jellyfin'], print, run=lambda argv, timeout: ran.append(argv))
            self.assertEqual(ran, [])

    def test_a_rerun_of_the_deployment_stops_the_app_taken_out(self):
        with tempfile.TemporaryDirectory() as d:
            data, hdd = Path(d) / 'ssd', Path(d) / 'hdd'
            data.mkdir()
            hdd.mkdir()
            # qBittorrent was deployed by an earlier run, then taken out of the list.
            (data / 'compose/qbittorrent').mkdir(parents=True)
            (data / 'compose/qbittorrent/compose.yaml').write_text('services: {}\n')
            ran, up = [], []
            ctx = mock.Mock(
                apply=True,
                log=lambda line: None,
                config={
                    'data_mount': str(data),
                    'media_mount': str(hdd),
                    'timezone': 'UTC',
                    'media': {'services': ['jellyfin'], 'uid': 1000, 'gid': 1000},
                },
            )
            with (
                mock.patch.object(media.os, 'geteuid', return_value=0),
                mock.patch.object(media, 'check_mount', side_effect=lambda p: '/dev/' + Path(p).name),
                mock.patch.object(media, 'verify_storage'),
                mock.patch.object(media.os, 'chown'),
                mock.patch.object(media, 'host_timezone', return_value='UTC'),
                mock.patch.object(media.shutil, 'which', return_value='/usr/bin/docker'),
                mock.patch.object(media, 'network_exists', return_value=True),
                mock.patch.object(media, 'run_cmd', side_effect=lambda argv, timeout=120: ran.append(argv) or ''),
                mock.patch.object(media, 'compose_up', side_effect=lambda base, *a, **k: up.append(base)),
            ):
                media._run(ctx)
            self.assertEqual([b[b.index('-p') + 1] for b in up], ['neko-jellyfin'])
            downs = [argv for argv in ran if argv[-1] == 'down']
            self.assertEqual(
                downs,
                [
                    [
                        'docker',
                        'compose',
                        '-f',
                        str(data / 'compose/qbittorrent/compose.yaml'),
                        '-p',
                        'neko-qbittorrent',
                        'down',
                    ]
                ],
            )
            self.assertTrue((data / 'compose/qbittorrent/compose.yaml').is_file())


class MonitorRefreshTests(unittest.TestCase):
    def test_the_installed_monitor_follows_the_app_selection(self):
        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'scripts/config.json'
            config = {'data_mount': d, 'media': {'services': ['jellyfin', 'qbittorrent']}}
            logged = []
            self.assertFalse(media_monitor.refresh(config, logged.append))  # not installed: nothing
            saved.parent.mkdir()
            saved.write_text(json.dumps(config))
            config = {'data_mount': d, 'media': {'services': ['jellyfin', 'sonarr']}}
            self.assertTrue(media_monitor.refresh(config, logged.append))
            self.assertEqual(json.loads(saved.read_text())['media']['services'], ['jellyfin', 'sonarr'])
            self.assertFalse(media_monitor.refresh(config, logged.append))
        self.assertIn('media_monitor', installer.REFRESHED)


class LibraryFolderTests(unittest.TestCase):
    """Seen on the rig (0.47.5): the media drive was replaced, and the apps started before
    setup ran on it. Docker made the folders it was told to bind (movies, tv, downloads),
    root's and closed to the apps, and setup then left them as they "existed": Radarr,
    Sonarr, qBittorrent and Lidarr could write nothing, through every later run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Path(self.tmp.name) / 'library'
        self.library.mkdir()
        self.me, self.logged = os.getuid(), []

    def folder(self, name, mode=0o755):
        path = self.library / name
        path.mkdir(parents=True)
        path.chmod(mode)
        return path

    def handed_over(self, path):
        # The test's own account stands in for root, as the one Docker makes folders as.
        media.library_folder(path, self.me, os.getgid(), self.logged.append, root=self.me)
        return path.stat().st_mode & 0o7777 == 0o2775

    def test_a_folder_docker_made_is_handed_to_the_apps(self):
        downloads = self.folder('downloads')
        inside = self.folder('downloads/music')  # the music downloads' mount point, Docker's too
        # In setup's order: the music downloads' folder first, then the one it is in (the
        # first fix handed over only the inner one: the outer no longer held "Docker's" only).
        self.assertTrue(self.handed_over(inside))
        self.assertTrue(self.handed_over(downloads))
        self.assertEqual(len(self.logged), 2)
        self.assertIn('made by Docker', self.logged[0])

    def test_a_missing_folder_is_made_for_the_apps(self):
        self.assertTrue(self.handed_over(self.library / 'movies'))
        self.assertEqual(self.logged, [])

    def test_a_folder_with_anything_in_it_or_set_up_another_way_is_left_alone(self):
        films = self.folder('movies')
        (films / 'Film (2020).mkv').write_text('x')
        closed = self.folder('tv', 0o750)
        other = self.folder('downloads')
        self.assertFalse(self.handed_over(films))
        self.assertFalse(self.handed_over(closed))
        # Someone else's folder (not root's): theirs to keep.
        media.library_folder(other, self.me, os.getgid(), self.logged.append, root=self.me + 1)
        self.assertEqual(other.stat().st_mode & 0o7777, 0o755)
        self.assertEqual(self.logged, [])


if __name__ == '__main__':
    unittest.main()
