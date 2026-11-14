"""monthly-recovery.py --stage: a monthly snapshot unpacked into a new folder, checked
against its archives, then marked as checked."""

import hashlib
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
        self.checked = {
            name: hashlib.sha256((self.snapshot / (name + '.tar')).read_bytes()).hexdigest()
            for name in ('data', 'media')
        }
        # verify's answer: the manifest, as it was when the archives were checked.
        manifest = {'sha256': self.checked}
        for target, value in (('verify', lambda s: manifest), ('ensure_room', lambda *a: None)):
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

    def test_a_missing_folder_above_the_destination_is_said_in_plain_words(self):
        # Seen on the test VM: --stage /mnt/.../restored/stage with no "restored" folder yet
        # ended in a traceback from the room check.
        with self.assertRaisesRegex(RuntimeError, 'folder above the recovery directory does not exist'):
            monthly.stage(self.snapshot, self.dir / 'nowhere' / 'staged')
        self.assertFalse((self.dir / 'nowhere').exists())

    def test_an_archive_changed_after_its_check_is_never_unpacked_or_marked(self):
        """The review of 0.47.2: unpacking and the comparison read the archive again by its
        name, so one changed after its checksum check was unpacked, compared with itself, and
        the copy marked checked."""
        archive(self.snapshot / 'media.tar', {'photo.jpg': b'something else'})  # after the check
        out = self.dir / 'staged'
        with self.assertRaisesRegex(RuntimeError, 'changed after it was checked'):
            monthly.stage(self.snapshot, out)
        self.assertFalse((out / 'verified.json').exists())
        self.assertFalse((out / 'media/photo.jpg').exists())
        self.assertEqual([p.name for p in out.iterdir() if p.name.startswith('.')], [])

    def test_unpacking_and_the_comparison_read_one_checked_private_copy(self):
        out = self.dir / 'staged'
        monthly.stage(self.snapshot, out)
        tars = [argv for argv in self.calls if argv[0] == 'tar']
        self.assertEqual(len(tars), 4)
        for argv in tars:
            source = argv[argv.index('--file') + 1]
            self.assertTrue(source.startswith('/proc/self/fd/'), argv)
            self.assertNotIn(str(self.snapshot), ' '.join(argv))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['data', 'media', 'verified.json'])

    def test_the_images_are_loaded_from_a_checked_private_copy(self):
        images = self.snapshot / 'images.tar'
        images.write_bytes(b'images')
        manifest = {'sha256': {'images': hashlib.sha256(b'images').hexdigest()}}
        seen = []

        def docker(argv, **kwargs):
            if 'info' in argv:
                return mock.Mock(stdout=str(self.dir) + '\n')
            source = argv[argv.index('--input') + 1]
            seen.append((source, os.read(kwargs['pass_fds'][0], 100)))
            return mock.Mock(returncode=0)

        with mock.patch.object(monthly.subprocess, 'run', docker):
            monthly.load_images(self.snapshot, manifest)
            self.assertEqual(len(seen), 1)
            self.assertTrue(seen[0][0].startswith('/proc/self/fd/'))
            self.assertEqual(seen[0][1], b'images')
            self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith('.neko-images-')], [])
            # Changed after its check: nothing is loaded.
            images.write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError, 'changed after it was checked'):
                monthly.load_images(self.snapshot, manifest)
        self.assertEqual(len(seen), 1)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith('.neko-images-')], [])

    def test_a_copy_that_fails_part_way_leaves_nothing_behind_and_keeps_its_own_error(self):
        """The review of 0.47.9: a read error from the backup drive left the partial copy in
        the hidden work folder, so removing the folder failed with "Directory not empty" and
        that error replaced the real one; every retry left another folder."""
        images = self.snapshot / 'images.tar'
        images.write_bytes(b'images')
        manifest = {'sha256': {'images': hashlib.sha256(b'images').hexdigest()}}

        class Failing(io.BytesIO):
            def read(self, size=-1):
                raise OSError(5, 'Input/output error')

        real_open = Path.open
        failing = {'images.tar'}

        def open_or_fail(path, *args, **kwargs):
            return Failing() if path.name in failing else real_open(path, *args, **kwargs)

        def docker(argv, **kwargs):
            return mock.Mock(stdout=str(self.dir) + '\n', returncode=0)

        with mock.patch.object(monthly.subprocess, 'run', docker), mock.patch.object(Path, 'open', open_or_fail):
            with self.assertRaisesRegex(OSError, 'Input/output error'):
                monthly.load_images(self.snapshot, manifest)
            # The same for a part of a staged copy (data.tar, read by stage).
            failing.add('data.tar')
            with self.assertRaisesRegex(OSError, 'Input/output error'):
                monthly.stage(self.snapshot, self.dir / 'staged')
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith('.neko-images-')], [])
        self.assertEqual([p.name for p in (self.dir / 'staged').iterdir() if p.name.startswith('.')], [])

    def test_a_full_or_read_only_drive_is_said_in_plain_words_not_a_traceback(self):
        """Seen through the recovery menu: a restore onto a drive that was full, read-only,
        or too small for the reserve ended in a Python traceback, and nothing else was said."""
        out = self.dir / 'staged'
        for error, reason in (
            (OSError(28, 'No space left on device'), 'No space left on device'),
            (OSError(30, 'Read-only file system', str(out)), f'Read-only file system ({out})'),
            (RuntimeError('not enough free space: free some space'), 'not enough free space'),
        ):
            with self.subTest(reason=reason):
                said = io.StringIO()

                def partly(snapshot, destination, error=error):
                    """The stage stopping part-way, with a folder of what was unpacked."""
                    Path(destination).mkdir(exist_ok=True)
                    raise error

                with (
                    mock.patch.object(monthly, 'stage', partly),
                    mock.patch.object(monthly.os, 'geteuid', return_value=0),
                    mock.patch.object(monthly.sys, 'stderr', said),
                ):
                    code = monthly.main(['--snapshot', str(self.snapshot), '--stage', str(out)])
                self.assertEqual(code, 1)
                self.assertIn('The monthly backup was not restored: ' + reason, said.getvalue())
                self.assertIn(f'is in {out} (incomplete', said.getvalue())
                self.assertNotIn('Traceback', said.getvalue())
                out.rmdir()


