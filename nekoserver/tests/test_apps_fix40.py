"""The 0.40.1 apps fixes: a decode-only chip is an answer, Nextcloud with two-factor
sign-in, odd file names, smb3 and automount shares, FAT and exFAT drives whose inode
numbers change, existing logins with dots or spaces, and Seerr without Sonarr and Radarr."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADMIN = {'username': 'admin', 'password': 'a-long-password', 'state': 'planned', 'allow_create': False}


class DecodeOnlyChipTests(unittest.TestCase):
    """A chip that decodes but cannot encode: Immich stays in software, and that is done."""
    def test_no_encoder_is_an_answer_not_a_step_left(self):
        from modules import hardware_video as hv
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / 'r.json'
            done = {'jellyfin': 'on (vaapi), decoding only: the chip has no working video encoder', 'immich': hv.NO_ENCODER}
            self.assertIsNone(hv.outcome(mock.Mock(), report, [('amd', '/dev/dri/renderD128', 'vaapi', 'vaapi')], done))
            self.assertEqual(json.loads(report.read_text())['status'], 'hardware')


class FakeNextcloud:
    """Nextcloud's local API: installed, and refusing the password over WebDAV (HTTP 401)."""
    def __init__(self, refuse=True):
        self.refuse = refuse
        self.headers = {}

    def call(self, endpoint, method='GET', payload=None, **kwargs):
        import account_setup
        if endpoint == '/status.php':
            return {'installed': True, 'maintenance': False, 'needsDbUpgrade': False}
        if self.refuse:
            raise account_setup.HTTPFailure(401)
        return b''

    def close(self):
        pass


def occ(two_factor, enforced=False, named=(), excluded=()):
    """docker, as Nextcloud's occ answers it (core/Command/TwoFactorAuth/Enforce.php and
    config:list). Two-factor sign-in required for everyone (enforced), or for the groups
    `named`, or for all but the groups `excluded`; the administrator is in the group admin."""
    def run(args, input_text=None):
        if 'user:info' in args:
            return json.dumps({'enabled': True, 'groups': ['admin']})
        if 'twofactorauth:state' in args:
            return 'Two-factor authentication is ' + ('' if two_factor else 'not ') + 'enabled for user admin\n'
        if 'twofactorauth:enforce' in args:
            return enforce_message(enforced or bool(named or excluded), named, excluded)
        if args[-3:] == ['config:list', 'system', '--output=json']:
            if named == 'unreadable':
                raise RuntimeError('Local account setup command failed; private output was suppressed')
            return json.dumps({'system': {'instanceid': 'oc1', 'twofactor_enforced': 'true' if enforced or named or excluded else 'false',
                                          'twofactor_enforced_groups': list(named), 'twofactor_enforced_excluded_groups': list(excluded)}})
        raise AssertionError(args)
    return run


def enforce_message(enforced, named, excluded):
    """What occ twofactorauth:enforce prints."""
    if not enforced:
        return 'Two-factor authentication is not enforced\n'
    text = ('Two-factor authentication is enforced for members of the group(s) ' + ', '.join(named) if named
            else 'Two-factor authentication is enforced for all users')
    return text + (', except members of ' + ', '.join(excluded) if excluded else '') + '\n'


