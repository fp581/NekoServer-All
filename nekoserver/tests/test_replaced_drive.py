"""Gate run C10 (VM3): the installation SSD was lost and replaced, then setup was run
again. P00 did not notice the lost drive; the storage step told the person to edit
my-config.json; "Retry this step" failed again with the old UUID after the fix; the
recovery menu offered "Nextcloud files" on a server without Nextcloud."""

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_state  # noqa: E402
import installer  # noqa: E402
import recovery_wizard  # noqa: E402
import wizard  # noqa: E402
from modules import storage  # noqa: E402
from test_install_state import Pc  # noqa: E402

SSD_LABEL = storage.DRIVES[0][1]


class LostDriveFirstTests(unittest.TestCase):
    """1. P00 says first, in plain words, that a saved drive is not connected."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.pc = Pc(folder.name)
        self.by_uuid = Path(folder.name) / 'by-uuid'
        self.by_uuid.mkdir()
        self.pc.settings()
        self.pc.steps()
        self.pc.schedule(install_state.VERSION)

    def found(self):
        journals = ((install_state.UPGRADE, self.pc.upgrade), (install_state.DRIVE_MOVE, self.pc.move))
        return install_state.found(self.pc.root, None, self.pc.units, self.pc.early, journals, self.by_uuid)

    def test_the_replaced_ssd_is_said_first(self):
        (self.by_uuid / 'bbbb-2222').write_text('')  # the HDD is there, the SSD is not
        found = self.found()
        self.assertEqual(found['missing'], ['data_ssd'])
        said = wizard.found_said(found)
        self.assertEqual(said[0], 'The drive that held the apps is not connected.')
        self.assertIn('If you replaced it, choose the new one', said[1])
        # Not "its apps and files are on /srv/nekoserver": that drive is gone.
        self.assertTrue(said[2].startswith('NekoServer'))
        self.assertNotIn('Its apps and files are on', said[2])
        self.assertFalse(any('my-config.json' in line for line in said))

    def test_both_drives_there_says_nothing_of_it(self):
        for uuid in ('aaaa-1111', 'bbbb-2222'):
            (self.by_uuid / uuid).write_text('')
        found = self.found()
        self.assertEqual(found['missing'], [])
        self.assertTrue(wizard.found_said(found)[0].startswith('NekoServer'))

    def test_folders_and_shared_folders_have_no_disk_to_miss(self):
        config = {
            'storage': {
                'data_ssd': {'uuid': 'x-1', 'folder': '/srv/nekoserver-system'},
                'media_hdd': {'uuid': 'nfs-1', 'network': {'transport': 'nfs', 'export': 'nas:/media'}},
            }
        }
        self.assertEqual(install_state.missing_drives(config, self.by_uuid), [])
        self.assertEqual(install_state.missing_drives({'storage': {'data_ssd': {'uuid': '../x'}}}, self.by_uuid), [])
        self.assertEqual(install_state.missing_drives({}, self.by_uuid), [])


class StoragePageAsksTests(unittest.TestCase):
    """1. The storage page asks for the new drive; no text says to edit the settings file."""

    def test_a_saved_drive_not_connected_is_asked_for_again(self):
        record, said = {'uuid': 'gone-0001', 'fstype': 'ext4'}, []
        uuid = storage.saved_choice(
            record,
            'gone-0001',
            [],
            'data_ssd',
            SSD_LABEL,
            True,
            inventory={'blockdevices': []},
            interactive=True,
            out_fn=said.append,
        )
        self.assertEqual((uuid, record), ('', {}))
        self.assertIn('The drive that held the apps (UUID gone-0001) is not connected.', said[0])
        self.assertIn('If you replaced it, choose the new one below.', said[0])
        self.assertNotIn('my-config.json', said[0])

    def test_with_nobody_at_the_keyboard_setup_says_how_to_choose_it(self):
        with self.assertRaises(RuntimeError) as stopped:
            storage.saved_choice({'uuid': 'gone-0001'}, 'gone-0001', [], 'media_hdd', 'the HDD', True)
        said = str(stopped.exception)
        self.assertIn('The drive that held the media files and the backups (UUID gone-0001) is not connected.', said)
        self.assertIn('run  sudo bash install.sh  in a terminal on the server and choose the new one', said)
        self.assertNotIn('my-config.json', said)
        # known_fixes.py still knows it as a drive that is not connected.
        import known_fixes

        self.assertEqual(known_fixes.fix_for(said)['id'], 'drive_missing')

    def test_the_storage_page_opens_for_a_drive_that_is_gone(self):
        config = {'storage': {'data_ssd': {'uuid': 'gone-0001'}, 'media_hdd': {'uuid': 'hdd-0001'}}}
        with (
            mock.patch.object(storage.sys, 'stdin', mock.Mock(isatty=mock.Mock(return_value=True))),
            mock.patch.object(storage, 'page_fields', return_value={}),
            mock.patch.object(install_state, 'missing_drives', return_value=['data_ssd']) as missing,
            mock.patch.object(storage, 'drives_ahead') as page,
            mock.patch.object(storage, 'existing_ahead') as films_only,
            mock.patch.object(storage, 'others_ahead'),
        ):
            storage.choose_ahead(config, print)
        missing.assert_called_once_with(config, storage.BY_UUID)
        page.assert_called_once()
        films_only.assert_not_called()

    def test_no_drive_text_tells_the_person_to_edit_the_settings_file(self):
        for text in (storage.LOST_DRIVE, storage.gone_said('data_ssd', 'u', None, False)):
            self.assertNotIn('my-config.json', text)
        source = (ROOT / 'modules' / 'storage.py').read_text()
        start = source.index('def saved_choice(')
        self.assertNotIn('my-config.json', source[start : source.index('def said_last_time(')])


class RetryReadsAgainTests(unittest.TestCase):
    """2. A fix made to the settings while the run went on is kept, so "Retry this step"
    (which reads the settings file again) can pass."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'my-config.json'
        self.start = {
            'storage': {'data_ssd': {'uuid': 'gone-0001', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'hdd-0001'}},
            'dashboard': {'port': 8090},
        }
        self.path.write_text(json.dumps(self.start))

    def test_a_later_save_keeps_the_fix_made_meanwhile(self):
        ctx = installer.Context(json.loads(self.path.read_text()), print, apply=True, config_path=self.path)
        # The person takes the lost SSD out while the run goes on ...
        fixed = json.loads(self.path.read_text())
        del fixed['storage']['data_ssd']['uuid']
        self.path.write_text(json.dumps(fixed))
        # ... and a later step of the same run saves something of its own.
        ctx.config['storage']['media_hdd']['fstype'] = 'xfs'
        ctx.config['dashboard']['port'] = 8091
        ctx.persist_config()
        saved = json.loads(self.path.read_text())
        self.assertNotIn('uuid', saved['storage']['data_ssd'])
        self.assertEqual(saved['storage']['media_hdd'], {'uuid': 'hdd-0001', 'fstype': 'xfs'})
        self.assertEqual(saved['dashboard'], {'port': 8091})
        # What the run takes out goes; the next save starts from this one.
        del ctx.config['dashboard']
        ctx.persist_config()
        self.assertNotIn('dashboard', json.loads(self.path.read_text()))

    def test_a_change_made_before_the_steps_start_is_saved_too(self):
        # installer._execute adds a newer release's steps to the profiles before the run's
        # context is made: they differ from the file as read, so they are saved.
        as_read = json.loads(self.path.read_text())
        config = json.loads(self.path.read_text())
        config['profiles'] = {'full': ['storage', 'docker']}
        ctx = installer.Context(config, print, apply=True, config_path=self.path, saved=as_read)
        ctx.persist_config()
        self.assertEqual(json.loads(self.path.read_text())['profiles'], {'full': ['storage', 'docker']})

    def test_an_unreadable_file_gets_the_run_settings(self):
        ctx = installer.Context(dict(self.start), print, apply=True, config_path=self.path)
        self.path.write_text('{ a comma too many, }')
        ctx.persist_config()
        self.assertEqual(json.loads(self.path.read_text()), self.start)

    def test_the_retry_runs_on_the_settings_as_they_are_now(self):
        # The retry hands the installer the settings file's path, never the settings read
        # at the start: each run of the installer reads it again.
        state = self.path.parent / 'state'
        state.mkdir()
        (state / 'state.json').write_text(json.dumps({'storage': {'status': 'failed', 'reason': 'gone'}}))
        seen = []

        def execute(args):
            seen.append(json.loads(Path(args.config).read_text())['storage']['data_ssd'])
            (state / 'state.json').write_text(json.dumps({'storage': {'status': 'success'}}))
            return 0

        args = argparse.Namespace(config=str(self.path))
        fixed = json.loads(self.path.read_text())
        del fixed['storage']['data_ssd']['uuid']
        self.path.write_text(json.dumps(fixed))
        with (
            mock.patch.object(wizard, 'state_file', return_value=state / 'state.json'),
            mock.patch.object(wizard.setup_progress, 'last', return_value={'steps': ['storage']}),
        ):
            code = wizard.retry_unfinished(execute, args, 1, state, asked=lambda rows: '1', output=lambda line: None)
        self.assertEqual((code, seen), (0, [{'fstype': 'ext4'}]))


class RecoveryMenuTests(unittest.TestCase):
    """3. The recovery menu offers only what this server has."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.media = folder.name

    def offered(self, **settings):
        return [key for key, _ in recovery_wizard.menu({'media_mount': self.media, **settings})]

    def test_no_nextcloud_files_without_nextcloud(self):
        self.assertNotIn('cloud', self.offered(nextcloud={'enabled': False}))
        self.assertIn('cloud', self.offered(nextcloud={'enabled': True}))
        self.assertIn('cloud', self.offered())  # on unless switched off, as modules/nextcloud.py runs it
        self.assertEqual(self.offered(nextcloud={'enabled': False}), ['photos', 'passwords', 'apps', 'host', 'monthly'])


if __name__ == '__main__':
    unittest.main()
