"""What the nightly-snapshot verify/restore (snapshot_restore.py, used by app-recovery.py
and host-recovery.py) refuses, with the words it says, and what its report holds: a
person restoring after a dead drive reads these words, and the report is the proof."""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import snapshot_restore as sr  # noqa: E402
from roomy import roomy_drive  # noqa: E402
from test_snapshot_restore import make  # noqa: E402

GOOD = {'uid': 0, 'gid': 0, 'mode': 0o644, 'size': 0}


def file_row(path, body, **more):
    return {**GOOD, 'path': path, 'kind': 'file', 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), **more}


class FormatTests(unittest.TestCase):
    def test_the_snapshots_the_nightly_backups_write_are_the_ones_taken(self):
        """The nightly snapshots are made by the Rust runtime: a name that differs here
        would refuse every snapshot at the one moment it is needed."""
        source = ROOT / 'runtime' / 'src'
        for kind, writer in ((sr.APPS, 'app_snapshot.rs'), (sr.HOST, 'host_snapshot.rs')):
            with self.subTest(writer=writer):
                self.assertIn('json!({"format":"%s"' % kind.format, (source / writer).read_text())
        self.assertEqual((sr.APPS.format, sr.HOST.format), ('neko-media-snapshot-v1', 'neko-host-snapshot-v1'))


class PathTests(unittest.TestCase):
    def refused(self, kind, value):
        with self.assertRaises(RuntimeError) as caught:
            sr.relative(kind, value)
        return str(caught.exception)

    def test_a_path_that_is_no_name_is_invalid(self):
        for value in (None, '', 'appdata/a\x00b', 5):
            with self.subTest(value=value):
                self.assertEqual(self.refused(sr.APPS, value), 'Invalid snapshot path')

    def test_a_path_that_leads_elsewhere_is_unsafe(self):
        for value in ('.', '/appdata/x', 'appdata/../x', 'appdata/./x', 'appdata//x', 'appdata/x/'):
            with self.subTest(value=value):
                self.assertEqual(self.refused(sr.APPS, value), 'Unsafe snapshot path')

    def test_only_the_snapshots_own_top_folders_are_taken(self):
        for root in ('appdata', 'compose', 'installer'):
            self.assertEqual(sr.relative(sr.APPS, root + '/x'), Path(root, 'x'))
        for value in ('etc/passwd', 'source1/x'):
            self.assertEqual(self.refused(sr.APPS, value), 'Unmanaged snapshot path')
        # A host snapshot's folders are source1, source2, ...
        self.assertEqual(sr.relative(sr.HOST, 'source12/home'), Path('source12', 'home'))
        for value in ('source/x', 'sourcex/x', 'appdata/x', 'source1x/y'):
            with self.subTest(value=value):
                self.assertEqual(self.refused(sr.HOST, value), 'Unmanaged snapshot path')