class NextcloudTwoFactorTests(unittest.TestCase):
    """With two-factor sign-in on, Nextcloud refuses plain passwords over WebDAV."""
    CONFIG = MappingProxyType({'data_mount': '/srv/x'})   # read-only: shared by every test

    def check(self, docker, refuse=True):
        import account_setup
        with mock.patch.object(account_setup, 'docker', side_effect=docker), \
                mock.patch.object(account_setup, 'safe_path', side_effect=Path):
            return account_setup.nextcloud(FakeNextcloud(refuse), ADMIN, self.CONFIG)

    def test_two_factor_on_is_a_checked_account(self):
        import account_setup
        self.assertEqual(self.check(occ(True)), account_setup.NEXTCLOUD_TWO_FACTOR)
        self.assertIn('app password', account_setup.NEXTCLOUD_TWO_FACTOR)

    def test_two_factor_required_for_everyone_counts_too(self):
        import account_setup
        self.assertEqual(self.check(occ(False, enforced=True)), account_setup.NEXTCLOUD_TWO_FACTOR)

    def test_two_factor_required_only_of_others_does_not_excuse_a_wrong_password(self):
        """QA-07: required for some groups only, or for everyone but the administrator's group:
        Nextcloud takes a plain password from the administrator, so a 401 means a wrong one."""
        import account_setup
        for docker in (occ(False, named=['family']), occ(False, excluded=['admin']), occ(False, named=['family'], excluded=['admin']),
                       occ(False, named='unreadable')):
            with self.subTest(docker=docker), self.assertRaises(account_setup.HTTPFailure):
                self.check(docker)

    def test_two_factor_required_of_the_administrators_group_counts(self):
        import account_setup
        for docker in (occ(False, named=['family', 'admin']), occ(False, excluded=['kids'])):
            with self.subTest(docker=docker):
                self.assertEqual(self.check(docker), account_setup.NEXTCLOUD_TWO_FACTOR)

    def test_the_rule_is_nextclouds_own(self):
        import account_setup
        rule = account_setup.two_factor_required_of

        def listed(**system):
            return json.dumps({'system': system})
        self.assertTrue(rule(listed(twofactor_enforced='true'), ['admin']))
        self.assertFalse(rule(listed(twofactor_enforced='false', twofactor_enforced_groups=['admin']), ['admin']))
        # Named groups overrule the excluded ones, as in MandatoryTwoFactor::isEnforcedFor.
        self.assertTrue(rule(listed(twofactor_enforced='true', twofactor_enforced_groups=['admin'],
                                    twofactor_enforced_excluded_groups=['admin']), ['admin']))
        for broken in ('', 'not json', '[]', json.dumps({'system': []}), listed(twofactor_enforced='true', twofactor_enforced_groups='admin')):
            with self.subTest(broken=broken):
                self.assertFalse(rule(broken, ['admin']))

    def test_a_wrong_password_without_two_factor_still_fails(self):
        import account_setup
        docker = occ(False)
        with self.assertRaises(account_setup.HTTPFailure):
            self.check(docker)

    def test_a_password_that_works_needs_no_note(self):
        self.assertIsNone(self.check(occ(False), refuse=False))

    def test_the_accounts_step_is_not_left_pending(self):
        import account_setup
        logged = []
        with mock.patch.object(account_setup.store, 'get', return_value=dict(ADMIN)), \
                mock.patch.object(account_setup.store, 'update'), \
                mock.patch.object(account_setup, 'verify_project'), \
                mock.patch.object(account_setup, 'wait'), \
                mock.patch.object(account_setup, 'docker', side_effect=occ(True)), \
                mock.patch.object(account_setup, 'safe_path', side_effect=Path):
            pending = account_setup.provision(self.CONFIG, ['nextcloud'], output=logged.append,
                                              client_factory=lambda app: FakeNextcloud())
        self.assertEqual(pending, [])
        self.assertIn('two-factor sign-in is on', logged[-1])


def make_tree(base, names):
    """A source folder with one album holding `names` (bytes), each 3 bytes long."""
    album = Path(base) / 'src' / 'Album'
    album.mkdir(parents=True)
    for name in names:
        with open(os.fsencode(str(album)) + b'/' + name, 'wb') as f:
            f.write(b'abc')
    (Path(base) / 'hdd' / 'library' / 'music').mkdir(parents=True)
    return {'type': 'local', 'path': str(Path(base) / 'src'), 'category': 'music'}


def library(base):
    return sorted(os.listdir(os.fsencode(str(Path(base) / 'hdd/library/music/Album'))))


class OddFileNameTests(unittest.TestCase):
    """An old file name that is not UTF-8 (Latin-1, say) never stops a whole import."""
    def test_a_latin1_name_is_copied_with_the_rest(self):
        import source_import as si
        with tempfile.TemporaryDirectory() as d:
            source = make_tree(d, [b'Caf\xe9.flac', b'ok.flac'])
            row = si.copy_source(source, Path(d) / 'hdd', apply=True)
            self.assertEqual((row['status'], row['copied']), ('copied', 2))
            self.assertEqual(library(d), [b'Caf\xe9.flac', b'ok.flac'])

    def test_a_name_that_cannot_be_used_is_skipped_and_named(self):
        import source_import as si
        real = si.import_file

        def refuse_odd(current, *args):
            if 'bad' in current.name:
                raise UnicodeEncodeError('utf-8', current.name, 0, 1, 'surrogates not allowed')
            return real(current, *args)
        with tempfile.TemporaryDirectory() as d:
            source = make_tree(d, [b'bad\xe9.flac', b'ok.flac'])
            with mock.patch.object(si, 'import_file', side_effect=refuse_odd):
                row = si.copy_source(source, Path(d) / 'hdd', apply=True)
            self.assertEqual((row['copied'], row['skipped_names']), (1, 1))
            self.assertIn('Album/bad\\xe9.flac', row['reason'])
            self.assertEqual(library(d), [b'ok.flac'])
            json.dumps(row)   # the report can be saved


