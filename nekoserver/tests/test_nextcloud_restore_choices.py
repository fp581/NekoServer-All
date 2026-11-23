"""nextcloud_restore (setup brings Nextcloud back after a new SSD), the choices a mutation
run of the 1.0 round found untested: which backup and which database copy are taken, which
snapshots count as keeping Nextcloud, what is unpacked and with which modes and owners,
and the waits and commands of the restore steps. Fixtures: tests/test_nextcloud_restore.py."""

import contextlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from test_nextcloud_restore import (
    OTHER,
    OWN,
    SNAPSHOT_AT,
    Server,
    odd_member,
    python_without_the_filter,
    tar_of,
)

import nextcloud_restore as ncr


@contextlib.contextmanager
def server(**kw):
    with tempfile.TemporaryDirectory() as tmp:
        yield Server(tmp, **kw)


def edit_json(path, **changes):
    value = json.loads(path.read_text())
    for key, new in changes.items():
        if new is None:
            value.pop(key, None)
        else:
            value[key] = new
    path.write_text(json.dumps(value))


def loaded_a_database_copy(s):
    return any('-d nextcloud_restoring' in c for c in s.commands())


class DatabaseCopyTests(unittest.TestCase):
    def note(self, s):
        return s.dump.parent / f'{s.dump.name}.json'

    def test_a_copy_made_by_another_major_version_is_not_used(self):
        """Nextcloud 32's program with a database Nextcloud 31 wrote would not start."""
        with server() as s:
            edit_json(self.note(s), nextcloud_version='31.0.9')
            self.assertEqual(s.offer(), (True, None))
            self.assertIn(
                f'Nextcloud: the database copy {s.dump.name} was made by Nextcloud 31.0.9, '
                'the snapshot holds Nextcloud 32: not used',
                s.said,
            )
            self.assertIn('  database (accounts, shares, calendars, contacts): from the same backup', s.said)
            self.assertFalse(loaded_a_database_copy(s))

    def test_a_copy_of_the_same_major_version_is_used(self):
        with server() as s:
            self.assertEqual(s.offer(), (True, None))
            self.assertTrue(loaded_a_database_copy(s))
            self.assertFalse(any('not used' in line for line in s.said))

    def test_a_copy_that_does_not_match_its_checksum_is_not_used(self):
        with server() as s:
            edit_json(self.note(s), sha256='0' * 64)
            self.assertEqual(s.offer(), (True, None))
            self.assertFalse(loaded_a_database_copy(s))

    def test_a_copy_made_at_the_snapshots_own_time_is_not_newer(self):
        with server() as s:
            edit_json(self.note(s), created_at=SNAPSHOT_AT)
            self.assertEqual(s.offer(), (True, None))
            self.assertFalse(loaded_a_database_copy(s))

    def test_copies_are_taken_newest_first_and_one_without_its_time_is_passed_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name, note in (
                ('nextcloud-20260901T030000Z.sql', {'created_at': 1}),
                ('nextcloud-20260908T030000Z.sql', {'created_at': 2}),
                ('nextcloud-20260915T030000Z.sql', {'when': 3}),
                ('nextcloud-20260922T030000Z.sql', ['not a note']),
                ('nextcloud-20260929T030000Z.sql', '{cut off'),
            ):
                (folder / name).write_text('x')
                (folder / f'{name}.json').write_text(note if isinstance(note, str) else json.dumps(note))
            found = ncr.dump_notes(folder)
        self.assertEqual(
            [(path.name, note['created_at']) for path, note in found],
            [('nextcloud-20260908T030000Z.sql', 2), ('nextcloud-20260901T030000Z.sql', 1)],
        )


