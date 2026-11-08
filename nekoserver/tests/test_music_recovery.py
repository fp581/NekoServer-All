"""music-recovery.py: a music backup inventory checked, and restored into a new folder."""

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('music_recovery', ROOT / 'music-recovery.py')
music = importlib.util.module_from_spec(spec)
spec.loader.exec_module(music)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.backup = self.dir / 'music'
        (self.backup / 'snapshots').mkdir(parents=True)
        self.files = {
            'Album/01 - first.flac': b'first song',
            'Album/02 - second.flac': b'second song',
            'loose.mp3': b'x',
        }
        rows = []
        for name, payload in self.files.items():
            checksum = hashlib.sha256(payload).hexdigest()
            folder = self.backup / 'objects' / checksum[:2]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / checksum).write_bytes(payload)
            rows.append({'path': name, 'sha256': checksum, 'size': len(payload)})
        self.snapshot = self.backup / 'snapshots' / 'inventory.json'
        self.snapshot.write_text(json.dumps({'format': 'neko-music-backup-v1', 'files': rows}))

    def test_the_files_are_restored_into_a_new_folder_and_checked(self):
        out = self.dir / 'restored'
        self.assertEqual(music.restore(self.backup, self.snapshot, out), 3)
        for name, payload in self.files.items():
            self.assertEqual((out / name).read_bytes(), payload)
            self.assertEqual(oct((out / name).stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct((out / 'Album').stat().st_mode & 0o777), '0o700')
        # Without a destination, the inventory is only checked.
        self.assertEqual(music.restore(self.backup, self.snapshot), 3)
        with self.assertRaisesRegex(RuntimeError, 'new, separate restore directory'):
            music.restore(self.backup, self.snapshot, out)

    def test_a_folder_swapped_in_between_two_files_is_a_failure_not_a_count(self):
        """The review of 0.47.9: each file was written through a fresh lookup of its path,
        so a folder swapped in mid-restore (its parent may be writable by others) got the
        rest of the files while the count printed said all were restored."""
        out, moved = self.dir / 'restored', self.dir / 'moved-away'
        real = shutil.copyfileobj

        def swap(inp, stream, *args):
            real(inp, stream, *args)
            if not moved.exists():
                out.rename(moved)
                out.mkdir()

        with mock.patch.object(music.shutil, 'copyfileobj', swap):
            with self.assertRaisesRegex(RuntimeError, 'was replaced while the music was restored'):
                music.restore(self.backup, self.snapshot, out)
        # Every file went into the folder that was made, none into the one swapped in.
        self.assertEqual(
            sorted(p.name for p in moved.rglob('*') if p.is_file()),
            ['01 - first.flac', '02 - second.flac', 'loose.mp3'],
        )
        self.assertEqual(list(out.iterdir()), [])

    def test_a_damaged_object_stops_the_restore(self):
        checksum = hashlib.sha256(b'x').hexdigest()
        (self.backup / 'objects' / checksum[:2] / checksum).write_bytes(b'y')
        with self.assertRaisesRegex(RuntimeError, 'Music backup checksum mismatch'):
            music.restore(self.backup, self.snapshot, self.dir / 'restored')
        self.assertFalse((self.dir / 'restored').exists())

    def test_a_copy_that_reads_back_differently_is_refused(self):
        out = self.dir / 'restored'
        real = os.fdopen

        def tamper(fd, mode='r', *args, **kwargs):
            stream = real(fd, mode, *args, **kwargs)
            if mode == 'wb':
                stream.write(b'junk')
            return stream

        with mock.patch.object(music.os, 'fdopen', tamper):
            with self.assertRaisesRegex(RuntimeError, 'Restored music checksum mismatch'):
                music.restore(self.backup, self.snapshot, out)


if __name__ == '__main__':
    unittest.main()
