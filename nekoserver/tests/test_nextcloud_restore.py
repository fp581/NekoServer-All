"""Blocker T1-R5 of the 1.0 round, the user's design "Setup brings it back": only the SSD
broke and was replaced. Setup's Nextcloud step refused ("already holds files"), the
gateway step failed after it, and a first-time user had no way on. Now setup offers to
bring this server's own Nextcloud back from its backup on the HDD, and only that."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nextcloud_restore  # noqa: E402
from modules import nextcloud  # noqa: E402

OWN = 'ocabc1234567'
OTHER = 'ocxyz7654321'
SNAPSHOT_AT = 1_790_000_000.0  # the monthly snapshot's time
DUMP_AT = SNAPSHOT_AT + 7 * 86400  # a week later: the weekly database copy


def config_php(instance):
    return (
        f"<?php\n$CONFIG = array (\n  'instanceid' => '{instance}',\n  'dbuser' => 'oc_admin',\n"
        "  'dbpassword' => 'secret',\n  'installed' => true,\n);\n"
    )


def tar_of(files, odd=()):
    """A tar of `files` (name: content), with their folders, and the `odd` members
    (TarInfo, no content: links, devices, names leading out) at the end."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        folders = sorted({str(Path(*Path(n).parts[:i])) for n in files for i in range(1, len(Path(n).parts))})
        for folder in folders:
            info = tarfile.TarInfo(folder)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, text in files.items():
            data = text.encode() if isinstance(text, str) else text
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in odd:
            tar.addfile(info, io.BytesIO(b'x' * info.size) if info.isfile() else None)
    return out.getvalue()


def odd_member(name, kind=tarfile.REGTYPE, link=''):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = 1 if kind == tarfile.REGTYPE else 0
    return info


@contextlib.contextmanager
def python_without_the_filter():
    """Python 3.11.0 to 3.11.3 (Debian 12 has 3.11.2): no tarfile.data_filter, and
    extractall takes no filter keyword."""
    real = tarfile.TarFile.extractall

    def extractall(self, path='.', members=None, *, numeric_owner=False, **given):
        if given:
            raise TypeError(f'extractall() got an unexpected keyword argument {next(iter(given))!r}')
        return real(self, path, members, numeric_owner=numeric_owner, filter='fully_trusted')

    with mock.patch.dict(tarfile.__dict__), mock.patch.object(tarfile.TarFile, 'extractall', extractall):
        del tarfile.data_filter
        yield


