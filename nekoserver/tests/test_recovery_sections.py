"""0.50.0: the recovery menu in three sections (the wizard specification's R01 to R03).

R01 "What would you like to restore?": the backup, its date or version, which files and
     the folder are chosen first; only the newest version and a new folder are offered first.
R02 "Review this restore": the exact source and folder, what is overwritten (nothing) and
     which apps stop (none), then ONE approval. Nothing is written before it, and Enter
     alone never gives it. "Is this a new server?" is gone: the snapshot and Docker are
     looked at instead.
R03 "Check or repair a drive": tests/test_network_storage.py, ReadOnlyCheckTests (a check
     and a repair are two actions; never on a mounted drive or a network share).
T31  recovery never appears on the install path.
T32  exact targets are shown (here for restores; for drives in ReadOnlyCheckTests).
"""

import ast
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import installer  # noqa: E402
import questions  # noqa: E402
import recovery_wizard  # noqa: E402


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


def tree(root):
    """Every file and folder under `root` with its size: what a write would change."""
    return sorted((str(p.relative_to(root)), p.stat().st_size if p.is_file() else -1) for p in Path(root).rglob('*'))


class Server(unittest.TestCase):
    """A server in a temporary folder: an SSD, an HDD with a photo archive, a USB drive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'hdd'
        self.media.mkdir()
        (self.root / 'ssd/installer').mkdir(parents=True)
        self.config = {
            'media_mount': str(self.media),
            'data_mount': str(self.root / 'ssd'),
            'photos': {'archive_dir': 'photos/archive'},
        }
        for name in ('hdd_ready', 'ssd_ready', 'usb_ready'):
            ready = mock.patch.object(recovery_wizard, name, return_value=True)
            ready.start()
            self.addCleanup(ready.stop)
        utc = mock.patch.dict(os.environ, {'TZ': 'UTC'})
        utc.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(utc.stop)
        self.out = []

    def write(self, rel, body=b'x'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def usb(self, mounted=None):
        """The USB drive stood in: mounted at `mounted`, or not plugged in."""
        entered = mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=mounted)
        left = mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None)
        entered.start()
        left.start()
        self.addCleanup(entered.stop)
        self.addCleanup(left.stop)

    def said(self):
        return '\n'.join(self.out)

    def watching(self, *typed):
        """A prompt that answers `typed` and notes, at each question, whether anything under
        the server's folder differs from how it was at the start. What it is asked is kept
        with what was said, in order."""
        before, replies = tree(self.root), iter(typed)
        self.changed_at = []

        def prompt(question=''):
            self.out.append(question)
            self.changed_at.append(tree(self.root) != before)
            return next(replies)

        return prompt


class MenuTests(Server):
    def menu(self, *typed):
        return recovery_wizard.menu_loop(self.config, answers(*typed), self.out.append)

    def test_the_menu_has_two_parts_and_says_first_that_choosing_changes_nothing(self):
        self.assertEqual(self.menu('1', '0', '2', '0', '0'), 0)
        said = self.said()
        for words in (
            'What do you need to do?',
            '  1. Restore something from a backup',
            '  2. Check or repair a drive',
            'What would you like to restore?',
            'Check or repair a drive',
            'Pressing Enter alone never approves anything.',
            'Nothing is written before that.',
            'A check only looks. A repair changes the drive',
        ):
            self.assertIn(words, said)
        self.assertLess(
            said.index('Choosing something here changes nothing yet'), said.index('What do you need to do?')
        )
        # A restore is not sold as more than it is.
        self.assertIn('It does not reinstall Linux or the apps.', said)

    def test_every_entry_of_the_old_menu_is_still_reachable(self):
        """What 0.49.0's one list of eight offered, and where each is now: (part, number)."""
        moved = {
            'Photos and videos (Immich)': ('1', '1', 'photos'),
            'Nextcloud files': ('1', '2', 'cloud'),
            'App password list': ('1', '3', 'passwords'),
            'App settings (nightly backup)': ('1', '4', 'apps'),
            'Steam and Samba settings (nightly backup)': ('1', '5', 'host'),
            'Everything from a monthly snapshot': ('1', '6', 'monthly'),
            'Check a drive that turned read-only': ('2', '1', 'check'),
            'Repair damaged backup copies': ('2', '3', 'copies'),
        }
        for old, (part, number, key) in moved.items():
            with self.subTest(old=old):
                table = recovery_wizard.RESTORES if part == '1' else recovery_wizard.DRIVE_ACTIONS
                called = mock.Mock()
                with mock.patch.dict(table, {key: called}):
                    self.menu(part, number, '0')
                called.assert_called_once()
        # The repair, once the second half of "check a drive", is an entry of its own.
        self.assertEqual([key for key, _ in recovery_wizard.DRIVE_MENU], ['check', 'repair', 'copies'])

    def test_enter_alone_chooses_nothing_in_a_menu(self):
        self.assertEqual(self.menu('', '0'), 0)
        self.assertIn('Please type one of the numbers shown.', self.said())

    def test_stopping_after_something_ran_says_the_apps_may_be_stopped(self):
        """The specification: never "nothing was changed" once something may have been."""

        def stopped(*_):
            raise KeyboardInterrupt

        with mock.patch.dict(recovery_wizard.DRIVE_ACTIONS, {'check': stopped}):
            self.assertEqual(self.menu('2', '1'), 1)
        self.assertIn('the apps may still be stopped: run  sudo bash install.sh', self.out[-1])
        self.out.clear()
        self.assertEqual(recovery_wizard.menu_loop(self.config, stopped, self.out.append), 1)
        self.assertEqual(self.out[-1], '\nStopped. Nothing was changed.')