class WhichBackupTests(unittest.TestCase):
    def test_a_monthly_snapshot_newer_than_the_nightly_backup_is_used(self):
        with server(dump=False) as s:
            s.add_nightly(SNAPSHOT_AT - 86400)
            self.assertEqual(s.offer(), (True, None))
            self.assertEqual((s.data / 'appdata/nextcloud/html/index.php').read_text(), '<?php\n')
            self.assertIn(f'the backup of {ncr.shown(SNAPSHOT_AT)}?', s.asked[0])

    def test_a_newer_snapshot_of_another_server_leaves_this_servers_nightly_backup(self):
        with server(instance=OTHER, named=OTHER, dump=False) as s:
            s.add_nightly(SNAPSHOT_AT - 86400, instance=OWN, named=OWN)
            self.assertEqual(s.offer(), (True, None))
            self.assertIn('nightly', (s.data / 'appdata/nextcloud/html/index.php').read_text())

    def test_snapshots_are_listed_newest_first_and_only_real_monthly_ones(self):
        with server(dump=False) as s:
            folder = s.media / ncr.MONTHLY_DIR
            newer = folder / f'{int(SNAPSHOT_AT * 1e6) + 1}-8'
            shutil.copytree(s.snapshot, newer)
            edit_json(newer / 'manifest.json', created_at=SNAPSHOT_AT + 100)
            nightly_here = folder / f'{int(SNAPSHOT_AT * 1e6) + 2}-9'
            shutil.copytree(s.snapshot, nightly_here)
            edit_json(nightly_here / 'manifest.json', created_at=SNAPSHOT_AT + 200, kind=ncr.NIGHTLY)
            (folder / f'{int(SNAPSHOT_AT * 1e6) + 3}-10').symlink_to(newer)
            found = ncr.monthly_snapshots(folder)
        self.assertEqual([path for path, _ in found], [newer, s.snapshot])


class KeepsNextcloudTests(unittest.TestCase):
    def assertNotOffered(self, s):
        restored, said = s.offer()
        self.assertFalse(restored)
        self.assertTrue(said.startswith('No backup of Nextcloud\'s settings exists yet'), said)
        self.assertEqual(s.asked, [])

    def test_a_snapshot_that_does_not_keep_all_of_nextcloud_is_not_a_backup_of_it(self):
        cases = {
            'Nextcloud not among its apps': {'apps': ['immich']},
            'its settings folder not kept': {'paths': {'data': ['appdata/nextcloud'], 'media': []}},
            'another format': {'format': 'neko-maintenance-v0'},
            'no time': {'created_at': None},
        }
        for case, changes in cases.items():
            with self.subTest(case), server(dump=False) as s:
                edit_json(s.snapshot / 'manifest.json', **changes)
                self.assertNotOffered(s)

    def test_a_nightly_backup_without_its_database_copy_is_not_a_backup_of_it(self):
        for case in ('no checksum', 'no file'):
            with self.subTest(case), server(dump=False) as s:
                shutil.rmtree(s.media / ncr.MONTHLY_DIR)
                run = s.add_nightly(SNAPSHOT_AT)
                if case == 'no checksum':
                    manifest = json.loads((run / 'manifest.json').read_text())
                    del manifest['sha256']['database']
                    (run / 'manifest.json').write_text(json.dumps(manifest))
                else:
                    (run / ncr.SQL).unlink()
                self.assertNotOffered(s)


class ThisServersTests(unittest.TestCase):
    def test_only_the_database_says_whose_a_backup_is_when_config_php_is_sealed(self):
        """Another file of the program naming this server's folder does not make it this server's."""
        data = tar_of(
            {
                'appdata/nextcloud/postgres/base/1': f'path appdata_{OTHER}/preview',
                'appdata/nextcloud/html/notes.txt': f'see appdata_{OWN}',
            }
        )
        with tarfile.open(fileobj=io.BytesIO(data), mode='r|') as tar:
            named, version, config = ncr.member_facts(tar, ncr.needles({OWN}))
        self.assertEqual((named, version, config), (False, None, None))

    def test_a_name_split_between_two_reads_is_still_found(self):
        with mock.patch.object(ncr, 'CHUNK', 4):
            for before in range(12):
                with self.subTest(before=before):
                    self.assertTrue(ncr.holds(io.BytesIO(b'x' * before + b'appdata_abc-yy'), [b'appdata_abc']))
            self.assertTrue(ncr.holds(io.BytesIO(b'appdata_abc'), [b'appdata_abc']))
            self.assertFalse(ncr.holds(io.BytesIO(b'appdata_ab-c'), [b'appdata_abc']))


