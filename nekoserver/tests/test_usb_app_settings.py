"""1.0.0 (T1-R4): the weekly USB copy carries the newest nightly backup of the app settings,
sealed for the backup key, and the recovery menu restores it from there when the HDD is lost."""

import hashlib
import json
import os
import re
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

import backup_secrets  # noqa: E402
import recovery_wizard  # noqa: E402
import snapshot_restore as sr  # noqa: E402
from roomy import roomy_drive  # noqa: E402
from test_snapshot_restore import make  # noqa: E402

PASSWORD = 'correct horse'
NIGHT = 'snapshot-1792900000000000-1'
FILES = {
    'appdata/jellyfin/system.xml': b'<ApiKey>jellyfin-api-key-123</ApiKey>',
    'appdata/sonarr/config.xml': b'<Config><ApiKey>sonarr-key</ApiKey></Config>',
    'compose/docker-compose.yml': b'services: {}\n',
}


def answers(*values):
    given = iter(values)
    return lambda question='': next(given)


def seal(snapshot, copy, keys, scratch):
    """The weekly copy of `snapshot` in `copy`, made as the runtime makes it
    (usb_app_settings.rs): tar | openssl enc with a random password, that password sealed
    for the backup certificate, the key pair beside it, each file's checksum."""
    copy.mkdir(parents=True)
    for name in (backup_secrets.KEY_NAME, backup_secrets.CERT_NAME):
        shutil.copy(keys / name, copy / name)
    secret = os.urandom(32).hex()
    pass_file = scratch / 'archive.pass'
    pass_file.write_text(secret)
    archive = subprocess.run(
        ['tar', '--create', '--file', '-', '--numeric-owner', '-C', str(snapshot), '--', 'manifest.json', 'payload'],
        capture_output=True,
        check=True,
    ).stdout
    subprocess.run(
        [
            'openssl',
            'enc',
            *backup_secrets.APP_ENC_ARGS,
            '-pass',
            f'file:{pass_file}',
            '-out',
            str(copy / backup_secrets.APP_ARCHIVE),
        ],
        input=archive,
        check=True,
    )
    subprocess.run(
        [
            'openssl',
            'cms',
            '-encrypt',
            '-binary',
            '-aes256',
            '-outform',
            'DER',
            '-out',
            str(copy / backup_secrets.APP_ARCHIVE_KEY),
            str(keys / backup_secrets.CERT_NAME),
        ],
        input=secret.encode(),
        check=True,
    )
    pass_file.unlink()
    sums = {
        key: hashlib.sha256((copy / name).read_bytes()).hexdigest()
        for key, name in (
            ('archive', backup_secrets.APP_ARCHIVE),
            ('archive_key', backup_secrets.APP_ARCHIVE_KEY),
            ('secrets_key', backup_secrets.KEY_NAME),
            ('secrets_cert', backup_secrets.CERT_NAME),
        )
    }
    (copy / 'manifest.json').write_text(
        json.dumps({'format': backup_secrets.USB_APP_SETTINGS_FORMAT, 'snapshot': copy.name, 'sha256': sums})
    )
    return copy


class SameAsTheRuntimeTests(unittest.TestCase):
    def test_the_menu_reads_the_copy_the_weekly_job_writes(self):
        """The copy is written by the Rust runtime: a name or an encryption setting that
        differs here would leave it unopenable at the one moment it is needed."""
        source = (ROOT / 'runtime/src/usb_app_settings.rs').read_text()
        self.assertIn(f'const FOLDER: &str = "{backup_secrets.USB_APP_SETTINGS}";', source)
        self.assertIn(f'const FORMAT: &str = "{backup_secrets.USB_APP_SETTINGS_FORMAT}";', source)
        self.assertIn(f'const ARCHIVE: &str = "{backup_secrets.APP_ARCHIVE}";', source)
        self.assertIn(f'const ARCHIVE_KEY: &str = "{backup_secrets.APP_ARCHIVE_KEY}";', source)
        rust = re.search(r'ENC_ARGS: \[&str; \d+\] = \[([^\]]*)\]', source)[1]
        self.assertEqual(re.findall(r'"([^"]+)"', rust), backup_secrets.APP_ENC_ARGS)
        self.assertEqual(recovery_wizard.USB_APP_SETTINGS, backup_secrets.USB_APP_SETTINGS)
        self.assertIn('apps', recovery_wizard.ALSO_ON_USB)