class NetworkShareTypeTests(unittest.TestCase):
    """SMB shares mounted as smb3, and shares mounted only when first opened."""
    def source(self, d):
        return {'category': 'movies', 'type': 'remote', 'uri': 'smb://nas/movies', 'mounted_path': d, 'copy_to_library': True}

    def test_an_smb3_share_is_a_mounted_share(self):
        import source_import as si
        with tempfile.TemporaryDirectory() as d, mock.patch.object(si, 'findmnt', return_value='smb3'):
            self.assertEqual(si.source_folder(self.source(d)), Path(d))

    def test_the_folder_is_opened_before_asking_what_is_mounted(self):
        import source_import as si
        seen = []
        with tempfile.TemporaryDirectory() as d:
            real = os.scandir
            with mock.patch.object(si.os, 'scandir', side_effect=lambda p: seen.append('opened') or real(p)), \
                    mock.patch.object(si, 'findmnt', side_effect=lambda *a: seen.append('findmnt') or 'cifs'):
                si.source_folder(self.source(d))
        self.assertEqual(seen, ['opened', 'findmnt'])

    def test_an_automount_that_did_not_connect_is_not_mounted_yet(self):
        import source_import as si
        with tempfile.TemporaryDirectory() as d, mock.patch.object(si, 'findmnt', return_value='autofs'):
            row = si.import_one(self.source(d), Path(d), True, (None, None), None, {}, again=True)
            self.assertEqual(row['status'], 'needs_setup')
            self.assertIn('not mounted yet', row['reason'])
            finished = {si.source_key(self.source(d)): 'movies'}
            row = si.import_one(self.source(d), Path(d), True, (None, None), None, finished, again=True)
            self.assertEqual(row['status'], 'imported_earlier')


class UnstableInodeTests(unittest.TestCase):
    """FAT and exFAT drives (and some SMB shares) number a file again when the kernel
    forgets it, mid-import: simulated by swapping a planned file for an identical copy."""
    def renumbered_import(self, fstype):
        import source_import as si
        real = si.planned_copies

        def plan_then_renumber(*args, **kwargs):
            entries = real(*args, **kwargs)
            for current, _, stat in entries:
                copy = current.with_name('.renumber')
                copy.write_bytes(current.read_bytes())
                os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                os.replace(copy, current)   # the same file, size and date, with another inode
            return entries
        with tempfile.TemporaryDirectory() as d:
            source = make_tree(d, [b'one.flac', b'two.flac'])
            with mock.patch.object(si, 'planned_copies', side_effect=plan_then_renumber), \
                    mock.patch.object(si, 'findmnt', return_value=fstype):
                row = si.import_one(source, Path(d) / 'hdd', True, (None, None), None, {})
            return row, library(d) if (Path(d) / 'hdd/library/music/Album').exists() else []

    def test_an_exfat_drive_is_copied_whole(self):
        row, copied = self.renumbered_import('exfat')
        self.assertEqual(row['status'], 'copied')
        self.assertEqual(copied, [b'one.flac', b'two.flac'])

    def test_on_ext4_a_new_inode_is_still_a_changed_file_and_is_named(self):
        row, copied = self.renumbered_import('ext4')
        self.assertEqual(row['status'], 'needs_setup')
        self.assertIn('Album/one.flac', row['reason'])
        self.assertEqual(copied, [])

    def test_a_changed_size_is_still_caught_without_inodes(self):
        import source_import as si
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, [b'one.flac'])
            path = Path(d) / 'src/Album/one.flac'
            planned = path.lstat()
            path.write_bytes(b'abcdef')
            with self.assertRaises(si.SourceChanged):
                si.open_source(path, planned, stable=False)

    def test_filesystem_types(self):
        import source_import as si
        for kind, stable in (('vfat', False), ('exfat', False), ('fuseblk', False), ('fuse.sshfs', False),
                             ('smb3', False), ('ext4', True), ('xfs', True), (None, True)):
            with mock.patch.object(si, 'findmnt', return_value=kind):
                self.assertIs(si.stable_inodes('/media/usb'), stable, kind)


