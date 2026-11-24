"""Job D (mirror_add.py): how a mirror's members, its missing drive and the new member are
read, on the stand-in /sys, /dev and runner of test_mirror_add. A member read wrongly
would lay the new drive out like the wrong disk, offer a drive too small, or tell a
restart that an add happened when it did not (or the other way round)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mirror_add
from tests.test_mirror_add import GIB, POOL, REQUEST, SHOW, UUID, Stand, by_id, md_sys, write

MOUNT = '/mnt/nekoserver-media'


class Folders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dev = by_id(self.root / 'dev', 'sdc')

    def answers(self, **more):
        out = {
            f'btrfs filesystem show --raw {UUID}': (0, SHOW),
            'btrfs filesystem show --raw': (0, SHOW),
            'findmnt': (0, f'{MOUNT}\n'),
            'btrfs replace status': (0, 'Never started\n'),
        }
        out.update(more)
        return out


class MdMembersTests(Folders):
    def test_a_faulty_member_is_never_the_one_copied(self):
        sys_block = md_sys(self.root / 'sys')
        write(sys_block / 'md0' / 'md' / 'dev-sda1' / 'state', 'faulty')
        write(sys_block / 'md0' / 'md' / 'dev-sdd1' / 'state', 'in_sync')
        array = mirror_add.md_array(sys_block, 'md0')
        self.assertEqual(array['like'], '/dev/sdb1')
        self.assertEqual(mirror_add.md_members(sys_block / 'md0' / 'md'), [('sdb1', 2048), ('sdd1', 0)])

    def test_with_no_working_member_left_there_is_nothing_to_copy(self):
        sys_block = md_sys(self.root / 'sys')
        write(sys_block / 'md0' / 'md' / 'dev-sdb1' / 'state', 'faulty')
        array = mirror_add.md_array(sys_block, 'md0')
        self.assertEqual((array['like'], array['min_bytes']), (None, None))
        self.assertIn('No drive of this mirror is left to copy from', mirror_add.why_not(array))

    def test_a_member_that_says_no_offset_or_size_counts_none(self):
        whole = md_sys(self.root / 'w', partition=False)
        (whole / 'md0' / 'md' / 'dev-sdb' / 'offset').unlink()
        self.assertEqual(mirror_add.md_array(whole, 'md0')['min_bytes'], 8 * GIB)
        part = md_sys(self.root / 'p')
        (part / 'sdb' / 'sdb1' / 'size').unlink()
        self.assertEqual(mirror_add.md_array(part, 'md0')['min_bytes'], 2048 * 512 + (1 << 20))

    def test_metadata_0_90_needs_room_past_the_data_too(self):
        end = mirror_add.md_array(md_sys(self.root / 'e', partition=False, metadata='0.90'), 'md0')
        self.assertEqual(end['min_bytes'], 264192 * 512 + 8 * GIB + (4 << 20))

    def test_every_kind_of_rebuild_counts_as_one(self):
        for action in ('recover', 'resync', 'repair', 'reshape'):
            with self.subTest(action=action):
                self.assertTrue(mirror_add.md_array(md_sys(self.root / action, action=action), 'md0')['rebuilding'])

    def test_a_degraded_md_array_is_listed(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev, answers={'btrfs': (1, '')})
        self.assertEqual([a['id'] for a in mirror_add.degraded(stand.run, self.root / 'sys')], ['md-md0'])

    def test_a_mirror_that_does_not_say_its_size_is_offered_nothing(self):
        array = dict(mirror_add.md_array(md_sys(self.root / 'sys'), 'md0'), min_bytes=None)
        rows = mirror_add.offers([{'by_id': 'big', 'size_bytes': 16 * GIB}], [array], lambda d, m: None)
        self.assertEqual(
            rows,
            {
                'md-md0': [
                    {'by_id': 'big', 'ok': False, 'words': 'The mirror does not say how much room its drives need.'}
                ]
            },
        )
        self.assertIn('No drive of this mirror is left to copy from', mirror_add.why_not(array))

    def test_the_steps_the_page_shows(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev)
        mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(stand.steps, [('erase', None), ('add', None)])

    def test_after_a_power_cut_an_add_that_happened_is_a_power_cut_that_was_finished(self):
        sys_block = md_sys(self.root / 'sys')
        write(sys_block / 'md0' / 'md' / 'dev-sdc1' / 'state', 'spare')
        cut = {'phase': 'add', 'request': REQUEST, 'undo': {'member': 'ata-NEW_S9-part1'}}
        after = mirror_add.resume(cut, Stand(sys_block, self.dev).ctx)
        self.assertEqual((after['state'], after['power_cut']), ('resumed', True))

    def test_an_md_journal_is_never_finished_as_a_pool(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev)
        done = mirror_add.finish({'phase': 'add', 'request': REQUEST}, stand.ctx)
        self.assertEqual(done['state'], 'failed')
        self.assertIn('The pool is not open', done['message'])
        self.assertEqual(stand.asked, [])

    def test_the_folders_of_a_real_server(self):
        ctx = mirror_add.Context(run=None, erase=None, refused=None, journal=None, progress=None, clear=None)
        self.assertEqual((ctx.sys_block, ctx.dev), ('/sys/block', '/dev'))


class BtrfsPoolTests(Folders):
    def pool(self, show, **more):
        stand = Stand(
            '/nonexistent', self.dev, answers=self.answers(**{f'btrfs filesystem show --raw {UUID}': (0, show)}, **more)
        )
        return mirror_add.btrfs_pool(stand.run, UUID), stand

    def test_a_missing_device_however_btrfs_names_it(self):
        for path in ('<missing disk>', '/dev/sdd MISSING'):
            with self.subTest(path=path):
                show = SHOW.replace('<missing disk> MISSING', path)
                self.assertEqual(self.pool(show)[0]['devid'], 2)

    def test_the_first_device_still_there_is_the_one_copied(self):
        show = SHOW + '\tdevid    3 size 17179869184 used 2000 path /dev/sde\n'
        pool, stand = self.pool(show)
        self.assertEqual(pool['like'], '/dev/sdb')
        self.assertIn(f'findmnt -n -o TARGET --source UUID={UUID}', stand.asked)
        self.assertIn(f'btrfs replace status -1 {MOUNT}', stand.asked)

    def test_a_findmnt_that_failed_is_no_mount_whatever_it_said(self):
        pool, _ = self.pool(SHOW, findmnt=(1, 'findmnt: can not find UUID\n'))
        self.assertIsNone(pool['mount'])


class BtrfsMemberTests(Folders):
    ADDED = SHOW.replace('path <missing disk> MISSING', 'path /dev/sdc')

    def test_after_a_power_cut_the_pool_is_asked_whether_it_has_the_new_drive(self):
        cut = {'phase': 'add', 'request': POOL, 'undo': {'member': 'ata-NEW_S9'}}
        for show, rc, state in ((self.ADDED, 0, 'resumed'), (SHOW, 0, 'undone'), (self.ADDED, 1, 'undone')):
            with self.subTest(state=state, rc=rc):
                answers = self.answers(**{f'btrfs filesystem show --raw {UUID}': (rc, show)})
                stand = Stand('/nonexistent', self.dev, answers=answers)
                self.assertEqual(mirror_add.resume(cut, stand.ctx)['state'], state)
                self.assertIn(f'btrfs filesystem show --raw {UUID}', stand.asked)

    def test_a_device_whose_name_only_begins_with_the_new_ones_is_not_it(self):
        cut = {'phase': 'add', 'request': POOL, 'undo': {'member': 'ata-NEW_S9'}}
        show = SHOW.replace('path <missing disk> MISSING', 'path /dev/sdc1')
        stand = Stand(
            '/nonexistent', self.dev, answers=self.answers(**{f'btrfs filesystem show --raw {UUID}': (0, show)})
        )
        self.assertEqual(mirror_add.resume(cut, stand.ctx)['state'], 'undone')

    def test_a_balance_that_fails_is_said_after_the_drive_is_in(self):
        statuses = [(0, 'Never started\n'), (0, '50.0% done\n'), (0, 'Started on 1.Dec, finished on 1.Dec\n')]
        balance = (1, 'ERROR: error during balancing\nsecond line\nThere may be more info in syslog\n')
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(**{'btrfs replace status': statuses, 'btrfs balance': balance}),
        )
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(set(result), {'state', 'message', 'advice'})
        self.assertEqual(result['state'], 'done')
        self.assertIn('did not finish', result['message'])
        self.assertEqual(result['advice'], 'There may be more info in syslog')
        self.assertIn(('rebuild', 100.0), stand.steps)
        self.assertEqual(stand.cleared, 1)
        # Every look at the replace asks for one line, never a status that keeps printing.
        self.assertEqual(stand.asked.count(f'btrfs replace status -1 {MOUNT}'), 3)

    def test_a_balance_that_works_ends_with_no_advice(self):
        statuses = [(0, 'Never started\n'), (0, 'Started on 1.Dec, finished on 1.Dec\n')]
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**{'btrfs replace status': statuses}))
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(set(result), {'state', 'message', 'advice'})
        self.assertEqual((result['state'], result['advice']), ('done', None))
        self.assertIn('on both drives again', result['message'])

    def test_a_replace_btrfs_refuses_erases_but_changes_nothing_in_the_pool(self):
        start = (1, 'ERROR: x\nsecond line\nERROR: target device smaller than source device\n')
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**{'btrfs replace start': start}))
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(
            result,
            {
                'state': 'failed',
                'message': 'The pool did not take the new drive, so nothing was changed in it. The new drive was erased.',
                'advice': 'ERROR: target device smaller than source device',
            },
        )
        self.assertEqual(stand.phases, [('erase', False), ('add', False)])
        self.assertEqual(stand.cleared, 1)
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**{'btrfs replace start': (1, '')}))
        self.assertIsNone(mirror_add.run_job(POOL, stand.ctx)['advice'])

    def test_a_replace_that_says_nothing_is_said_so(self):
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(**{'btrfs replace status': [(0, 'Never started\n'), (1, '')]}),
        )
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertTrue(result['advice'].endswith('Btrfs said: nothing'), result['advice'])


class ResultFieldsTests(Folders):
    """Each answer has the fields the page reads, and no others."""

    def test_every_answer_has_its_fields(self):
        fields = {'state', 'message', 'advice'}
        md = md_sys(self.root / 'sys')
        refused = mirror_add.run_job({**REQUEST, 'array': 'md-md9'}, Stand(md, self.dev).ctx)
        self.assertEqual(set(refused), fields)
        added = mirror_add.run_job(REQUEST, Stand(md, self.dev).ctx)
        self.assertEqual(set(added), fields)
        (self.dev / 'disk' / 'by-id' / 'ata-NEW_S9-part1').unlink()
        unnamed = mirror_add.run_job(REQUEST, Stand(md, self.dev).ctx)
        self.assertEqual(set(unnamed), fields)
        not_open = mirror_add.finish({'request': REQUEST}, Stand(md, self.dev).ctx)
        self.assertEqual(set(not_open), fields)
        cut = {'phase': 'add', 'request': REQUEST, 'undo': {'member': 'ata-NEW_S9-part1'}}
        undone = mirror_add.resume(cut, Stand(md, self.dev).ctx)
        self.assertEqual(set(undone), fields | {'power_cut'})
        self.assertIsNone(undone['advice'])

    def test_a_failed_md_add_that_says_nothing_gives_no_advice(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev, answers={'mdadm': (1, '')})
        self.assertIsNone(mirror_add.run_job(REQUEST, stand.ctx)['advice'])

    def test_a_resumed_add_has_its_fields(self):
        sys_block = md_sys(self.root / 'sys')
        write(sys_block / 'md0' / 'md' / 'dev-sdc1' / 'state', 'spare')
        cut = {'phase': 'add', 'request': REQUEST, 'undo': {'member': 'ata-NEW_S9-part1'}}
        after = mirror_add.resume(cut, Stand(sys_block, self.dev).ctx)
        self.assertEqual(set(after), {'state', 'message', 'advice', 'power_cut'})
        self.assertIn('It is in the mirror', after['message'])


if __name__ == '__main__':
    unittest.main()
