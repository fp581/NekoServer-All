"""recover.py, the recovery commands RECOVERY.md gives (check, checkpoint, reattach,
snapshot, restore-test, resume, install-docker): each command's options, which recovery
step it runs with what, what it prints, its exit code, and its words when it stops."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recover  # noqa: E402

SETTINGS = {'data_mount': '/srv/nekoserver'}
CONFIG = '/root/my-config.json'


class Ran:
    """recover.main with the recovery steps recorded instead of run."""

    def __init__(self, test, result=None, settings=SETTINGS, problem=None):
        self.test, self.result = test, result if result is not None else {'status': 'pass'}
        self.settings, self.problem = settings, problem

    def __call__(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        steps = mock.MagicMock()
        for name in (
            'mounts',
            'verify_bundle',
            'runtime_problem',
            'checkpoint',
            'reattach',
            'snapshot',
            'resume',
            'install_docker',
            'restore_test',
        ):
            getattr(steps, name).return_value = self.result
        steps.verify_bundle.return_value = '1.0.0'
        steps.runtime_problem.return_value = None

        def read(path):
            if self.problem:
                raise RuntimeError(self.problem)
            self.test.assertEqual(path, CONFIG)
            return dict(self.settings)

        with (
            mock.patch.object(recover, 'recovery', steps),
            mock.patch.object(recover.neko_common, 'read_settings', read),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = recover.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        self.steps, self.out, self.err = steps, out.getvalue(), err.getvalue()
        return code


class CommandsTests(unittest.TestCase):
    def test_each_command_runs_its_step_with_what_was_typed(self):
        bundle = Path(recover.__file__).parent
        cases = [
            (['check'], 'verify_bundle', ((bundle, SETTINGS), {})),
            (
                ['check', '--bundle', '/media/usb/nekoserver'],
                'verify_bundle',
                ((Path('/media/usb/nekoserver'), SETTINGS), {}),
            ),
            (['checkpoint'], 'checkpoint', ((SETTINGS, False), {})),
            (['checkpoint', '--apply'], 'checkpoint', ((SETTINGS, True), {})),
            (['reattach'], 'reattach', ((SETTINGS, bundle, False, ''), {})),
            (['reattach', '--apply', '--confirm', 'UUID-1'], 'reattach', ((SETTINGS, bundle, True, 'UUID-1'), {})),
            (
                ['snapshot', '--destination', '/srv/x/s1'],
                'snapshot',
                ((SETTINGS, Path('/srv/x/s1'), False, CONFIG), {}),
            ),
            (['resume'], 'resume', ((SETTINGS,), {})),
            (['install-docker'], 'install_docker', ((SETTINGS, False), {})),
            (['install-docker', '--apply'], 'install_docker', ((SETTINGS, True), {})),
            (
                ['restore-test', '--snapshot', '/s', '--workspace', '/w'],
                'restore_test',
                ((SETTINGS, Path('/s'), Path('/w'), 2284, False, ''), {}),
            ),
            (
                [
                    'restore-test',
                    '--snapshot',
                    '/s',
                    '--workspace',
                    '/w',
                    '--port',
                    '2300',
                    '--apply',
                    '--confirm',
                    'RESTORE-ISOLATED',
                ],
                'restore_test',
                ((SETTINGS, Path('/s'), Path('/w'), 2300, True, 'RESTORE-ISOLATED'), {}),
            ),
        ]
        for argv, step, call in cases:
            with self.subTest(argv=argv):
                ran = Ran(self)
                self.assertEqual(ran('--config', CONFIG, *argv), 0, ran.err)
                self.assertEqual(getattr(ran.steps, step).call_args, mock.call(*call[0], **call[1]))

    def test_the_settings_may_be_named_after_the_command_too(self):
        ran = Ran(self)
        self.assertEqual(ran('resume', '--config', CONFIG), 0, ran.err)
        ran.steps.resume.assert_called_once_with(SETTINGS)

    def test_options_a_command_does_not_have_are_refused(self):
        for argv in (
            ['check', '--apply'],
            ['resume', '--apply'],
            ['checkpoint', '--bundle', '/x'],
            ['snapshot', '--destination', '/x', '--confirm', 'y'],
            ['checkpoint', '--confirm', 'y'],
            ['snapshot'],
            ['restore-test', '--workspace', '/w'],
            ['restore-test', '--snapshot', '/s'],
            [],
        ):
            with self.subTest(argv=argv):
                ran = Ran(self)
                self.assertEqual(ran('--config', CONFIG, *argv), 2)
                self.assertIn('error:', ran.err)

    def test_each_command_says_what_it_does_in_its_help(self):
        for name, what in recover.COMMANDS.items():
            with self.subTest(name=name):
                ran = Ran(self)
                self.assertEqual(ran(name, '--help'), 0)
                self.assertIn(what.split(';')[0][:40], ' '.join(ran.out.split()))
        ran = Ran(self)
        ran('--help')
        self.assertIn('sudo bash install.sh --recover', ran.out)
        ran = Ran(self)
        ran('reattach', '--help')
        self.assertIn('With --apply: the UUID of the app SSD', ' '.join(ran.out.split()))
        ran = Ran(self)
        ran('restore-test', '--help')
        self.assertIn('With --apply: the word RESTORE-ISOLATED', ' '.join(ran.out.split()))


class ResultsTests(unittest.TestCase):
    def test_the_result_is_printed_and_waiting_for_setup_is_exit_code_one(self):
        for status, code in (('pass', 0), ('needs_setup', 1), ('preview', 0)):
            with self.subTest(status=status):
                ran = Ran(self, result={'status': status, 'reason': 'x'})
                self.assertEqual(ran('--config', CONFIG, 'resume'), code)
                self.assertEqual(ran.out, json.dumps({'status': status, 'reason': 'x'}, indent=2) + '\n')

    def test_check_says_it_is_no_restore_acceptance_or_what_the_program_needs(self):
        steps = mock.MagicMock()
        steps.verify_bundle.return_value = '1.0.0'
        steps.runtime_problem.return_value = None
        with mock.patch.object(recover, 'recovery', steps):
            self.assertEqual(
                recover.check(SETTINGS, Path('/b')),
                {
                    'status': 'pass',
                    'reason': 'Storage identities and preserved installer verified; this is not restore acceptance',
                    'bundle_version': '1.0.0',
                },
            )
            steps.mounts.assert_called_once_with(SETTINGS)
            steps.runtime_problem.return_value = 'The program does not run on this Linux'
            self.assertEqual(
                recover.check(SETTINGS, Path('/b')),
                {
                    'status': 'needs_setup',
                    'reason': 'The program does not run on this Linux',
                    'bundle_version': '1.0.0',
                },
            )
        steps.runtime_problem.assert_called_with(Path('/b'))

    def test_checkpoint_says_whether_it_was_saved_and_what_to_do_next(self):
        steps = mock.MagicMock()
        steps.checkpoint.return_value = {'status': 'pass', 'docker_root': '/srv/nekoserver/docker'}
        with mock.patch.object(recover, 'recovery', steps):
            self.assertEqual(
                recover.checkpoint(SETTINGS, True),
                {
                    'status': 'pass',
                    'docker_root': '/srv/nekoserver/docker',
                    'reason': 'Saved on the SSD as installer/host-checkpoint.json. Keep a copy off this PC too '
                    '(RECOVERY.md, "Prepare while the current server works").',
                },
            )
            self.assertEqual(
                recover.checkpoint(SETTINGS, False)['reason'],
                'Preview only: nothing was saved. To save it, run the same command with  --apply  added.',
            )


class StoppedTests(unittest.TestCase):
    def test_a_settings_file_that_cannot_be_used_is_named_and_nothing_runs(self):
        ran = Ran(self, problem='/root/my-config.json is cut off')
        self.assertEqual(ran('--config', CONFIG, 'check'), 2)
        self.assertEqual(ran.err, 'Recovery stopped: /root/my-config.json is cut off.\n' + recover.AFTER_FAILURE + '\n')
        ran.steps.mounts.assert_not_called()

    def test_a_step_that_fails_says_so_and_that_nothing_was_overwritten(self):
        ran = Ran(self)
        ran.result = None
        with mock.patch.object(recover, 'run', side_effect=RuntimeError('The app SSD is not mounted')):
            self.assertEqual(ran('--config', CONFIG, 'check'), 2)
        self.assertEqual(ran.err, 'Recovery failed: The app SSD is not mounted\n' + recover.AFTER_FAILURE + '\n')
        self.assertEqual(
            recover.AFTER_FAILURE,
            'Nothing was formatted, and no backup or snapshot was overwritten. Fix what it names and run the same '
            'command again. The guided menu for getting files back is  sudo bash install.sh --recover ; '
            'RECOVERY.md explains each command.',
        )

    def test_each_kind_of_failure_is_said_without_its_details(self):
        import subprocess

        from installer import InstallerError

        cases = [
            (InstallerError('Data and media mounts must be separate'), 'Data and media mounts must be separate'),
            (KeyboardInterrupt(), 'stopped with Ctrl+C.'),
            (
                FileNotFoundError(2, 'No such file or directory', '/srv/x'),
                'a file or folder it needs does not exist: /srv/x. Check the path. A folder it is to put things in '
                'must exist already: make it first with  sudo mkdir -p  followed by the folder.',
            ),
            (PermissionError(13, 'Permission denied', '/srv/x'), 'Permission denied (/srv/x).'),
            (OSError(5, 'Input/output error'), 'Input/output error.'),
            (OSError('no number'), 'a file could not be read or written.'),
            (
                KeyError('data_mount'),
                'the settings have no "data_mount" entry. Use the settings setup made (--config /path/to/my-config.json).',
            ),
            (
                KeyError(),
                'the settings have no "" entry. Use the settings setup made (--config /path/to/my-config.json).',
            ),
            (
                ValueError('password=hunter2'),
                'a settings or record file could not be read (it is damaged, or not the file this expects).',
            ),
            (
                TypeError('x'),
                'a settings or record file could not be read (it is damaged, or not the file this expects).',
            ),
            (subprocess.TimeoutExpired(['docker', 'compose', 'up'], 30), 'a program it ran did not finish.'),
        ]
        for exc, said in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(recover.failure_text(exc), said)
        for exc, _ in cases:
            with self.subTest(main=type(exc).__name__):
                ran = Ran(self)
                with mock.patch.object(recover, 'run', side_effect=exc):
                    self.assertEqual(ran('--config', CONFIG, 'resume'), 2)
                self.assertTrue(ran.err.startswith('Recovery failed: '), ran.err)


class GuideTests(unittest.TestCase):
    def test_recovery_md_lists_the_same_commands(self):
        """RECOVERY.md, "What each operation does": a command missing there, or one there
        that recover.py does not have, sends a person after a dead drive the wrong way."""
        text = (ROOT / 'RECOVERY.md').read_text()
        section = text.split('### What each operation does', 1)[1].split('\n### ', 1)[0]
        rows = [line for line in section.splitlines() if line.startswith('| ')][2:]  # past the head and its rule
        listed = [line.split('|')[1].strip() for line in rows]
        self.assertEqual(listed, list(recover.COMMANDS))


if __name__ == '__main__':
    unittest.main()