class Server:
    """A server whose SSD was replaced: its HDD keeps the files folder and the backups."""

    def __init__(self, tmp, instance=OWN, sealed=False, dump=True, named=OWN, odd=()):
        self.data, self.media = Path(tmp) / 'ssd', Path(tmp) / 'hdd'
        self.data.mkdir()
        self.config = {'data_mount': str(self.data), 'media_mount': str(self.media), 'nextcloud': {'enabled': True}}
        self.files = self.media / 'cloud/files'
        for path, text in (
            (f'appdata_{OWN}/preview/1.png', 'p'),
            ('alice/files/holiday.jpg', 'photo'),
            ('.ncdata', '# Nextcloud data directory\n'),
        ):
            (self.files / path).parent.mkdir(parents=True, exist_ok=True)
            (self.files / path).write_text(text)
        self.instance, self.sealed = instance, sealed
        managed = {
            'format': 'neko-nextcloud-v1',
            'image': 'nextcloud:32-apache',
            'model': {},
            'settings': nextcloud.settings(
                {**self.config, 'nextcloud': {'admin_user': 'alice'}}, 'nextcloud:32-apache'
            ),
        }
        members = {
            'appdata/nextcloud/html/index.php': '<?php\n',
            'appdata/nextcloud/html/version.php': "<?php\n$OC_VersionString = '32.0.3';\n",
            'appdata/nextcloud/postgres/PG_VERSION': '17\n',
            'appdata/nextcloud/postgres/base/16384/1259': b'\x00\x01path appdata_' + named.encode() + b'/preview\x00',
            'compose/nextcloud/compose.yaml': 'services: {}\n',
            'compose/nextcloud/managed.json': json.dumps(managed),
        }
        if not sealed:
            members['compose/nextcloud/.env'] = 'DB_PASSWORD=x\nADMIN_PASSWORD=y\n'
            members['appdata/nextcloud/html/config/config.php'] = config_php(instance)
        self.snapshot = self.media / f'backups/monthly/{int(SNAPSHOT_AT * 1e6)}-7'
        self.snapshot.mkdir(parents=True)
        data_tar = tar_of(members, odd)
        (self.snapshot / 'data.tar').write_bytes(data_tar)
        if sealed:
            (self.snapshot / 'secrets.tar.cms').write_bytes(b'sealed')
        (self.snapshot / 'manifest.json').write_text(
            json.dumps(
                {
                    'format': 'neko-maintenance-v1',
                    'kind': 'monthly',
                    'apps': ['nextcloud'],
                    'created_at': SNAPSHOT_AT,
                    'paths': {'data': ['appdata/nextcloud', 'compose/nextcloud'], 'media': []},
                    'sha256': {'data': hashlib.sha256(data_tar).hexdigest()},
                    'password_files': 'encrypted' if sealed else 'unencrypted_in_data_tar',
                }
            )
        )
        if dump:
            folder = self.media / 'backups/nextcloud-database'
            folder.mkdir(parents=True)
            self.dump = folder / 'nextcloud-20260901T030000Z.sql'
            self.dump.write_text(f"COPY public.oc_filecache FROM stdin;\n1\tappdata_{named}/preview\n\\.\n")
            (folder / f'{self.dump.name}.json').write_text(
                json.dumps(
                    {
                        'format': 'neko-nextcloud-dump-v1',
                        'nextcloud_version': '32.0.5',
                        'created_at': DUMP_AT,
                        'sha256': hashlib.sha256(self.dump.read_bytes()).hexdigest(),
                    }
                )
            )
        self.said, self.asked, self.calls, self.adopted = [], [], [], []
        self.ctx = SimpleNamespace(config=self.config, log=self.said.append, apply=True)

    def tree(self, root):
        return sorted(
            (str(p.relative_to(root)), p.read_bytes() if p.is_file() else None) for p in Path(root).rglob('*')
        )

    def app(self, status=None):
        return SimpleNamespace(
            compose_base=lambda manifest, env: ['compose', str(manifest)],
            status=status or (lambda base: {'installed': True, 'maintenance': False, 'needsDbUpgrade': False}),
            settings_problem=lambda owned: ('', owned['settings']['admin_user']),
            adopt=self.adopted.append,
        )

    def run(self, fail=None):
        def run(argv, **kw):
            stdin = kw.get('stdin')
            self.calls.append((argv, stdin.read() if stdin else b''))
            code = 1 if fail and fail in ' '.join(argv) else 0
            return subprocess.CompletedProcess(argv, code, b'' if 'stdin' in kw else '', b'ERROR: bad' if code else '')

        return run

    def offer(self, answer=True, interactive=True, fail=None, status=None, up=None):
        def ask(question):
            self.asked.append(question)
            return answer

        with (
            mock.patch.object(nextcloud_restore.os, 'chown') as self.chown,
            mock.patch.object(
                nextcloud_restore, 'checked_run', side_effect=lambda argv, *a: self.calls.append((argv, b''))
            ),
            mock.patch.object(
                nextcloud_restore,
                'compose_up',
                side_effect=up or (lambda base, *a: self.calls.append((base + ['up'], b''))),
            ),
            mock.patch.object(nextcloud_restore.time, 'sleep'),
            mock.patch('backup_secrets.asked_restore', side_effect=self.unseal),
        ):
            return nextcloud_restore.offered(self.ctx, self.app(status), ask, interactive, self.run(fail))

    def unseal(self, snapshot, into, out=print):
        """The sealed password files opened with the backup password (as the menu asks it)."""
        self.asked.append('backup password')
        for part, text in (
            ('compose/nextcloud/.env', 'DB_PASSWORD=x\nADMIN_PASSWORD=y\n'),
            ('appdata/nextcloud/html/config/config.php', config_php(self.instance)),
            ('secrets/accounts/APP-CREDENTIALS.txt', 'kept out'),
        ):
            (Path(into) / part).parent.mkdir(parents=True, exist_ok=True)
            (Path(into) / part).write_text(text)
        return into

    def commands(self):
        return [' '.join(argv) for argv, _ in self.calls]

    def add_nightly(self, at, named=OWN, sealed=False, sql_sum=None, version=True, instance=None):
        """A nightly backup (row C-01, T2's layout): the program folder and settings in
        data.tar (no database folder, no html/data), the database as nextcloud.sql."""
        run = self.media / f'backups/nextcloud-nightly/snapshot-{int(at * 1e6)}-4242'
        run.mkdir(parents=True)
        members = {
            'appdata/nextcloud/html/index.php': '<?php // nightly\n',
            'compose/nextcloud/compose.yaml': 'services: {}\n',
            'compose/nextcloud/managed.json': json.dumps(
                {
                    'format': 'neko-nextcloud-v1',
                    'image': 'nextcloud:32-apache',
                    'model': {},
                    'settings': nextcloud.settings(
                        {**self.config, 'nextcloud': {'admin_user': 'alice'}}, 'nextcloud:32-apache'
                    ),
                }
            ),
        }
        if version:
            members['appdata/nextcloud/html/version.php'] = "<?php\n$OC_VersionString = '32.0.6';\n"
        if not sealed:
            members['compose/nextcloud/.env'] = 'DB_PASSWORD=x\nADMIN_PASSWORD=y\n'
            members['appdata/nextcloud/html/config/config.php'] = config_php(instance or self.instance)
        data_tar = tar_of(members)
        (run / 'data.tar').write_bytes(data_tar)
        sql = f"COPY public.oc_filecache FROM stdin;\n1\tappdata_{named}/preview\n2\tnightly\n\\.\n".encode()
        (run / 'nextcloud.sql').write_bytes(sql)
        if sealed:
            (run / 'secrets.tar.cms').write_bytes(b'sealed')
        (run / 'manifest.json').write_text(
            json.dumps(
                {
                    'format': 'neko-maintenance-v1',
                    'kind': 'nextcloud-nightly',
                    'apps': ['nextcloud'],
                    'created_at': at,
                    'nextcloud_version': '32.0.6',
                    'paused_seconds': 12,
                    'paths': {'data': ['appdata/nextcloud/html', 'compose/nextcloud'], 'media': []},
                    'sha256': {
                        'data': hashlib.sha256(data_tar).hexdigest(),
                        'database': sql_sum or hashlib.sha256(sql).hexdigest(),
                    },
                    'password_files': 'encrypted' if sealed else 'unencrypted_in_data_tar',
                }
            )
        )
        self.nightly_sql = sql
        return run