@unittest.skipUnless(shutil.which('openssl'), 'the openssl command is not installed')
class UsbAppSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys_tmp = tempfile.TemporaryDirectory()
        cls.keys = Path(cls.keys_tmp.name) / 'keys'
        backup_secrets.create_key(PASSWORD, cls.keys)

    @classmethod
    def tearDownClass(cls):
        cls.keys_tmp.cleanup()

    def setUp(self):
        roomy_drive(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media, self.usb = self.root / 'hdd', self.root / 'usb'
        self.night = self.root / 'night' / NIGHT
        make(self.night, sr.APPS, FILES)
        self.copy = seal(self.night, self.usb / 'app-settings' / NIGHT, self.keys, self.root)
        self.config = {
            'data_mount': str(self.root / 'ssd'),
            'media_mount': str(self.media),
            'photos': {'external_backup_uuid': 'usb-1'},
        }
        self.out = []

    def test_the_usb_copy_opens_with_the_backup_password_to_the_very_night(self):
        into = self.root / 'opened'
        backup_secrets.open_app_settings(self.copy, into, PASSWORD, key_dir=self.root / 'no-key-here')
        for rel, body in FILES.items():
            self.assertEqual((into / 'payload' / rel).read_bytes(), body)
        self.assertEqual((into / 'manifest.json').read_bytes(), (self.night / 'manifest.json').read_bytes())
        sr.verify(sr.APPS, into)  # app-recovery.py's own check passes on it
        self.assertEqual(into.stat().st_mode & 0o777, 0o700)
        # Nothing on the drive is readable without the password.
        for path in self.copy.iterdir():
            self.assertNotIn(b'jellyfin-api-key-123', path.read_bytes())

    def test_a_wrong_password_or_a_damaged_copy_restores_nothing(self):
        into = self.root / 'opened'
        with self.assertRaises(RuntimeError) as wrong:
            backup_secrets.open_app_settings(self.copy, into, 'wrong', key_dir=self.root / 'no-key-here')
        self.assertEqual(str(wrong.exception), backup_secrets.WRONG_PASSWORD)
        self.assertFalse(into.exists())
        archive = self.copy / backup_secrets.APP_ARCHIVE
        data = bytearray(archive.read_bytes())
        data[-1] ^= 1
        archive.write_bytes(bytes(data))
        with self.assertRaises(RuntimeError) as damaged:
            backup_secrets.open_app_settings(self.copy, into, PASSWORD, key_dir=self.root / 'no-key-here')
        self.assertIn('The copy of the app settings on the USB drive is damaged', str(damaged.exception))
        self.assertFalse(into.exists())

    def restore(self, *typed, password=PASSWORD, usb=True):
        """The menu's "App settings, from a nightly backup", the first night chosen; the
        tool's restore done in this process (without owners: the tests need no root)."""
        calls, seen = [], []

        def tool(script, *args):
            snapshot, destination = Path(args[2]), Path(args[4])
            calls.append((script, *args))
            seen.append(sorted(p.name for p in snapshot.iterdir()))
            sr.restore(sr.APPS, snapshot, destination)
            return True

        target = self.root / 'restored'
        with (
            mock.patch.object(recovery_wizard.Usb, '__enter__', return_value=self.usb if usb else None),
            mock.patch.object(recovery_wizard.Usb, '__exit__', return_value=None),
            mock.patch.object(recovery_wizard, 'restore_target', return_value=target) as where,
            mock.patch.object(recovery_wizard, 'tool', side_effect=tool),
            mock.patch.object(backup_secrets.getpass, 'getpass', return_value=password) as asked,
        ):
            recovery_wizard.restore_nightly(self.config, 'apps', answers(*typed), self.out.append)
        return target, calls, seen, asked, where

    def test_with_the_hdd_lost_the_usb_copy_is_listed_and_restored(self):
        target, calls, seen, asked, where = self.restore('1', 'yes')
        said = '\n'.join(self.out)
        self.assertIn(f'1. {recovery_wizard.when("1792900000000000")}{recovery_wizard.FROM_USB}', said)
        self.assertIn('  Password:     it asks for the backup password chosen during setup', said)
        self.assertEqual(where.call_args.args[-1], self.usb)  # never restored onto the USB drive itself
        asked.assert_called_once()
        self.assertEqual(seen, [['manifest.json', 'payload']])
        self.assertIn('--preserve-owners', calls[0])
        for rel, body in FILES.items():
            self.assertEqual((target / rel).read_bytes(), body)
        # The opened copy is gone again: only the restore is left.
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if 'opening' in p.name), [])
        self.assertIn('Done: restored into', said)

    def test_a_restore_from_the_hdd_is_the_same_as_before(self):
        """Differential: with the HDD there, its night is restored exactly as before the
        USB copy existed (same tool call, no password, same files), with or without the
        USB drive's copy of the same night beside it in the list."""
        hdd_night = self.media / 'backups/media-applications' / NIGHT
        hdd_night.parent.mkdir(parents=True)
        shutil.copytree(self.night, hdd_night)
        results = []
        for usb in (False, True):
            self.out.clear()
            target, calls, seen, asked, _ = self.restore('1', 'yes', usb=usb)
            restored = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()}
            restored.pop('restore-report.json')
            asked.assert_not_called()
            results.append((calls, restored, [line for line in self.out if 'USB drive' not in line]))
            shutil.rmtree(target)
        self.assertEqual(results[0], results[1])
        calls, restored, _ = results[0]
        self.assertEqual(calls[0][1:4], ('restore', '--snapshot', str(hdd_night)))
        self.assertEqual(restored, FILES)

    def test_nothing_on_either_drive_is_said(self):
        shutil.rmtree(self.usb)
        self.restore(usb=True)
        self.assertEqual(
            self.out[-1],
            'No nightly backup of app settings found on the HDD yet, nor on the USB drive (or it is not plugged in).',
        )


if __name__ == '__main__':
    unittest.main()
