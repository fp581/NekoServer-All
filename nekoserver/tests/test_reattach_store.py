"""After reinstalling Linux, reattaching the SSD's Docker store accepts Docker's own
empty folder tree in /var/lib/docker (the same test setup uses), and still refuses a
default store with containers, images or volumes in it."""
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recovery_tools  # noqa: E402


class ReattachDefaultStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.data = root / 'srv'
        (self.data / 'installer').mkdir(parents=True)
        (self.data / 'docker-engine/overlay2').mkdir(parents=True)
        self.config = {'data_mount': str(self.data), 'media_mount': str(root / 'media'),
                       'storage': {'data_ssd': {'uuid': 'aaaa-1111'}}, 'media': {'uid': 1000, 'gid': 1000}}
        checkpoint = {'format': 'neko-host-checkpoint-v1', 'architecture': platform.machine(), 'driver': 'overlay2',
                      'docker_root': str(self.data / 'docker-engine'), 'docker_version': '27.0.0',
                      'uid': 1000, 'gid': 1000, **{k: self.config[k] for k in ('storage', 'data_mount', 'media_mount')}}
        (self.data / 'installer/host-checkpoint.json').write_text(json.dumps(checkpoint))
        self.default = root / 'var-lib-docker'
        for sub in ('containers', 'volumes', 'image/overlay2/imagedb/content/sha256', 'network', 'overlay2', 'tmp'):
            (self.default / sub).mkdir(parents=True)
        (self.default / 'volumes/metadata.db').write_bytes(b'')
        real_safe = recovery_tools.safe_path
        places = {'/var/lib/docker': self.default, '/etc/docker/daemon.json': root / 'daemon.json',
                  str(recovery_tools.DOCKER_STORAGE_DROPIN): root / 'dropin.conf'}
        for patch in (
            mock.patch.object(recovery_tools, 'mounts'),
            mock.patch.object(recovery_tools, 'verify_bundle'),
            mock.patch.object(recovery_tools, 'host_supported', return_value=True),
            mock.patch.object(recovery_tools, 'command', return_value='Docker version 27.0.0'),
            mock.patch.object(recovery_tools.subprocess, 'run', return_value=mock.Mock(returncode=3)),
            mock.patch.object(recovery_tools.docker_module, 'prepare_daemon_config', return_value={}),
            mock.patch.object(recovery_tools, 'safe_path', side_effect=lambda p: places.get(str(p)) or real_safe(p)),
        ):
            patch.start()
        self.addCleanup(mock.patch.stopall)

    def preview(self):
        return recovery_tools.reattach(self.config, Path('/bundle'))

    def test_dockers_empty_folder_tree_is_not_data(self):
        self.assertEqual(self.preview()['status'], 'needs_setup')

    def test_containers_images_or_volumes_are_still_protected(self):
        for made in ('containers/0123abcd', 'volumes/photos', 'image/overlay2/imagedb/content/sha256/feed'):
            (self.default / made).mkdir()
            with self.assertRaisesRegex(RuntimeError, 'Default Docker store contains data'):
                self.preview()
            (self.default / made).rmdir()


if __name__ == '__main__':
    unittest.main()