class OfferedOnlyThenTests(unittest.TestCase):
    def test_the_offer_comes_only_when_the_ssd_is_new_and_the_files_and_this_servers_backup_are_on_the_hdd(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            restored, said = server.offer(answer=False)
            self.assertFalse(restored)
            self.assertEqual(len(server.asked), 1)
            self.assertIn('Bring Nextcloud back from the backup of', server.asked[0])
            told = '\n'.join(server.said)
            self.assertIn(f'{server.files} holds this server\'s Nextcloud files', told)
            self.assertIn('the newer copy of', told)  # the weekly database copy is newer than the snapshot
        cases = {
            'files folder empty (a new HDD: given back elsewhere)': lambda s: (
                shutil.rmtree(s.files),
                s.files.mkdir(),
            ),
            'no Nextcloud folder of its own in the files folder': lambda s: os.rename(
                s.files / f'appdata_{OWN}', s.files / 'old-stuff'
            ),
            'the SSD holds Nextcloud data already': lambda s: (
                (s.data / 'appdata/nextcloud/postgres').mkdir(parents=True),
                (s.data / 'appdata/nextcloud/postgres/PG_VERSION').write_text('17'),
            ),
        }
        for case, change in cases.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp)
                change(server)
                self.assertEqual(server.offer(), (False, None))
                self.assertEqual(server.asked, [])
                self.assertFalse((server.data / 'compose/nextcloud/compose.yaml').exists())