class MetadataTests(unittest.TestCase):
    LIMITS = {'uid': 2**32 - 2, 'gid': 2**32 - 2, 'mode': 0o7777, 'size': 2**63 - 1}

    def bad(self, row):
        with self.assertRaisesRegex(RuntimeError, '^Invalid file metadata$'):
            sr.check_metadata(row)

    def test_owner_mode_and_size_within_what_linux_has(self):
        for key, maximum in self.LIMITS.items():
            with self.subTest(key=key):
                sr.check_metadata({**GOOD, key: maximum})
                self.bad({**GOOD, key: maximum + 1})
                self.bad({**GOOD, key: -1})
                self.bad({**GOOD, key: str(maximum)})
                self.bad({k: v for k, v in GOOD.items() if k != key})

    def test_the_time_last_changed_is_a_whole_second_and_nanoseconds(self):
        sr.check_metadata({**GOOD, 'mtime': 2**62, 'mtime_nsec': 10**9 - 1})
        sr.check_metadata({**GOOD, 'mtime': -(2**62), 'mtime_nsec': 0})
        for mtime, nsec in ((2**62 + 1, 0), (-(2**62) - 1, 0), (1.5, 0), (1, 10**9), (1, -1), (1, None), (1, '0')):
            with self.subTest(mtime=mtime, nsec=nsec):
                self.bad({**GOOD, 'mtime': mtime, 'mtime_nsec': nsec})


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snap = Path(self.tmp.name) / 'snap'

    def refused(self, kind=sr.APPS):
        with self.assertRaises(RuntimeError) as caught:
            sr.verify(kind, self.snap)
        return str(caught.exception)

    def manifest(self, **fields):
        (self.snap / 'payload').mkdir(parents=True, exist_ok=True)
        (self.snap / 'manifest.json').write_text(json.dumps({'format': sr.APPS.format, **fields}))

    def test_a_snapshot_without_its_list_of_files_is_refused(self):
        self.snap.mkdir()
        self.assertEqual(self.refused(), 'Missing or oversized snapshot manifest')

    def test_a_list_of_files_too_big_to_be_real_is_not_read(self):
        self.snap.mkdir()
        with open(self.snap / 'manifest.json', 'wb') as handle:
            handle.truncate(128_000_001)  # sparse: takes no room
        self.assertEqual(self.refused(), 'Missing or oversized snapshot manifest')

    def test_the_other_kinds_snapshot_is_refused(self):
        make(self.snap, sr.APPS, {'appdata/a': b'1'})
        self.assertEqual(self.refused(sr.HOST), 'Unsupported snapshot format')

    def test_a_list_with_no_files_or_too_many_is_refused(self):
        for files in (None, {}, [], [0] * 200_001):
            with self.subTest(files=type(files).__name__ + str(len(files or ()))):
                self.manifest(files=files)
                self.assertEqual(self.refused(), 'Invalid file inventory')
        self.manifest(files=[0] * 200_000)  # as many as allowed: each entry is then checked
        self.assertEqual(self.refused(), 'Invalid inventory entry')

    def test_a_file_listed_twice_or_of_a_kind_this_snapshot_cannot_have_is_refused(self):
        make(self.snap, sr.APPS, {'appdata/a': b'1'})
        row = file_row('appdata/a', b'1')
        for rows in ([row, row], [{**row, 'kind': 'symlink', 'link_target': 'x'}], [{**row, 'kind': 'fifo'}]):
            with self.subTest(rows=[r['kind'] for r in rows]):
                self.manifest(files=rows)
                self.assertEqual(self.refused(), 'Duplicate or invalid inventory entry')

    def test_a_folder_listed_but_missing_is_refused(self):
        make(self.snap, sr.APPS, {'appdata/a': b'1'}, [{**GOOD, 'path': 'appdata/gone', 'kind': 'directory'}])
        with self.assertRaises((RuntimeError, OSError)):
            sr.verify(sr.APPS, self.snap)
        (self.snap / 'payload/appdata/gone').write_text('a file, not a folder')
        self.assertEqual(self.refused(), 'Snapshot directory missing')

    def test_a_file_with_a_second_name_is_refused(self):
        make(self.snap, sr.APPS, {'appdata/a': b'1'})
        os.link(self.snap / 'payload/appdata/a', self.snap / 'second-name')
        self.assertEqual(self.refused(), 'Snapshot file checksum or size mismatch')

    def test_a_file_on_the_drive_not_in_the_list_is_refused(self):
        make(self.snap, sr.APPS, {'appdata/a': b'1'})
        (self.snap / 'payload/appdata/extra').write_text('x')
        self.assertEqual(self.refused(), 'Snapshot contains missing or unlisted entries')

    def test_a_link_kept_as_text_must_say_where_it_led(self):
        self.snap.mkdir()
        (self.snap / 'payload/source1').mkdir(parents=True)
        (self.snap / 'payload/source1/link').write_text('/home/alice/music')
        row = {**file_row('source1/link', b'/home/alice/music'), 'kind': 'symlink'}
        folder = {**GOOD, 'path': 'source1', 'kind': 'directory'}
        for target, said in (('/home/alice/films', 'Link inventory mismatch'), (None, 'Link inventory mismatch')):
            with self.subTest(target=target):
                (self.snap / 'manifest.json').write_text(
                    json.dumps({'format': sr.HOST.format, 'files': [folder, {**row, 'link_target': target}]})
                )
                self.assertEqual(self.refused(sr.HOST), said)
        (self.snap / 'manifest.json').write_text(
            json.dumps({'format': sr.HOST.format, 'files': [folder, {**row, 'link_target': '/home/alice/music'}]})
        )
        sr.verify(sr.HOST, self.snap)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        roomy_drive(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = self.root / 'snap'
        make(self.snap, sr.APPS, {'appdata/sonarr/config.xml': b'<x/>', 'compose/a.yml': b'abc'})

    def refused(self, destination, **more):
        with self.assertRaises(RuntimeError) as caught:
            sr.restore(sr.APPS, self.snap, destination, **more)
        return str(caught.exception)

    def test_the_new_folder_must_be_new_and_apart_from_the_snapshot(self):
        self.assertEqual(self.refused(self.snap), 'Restore needs a new absolute directory')
        self.assertEqual(self.refused(Path('new')), 'Restore needs a new absolute directory')
        self.assertEqual(
            self.refused(self.snap / 'payload' / 'new'), 'Restore directory must be separate from snapshot'
        )
        self.assertFalse((self.snap / 'payload' / 'new').exists())

    def test_a_missing_parent_folder_is_named_with_the_command_that_makes_it(self):
        parent = self.root / 'not-there'
        self.assertEqual(
            self.refused(parent / 'restored'),
            f'The folder {parent} does not exist, so nothing was restored. Create it first '
            f'(sudo mkdir -p "{parent}"), or choose a new folder inside one that exists',
        )

    def test_the_room_asked_for_is_the_files_own_size(self):
        snap = self.root / 'sized'
        make(snap, sr.APPS, {'appdata/sonarr/config.xml': b'<x/>'})
        manifest = json.loads((snap / 'manifest.json').read_text())
        for row in manifest['files']:
            if row['kind'] == 'directory':
                row['size'] = 4096  # as a folder's size is on most drives
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        with mock.patch.object(sr, 'ensure_room') as room:
            sr.restore(sr.APPS, snap, self.root / 'new')
        room.assert_called_once_with(self.root, len(b'<x/>'))

    def test_a_time_that_cannot_be_set_on_one_entry_still_sets_the_others(self):
        snap = self.root / 'dated'
        make(snap, sr.APPS, {'appdata/a': b'1', 'appdata/b': b'2'})
        manifest = json.loads((snap / 'manifest.json').read_text())
        for row in manifest['files']:
            row.update(mtime=1_700_000_000, mtime_nsec=0)
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        real = sr.Destination.entry

        def entry(target, parts):
            if parts[-1] == 'a':
                raise PermissionError(13, 'Permission denied')
            return real(target, parts)

        with mock.patch.object(sr.Destination, 'entry', entry):
            out = sr.restore(sr.APPS, snap, self.root / 'new')
        self.assertIs(json.loads((out / sr.REPORT_NAME).read_text())['times_restored'], False)
        self.assertEqual((out / 'appdata/b').stat().st_mtime_ns, 1_700_000_000 * 10**9)

    @unittest.skipIf(os.geteuid() == 0, 'as root the owners can be given back')
    def test_owners_cannot_be_given_back_without_root_and_nothing_is_made(self):
        self.assertEqual(
            self.refused(self.root / 'new', preserve_owners=True), 'Root required to restore original ownership'
        )
        self.assertFalse((self.root / 'new').exists())

    def test_the_new_folder_is_private(self):
        out = sr.restore(sr.APPS, self.snap, self.root / 'new')
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / 'compose/a.yml').stat().st_mode & 0o777, 0o600)

    def test_the_app_restore_report_says_what_was_and_was_not_done(self):
        out = sr.restore(sr.APPS, self.snap, self.root / 'new')
        self.assertEqual(
            json.loads((out / sr.REPORT_NAME).read_text()),
            {
                'format': 'neko-media-restore-v1',
                'files': 3,
                'checksums_verified': True,
                'original_owners_restored': False,
                'ownership': 'not requested',
                'containers_started': False,
                'application_startup_verified': False,
                'times_restored': True,
            },
        )

    def test_the_host_restore_report_names_the_sources_and_what_was_left_out(self):
        snap = self.root / 'host'
        (snap / 'payload/source1').mkdir(parents=True)
        (snap / 'payload/source1/smb.conf').write_bytes(b'[global]')
        rows = [{**GOOD, 'path': 'source1', 'kind': 'directory'}, file_row('source1/smb.conf', b'[global]')]
        (snap / 'manifest.json').write_text(
            json.dumps(
                {
                    'format': sr.HOST.format,
                    'files': rows,
                    'sources': ['/etc/samba'],
                    'missing_optional': ['/etc/exports'],
                    'left_out': ['steam'],
                }
            )
        )
        out = sr.restore(sr.HOST, snap, self.root / 'new-host')
        self.assertEqual(
            json.loads((out / sr.REPORT_NAME).read_text()),
            {
                'format': 'neko-host-restore-v1',
                'files': 2,
                'checksums_verified': True,
                'original_owners_restored': False,
                'ownership': 'not requested',
                'services_started': False,
                'application_startup_verified': False,
                'links_are_inert_text': True,
                'live_paths_changed': False,
                'sources': ['/etc/samba'],
                'missing_optional': ['/etc/exports'],
                'left_out': ['steam'],
                'times_restored': True,
            },
        )

    def test_a_time_the_drive_refuses_is_reported_not_a_failure(self):
        snap = self.root / 'dated'
        make(snap, sr.APPS, {'appdata/a': b'1'})
        manifest = json.loads((snap / 'manifest.json').read_text())
        manifest['files'][0].update(mtime=1_700_000_000, mtime_nsec=5)
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        with mock.patch.object(sr.os, 'utime', side_effect=PermissionError(1, 'Operation not permitted')):
            out = sr.restore(sr.APPS, snap, self.root / 'new')
        self.assertIs(json.loads((out / sr.REPORT_NAME).read_text())['times_restored'], False)
        self.assertEqual((out / 'appdata/a').read_bytes(), b'1')

    def test_a_file_that_reads_back_wrong_stops_the_restore(self):
        real = sr.hashlib.sha256

        class Wrong:
            def __init__(self, *args):
                self.inner = real(*args)

            def update(self, block):
                self.inner.update(block + b'!')

            def hexdigest(self):
                return self.inner.hexdigest()

        with mock.patch.object(sr.hashlib, 'sha256', Wrong):
            self.assertEqual(
                self.refused(self.root / 'new'), 'Restored file verification failed; partial directory retained'
            )


