"""0.50.0: a folder a NAS shares over NFS as the media drive or the backup drive
(nfs_exports.py, modules/network_storage.py and what the drives step, the weekly copy
and the recovery menu do with one). showmount, mount and the NAS's answers are stood in
for; the checks of a shared folder run on a real folder of this machine."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host  # noqa: E402
import native_runtime  # noqa: E402
import neko_common  # noqa: E402
import nfs_exports as nfs  # noqa: E402
import recovery_wizard  # noqa: E402
from modules import network_storage as nas  # noqa: E402
from modules import photo_backup, server_os, storage  # noqa: E402

NAS = '192.168.1.20'
MEDIA = f'{NAS}:/volume1/media'
BACKUP = f'{NAS}:/volume1/backup'
MEDIA_ID = 'nfs-445c95883321d23be4c45e535d45cc97'  # the same value in the runtime's test (common.rs)
GB = 1024**3


def done(code=0, out='', err=''):
    return mock.Mock(returncode=code, stdout=out, stderr=err)


def media_record(fstype='nfs4'):
    return nfs.record_for(MEDIA, fstype)


class AddressTests(unittest.TestCase):
    def test_a_shared_folder_is_named_by_the_nas_and_its_folder(self):
        self.assertEqual(nfs.address(NAS, '/volume1/media/'), MEDIA)
        self.assertEqual(nfs.address('nas.home', '/mnt/HD/HD_a2/Public'), 'nas.home:/mnt/HD/HD_a2/Public')
        self.assertEqual(nfs.parts(MEDIA), (NAS, '/volume1/media'))
        # Nothing a line of /etc/fstab could not hold, and no way out of the folder.
        for folder in ('volume1/media', '/My Media', '/a/../b', '/', '', '/a\\040b', '/a#b'):
            self.assertIsNone(nfs.address(NAS, folder), folder)
        self.assertIsNone(nfs.address('not a host', '/x'))
        with self.assertRaisesRegex(RuntimeError, 'Not the address of a folder shared over NFS'):
            nfs.parts('/dev/sdb1')

    def test_its_identity_stands_in_for_a_uuid_and_is_the_runtimes(self):
        self.assertEqual(nfs.identity(MEDIA), MEDIA_ID)
        self.assertNotEqual(nfs.identity(BACKUP), MEDIA_ID)
        # It passes wherever a UUID is checked (storage.mount_specs, the weekly copy).
        self.assertRegex(MEDIA_ID, r'^[A-Za-z0-9-]{8,80}$')
        common = (ROOT / 'runtime/src/common.rs').read_text()
        self.assertIn(f'assert_eq!(id, "{MEDIA_ID}");', common)

    def test_the_record_names_the_folder_and_a_changed_one_is_refused(self):
        record = media_record()
        self.assertEqual(record, {'uuid': MEDIA_ID, 'fstype': 'nfs4', 'network': {'transport': 'nfs', 'export': MEDIA}})
        self.assertEqual(nfs.export_of(record), MEDIA)
        self.assertEqual(nfs.checked_record(record, 'x'), MEDIA)
        # A disk, and a disk on the NAS over iSCSI, are not shared folders.
        self.assertIsNone(nfs.export_of({'uuid': 'abcd-1234', 'fstype': 'ext4'}))
        self.assertIsNone(nfs.export_of({'uuid': 'u', 'network': {'transport': 'iscsi', 'wwn': '0x1'}}))
        for changed in ({'uuid': 'nfs-0000'}, {'fstype': 'ext4'}):
            with self.assertRaisesRegex(RuntimeError, 'changed by hand'):
                nfs.checked_record({**record, **changed}, '"media_hdd" under "storage"')

    def test_what_is_mounted_is_told_from_findmnt(self):
        self.assertEqual(nfs.mounted(f'nfs4 {MEDIA}'), (MEDIA_ID, 'nfs4'))
        self.assertEqual(nfs.mounted(f'nfs {MEDIA}'), (MEDIA_ID, 'nfs'))
        for other in ('abcd-1234 ext4 /dev/sdb1', 'autofs systemd-1', 'cifs //nas/share', '', None):
            self.assertIsNone(nfs.mounted(other))

    def test_the_line_in_fstab_never_holds_up_the_start(self):
        line = nfs.fstab_line(MEDIA, '/mnt/nekoserver-media', 'nfs4')
        self.assertEqual(
            line,
            f'{MEDIA} /mnt/nekoserver-media nfs nfsvers=4,hard,nodev,nosuid,_netdev,nofail,x-systemd.automount,'
            'x-systemd.mount-timeout=30s 0 0',
        )
        self.assertIn('nfsvers=3,', nfs.fstab_line(MEDIA, '/mnt/m', 'nfs'))
        self.assertTrue(nfs.own_line(line.split()))
        self.assertFalse(nfs.own_line(f'{MEDIA} /mnt/m nfs defaults 0 0'.split()))
        self.assertFalse(nfs.own_line('UUID=abcd /mnt/m ext4 defaults,nofail 0 2'.split()))
        with self.assertRaisesRegex(RuntimeError, 'whitespace'):
            nfs.fstab_line(MEDIA, '/mnt/my media', 'nfs4')


class FindingTests(unittest.TestCase):
    def test_the_nas_own_list_is_read(self):
        shown = '/volume1/media 192.168.1.0/24,192.168.1.30\n/volume1/backup *\n/volume1/My Films *\n/ *\n\n'
        run = mock.Mock(return_value=done(out=shown))
        self.assertEqual(
            nfs.shared_folders(NAS, run),
            [('/volume1/media', '192.168.1.0/24,192.168.1.30'), ('/volume1/backup', '*')],
        )
        self.assertEqual(run.call_args.args[0], ['showmount', '-e', '--no-headers', NAS])

    def test_a_nas_that_gives_no_list_has_none(self):
        # NFS version 4 alone: showmount has nothing to ask (or is not answered).
        self.assertEqual(nfs.shared_folders(NAS, mock.Mock(return_value=done(1, err='clnt_create: RPC'))), [])
        slow = mock.Mock(side_effect=subprocess.TimeoutExpired('showmount', 30))
        self.assertEqual(nfs.shared_folders(NAS, slow), [])
        self.assertEqual(nfs.shared_folders(NAS, mock.Mock(side_effect=FileNotFoundError)), [])

    def test_each_linux_names_the_nfs_tools(self):
        names = {host.Debian: 'nfs-common', host.Arch: 'nfs-utils', host.Fedora: 'nfs-utils', host.Suse: 'nfs-client'}
        for family, package in names.items():
            self.assertEqual(family(lambda *a, **k: None, {}).names('nfs-utils'), [package], family)

    def test_the_tools_are_installed_only_when_missing(self):
        family = mock.Mock()
        family.install.return_value = []
        lines = []
        with mock.patch.object(host, 'current', return_value=family):
            nas.install_nfs_tools(lines.append)
        family.install.assert_called_once_with(['nfs-utils'])  # host.install adds only what is missing
        self.assertEqual(lines, ['NFS tools ready'])


@unittest.skipIf(sys.platform == 'win32', 'file owners, permissions and locks as Linux has them')
class TryingOutTests(unittest.TestCase):
    """The checks of a shared folder, on a folder of this machine standing in for it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.me = (os.getuid(), os.getgid())
        patcher = mock.patch.object(nfs, 'this_pc', return_value='192.168.1.50')
        patcher.start()
        self.addCleanup(patcher.stop)

    def try_out(self, as_user=lambda uid, gid, folder: ''):
        return nfs.try_out(self.folder, MEDIA, *self.me, as_user=as_user)

    def test_a_folder_that_works_passes_and_nothing_is_left_on_it(self):
        found = self.try_out()
        self.assertGreater(found['size'], 0)
        self.assertLessEqual(found['free'], found['size'])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_the_apps_own_part_runs_as_written(self):
        # The program the apps' account runs (run_as starts it as that account).
        spot = self.folder / 'apps'
        spot.mkdir()
        ran = subprocess.run(
            [sys.executable, '-I', '-c', nfs.AS_THE_APPS, str(spot), *map(str, self.me)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual((ran.returncode, ran.stdout.strip(), ran.stderr.strip()), (0, 'ok', ''))
        other = subprocess.run(
            [sys.executable, '-I', '-c', nfs.AS_THE_APPS, str(spot), '4242', '4242'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(other.stderr.strip(), 'owner %d:%d' % self.me)

    def test_it_is_tried_as_the_apps_account_in_a_folder_of_its_own(self):
        run = mock.Mock(return_value=done(out='ok\n'))
        self.assertEqual(nfs.run_as(1000, 1000, '/x/apps', run), '')
        self.assertEqual(run.call_args.kwargs['user'], 1000)
        self.assertEqual(run.call_args.kwargs['group'], 1000)
        self.assertEqual(run.call_args.kwargs['extra_groups'], [])
        self.assertEqual(
            nfs.run_as(1000, 1000, '/x', mock.Mock(return_value=done(1, err='lock No locks available'))),
            'lock No locks available',
        )
        hung = mock.Mock(side_effect=subprocess.TimeoutExpired('python', 90))
        self.assertEqual(nfs.run_as(1000, 1000, '/x', hung), 'lock no answer within 90 seconds')

    def refused(self, pattern, **changes):
        with self.assertRaisesRegex(nfs.Refused, pattern) as caught, mock.patch.multiple(nfs.os, **changes):
            self.try_out()
        self.assertEqual(list(self.folder.iterdir()), [])  # the test folder is always removed
        return str(caught.exception)

    def test_read_only_access_is_refused_with_what_to_allow(self):
        self.folder.chmod(0o500)
        self.addCleanup(self.folder.chmod, 0o700)
        if os.geteuid() == 0:
            self.skipTest('root writes anywhere on a local folder')
        said = self.refused('may read .* but not write to it', getpid=os.getpid)
        self.assertIn('give this PC (192.168.1.50) read and write access', said)
        self.assertIn('Nothing was changed', said)

    def test_root_squash_is_refused_with_its_names_on_a_nas(self):
        # The file root wrote belongs to the NAS's guest account.
        said = self.refused('turns this PC.s administrator \\(root\\) into a guest', geteuid=lambda: 4242)
        for name in ('root squash', 'no_root_squash', 'Squash: no mapping', 'Maproot user: root'):
            self.assertIn(name, said)
        # Or: giving a file to the apps' account is not permitted.
        said = self.refused('root squash', chown=mock.Mock(side_effect=PermissionError(1, 'Operation not permitted')))
        self.assertIn('a restore would hand every file to the wrong account', said)

    def test_an_owner_or_permissions_the_nas_does_not_keep_are_refused(self):
        with self.assertRaisesRegex(nfs.Refused, 'does not keep who owns a file .* "all squash"'):
            with mock.patch.object(nfs.os, 'chown'):  # said yes, kept nothing
                nfs.owner_kept(self.spot(), MEDIA, 4242, 4242)
        with self.assertRaisesRegex(nfs.Refused, 'does not keep the permissions .* Windows-style'):
            with mock.patch.object(nfs.os, 'chmod'):
                nfs.owner_kept(self.spot('b'), MEDIA, *self.me)

    def spot(self, name='a'):
        made = self.folder / name
        made.mkdir()
        return made

    def test_what_the_apps_account_cannot_do_is_refused_with_the_reason(self):
        cases = {
            'owner 65534:65534': 'belongs to 65534:65534 there: the NAS maps every account',
            'lock No locks available': 'File locks do not work .*No locks available.*NFS version 4',
            'shared': 'File locks do not work .*given to another as well',
            'lock no answer within 90 seconds': 'File locks do not work .*no answer within 90 seconds',
            'write Permission denied': 'could not use a folder of its own .*write Permission denied.*permissions 755',
        }
        for said, pattern in cases.items():
            with self.subTest(said=said), self.assertRaisesRegex(nfs.Refused, pattern):
                self.try_out(as_user=lambda uid, gid, folder, said=said: said)
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_size_that_makes_no_sense_is_refused(self):
        def reported(blocks, free):
            return SimpleNamespace(f_blocks=blocks, f_bavail=free, f_frsize=4096)

        for blocks, free in ((0, 0), (10, 20), (2**60, 5)):
            with self.subTest(blocks=blocks), mock.patch.object(nfs.os, 'statvfs', return_value=reported(blocks, free)):
                with self.assertRaisesRegex(nfs.Refused, 'reports no usable size'):
                    nfs.room(self.folder, MEDIA)
        with mock.patch.object(nfs.os, 'statvfs', return_value=reported(1000, 400)):
            self.assertEqual(nfs.room(self.folder, MEDIA), {'size': 4096000, 'free': 1638400})


class MountForTheTestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(nfs, 'this_pc', return_value='192.168.1.50')
        patcher.start()
        self.addCleanup(patcher.stop)

    def nas(self, answers):
        """A stand-in for mount and umount: `answers` maps an NFS version to mount's result."""
        calls = []

        def run(argv, **_):
            calls.append(argv)
            if argv[0] == 'mount':
                version = argv[4].split(',')[0].removeprefix('nfsvers=')
                return answers[version]
            return done()

        return run, calls

    def test_version_4_first_then_3_and_always_unmounted(self):
        folder = Path(self.tmp.name) / 'test'
        run, calls = self.nas({'4': done(32, err='mount.nfs: Protocol not supported'), '3': done()})
        with mock.patch.object(nfs, 'try_out', return_value={'size': 10, 'free': 5}) as tried:
            found = nfs.tried(MEDIA, 1000, 1000, run, folder)
        self.assertEqual(found, {'fstype': 'nfs', 'size': 10, 'free': 5})
        tried.assert_called_once_with(folder, MEDIA, 1000, 1000)
        mounts = [c for c in calls if c[0] == 'mount']
        self.assertEqual([c[4].split(',')[0] for c in mounts], ['nfsvers=4', 'nfsvers=3'])
        # Private, and never waiting for good on a NAS that stops answering mid-test.
        self.assertEqual(mounts[0][:4], ['mount', '-t', 'nfs', '-o'])
        self.assertIn('soft', mounts[0][4].split(','))
        self.assertEqual(mounts[0][5:], [MEDIA, str(folder)])
        self.assertEqual(calls[-1], ['umount', str(folder)])
        self.assertFalse(folder.exists())

    def test_a_refusal_is_unmounted_too(self):
        folder = Path(self.tmp.name) / 'test'
        run, calls = self.nas({'4': done()})
        with mock.patch.object(nfs, 'try_out', side_effect=nfs.Refused('no locks')):
            with self.assertRaisesRegex(nfs.Refused, 'no locks'):
                nfs.tried(MEDIA, 1000, 1000, run, folder)
        self.assertEqual(calls[-1], ['umount', str(folder)])

    def test_a_nas_that_will_not_mount_it_says_what_to_change(self):
        cases = {
            'mount.nfs: access denied by server while mounting': "allow this PC's address \\(192.168.1.50\\)",
            'mount.nfs: mounting failed, reason given by server: No such file or directory': 'check the name of the shared folder .*/volume1/media was not found',
            'mount.nfs: Connection refused': 'check that NFS is switched on',
        }
        for said, fix in cases.items():
            run, _ = self.nas({'4': done(32, err=said), '3': done(32, err=said)})
            with self.subTest(said=said), self.assertRaisesRegex(nfs.Refused, fix) as caught:
                nfs.tried(MEDIA, 1000, 1000, run, Path(self.tmp.name) / 'test')
            self.assertIn(f'would not let this PC open {MEDIA}', str(caught.exception))
            self.assertIn('Nothing was changed', str(caught.exception))


class Ctx:
    apply = True

    def __init__(self, config):
        self.config, self.lines, self.saved = config, [], 0

    def log(self, line):
        self.lines.append(line)

    def persist_config(self):
        self.saved += 1


class KindTests(unittest.TestCase):
    """What the NAS offers at its address decides between iSCSI and NFS."""

    def kind(self, options, typed=(), iscsi=False, shared=False, interactive=True):
        answers = iter(typed)
        lines = []
        with (
            mock.patch.object(nas, 'answers', return_value=iscsi),
            mock.patch.object(nas, 'nfs_answers', return_value=shared),
        ):
            chosen = nas.chosen_kind(options, interactive, lines.append, lambda prompt: next(answers))
        return chosen, lines

    def test_a_nas_chosen_before_stays_iscsi_and_nobody_is_asked(self):
        self.assertEqual(
            self.kind({'wanted': True, 'portal': f'{NAS}:3260', 'targets': ['iqn.2013-03.com.x:m']})[0], 'iscsi'
        )
        self.assertEqual(self.kind({'wanted': True}, interactive=False)[0], 'iscsi')
        self.assertEqual(self.kind({'wanted': True, 'kind': 'nfs'})[0], 'nfs')

    def test_only_nfs_answers(self):
        options = {'wanted': True}
        chosen, lines = self.kind(options, [NAS], shared=True)
        self.assertEqual(chosen, 'nfs')
        self.assertEqual(options, {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS}})
        self.assertIn(f'The NAS at {NAS} offers folders shared over NFS: NekoServer uses those', lines)

    def test_only_iscsi_answers(self):
        options = {'wanted': True}
        self.assertEqual(self.kind(options, [NAS], iscsi=True)[0], 'iscsi')
        self.assertEqual(options, {'wanted': True, 'kind': 'iscsi', 'portal': f'{NAS}:3260'})

    def test_both_answer_and_the_person_chooses(self):
        options = {'wanted': True}
        chosen, lines = self.kind(options, [NAS, 'x', '2'], iscsi=True, shared=True)
        self.assertEqual(chosen, 'nfs')
        said = '\n'.join(lines)
        self.assertIn('1) iSCSI', said)
        self.assertIn('2) NFS: a folder the NAS shares. Nothing has to be formatted', said)
        self.assertIn('Type 1 or 2.', said)
        self.assertEqual(self.kind({'wanted': True}, [NAS, ''], iscsi=True, shared=True)[0], 'iscsi')  # Enter

    def test_nothing_answers(self):
        with self.assertRaisesRegex(
            RuntimeError, f'Nothing answers at {NAS}, neither iSCSI .port 3260. nor NFS .port 2049.'
        ):
            self.kind({'wanted': True}, [NAS])

    def test_a_broken_setting_stops_before_anything_changes(self):
        for values in (
            {'kind': 'smb'},
            {'nfs': []},
            {'nfs': {'server': NAS, 'media': 'volume1'}},
            {'nfs': {'media': '/volume1/media'}},
        ):
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                nas.options({'network_storage': {'wanted': True, **values}})
        saved = {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS, 'media': '/volume1/media'}}
        self.assertEqual(nas.options({'network_storage': saved}), saved)


class StepTests(unittest.TestCase):
    """The network storage step with a NAS that shares folders, every command stood in for."""

    LISTED = [('/volume1/media', '192.168.1.0/24'), ('/volume1/backup', '192.168.1.0/24')]

    def step(self, config, typed=(), listed=None, tried=None, interactive=True, answers=True):
        ctx = Ctx(config)
        answers_typed = iter(typed)
        tried = tried or (lambda export, uid, gid: {'fstype': 'nfs4', 'size': 2000 * GB, 'free': 1500 * GB})
        with (
            mock.patch.object(nas.os, 'geteuid', return_value=0),
            mock.patch('sys.stdin.isatty', return_value=interactive),
            mock.patch('builtins.input', side_effect=lambda prompt='': next(answers_typed)),
            mock.patch.object(nas, 'install_nfs_tools') as tools,
            mock.patch.object(nas, 'install_tools') as iscsi_tools,
            mock.patch.object(nas, 'wait_for_network') as waited,
            mock.patch.object(nas, 'answers', return_value=False),
            mock.patch.object(nas, 'nfs_answers', return_value=answers),
            mock.patch.object(nfs, 'shared_folders', return_value=self.LISTED if listed is None else listed),
            mock.patch.object(nfs, 'tried', side_effect=tried) as tried_out,
        ):
            result = nas.run(ctx)
            tools.assert_called_once()
            waited.assert_called_once()
            iscsi_tools.assert_not_called()  # no iSCSI tools for a NAS used over NFS
        return ctx, result, tried_out

    def test_the_first_run_finds_asks_tries_and_keeps_both_folders(self):
        config = {'network_storage': {'wanted': True}, 'media': {'uid': 1001, 'gid': 1002}}
        ctx, result, tried = self.step(config, [NAS, '1', '/volume1/backup'])
        self.assertIsNone(result)
        self.assertEqual(
            config['network_storage'],
            {
                'wanted': True,
                'kind': 'nfs',
                'nfs': {'server': NAS, 'media': '/volume1/media', 'backup': '/volume1/backup'},
            },
        )
        # Tried as the apps' account before anything is kept.
        self.assertEqual([c.args for c in tried.call_args_list], [(MEDIA, 1001, 1002), (BACKUP, 1001, 1002)])
        self.assertEqual(config['storage']['media_hdd'], media_record())
        self.assertEqual(
            {k: v for k, v in config['photos'].items() if k.startswith('external_backup')},
            {
                'external_backup_uuid': nfs.identity(BACKUP),
                'external_backup_fstype': 'nfs4',
                'external_backup_network': {'transport': 'nfs', 'export': BACKUP},
            },
        )
        self.assertNotIn('data_ssd', config['storage'])  # never asked for, never offered
        self.assertGreaterEqual(ctx.saved, 1)
        said = '\n'.join(ctx.lines)
        self.assertIn('experimental', said)
        self.assertIn('A folder shared over NFS is for films, music and backups.', said)
        self.assertNotIn('lower', said)  # no promise about speed (the wizard specification)
        self.assertIn(f'The NAS at {NAS} shares these folders over NFS:', said)
        self.assertIn('  1) /volume1/media  (for: 192.168.1.0/24)', said)
        self.assertIn(f"{MEDIA}: tried out as root and as the apps' account (1001:1002)", said)
        self.assertIn('NFS version 4); 1500 GB free of 2000 GB', said)
        self.assertIn('one box', said)

    def test_only_the_media_drive_on_the_nas(self):
        config = {'network_storage': {'wanted': True}}
        ctx, _, tried = self.step(config, [NAS, '1', ''])
        self.assertEqual(config['network_storage']['nfs'], {'server': NAS, 'media': '/volume1/media'})
        self.assertEqual(tried.call_count, 1)
        self.assertNotIn('photos', config)
        self.assertNotIn('one box', '\n'.join(ctx.lines))

    def test_a_nas_with_no_list_has_the_folder_typed(self):
        config = {'network_storage': {'wanted': True}}
        ctx, _, _ = self.step(config, [NAS, 'media', '/export/media', ''], listed=[])
        self.assertEqual(config['network_storage']['nfs']['media'], '/export/media')
        said = '\n'.join(ctx.lines)
        self.assertIn('gives no list of its shared folders', said)
        self.assertIn('starting with / and without spaces', said)  # "media" was not a folder's name

    def test_no_folder_chosen_changes_nothing(self):
        config = {'network_storage': {'wanted': True}}
        with self.assertRaisesRegex(RuntimeError, 'No shared folder was chosen'):
            self.step(config, [NAS, '', ''])
        self.assertNotIn('storage', config)

    def test_a_later_run_asks_nothing_and_tries_the_folders_again(self):
        saved = {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS, 'media': '/volume1/media'}}
        config = {'network_storage': saved, 'storage': {'media_hdd': media_record()}}
        ctx, _, tried = self.step(config, interactive=False)
        tried.assert_called_once_with(MEDIA, 1000, 1000)
        self.assertEqual(config['storage']['media_hdd'], media_record())

    def test_a_folder_that_cannot_be_used_stops_setup_with_the_reason(self):
        def tried(export, uid, gid):
            raise nfs.Refused(f'File locks do not work on {export}. On the NAS, switch on NFS version 4')

        config = {'network_storage': {'wanted': True}}
        with self.assertRaisesRegex(nfs.Refused, 'File locks do not work .* On the NAS, switch on'):
            self.step(config, [NAS, '1', '2'], tried=tried)
        self.assertNotIn('storage', config)  # nothing kept as a drive
        self.assertNotIn('photos', config)

    def test_a_nas_that_stopped_answering_says_what_to_check(self):
        saved = {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS, 'media': '/volume1/media'}}
        with self.assertRaisesRegex(
            RuntimeError, f'Nothing answers NFS at {NAS} .port 2049.: check that the NAS is on'
        ):
            self.step({'network_storage': saved}, interactive=False, answers=False)

    def test_nobody_at_the_terminal_and_nothing_chosen_yet(self):
        saved = {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS}}
        with self.assertRaisesRegex(
            RuntimeError, 'No shared folder is chosen yet; run  sudo bash install.sh  in a terminal'
        ):
            self.step({'network_storage': saved}, interactive=False)

    def test_one_folder_cannot_be_both_drives_nor_one_inside_the_other(self):
        for backup in ('/volume1/media', '/volume1/media/backup', '/volume1'):
            saved = {'wanted': True, 'kind': 'nfs', 'nfs': {'server': NAS, 'media': '/volume1/media', 'backup': backup}}
            config = {'network_storage': saved}
            with (
                self.subTest(backup=backup),
                self.assertRaisesRegex(RuntimeError, 'must be two separate shared folders'),
            ):
                self.step(config, interactive=False)
            self.assertNotIn('storage', config)
        nas.apart({'media': MEDIA, 'backup': f'{NAS}:/volume1/media2'})  # a name that only starts the same

    def test_a_folder_too_small_for_the_media_drive_is_refused(self):
        smallest, _ = storage.sizes('media_hdd')
        config = {}
        found = {'fstype': 'nfs4', 'size': (smallest - 1) * GB, 'free': GB}
        with self.assertRaisesRegex(RuntimeError, f'too small for the media drive: it needs at least {smallest} GB'):
            nas.keep_folder(config, 'media', MEDIA, found)
        self.assertEqual(config.get('storage', {}).get('media_hdd', {}), {})

    def test_a_drive_already_chosen_is_never_swapped_by_itself(self):
        found = {'fstype': 'nfs4', 'size': 2000 * GB, 'free': GB}
        config = {'storage': {'media_hdd': {'uuid': 'abcd-1234', 'fstype': 'ext4'}}}
        with self.assertRaisesRegex(RuntimeError, 'already has a media drive .UUID abcd-1234.*never swaps it'):
            nas.keep_folder(config, 'media', MEDIA, found)
        self.assertEqual(config['storage']['media_hdd'], {'uuid': 'abcd-1234', 'fstype': 'ext4'})
        config = {'photos': {'external_backup_uuid': 'usb-00000001', 'external_backup_fstype': 'exfat'}}
        with self.assertRaisesRegex(RuntimeError, 'already has a backup drive'):
            nas.keep_folder(config, 'backup', BACKUP, found)
        self.assertEqual(config['photos']['external_backup_uuid'], 'usb-00000001')