class RestoreChoiceTests(Server):
    """R01: everything is chosen before anything is written, with the careful choice first."""

    def setUp(self):
        super().setUp()
        self.usb()
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        self.write('hdd/photos/archive/upload/u1/a.neko-20261020T010000Z.jpg', b'photo, edited')
        # The weekly copy's own note: the dated copy is the current version of a.jpg.
        self.write('hdd/photos/archive/upload/u1/.a.jpg.neko-latest', b'a.neko-20261020T010000Z.jpg')
        self.write('ssd/photos/immich-library/upload/live.jpg', b'live')

    def photos(self, *typed):
        recovery_wizard.restore_photos(self.config, self.watching(*typed), self.out.append)
        return self.said()

    def test_the_fields_come_in_order_backup_files_versions_folder_then_the_review(self):
        said = self.photos('1', '1', '1', 'yes')
        order = [
            'Which backup of your photos?',
            'Which versions of each file?',
            'Where should the restored files go?',
            'Review this restore',
            'Restore these items now?',
            'Copying...',
        ]
        places = [said.index(words) for words in order]
        self.assertEqual(places, sorted(places))
        # The backup is named by where it is, not only by "the HDD".
        self.assertIn(f'  1. The HDD: the weekly copy in {self.media}/photos/archive', said)

    def test_only_the_newest_version_and_a_new_folder_are_what_is_offered_first(self):
        said = self.photos('1', '1', '1', 'yes')
        self.assertIn('  1. Only the newest version of each file (recommended)', said)
        self.assertIn('  2. Every version of each file, the older ones too', said)
        self.assertIn('They go into a NEW folder, so nothing you have now is overwritten:', said)
        self.assertIn('  1. Yes, use this folder\n  2. No, I will type another folder', said.replace('\n\n', '\n'))
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual([p.name for p in (restored / 'upload/u1').iterdir()], ['a.jpg'])
        self.assertEqual((restored / 'upload/u1/a.jpg').read_bytes(), b'photo, edited')

    def test_every_version_is_still_there_to_choose(self):
        self.photos('1', '2', '1', 'yes')
        restored = next((self.media / 'restored').iterdir())
        self.assertEqual(
            sorted(p.name for p in (restored / 'upload/u1').iterdir()), ['a.jpg', 'a.neko-20261020T010000Z.jpg']
        )

    def test_going_back_at_any_field_restores_nothing_and_says_so(self):
        for typed in (('1', '0'), ('1', '1', '0'), ('1', '1', '2', '')):
            with self.subTest(typed=typed):
                self.out.clear()
                self.photos(*typed)
                self.assertEqual(self.out[-1], 'Nothing was restored and nothing was changed.')
                self.assertFalse((self.media / 'restored').exists())
                self.assertFalse(any(self.changed_at))