class YesRestoresTests(unittest.TestCase):
    def test_yes_brings_settings_program_and_the_newer_database_back_and_the_files_folder_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            files_before = server.tree(server.files)
            self.assertEqual(server.offer(), (True, None))
            data = server.data
            self.assertEqual((data / 'appdata/nextcloud/html/index.php').read_text(), '<?php\n')
            php = data / 'appdata/nextcloud/html/config/config.php'
            self.assertIn(OWN, php.read_text())
            # Nextcloud writes its own config.php: www-data's, not root's ("Cannot write into config directory").
            # Given to www-data while still staged, so it is never in place with the wrong owner.
            staged = data / nextcloud_restore.STAGING / 'appdata/nextcloud/html/config/config.php'
            self.assertIn(mock.call(staged, 33, 33, follow_symlinks=False), server.chown.call_args_list)
            self.assertEqual(php.stat().st_mode & 0o777, 0o640)
            self.assertEqual((data / 'compose/nextcloud/.env').stat().st_mode & 0o777, 0o600)
            self.assertTrue((data / 'compose/nextcloud/compose.yaml').is_file())
            said = server.commands()
            # The database first, the newer copy loaded as Nextcloud's own database user, then Nextcloud.
            database = next(i for i, c in enumerate(said) if c.endswith('up -d --wait database'))
            load = next(i for i, c in enumerate(said) if '-U oc_admin -d nextcloud_restoring' in c)
            up = said.index(next(c for c in said if c.endswith(' up')))
            scan = next(i for i, c in enumerate(said) if c.endswith('files:scan --all'))
            self.assertLess(database, load)
            self.assertLess(load, up)
            self.assertLess(up, scan)
            self.assertEqual(server.calls[load][1], server.dump.read_bytes())
            self.assertTrue(any('RENAME TO nextcloud' in c for c in said))
            self.assertEqual(server.tree(server.files), files_before)
            self.assertFalse((data / nextcloud_restore.STAGING).exists())
            self.assertEqual(server.adopted, ['alice'])
            self.assertIn('Sign in with the Nextcloud login you had before', '\n'.join(server.said))

    def test_a_snapshot_with_a_backup_key_asks_the_backup_password_and_takes_only_nextclouds_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp, sealed=True, dump=False)
            self.assertEqual(server.offer(), (True, None))
            self.assertEqual(server.asked[1], 'backup password')
            self.assertIn('It asks for the backup password', '\n'.join(server.said))
            self.assertIn(OWN, (server.data / 'appdata/nextcloud/html/config/config.php').read_text())
            self.assertFalse((server.data / 'secrets').exists())  # the password list is not put on the SSD
            # No newer database copy: the snapshot's own database stays, nothing is loaded.
            self.assertFalse(any('nextcloud_restoring' in c for c in server.commands()))


