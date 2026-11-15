"""music-recovery.py: a music backup inventory checked, and restored into a new folder."""

import errno
import hashlib
import io
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
        real = music.copy_checked_into

        def swap(*args, **kwargs):
            landed = real(*args, **kwargs)
            if not moved.exists():
                out.rename(moved)
                out.mkdir()
            return landed

        with mock.patch.object(music, 'copy_checked_into', swap):
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
            if mode == 'w+b':
                stream.write(b'junk')
            return stream

        with mock.patch.object(music.os, 'fdopen', tamper):
            with self.assertRaisesRegex(RuntimeError, 'does not match what was read'):
                music.restore(self.backup, self.snapshot, out)

    def test_each_file_is_written_out_and_read_back_from_the_drive(self):
        """Review of 0.51.2 (P2): the copy was read back straight after the write, with no
        fsync, so Linux answered from its memory and not from the drive."""
        calls = []
        real_sync, real_advise = os.fsync, os.posix_fadvise
        with (
            mock.patch.object(os, 'fsync', lambda fd: (calls.append('fsync'), real_sync(fd))[1]),
            mock.patch.object(os, 'posix_fadvise', lambda *a: (calls.append('drop'), real_advise(*a))[1]),
        ):
            self.assertEqual(music.restore(self.backup, self.snapshot, self.dir / 'restored'), 3)
        self.assertEqual(calls, ['fsync', 'drop'] * 3)

    def refusing_colons(self, errno_number):
        """os.open as an exFAT drive answers it: a name with ':' is refused."""
        real = os.open

        def refuse(path, *args, **kwargs):
            if ':' in str(path):
                raise OSError(errno_number, os.strerror(errno_number), str(path))
            return real(path, *args, **kwargs)

        return mock.patch.object(music.os, 'open', refuse)

    def test_names_the_drive_refuses_are_named_and_the_rest_is_restored(self):
        """Seen on an exFAT loop drive in 0.51.2: 'a:b.mp3' stopped the restore half-way
        with a Python traceback, leaving some files restored and nothing saying which."""
        payload = b'colon song'
        checksum = hashlib.sha256(payload).hexdigest()
        (self.backup / 'objects' / checksum[:2]).mkdir(exist_ok=True)
        (self.backup / 'objects' / checksum[:2] / checksum).write_bytes(payload)
        value = json.loads(self.snapshot.read_text())
        value['files'].insert(0, {'path': 'Album/a:b.mp3', 'sha256': checksum, 'size': len(payload)})
        self.snapshot.write_text(json.dumps(value))
        out = self.dir / 'restored'
        for number in (errno.EINVAL, errno.ENOENT):  # Linux's exFAT driver; FUSE
            with self.subTest(errno=number), self.refusing_colons(number):
                shutil.rmtree(out, ignore_errors=True)
                with self.assertRaisesRegex(RuntimeError, r'1 of 4 music files were not restored.*Album/a:b\.mp3'):
                    music.restore(self.backup, self.snapshot, out)
                for name, body in self.files.items():
                    self.assertEqual((out / name).read_bytes(), body)
        # From the command line: plain words, never a traceback.
        shutil.rmtree(out)
        err = io.StringIO()
        with self.refusing_colons(errno.EINVAL), mock.patch.object(music.sys, 'stderr', err):
            code = music.main(
                ['--backup', str(self.backup), '--snapshot', str(self.snapshot), '--destination', str(out)]
            )
        self.assertEqual(code, 1)
        self.assertIn('restore into a new folder on a Linux drive (ext4)', err.getvalue())

    def test_a_full_drive_still_stops_the_restore(self):
        real = os.open

        def full(path, *args, **kwargs):
            if str(path).endswith('.flac'):
                raise OSError(errno.ENOSPC, 'No space left on device', str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(music.os, 'open', full):
            with self.assertRaisesRegex(OSError, 'No space left'):
                music.restore(self.backup, self.snapshot, self.dir / 'restored')


class MenuTests(unittest.TestCase):
    """Deep check 675, on loop drives with the real nightly music backup: the recovery menu
    said "Films, shows and music are not backed up" and had no line for the music backup
    it had made, and no guide said how to get it back; music-recovery.py by hand left the
    files root's private ones, which Navidrome (run as the media account) cannot read."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from roomy import roomy_drive
        import recovery_wizard

        self.wizard = recovery_wizard
        roomy_drive(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = {
            'media_mount': str(self.dir / 'hdd'),
            'data_mount': str(self.dir / 'ssd'),
            'media': {'uid': os.getuid(), 'gid': os.getgid()},
        }
        ready = mock.patch.object(recovery_wizard, 'hdd_ready', return_value=True)
        ready.start()
        self.addCleanup(ready.stop)

    def backed_up(self):
        backup = self.dir / 'hdd/backups/music'
        payload = b'a song'
        checksum = hashlib.sha256(payload).hexdigest()
        (backup / 'objects' / checksum[:2]).mkdir(parents=True)
        (backup / 'objects' / checksum[:2] / checksum).write_bytes(payload)
        (backup / 'snapshots').mkdir()
        row = {'path': 'Artist/Album/01 Song.flac', 'sha256': checksum, 'size': len(payload)}
        (backup / 'snapshots/1794782711045838-23790.json').write_text(
            json.dumps({'format': 'neko-music-backup-v1', 'files': [row], 'waiting': []})
        )

    def answers(self, *values):
        it = iter(values)
        return lambda question='': next(it)

    def section(self, *answers):
        out = []
        self.wizard.restore_section(self.config, self.answers(*answers), out.append)
        return '\n'.join(out)

    def test_a_server_with_a_music_backup_has_it_in_the_menu_and_restores_it(self):
        self.backed_up()
        said = self.section('0')
        self.assertIn('Music files, from the nightly music backup', said)
        self.assertNotIn('music are not backed up', said)
        number = next(
            line for line in said.splitlines() if line.endswith('. Music files, from the nightly music backup')
        )
        number = number.split('.')[0].strip()
        given = []
        with mock.patch.object(self.wizard, 'give_to', side_effect=lambda *a: given.append(a)):
            said = self.section(number, '1', '1', 'yes')
        restored = next((self.dir / 'hdd/restored').iterdir())
        self.assertEqual((restored / 'Artist/Album/01 Song.flac').read_bytes(), b'a song')
        # Given to the account the music apps run as, and the line that copies it back.
        self.assertEqual(given, [(restored, os.getuid(), os.getgid())])
        self.assertIn(f'sudo cp -a -n {restored}/. {self.dir}/ssd/library/music/', said)

    def test_a_server_without_one_is_told_music_is_not_backed_up(self):
        said = self.section('0')
        self.assertNotIn('Music files', said)
        self.assertIn('Films, shows and music are not backed up', said)


if __name__ == '__main__':
    unittest.main()
