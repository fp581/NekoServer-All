"""Gate run C17: the Music group's own drive (d3) was lost and replaced by a fresh disk.
Setup told the person to edit my-config.json, crashed after the approval because the
settings still named d3, left Lidarr running on the lost drive's empty folder, and the
empty folders Docker had made there blocked mounting the new drive.

Now: the first page and the storage page say plainly that the drive for Music is not
connected; the page asks for the drive that replaces it among the drive list; nothing
in the settings names the lost drive any more; the group's apps are made again on the
new drive (which gets a name of its own) with empty settings, and the finishing page
says so; empty folders left in a mount folder are taken out, files never."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
import install_state  # noqa: E402
import neko_common  # noqa: E402
import placement  # noqa: E402
import wizard  # noqa: E402
from modules import storage  # noqa: E402


def settings(top):
    return {
        'data_mount': str(top / 'ssd'),
        'media_mount': str(top / 'hdd'),
        'storage': {
            'data_ssd': {'uuid': 'ssd-1', 'fstype': 'ext4'},
            'media_hdd': {'uuid': 'hdd-1', 'fstype': 'ext4'},
            'd3': {'uuid': 'lost-3', 'fstype': 'ext4'},
        },
        'locations': {'music': 'd3'},
        'install': {'music': 'd3'},
        'media': {'services': ['jellyfin', 'navidrome', 'lidarr']},
    }


class LostGroupDriveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.top = Path(tmp.name)
        self.by_uuid = self.top / 'by-uuid'
        self.by_uuid.mkdir()
        for uuid in ('ssd-1', 'hdd-1'):
            (self.by_uuid / uuid).touch()
        self.config = settings(self.top)
        self.config['placement'] = placement.derived(self.config)
        drive_table.STARTED_EMPTY.clear()
        self.addCleanup(drive_table.STARTED_EMPTY.clear)

    def test_the_first_page_names_the_drive_by_what_it_holds(self):
        found = install_state.missing_group_drives(self.config, self.by_uuid)
        self.assertEqual(found, {'d3': 'Music (Navidrome)'})
        lines = wizard.found_said({'kind': 'installed', 'missing': [], 'missing_groups': found})
        self.assertEqual(lines[0], 'The drive for Music (Navidrome) is not connected.')
        self.assertEqual(lines[1], wizard.MISSING_NEXT)
        # Connected: nothing is said. A folder a NAS shares is never "lost" here.
        (self.by_uuid / 'lost-3').touch()
        self.assertEqual(install_state.missing_group_drives(self.config, self.by_uuid), {})
        (self.by_uuid / 'lost-3').unlink()
        self.config['storage']['d3']['network'] = {'transport': 'nfs', 'export': 'nas:/music'}
        self.assertEqual(install_state.missing_group_drives(self.config, self.by_uuid), {})

    def test_the_storage_page_says_it_plainly_and_gives_the_drive_up_on_its_copy(self):
        said, chosen = [], {}
        draft = json.loads(json.dumps(self.config))
        storage.lost_ahead(draft, chosen, said.append, str(self.by_uuid) + '/')
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith('The drive for Music (Navidrome) (UUID lost-3) is not connected.'))
        self.assertIn('choose the drive that replaces it below', said[0])
        self.assertIn('"App settings, from a nightly backup"', said[0])
        self.assertNotIn('my-config.json', said[0])
        self.assertEqual(chosen['lost']['d3']['what'], 'Music (Navidrome)')
        self.assertEqual(set(chosen['lost']['d3']['apps']), {'navidrome', 'lidarr'})
        # Nothing in the copy names the lost drive: the page asks where Music goes now.
        self.assertNotIn('"d3"', json.dumps(draft))
        self.assertIn('music', storage.page_fields(draft))

    def test_once_approved_nothing_in_the_settings_names_the_lost_drive(self):
        storage.give_up_placed(self.config, 'd3')
        self.assertNotIn('d3', json.dumps(self.config))
        self.assertNotIn('d3', self.config['storage'])
        self.assertEqual((self.config['locations'], self.config['install']), ({}, {}))
        self.assertEqual(placement.table(self.config)['music'], {'install': 'ssd', 'data': 'ssd'})
        # The settings check that stopped setup after the approval passes now.
        neko_common.location(self.config, 'music')

    def test_the_groups_apps_are_made_again_and_the_finishing_page_says_their_settings_start_empty(self):
        ran = []

        def run(argv, **_):
            ran.append(argv)
            listed = argv[:3] == ['docker', 'ps', '-aq']
            app = argv[-1].rsplit('neko-', 1)[1] if listed else ''
            return SimpleNamespace(returncode=0, stdout=f'id-{app}\n' if listed else '')

        lost = {'d3': {'what': 'Music (Navidrome)', 'apps': ['navidrome', 'lidarr']}}
        with mock.patch.object(neko_common, 'EXTRA_MOUNTS', self.top / 'drives'):
            storage.settings_start_empty(lost, run)
        removed = [argv for argv in ran if argv[:3] == ['docker', 'rm', '-f']]
        self.assertEqual(removed, [['docker', 'rm', '-f', 'id-navidrome'], ['docker', 'rm', '-f', 'id-lidarr']])
        lines = wizard.started_empty_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('Music (Navidrome): set up again on the drive that replaced the lost one'))
        self.assertIn('with empty settings', lines[0])
        self.assertIn('"App settings, from a nightly backup"', lines[0])
        import finish_page

        summary = {'apps': [], 'tests': [], 'pending': [], 'attention': [], 'server': {}, 'outcome': 'ready'}
        with (
            mock.patch.object(finish_page, 'dashboard_shown', return_value=None),
            mock.patch.object(finish_page, 'secrets_shown', return_value=[]),
        ):
            self.assertEqual(finish_page.page(self.config, summary)['waiting'], lines)

    def test_the_finishing_screen_still_ends_with_its_rule_after_the_started_empty_line(self):
        drive_table.STARTED_EMPTY['d3'] = {'what': 'Music (Navidrome)'}
        said = []
        with (
            mock.patch.object(wizard, 'show_end'),
            mock.patch.object(wizard, 'planned', return_value=(['system'], {'system': {'status': 'success'}})),
        ):
            wizard.finishing_screen({'data_mount': '/srv/nekoserver'}, {}, output=said.append)
        self.assertIn('\n' + wizard.started_empty_lines()[0], said)
        self.assertEqual(said[-1], '=' * 64 + '\n')

    def test_the_new_drive_never_gets_the_lost_drives_name(self):
        drives = self.top / 'drives'
        (drives / 'd3').mkdir(parents=True)  # the lost drive's mount folder is still there
        config = {'storage': {'data_ssd': {'uuid': 'ssd-1'}, 'media_hdd': {'uuid': 'hdd-1'}}}
        with (
            mock.patch.object(neko_common, 'EXTRA_MOUNTS', drives),
            mock.patch.object(storage, 'new_drive'),
            mock.patch.object(storage, 'whole'),
            mock.patch.object(storage, 'unlocked'),
        ):
            self.assertTrue(storage.used_before('d3'))
            self.assertEqual(storage.placed_on(config, {'uuid': 'fresh-4', 'device': '/dev/sdd'}), 'd4')


class LeftoverTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / 'd3'
        for inside in ('library/downloads/music', 'library/existing', 'library/music'):
            (self.folder / inside).mkdir(parents=True)

    def test_empty_folders_left_by_docker_are_taken_out(self):
        self.assertTrue(storage.clear_leftovers(self.folder))
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertTrue(self.folder.is_dir(), 'the mount folder itself stays')

    def test_a_file_is_never_removed_nor_the_folders_that_hold_it(self):
        (self.folder / 'library/music/song.flac').write_bytes(b'x')
        outside = Path(self.folder.parent) / 'outside'
        (outside / 'kept').mkdir(parents=True)
        (self.folder / 'link').symlink_to(outside)
        self.assertFalse(storage.clear_leftovers(self.folder))
        self.assertTrue((self.folder / 'library/music/song.flac').is_file())
        self.assertFalse((self.folder / 'library/existing').exists(), 'the empty ones still go')
        self.assertTrue((outside / 'kept').is_dir(), 'a link is never followed')

    def test_the_mount_check_says_files_are_never_removed(self):
        (self.folder / 'library/music/song.flac').write_bytes(b'x')
        spec = [('d3', 'new-4', 'ext4', str(self.folder), None)]
        with (
            mock.patch.object(storage, 'shared_folders', return_value={}),
            mock.patch.object(storage, 'system_folders', return_value={}),
            mock.patch.object(storage, 'validate_device'),
            mock.patch.object(storage, 'waits_for_first_use', return_value=False),
            mock.patch.object(storage, 'findmnt', return_value=None),
            self.assertRaises(RuntimeError) as caught,
        ):
            storage.checked_before_mount({}, spec)
        self.assertIn('NekoServer never removes files', str(caught.exception))
        self.assertTrue((self.folder / 'library/music/song.flac').is_file())


if __name__ == '__main__':
    unittest.main()
