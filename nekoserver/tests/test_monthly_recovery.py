"""monthly-recovery.py --stage: a monthly snapshot unpacked into a new folder, checked
against its archives, then marked as checked."""

import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('monthly_recovery', ROOT / 'monthly-recovery.py')
monthly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monthly)


def archive(path, files):
    with tarfile.open(path, 'w') as bundle:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.uid = info.gid = os.getuid()
            bundle.addfile(info, io.BytesIO(payload))


@unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'needs /proc')
class StageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.snapshot = self.dir / 'snapshot'
        self.snapshot.mkdir()
        archive(self.snapshot / 'data.tar', {'app/db.sql': b'data'})
        archive(self.snapshot / 'media.tar', {'photo.jpg': b'media'})
        for target, value in (('verify', lambda s: {}), ('ensure_room', lambda *a: None)):
            patcher = mock.patch.object(monthly, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Ownership kept as root does it; as another account tar can't, so it is dropped.
        real = subprocess.run
        self.calls = []

        def run(argv, **kwargs):
            if os.geteuid() != 0:
                argv = [a for a in argv if a not in ('--same-owner', '--acls', '--xattrs')]
            self.calls.append(argv)
            return real(argv, **kwargs)

        patcher = mock.patch.object(monthly.subprocess, 'run', run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_snapshot_is_unpacked_checked_and_marked(self):
        out = self.dir / 'staged'
        monthly.stage(self.snapshot, out)
        self.assertEqual((out / 'data/app/db.sql').read_bytes(), b'data')
        self.assertEqual((out / 'media/photo.jpg').read_bytes(), b'media')
        self.assertIs(json.loads((out / 'verified.json').read_text())['live_data_replaced'], False)
        self.assertFalse((out / 'verified.json.tmp').exists())

    def test_a_folder_swapped_in_between_the_parts_is_never_marked_checked(self):
        """0.45.8: the folder was named by its path for each part, so one swapped in after
        the first part got the second part and the record that the copy was complete."""
        out, moved = self.dir / 'staged', self.dir / 'moved-away'
        real = monthly.subprocess.run

        def swap(argv, **kwargs):
            done = real(argv, **kwargs)
            if argv[1] == '--compare' and not moved.exists():
                out.rename(moved)
                out.mkdir()
                (out / 'media').mkdir()
            return done

        with mock.patch.object(monthly.subprocess, 'run', swap):
            with self.assertRaisesRegex(RuntimeError, 'was replaced while the snapshot was unpacked'):
                monthly.stage(self.snapshot, out)
        self.assertFalse((out / 'verified.json').exists())
        self.assertEqual(list((out / 'media').iterdir()), [])
        # Both parts went into the folder that was made, never marked complete.
        self.assertEqual((moved / 'media/photo.jpg').read_bytes(), b'media')
        self.assertFalse((moved / 'verified.json').exists())


if __name__ == '__main__':
    unittest.main()