class NoKeepsTheRefusalTests(unittest.TestCase):
    def test_no_changes_nothing_and_the_refusal_names_what_was_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            restored, said = server.offer(answer=False)
            self.assertFalse(restored)
            self.assertIn('you chose not to bring it back', said)
            self.assertEqual(list(server.data.iterdir()), [])
            with mock.patch.object(nextcloud, 'resolve_image', side_effect=AssertionError('not reached')):
                with self.assertRaises(RuntimeError) as refused:
                    nextcloud.first_install(
                        server.ctx,
                        {},
                        None,
                        'auto',
                        (server.data / 'compose/nextcloud', None, None, None),
                        said,
                    )
            self.assertIn(f'{server.files} already holds files', str(refused.exception))
            self.assertIn('you chose not to bring it back', str(refused.exception))
            # It no longer points to a section a first-time user cannot follow.
            self.assertNotIn('restore it from its backup (RECOVERY.md)', str(refused.exception))
            self.assertIn('The SSD or the HDD broke', str(refused.exception))


class YoungServerTests(unittest.TestCase):
    # Coordinator, T1-R5 follow-up: a server before its first night has no backup of Nextcloud
    # at all (row C-01 added the nightly one), and the refusal pointed to an offer
    # that could never come.
    def test_with_no_backup_of_its_settings_yet_the_refusal_says_so_and_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            shutil.rmtree(server.media / 'backups/monthly')  # only the weekly database copy is there
            restored, said = server.offer()
            self.assertFalse(restored)
            self.assertEqual(server.asked, [])
            self.assertEqual(list(server.data.iterdir()), [])
            with mock.patch.object(nextcloud, 'resolve_image', side_effect=AssertionError('not reached')):
                with self.assertRaises(RuntimeError) as refused:
                    nextcloud.first_install(
                        server.ctx, {}, None, 'auto', (server.data / 'compose/nextcloud', None, None, None), said
                    )
            told = str(refused.exception)
            self.assertIn('No backup of Nextcloud\'s settings exists yet', told)
            self.assertIn('the nightly backup saves them first in the first night after setup', told)
            self.assertIn(f'Your files stay as they are in {server.files}', told)
            self.assertIn('copy your files into Nextcloud', told)
            self.assertNotIn('No monthly snapshot', told)


