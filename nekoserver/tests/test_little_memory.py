"""Deep check 275: a computer with little memory (here: 1 GB of address space). The card
restore tool's progress file and the recovery menu's list of a backup's files were read
with one read(1 GB + 1), which sets that much memory aside before reading anything: the
restore stopped with MemoryError however small the file. Each now reads a chunk at a time."""

import gzip
import json
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GB = 1 << 30


def little_memory():
    resource.setrlimit(resource.RLIMIT_AS, (GB, GB))


class LittleMemoryTests(unittest.TestCase):
    def run_limited(self, code, *argv):
        return subprocess.run(
            [sys.executable, '-c', code, str(ROOT), *argv],
            preexec_fn=little_memory,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_a_restore_with_a_progress_file_runs_in_one_gigabyte(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = Path(tmp) / 'progress.json'
            progress.write_text('{"format": "neko-restore-progress", "version": 2}\n')
            code = (
                'import sys; sys.path.insert(0, sys.argv[1]); import neko_restore; neko_restore.Progress(sys.argv[2])'
            )
            result = self.run_limited(code, str(progress))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('MemoryError', result.stderr)

    def test_a_backups_list_of_files_is_read_in_one_gigabyte(self):
        with tempfile.TemporaryDirectory() as tmp:
            listed = Path(tmp) / 'files.json.gz'
            with gzip.open(listed, 'wt') as stream:
                json.dump({'format': 'not-a-list'}, stream)
            code = (
                'import sys; sys.path.insert(0, sys.argv[1]); import recovery_wizard; '
                'print(recovery_wizard.read_inventory(sys.argv[2]))'
            )
            result = self.run_limited(code, str(listed))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'None')


if __name__ == '__main__':
    unittest.main()
