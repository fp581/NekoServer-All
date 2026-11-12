"""0.50.0, item 5: everything connected to the PC is shown before the first drive question;
each role is chosen by a row's number, with nothing typed back; and every
later message names the drive as the list did, never by its Linux name alone.

The words are held by a golden file (tests/golden/drive-questions.txt): the whole
conversation of a first run, a rerun, and a PC with no drive to use. When a change to
them is meant, write it again on purpose:

    NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_drive_table
"""

import copy
import difflib
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import drive_table  # noqa: E402
import nfs_exports  # noqa: E402
import setup_progress  # noqa: E402
from modules import photo_backup, storage  # noqa: E402

GOLDEN = ROOT / 'tests' / 'golden' / 'drive-questions.txt'
UPDATE = os.environ.get('NEKO_UPDATE_GOLDEN') == '1'


def part(name, fstype, uuid, size, label=None, mountpoint=None):
    return {
        'name': name, 'type': 'part', 'fstype': fstype, 'uuid': uuid, 'size': size, 'label': label,
        'mountpoint': mountpoint,
    }  # fmt: skip


def disk(name, size, model, tran, rota, *parts, **more):
    return {
        'name': name, 'type': 'disk', 'size': size, 'model': model, 'tran': tran, 'rota': rota,
        'children': list(parts), **more,
    }  # fmt: skip


# One of everything: the disk Linux runs from, an SSD, an HDD the desktop opened, a USB
# stick (exFAT), a USB stick too small, a new disk with nothing on it, a disk on a NAS
# (iSCSI), a second NVMe drive, a Windows disk; and what is no drive at all.
INVENTORY = {
    'blockdevices': [
        {'name': 'loop0', 'type': 'loop', 'size': '73.9M', 'fstype': 'squashfs', 'mountpoint': '/snap/core/1'},
        {'name': 'sr0', 'type': 'rom', 'size': '1024M'},
        disk(
            'nvme0n1', '476.9G', 'WD SN770', 'nvme', False,
            part('nvme0n1p1', 'vfat', 'EF00-0001', '512M', mountpoint='/boot/efi'),
            part('nvme0n1p2', 'ext4', 'root-0001', '476.4G', mountpoint='/'),
        ),
        disk('sda', '465.8G', 'Samsung SSD 870', 'sata', False, part('sda1', 'ext4', 'ssd-0001', '465.8G', 'apps')),
        disk(
            'sdb', '1.8T', 'WDC WD20EZRZ', 'sata', True,
            part('sdb1', 'ext4', 'hdd-0001', '1.8T', 'media', '/run/media/neko/media'),
        ),
        disk('sdc', '28.7G', 'SanDisk Ultra', 'usb', True, part('sdc1', 'exfat', 'USB0-0001', '28.7G', 'BACKUP'), rm=True),
        disk('sdd', '14.9G', 'Cruzer Blade', 'usb', True, part('sdd1', 'ext4', 'small-0001', '14.9G', 'stick'), rm=True),
        {'name': 'sde', 'type': 'disk', 'size': '931.5G', 'model': 'ST1000DM010', 'tran': 'sata', 'rota': True},
        {
            'name': 'sdf', 'type': 'disk', 'size': '3.6T', 'model': 'Synology LUN', 'tran': 'iscsi', 'rota': True,
            'wwn': '0x6001405abc', 'fstype': 'ext4', 'uuid': 'nas-0001', 'label': 'nas-media',
        },
        disk('nvme1n1', '232.9G', 'KINGSTON SNV2S250G', 'nvme', False, part('nvme1n1p1', 'xfs', 'nvme-0002', '232.9G')),
        disk('sdg', '238.5G', 'Crucial MX500', 'sata', False, part('sdg1', 'ntfs', 'WIN0-0001', '238.5G', 'Windows')),
        {'name': 'zram0', 'type': 'disk', 'size': '8G', 'mountpoint': '[SWAP]'},
    ]
}  # fmt: skip
FOLDERS = {
    'data_mount': '/nonexistent/nekoserver-test',
    'media_mount': '/mnt/nekoserver-media',
    'external_backup_mount': '/mnt/nekoserver-photos',
}
# What looking into each drive finds: (how full in percent, whether it holds files).
INSIDE = {'sda1': (1, None), 'sdb1': (40, True), 'sdc1': (None, None), 'sdd1': (3, None), 'nvme1n1p1': (None, None)}


