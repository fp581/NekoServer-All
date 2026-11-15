"""Standalone tools that stop say why, not only that they stopped (deep check plan 1).

Seen on a fresh Ubuntu with no drive connected and the runtime not built:
`acceptance.py --save` said "could not finish; inspect configuration and local services",
`source_import.py` said "unavailable; check configured mounts and source inventory".
The reason (the HDD not mounted, the runtime not built) was there, and thrown away."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance  # noqa: E402
import neko_common  # noqa: E402
import source_import  # noqa: E402

REFUSED = 'The HDD is not mounted at /mnt/nekoserver-media'


def said_on_stderr(call):
    """(exit code, what was written to stderr) of `call`."""
    said = io.StringIO()
    with contextlib.redirect_stderr(said), contextlib.redirect_stdout(io.StringIO()):
        code = call()
    return code, said.getvalue()


class StopReasonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / 'my-config.json'
        self.settings.write_text(json.dumps({'data_mount': tmp.name, 'media_mount': tmp.name}))

    def test_the_acceptance_check_says_why_it_could_not_finish(self):
        with (
            mock.patch.object(acceptance.os, 'geteuid', return_value=0),
            mock.patch.object(acceptance, 'checked_settings', lambda value: value),
            mock.patch.object(acceptance, 'inspect', side_effect=RuntimeError(REFUSED)),
        ):
            code, said = said_on_stderr(lambda: acceptance.main(['--config', str(self.settings), '--save']))
        self.assertEqual(code, 2)
        self.assertIn('Acceptance check could not finish: ' + REFUSED, said)
        self.assertIn('No application state was changed.', said)

    def test_source_import_says_why_it_is_unavailable(self):
        with (
            mock.patch.object(source_import, 'checked_settings', lambda value: value),
            mock.patch.object(source_import, 'verify_storage', side_effect=RuntimeError(REFUSED)),
        ):
            code, said = said_on_stderr(
                lambda: source_import.main(['--config', str(self.settings), '--sources', str(self.settings), '--apply'])
            )
        self.assertEqual(code, 2)
        self.assertIn('Source import stopped: ' + REFUSED, said)

    def test_a_missing_setting_is_named_as_one(self):
        """str() of a KeyError is the key alone ("'data_mount'"): it is said as a setting."""
        self.settings.write_text('{}')
        with mock.patch.object(source_import, 'checked_settings', lambda value: value):
            code, said = said_on_stderr(lambda: source_import.main(['--config', str(self.settings)]))
        self.assertEqual(code, 2)
        self.assertIn("the setting 'data_mount' is missing", said)


class RoomTests(unittest.TestCase):
    def test_a_small_restore_refused_for_the_reserve_says_so(self):
        """A restore of a few photos onto a nearly full drive said "the restore needs 0.0 GB
        and 0.1 GB is free": the 5 GB the drive must keep free is what refuses it."""
        gib, mib = neko_common.GIB, 1024**2
        with mock.patch('shutil.disk_usage', return_value=mock.Mock(total=100 * gib, free=100 * mib)):
            with self.assertRaises(RuntimeError) as caught:
                neko_common.ensure_room('/x', 3 * mib)
        said = str(caught.exception)
        self.assertNotIn('0.0 GB', said)
        self.assertIn('needs 3 MB', said)
        self.assertIn('must keep 5.0 GB free', said)
        self.assertIn('100 MB is free', said)


class TimeZoneTests(unittest.TestCase):
    def test_a_misspelt_time_zone_is_said_with_the_one_meant(self):
        """Deep check plan 99: "Europe/Londn" and "europe/london" were "not installed under
        /usr/share/zoneinfo", which sends a person looking for something to install."""
        for typed, meant in (('Europe/Londn', 'Europe/London'), ('europe/london', 'Europe/London')):
            with self.subTest(typed=typed), self.assertRaises(RuntimeError) as caught:
                neko_common.host_timezone({'timezone': typed})
            said = str(caught.exception)
            self.assertNotIn('not installed', said)
            self.assertIn(f'did you mean {meant}?', said)
            self.assertIn('timedatectl list-timezones', said)


class ClosedTerminalTests(unittest.TestCase):
    def test_immich_runs_again_when_the_terminal_closes_during_a_snapshot(self):
        """Deep check 452: `recover.py snapshot --apply` stops Immich while every photo is
        copied (hours) and starts it again in a finally block. A closed terminal or a
        dropped SSH connection (SIGHUP), or a `kill` (SIGTERM), ended the tool on the spot:
        Immich stayed off until the next restart of the PC, with nobody told. Run as a
        program of its own, the copy stood in for by a wait."""
        import signal
        import subprocess

        program = (
            'import os, signal, sys, time\n'
            f'sys.path.insert(0, {str(ROOT)!r})\n'
            'import recover, recovery_tools\n'
            'def snapshot(a, config):\n'
            '    with recovery_tools.restarted_after(config, ["compose"], ["immich-server"]):\n'
            '        print("Immich stopped", flush=True)\n'
            '        os.kill(os.getpid(), getattr(signal, sys.argv[1]))\n'
            '        time.sleep(30)\n'
            'recovery_tools.restart_owed = lambda config, base=None: print("Immich started again", flush=True)\n'
            'recover.run = snapshot\n'
            'recover.checked_settings = lambda value: value\n'
            'recover.read_settings = lambda path: ({}, None)\n'
            'sys.exit(recover.main(["check"]))\n'
        )
        for name in ('SIGHUP', 'SIGTERM'):
            with self.subTest(name):
                ran = subprocess.run(
                    [sys.executable, '-c', program, name],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_DFL),
                )
                self.assertEqual(ran.stdout.splitlines(), ['Immich stopped', 'Immich started again'], ran.stderr)
                self.assertEqual(ran.returncode, 2)
                self.assertIn('Recovery failed: stopped', ran.stderr)
                self.assertNotIn('Traceback', ran.stderr)


if __name__ == '__main__':
    unittest.main()