class ExistingLoginNameTests(unittest.TestCase):
    """A login you already have in the app is taken as the app has it."""
    def test_names_the_apps_allow_are_accepted_for_an_existing_login(self):
        import account_store
        for app, name in (('jellyfin', 'mary.smith'), ('jellyfin', 'Mary Smith'), ('sonarr', '1stuser'),
                          ('immich', 'admin@localhost'), ('nextcloud', 'mary.smith')):
            account_store.validate_login(app, name, 'pw', existing=True)

    def test_unsafe_names_are_still_refused(self):
        import account_store
        for app, name in (('jellyfin', 'tab\there'), ('jellyfin', ' lead'), ('nextcloud', 'a:b'),
                          ('nextcloud', '-option'), ('immich', 'no-at-sign'), ('jellyfin', 'x' * 129)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                account_store.validate_login(app, name, 'pw', existing=True)

    def test_new_logins_keep_the_strict_pattern(self):
        import account_store
        with self.assertRaises(ValueError):
            account_store.validate_login('jellyfin', 'mary.smith', 'a-long-password', existing=False)

    def test_collect_saves_an_existing_dotted_login(self):
        import account_store
        saved = []
        answers = iter(['2', 'mary.smith'])
        passwords = iter(['pw', 'pw'])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'appdata/jellyfin/config').mkdir(parents=True)   # existing app data
            empty = {'format': 1, 'mode': 'per-app', 'apps': {}}
            with mock.patch.object(account_store, 'load', return_value=empty), \
                    mock.patch.object(account_store, 'save', lambda config, v: saved.append(json.loads(json.dumps(v)))):
                account_store.collect({'data_mount': d}, ['jellyfin'], prompt=lambda q='': next(answers),
                                      password_prompt=lambda q='': next(passwords), output=lambda s: None)
        entry = saved[-1]['apps']['jellyfin']
        self.assertEqual((entry['username'], entry['allow_create']), ('mary.smith', False))


class SeerrWithoutRequestAppsTests(unittest.TestCase):
    """Seerr kept without Sonarr and Radarr has nothing to send requests to."""
    def test_the_library_row_is_not_a_step_left(self):
        import media_libraries
        self.assertEqual(media_libraries.request_providers_row(('jellyfin', 'seerr', 'qbittorrent'))['status'], 'pass')
        self.assertEqual(media_libraries.request_providers_row(('jellyfin', 'seerr', 'radarr'))['status'], 'needs_setup')

    def test_the_provider_check_passes_with_a_note(self):
        import seerr_providers
        rows = seerr_providers.configure('/srv/x', apps=('jellyfin', 'seerr'), factory=mock.Mock(side_effect=AssertionError))
        self.assertEqual([r['status'] for r in rows], ['pass'])
        self.assertIn('Not needed', rows[0]['reason'])


class RemovedAppNamedTests(unittest.TestCase):
    """Connections setup does not take away are named when Prowlarr or Jellyfin is removed."""
    def test_removing_prowlarr_names_the_indexers_left_in_the_arr_apps(self):
        from modules import onboarding
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'compose/prowlarr').mkdir(parents=True)
            (Path(d) / 'compose/prowlarr/compose.yaml').write_text('services: {}\n')
            logged = []
            ctx = mock.Mock(config={'data_mount': d, 'media': {'services': ['jellyfin', 'sonarr']}}, log=logged.append)
            onboarding.remove_stale(ctx, Path(d) / 'release', Path(d) / 'config.json')
            self.assertTrue(any('Prowlarr is no longer' in line for line in logged))
            ctx.config['media']['services'] = ['jellyfin']   # no app keeps its indexers
            logged.clear()
            onboarding.remove_stale(ctx, Path(d) / 'release', Path(d) / 'config.json')
            self.assertEqual(logged, [])


if __name__ == '__main__':
    unittest.main()
