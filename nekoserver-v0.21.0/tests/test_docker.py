import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules import docker

class DockerFoundationTests(unittest.TestCase):
    def test_reject_root_path(self):
        with self.assertRaisesRegex(RuntimeError, 'dedicated absolute'):
            docker.mounted_data_root('/')
    def test_reject_missing_path(self):
        with self.assertRaisesRegex(RuntimeError, 'does not exist'):
            docker.mounted_data_root('/never-existing-nekoserver-volume-12999')
    def test_reject_os_root_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(docker, 'call', return_value='/'):
                with self.assertRaisesRegex(RuntimeError, 'OS filesystem'):
                    docker.mounted_data_root(temp)
    def test_allow_separate_volume(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(docker, 'call', side_effect=[temp, '/dev/sdx1']):
                self.assertEqual(docker.mounted_data_root(temp), (temp, '/dev/sdx1'))

    def test_existing_conflicting_daemon_root_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'daemon.json'
            path.write_text('{"data-root":"/elsewhere","log-driver":"json-file"}')
            with self.assertRaisesRegex(RuntimeError, 'manual migration'):
                docker.prepare_daemon_config(path, Path(temp) / 'docker-engine')
    def test_daemon_configuration_preserves_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'daemon.json'
            path.write_text('{"log-driver":"json-file"}')
            result = docker.prepare_daemon_config(path, Path(temp) / 'docker-engine')
            self.assertEqual(result['log-driver'], 'json-file')
            self.assertEqual(result['data-root'], str(Path(temp) / 'docker-engine'))
    def test_reject_child_of_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(docker, 'call', return_value='/mnt'):
                with self.assertRaisesRegex(RuntimeError, 'itself be a mountpoint'):
                    docker.mounted_data_root(temp)

if __name__ == '__main__':
    unittest.main()


class ExistingEngineComposeTests(unittest.TestCase):
    def test_existing_plugin_does_not_install(self):
        from types import SimpleNamespace
        with patch.object(docker.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), patch.object(docker, 'call') as call:
            docker.ensure_active_compose({})
            call.assert_not_called()

    def test_missing_plugin_is_installed_and_verified(self):
        from types import SimpleNamespace
        with patch.object(docker.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), patch.object(docker.shutil, 'which', return_value='/fake/pacman'), patch.object(docker.platform, 'system', return_value='Linux'), patch.object(docker, 'call') as call:
            docker.ensure_active_compose({'install_engine': True})
            self.assertEqual(call.call_args_list[0].args[0], ['pacman', '-Si', 'docker-compose'])
            self.assertEqual(call.call_args_list[1].args[0], ['pacman', '-S', '--needed', '--noconfirm', 'docker-compose'])
            self.assertEqual(call.call_args_list[2].args[0], ['docker', 'compose', 'version'])

    def test_missing_plugin_without_permission_stops(self):
        from types import SimpleNamespace
        with patch.object(docker.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), patch.object(docker, 'call') as call:
            with self.assertRaisesRegex(RuntimeError, 'Compose missing'):
                docker.ensure_active_compose({'install_engine': False})
            call.assert_not_called()