class NightlyBackupTests(unittest.TestCase):
    # Row C-01 (the user's decision): Nextcloud is backed up nightly in 1.0.0, so a server
    # younger than a month is brought back too; the newest of the nightly backup, the
    # weekly database copy and the monthly snapshot is used, through the same checked code.
    def test_a_nightly_backup_alone_is_offered_and_brings_nextcloud_back(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp, dump=False)
                shutil.rmtree(server.media / 'backups/monthly')
                night = SNAPSHOT_AT + 3 * 86400
                server.add_nightly(night, sealed=sealed)
                files_before = server.tree(server.files)
                self.assertEqual(server.offer(), (True, None))
                self.assertIn(f'the backup of {nextcloud_restore.shown(night)}?', server.asked[0])
                told = '\n'.join(server.said)
                self.assertIn(f'the nightly backup of {nextcloud_restore.shown(night)}', told)
                self.assertIn('database (accounts, shares, calendars, contacts): from the same backup', told)
                data = server.data
                # The program folder came back whole: never a bare config.php without version.php.
                self.assertIn('32.0.6', (data / 'appdata/nextcloud/html/version.php').read_text())
                self.assertIn(OWN, (data / 'appdata/nextcloud/html/config/config.php').read_text())
                self.assertEqual(list((data / 'appdata/nextcloud/postgres').iterdir()), [])
                self.assertTrue((data / 'appdata/nextcloud/html/data').is_dir())
                said = server.commands()
                # Its own database user is made (password on stdin only), then its database loaded.
                role = next(
                    i for i, c in enumerate(said) if c.endswith('psql -q -U nextcloud -d postgres -v ON_ERROR_STOP=1')
                )
                self.assertIn(b'CREATE ROLE "oc_admin" LOGIN PASSWORD \'secret\'', server.calls[role][1])
                self.assertFalse(any('secret' in c for c in said))
                load = next(i for i, c in enumerate(said) if '-U oc_admin -d nextcloud_restoring' in c)
                self.assertLess(role, load)
                self.assertEqual(server.calls[load][1], server.nightly_sql)
                self.assertEqual(server.tree(server.files), files_before)
                self.assertFalse((data / nextcloud_restore.STAGING).exists())

    def test_the_database_user_is_made_with_its_password_quoted_for_psql(self):
        # T5: a password with ' and \ in config.php (PHP writes them as \' and \\) reaches psql as one literal.
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            php = rb"""<?php $CONFIG = array ('dbuser' => 'oc_alice', 'dbpassword' => 'a\'b\\c$neko:y',);"""
            nextcloud_restore.role_sql(stage, 'oc_alice', php)
            self.assertEqual(
                (stage / 'role.sql').read_text(),
                "DO $neko$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oc_alice') THEN "
                'CREATE ROLE "oc_alice" LOGIN PASSWORD ' + r"'a''b\c$neko:y'" + '; END IF; END$neko$;\n',
            )
            self.assertEqual((stage / 'role.sql').stat().st_mode & 0o777, 0o600)
            weird = php.replace(b':y', b'$:y').replace(b'$neko:', b'$neko$')
            elsewhere = stage / 'x'
            with self.assertRaisesRegex(RuntimeError, 'cannot be given to psql'):
                nextcloud_restore.role_sql(elsewhere, 'oc_alice', weird)

    def test_the_newest_of_nightly_backup_weekly_copy_and_monthly_snapshot_is_used(self):
        cases = {
            # (nightly time, what is loaded, what the date is)
            'the nightly backup is the newest': (DUMP_AT + 86400, 'nightly', DUMP_AT + 86400),
            'a weekly copy newer than the nightly backup': (DUMP_AT - 86400, 'weekly', DUMP_AT),
        }
        for case, (night, loaded, date) in cases.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp)  # monthly snapshot and a weekly copy a week later
                server.add_nightly(night)
                self.assertEqual(server.offer(), (True, None))
                self.assertIn(f'the backup of {nextcloud_restore.shown(date)}?', server.asked[0])
                # Settings and program from the nightly backup, newer than the monthly snapshot.
                self.assertIn('nightly', (server.data / 'appdata/nextcloud/html/index.php').read_text())
                said = server.commands()
                load = next(i for i, c in enumerate(said) if '-U oc_admin -d nextcloud_restoring' in c)
                expected = server.nightly_sql if loaded == 'nightly' else server.dump.read_bytes()
                self.assertEqual(server.calls[load][1], expected)

    def test_a_damaged_or_foreign_nightly_backup_is_passed_over_for_the_monthly_snapshot(self):
        for case, kw in {
            'damaged': {'sql_sum': '0' * 64},
            'another server\'s': {'named': OTHER, 'instance': OTHER},
        }.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp)
                server.add_nightly(DUMP_AT + 86400, **kw)
                self.assertEqual(server.offer(), (True, None))
                self.assertEqual((server.data / 'appdata/nextcloud/html/index.php').read_text(), '<?php\n')
                self.assertIn(f'the backup of {nextcloud_restore.shown(DUMP_AT)}?', server.asked[0])

    def test_a_nightly_backup_without_its_program_folder_puts_the_ssd_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp, dump=False)
            shutil.rmtree(server.media / 'backups/monthly')
            server.add_nightly(SNAPSHOT_AT, version=False)
            restored, said = server.offer()
            self.assertFalse(restored)
            self.assertIn('the backup holds no appdata/nextcloud/html/version.php', said)
            self.assertIn('the SSD was put back as it was', said)
            self.assertEqual(list(server.data.iterdir()), [])


