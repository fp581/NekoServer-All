"""Wizard specification T21: the number of app backups to keep (app_backup.keep_snapshots)
is never applied to the photo archive, the media, the music backup or the offline (USB)
copy. Nothing there is removed because "only N are kept".

The runtime's backup code is read as functions and the calls between them
(spec_sources). The count is applied in one place, common::prune_snapshots, which
removes only folders named snapshot-<number>-<number> in the folder it is given.

What breaks each test (one line each):
- a call to common::prune_snapshots (or backup::publish_snapshot) in backup::archive_with,
  backup::mirror or music_backup::run fails
  test_no_archive_media_or_offline_job_can_reach_the_snapshot_count;
- reading ["app_backup"]["keep_snapshots"] in backup.rs fails
  test_the_app_backup_count_is_read_only_by_the_app_backup_job;
- changing the snapshot name pattern in common::complete_snapshots to r"^.*$" fails
  test_the_count_only_ever_removes_snapshot_folders;
- a new function in backup.rs that calls fs::remove_file fails
  test_every_place_the_backup_code_deletes_is_a_known_one (look at it, then add it);
- '--keep' in photo_preservation.command_line fails
  test_no_archive_or_copy_command_carries_the_app_backup_count.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import spec_sources  # noqa: E402
from modules import photo_preservation  # noqa: E402

# Where the count of snapshots is applied, and what leads there.
SNAPSHOT_COUNT = [
    ('common', 'prune_snapshots'),
    ('common', 'remove_snapshot'),
    ('backup', 'publish_snapshot'),
    ('backup', 'make_room'),
    ('backup', 'make_room_with'),
]
# The jobs and job parts that keep everything: none may get to the count.
KEEP_EVERYTHING = {
    'the photo archive (weekly, with the Immich database copy)': ('backup', 'preservation'),
    'the photo archive pass itself': ('backup', 'archive_with'),
    'the photo archive, run by hand': ('backup', 'archive'),
    'the monthly check of the archive': ('backup', 'monthly_archive_check'),
    'the offline copy to the USB drive (weekly)': ('backup', 'weekly'),
    'the offline copy itself': ('backup', 'copy_to_usb'),
    'the copy of a folder tree (photos to USB; copy by hand)': ('backup', 'mirror'),
    'the Nextcloud files to the HDD and the USB drive': ('backup', 'cloud_to_usb'),
    'the music backup': ('music_backup', 'run'),
    'the media check': ('media', 'run'),
}
# Every function of backup.rs that deletes anything, and what it deletes. A new one fails
# the test below: read it, make sure no count reaches photos, media or the offline copy,
# then add it here.
DELETES_IN_BACKUP = {
    'copy_verified_with': 'its own unfinished copy of one file',
    'clear_partials': 'unfinished copies left by a run that was cut off',
    'follow_moves': 'on the USB drive, the note of a file it moved aside as the HDD archive did (the file is moved)',
    'remove_emptied': 'a folder that move left empty, with the notes in it',
    'keep_newest_lists': 'password lists on the USB drive beyond the newest three',
    'copy_newest_of': 'a damaged database copy on the USB drive, before it is copied again',
    'copy_newest_secrets': 'a damaged password list on the USB drive, before it is copied again',
    'seal_passwords_at': 'the password list it was making, when making it failed',
    'execute_keeping': "the job's own \"running\" report",
    'finish_journal': "the job's own resume journal, once its work is done",
    'make_room_with': 'the oldest app snapshots, when the HDD is short of room',
    'database_of': 'its own unfinished database copy',
    'clear_stale_dumps': 'unfinished database copies older than an hour',
    'prune_dumps_of': 'database copies beyond the newest four (files named <app>-<date> only)',
}


class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = spec_sources.rust_functions()
        cls.calls = spec_sources.rust_calls(cls.functions)

    def test_the_reading_sees_the_count_where_it_is_applied(self):
        for function in SNAPSHOT_COUNT + list(KEEP_EVERYTHING.values()):
            self.assertIn(function, self.functions)
        app_job = spec_sources.rust_reachable(self.calls, ('app_snapshot', 'run'))
        settings_job = spec_sources.rust_reachable(self.calls, ('host_snapshot', 'run'))
        for function in SNAPSHOT_COUNT[:3]:
            self.assertIn(function, app_job)  # the app backup does count
            self.assertIn(function, settings_job)

    def test_no_archive_media_or_offline_job_can_reach_the_snapshot_count(self):
        for what, start in KEEP_EVERYTHING.items():
            reached = spec_sources.rust_reachable(self.calls, start)
            self.assertGreater(len(reached), 3, what)
            for function in SNAPSHOT_COUNT:
                chain = spec_sources.rust_path(self.calls, start, function)
                self.assertIsNone(chain, f'{what} reaches the snapshot count: {chain}')

    def test_the_count_is_applied_by_one_function_called_from_one_place(self):
        callers = lambda goal: sorted(caller for caller, called in self.calls.items() if goal in called)  # noqa: E731
        self.assertEqual(callers(('common', 'prune_snapshots')), [('backup', 'publish_snapshot')])
        self.assertEqual(
            callers(('common', 'remove_snapshot')), [('backup', 'make_room_with'), ('common', 'prune_snapshots')]
        )
        self.assertEqual(
            callers(('backup', 'publish_snapshot')), [('app_snapshot', 'snapshot'), ('host_snapshot', 'snapshot')]
        )
        self.assertEqual(callers(('backup', 'make_room')), [('app_snapshot', 'snapshot')])

    def test_the_app_backup_count_is_read_only_by_the_app_backup_job(self):
        readers = [name for name, body in self.functions.items() if '["app_backup"]["keep_snapshots"]' in body]
        self.assertEqual(readers, [('app_snapshot', 'snapshot')])
        # Each count has its own setting; the app backup's is not borrowed anywhere.
        self.assertIn('["host_backup"]["keep_snapshots"]', self.functions[('host_snapshot', 'snapshot')])
        self.assertIn('["maintenance"]["keep_snapshots"]', self.functions[('maintenance', 'keep_snapshots')])

    def test_the_count_only_ever_removes_snapshot_folders(self):
        listing = self.functions[('common', 'complete_snapshots')]
        self.assertIn('r"^snapshot-([0-9]+)-([0-9]+)$"', listing)
        pruning = self.functions[('common', 'prune_snapshots')]
        self.assertIn('complete_snapshots(root)', pruning)
        self.assertIn('remove_snapshot(root, &name)', pruning)
        # The folders it is given hold app and settings snapshots, nothing else.
        self.assertIn('media.join("backups/media-applications")', self.functions[('app_snapshot', 'snapshot')])
        self.assertIn('media.join("backups/host-user")', self.functions[('host_snapshot', 'snapshot')])
        for body in (self.functions[('app_snapshot', 'snapshot')], self.functions[('host_snapshot', 'snapshot')]):
            self.assertRegex(body, r'publish_snapshot\(\s*&root,')

    def test_database_copies_have_their_own_count_and_only_their_own_names(self):
        pruning = self.functions[('backup', 'prune_dumps_of')]
        self.assertIn('if is_dump(app, &name)', pruning)
        self.assertRegex(self.functions[('backup', 'is_dump')], r'strip_prefix\(app\)')
        self.assertNotIn('keep_snapshots', pruning)
        self.assertIn('a.number("db-keep", 4)', self.functions[('backup', 'preservation')])

    def test_every_place_the_backup_code_deletes_is_a_known_one(self):
        deleting = re.compile(r'\b(remove_file|remove_dir_all|remove_dir|remove_snapshot)\(')
        found = {name for (file, name), body in self.functions.items() if file == 'backup' and deleting.search(body)}
        self.assertEqual(found, set(DELETES_IN_BACKUP))
        # The music backup and the media check delete nothing at all.
        for file in ('music_backup', 'media'):
            self.assertEqual(
                [name for (f, name), body in self.functions.items() if f == file and deleting.search(body)], []
            )

    def test_no_archive_or_copy_command_carries_the_app_backup_count(self):
        line = photo_preservation.command_line('BIN', 'CFG', 'DATA', 'MEDIA', 600, 'ARCHIVE', 4)
        self.assertEqual(re.findall(r'--[a-z-]*keep[a-z-]*', line), ['--db-keep'])
        # In setup, the setting is named only where the three snapshot jobs are set up.
        named = sorted(
            name
            for name, tree in spec_sources.python_files().items()
            if any(isinstance(node, ast.Constant) and node.value == 'keep_snapshots' for node in ast.walk(tree))
        )
        self.assertEqual(
            named, ['installer.py', 'modules/app_backup.py', 'modules/host_backup.py', 'modules/maintenance.py']
        )
        # And in every set of unit files setup writes (the golden files), no job but those
        # is started with a count.
        checked = 0
        for unit in sorted((spec_sources.PROJECT / 'tests' / 'golden').glob('*/*.service')):
            for start in re.findall(r'ExecStart=.*', unit.read_text(encoding='utf-8')):
                checked += 1
                self.assertEqual([w for w in re.findall(r'--[a-z-]*keep[a-z-]*', start) if w != '--db-keep'], [], unit)
        self.assertGreater(checked, 50)


if __name__ == '__main__':
    unittest.main()
