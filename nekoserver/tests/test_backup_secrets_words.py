"""The words the backup password tool (backup_secrets.py) says, word for word: a person
restoring the app password list after a dead drive, or choosing a new backup password,
acts on them; and what it keeps (the old key) and refuses (a snapshot it cannot trust)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_secrets as bs  # noqa: E402
import test_backup_secrets  # noqa: E402

PASSWORD = 'correct horse'
INTRO = [
    'One-time setup: monthly backups store the app password list encrypted.',
    'Choose a backup password for it (at least 8 characters; your login password is fine too).',
    'You are asked for this only now. Nightly and monthly backups run on their own and never ask.',
]
WRITE_IT_DOWN = 'Write it down somewhere safe: without it the password list in your backups cannot be recovered.'
CREATED = 'Backup key created. Restoring the password list will ask for this backup password.'


@unittest.skipUnless(shutil.which('openssl'), 'the openssl command is not installed')
class WordsTests(unittest.TestCase):
    seal_snapshot = test_backup_secrets.BackupSecretsTests.seal_snapshot
    damage_key = test_backup_secrets.BackupSecretsTests.damage_key

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = self.root / 'keys'
        self.log = []

    def make_key(self, password=PASSWORD, keys=None):
        answers = iter([password, password])
        bs.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=keys or self.keys)

    def refused(self, snapshot, destination=None, password=PASSWORD, **more):
        more.setdefault('key_dir', self.keys)  # never the server's own /etc/nekoserver-backups
        with self.assertRaises(RuntimeError) as caught:
            bs.restore(snapshot, destination or self.root / 'out', password, **more)
        self.assertFalse((self.root / 'out').exists())
        return str(caught.exception)

    # ----- setting the backup password up

    def test_setup_says_what_the_password_is_for_and_that_it_is_asked_once(self):
        self.make_key()
        self.assertEqual(self.log, [*INTRO, WRITE_IT_DOWN, CREATED])
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o700)
        self.log.clear()
        bs.setup(ask=self.fail, out=self.log.append, key_dir=self.keys)
        self.assertEqual(self.log, ['Backup password-file key already set up.'])

    def test_no_password_confirmed_says_how_to_try_again(self):
        with self.assertRaises(RuntimeError) as caught:
            bs.setup(ask=lambda prompt: 'short', out=self.log.append, key_dir=self.keys)
        self.assertEqual(
            str(caught.exception),
            'Backup key not created: no backup password was confirmed. Run  sudo python3 backup_secrets.py --setup  '
            'to try again',
        )
        self.assertEqual(self.log.count('Use at least 8 characters.'), 3)
        self.assertFalse(bs.configured(self.keys))

    def test_a_password_of_exactly_eight_characters_is_taken(self):
        self.make_key('12345678')
        self.assertTrue(bs.unlocks('12345678', self.keys / bs.KEY_NAME))

    def test_an_empty_or_multi_line_password_never_protects_the_key(self):
        with self.assertRaisesRegex(RuntimeError, '^An empty password cannot protect the backup key$'):
            bs.create_key('', self.keys)
        with self.assertRaisesRegex(RuntimeError, '^Password cannot contain line breaks$'):
            bs.openssl(['version'], b'', 'two\nlines')
        with self.assertRaisesRegex(RuntimeError, '^Password cannot contain line breaks$'):
            bs.openssl(['version'], b'', 'two\rlines')
        self.assertFalse(self.keys.exists())

    def test_an_openssl_command_that_fails_is_named(self):
        with self.assertRaisesRegex(RuntimeError, '^openssl pkey failed$'):
            bs.openssl(['pkey', '-in', str(self.root / 'no-such-key.p8'), '-noout'])
        self.assertTrue(bs.openssl(['version']).startswith(b'OpenSSL'))

    def test_a_password_reaches_openssl_with_its_line_end(self):
        """openssl reads the password from the pipe up to the line end."""
        self.make_key()
        self.assertTrue(bs.unlocks(PASSWORD, self.keys / bs.KEY_NAME))
        self.assertFalse(bs.unlocks(PASSWORD + 'x', self.keys / bs.KEY_NAME))

    def test_a_key_folder_with_only_one_of_its_two_files_is_not_set_up(self):
        self.make_key()
        for name in (bs.KEY_NAME, bs.CERT_NAME):
            with self.subTest(left=name):
                kept = self.root / name
                shutil.copy(self.keys / name, kept)
                (self.keys / name).unlink()
                self.assertFalse(bs.configured(self.keys))
                shutil.copy(kept, self.keys / name)
        self.assertTrue(bs.configured(self.keys))

    def test_the_two_questions_say_which_password(self):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return PASSWORD

        bs.setup(ask=ask, out=self.log.append, key_dir=self.keys)
        self.assertEqual(asked, ['Backup password: ', 'Same backup password again: '])

    # ----- a new backup password

    def new_password(self, answers, usb_copy=False):
        answers = iter(answers)
        bs.new_password(
            ask=lambda prompt: next(answers),
            out=self.log.append,
            key_dir=self.keys,
            stamp='20261123-120000',
            usb_copy=lambda: usb_copy,
        )

    def test_a_new_password_keeps_the_old_key_beside_it_and_says_what_comes_next(self):
        self.make_key()
        old_key = (self.keys / bs.KEY_NAME).read_bytes()
        self.log.clear()
        self.new_password(['new password', 'new password'])
        self.assertEqual(
            self.log,
            [
                'A new backup password, for the backups from now on.',
                'Backups made before keep the password they were made with: they open only with that one.',
                'Choose the new backup password (at least 8 characters; your login password is fine too).',
                WRITE_IT_DOWN,
                CREATED,
                bs.NEW_LIST_MONTHLY,
            ],
        )
        self.assertEqual((self.keys / f'{bs.KEY_NAME}.before-20261123-120000').read_bytes(), old_key)
        self.assertTrue((self.keys / f'{bs.CERT_NAME}.before-20261123-120000').is_file())
        self.assertTrue(bs.unlocks('new password', self.keys / bs.KEY_NAME))
        self.assertFalse((self.keys / '.new-key-20261123-120000').exists())
        self.assertEqual(
            bs.NEW_LIST_MONTHLY,
            'The next monthly backup makes the first password list with the new backup password. Until then, '
            'every password list in your backups opens only with the old one.',
        )

    def test_the_old_key_is_named_with_the_day_and_time_it_was_set_aside(self):
        self.make_key()
        answers = iter(['new password', 'new password'])
        bs.new_password(
            ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys, usb_copy=lambda: False
        )
        kept = sorted(p.name for p in self.keys.iterdir() if '.before-' in p.name)
        self.assertEqual(len(kept), 2, kept)
        for name in kept:
            self.assertRegex(name, r'^secrets-(key\.p8|recipient\.pem)\.before-\d{8}-\d{6}$')

    def test_with_the_weekly_usb_copy_setup_makes_the_new_list(self):
        self.make_key()
        self.log.clear()
        self.new_password(['new password', 'new password'], usb_copy=True)
        self.assertEqual(
            self.log[-1],
            'Now run  sudo bash install.sh  once: it makes a new password list with the new backup password.',
        )

    def test_no_new_password_confirmed_changes_nothing_and_says_so(self):
        self.make_key()
        before = (self.keys / bs.KEY_NAME).read_bytes()
        with self.assertRaises(RuntimeError) as caught:
            self.new_password(['new password', 'typo'] * 3)
        self.assertEqual(
            str(caught.exception),
            'No new backup password was confirmed, so nothing changed: the backup password is the one you had. '
            'Run  sudo python3 backup_secrets.py --new-password  to try again',
        )
        self.assertEqual((self.keys / bs.KEY_NAME).read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.keys.iterdir()), sorted([bs.KEY_NAME, bs.CERT_NAME]))

    def test_a_new_password_with_no_key_yet_is_the_first_setup(self):
        self.new_password([PASSWORD, PASSWORD])
        self.assertEqual(self.log, [*INTRO, WRITE_IT_DOWN, CREATED])

    # ----- restoring the password list

    def test_a_snapshot_that_is_not_one_is_named_so(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.assertEqual(self.refused(self.root / 'gone'), 'Snapshot directory not found')
        self.assertEqual(self.refused('snapshot'), 'Snapshot must be an absolute path without ".."')
        self.assertEqual(self.refused(f'{snap}/../snapshot'), 'Snapshot must be an absolute path without ".."')
        self.assertEqual(self.refused(f'{snap}\x00'), 'Snapshot must be an absolute path without ".."')
        self.assertEqual(self.refused(snap, Path('out')), 'Restore directory must be an absolute path without ".."')
        manifest = snap / 'manifest.json'
        saved = manifest.read_text()
        manifest.unlink()
        self.assertEqual(self.refused(snap), 'Not a monthly snapshot: manifest.json missing or unreadable')
        manifest.write_text('[1]')
        self.assertEqual(self.refused(snap), 'Not a monthly snapshot')
        manifest.write_text(saved)
        self.assertEqual(bs.restore(snap, self.root / 'out', PASSWORD, key_dir=self.keys), self.root / 'out')

    def test_a_snapshot_missing_a_file_says_which(self):
        self.make_key()
        snap = self.seal_snapshot()
        for name, said in (
            (bs.CERT_NAME, 'Snapshot key copy is missing'),
            (bs.KEY_NAME, 'Snapshot key copy is missing'),
            (bs.ARCHIVE_NAME, 'This snapshot holds no encrypted password files'),
        ):
            with self.subTest(name=name):
                (snap / name).unlink()
                self.assertEqual(self.refused(snap), said)

    def test_a_snapshot_without_a_checksum_for_its_list_is_refused(self):
        self.make_key()
        snap = self.seal_snapshot()
        manifest = json.loads((snap / 'manifest.json').read_text())
        for sums in ({}, {'secrets': 5}, {'secrets': 'ab' * 31 + 'a'}, 'not a table'):
            with self.subTest(sums=sums):
                (snap / 'manifest.json').write_text(json.dumps({**manifest, 'sha256': sums}))
                self.assertEqual(
                    self.refused(snap),
                    'This snapshot has no checksum for its encrypted password files; refusing to restore them',
                )

    def test_a_damaged_list_or_certificate_says_to_use_another_snapshot(self):
        self.make_key()
        snap = self.seal_snapshot()
        manifest = json.loads((snap / 'manifest.json').read_text())
        sums = manifest['sha256']
        (snap / 'manifest.json').write_text(json.dumps({**manifest, 'sha256': {**sums, 'secrets': 'a' * 64}}))
        self.assertEqual(
            self.refused(snap),
            'The encrypted password files in this snapshot are damaged (checksum mismatch); nothing was restored. '
            'Use another monthly snapshot.',
        )
        (snap / 'manifest.json').write_text(json.dumps({**manifest, 'sha256': {**sums, 'secrets_cert': 'a' * 64}}))
        self.assertEqual(
            self.refused(snap),
            'The backup certificate in this snapshot is damaged (checksum mismatch); nothing was restored. '
            'Use another monthly snapshot.',
        )
        # Snapshots from before 0.28.7 record no checksum of the key copy: it is opened as it is.
        (snap / 'manifest.json').write_text(json.dumps({**manifest, 'sha256': {'secrets': sums['secrets']}}))
        self.assertEqual(bs.restore(snap, self.root / 'out', PASSWORD, key_dir=self.keys), self.root / 'out')

    def test_the_new_folder_must_be_new_and_inside_a_folder_that_exists(self):
        self.make_key()
        snap = self.seal_snapshot()
        (self.root / 'exists').mkdir()
        for destination in (self.root / 'exists', self.root / 'missing' / 'out'):
            with self.subTest(destination=destination):
                self.assertEqual(
                    self.refused(snap, destination),
                    'Choose a new directory, inside an existing folder, for the restored password files',
                )

    def test_a_wrong_password_says_which_password_and_what_to_do_when_it_is_forgotten(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.assertEqual(self.refused(snap, password='not it at all'), bs.WRONG_PASSWORD)
        self.assertEqual(
            bs.WRONG_PASSWORD,
            'Wrong password for this backup; nothing was restored. It is the backup password you chose during '
            "setup (not your login password, unless you chose that). Forgotten it? Then this list cannot be "
            "opened. While the server works, each app's login is still shown by  account-setup.py show  "
            '(README.md, "Passwords"), and  sudo python3 backup_secrets.py --new-password  chooses a new backup '
            'password for the backups from now on',
        )

    def test_a_backup_sealed_with_an_earlier_key_asks_for_the_password_of_then(self):
        self.make_key()
        snap = self.seal_snapshot()
        shutil.rmtree(self.keys)
        self.make_key('a newer password')
        self.assertEqual(self.refused(snap, password='a newer password', key_dir=self.keys), bs.EARLIER_KEY)
        self.assertEqual(
            bs.EARLIER_KEY,
            "This backup was made with an earlier backup key, before this server's key was made again (a new "
            'install, or a new backup password), so your current backup password does not open it. Enter the '
            'backup password you had when this backup was made; nothing was restored',
        )

    def test_a_damaged_key_copy_opens_with_the_servers_own_key_and_says_so(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.damage_key(snap)
        self.log.clear()
        out = bs.restore(snap, self.root / 'out', PASSWORD, key_dir=self.keys, out=self.log.append)
        self.assertTrue((out / 'secrets/accounts/APP-CREDENTIALS.txt').is_file())
        self.assertEqual(
            self.log,
            ["The key copy in this snapshot is damaged; this server's own backup key (the same key) opens it instead."],
        )

    def test_a_damaged_key_copy_with_no_same_key_here_says_to_use_another_snapshot(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.damage_key(snap)
        self.assertEqual(
            self.refused(snap, key_dir=self.root / 'no-key-here'),
            'The backup key copy in this snapshot is damaged (checksum mismatch), and this server has no copy of '
            'the same key; nothing was restored. Use another monthly snapshot.',
        )

    def test_an_unpack_that_fails_says_tars_last_line_or_that_it_gave_none(self):
        self.make_key()
        snap = self.seal_snapshot()
        real = subprocess.run

        def tar_fails(stderr):
            def run(argv, *args, **kwargs):
                if argv[0] == 'tar':  # a tar that fails, run with the same options as tar
                    kwargs.pop('input', None)
                    script = 'printf "$1" >&2; exit 2'
                    return real(['sh', '-c', script, 'tar', stderr], *args, **kwargs)
                return real(argv, *args, **kwargs)

            return run

        for stderr, reason in (
            ('tar: one\ntar: No space left on device\n', 'tar: No space left on device'),
            ('', 'no reason given'),
            ('tar: bad \\377 name\n', 'tar: bad \ufffd name'),
        ):
            with self.subTest(stderr=stderr), mock.patch.object(bs.subprocess, 'run', tar_fails(stderr)):
                self.assertEqual(
                    self.refused(snap),
                    f'The password files could not be unpacked ({reason}); nothing was restored. '
                    'Check the free space there, or choose another folder and try again',
                )

        def no_tar(argv, *args, **kwargs):
            if argv[0] == 'tar':
                raise FileNotFoundError(2, 'No such file or directory', 'tar')
            return real(argv, *args, **kwargs)

        with mock.patch.object(bs.subprocess, 'run', no_tar):
            self.assertIn("could not be unpacked ([Errno 2] No such file or directory: 'tar')", self.refused(snap))


class CommandLineTests(unittest.TestCase):
    def test_the_help_names_each_task_and_what_it_takes(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['backup_secrets.py', '--help']), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                bs.main()
        said = ' '.join(out.getvalue().split())
        for words in (
            '--setup Create the backup key, protected by a backup password you choose',
            '--new-password Forgot the backup password: choose a new one for the backups from now on',
            '--restore SNAPSHOT Monthly snapshot directory (backups/monthly/RUN)',
            '--to NEW_DIR New directory for the decrypted password files',
        ):
            self.assertIn(words, said)

    def test_it_runs_as_a_command(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'backup_secrets.py')], capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(result.returncode, 2)
        self.assertRegex(result.stderr, 'error: (Run with sudo|Use --setup, --new-password, or --restore)')

    def run_main(self, *argv, root=True, **patches):
        err = io.StringIO()
        with (
            mock.patch.object(sys, 'argv', ['backup_secrets.py', *argv]),
            mock.patch.object(bs.os, 'geteuid', return_value=0 if root else 1000),
            contextlib.redirect_stderr(err),
            contextlib.ExitStack() as stack,
        ):
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(bs, name, value))
            try:
                bs.main()
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, err.getvalue()

    def test_it_runs_only_with_sudo(self):
        code, err = self.run_main('--setup', root=False)
        self.assertEqual(code, 2)
        self.assertIn('error: Run with sudo', err)

    def test_without_a_task_it_says_the_three_there_are(self):
        for argv in ((), ('--restore', '/srv/snap')):
            with self.subTest(argv=argv):
                code, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                self.assertIn('error: Use --setup, --new-password, or --restore SNAPSHOT --to NEW_DIR', err)

    def test_each_task_runs_its_own_part(self):
        for flag, name in (('--setup', 'setup'), ('--new-password', 'new_password')):
            with self.subTest(flag=flag):
                called = mock.Mock()
                self.assertEqual(self.run_main(flag, **{name: called})[0], 0)
                called.assert_called_once_with()

    def test_a_restore_says_where_the_files_are(self):
        restored = mock.Mock(return_value=Path('/root/passwords'))
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code, _ = self.run_main('--restore', '/srv/snap', '--to', '/root/passwords', asked_restore=restored)
        self.assertEqual(code, 0)
        restored.assert_called_once_with(Path('/srv/snap'), Path('/root/passwords'))
        self.assertEqual(printed.getvalue(), 'Password files restored to /root/passwords\n')

    def test_a_refusal_is_said_and_ctrl_c_stops_quietly(self):
        self.assertEqual(self.run_main('--setup', setup=mock.Mock(side_effect=RuntimeError('said')))[0], 'said')
        for stop in (KeyboardInterrupt, EOFError):
            with self.subTest(stop=stop.__name__):
                self.assertEqual(self.run_main('--setup', setup=mock.Mock(side_effect=stop))[0], '\nStopped.')

    def test_the_password_question_names_which_password(self):
        self.assertEqual(
            bs.RESTORE_PROMPT,
            'Backup password that protected this backup (for backups made before 0.28.5: your sudo password at '
            'the time): ',
        )
        with (
            mock.patch.object(bs.getpass, 'getpass', return_value='pw') as asked,
            mock.patch.object(bs, 'restore', return_value=Path('/x')) as restore,
        ):
            self.assertEqual(bs.asked_restore('/srv/snap', '/x'), Path('/x'))
        asked.assert_called_once_with(bs.RESTORE_PROMPT)
        self.assertEqual(restore.call_args.args, ('/srv/snap', '/x', 'pw'))


class SameFilesAsTheBackupsTests(unittest.TestCase):
    def test_the_key_is_kept_where_the_monthly_backup_looks_for_it(self):
        """The monthly backup (Rust runtime) seals the list to the certificate in this
        folder and copies the key from it into each snapshot."""
        maintenance = (ROOT / 'runtime' / 'src' / 'maintenance.rs').read_text()
        self.assertIn('pub(crate) const SECRETS_KEY_DIR: &str = "%s";' % bs.KEY_DIR, maintenance)
        collect = (ROOT / 'runtime' / 'src' / 'collect.rs').read_text()
        for name in (bs.KEY_NAME, bs.CERT_NAME):
            self.assertIn('"%s/%s"' % (bs.KEY_DIR, name), collect)
        self.assertIn('pub(crate) const SECRETS_ARCHIVE: &str = "%s";' % bs.ARCHIVE_NAME, maintenance)
        self.assertEqual(bs.KEY_DIR, Path('/etc/nekoserver-backups'))


if __name__ == '__main__':
    unittest.main()
