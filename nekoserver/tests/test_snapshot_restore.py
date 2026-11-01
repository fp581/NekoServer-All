"""The shared nightly-snapshot verify/restore used by app-recovery.py and host-recovery.py."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import snapshot_restore as sr  # noqa: E402


def make(snapshot, kind, files, extra_rows=()):
    rows = []
    for rel, body in files.items():
        path = snapshot / 'payload' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        rows.append(
            {
                'path': rel,
                'kind': 'file',
                'uid': 0,
                'gid': 0,
                'mode': 0o644,
                'size': len(body),
                'sha256': hashlib.sha256(body).hexdigest(),
            }
        )
    rows.extend(extra_rows)
    for rel in sorted({str(Path(r).parent) for r in files} - {'.'}):
        if rel.split('/')[0] in kind.roots and '/' not in rel:
            continue  # app snapshots: top-level parents are implicit
        rows.append({'path': rel, 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0})
    (snapshot / 'manifest.json').write_text(json.dumps({'format': kind.format, 'files': rows}))


class SnapshotRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_app_snapshot_verifies_restores_and_catches_tampering(self):
        snap = self.root / 'snap'
        make(snap, sr.APPS, {'appdata/sonarr/config.xml': b'<x/>'})
        sr.verify(sr.APPS, snap)
        out = sr.restore(sr.APPS, snap, self.root / 'new')
        self.assertEqual((out / 'appdata/sonarr/config.xml').read_bytes(), b'<x/>')
        report = json.loads((out / 'restore-report.json').read_text())
        self.assertEqual(report['format'], 'neko-media-restore-v1')
        self.assertFalse(report['containers_started'])
        (snap / 'payload/appdata/sonarr/config.xml').write_bytes(b'<y/>')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            sr.verify(sr.APPS, snap)

    def test_host_snapshot_keeps_links_as_text_and_rejects_foreign_roots(self):
        snap = self.root / 'snap'
        link = {
            'path': 'source0/link',
            'kind': 'symlink',
            'uid': 0,
            'gid': 0,
            'mode': 0o777,
            'size': 9,
            'sha256': hashlib.sha256(b'/etc/xyz').hexdigest(),
            'link_target': '/etc/xyz',
        }
        (snap / 'payload/source0').mkdir(parents=True)
        (snap / 'payload/source0/link').write_text('/etc/xyz')
        link['size'] = 8
        make(snap, sr.HOST, {'source0/smb.conf': b'[global]'}, [link])
        out = sr.restore(sr.HOST, snap, self.root / 'new')
        self.assertEqual((out / 'source0/link').read_text(), '/etc/xyz')
        self.assertTrue(json.loads((out / 'restore-report.json').read_text())['links_are_inert_text'])
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'):
            sr.relative(sr.HOST, 'appdata/x')
        with self.assertRaisesRegex(RuntimeError, 'Unmanaged'):
            sr.relative(sr.APPS, 'source0/x')

    def test_a_backslash_in_a_name_is_restored_like_any_other_character(self):
        # Linux allows it, and the nightly snapshots record such names as they are.
        snap = self.root / 'snap'
        make(snap, sr.HOST, {'source0/notes\\2026.txt': b'notes'})
        out = sr.restore(sr.HOST, snap, self.root / 'new')
        self.assertEqual((out / 'source0' / 'notes\\2026.txt').read_bytes(), b'notes')
        for unsafe in ('source0/../x', '/source0/x', 'source0/./x', 'source0/x\x00'):
            with self.assertRaises(RuntimeError, msg=unsafe):
                sr.relative(sr.HOST, unsafe)

    def test_the_report_says_owners_were_restored_only_when_they_were(self):
        from unittest import mock

        snap = self.root / 'snap'
        make(snap, sr.APPS, {'appdata/sonarr/config.xml': b'<x/>'})
        report = lambda out: json.loads((out / 'restore-report.json').read_text())
        with (
            mock.patch.object(sr.os, 'geteuid', return_value=0),
            mock.patch.object(sr.os, 'chown', side_effect=PermissionError(1, 'Operation not permitted')),
        ):
            with self.assertRaises(PermissionError):
                sr.restore(sr.APPS, snap, self.root / 'failed', preserve_owners=True)
        failed = report(self.root / 'failed')
        self.assertFalse(failed['original_owners_restored'])
        self.assertEqual(failed['ownership'], 'incomplete')
        with mock.patch.object(sr.os, 'geteuid', return_value=0), mock.patch.object(sr.os, 'chown'):
            done = report(sr.restore(sr.APPS, snap, self.root / 'done', preserve_owners=True))
        self.assertTrue(done['original_owners_restored'])
        self.assertEqual(done['ownership'], 'restored')
        plain = report(sr.restore(sr.APPS, snap, self.root / 'plain'))
        self.assertEqual((plain['original_owners_restored'], plain['ownership']), (False, 'not requested'))

    def test_files_and_folders_get_their_original_times_back(self):
        snap = self.root / 'snap'
        top = {'path': 'source0', 'kind': 'directory', 'uid': 0, 'gid': 0, 'mode': 0o755, 'size': 0}
        make(snap, sr.HOST, {'source0/docs/old.txt': b'2020', 'source0/docs/deep/new.txt': b'x'}, [top])
        manifest = json.loads((snap / 'manifest.json').read_text())
        when = {
            'source0/docs/old.txt': (1_580_000_000, 123_456_789),
            'source0/docs': (1_590_000_000, 5),
            'source0/docs/deep': (1_600_000_000, 0),
        }
        for row in manifest['files']:
            if row['path'] in when:
                row['mtime'], row['mtime_nsec'] = when[row['path']]
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        out = sr.restore(sr.HOST, snap, self.root / 'new')
        for rel, (seconds, nanoseconds) in when.items():
            self.assertEqual((out / rel).stat().st_mtime_ns, seconds * 10**9 + nanoseconds, rel)
        # A file without a recorded time (an older snapshot) is simply left as restored.
        self.assertGreater((out / 'source0/docs/deep/new.txt').stat().st_mtime, 1_700_000_000)
        self.assertTrue(json.loads((out / 'restore-report.json').read_text())['times_restored'])
        # A filesystem that refuses the time: the files are still restored, and the report says so.
        from unittest import mock

        with mock.patch.object(sr.os, 'utime', side_effect=PermissionError(1, 'Operation not permitted')):
            again = sr.restore(sr.HOST, snap, self.root / 'again')
        self.assertEqual((again / 'source0/docs/old.txt').read_bytes(), b'2020')
        self.assertFalse(json.loads((again / 'restore-report.json').read_text())['times_restored'])
        manifest['files'][0]['mtime'] = 'yesterday'
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, 'metadata'):
            sr.verify(sr.HOST, snap)

    def test_restore_never_reuses_an_existing_folder(self):
        snap = self.root / 'snap'
        make(snap, sr.APPS, {'compose/a.yaml': b'a'})
        (self.root / 'exists').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'new absolute directory'):
            sr.restore(sr.APPS, snap, self.root / 'exists')

    def test_a_missing_parent_folder_says_what_to_do(self):
        # IT-16: it said only "No such file or directory".
        snap = self.root / 'snap'
        make(snap, sr.APPS, {'compose/a.yaml': b'a'})
        parent = self.root / 'not-there'
        with self.assertRaisesRegex(RuntimeError, f'sudo mkdir -p "{parent}"'):
            sr.restore(sr.APPS, snap, parent / 'restored')
        self.assertFalse(parent.exists())

    def test_command_line_tools_still_work(self):
        snap = self.root / 'snap'
        make(snap, sr.APPS, {'installer/x.json': b'{}'})
        for tool, ok in (('app-recovery.py', 0), ('host-recovery.py', 1)):
            result = subprocess.run(
                [sys.executable, str(ROOT / tool), 'verify', '--snapshot', str(snap)],
                capture_output=True,
                text=True,
                env={**os.environ, 'PYTHONPATH': str(ROOT)},
            )
            self.assertEqual(result.returncode, ok, tool + result.stderr)


if __name__ == '__main__':
    unittest.main()