class ReviewTests(Server):
    """R02: one review of everything, one approval, and nothing written before it."""

    def setUp(self):
        super().setUp()
        self.usb(self.root / 'usb')
        self.write('hdd/photos/archive/upload/u1/a.jpg', b'photo')
        self.write('usb/nextcloud/alice/files/todo.txt', b'todo')
        self.write('ssd/photos/immich-library/upload/live.jpg', b'live')

    def test_the_review_names_the_exact_backup_folder_and_what_stays_untouched(self):
        """Also T32: the exact source and target of a restore are shown before it."""
        recovery_wizard.restore_photos(self.config, self.watching('1', '1', '1', 'yes'), self.out.append)
        said = self.said()
        review = said[said.index('Review this restore') : said.index('Restore these items now?')]
        restored = next((self.media / 'restored').iterdir())
        for line in (
            '  Restore:      Photos and videos (Immich)',
            f'  From:         The HDD: the weekly copy in {self.media}/photos/archive',
            '  Which files:  everything this backup ever kept, also files you deleted or renamed: the newest '
            'version of each (1 file)',
            '  Size:         less than 1 MB (free space where it goes: ',
            f'  Into:         {restored} (a new folder)',
            f'  Overwritten:  nothing. Your Immich library ({self.root}/ssd/photos/immich-library) stays as it is.',
            '  Apps:         none is stopped. They keep running while this is restored.',
        ):
            self.assertIn(line, review)
        self.assertEqual((self.root / 'ssd/photos/immich-library/upload/live.jpg').read_bytes(), b'live')

    def test_nothing_is_written_before_the_approval(self):
        """Not the restore, not the HDD's restore folder: the server is as it was at every
        question, the approval included."""
        restores = {
            'photos': (recovery_wizard.restore_photos, ('1', '1', '1', 'yes')),
            'Nextcloud files': (recovery_wizard.restore_cloud, ('1', '1', '1', 'yes')),
        }
        for name, (restore, typed) in restores.items():
            with self.subTest(restore=name):
                restore(self.config, self.watching(*typed), self.out.append)
                self.assertEqual(self.changed_at, [False] * len(typed))
                self.assertIn('Done: 1 file copied', self.said())  # and after it, the restore is made
                self.out.clear()

    def test_enter_alone_never_approves_and_no_restores_nothing(self):
        recovery_wizard.restore_photos(self.config, self.watching('1', '1', '1', '', '', 'no'), self.out.append)
        self.assertIn('Please type yes or no.', self.out)
        self.assertEqual(self.out[-1], 'Nothing was restored.')
        self.assertEqual(self.said().count('Restore these items now?'), 3)  # asked again each time
        self.assertFalse(any(self.changed_at))
        self.assertFalse((self.media / 'restored').exists())

    def test_the_question_says_what_enter_does(self):
        asked = questions.terminal('restore_approve')
        self.assertIn('Restore these items now?', asked)
        self.assertIn('Nothing has been changed so far.', asked)
        self.assertIn('Enter alone does nothing.', asked)
        self.assertEqual(questions.window('restore_approve')['choices'][0]['label'], 'Restore selected items')

    def test_a_restore_with_no_room_stops_at_the_review_before_the_question(self):
        full = RuntimeError('The restore needs 9 GB; only 1 GB is free')
        with mock.patch.object(recovery_wizard, 'ensure_room', side_effect=full), self.assertRaises(RuntimeError):
            recovery_wizard.restore_photos(self.config, self.watching('1', '1', '1'), self.out.append)
        self.assertNotIn('Restore these items now?', self.said())
        self.assertFalse(any(self.changed_at))

    def nightly(self):
        run = self.media / 'backups/media-applications/snapshot-1792900000000000-1'
        run.mkdir(parents=True)
        (run / 'manifest.json').write_text('{}')
        return run

    def test_the_tools_run_only_after_the_approval(self):
        """The nightly backups, the monthly snapshot and the password list are restored by
        tools of their own: none is started, and no folder made, before the yes."""
        self.nightly()
        month = self.media / 'backups/monthly/1792900000000000-1'
        month.mkdir(parents=True)
        (month / 'manifest.json').write_text(json.dumps({'apps': ['immich', 'jellyfin']}))
        (month / 'restore-tests.json').write_text('{}')
        (month / 'secrets.tar.cms').write_bytes(b'sealed')
        restores = {
            'app settings': lambda prompt: recovery_wizard.restore_nightly(
                self.config, 'apps', prompt, self.out.append
            ),
            'monthly snapshot': lambda prompt: recovery_wizard.restore_monthly(self.config, prompt, self.out.append),
            'password list': lambda prompt: recovery_wizard.restore_passwords(self.config, prompt, self.out.append),
        }
        for name, restore in restores.items():
            for approval in ('no', 'yes'):
                with self.subTest(restore=name, approval=approval), mock.patch.object(recovery_wizard, 'tool') as tool:
                    self.out.clear()
                    restore(self.watching('1', '1', '', approval))
                    self.assertEqual(tool.called, approval == 'yes')
                    self.assertFalse(any(self.changed_at))
                    said = self.said()
                    self.assertIn('  Overwritten:  nothing.', said)
                    self.assertIn('  Apps:         none is stopped.', said)
                    if approval == 'no':
                        self.assertEqual(self.out[-1], 'Nothing was restored and nothing was changed.')
        # Each review names its exact backup.
        self.assertIn(f'  From:         {month} (made ', said)

    def test_the_monthly_review_says_what_a_snapshot_holds_and_does_not(self):
        month = self.media / 'backups/monthly/1792900000000000-1'
        month.mkdir(parents=True)
        (month / 'manifest.json').write_text(json.dumps({'apps': ['immich', 'jellyfin']}))
        (month / 'restore-tests.json').write_text('{}')
        with mock.patch.object(recovery_wizard, 'tool', return_value=True):
            recovery_wizard.restore_monthly(self.config, answers('1', '1', 'yes'), self.out.append)
        said = self.said()
        self.assertIn('  Apps in it:   immich, jellyfin', said)
        self.assertIn('  Not in it:    Linux itself, your photos, your Nextcloud files, movies, shows and music.', said)


