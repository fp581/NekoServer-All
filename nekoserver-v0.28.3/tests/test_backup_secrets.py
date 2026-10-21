"""The app password list in backups opens only with the sudo password."""
import hashlib
import json
import os
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
        backup_secrets.setup(ask=lambda prompt: password, out=self.log.append, key_dir=self.keys,
                             user='neko', check=lambda user, pw: pw == 'correct horse')

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
        digest = hashlib.sha256((snap / backup_secrets.ARCHIVE_NAME).read_bytes()).hexdigest()
        (snap / 'manifest.json').write_text(json.dumps({'format': 'neko-maintenance-v1', 'sha256': {'secrets': digest}}))
        return snap

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

    def test_setup_refuses_anything_but_the_sudo_password(self):
        answers = iter(['typo1', 'typo2', 'typo3'])
        with self.assertRaises(RuntimeError):
            backup_secrets.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys,
                                 user='neko', check=lambda user, pw: pw == 'correct horse')
        self.assertFalse(backup_secrets.configured(self.keys))

    def test_setup_retries_then_accepts(self):
        answers = iter(['typo', 'correct horse'])
        backup_secrets.setup(ask=lambda prompt: next(answers), out=self.log.append, key_dir=self.keys,
                             user='neko', check=lambda user, pw: pw == 'correct horse')
        self.assertTrue(backup_secrets.configured(self.keys))

    def test_setup_keeps_an_existing_key(self):
        self.make_key()
        before = (self.keys / backup_secrets.KEY_NAME).read_bytes()
        backup_secrets.setup(ask=self.fail, out=self.log.append, key_dir=self.keys, user='neko')
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

    def test_sudo_user_required(self):
        for env in ({}, {'SUDO_USER': 'root'}):
            with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(RuntimeError):
                backup_secrets.sudo_user()


if __name__ == '__main__':
    unittest.main()