class PatternTests(unittest.TestCase):
    # SonarCloud (python:S6353) asked for \d and \w: they keep meaning ASCII digits and
    # letters only, as [0-9] and [A-Za-z0-9_] did (re.ASCII on the str patterns).
    def test_folder_and_copy_names_take_ascii_digits_only(self):
        self.assertTrue(nextcloud_restore.MONTHLY.fullmatch('1790000000000000-7'))
        self.assertIsNone(nextcloud_restore.MONTHLY.fullmatch(chr(0x661) * 2 + '-7'))  # Arabic-Indic digits
        self.assertTrue(nextcloud_restore.NIGHTLY_RUN.fullmatch('snapshot-1790000000000000-4242'))
        self.assertIsNone(nextcloud_restore.NIGHTLY_RUN.fullmatch('snapshot-' + chr(0x661) + '-4242'))
        self.assertIsNone(nextcloud_restore.NIGHTLY_RUN.fullmatch('.snapshot-1790000000000000-4242.partial'))
        self.assertTrue(nextcloud_restore.DUMP.fullmatch('nextcloud-20260901T030000Z.sql'))
        self.assertIsNone(nextcloud_restore.DUMP.fullmatch('nextcloud-2026090' + chr(0x661) + 'T030000Z.sql'))
        self.assertIsNone(nextcloud_restore.DUMP.fullmatch('nextcloud-20260901T030000Zxsql'))
        self.assertEqual(nextcloud_restore.DB_USER.search(b"'dbuser' => 'oc_Alice9'")[1], b'oc_Alice9')
        self.assertIsNone(nextcloud_restore.DB_USER.search(("'dbuser' => 'oc_" + chr(0xE9) + "'").encode()))
        self.assertEqual(nextcloud_restore.VERSION.search(b"$OC_VersionString = '32.0.3';")[1], b'32')


class ForeignBackupTests(unittest.TestCase):
    def test_another_servers_backup_is_never_offered(self):
        for sealed in (False, True):
            with self.subTest(sealed=sealed), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp, instance=OTHER, sealed=sealed, named=OTHER)
                restored, said = server.offer()
                self.assertFalse(restored)
                self.assertEqual(server.asked, [])
                self.assertIn('is not this server\'s', said)
                self.assertIn(f'appdata_{OWN}', said)
                self.assertEqual(list(server.data.iterdir()), [])

    def test_a_sealed_backup_whose_config_names_another_server_is_refused_after_the_password(self):
        # Its database named this server's folder, its config.php another id: nothing is switched in.
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp, instance=OTHER, sealed=True, dump=False)
            restored, said = server.offer()
            self.assertFalse(restored)
            self.assertIn('not this server\'s (another instance id)', said)
            self.assertEqual(list(server.data.iterdir()), [])


class UnattendedNeverRestoresTests(unittest.TestCase):
    def test_a_run_nobody_watches_never_restores(self):
        for interactive in (False, None):  # None: no terminal (a dashboard upgrade, a timer)
            with self.subTest(interactive=interactive), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp)
                with mock.patch.object(sys.stdin, 'isatty', return_value=False):
                    restored, said = server.offer(interactive=interactive)
                self.assertFalse(restored)
                self.assertEqual(server.asked, [])
                self.assertIn('never unattended', said)
                self.assertEqual(list(server.data.iterdir()), [])
                self.assertEqual(server.calls, [])