class CommandLineTests(unittest.TestCase):
    """What the recovery menu shows of the tool: its own lines, never a traceback."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.snapshot = self.dir / '1700000000000000-1'
        self.snapshot.mkdir()
        sums = {}
        for name in ('data', 'media', 'images'):
            archive(self.snapshot / (name + '.tar'), {name + '.txt': name.encode()})
            sums[name] = hashlib.sha256((self.snapshot / (name + '.tar')).read_bytes()).hexdigest()
        manifest = {'format': 'neko-maintenance-v1', 'sha256': sums}
        (self.snapshot / 'manifest.json').write_text(json.dumps(manifest))

    def run_tool(self, *argv):
        err = io.StringIO()
        with mock.patch.object(monthly.os, 'geteuid', return_value=0), mock.patch.object(monthly.sys, 'stderr', err):
            code = monthly.main(['--snapshot', str(self.snapshot), *argv])
        return code, err.getvalue()

    def test_a_damaged_snapshot_is_said_in_plain_words_and_nothing_is_unpacked(self):
        """Seen on the loop drives of 0.51.2: a byte changed in data.tar on the HDD ended
        the recovery menu's restore in a Python traceback ("Snapshot checksum mismatch:
        data"), and the menu said nothing after it."""
        data = self.snapshot / 'data.tar'
        body = bytearray(data.read_bytes())
        body[600] ^= 0xFF
        data.write_bytes(bytes(body))
        code, said = self.run_tool('--stage', str(self.dir / 'staged'))
        self.assertEqual(code, 1)
        self.assertIn('The monthly snapshot 1700000000000000-1 is damaged: its file data.tar', said)
        self.assertIn('Choose another monthly snapshot', said)
        self.assertNotIn('Traceback', said)
        self.assertFalse((self.dir / 'staged').exists())

    def test_a_missing_folder_above_the_stage_is_said_without_a_traceback(self):
        code, said = self.run_tool('--stage', str(self.dir / 'nowhere' / 'staged'))
        self.assertEqual(code, 1)
        self.assertIn('folder above the recovery directory does not exist', said)


if __name__ == '__main__':
    unittest.main()
