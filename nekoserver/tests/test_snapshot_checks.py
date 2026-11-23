"""The checks a photo snapshot passes before the restore rehearsal uses it, and how the
rehearsal lays out its copy (recovery_tools.py; mutation test T6 found them untested).

A snapshot is a folder: the photos (upload/), the database dump (database.sql), Immich's
Compose settings as they were (compose-resolved.json) and its record (snapshot.json) with
a checksum for every other file. A damaged or changed snapshot must be refused before
anything is restored from it."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recovery_tools  # noqa: E402

PINNED = 'ghcr.io/immich-app/immich-server@sha256:' + 'a' * 64


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Snapshot:
    """A small, valid snapshot in a folder of its own."""

    def __init__(self, folder):
        self.path = Path(folder) / 'snapshot'
        self.files = {
            'database.sql': b'CREATE TABLE assets (id int);\n',
            'compose-resolved.json': json.dumps({'services': {'immich-server': {'image': PINNED}}}).encode(),
            'upload/library/2026/beach.jpg': b'a photo',
        }
        self.meta = {
            'format': 'neko-immich-snapshot-v1',
            'immich_version': 'v1.140.0',
            'database_bytes': 1000,
            'source_upload': '/srv/nekoserver/photos',
            'source_database': '/srv/nekoserver/immich-db',
            'empty_directories': ['library/2025'],
        }
        self.write()

    def write(self, sha256=None):
        for name, data in self.files.items():
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_bytes(data)
        record = dict(self.meta, sha256=sha256 if sha256 is not None else {n: sha(d) for n, d in self.files.items()})
        (self.path / 'snapshot.json').write_text(json.dumps(record))

    def verified(self):
        with mock.patch.object(recovery_tools, 'validate_model'):
            return recovery_tools.verify_snapshot(self.path)


class VerifySnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.snapshot = Snapshot(tmp.name)

    def refused(self, message):
        with self.assertRaises(RuntimeError) as caught:
            self.snapshot.verified()
        self.assertEqual(str(caught.exception), message)

    def test_a_whole_snapshot_is_taken_with_its_compose_settings(self):
        meta, model = self.snapshot.verified()
        self.assertEqual(meta['immich_version'], 'v1.140.0')
        self.assertEqual(model, {'services': {'immich-server': {'image': PINNED}}})

    def test_a_snapshot_still_being_made_is_refused(self):
        (self.snapshot.path / '.incomplete').write_text('{}')
        self.refused('Incomplete snapshot cannot be restored')

    def test_a_record_of_another_format_or_without_immichs_version_is_refused(self):
        for change in (
            {'format': 'neko-immich-snapshot-v2'},
            {'immich_version': 'latest'},
            {'immich_version': 'v1.140'},
        ):
            with self.subTest(change=change):
                self.snapshot.meta.update(change)
                self.snapshot.write()
                self.refused('Unsupported snapshot format/version')
                self.snapshot.meta.update(format='neko-immich-snapshot-v1', immich_version='v1.140.0')

    def test_a_record_without_the_databases_or_composes_checksum_is_refused(self):
        whole = {n: sha(d) for n, d in self.snapshot.files.items()}
        for sha256 in (
            [],
            {k: v for k, v in whole.items() if k != 'database.sql'},
            {k: v for k, v in whole.items() if k != 'compose-resolved.json'},
        ):
            with self.subTest(sha256=sha256):
                self.snapshot.write(sha256=sha256)
                self.refused('Snapshot lacks database/Compose checksums')

    def test_a_record_without_immichs_version_is_refused(self):
        del self.snapshot.meta['immich_version']
        self.snapshot.write()
        self.refused('Unsupported snapshot format/version')

    def test_a_snapshot_of_a_server_with_no_photos_yet_is_taken(self):
        """A new server snapshotted before its first photo: the dump and Compose only."""
        del self.snapshot.files['upload/library/2026/beach.jpg']
        (self.snapshot.path / 'upload/library/2026/beach.jpg').unlink()
        self.snapshot.write()
        meta, _ = self.snapshot.verified()
        self.assertEqual(set(meta['sha256']), {'database.sql', 'compose-resolved.json'})

    def test_a_file_the_record_does_not_list_is_refused(self):
        (self.snapshot.path / 'upload' / 'extra.jpg').write_bytes(b'not in the record')
        self.refused('Snapshot file inventory differs')

    def test_a_photo_changed_since_the_snapshot_is_refused(self):
        (self.snapshot.path / 'upload/library/2026/beach.jpg').write_bytes(b'another photo')
        self.refused('Snapshot checksum mismatch')

    def test_a_compose_file_that_names_images_by_tag_is_refused(self):
        self.snapshot.files['compose-resolved.json'] = json.dumps(
            {'services': {'immich-server': {'image': 'ghcr.io/immich-app/immich-server:release'}}}
        ).encode()
        self.snapshot.write()
        self.refused('Snapshot images are not pinned by digest')

    def test_a_record_without_the_databases_size_is_refused(self):
        self.snapshot.meta['database_bytes'] = 0
        self.snapshot.write()
        self.refused('Snapshot database size estimate missing')

    def test_an_empty_folder_outside_the_snapshot_is_refused(self):
        self.snapshot.meta['empty_directories'] = ['../../etc']
        self.snapshot.write()
        self.refused('Invalid snapshot directory inventory')


class RehearsalServiceTests(unittest.TestCase):
    """isolated_service: a service of the snapshot as the rehearsal runs it."""

    def service(self, name, source):
        with mock.patch.object(recovery_tools, 'isolated_volumes', return_value=['the rehearsal folders']):
            return recovery_tools.isolated_service(name, source, Path('/w'), 2284)

    def test_immich_runs_on_its_own_port_with_its_own_settings_only(self):
        source = {
            'image': PINNED,
            'command': ['start.sh'],
            'entrypoint': ['tini'],
            'environment': {
                'UPLOAD_LOCATION': '/srv/photos',
                'DB_DATA_LOCATION': '/srv/db',
                'TZ': 'Asia/Jerusalem',
                'X': None,
            },
            'healthcheck': {'test': ['CMD', 'true']},
            'shm_size': '128mb',
            'stop_grace_period': '30s',
            'init': True,
            'restart': 'always',
            'container_name': 'immich_server',
            'ports': [{'target': 2283, 'published': '2283'}],
            'depends_on': {'database': {'condition': 'service_started'}},
        }
        service = self.service('immich-server', source)
        self.assertEqual(
            service,
            {
                'image': PINNED,
                'command': ['start.sh'],
                'entrypoint': ['tini'],
                'environment': {
                    'TZ': 'Asia/Jerusalem',
                    'X': None,
                    'DB_HOSTNAME': 'database',
                    'REDIS_HOSTNAME': 'redis',
                    'IMMICH_MACHINE_LEARNING_URL': 'http://immich-machine-learning:3003',
                },
                'healthcheck': {'test': ['CMD', 'true']},
                'shm_size': '128mb',
                'stop_grace_period': '30s',
                'init': True,
                'restart': 'no',
                'volumes': ['the rehearsal folders'],
                'ports': [{'target': 2283, 'published': '2284', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
                'depends_on': {'database': {'condition': 'service_started'}},
            },
        )
        # A copy: nothing the rehearsal changes reaches the snapshot's own settings.
        service['command'].append('more')
        service['healthcheck']['test'].append('more')
        service['depends_on']['database']['condition'] = 'changed'
        self.assertEqual(source['command'], ['start.sh'])
        self.assertEqual(source['healthcheck'], {'test': ['CMD', 'true']})
        self.assertEqual(source['depends_on'], {'database': {'condition': 'service_started'}})

    def test_each_service_folder_is_in_the_rehearsals_workspace(self):
        """Photos and the database go to their own folders there; a cache volume keeps its
        name inside the workspace; the clock's file is not bound at all."""
        workspace = Path('/w')
        clock = {'source': '/etc/localtime', 'target': '/etc/localtime'}
        bound = lambda name, volume: recovery_tools.isolated_volumes(name, {'volumes': [clock, volume]}, workspace)
        expected = lambda folder, target: [
            {'type': 'bind', 'source': folder, 'target': target, 'bind': {'create_host_path': False}}
        ]
        self.assertEqual(
            bound('immich-server', {'source': '/srv/photos', 'target': '/data'}), expected('/w/upload', '/data')
        )
        self.assertEqual(
            bound('database', {'source': '/srv/db', 'target': '/var/lib/postgresql/data'}),
            expected('/w/postgres', '/var/lib/postgresql/data'),
        )
        self.assertEqual(
            bound('immich-machine-learning', {'source': 'model-cache', 'target': '/cache'}),
            expected('/w/cache/model-cache', '/cache'),
        )

    def test_a_setting_left_to_be_filled_in_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, '^Unresolved environment interpolation in snapshot$'):
            self.service('database', {'image': PINNED, 'environment': {'POSTGRES_PASSWORD': '${DB_PASSWORD}'}})

    def test_only_immichs_own_services_may_be_waited_for(self):
        for depends_on in (['database'], {'mailer': {}}):
            with (
                self.subTest(depends_on=depends_on),
                self.assertRaisesRegex(RuntimeError, '^Unsupported service dependency$'),
            ):
                self.service('immich-server', {'image': PINNED, 'environment': {}, 'depends_on': depends_on})


class RehearsalCopyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.snapshot = Snapshot(tmp.name)
        self.workspace = Path(tmp.name) / 'workspace'
        self.workspace.mkdir()
        self.meta = json.loads((self.snapshot.path / 'snapshot.json').read_text())

    def test_the_rehearsal_gets_its_own_project_name(self):
        asked = []
        with mock.patch.object(recovery_tools, 'command', side_effect=lambda argv: asked.append(argv) or ''):
            project = recovery_tools.new_test_project()
        self.assertRegex(project, r'^neko-restore-[0-9a-f]{12}$')
        label = 'label=com.docker.compose.project=' + project
        self.assertEqual(
            asked,
            [
                recovery_tools.DOCKER + ['ps', '-a', '--filter', label, '-q'],
                recovery_tools.DOCKER + ['network', 'ls', '--filter', label, '-q'],
            ],
        )

    def test_a_project_name_already_in_use_is_not_taken(self):
        with (
            mock.patch.object(recovery_tools, 'command', return_value='3f2a\n'),
            self.assertRaisesRegex(RuntimeError, '^Restore project identity already exists$'),
        ):
            recovery_tools.new_test_project()

    def test_the_photos_and_the_empty_albums_are_copied_into_the_workspace(self):
        recovery_tools.copy_photos(self.snapshot.path, self.workspace, self.meta)
        upload = self.workspace / 'upload'
        self.assertEqual((upload / 'library/2026/beach.jpg').read_bytes(), b'a photo')
        self.assertTrue((upload / 'library/2025').is_dir())
        self.assertEqual(upload.stat().st_mode & 0o777, 0o700)
        self.assertEqual((upload / 'library/2025').stat().st_mode & 0o777, 0o700)

    def test_a_photo_that_differs_from_its_checksum_stops_the_copy(self):
        self.meta['sha256']['upload/library/2026/beach.jpg'] = sha(b'the photo as it was')
        with self.assertRaisesRegex(RuntimeError, '^Snapshot photo changed after verification$'):
            recovery_tools.copy_photos(self.snapshot.path, self.workspace, self.meta)

    def test_each_folder_the_services_use_is_made_private(self):
        folder = self.workspace / 'database' / 'data'
        isolated = {'services': {'database': {'volumes': [{'source': str(folder)}]}}}
        recovery_tools.make_service_folders(isolated)
        recovery_tools.make_service_folders(isolated)  # made already: fine
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_the_database_dump_is_staged_privately_as_the_rehearsal_reads_it(self):
        sql = recovery_tools.staged_database(self.snapshot.path, self.workspace, self.meta)
        self.assertEqual(sql, self.workspace / 'restore.sql')
        self.assertEqual(sql.read_bytes(), b'CREATE TABLE assets (id int);\n')
        self.assertEqual(os.stat(sql).st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):  # never written over
            recovery_tools.staged_database(self.snapshot.path, self.workspace, self.meta)

    def test_a_dump_that_differs_from_its_checksum_is_not_staged(self):
        self.meta['sha256']['database.sql'] = sha(b'the dump as it was')
        with self.assertRaisesRegex(RuntimeError, '^Snapshot SQL changed after verification$'):
            recovery_tools.staged_database(self.snapshot.path, self.workspace, self.meta)
        self.assertFalse((self.workspace / 'restore.sql').exists())

    def test_a_dump_changed_while_it_is_staged_is_refused(self):
        right = self.meta['sha256']['database.sql']
        with (
            mock.patch.object(recovery_tools, 'digest', side_effect=[right, 'f' * 64]),
            self.assertRaisesRegex(RuntimeError, '^Snapshot SQL changed during restore staging$'),
        ):
            recovery_tools.staged_database(self.snapshot.path, self.workspace, self.meta)


if __name__ == '__main__':
    unittest.main()