class AppVersionTests(Server):
    """R02: "Is this a new server?" is not asked. What the snapshot saved and what Docker
    on this PC has are looked at; a question comes only when there is a real choice."""

    def setUp(self):
        super().setUp()
        self.month = self.media / 'backups/monthly/1792900000000000-1'
        self.month.mkdir(parents=True)
        (self.month / 'manifest.json').write_text(json.dumps({'apps': ['immich']}))
        (self.month / 'restore-tests.json').write_text('{}')
        self.ids = ['sha256:' + hashlib.sha256(name).hexdigest() for name in (b'one', b'two', b'three')]

    def images(self, configs):
        listed = json.dumps([{'Config': config, 'RepoTags': None, 'Layers': []} for config in configs]).encode()
        with tarfile.open(self.month / 'images.tar', 'w') as archive:
            entry = tarfile.TarInfo('manifest.json')
            entry.size = len(listed)
            archive.addfile(entry, io.BytesIO(listed))

    def saved(self):
        """images.tar as Docker writes it: newer ones name blobs/sha256/<id>, older ones <id>.json."""
        one, two, three = (name.split(':')[1] for name in self.ids)
        self.images([f'blobs/sha256/{one}', f'{two}.json', f'blobs/sha256/{three}'])

    def restore(self, *typed, missing):
        calls = []
        with (
            mock.patch.object(recovery_wizard, 'tool', side_effect=lambda *a: calls.append(a) or True),
            mock.patch.object(recovery_wizard, 'versions_missing', side_effect=missing) as looked,
        ):
            recovery_wizard.restore_monthly(self.config, self.watching(*typed), self.out.append)
        self.looked = looked
        return calls[0] if calls else None

    def test_the_saved_versions_are_read_from_the_snapshot(self):
        self.saved()
        self.assertEqual(recovery_wizard.saved_app_versions(self.month), sorted(self.ids))
        self.images(['../../etc/passwd'])
        self.assertIsNone(recovery_wizard.saved_app_versions(self.month))
        (self.month / 'images.tar').write_bytes(b'not an archive')
        self.assertIsNone(recovery_wizard.saved_app_versions(self.month))

    def test_docker_is_asked_which_of_them_it_has(self):
        def docker(argv, **kwargs):
            return mock.Mock(returncode=1 if argv[-1] == self.ids[1] else 0)

        self.assertEqual(recovery_wizard.versions_missing(self.ids, docker), [self.ids[1]])
        self.assertIsNone(recovery_wizard.versions_missing(self.ids, lambda argv, **k: mock.Mock(returncode=1)))
        self.assertIsNone(recovery_wizard.versions_missing(self.ids, mock.Mock(side_effect=FileNotFoundError)))

    def test_with_every_version_on_this_pc_nothing_is_asked_or_loaded(self):
        self.saved()
        ran = self.restore('1', '1', 'yes', missing=lambda ids: [])
        self.assertNotIn('--load-images', ran)
        self.looked.assert_called_once_with(sorted(self.ids))
        said = self.said()
        self.assertIn('all 3 app versions this snapshot was made with are on this PC already', said)
        self.assertNotIn('Put the app versions saved in this snapshot back', said)

    def test_versions_missing_here_are_a_real_choice_shown_in_the_review(self):
        self.saved()
        ran = self.restore('1', '1', 'yes', 'yes', missing=lambda ids: ids[:2])
        self.assertIn('--load-images', ran)
        said = self.said()
        self.assertIn('Put the app versions saved in this snapshot back on this PC too?', said)
        self.assertIn('2 of the 3 app versions this snapshot was made with are not on this PC.', said)
        self.assertIn('  App versions: put back too (', said)
        self.assertLess(said.index('Put the app versions'), said.index('Review this restore'))
        self.out.clear()
        ran = self.restore('1', '1', 'no', 'yes', missing=lambda ids: ids[:2])
        self.assertNotIn('--load-images', ran)
        self.assertIn('  App versions: not put back: you chose to unpack only the settings and databases.', self.said())

    def test_without_docker_they_are_not_loaded_and_the_review_says_why(self):
        self.saved()
        ran = self.restore('1', '1', 'yes', missing=lambda ids: None)
        self.assertNotIn('--load-images', ran)
        self.assertIn('(Docker) is not installed or not running on this PC', self.said())

    def test_a_snapshot_without_saved_versions_asks_nothing(self):
        ran = self.restore('1', '1', 'yes', missing=mock.Mock(side_effect=AssertionError))
        self.assertNotIn('--load-images', ran)
        self.assertIn('this snapshot has no saved app versions', self.said())

    def test_nobody_is_asked_whether_the_server_is_new(self):
        for entry in questions.Q.values():
            if entry['screen'] == 'Recovery':
                self.assertNotIn('new server', (entry['title'] + entry.get('hint', '')).lower())
        self.assertNotIn('Is this a new server', (ROOT / 'recovery_wizard.py').read_text())


