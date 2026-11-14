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


if __name__ == '__main__':
    unittest.main()