class UnpackedTests(unittest.TestCase):
    def test_what_the_snapshot_keeps_of_other_apps_is_never_unpacked(self):
        """A monthly snapshot keeps every app: a device file of another one stops nothing."""
        others = [odd_member('appdata/jellyfin/dev', tarfile.BLKTYPE), odd_member('compose/immich/.env')]
        for old in (False, True):
            with self.subTest(old_python=old), server(odd=others) as s:
                with python_without_the_filter() if old else contextlib.nullcontext():
                    self.assertEqual(s.offer(), (True, None))
                self.assertFalse((s.data / 'appdata/jellyfin').exists())
                self.assertFalse((s.data / 'compose/immich').exists())

    def test_set_id_sticky_and_group_or_other_write_bits_are_cleared(self):
        member = odd_member('appdata/nextcloud/html/run.sh')
        member.mode = 0o7777
        for old in (False, True):
            with self.subTest(old_python=old), server(odd=[member]) as s:
                with python_without_the_filter() if old else contextlib.nullcontext():
                    self.assertEqual(s.offer(), (True, None))
                mode = stat.S_IMODE((s.data / 'appdata/nextcloud/html/run.sh').stat().st_mode)
                self.assertEqual(oct(mode), oct(0o755))

    def test_owners_are_kept_by_number(self):
        """The database's folder is the database's own user (999 in the container), whatever
        that number is called on this PC."""
        real, given = tarfile.TarFile.extractall, []

        def extractall(self, *a, **kw):
            given.append(kw.get('numeric_owner'))
            return real(self, *a, **kw)

        with server() as s, mock.patch.object(tarfile.TarFile, 'extractall', extractall):
            self.assertEqual(s.offer(), (True, None))
        self.assertEqual(given, [True])

    def test_a_nightly_backup_gets_an_empty_database_folder_and_a_data_folder_for_nextcloud(self):
        with server(dump=False) as s:
            shutil.rmtree(s.media / ncr.MONTHLY_DIR)
            s.add_nightly(SNAPSHOT_AT)
            self.assertEqual(s.offer(), (True, None))
            postgres, files = s.data / ncr.POSTGRES, s.data / ncr.HTML / 'data'
            self.assertEqual(list(postgres.iterdir()), [])
            self.assertEqual(stat.S_IMODE(postgres.stat().st_mode), 0o700)  # PostgreSQL refuses any other
            self.assertEqual(stat.S_IMODE(files.stat().st_mode), 0o750)
            owned = [c for c in s.chown.call_args_list if str(c.args[0]).endswith('/appdata/nextcloud/html/data')]
            self.assertEqual([c.args[1:] + (c.kwargs,) for c in owned], [(33, 33, {'follow_symlinks': False})])

    def test_the_passwords_file_is_roots_alone_and_config_php_nextclouds(self):
        with server() as s:
            self.assertEqual(s.offer(), (True, None))
            calls = {str(c.args[0]).rsplit('/', 1)[-1]: (c.args[1:], c.kwargs) for c in s.chown.call_args_list}
            self.assertEqual(calls['.env'], ((0, 0), {'follow_symlinks': False}))
            self.assertEqual(calls['config.php'], ((33, 33), {'follow_symlinks': False}))
            self.assertEqual(stat.S_IMODE((s.data / ncr.ENV).stat().st_mode), 0o600)


class NotOfferedTests(unittest.TestCase):
    def test_no_files_folder_on_the_hdd_is_no_offer(self):
        with server() as s:
            shutil.rmtree(s.files)
            self.assertEqual(s.offer(), (False, None))
            self.assertEqual(s.asked, [])