def looked(name, uuid, fstype, mountpoint):
    return INSIDE.get(name, (None, None))


class DriveListCase(unittest.TestCase):
    def setUp(self):
        drive_table.forget_shown()
        for target, stand_in in ((drive_table, looked), (storage, lambda name: None)):
            name = 'look_inside' if target is drive_table else 'device_sub_uuid'
            patch = mock.patch.object(target, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)
        # The disk Linux runs from, the same on every machine: ext4 with 300 GB free.
        for name, value in (('system_filesystem', ('root-0001', 'ext4')), ('system_free_gb', 300)):
            patch = mock.patch.object(storage, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def table(self, config=None, inventory=None):
        return drive_table.rows(inventory or INVENTORY, config or dict(FOLDERS))


class TableTests(DriveListCase):
    def test_every_drive_is_a_row_with_its_number_and_what_it_is(self):
        table = self.table()
        self.assertEqual(
            [(row['number'], row['name'], drive_table.words(row)) for row in table],
            [
                (1, 'nvme0n1', '477 GB NVMe SSD, WD SN770'),
                (2, 'sda1', '466 GB SSD, Samsung SSD 870, named "apps"'),
                (3, 'sdb1', '1.8 TB HDD, WDC WD20EZRZ, named "media"'),
                (4, 'sdc1', '29 GB USB drive, SanDisk Ultra, named "BACKUP"'),
                (5, 'sdd1', '15 GB USB drive, Cruzer Blade, named "stick"'),
                (6, 'sde', '932 GB HDD, ST1000DM010'),
                (7, 'sdf', '3.6 TB network disk (iSCSI), Synology LUN, named "nas-media"'),
                (8, 'nvme1n1p1', '233 GB NVMe SSD, KINGSTON SNV2S250G'),
                (9, 'sdg1', '238 GB SSD, Crucial MX500, named "Windows"'),
            ],
        )
        # The filesystem, its UUID, where it is open and how full it is.
        inside = {row['name']: drive_table.inside(row) for row in table}
        self.assertEqual(inside['sda1'], 'ext4, UUID ssd-0001; not open; about 1 % full')
        self.assertEqual(
            inside['sdb1'], 'ext4, UUID hdd-0001; open at /run/media/neko/media; about 40 % full, holds files'
        )
        self.assertEqual(inside['sdf'], 'ext4, UUID nas-0001; the NAS disk 0x6001405abc; not open')
        self.assertEqual(inside['sdg1'], 'NTFS (Windows), UUID WIN0-0001; not open')
        self.assertEqual((inside['nvme0n1'], inside['sde']), ('', ''))

    def test_each_role_says_which_drives_can_have_it_and_why_the_others_cannot(self):
        config = dict(FOLDERS)
        table = self.table(config)

        def can(key):
            return [row['number'] for row in drive_table.usable(table, key, config)]

        # The disk Linux runs from (1) can hold the apps or the media, never the offline backup.
        self.assertEqual(can('data_ssd'), [1, 2, 3, 8])
        self.assertEqual(can('media_hdd'), [1, 2, 3, 7, 8])
        # A disk on the NAS may hold the backup; the 15 GB stick (5) is too small for it (gate run B4).
        self.assertEqual(can('usb'), [2, 3, 4, 7, 8])
        self.assertEqual(drive_table.verdict(table[0], 'usb', config), drive_table.SYSTEM_NOT_OFFLINE)
        why = {row['number']: drive_table.verdict(row, 'data_ssd', config) for row in table}
        self.assertEqual(why[1], '')
        self.assertTrue(why[4].startswith('it is formatted exFAT, which Linux apps can'), why[4])
        self.assertEqual(
            why[5], 'it is too small: the app SSD needs a drive of at least 22 GB (20 GB free once formatted)'
        )
        self.assertTrue(why[6].startswith('it has no filesystem yet. To use it, format it as ext4 yourself'), why[6])
        self.assertIn('sudo mkfs.ext4 /dev/sde  (this erases what is on it; setup never formats a drive)', why[6])
        self.assertTrue(why[7].startswith('it is on the NAS, and the app SSD must be connected'))
        self.assertIn('copy its files elsewhere first', why[9])
        # The drive just chosen for the apps may hold the media and backups too (0.50.0: one
        # drive works); it is never the offline backup as well.
        config['storage'] = {'data_ssd': {'uuid': 'ssd-0001'}}
        self.assertEqual(drive_table.verdict(table[1], 'media_hdd', config), '')
        self.assertTrue(drive_table.verdict(table[1], 'usb', config))
        self.assertEqual(drive_table.verdict(table[1], 'data_ssd', config), '')
        self.assertEqual(drive_table.can_be(table[1], config), 'Chosen as the app SSD.')

    def test_a_drive_in_use_elsewhere_and_two_roles_on_one_disk_are_refused(self):
        inventory = copy.deepcopy(INVENTORY)
        sda = inventory['blockdevices'][3]
        sda['children'][0]['mountpoint'] = '/mnt/films'
        sda['children'].append(part('sda2', 'ext4', 'ssd-0002', '100G'))
        config = {**FOLDERS, 'storage': {'media_hdd': {'uuid': 'ssd-0002'}}}
        table = self.table(config, inventory)
        named = {row['name']: row for row in table}
        self.assertEqual(
            drive_table.verdict(named['sda1'], 'data_ssd', config),
            'it is in use at /mnt/films. To use it, unmount it first (sudo umount /mnt/films, and take out its '
            'line in /etc/fstab if you added one)',
        )
        named['sda1']['mountpoint'] = None
        self.assertEqual(
            drive_table.verdict(named['sda1'], 'data_ssd', config), 'it is on the same disk as the media HDD'
        )

    def test_a_folder_the_nas_shares_is_a_row_with_its_role(self):
        export = 'nas.lan:/volume1/media'
        config = {**FOLDERS, 'storage': {'media_hdd': nfs_exports.record_for(export, 'nfs4')}}
        row = self.table(config)[-1]
        self.assertEqual((row['number'], row['name'], row['type']), (10, export, 'network folder (NFS)'))
        self.assertEqual(drive_table.brief(row), 'drive 10 (nas.lan:/volume1/media: a folder the NAS shares (NFS))')
        self.assertEqual(drive_table.can_be(row, config), 'Chosen as the media HDD.')
        self.assertEqual(drive_table.verdict(row, 'usb', config), 'it is already the media HDD')

    def test_what_is_no_drive_is_left_out_and_a_disk_of_reserved_partitions_is_never_one_to_format(self):
        inventory = {
            'blockdevices': [
                {'name': 'mmcblk0boot0', 'type': 'disk', 'size': '4M'},
                {'name': 'sdx', 'type': 'disk', 'size': '0B'},
                disk('sdh', '1T', 'X', 'sata', True, {'name': 'sdh1', 'type': 'part', 'size': '16M', 'fstype': None}),
                disk('sdi', '1T', 'Y', 'sata', True, part('sdi1', 'swap', 'swap-1', '8G')),
            ]
        }
        table = self.table(inventory=inventory)
        self.assertEqual([row['name'] for row in table], ['sdh', 'sdi'])
        for row in table:
            self.assertEqual(row['problem'], 'nothing on it is a filesystem Linux apps can keep files on')

    def test_a_drive_not_open_is_looked_into_only_through_its_own_header(self):
        """How full an ext4 drive is comes from dumpe2fs -h (read-only), and only when the
        device is still the one with that UUID; nothing is mounted to look."""
        ran = []

        def run(argv, **_):
            ran.append(argv)
            return SimpleNamespace(stdout='Block count:              1000\nFree blocks:              750\n')

        self.assertEqual(drive_table.ext_fullness('sdb1', run), 25)
        self.assertEqual(ran, [['dumpe2fs', '-h', '/dev/sdb1']])
        self.assertIsNone(drive_table.ext_fullness('sdb1', lambda argv, **_: SimpleNamespace(stdout='')))
        self.assertFalse(drive_table.still_that_drive('sdb1', 'no-such-uuid'))
        self.assertFalse(drive_table.still_that_drive('sdb1', ''))
        source = (ROOT / 'drive_table.py').read_text()
        self.assertNotIn("'mount'", source)
        self.assertNotIn('mkfs', source.replace('sudo mkfs.ext4 /dev/{name}', ''))  # only as words to the person

    def test_lsblk_is_asked_whether_a_disk_spins(self):
        with mock.patch.object(storage, 'call', return_value='{"blockdevices": []}') as call:
            storage.inventory_now()
        self.assertIn('ROTA', call.call_args[0][0][3].split(','))


class Conversation:
    """What the terminal shows: every line setup prints and every question with its answer."""

    def __init__(self, answers):
        self.answers = iter(answers)
        self.lines = []
        self.pages = []

    def out(self, line):
        self.lines.append(str(line))

    def ask(self, prompt=''):
        answer = next(self.answers)
        self.pages.append(setup_progress.page_shown())
        self.lines.append(f'{prompt}[{answer}]')
        return answer

    def heading(self, text):
        self.lines += ['', f'===== {text} =====']


class QuestionTests(DriveListCase):
    def first_run(self, talk):
        config = dict(FOLDERS)
        storage.resolve_storage_identity(config, INVENTORY, True, True, talk.ask, talk.out)
        return config

    def test_the_whole_conversation_is_the_golden_text(self):
        talk = Conversation(['5', '2', '3', '9', '4', '1', '1'])
        talk.heading('A first run: the list, then the app SSD, the media HDD and the USB backup drive')
        config = self.first_run(talk)
        photo_backup.select_external(config, INVENTORY, talk.ask, talk.out)
        self.assertEqual(config['storage']['data_ssd'], {'uuid': 'ssd-0001', 'fstype': 'ext4'})
        self.assertEqual(config['storage']['media_hdd'], {'uuid': 'hdd-0001', 'fstype': 'ext4'})
        self.assertEqual(config['photos']['external_backup_uuid'], 'USB0-0001')
        talk.heading('Setup run again: the drives chosen last time')
        storage.resolve_storage_identity(config, INVENTORY, True, True, talk.ask, talk.out)
        talk.heading('A PC with one drive: the disk Linux runs from holds the apps, the media and the backups')
        alone = {'blockdevices': [INVENTORY['blockdevices'][2]]}
        one = dict(FOLDERS)
        with mock.patch('builtins.input', talk.ask):
            storage.resolve_storage_identity(one, alone, True, True, talk.ask, talk.out)
        self.assertEqual(
            one['storage'],
            {
                'data_ssd': {'uuid': 'root-0001', 'fstype': 'ext4', 'folder': '/var/lib/nekoserver/apps'},
                'media_hdd': {'uuid': 'root-0001', 'fstype': 'ext4', 'folder': '/var/lib/nekoserver/media'},
            },
        )
        talk.heading('A PC with no drive that can be used')
        bare = {'blockdevices': [INVENTORY['blockdevices'][2], INVENTORY['blockdevices'][7]]}
        with self.assertRaises(RuntimeError) as caught, mock.patch.object(storage, 'system_free_gb', return_value=5):
            storage.resolve_storage_identity(dict(FOLDERS), bare, True, True, talk.ask, talk.out)
        talk.out(caught.exception)
        said = '\n'.join(talk.lines).strip() + '\n'
        if UPDATE:
            GOLDEN.write_text(said)
        kept = GOLDEN.read_text() if GOLDEN.is_file() else ''
        diff = '\n'.join(difflib.unified_diff(kept.splitlines(), said.splitlines(), 'golden', 'now', lineterm=''))
        self.assertEqual(said, kept, 'the drive questions changed:\n' + diff)

    def test_every_message_names_the_drive_as_the_list_did(self):
        talk = Conversation(['5', '2', '3'])
        config = self.first_run(talk)
        said = [line for line in talk.lines if 'drive 2' in line.lower() or 'drive 5' in line.lower()]
        self.assertIn(
            'Drive 5 (sdd1: 15 GB USB drive, Cruzer Blade, named "stick") cannot be the app SSD: it is too small: '
            'the app SSD needs a drive of at least 22 GB (20 GB free once formatted). Choose another one from the list.',
            said,
        )
        # The choice is said back in words; no name is typed (choosing changes nothing on a drive).
        self.assertIn(
            'Drive 2 (sda1: 466 GB SSD, Samsung SSD 870, named "apps") is the app SSD. '
            'Nothing on it is changed by choosing it.',
            said,
        )
        self.assertFalse([line for line in talk.lines if 'to confirm' in line])
        # A rerun: the saved choice with its number, its description and its UUID.
        rerun = Conversation([])
        storage.resolve_storage_identity(config, INVENTORY, True, True, rerun.ask, rerun.out)
        self.assertEqual(
            rerun.lines,
            [
                'Last time you chose drive 2 (sda1: 466 GB SSD, Samsung SSD 870, named "apps") as the app SSD '
                '(UUID ssd-0001). It is used again.',
                'Last time you chose drive 3 (sdb1: 1.8 TB HDD, WDC WD20EZRZ, named "media") as the media HDD '
                '(UUID hdd-0001). It is used again.',
            ],
        )
        # A later error names it the same way (a disk on the NAS saved as the app SSD).
        nas = {**FOLDERS, 'storage': {'data_ssd': {'uuid': 'nas-0001', 'fstype': 'ext4'}}}
        with self.assertRaisesRegex(RuntimeError, r'^Drive 7 \(sdf: 3\.6 TB network disk \(iSCSI\), Synology LUN'):
            storage.resolve_storage_identity(nas, INVENTORY, True, True, rerun.ask, rerun.out)
        # No message is a Linux name alone: none starts with one.
        self.assertFalse([line for line in talk.lines if line.startswith(('sd', 'nvme', '/dev/'))])

    def test_the_list_is_shown_once_before_the_first_question_and_again_only_when_it_changed(self):
        talk = Conversation(['2', 'sda1', '3', 'sdb1', '4', 'sdc1'])
        config = self.first_run(talk)
        photo_backup.select_external(config, INVENTORY, talk.ask, talk.out)
        self.assertEqual(talk.lines.count('Everything connected to this PC:'), 1)
        self.assertEqual(talk.lines[0], 'Everything connected to this PC:')
        self.assertIn(drive_table.NAMES_CHANGE, talk.lines)
        self.assertIn(drive_table.NEVER_FORMATS, talk.lines)
        # A drive plugged in since: the list is shown again.
        more = copy.deepcopy(INVENTORY)
        more['blockdevices'].append(disk('sdz', '1T', 'New', 'usb', True, part('sdz1', 'ext4', 'new-0001', '1T')))
        config['photos'] = {}
        again = Conversation(['', ''])
        photo_backup.select_external(config, more, again.ask, again.out)
        self.assertEqual(again.lines[0], 'Everything connected to this PC:')

    def test_enter_alone_takes_the_recommended_drive_or_skips_the_usb_drive(self):
        """0.50.0, the wizard (P02): every drive line comes with the recommended drive, so a
        person who changes nothing gets a sensible layout (three drives: the one every
        server before had)."""
        talk = Conversation(['', ''])
        config = self.first_run(talk)
        # The media and backups: the biggest drive in this PC (the HDD, before the bigger
        # disk on the NAS); the apps: the bigger SSD of the others.
        self.assertEqual(config['storage']['media_hdd']['uuid'], 'hdd-0001')
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-0001')
        said = '\n'.join(talk.lines)
        self.assertIn('named "apps", ext4, about 1 % full  (recommended)', said)
        self.assertIn('(the app SSD) (Enter for 2, the recommended one): []', said)
        self.assertIn('(the media HDD) (Enter for 3, the recommended one): []', said)
        best = drive_table.recommended(self.table(), dict(FOLDERS))
        self.assertEqual({key: row['number'] for key, row in best.items()}, {'data_ssd': 2, 'media_hdd': 3, 'usb': 4})
        # Where no drive can have the role there is nothing to recommend, and Enter stops.
        bare = {'blockdevices': [INVENTORY['blockdevices'][7]]}
        table = self.table(inventory=bare)
        self.assertIsNone(drive_table.recommended(table, dict(FOLDERS))['data_ssd'])
        with self.assertRaisesRegex(RuntimeError, 'No drive was chosen for the app SSD. Run  sudo bash install.sh'):
            drive_table.ask(table, 'data_ssd', dict(FOLDERS), Conversation(['']).ask, lambda line: None)
        config = {**FOLDERS, 'storage': {'data_ssd': {'uuid': 'ssd-0001'}, 'media_hdd': {'uuid': 'hdd-0001'}}}
        # The offline backup is optional: Enter skips it, whatever is recommended.
        talk = Conversation([''])
        self.assertIsNone(photo_backup.select_external(config, INVENTORY, talk.ask, talk.out))
        self.assertTrue(talk.lines[-1].endswith('(the USB backup drive) (Enter to skip for now): []'), talk.lines[-1])


class WindowFormTests(DriveListCase):
    """The setup window gets the same list as a page: a card for each drive, chosen by
    clicking (its number is the answer); nothing is typed back."""

    def test_each_question_is_a_page_of_the_same_list_and_nothing_is_typed_back(self):
        talk = Conversation(['2', '3'])
        config = dict(FOLDERS)
        storage.resolve_storage_identity(config, INVENTORY, True, True, talk.ask, talk.out)
        choose = talk.pages[0]
        self.assertEqual((choose['form'], choose['asking'], choose['skippable']), ('drives', 'choice', False))
        self.assertEqual(choose['what'], 'Choose the drive for your apps, photos and music (the app SSD).')
        self.assertTrue(choose['needs'].startswith('It needs at least 20 GB free'))
        self.assertEqual(choose['names'], drive_table.NAMES_CHANGE)
        self.assertEqual(len(choose['rows']), 9)
        self.assertEqual(
            choose['rows'][1],
            {
                'number': 2,
                'name': 'sda1',
                'what': '466 GB SSD, Samsung SSD 870, named "apps"',
                'inside': 'ext4, UUID ssd-0001; not open; about 1 % full',
                'role': '',
                'why': '',
                'caution': '',
            },
        )
        # The disk Linux runs from can be chosen, and says what that means before it is.
        self.assertEqual((choose['rows'][0]['why'], choose['rows'][0]['caution']), ('', drive_table.SYSTEM_DISK))
        self.assertEqual(len(talk.pages), 2)  # two questions, no confirmation between them
        self.assertNotIn('confirm', choose)
        # The media HDD's page: the drive just chosen says its role, and may hold both.
        second = talk.pages[1]['rows'][1]
        self.assertEqual((second['role'], second['why']), ('Chosen as the app SSD', ''))

    def test_the_usb_drive_page_can_be_skipped(self):
        talk = Conversation([''])
        config = {**FOLDERS, 'storage': {'data_ssd': {'uuid': 'ssd-0001'}, 'media_hdd': {'uuid': 'hdd-0001'}}}
        photo_backup.select_external(config, INVENTORY, talk.ask, talk.out)
        self.assertTrue(talk.pages[0]['skippable'])
        self.assertEqual(talk.pages[0]['needs'], '')


class StoragePageTests(DriveListCase):
    """0.50.0, the wizard (P02): the drives step's questions are one page in the window."""

    def context(self, **more):
        config = {**FOLDERS, 'photos': {'deploy_immich': True}, **more}
        return SimpleNamespace(config=config, log=lambda line: None)

    def test_the_page_asks_only_what_has_no_answer_yet(self):
        new = self.context().config
        self.assertEqual(
            list(storage.page_fields(new)), ['data_ssd', 'media_hdd', 'photos', 'movies', 'music', 'cloud', 'usb']
        )
        # A kind of files this server does not keep has no line; a saved answer is not asked again.
        partly = {
            **new,
            'media_apps': {'photos': False},
            'storage': {'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'}},
            'locations': {'music': 'ssd'},
        }
        self.assertEqual(list(storage.page_fields(partly)), ['media_hdd', 'movies', 'cloud', 'usb'])
        done = {
            **partly,
            'storage': {**partly['storage'], 'media_hdd': {'uuid': 'hdd-0001', 'fstype': 'ext4'}},
            'locations': {'music': 'ssd', 'cloud': 'media', 'movies': 'media'},
            'photos': {'external_backup_uuid': 'USB0-0001'},
        }
        self.assertEqual(storage.page_fields(done), {})
        # A server that is set up is not asked where its films are: they stay where they are.
        with mock.patch.object(storage, 'set_up_before', return_value=True):
            self.assertNotIn('movies', storage.page_fields(partly))
        # Nothing to ask, or nobody there: no page.
        for config, at_terminal in ((done, True), (new, False)):
            with mock.patch('sys.stdin.isatty', return_value=at_terminal):
                with storage.storage_page(SimpleNamespace(config=config), INVENTORY):
                    self.assertIsNone(setup_progress.page_shown())

    def test_every_line_is_on_the_page_and_the_windows_answers_are_not_asked_again(self):
        ctx = self.context()
        never = mock.Mock(side_effect=AssertionError('asked in the terminal'))
        said = []
        with mock.patch('sys.stdin.isatty', return_value=True), storage.storage_page(ctx, INVENTORY):
            page = setup_progress.page_shown()
            self.assertEqual((page['form'], page['title']), ('storage', storage.PAGE_TITLE))
            self.assertEqual(
                [(role['key'], role['title'], role['optional']) for role in page['roles']],
                [
                    ('data_ssd', 'NekoServer and your apps', False),
                    ('media_hdd', 'Backups', False),
                    ('usb', 'Offline backup on a removable drive (optional)', True),
                ],
            )
            # Each role's drives with why one cannot be it: the capacity is said in numbers.
            ssd = page['roles'][0]
            self.assertTrue(ssd['needs'].startswith('It needs at least 20 GB free, so a drive of 22 GB or more'))
            self.assertIn('it is too small: the app SSD needs a drive of at least 22 GB', ssd['rows'][4]['why'])
            self.assertEqual(ssd['rows'][1]['why'], '')
            self.assertEqual([place['key'] for place in page['places']], ['photos', 'movies', 'music', 'cloud'])
            # Backups are a line of their own; files kept beside their backup are told so.
            self.assertEqual(page['roles'][1]['holds'], storage.BACKUPS_HOLD)
            # Each line names the recommended place first, chosen when the page opens: nothing
            # is left for setup to decide unseen. No drive is the apps' yet, so that is the backups'.
            self.assertEqual(
                page['places'][0]['options'][:2],
                [
                    ['media', 'Recommended: same drive as the backups'],
                    ['ssd', 'Same drive as NekoServer and your apps'],
                ],
            )
            self.assertEqual(page['places'][0]['recommended'], 'media')
            self.assertEqual(
                storage.place_options('ssd', [])[0], ['ssd', 'Recommended: same drive as NekoServer and your apps']
            )
            # The photos always have a copy there; the music only where its backup is switched on.
            music_backup = ctx.config.get('music', {}).get('backup_enabled') is True
            self.assertEqual(
                [bool(place['beside_backup']) for place in page['places']], [True, False, music_backup, False]
            )
            self.assertEqual(storage.backed_up_beside({'music': {'backup_enabled': True}}), ('photos', 'music'))
            self.assertEqual(storage.backed_up_beside({}), ('photos',))
            # The two roles' drives, or a drive of their own: any other drive that could hold them.
            self.assertEqual(
                [option[0] for option in page['places'][0]['options']],
                ['media', 'ssd', 'drive:2', 'drive:3', 'drive:7', 'drive:8'],
            )
            self.assertEqual(
                page['places'][0]['options'][2][1],
                'On a drive of their own: 2 · 466 GB SSD, Samsung SSD 870, named "apps"',
            )
            self.assertIn('SMB) are not supported yet', page['network'])
            # The window sends every line with the first question's answer.
            setup_progress._page['answers'].update({'data_ssd': '2', 'media_hdd': '3', 'usb': '4', 'photos': 'media'})
            storage.resolve_storage_identity(ctx.config, INVENTORY, True, True, never, said.append)
            self.assertEqual(ctx.config['storage']['data_ssd'], {'uuid': 'ssd-0001', 'fstype': 'ext4'})
            self.assertEqual(ctx.config['storage']['media_hdd'], {'uuid': 'hdd-0001', 'fstype': 'ext4'})
            # A place named by the window is taken as it is ("media"), with no number typed.
            room = {'ssd': 400, 'media': 900}
            self.assertEqual(
                storage.asked_location(ctx, "Immich's photos", 'ssd', room, False, never, key='photos'), 'media'
            )
            photo_backup.select_external(ctx.config, INVENTORY, never, said.append)
            self.assertEqual(ctx.config['photos']['external_backup_uuid'], 'USB0-0001')
        self.assertIsNone(setup_progress.page_shown())
        self.assertIn(
            'Drive 2 (sda1: 466 GB SSD, Samsung SSD 870, named "apps") is the app SSD. '
            'Nothing on it is changed by choosing it.',
            said,
        )

    def test_an_apps_files_can_have_a_drive_of_their_own(self):
        """Any other drive that could hold them is offered beside the two roles' drives: in
        the window by its row ("drive:8"), in the terminal by a number after the two."""
        chosen = {
            'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'},
            'media_hdd': {'uuid': 'hdd-0001', 'fstype': 'ext4'},
        }
        ctx = self.context(storage=chosen, nextcloud={'enabled': False}, media_apps={'movies': False, 'music': False})
        ctx.persist_config = mock.Mock()
        said = []
        ctx.log = said.append
        with mock.patch.object(storage, 'free_gb', return_value=400):
            # The terminal: 1 and 2 are the two roles' drives, 3 onward the others (rows 7 and 8).
            storage.choose_locations(ctx, lambda prompt='': '4', True, inventory=INVENTORY)
        self.assertIn(
            '  3) a drive of their own: drive 7 (sdf: 3.6 TB network disk (iSCSI), Synology LUN, named "nas-media")',
            said,
        )
        self.assertEqual(ctx.config['locations'], {'photos': 'd3'})
        self.assertEqual(ctx.config['storage']['d3'], {'uuid': 'nvme-0002', 'fstype': 'xfs'})
        self.assertIn(
            "Immich's photos and videos: on drive 8 (nvme1n1p1: 233 GB NVMe SSD, KINGSTON SNV2S250G), a drive of their own",
            said,
        )
        # It is mounted like the two (neko_common.drives_in_use), and is no longer free for another role.
        import neko_common

        self.assertEqual(neko_common.drives_in_use(ctx.config), ['d3'])
        table = self.table(ctx.config)
        self.assertEqual(
            drive_table.verdict(table[7], 'usb', ctx.config),
            "it already holds an app's files (chosen as a drive of their own)",
        )
        # The window names the row; a drive that is one of the roles is that role, not a new name.
        again = self.context(
            storage=dict(chosen), nextcloud={'enabled': False}, media_apps={'movies': False, 'music': False}
        )
        again.persist_config = mock.Mock()
        with mock.patch.object(storage, 'free_gb', return_value=400), storage.storage_page(again, INVENTORY):
            pass
        row = next(row for row in self.table(again.config) if row['uuid'] == 'hdd-0001')
        self.assertEqual(storage.placed_on(again.config, row), 'media')

    def test_none_for_now_is_an_answer_for_the_offline_backup(self):
        """Until the wizard of 0.50.0 a skipped USB drive was asked for again by the weekly
        backup step, and waited as "needs you". Now it is a choice: saved, not asked
        again, and the weekly copy is left out."""
        ctx = self.context(storage={'data_ssd': {'uuid': 'ssd-0001'}, 'media_hdd': {'uuid': 'hdd-0001'}})
        ctx.persist_config = mock.Mock()
        said = []
        ctx.log = said.append
        with (
            mock.patch('sys.stdin.isatty', return_value=True),
            mock.patch.object(storage, 'inventory_now', return_value=INVENTORY),
            mock.patch('builtins.input', return_value=''),
        ):
            storage.choose_usb(ctx)
            self.assertIs(ctx.config['photos']['external_backup_wanted'], False)
            ctx.persist_config.assert_called_once()
            self.assertTrue(any(line.startswith('Offline backup: none, as you chose.') for line in said))
            self.assertNotIn('usb', storage.page_fields(ctx.config))
            # Not asked a second time, by this step or by the weekly backup step.
            with mock.patch('builtins.input', side_effect=AssertionError('asked again')):
                storage.choose_usb(ctx)
                step = SimpleNamespace(config=ctx.config, apply=True, log=said.append)
                with (
                    mock.patch('os.geteuid', return_value=0),
                    mock.patch.object(photo_backup, 'switch_off') as off,
                ):
                    result = photo_backup.run(step)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('Left out by choice', result['reason'])
        off.assert_called_once()
        # The dashboard's settings carry the choice (the collector shows the job as switched off).
        from modules import dashboard

        self.assertIn("clean['photos']['external_backup_wanted'] = False", (ROOT / 'modules/dashboard.py').read_text())
        self.assertTrue(callable(dashboard.run))

    def test_nothing_asks_for_a_drives_name_any_more(self):
        import neko_common

        self.assertFalse(hasattr(neko_common, 'confirmed'))
        self.assertFalse(hasattr(drive_table, 'confirm'))
        for name in ('drive_table.py', 'neko_common.py', 'modules/storage.py', 'modules/photo_backup.py'):
            self.assertNotIn('to confirm it is', (ROOT / name).read_text(), name)
        self.assertNotIn('page.confirm', (ROOT / 'setup-gui/setup.js').read_text())


if __name__ == '__main__':
    unittest.main()