RECOVERY_QUESTIONS = [key for key, entry in questions.Q.items() if entry['screen'] == 'Recovery']


class InstallPathTests(unittest.TestCase):
    """T31: recovery and repair never appear on the normal install path. They are reached
    by `install.sh --recover` alone."""

    def sources(self):
        """The program's own files (not its tests, not the recovery menu itself)."""
        found = [p for p in ROOT.glob('*.py')] + [p for p in (ROOT / 'modules').glob('*.py')]
        return [p for p in found if p.name != 'recovery_wizard.py']

    def test_only_install_sh_recover_starts_the_recovery_menu(self):
        starts = []
        for path in self.sources():
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                if 'recovery_wizard' in names:
                    starts.append(path.name)
        self.assertEqual(starts, ['installer.py'])
        # ... and there, only under --recover.
        tree = ast.parse((ROOT / 'installer.py').read_text())
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == 'args.recover'
            and 'recovery_wizard.run' in ast.unparse(node)
        ]
        self.assertEqual(len(guarded), 1)
        self.assertEqual(ast.unparse(tree).count('recovery_wizard'), ast.unparse(guarded[0]).count('recovery_wizard'))

    def test_no_step_of_setup_asks_a_recovery_question(self):
        self.assertEqual(
            RECOVERY_QUESTIONS, ['restore_approve', 'restore_app_versions', 'check_drive', 'repair_drive', 'fix_copies']
        )
        for path in self.sources():
            if path.name == 'questions.py':
                continue
            text = path.read_text()
            for key in RECOVERY_QUESTIONS:
                with self.subTest(file=path.name, question=key):
                    self.assertNotIn(f"'{key}'", text)

    def test_setup_and_its_other_commands_never_reach_the_recovery_menu(self):
        """Run for real: the window's setup, a plain run and a check. The recovery menu
        stands in as something that fails the test if anything touches it."""
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / 'my-config.json'
            settings.write_text('{}')
            recovery = mock.Mock(run=mock.Mock(side_effect=AssertionError('recovery started')))
            with (
                mock.patch.dict(sys.modules, recovery_wizard=recovery),
                mock.patch.object(installer, 'execute', return_value=0) as execute,
                mock.patch('wizard.run', return_value=0) as setup,
                mock.patch('builtins.print'),
            ):
                self.assertEqual(installer.main(['--wizard']), 0)
                self.assertEqual(installer.main(['--config', str(settings)]), 0)
                self.assertEqual(installer.main(['--check', '--config', str(settings)]), 0)
            self.assertEqual((setup.call_count, execute.call_count), (1, 2))
            recovery.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