class DrivesStepTests(unittest.TestCase):
    """What the drives step does with a media drive that is a shared folder."""

    def config(self):
        return {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/mnt/nekoserver-media',
            'storage': {'data_ssd': {'uuid': 'abcd-1234', 'fstype': 'ext4'}, 'media_hdd': media_record()},
        }

    def test_the_apps_ssd_is_never_a_shared_folder(self):
        with self.assertRaisesRegex(RuntimeError, 'The SSD for the apps cannot be a folder shared over NFS'):
            storage.shared_folder(media_record(), 'data_ssd')
        config = self.config()
        config['storage']['data_ssd'] = media_record()
        with self.assertRaisesRegex(RuntimeError, 'cannot be a folder shared over NFS'):
            storage.resolve_storage_identity(config, {'blockdevices': []}, apply=True)
        self.assertEqual(storage.shared_folder(media_record(), 'media_hdd'), MEDIA)
        self.assertIsNone(storage.shared_folder({'uuid': 'abcd-1234', 'fstype': 'ext4'}, 'data_ssd'))

    def test_no_disk_is_looked_for_behind_a_shared_folder(self):
        inventory = {
            'blockdevices': [
                {'name': 'sda', 'type': 'disk', 'children': [{'name': 'sda1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'os-1', 'mountpoints': ['/']}]},
                {'name': 'sdb', 'type': 'disk', 'tran': 'usb', 'size': '232.9G', 'children': [{'name': 'sdb1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'abcd-1234', 'size': '232.9G'}]},
            ]
        }  # fmt: skip
        config = self.config()
        found = storage.resolve_storage_identity(config, inventory, apply=True, interactive=False)
        self.assertEqual(found['media_hdd'], media_record())
        specs = storage.mount_specs(config)
        self.assertEqual(specs[1], ('media_hdd', MEDIA_ID, 'nfs4', '/mnt/nekoserver-media', None))
        self.assertEqual(storage.shared_folders(config), {MEDIA_ID: MEDIA})

    def test_a_disk_may_still_not_be_nfs_typed(self):
        config = self.config()
        config['storage']['media_hdd'] = {'uuid': 'efgh-5678', 'fstype': 'nfs4'}
        with self.assertRaisesRegex(RuntimeError, 'fstype must be ext4, xfs or btrfs'):
            storage.mount_specs(config)

    def fstab(self, before):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fstab'
            path.write_text(before)
            notes = []
            specs = [
                ('data_ssd', 'abcd-1234', 'ext4', '/srv/nekoserver', None),
                ('media_hdd', MEDIA_ID, 'nfs4', '/mnt/nekoserver-media', None),
            ]
            changed = storage.install_fstab(specs, path, notes.append, frozenset(), {MEDIA_ID: MEDIA})
            return changed, path.read_text().splitlines(), notes

    SSD = 'UUID=abcd-1234 /srv/nekoserver ext4 defaults,nofail,x-systemd.device-timeout=90s 0 2'
    LINE = nfs.fstab_line(MEDIA, '/mnt/nekoserver-media', 'nfs4')

    def test_its_line_is_written_beside_the_ssds(self):
        changed, lines, _ = self.fstab('UUID=os / ext4 defaults 0 1\n')
        self.assertTrue(changed)
        self.assertEqual(lines, ['UUID=os / ext4 defaults 0 1', self.SSD, self.LINE])
        # And left alone the next time.
        changed, lines, _ = self.fstab('\n'.join(lines) + '\n')
        self.assertFalse(changed)

    def test_a_line_of_its_own_follows_a_new_address_or_version(self):
        old = nfs.fstab_line(f'{NAS}:/volume1/films', '/mnt/nekoserver-media', 'nfs')
        changed, lines, _ = self.fstab(f'{self.SSD}\n{old}\n')
        self.assertTrue(changed)
        self.assertEqual(lines, [self.SSD, self.LINE])

    def test_the_line_of_a_drive_given_up_for_the_nas_is_replaced_and_said(self):
        hdd = 'UUID=efgh-5678 /mnt/nekoserver-media ext4 defaults,nofail,x-systemd.device-timeout=90s 0 2'
        changed, lines, notes = self.fstab(f'{self.SSD}\n{hdd}\n')
        self.assertEqual(lines, [self.SSD, self.LINE])
        self.assertIn(f'is replaced by one for the shared folder {MEDIA}', notes[0])
        # Still connected: setup never takes a live drive's line away.
        with self.assertRaisesRegex(RuntimeError, 'already has a line for /mnt/nekoserver-media'):
            storage.shared_folder_line([self.SSD, hdd], '/mnt/nekoserver-media', self.LINE, connected=lambda u: True)

    def test_a_line_someone_else_wrote_for_the_folder_stops_setup(self):
        theirs = f'{MEDIA} /mnt/nekoserver-media nfs defaults 0 0'
        with self.assertRaisesRegex(RuntimeError, 'which NekoServer did not write for the shared folder'):
            self.fstab(f'{self.SSD}\n{theirs}\n')

    def test_what_is_mounted_is_checked_by_its_address(self):
        with mock.patch.object(storage, 'findmnt', return_value=f'nfs4 {MEDIA}'):
            self.assertEqual(storage.mounted_filesystem('/mnt/nekoserver-media'), (MEDIA_ID, 'nfs4'))
        with mock.patch.object(storage, 'findmnt', return_value=f'nfs4 {BACKUP}'):
            self.assertNotEqual(storage.mounted_filesystem('/mnt/nekoserver-media'), (MEDIA_ID, 'nfs4'))
        with mock.patch.object(neko_common, 'findmnt', return_value=f'nfs4 {MEDIA}'):
            self.assertEqual(neko_common.mounted_identity('/mnt/nekoserver-media'), (MEDIA_ID, 'nfs4', ''))
            self.assertTrue(neko_common.identity_matches('/mnt/nekoserver-media', media_record()))

    def test_the_checkup_sees_a_mounted_shared_folder_as_the_drive_it_is(self):
        import status_checks

        with mock.patch.object(status_checks, 'findmnt', return_value=f'nfs4 {MEDIA}'):
            self.assertEqual(
                status_checks.mount_info('/mnt/nekoserver-media', MEDIA_ID, 'nfs4'),
                {'status': 'mounted', 'source': MEDIA},
            )
            self.assertEqual(
                status_checks.mount_info('/mnt/nekoserver-media', nfs.identity(BACKUP), 'nfs4'),
                {'status': 'wrong_drive'},
            )
        with mock.patch.object(status_checks, 'findmnt', return_value='autofs systemd-1'):
            self.assertEqual(status_checks.mount_info('/mnt/nekoserver-media', MEDIA_ID, 'nfs4'), {'status': 'missing'})

    def test_a_folder_not_opened_since_the_start_is_opened_then_checked(self):
        # After a restart only systemd's automount holds the folder until it is used.
        states = iter(['systemd-1', 'autofs'])

        def findmnt(columns, target, **_):
            return next(states)

        with (
            mock.patch.object(storage, 'findmnt', side_effect=findmnt),
            mock.patch.object(storage.os, 'listdir') as opened,
            mock.patch.object(storage, 'mounted_filesystem', return_value=(MEDIA_ID, 'nfs4')),
            mock.patch.object(storage, 'mounted_subvolume', return_value=None),
            mock.patch.object(storage, 'call') as called,
            mock.patch.object(storage.Path, 'mkdir'),
        ):
            storage.mount_and_check(MEDIA_ID, '/mnt/nekoserver-media', None, 'nfs4')
        opened.assert_called_once_with('/mnt/nekoserver-media')
        called.assert_not_called()  # systemd mounts it, with the line in /etc/fstab

    def test_a_nas_that_is_off_is_said_when_the_folder_is_opened(self):
        with mock.patch.object(storage.os, 'listdir', side_effect=OSError(112, 'Host is down')):
            with self.assertRaisesRegex(RuntimeError, 'could not be mounted .Host is down.: check that the NAS is on'):
                storage.open_shared_folder('/mnt/nekoserver-media')


class WeeklyCopyTests(unittest.TestCase):
    def config(self):
        return {
            'data_mount': '/srv/nekoserver',
            'media_mount': '/mnt/nekoserver-media',
            'storage': {'data_ssd': {'uuid': 'abcd-1234', 'fstype': 'ext4'}, 'media_hdd': media_record()},
            'photos': {
                'external_backup_uuid': nfs.identity(BACKUP),
                'external_backup_fstype': 'nfs4',
                'external_backup_network': {'transport': 'nfs', 'export': BACKUP},
            },
        }

    def test_the_backup_folder_needs_no_disk_and_is_never_the_media_folder(self):
        config = self.config()
        self.assertEqual(photo_backup.shared_backup_folder(config), BACKUP)
        with mock.patch('modules.storage.inventory_now', side_effect=AssertionError('no disk to list')):
            photo_backup.check_drive(config, nfs.identity(BACKUP), 'nfs4')
        same = self.config()
        same['photos'].update(
            external_backup_uuid=MEDIA_ID, external_backup_network={'transport': 'nfs', 'export': MEDIA}
        )
        with self.assertRaisesRegex(RuntimeError, 'are the same shared folder'):
            photo_backup.shared_backup_folder(same)
        usb = {'photos': {'external_backup_uuid': 'usb-00000001', 'external_backup_fstype': 'exfat'}}
        self.assertIsNone(photo_backup.shared_backup_folder(usb))

    def test_the_weekly_copy_is_told_the_folders_address(self):
        with mock.patch.object(native_runtime, 'saved_paths', return_value={}):
            found = native_runtime.storage_config(self.config())
            self.assertEqual(found['backup_drive'], {'export': BACKUP})
            self.assertEqual(found['storage']['media_hdd'], media_record())
            lun = self.config()
            lun['photos']['external_backup_network'] = {'transport': 'iscsi', 'wwn': '0x6001405abc'}
            self.assertEqual(native_runtime.storage_config(lun)['backup_drive'], {'wwn': '0x6001405abc'})

    def test_drive_return_waits_for_no_device_behind_a_shared_folder(self):
        run = mock.Mock(return_value=done(out='dev-disk-by\\x2duuid-abcd\\x2d1234.device\n'))
        self.assertEqual(server_os.drive_devices(self.config(), run), ['dev-disk-by\\x2duuid-abcd\\x2d1234.device'])
        run.assert_called_once()
        self.assertTrue(server_os.on_a_nas(self.config()))  # the NAS check watches it

    def test_the_recovery_menu_opens_the_backup_folder_by_its_address(self):
        config = {**self.config(), 'external_backup_mount': '/mnt/nekoserver-photos'}
        usb = recovery_wizard.Usb(config)
        self.assertEqual(usb.export, BACKUP)
        mounts = []

        def run(argv, **_):
            mounts.append(argv)
            return done()

        identity = (nfs.identity(BACKUP), 'nfs4', '')
        with (
            mock.patch.object(recovery_wizard, 'findmnt', return_value=None),
            mock.patch.object(recovery_wizard.subprocess, 'run', side_effect=run),
            mock.patch.object(recovery_wizard, 'mounted_identity', return_value=identity),
            mock.patch.object(recovery_wizard.Path, 'mkdir'),
        ):
            self.assertEqual(usb.open(), Path('/mnt/nekoserver-photos'))
        self.assertEqual(
            mounts, [['mount', '-t', 'nfs', '-o', 'nfsvers=4,hard,nodev,nosuid,ro', BACKUP, '/mnt/nekoserver-photos']]
        )
        self.assertTrue(usb.mounted_here)
        # Another folder mounted there is never taken for it.
        other = recovery_wizard.Usb(config)
        with (
            mock.patch.object(recovery_wizard, 'findmnt', return_value='/mnt/nekoserver-photos'),
            mock.patch.object(recovery_wizard, 'mounted_identity', return_value=(MEDIA_ID, 'nfs4', '')),
        ):
            self.assertIsNone(other.open())

    def test_a_shared_folder_that_stopped_taking_writes_is_the_nas_to_check(self):
        said = []
        config = self.config()
        rows = [('the media drive', '/mnt/nekoserver-media', config['storage']['media_hdd'])]
        with (
            mock.patch.object(recovery_wizard, 'drive_rows', return_value=rows),
            mock.patch.object(recovery_wizard.os.path, 'ismount', return_value=True),
        ):
            recovery_wizard.checked_drive(
                config,
                lambda _: 'yes',
                said.append,
                mock.Mock(side_effect=AssertionError),
                lambda f: False,
                lambda f: 0,
            )
        text = '\n'.join(said)
        self.assertIn(f'is a folder the NAS shares ({MEDIA}), and it does not take new files', text)
        self.assertIn('NekoServer never checks or repairs a shared folder from this PC', text)


if __name__ == '__main__':
    unittest.main()