class OlderPythonTests(unittest.TestCase):
    """T5's review: tarfile's filter came with Python 3.11.4; on Debian 12 (3.11.2) the
    restore stopped with a TypeError and nothing came back."""

    def test_the_restore_works_on_a_python_without_tarfiles_filter(self):
        with tempfile.TemporaryDirectory() as tmp, python_without_the_filter():
            server = Server(tmp)
            self.assertEqual(server.offer(), (True, None))
            self.assertEqual((server.data / 'appdata/nextcloud/html/index.php').read_text(), '<?php\n')

    def test_a_member_leading_out_of_the_folder_is_never_unpacked_with_or_without_the_filter(self):
        for case, member in (
            ('a name with ..', odd_member('appdata/nextcloud/html/../../../../../evil')),
            ('a link to an absolute path', odd_member('appdata/nextcloud/html/etc', tarfile.SYMTYPE, '/etc')),
            ('a link leading out', odd_member('appdata/nextcloud/html/up', tarfile.SYMTYPE, '../../../../../..')),
            ('a device', odd_member('appdata/nextcloud/html/disk', tarfile.BLKTYPE)),
        ):
            for old in (False, True):
                with self.subTest(case, old_python=old), tempfile.TemporaryDirectory() as tmp:
                    server = Server(tmp, odd=[member])
                    with python_without_the_filter() if old else contextlib.nullcontext():
                        restored, said = server.offer()
                    self.assertFalse(restored)
                    self.assertIn('did not work', said)
                    self.assertEqual(list(server.data.iterdir()), [])
                    self.assertFalse((Path(tmp) / 'evil').exists())

    def test_a_chain_of_links_each_inside_as_text_never_climbs_out(self):
        # T5's review of d58e12e: a -> '.', a/b -> '..', b/b2 -> '..' ... each stays inside as
        # text, but followed on the disk they climb out; without the filter (Debian 12) the
        # file after them was written outside the folder, as root.
        escaped = f'OUT-{os.getpid()}-chain'
        part, path = 'appdata/nextcloud', 'appdata/nextcloud/a/b'
        chain = [odd_member(f'{part}/a', tarfile.SYMTYPE, '.'), odd_member(path, tarfile.SYMTYPE, '..')]
        for n in range(2, 8):
            path += f'/b{n}'
            chain.append(odd_member(path, tarfile.SYMTYPE, '..'))
        chain.append(odd_member(f'{path}/{escaped}'))
        for old in (False, True):
            with self.subTest(old_python=old), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp, odd=chain)
                with python_without_the_filter() if old else contextlib.nullcontext():
                    restored, said = server.offer()
                self.assertFalse(restored)
                self.assertIn('leads out of its folder', said)
                self.assertEqual(list(server.data.iterdir()), [])
                staging = server.data / nextcloud_restore.STAGING / '.unpacking'
                for folder in staging.parents:
                    self.assertFalse((folder / escaped).exists(), folder)


class FailedStepTests(unittest.TestCase):
    def test_a_step_that_fails_puts_the_ssd_back_as_it_was(self):
        def broken_up(base, *a):
            raise RuntimeError('Nextcloud (docker compose up) failed: no space left')

        def never_ready(base):
            return {'installed': True, 'maintenance': True}

        for case, kw in (
            ('the database copy does not load', {'fail': 'nextcloud_restoring --single-transaction'}),
            ('Nextcloud does not start', {'up': broken_up}),
            ('the file scan fails', {'fail': 'files:scan'}),
            ('Nextcloud never answers as ready', {'status': never_ready}),
        ):
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                server = Server(tmp)
                # Setup may have made Nextcloud's empty folders on the new SSD already.
                (server.data / 'appdata/nextcloud/html').mkdir(parents=True)
                (server.data / 'compose/nextcloud').mkdir(parents=True)
                ssd_before, files_before = server.tree(server.data), server.tree(server.files)
                with mock.patch.object(nextcloud_restore, 'READY_WAIT', 0):
                    restored, said = server.offer(**kw)
                self.assertFalse(restored)
                self.assertIn('did not work', said)
                self.assertIn('the SSD was put back as it was', said)
                self.assertEqual(server.tree(server.data), ssd_before)
                self.assertEqual(server.tree(server.files), files_before)
                self.assertTrue(any(c.endswith(' down') for c in server.commands()))

    def test_setup_stopped_with_ctrl_c_puts_the_ssd_back_too(self):
        def stopped(base, *a):
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            with self.assertRaises(KeyboardInterrupt):
                server.offer(up=stopped)
            self.assertEqual(list(server.data.iterdir()), [])

    def test_a_damaged_snapshot_is_never_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(tmp)
            tar = server.snapshot / 'data.tar'
            tar.write_bytes(tar.read_bytes()[:-1] + b'x')
            restored, said = server.offer()
            self.assertFalse(restored)
            self.assertEqual(server.asked, [])
            self.assertIn('is damaged', said)


if __name__ == '__main__':
    unittest.main()
