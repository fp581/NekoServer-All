"""T1-9 and T1-10 (1.0.1 VM drill 4): a move whose setup run refused the new place.

The photos were moved to an Extra drive and the settings switched, then setup refused:
Immich's .env still named the old folder ("Existing Immich paths differ"). The job ended
half: the settings and the copy on the new drive, Immich on the old folder, and a journal
that neither the start of the computer nor a new job could clear. Now:

- setup run again for a move takes the place the move left for the one the settings
  name now, in every app's own record of its folders (Immich's .env, Nextcloud's
  recorded settings), so it changes them instead of refusing; the media apps' Compose
  files are setup's own and are written again, and the drives check (same_place) takes
  the old folder that is still there;
- a setup run that still fails after the switch undoes the move ("unswitch"): what the
  apps wrote in the new place is copied back, the settings name the old place again, the
  new copy goes, setup runs again for the old place, and the job ends "failed" with the
  reason and no journal; a power cut in the middle goes on with it at the next start;
- the dashboard keeps its port: its socket stays up through the move (stopping it
  stopped the page with it), and a port only closed connections are waiting on is free."""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage_jobs  # noqa: E402
from modules import dashboard, nextcloud, photos, storage  # noqa: E402
from tests.test_storage_jobs import ID, Jobs, last_result, saved_journal  # noqa: E402

REFUSED = 'FAILED photos: Existing Immich paths differ'


class Refusing:
    """Setup run again: refuses (raises) or works, in turn; what the apps write meanwhile."""

    def __init__(self, test, *answers, writes=None):
        self.test, self.answers, self.writes = test, list(answers), writes or {}

    def __call__(self, job):
        self.test.setups.append(json.loads(json.dumps(job.config)))
        for path, text in self.writes.items():
            (self.test.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.test.root / path).write_text(text)
        self.writes = {}
        answer = self.answers.pop(0)
        if answer:
            raise RuntimeError(answer)


