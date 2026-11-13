"""0.50.0 gate run A: with Docker stopped, the connecting step said each app was
"unavailable" and blamed Jellyfin's setup. It now says Docker is down first, in words the
retry page has a fix for."""

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import known_fixes  # noqa: E402
from modules import onboarding  # noqa: E402


class DockerDownTests(unittest.TestCase):
    def test_docker_that_is_down_is_said_with_its_fix(self):
        down = mock.Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='Cannot connect'))
        with self.assertRaises(RuntimeError) as stopped:
            onboarding.docker_running(down)
        self.assertEqual(known_fixes.fix_for(str(stopped.exception))['id'], 'docker_down')
        missing = mock.Mock(side_effect=FileNotFoundError('docker'))
        with self.assertRaises(RuntimeError):
            onboarding.docker_running(missing)
        hung = mock.Mock(side_effect=subprocess.TimeoutExpired('docker', 60))
        with self.assertRaises(RuntimeError):
            onboarding.docker_running(hung)
        # Docker answering: nothing is said.
        onboarding.docker_running(mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='29.0', stderr='')))


if __name__ == '__main__':
    unittest.main()
