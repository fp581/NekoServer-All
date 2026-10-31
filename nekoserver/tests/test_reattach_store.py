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
        self.config = {
            'data_mount': str(self.data),
            'media_mount': str(root / 'media'),
            'storage': {'data_ssd': {'uuid': 'aaaa-1111'}},
            'media': {'uid': 1000, 'gid': 1000},
        }
        checkpoint = {
            'format': 'neko-host-checkpoint-v1',
            'architecture': platform.machine(),
            'driver': 'overlay2',
            'docker_root': str(self.data / 'docker-engine'),
            'docker_version': '27.0.0',
            'uid': 1000,
            'gid': 1000,
            **{k: self.config[k] for k in ('storage', 'data_mount', 'media_mount')},
        }
        (self.data / 'installer/host-checkpoint.json').write_text(json.dumps(checkpoint))
        self.daemon = root / 'daemon.json'
        self.default = root / 'var-lib-docker'
        for sub in ('containers', 'volumes', 'image/overlay2/imagedb/content/sha256', 'network', 'overlay2', 'tmp'):
            (self.default / sub).mkdir(parents=True)
        (self.default / 'volumes/metadata.db').write_bytes(b'')
        real_safe = recovery_tools.safe_path
        places = {
            '/var/lib/docker': self.default,
            '/etc/docker/daemon.json': root / 'daemon.json',
            str(recovery_tools.DOCKER_STORAGE_DROPIN): root / 'dropin.conf',
        }
        for patch in (
            mock.patch.object(recovery_tools, 'mounts'),
            mock.patch.object(recovery_tools, 'verify_bundle'),
            mock.patch.object(recovery_tools, 'host_supported', return_value=True),
            mock.patch.object(recovery_tools, 'command', return_value='Docker version 27.0.0'),
            mock.patch.object(recovery_tools.subprocess, 'run', return_value=mock.Mock(returncode=3)),
            mock.patch.object(recovery_tools.docker_module, 'prepare_daemon_config', return_value={}),
            mock.patch.object(recovery_tools, 'safe_path', side_effect=lambda p: places.get(str(p)) or real_safe(p)),
            mock.patch.object(recovery_tools.native_runtime, 'runs_here', return_value=True),
            mock.patch.object(recovery_tools.shutil, 'which', return_value='/usr/bin/docker'),
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

    def test_a_kept_program_that_does_not_run_here_stops_before_anything_changes(self):
        # Built on a newer Linux (Arch, glibc 2.39+) and kept on the SSD; Debian 12 can't start it.
        with (
            mock.patch.object(recovery_tools.native_runtime, 'runs_here', return_value=False),
            mock.patch.object(recovery_tools.docker_module, 'install_storage_guard') as guard,
        ):
            for apply in (False, True):
                with self.assertRaisesRegex(RuntimeError, 'does not start on this one.*sudo bash install.sh'):
                    recovery_tools.reattach(self.config, Path('/bundle'), apply, 'aaaa-1111')
        guard.assert_not_called()
        self.assertFalse(self.daemon.exists())

    def test_docker_not_installed_yet_names_the_install_step(self):
        with (
            mock.patch.object(recovery_tools.shutil, 'which', return_value=None),
            self.assertRaisesRegex(RuntimeError, 'recover.py install-docker --apply'),
        ):
            self.preview()

    def test_an_older_docker_names_the_install_step(self):
        with (
            mock.patch.object(recovery_tools, 'command', return_value='Docker version 20.10.24+dfsg1'),
            self.assertRaisesRegex(RuntimeError, '27.0.0 or newer.*install-docker --apply'),
        ):
            self.preview()

    def test_dockers_failed_start_count_is_cleared_before_it_starts(self):
        calls = []
        recovery_tools.subprocess.run.side_effect = lambda argv, **_: calls.append(argv) or mock.Mock(returncode=3)
        recovery_tools.command.side_effect = lambda argv, *a, **k: calls.append(argv) or 'Docker version 27.0.0'
        lock = mock.MagicMock()
        with (
            mock.patch.object(recovery_tools, 'require_root'),
            mock.patch.object(recovery_tools, 'lock', return_value=lock),
            mock.patch.object(recovery_tools.docker_module, 'install_storage_guard'),
            mock.patch.object(recovery_tools, 'atomic_write'),
            mock.patch.object(recovery_tools, 'checkpoint', return_value={'docker_version': '27.0.0'}),
        ):
            recovery_tools.reattach(self.config, Path('/bundle'), True, 'aaaa-1111')
        reset = calls.index(['systemctl', 'reset-failed', 'docker.service', 'docker.socket'])
        self.assertLess(reset, calls.index(['systemctl', 'enable', '--now', 'docker']))


class InstallDockerTests(unittest.TestCase):
    def test_dockers_packages_are_installed_as_setup_does_and_left_stopped(self):
        family = mock.Mock(label='Debian / Ubuntu')
        family.install.return_value = ['docker', 'docker-compose']
        config = {'data_mount': '/srv/nekoserver'}
        preview = recovery_tools.install_docker(config, apply=False, family=family)
        self.assertIn('--apply', preview['reason'])
        family.install.assert_not_called()
        with (
            mock.patch.object(recovery_tools, 'require_root'),
            mock.patch.object(recovery_tools.subprocess, 'run', return_value=mock.Mock(returncode=3)),
        ):
            done = recovery_tools.install_docker(config, apply=True, family=family)
        family.install.assert_called_once_with(['docker', 'docker-compose'])
        self.assertIn('/srv/nekoserver/installer/releases/', done['reason'])
        self.assertIn('reattach', done['reason'])
        # One that runs anyway (started by something else) is named, with how to stop it.
        with (
            mock.patch.object(recovery_tools, 'require_root'),
            mock.patch.object(recovery_tools.subprocess, 'run', return_value=mock.Mock(returncode=0)),
            self.assertRaisesRegex(RuntimeError, 'systemctl stop docker.service docker.socket'),
        ):
            recovery_tools.install_docker(config, apply=True, family=family)


class RecoverCheckTests(unittest.TestCase):
    def test_check_says_when_the_kept_program_does_not_run_here(self):
        import recover

        with (
            mock.patch.object(recover.recovery, 'mounts'),
            mock.patch.object(recover.recovery, 'verify_bundle', return_value='0.39.5'),
            mock.patch.object(recover.recovery.native_runtime, 'runs_here', return_value=False),
        ):
            result = recover.check({}, Path('/bundle'))
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('sudo bash install.sh', result['reason'])
        with (
            mock.patch.object(recover.recovery, 'mounts'),
            mock.patch.object(recover.recovery, 'verify_bundle', return_value='0.39.5'),
            mock.patch.object(recover.recovery.native_runtime, 'runs_here', return_value=True),
        ):
            self.assertEqual(recover.check({}, Path('/bundle'))['status'], 'pass')


if __name__ == '__main__':
    unittest.main()
