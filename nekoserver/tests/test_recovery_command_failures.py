"""recovery_tools.command and command_problem: how a recovery step runs a program, and
what a person is told when that program is missing, hangs or fails (its output is never
shown). Also the fixed names the steps use: Docker's own socket and Immich's services."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_tools

DOCKER_LOOK = 'sudo systemctl status docker  and  sudo journalctl -u docker -n 50'


class ProblemWordsTests(unittest.TestCase):
    def test_a_missing_program_is_named_and_docker_says_how_to_install_it(self):
        self.assertEqual(
            recovery_tools.command_problem('docker', 'missing'),
            'The program "docker" is not installed on this PC, so this step cannot run yet. '
            'Setup installs Docker:  sudo bash install.sh  (after replacing Linux, install it with '
            'recover.py install-docker --apply instead: RECOVERY.md, step 4). Then run this command again.',
        )
        self.assertEqual(
            recovery_tools.command_problem('rsync', 'missing'),
            'The program "rsync" is not installed on this PC, so this step cannot run yet. '
            'Then run this command again.',
        )

    def test_a_program_that_hangs_is_waited_for_a_minute_and_says_where_to_look(self):
        self.assertEqual(
            recovery_tools.command_problem('docker', 'timeout'),
            '"docker" did not answer within 60 seconds. It may be busy (a backup or an update, say): '
            'wait a few minutes and run this command again. If it keeps happening, check it with  ' + DOCKER_LOOK,
        )
        self.assertEqual(
            recovery_tools.command_problem('systemctl', 'timeout', 180),
            '"systemctl" did not answer within 180 seconds. It may be busy (a backup or an update, say): '
            'wait a few minutes and run this command again. If it keeps happening, check it with  '
            'sudo systemctl --failed',
        )

    def test_a_program_that_fails_says_where_to_look(self):
        for name, look in (
            ('docker', DOCKER_LOOK),
            ('systemctl', 'sudo systemctl --failed'),
            ('pg_dump', 'sudo journalctl -n 50'),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    recovery_tools.command_problem(name, 'error'),
                    f'"{name}" reported an error, so this step stopped. See what is wrong with  {look}  '
                    'then run this command again.',
                )

    def test_recovery_md_has_the_step_the_docker_words_send_a_person_to(self):
        text = (ROOT / 'RECOVERY.md').read_text()
        self.assertIn('install-docker --apply', text)


class CommandTests(unittest.TestCase):
    def ran(self, **result):
        done = subprocess.CompletedProcess([], result.pop('returncode', 0), **result)
        return mock.patch.object(recovery_tools.subprocess, 'run', return_value=done)

    def test_the_output_is_given_back_as_text(self):
        with self.ran(stdout=b'3f2a\n') as run:
            self.assertEqual(recovery_tools.command(['docker', 'ps']), '3f2a\n')
        self.assertEqual(
            run.call_args,
            mock.call(
                ['docker', 'ps'],
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            ),
        )
        with self.ran(stdout='text\n'):
            self.assertEqual(recovery_tools.command(['x']), 'text\n')

    def test_a_real_program_runs_and_its_output_comes_back(self):
        self.assertEqual(recovery_tools.command([sys.executable, '-c', 'print("hi")']), 'hi\n')

    def test_output_sent_to_a_file_goes_there_and_nothing_comes_back(self):
        with tempfile.TemporaryFile() as out:
            self.assertEqual(recovery_tools.command([sys.executable, '-c', 'print("dump")'], stdout=out), '')
            out.seek(0)
            self.assertEqual(out.read(), b'dump\n')

    def test_each_way_a_program_fails_is_said_without_its_output(self):
        secret = 'POSTGRES_PASSWORD=hunter2'
        cases = [
            (FileNotFoundError(2, 'No such file'), recovery_tools.command_problem('docker', 'missing')),
            (
                subprocess.TimeoutExpired(['docker'], 90, output=secret),
                recovery_tools.command_problem('docker', 'timeout', 90),
            ),
            (PermissionError(13, secret), recovery_tools.command_problem('docker', 'error')),
        ]
        for exc, said in cases:
            with self.subTest(exc=type(exc).__name__):
                with (
                    mock.patch.object(recovery_tools.subprocess, 'run', side_effect=exc),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    recovery_tools.command(['docker', 'compose', 'config'], 90)
                self.assertEqual(str(raised.exception), said)
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_a_program_that_ends_with_an_error_code_stops_the_step(self):
        with (
            self.ran(returncode=1, stdout=b'POSTGRES_PASSWORD=hunter2', stderr=b'bad'),
            self.assertRaises(RuntimeError) as raised,
        ):
            recovery_tools.command(['docker', 'compose', 'config'])
        self.assertEqual(str(raised.exception), recovery_tools.command_problem('docker', 'error'))
        with self.assertRaises(RuntimeError) as raised:
            recovery_tools.command([sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertIn('reported an error', str(raised.exception))


class FixedNamesTests(unittest.TestCase):
    def test_docker_is_asked_on_its_own_system_socket(self):
        """Not wherever DOCKER_HOST or a docker context points (a rootless Docker, another
        machine): the recovery steps act on the server's own containers."""
        self.assertEqual(recovery_tools.DOCKER, ['docker', '--host', 'unix:///var/run/docker.sock'])

    def test_immichs_services_are_its_database_cache_and_the_apps_restart_plan(self):
        """The services a snapshot allows to run and restarts: the runtime restarts Immich
        as immich-server and immich-machine-learning (apps.rs restart_plan)."""
        apps = (ROOT / 'runtime' / 'src' / 'apps.rs').read_text()
        plan = re.search(r'"immich" => vec!\[([^\]]*)\]', apps).group(1)
        restarted = set(re.findall(r'"([^"]+)"', plan))
        self.assertEqual(restarted, {'immich-server', 'immich-machine-learning'})
        self.assertEqual(recovery_tools.SERVICES, restarted | {'database', 'redis'})

    def test_a_cut_off_snapshot_restarts_every_service_it_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            record = Path(folder) / 'resume.json'
            record.write_text(
                '{"format": "neko-immich-snapshot-resume-v1", '
                '"services": ["redis", "immich-server", "immich-machine-learning"]}'
            )
            running = {'database'}
            calls = []

            def command(argv, timeout=60):
                calls.append(argv)
                if argv[-1] == 'start' or 'start' in argv:
                    running.update(argv[argv.index('start') + 1 :])
                    return ''
                return '\n'.join(sorted(running))

            with (
                mock.patch.object(recovery_tools, 'SNAPSHOT_RESUME', record),
                mock.patch.object(recovery_tools, 'SNAPSHOT_HOOK', Path(folder) / 'hook.service'),
                mock.patch.object(recovery_tools, 'command', command),
            ):
                restarted = recovery_tools.restart_owed({}, base=['compose'], sleep=lambda s: self.fail('waited'))
            self.assertEqual(restarted, ['immich-machine-learning', 'immich-server', 'redis'])
            self.assertIn(['compose', 'start', 'immich-machine-learning', 'immich-server', 'redis'], calls)
            self.assertFalse(record.exists())


if __name__ == '__main__':
    unittest.main()