class StepTests(unittest.TestCase):
    def test_the_staging_folder_with_the_passwords_is_roots_alone(self):
        modes = []

        def take_out(backup, stage, ask):
            modes.append(stat.S_IMODE(stage.stat().st_mode))
            raise RuntimeError('stop here')

        with server() as s, mock.patch.object(ncr, 'take_out', side_effect=take_out):
            restored, _ = s.offer()
        self.assertFalse(restored)
        self.assertEqual(modes, [0o700])

    def test_nextcloud_is_waited_for_until_it_answers_ready(self):
        answers = iter([None, {'installed': True, 'maintenance': True}, {'installed': True}])
        slept = []
        with mock.patch.object(ncr.time, 'sleep', side_effect=slept.append):
            ncr.ready(['compose'], lambda base: next(answers), wait=60)
        self.assertEqual(slept, [10, 10])

    def test_one_that_never_answers_ready_stops_after_the_wait(self):
        clock = iter([0.0, 0.0, 1799.0, 1801.0])
        with (
            mock.patch.object(ncr.time, 'monotonic', side_effect=lambda: next(clock)),
            mock.patch.object(ncr.time, 'sleep'),
            self.assertRaises(RuntimeError) as raised,
        ):
            ncr.ready(['compose'], lambda base: {'installed': False})
        self.assertEqual(str(raised.exception), 'Nextcloud did not answer as ready within 30 minutes')

    def run_recorded(self, code=0, stderr=b''):
        self.ran = []

        def run(argv, **kw):
            self.ran.append((argv, {k: v for k, v in kw.items() if k != 'stdin'}))
            return subprocess.CompletedProcess(argv, code, b'', stderr)

        return run

    def test_the_database_copy_is_loaded_with_six_hours_and_its_errors_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / 'nextcloud.sql'
            dump.write_text('x')
            (Path(tmp) / 'role.sql').write_text('x')
            ncr.database_loaded(['compose'], 'oc_admin', dump, self.run_recorded())
            self.assertEqual(len(self.ran), 4)
            for _, kw in self.ran:
                self.assertEqual(kw, {'capture_output': True, 'timeout': 6 * 3600, 'check': False})
            for stderr, said in (
                (b'NOTICE: x\nNOTICE: y\nERROR: relation exists\n', 'ERROR: relation exists'),
                (b'', 'no details'),
                (None, 'no details'),
            ):
                with self.subTest(stderr=stderr), self.assertRaises(RuntimeError) as raised:
                    ncr.database_loaded(['compose'], 'oc_admin', dump, self.run_recorded(3, stderr))
                self.assertEqual(str(raised.exception), f'its database copy did not load: {said}')

    def test_the_file_scan_runs_as_nextcloud_with_six_hours(self):
        ncr.scanned_all(['compose'], self.run_recorded())
        self.assertEqual(
            self.ran,
            [
                (
                    ['compose', 'exec', '-T', '-u', 'www-data', 'nextcloud', 'php', 'occ', 'files:scan', '--all'],
                    {'capture_output': True, 'text': True, 'timeout': 6 * 3600, 'check': False},
                )
            ],
        )
        with self.assertRaises(RuntimeError) as raised:
            ncr.scanned_all(['compose'], self.run_recorded(2))
        self.assertEqual(str(raised.exception), 'its file scan ended with error 2')

    def test_the_containers_are_removed_when_the_ssd_is_put_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, stage = Path(tmp) / 'ssd', Path(tmp) / 'stage'
            data.mkdir()
            stage.mkdir()
            ncr.put_back(data, stage, ['compose'], self.run_recorded(), [])
        self.assertEqual(
            self.ran, [(['compose', 'down'], {'capture_output': True, 'text': True, 'timeout': 600, 'check': False})]
        )

    def test_the_waits_are_half_an_hour(self):
        self.assertEqual(ncr.READY_WAIT, 1800)
        with server() as s:
            waits = []
            with (
                mock.patch.object(ncr, 'checked_run', side_effect=lambda argv, wait, *a: waits.append(wait)),
                mock.patch.object(ncr, 'compose_up', side_effect=lambda base, name, log, wait: waits.append(wait)),
                mock.patch.object(ncr.os, 'chown'),
                mock.patch.object(ncr.time, 'sleep'),
            ):
                self.assertEqual(ncr.offered(s.ctx, s.app(), lambda q: True, True, s.run()), (True, None))
        self.assertEqual(waits, [1800, 1800])


if __name__ == '__main__':
    unittest.main()
