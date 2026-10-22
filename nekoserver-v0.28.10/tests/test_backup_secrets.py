"""The app password list in backups opens only with the backup password."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_secrets  # noqa: E402

SECRET = b'jellyfin admin hunter2-topsecret\n'


class BackupSecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.keys = self.root / 'keys'
        self.log = []

    def make_key(self, password='correct horse'):
        answers = iter([password, password])
        backup_secrets.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys)

    def seal_snapshot(self):
        """Build a snapshot the way the Rust worker does: tar | openssl cms -encrypt."""
        data = self.root / 'ssd'
        (data / 'secrets/accounts').mkdir(parents=True)
        (data / 'secrets/accounts/APP-CREDENTIALS.txt').write_bytes(SECRET)
        snap = self.root / 'snapshot'
        snap.mkdir()
        (snap / 'manifest.json').write_text('{"format": "neko-maintenance-v1"}')
        archive = subprocess.run(['tar', '--create', '--file', '-', '-C', str(data), 'secrets/accounts'],
                                 capture_output=True, check=True).stdout
        subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes256', '-outform', 'DER',
                        '-out', str(snap / backup_secrets.ARCHIVE_NAME), str(self.keys / backup_secrets.CERT_NAME)],
                       input=archive, check=True)
        for name in (backup_secrets.KEY_NAME, backup_secrets.CERT_NAME):
            (snap / name).write_bytes((self.keys / name).read_bytes())
        sums = {key: hashlib.sha256((snap / name).read_bytes()).hexdigest() for key, name in (
            ('secrets', backup_secrets.ARCHIVE_NAME), ('secrets_key', backup_secrets.KEY_NAME),
            ('secrets_cert', backup_secrets.CERT_NAME))}
        (snap / 'manifest.json').write_text(json.dumps({'format': 'neko-maintenance-v1', 'sha256': sums}))
        return snap

    def damage_key(self, snap):
        key = snap / backup_secrets.KEY_NAME
        text = bytearray(key.read_bytes())
        text[200] = ord('A') if text[200] != ord('A') else ord('B')  # inside the base64 body
        key.write_bytes(bytes(text))

    def test_setup_creates_private_key_locked_with_the_password(self):
        self.make_key()
        key = self.keys / backup_secrets.KEY_NAME
        self.assertTrue(backup_secrets.configured(self.keys))
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.keys / backup_secrets.CERT_NAME).stat().st_mode & 0o777, 0o600)
        self.assertIn(b'BEGIN ENCRYPTED PRIVATE KEY', key.read_bytes())
        self.assertTrue(backup_secrets.unlocks('correct horse', key))
        self.assertFalse(backup_secrets.unlocks('wrong', key))
        # PBKDF2-SHA256 at 600,000 iterations, AES-256.
        params = subprocess.run(['openssl', 'asn1parse', '-in', str(key)], capture_output=True, text=True).stdout
        self.assertIn('hmacWithSHA256', params)
        self.assertIn('927C0', params)  # 600000 in hex
        self.assertIn('aes-256-cbc', params)

    def test_a_typo_never_becomes_the_backup_password(self):
        # Typed twice and compared: sudo is not asked at all, so a passwordless sudo
        # rule can't let any text through.
        answers = iter(['correct horse', 'correct hrose', 'short', 'battery staple', 'battery stapel'])
        with mock.patch.object(backup_secrets.subprocess, 'run', wraps=subprocess.run) as run, \
                self.assertRaisesRegex(RuntimeError, 'no backup password was confirmed'):
            backup_secrets.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys)
        self.assertFalse(backup_secrets.configured(self.keys))
        self.assertFalse(any('sudo' in call.args[0] for call in run.call_args_list))
        self.assertIn('The two passwords differ. Try again.', self.log)
        self.assertIn('Use at least 8 characters.', self.log)

    def test_setup_retries_then_accepts(self):
        answers = iter(['correct horse', 'typo', 'correct horse', 'correct horse'])
        backup_secrets.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys)
        self.assertTrue(backup_secrets.configured(self.keys))
        self.assertTrue(backup_secrets.unlocks('correct horse', self.keys / backup_secrets.KEY_NAME))

    def test_setup_keeps_an_existing_key(self):
        self.make_key()
        before = (self.keys / backup_secrets.KEY_NAME).read_bytes()
        backup_secrets.setup(ask=self.fail, out=self.log.append, key_dir=self.keys)
        self.assertEqual((self.keys / backup_secrets.KEY_NAME).read_bytes(), before)

    def test_restore_with_right_password_and_refusal_otherwise(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.assertNotIn(b'topsecret', (snap / backup_secrets.ARCHIVE_NAME).read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'Wrong password'):
            backup_secrets.restore(snap, self.root / 'out-wrong', 'nope')
        self.assertFalse((self.root / 'out-wrong').exists())
        out = backup_secrets.restore(snap, self.root / 'out', 'correct horse')
        self.assertEqual((out / 'secrets/accounts/APP-CREDENTIALS.txt').read_bytes(), SECRET)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)

    def test_a_damaged_archive_is_refused_before_anything_is_restored(self):
        # CMS decryption does not notice changed bytes; the snapshot's checksum must.
        self.make_key()
        snap = self.seal_snapshot()
        archive = snap / backup_secrets.ARCHIVE_NAME
        data = bytearray(archive.read_bytes())
        data[-20] ^= 0xFF  # inside the ciphertext
        archive.write_bytes(bytes(data))
        with self.assertRaisesRegex(RuntimeError, 'damaged'):
            backup_secrets.restore(snap, self.root / 'out', 'correct horse')
        self.assertFalse((self.root / 'out').exists())
        manifest = json.loads((snap / 'manifest.json').read_text())
        del manifest['sha256']
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, 'no checksum'):
            backup_secrets.restore(snap, self.root / 'out', 'correct horse')
        self.assertFalse((self.root / 'out').exists())

    def test_a_damaged_key_copy_is_never_called_a_wrong_password(self):
        self.make_key()
        snap = self.seal_snapshot()
        self.damage_key(snap)
        elsewhere = self.root / 'no-key-here'
        with self.assertRaisesRegex(RuntimeError, 'backup key copy in this snapshot is damaged'):
            backup_secrets.restore(snap, self.root / 'out', 'correct horse', key_dir=elsewhere)
        self.assertFalse((self.root / 'out').exists())
        # The server still has the same key: it opens the snapshot instead.
        said = []
        out = backup_secrets.restore(snap, self.root / 'out', 'correct horse', key_dir=self.keys, out=said.append)
        self.assertEqual((out / 'secrets/accounts/APP-CREDENTIALS.txt').read_bytes(), SECRET)
        self.assertIn('damaged', said[0])
        # A wrong password stays a wrong password.
        (snap / backup_secrets.KEY_NAME).write_bytes((self.keys / backup_secrets.KEY_NAME).read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'Wrong password'):
            backup_secrets.restore(snap, self.root / 'out2', 'nope', key_dir=self.keys)

    def test_a_damaged_key_copy_fails_the_snapshot_check(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('monthly_recovery', ROOT / 'monthly-recovery.py')
        monthly = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monthly)
        self.make_key()
        snap = self.seal_snapshot()
        for name in ('data', 'media', 'images'):
            (snap / f'{name}.tar').write_bytes(name.encode())
        manifest = json.loads((snap / 'manifest.json').read_text())
        manifest['sha256'].update({n: hashlib.sha256(n.encode()).hexdigest() for n in ('data', 'media', 'images')})
        (snap / 'manifest.json').write_text(json.dumps(manifest))
        monthly.verify(snap)
        self.damage_key(snap)
        with self.assertRaisesRegex(RuntimeError, 'secrets_key'):
            monthly.verify(snap)

    def test_restore_refuses_existing_destination(self):
        self.make_key()
        snap = self.seal_snapshot()
        (self.root / 'exists').mkdir()
        with self.assertRaises(RuntimeError):
            backup_secrets.restore(snap, self.root / 'exists', 'correct horse')

    def test_restore_refuses_unsafe_or_wrong_paths(self):
        self.make_key()
        snap = self.seal_snapshot()
        out = str(self.root / 'out')
        cases = [
            ('-snapshot', out),                       # would look like an option
            ('snapshot', out),                        # relative
            (str(self.root / 'snapshot/../snapshot'), out),
            (str(snap), '-out'),
            (str(snap), str(self.root / 'missing-parent/out')),
        ]
        for snapshot, dest in cases:
            with self.subTest(snapshot=snapshot, dest=dest), self.assertRaises(RuntimeError):
                backup_secrets.restore(snapshot, dest, 'correct horse')
        (self.root / 'link').symlink_to(snap)
        with self.assertRaises(RuntimeError):
            backup_secrets.restore(str(self.root / 'link'), out, 'correct horse')
        (snap / 'manifest.json').write_text('{"format": "something-else"}')
        with self.assertRaisesRegex(RuntimeError, 'Not a monthly snapshot'):
            backup_secrets.restore(snap, out, 'correct horse')
        self.assertFalse((self.root / 'out').exists())

    def test_password_never_on_command_line(self):
        seen = []
        real = subprocess.run

        def spy(args, *a, **k):
            seen.append(args)
            return real(args, *a, **k)
        with mock.patch.object(backup_secrets.subprocess, 'run', spy):
            self.make_key()
        self.assertTrue(seen)
        self.assertFalse(any('correct horse' in ' '.join(map(str, args)) for args in seen))


if __name__ == '__main__':
    unittest.main()