class ModeTests(unittest.TestCase):
    def test_set_id_bits_are_never_given_back_to_a_file(self):
        self.assertEqual(sr.restored_mode({'kind': 'file', 'mode': 0o6755}), 0o755)
        self.assertEqual(sr.restored_mode({'kind': 'directory', 'mode': 0o2775}), 0o2775)
        self.assertEqual(sr.restored_mode({'kind': 'symlink', 'mode': 0o777}), 0o600)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        roomy_drive(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snap = self.root / 'snap'
        make(self.snap, sr.APPS, {'appdata/a': b'1'})

    def run_tool(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = sr.main(sr.APPS, 'test', list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_verify_says_the_checksums_were_checked(self):
        self.assertEqual(
            self.run_tool('verify', '--snapshot', str(self.snap)),
            (0, 'Snapshot inventory and file checksums verified.\n', ''),
        )

    def test_restore_needs_a_new_folder_named(self):
        code, _, err = self.run_tool('restore', '--snapshot', str(self.snap))
        self.assertEqual(code, 2)
        self.assertIn('error: restore requires --destination', err)

    def test_restore_says_the_apps_were_not_touched(self):
        code, out, _ = self.run_tool('restore', '--snapshot', str(self.snap), '--destination', str(self.root / 'new'))
        self.assertEqual((code, out), (0, sr.APPS.restored_message + '\n'))
        self.assertEqual(
            sr.APPS.restored_message, 'Files restored into a new folder. Your apps were not changed or restarted.'
        )

    def test_the_host_restore_says_links_are_kept_as_text(self):
        self.assertEqual(
            sr.HOST.restored_message,
            'Files restored into a new private folder (shortcut links are saved as plain text there, so they '
            'point nowhere until copied back). Nothing on the server was changed or restarted.',
        )

    def test_the_snapshot_must_be_named(self):
        code, _, err = self.run_tool('verify')
        self.assertEqual(code, 2)
        self.assertIn('the following arguments are required: --snapshot', err)

    def test_a_failure_says_nothing_live_changed_and_why(self):
        (self.snap / 'payload/appdata/a').write_bytes(b'2')
        code, out, err = self.run_tool('verify', '--snapshot', str(self.snap))
        self.assertEqual((code, out), (1, ''))
        self.assertEqual(
            err,
            'Snapshot verification or restoration failed. Live app data was not changed. '
            'Reason: Snapshot file checksum or size mismatch\n',
        )
        self.assertEqual(
            sr.HOST.failed_message,
            'Snapshot verification or restoration failed. Live OS/user/Samba data was not changed.',
        )

    def test_the_reason_is_said_in_words_a_person_can_act_on(self):
        self.assertEqual(
            sr.failure_reason(OSError(28, 'No space left on device', '/mnt/new/a')),
            'No space left on device (/mnt/new/a)',
        )
        self.assertEqual(sr.failure_reason(OSError(28, 'No space left on device')), 'No space left on device')
        self.assertEqual(sr.failure_reason(KeyError('size')), "the snapshot's list of files is not valid (KeyError)")
        self.assertEqual(sr.failure_reason(KeyboardInterrupt()), 'stopped with Ctrl+C')


if __name__ == '__main__':
    unittest.main()