class UnswitchTests(Jobs):
    def refusing(self, *answers, writes=None):
        return mock.patch.object(storage_jobs, 'setup_again', Refusing(self, *answers, writes=writes))

    def test_a_move_setup_refuses_is_undone_and_ends_failed_with_the_reason(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'a/x.jpg': 'one'})
        late = {'drives/d3/photos/immich-library/late.jpg': 'taken meanwhile'}
        with self.refusing(REFUSED, None, writes=late):
            self.assertEqual(self.run_job('move', kind='photos', to='d3'), 1)
        self.assertEqual((self.root / 'ssd/photos/immich-library/a/x.jpg').read_text(), 'one')
        self.assertEqual((self.root / 'ssd/photos/immich-library/late.jpg').read_text(), 'taken meanwhile')
        self.assertEqual([p for p in (self.root / 'drives/d3').rglob('*') if not p.is_dir()], [])
        self.assertEqual([s['locations'] for s in self.setups], [{'photos': 'd3'}, {}])
        for index in (0, 1):
            self.assertEqual(self.saved(index)['locations'], {})
        result = last_result(self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('stay on Apps drive', result['message'])
        self.assertIn(REFUSED, result['message'])
        self.assertIn('Nothing was changed', result['message'])
        self.assertIsNone(saved_journal(self.root))
        self.assertEqual(self.started[-1], ['nekoserver-immich.service'])

    def test_a_second_refusal_still_leaves_the_old_state_and_no_journal(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        with self.refusing(REFUSED, 'FAILED gateway_lan: no address'):
            self.assertEqual(self.run_job('move', kind='photos', to='media'), 1)
        self.assertEqual((self.root / 'ssd/photos/immich-library/x.jpg').read_text(), 'one')
        self.assertFalse((self.root / 'media/photos/immich-library').exists())
        self.assertEqual(self.saved()['locations'], {})
        self.assertIn('no address', last_result(self.root)['message'])
        self.assertIsNone(saved_journal(self.root))

    def test_a_new_job_is_taken_after_a_refused_move(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        with self.refusing(REFUSED, None):
            self.run_job('move', kind='photos', to='media')
        self.assertEqual(self.run_job('move', kind='photos', to='media'), 0)
        self.assertEqual(self.saved()['locations'], {'photos': 'media'})

    def test_backups_retired_before_setup_come_back_when_it_refuses(self):
        self.files(self.root / 'media', **{'backups/host.tar': 'h', 'photos/archive/2020.zip': 'z'})
        with self.refusing('FAILED backups', None), mock.patch.object(storage_jobs, 'unbind_backups') as unbind:
            self.assertEqual(self.run_job('move', kind='backups', to='d3'), 1)
        self.assertEqual((self.root / 'media/backups/host.tar').read_text(), 'h')
        self.assertEqual((self.root / 'media/photos/archive/2020.zip').read_text(), 'z')
        self.assertFalse((self.root / f'media/nekoserver-old-backups-{ID}').exists())
        self.assertEqual([p for p in (self.root / 'drives/d3').rglob('*') if not p.is_dir()], [])
        self.assertEqual(unbind.call_args[0][0]['locations'], {'backups': 'd3'})
        self.assertEqual(self.saved()['locations'], {})

    def test_going_back_to_an_old_copy_that_setup_refuses_keeps_that_old_copy(self):
        self.config['locations'] = {'photos': 'media'}
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'now'})
        old = self.root / 'ssd/nekoserver-old-photos-fedcba9876543210/photos/immich-library'
        self.files(old, **{'x.jpg': 'before'})
        with self.refusing(REFUSED, None):
            self.assertEqual(self.run_job('back', kind='photos', copy='fedcba9876543210'), 1)
        self.assertEqual((self.root / 'media/photos/immich-library/x.jpg').read_text(), 'now')
        self.assertTrue(old.is_dir())
        self.assertEqual(self.saved()['locations'], {'photos': 'media'})

    def test_a_power_cut_while_undoing_goes_on_with_it_at_the_next_start(self):
        self.files(self.root / 'ssd/photos/immich-library', **{'x.jpg': 'one'})
        self.files(self.root / 'media/photos/immich-library', **{'x.jpg': 'one', 'late.jpg': 'meanwhile'})
        self.config['locations'] = {'photos': 'media'}
        job = storage_jobs.Job(
            self.request('move', kind='photos', to='media'), self.config, self.settings, self.root / 'state'
        )
        job.journal(
            'unswitch',
            True,
            staging=str(self.root / f'media/nekoserver-new-photos-{ID}'),
            here='ssd',
            to='media',
            change=[['locations', 'photos', None]],
            why=REFUSED,
        )
        with mock.patch.object(storage_jobs, 'run', mock.Mock()):
            self.assertEqual(storage_jobs.resume(self.config, self.settings, self.root / 'state', self.root / 'run'), 0)
        self.assertEqual(self.saved()['locations'], {})
        self.assertEqual(storage_jobs.finish(self.config, self.settings, self.root / 'state', self.root / 'run'), 1)
        self.assertEqual((self.root / 'ssd/photos/immich-library/late.jpg').read_text(), 'meanwhile')
        self.assertFalse((self.root / 'media/photos/immich-library').exists())
        self.assertEqual(self.setups[-1]['locations'], {})
        self.assertEqual(last_result(self.root)['state'], 'failed')
        self.assertIsNone(saved_journal(self.root))


class MovedHereTests(unittest.TestCase):
    """What setup run again reads of the job (storage_jobs.moved_here)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('ssd', 'media', 'drives/d3', 'state'):
            (self.root / name).mkdir(parents=True)
        self.config = {
            'data_mount': str(self.root / 'ssd'),
            'media_mount': str(self.root / 'media'),
            'storage': {'data_ssd': {'uuid': 's'}, 'media_hdd': {'uuid': 'h'}, 'd3': {'uuid': 'd3-uuid'}},
            'locations': {},
        }
        for patch in (
            mock.patch('neko_common.EXTRA_MOUNTS', self.root / 'drives'),
            mock.patch.object(storage_jobs, 'STATE', self.root / 'state'),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def moving(self, kind, here, to, phase='start', committed=True, job='move'):
        self.config['locations'][kind] = to if phase == 'start' else here
        request = {'format': storage_jobs.FORMAT, 'id': ID, 'job': job, 'kind': kind, 'to': to}
        storage_jobs.Job(request, self.config, [], self.root / 'state').journal(phase, committed, here=here, to=to)

    def drive(self, name):
        return self.root / {'ssd': 'ssd', 'media': 'media'}.get(name, f'drives/{name}')

    def test_only_a_committed_move_of_that_kind_whose_settings_name_its_new_place(self):
        self.assertIsNone(storage_jobs.moved_here(self.config, 'photos'))
        self.moving('photos', 'ssd', 'd3')
        self.assertEqual(storage_jobs.moved_here(self.config, 'photos'), (self.drive('ssd'), self.drive('d3')))
        self.assertIsNone(storage_jobs.moved_here(self.config, 'cloud'))
        self.config['locations']['photos'] = 'media'
        self.assertIsNone(storage_jobs.moved_here(self.config, 'photos'))
        self.moving('photos', 'ssd', 'd3', phase='switch', committed=False)
        self.assertIsNone(storage_jobs.moved_here(self.config, 'photos'))

    def test_while_a_move_is_undone_the_other_way_round(self):
        self.moving('cloud', 'media', 'd3', phase='unswitch')
        self.assertEqual(storage_jobs.moved_here(self.config, 'cloud'), (self.drive('d3'), self.drive('media')))

    # ---- Immich: its .env names the folder --------------------------------------------------

    def env(self, upload):
        env = self.root / 'ssd/compose/immich/.env'
        env.parent.mkdir(parents=True, exist_ok=True)
        db = self.root / 'ssd/appdata/immich/postgres'
        env.write_text(f'UPLOAD_LOCATION={upload}\nDB_DATA_LOCATION={db}\nIMMICH_VERSION=v2.1.0\nTZ=UTC\n')
        env.chmod(0o600)
        return env, db

    def immich_check(self, env, db, said):
        """What setup's photos step does with an existing .env (_run, then checked_env)."""
        upload = photos.photo_library(self.config)
        with mock.patch.object(photos, 'ROOT_UID', os.getuid()):
            photos.follow_drives_move(env, upload, self.config, said.append)
            return photos.checked_env(env, upload, db)

    def test_immich_follows_a_move_of_the_photos(self):
        for here, to in (('ssd', 'd3'), ('ssd', 'media'), ('d3', 'ssd')):
            with self.subTest(here=here, to=to):
                env, db = self.env(self.drive(here) / 'photos/immich-library')
                self.moving('photos', here, to)
                said = []
                content = self.immich_check(env, db, said)
                self.assertIn(f'UPLOAD_LOCATION={self.drive(to)}/photos/immich-library', content.splitlines())
                self.assertIn('IMMICH_VERSION=v2.1.0', content.splitlines())
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                self.assertIn('moved from the Drives page', said[0])

    def test_immich_still_refuses_settings_changed_by_hand(self):
        env, db = self.env(self.drive('ssd') / 'photos/immich-library')
        self.config['locations']['photos'] = 'd3'
        with self.assertRaisesRegex(RuntimeError, 'Existing Immich paths differ'):
            self.immich_check(env, db, [])
        # A move of another kind, or one from another folder than the .env names: refused too.
        self.moving('music', 'ssd', 'd3')
        with self.assertRaisesRegex(RuntimeError, 'Existing Immich paths differ'):
            self.immich_check(env, db, [])
        env, db = self.env(self.root / 'elsewhere/immich-library')
        self.moving('photos', 'ssd', 'd3')
        with self.assertRaisesRegex(RuntimeError, 'Existing Immich paths differ'):
            self.immich_check(env, db, [])

    # ---- Nextcloud: its recorded settings name the drive ---------------------------------------

    def nextcloud_check(self, recorded_drive):
        """setup's Nextcloud step on an installed Nextcloud (brought_up_to_date)."""
        folder = self.root / 'ssd/compose/nextcloud'
        folder.mkdir(parents=True, exist_ok=True)
        manifest, managed = folder / 'compose.yaml', folder / 'managed.json'
        self.config['nextcloud'] = {'admin_user': 'admin'}
        moved = dict(self.config, locations={**self.config['locations'], 'cloud': recorded_drive})
        if recorded_drive == 'media':
            moved['locations'].pop('cloud')
        image = 'nextcloud:31.0.0-apache'
        owned = {'image': image, 'model': nextcloud.model(moved, image), 'settings': nextcloud.settings(moved, image)}
        managed.write_text(json.dumps(owned))
        manifest.write_text(json.dumps(owned['model']))
        said = []
        nextcloud.brought_up_to_date(
            SimpleNamespace(config=self.config, log=said.append), owned, image, (manifest, managed)
        )
        return json.loads(managed.read_text()), said

    def test_nextcloud_follows_a_move_of_its_files(self):
        self.moving('cloud', 'media', 'd3')
        written, said = self.nextcloud_check('media')
        self.assertEqual(written['settings']['media_mount'], str(self.drive('d3')))
        self.assertEqual(written['model'], nextcloud.model(self.config, 'nextcloud:31.0.0-apache'))
        self.assertTrue(any('moved from the Drives page' in line for line in said))

    def test_nextcloud_still_refuses_settings_changed_by_hand(self):
        self.config['locations']['cloud'] = 'd3'
        with self.assertRaisesRegex(RuntimeError, 'media_mount'):
            self.nextcloud_check('media')

    # ---- the drives check and the media apps' own files ------------------------------------

    def test_the_drives_check_takes_a_moved_kind_whose_old_folder_is_still_there(self):
        # Setup checks the two drives only (an Extra drive is taken as chosen); the old
        # folders are renamed out of the way after it ran.
        for kind, folder, here, to in (
            ('movies', 'library/movies', 'media', 'ssd'),
            ('music', 'library/music', 'ssd', 'media'),
            ('cloud', 'cloud/files', 'media', 'ssd'),
            ('photos', 'photos/immich-library', 'ssd', 'media'),
        ):
            with self.subTest(kind=kind):
                for place in (here, to):
                    (self.drive(place) / folder).mkdir(parents=True, exist_ok=True)
                    (self.drive(place) / folder / 'a.file').write_text('a')
                self.moving(kind, here, to)
                said = []
                kept = storage.kept_location(self.config, kind)
                storage.same_place(SimpleNamespace(log=said.append, config=self.config), kind, kind, to, kept)
                self.assertIn('used from', said[0])

    def test_the_media_apps_compose_files_are_setups_own_and_written_again_for_the_new_place(self):
        from modules import media

        folder = self.root / 'ssd/compose/jellyfin'
        folder.mkdir(parents=True)
        dest = folder / 'compose.yaml'
        template = (Path(media.__file__).resolve().parents[1] / 'compose' / 'jellyfin.yaml').read_text()
        before = media.render(template, self.root / 'ssd', self.drive('media'), 1000, 1000, 'UTC', '127.0.0.1')
        dest.write_text(before)
        media.remember(dest, before)
        after = media.render(template, self.root / 'ssd', self.drive('d3'), 1000, 1000, 'UTC', '127.0.0.1')
        self.assertNotEqual(before, after)
        backup, current = media.replacement(dest, after)
        self.assertEqual(current, before)


class DashboardPortTests(unittest.TestCase):
    def test_the_dashboard_socket_stays_up_through_a_move(self):
        self.assertIn(dashboard.SOCKET, storage_jobs.left_alone())
        self.assertIn(dashboard.SERVICE, storage_jobs.left_alone())

    def test_a_port_only_closed_connections_wait_on_is_free(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            client = socket.create_connection(('127.0.0.1', port))
            accepted, _ = server.accept()
            self.assertFalse(dashboard.port_free(port))  # a program listening on it
            accepted.close()  # the server's side closes first: it waits in TIME_WAIT
            client.close()
        self.assertTrue(dashboard.port_free(port))


if __name__ == '__main__':
    unittest.main()
