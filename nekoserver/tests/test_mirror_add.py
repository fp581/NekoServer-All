"""Job D of the Drives page (mirror_add.py, 1.0.1 contract §7): a replacement drive added
to a mirror that lost one, mdadm and Btrfs, and the boot job after a power cut.

On stand-in /sys and /dev folders and a stand-in runner that notes every command: no disk,
no mdadm, no btrfs is touched. The drive is checked and erased by storage_jobs' one entry
point, here a stand-in that notes what it was asked."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mirror_add

GIB = 1 << 30


class Refused(Exception):
    pass


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{text}\n')


def md_sys(root, degraded=1, action='idle', partition=True, metadata='1.2'):
    """md0 of two members; sdb1 still there, the other one gone."""
    md = root / 'md0' / 'md'
    write(md / 'degraded', degraded)
    write(md / 'sync_action', action)
    write(md / 'component_size', 8 * GIB // 1024)
    write(md / 'metadata_version', metadata)
    if partition:
        write(md / 'dev-sdb1' / 'state', 'in_sync')
        write(md / 'dev-sdb1' / 'offset', 2048)
        write(root / 'sdb' / 'sdb1' / 'start', 2048)
        write(root / 'sdb' / 'sdb1' / 'size', 8 * GIB // 512 + 4096)
    else:
        write(md / 'dev-sdb' / 'state', 'in_sync')
        write(md / 'dev-sdb' / 'offset', 264192)
    return root


def by_id(dev, disk):
    """/dev/disk/by-id for the new drive, as its kernel name `disk` (sdc, or sdd after a
    restart renamed it)."""
    folder = dev / 'disk' / 'by-id'
    folder.mkdir(parents=True, exist_ok=True)
    for name, to in (('ata-NEW_S9', disk), ('ata-NEW_S9-part1', f'{disk}1')):
        link = folder / name
        if link.is_symlink():
            link.unlink()
        os.symlink(f'../../{to}', link)
    return dev


class Stand:
    """storage_jobs' side: the runner, erase, journal, progress and clear."""

    def __init__(self, sys_block, dev, answers=None, refuse=None, away=None):
        self.asked = []
        # What the drive check says at each look (None: there), in turn; the folders asked.
        self.away = list(away or [])
        self.looked = []
        self.answers = answers or {}
        self.refuse = refuse
        self.journals = []
        self.steps = []
        self.cleared = 0
        self.erased = []
        self.ctx = mirror_add.Context(
            run=self.run,
            erase=self.erase,
            refused=Refused,
            journal=lambda phase, committed, undo: self.journals.append((phase, committed, undo)),
            progress=lambda step, percent=None: self.steps.append((step, percent)),
            clear=self.clear,
            sys_block=sys_block,
            dev=dev,
            sleep=self.sleep,
            away=self.drive_away,
        )
        self.slept = 0

    def drive_away(self, folder):
        self.looked.append(folder)
        return self.away.pop(0) if len(self.away) > 1 else (self.away[0] if self.away else None)

    def sleep(self, seconds):
        # A wait that never ends fails the test instead of hanging it.
        self.slept += 1
        if self.slept > 50:
            raise AssertionError('still waiting after 50 looks')

    @property
    def phases(self):
        return [(phase, committed) for phase, committed, _ in self.journals]

    def run(self, argv, timeout=None):
        said = ' '.join(argv)
        self.asked.append(said)
        if argv[:3] == ['btrfs', 'replace', 'status']:
            assert timeout, 'a look at a replace has a time limit'
        for prefix, answer in self.answers.items():
            if said.startswith(prefix):
                if isinstance(answer, list):
                    # In turn, and the last one for every look after it.
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                return answer
        return (0, '')

    def erase(self, by_id, serial, size_bytes, layout, like, minimum_bytes):
        self.erased.append((by_id, serial, size_bytes, layout, like, minimum_bytes))
        if self.refuse:
            raise Refused(self.refuse)
        return '/dev/sdc1' if like.endswith('1') else '/dev/sdc'

    def clear(self):
        self.cleared += 1


REQUEST = {
    'job': 'mirror_add',
    'array': 'md-md0',
    'disk': {'by_id': 'ata-NEW_S9', 'serial': 'S9', 'size_bytes': 16 * GIB},
    'confirm': '16 GB S9',
}


class Md(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dev = by_id(self.root / 'dev', 'sdc')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_array_says_what_the_new_drive_needs(self):
        array = mirror_add.md_array(md_sys(self.root / 'sys'), 'md0')
        self.assertEqual(array['id'], 'md-md0')
        self.assertTrue(array['degraded'])
        self.assertEqual(array['like'], '/dev/sdb1')
        # A partition member: the same partition, where it is, and the table's room.
        self.assertEqual(array['min_bytes'], (2048 + 8 * GIB // 512 + 4096) * 512 + (1 << 20))
        whole = mirror_add.md_array(md_sys(self.root / 'w', partition=False), 'md0')
        self.assertEqual(whole['like'], '/dev/sdb')
        self.assertEqual(whole['min_bytes'], 264192 * 512 + 8 * GIB, "the array's used size, not the old disk's")
        # Metadata kept at the end of each member (0.90, 1.0) needs room past the data.
        end = mirror_add.md_array(md_sys(self.root / 'e', partition=False, metadata='1.0'), 'md0')
        self.assertEqual(end['min_bytes'], 264192 * 512 + 8 * GIB + (4 << 20))

    def test_a_replacement_is_erased_by_the_one_entry_point_then_added(self):
        sys_block = md_sys(self.root / 'sys')
        stand = Stand(sys_block, self.dev)
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('being copied onto it', result['message'])
        array = mirror_add.md_array(sys_block, 'md0')
        self.assertEqual(
            stand.erased, [('ata-NEW_S9', 'S9', 16 * GIB, 'mirror_member', '/dev/sdb1', array['min_bytes'])]
        )
        self.assertIn('mdadm /dev/md0 --add /dev/sdc1', stand.asked)
        # The journal before each change, on the near side of the commit point (the add),
        # and the new member by its stable name, never /dev/sdX.
        self.assertEqual(stand.phases, [('erase', False), ('add', False)])
        self.assertEqual(stand.journals[1][2]['member'], 'ata-NEW_S9-part1')
        self.assertNotIn('/dev/', str(stand.journals))
        self.assertEqual(stand.cleared, 1)

    def test_a_mirror_whose_drive_is_away_is_refused_before_anything_is_erased(self):
        """T1-18: every drive a job uses is checked to be there, and the same one, before it
        starts: the mirror's own (where it is mounted), with the same words as every job."""
        mounted = {'findmnt -n -o TARGET --source /dev/md0': (0, '/mnt/nekoserver-media\n')}
        for said in ('missing', 'other'):
            stand = Stand(md_sys(self.root / said), self.dev, answers=mounted, away=[said])
            result = mirror_add.run_job(REQUEST, stand.ctx)
            self.assertEqual(result['state'], 'failed', result)
            self.assertIn('Plug it in (the same drive)', result['message'])
            self.assertIn('/mnt/nekoserver-media', result['message'])
            self.assertEqual((stand.erased, stand.journals), ([], []))
            self.assertEqual(stand.looked, ['/mnt/nekoserver-media'])

    def test_a_mirror_whose_drive_goes_while_the_new_one_is_erased_is_not_added_to(self):
        mounted = {'findmnt -n -o TARGET --source /dev/md0': (0, '/mnt/nekoserver-media\n')}
        stand = Stand(md_sys(self.root / 'sys'), self.dev, answers=mounted, away=[None, 'missing'])
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('nothing was changed in the mirror', result['message'])
        self.assertIn('Plug it in (the same drive)', result['message'])
        self.assertEqual(len(stand.erased), 1)
        self.assertFalse([a for a in stand.asked if '--add' in a])
        self.assertEqual(stand.phases, [('erase', False)])
        self.assertEqual(stand.cleared, 1)
        self.assertEqual(stand.looked, ['/mnt/nekoserver-media'] * 2)

    def test_a_mirror_not_mounted_anywhere_has_no_drive_to_check(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev, away=['missing'])
        self.assertEqual(mirror_add.run_job(REQUEST, stand.ctx)['state'], 'done')
        self.assertEqual(stand.looked, [])

    def test_nothing_is_erased_for_a_mirror_that_needs_no_drive(self):
        for name, sys_block, words in (
            ('healthy', md_sys(self.root / 'h', degraded=0), 'all its drives'),
            ('rebuilding', md_sys(self.root / 'r', action='recover'), 'already being rebuilt'),
        ):
            stand = Stand(sys_block, self.dev)
            result = mirror_add.run_job(REQUEST, stand.ctx)
            self.assertEqual(result['state'], 'failed', name)
            self.assertIn(words, result['message'])
            self.assertEqual(stand.erased, [], name)
            self.assertEqual(stand.phases, [], name)
        stand = Stand(md_sys(self.root / 'x'), self.dev)
        gone = mirror_add.run_job({**REQUEST, 'array': 'md-md9'}, stand.ctx)
        self.assertIn('not on this server', gone['message'])
        self.assertEqual(stand.erased, [])
        for bad in ('md0', 'md-../x', 'btrfs-; rm', None):
            self.assertIsNone(mirror_add.find(bad, stand.run, self.root), bad)

    def test_a_refused_drive_changes_nothing(self):
        stand = Stand(md_sys(self.root / 'sys'), self.dev, refuse='This disk holds your offline backup.')
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result, {'state': 'failed', 'message': 'This disk holds your offline backup.', 'advice': None})
        self.assertFalse(any(a.startswith('mdadm') for a in stand.asked))
        self.assertEqual(stand.cleared, 1)

    def test_a_failed_add_says_so_and_ends_the_journal(self):
        stand = Stand(
            md_sys(self.root / 'sys'), self.dev, answers={'mdadm': (1, 'mdadm: add new device failed for /dev/sdc1\n')}
        )
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('nothing was changed in it', result['message'])
        self.assertIn('add new device failed', result['advice'])
        self.assertEqual(stand.cleared, 1)

    def test_only_an_idle_array_takes_a_new_member(self):
        """Every md sync_action is offered or at work; one not known yet is at work."""
        for action, at_work in (
            ('idle', False),
            ('frozen', False),
            ('recover', True),
            ('resync', True),
            ('repair', True),
            ('reshape', True),
            ('check', True),
            ('something-new', True),
        ):
            array = mirror_add.md_array(md_sys(self.root / action, action=action), 'md0')
            self.assertEqual(array['rebuilding'], at_work, action)

    def test_names_and_counts_are_read_in_one_pass(self):
        """Sonar S8786: no backtracking pattern on what /sys or Btrfs says."""
        for kname, disk in (
            ('sdb1', 'sdb'),
            ('sdb', 'sdb'),
            ('nvme0n1p2', 'nvme0n1'),
            ('mmcblk0p1', 'mmcblk0'),
            ('sdp1', 'sdp'),
            ('vdb12', 'vdb'),
        ):
            self.assertEqual(mirror_add.disk_of(kname), disk, kname)
        for said, done in (
            ('45.5% done, 0 write errs\n', 45.5),
            ('Started on 1.Dec, 100.0% done\n', 100.0),
            ('Never started\n', None),
            ('..% done', None),
        ):
            self.assertEqual(mirror_add.percent_done(said), done, said)
        self.assertEqual(
            mirror_add.read_errors('Started on 1.Dec, finished on 1.Dec, 2 write errs, 3 uncorr. read errs'), 5
        )
        self.assertEqual(mirror_add.read_errors('Never started'), 0)
        # A long line that a backtracking pattern takes ages over is read at once.
        long = '1' * 200_000 + 'x'
        started = time.monotonic()
        self.assertEqual(mirror_add.disk_of(long), long)
        self.assertIsNone(mirror_add.percent_done('9.' * 100_000 + '% don'))
        self.assertEqual(mirror_add.read_errors('1 ' * 100_000), 0)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_new_drive_without_a_stable_name_is_not_added(self):
        """Resume could not find it again, so it would undo a good add: stop before the add."""
        (self.dev / 'disk' / 'by-id' / 'ata-NEW_S9-part1').unlink()
        stand = Stand(md_sys(self.root / 'sys'), self.dev)
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('nothing was changed in it', result['message'])
        self.assertFalse(any(a.startswith('mdadm /dev/md0 --add') for a in stand.asked))
        self.assertEqual(stand.phases, [('erase', False)])
        self.assertEqual(stand.cleared, 1)

    def test_offers_use_the_one_refusal_function_with_the_mirrors_size(self):
        array = mirror_add.md_array(md_sys(self.root / 'sys'), 'md0')
        asked = []

        def refusal(disk, minimum):
            asked.append((disk['by_id'], minimum))
            return None if disk['size_bytes'] >= minimum else {'code': 'small', 'words': 'Too small'}

        disks = [{'by_id': 'big', 'size_bytes': 16 * GIB}, {'by_id': 'small', 'size_bytes': 4 * GIB}]
        self.assertEqual(
            mirror_add.offers(disks, [array], refusal),
            {
                'md-md0': [
                    {'by_id': 'big', 'ok': True, 'words': None},
                    {'by_id': 'small', 'ok': False, 'words': 'Too small'},
                ]
            },
        )
        self.assertEqual(asked, [('big', array['min_bytes']), ('small', array['min_bytes'])])

    def test_after_a_power_cut_the_boot_job_says_which_side_it_was(self):
        sys_block = md_sys(self.root / 'sys')
        stand = Stand(sys_block, self.dev)
        before = mirror_add.resume({'phase': 'erase', 'request': REQUEST, 'undo': {'disk': REQUEST['disk']}}, stand.ctx)
        self.assertEqual(before['state'], 'undone')
        self.assertTrue(before['power_cut'])
        self.assertIn('nothing was changed in the mirror', before['message'])
        cut = {'phase': 'add', 'request': REQUEST, 'undo': {'member': 'ata-NEW_S9-part1'}}
        self.assertEqual(mirror_add.resume(cut, stand.ctx)['state'], 'undone')
        self.assertIn('was erased', mirror_add.resume(cut, stand.ctx)['message'])
        # md took the drive before the power went, and the restart named it sdd: found by
        # its stable name, the rebuild goes on by itself.
        by_id(self.dev, 'sdd')
        write(sys_block / 'md0' / 'md' / 'dev-sdd1' / 'state', 'spare')
        after = mirror_add.resume(cut, stand.ctx)
        self.assertEqual(after['state'], 'resumed', after)
        self.assertFalse(any(a.startswith('mdadm') for a in stand.asked), 'never added twice')
        self.assertEqual(stand.erased, [], 'never erased again')


SHOW = """Label: 'media'  uuid: 1b2c3d4e-0000-4000-8000-000000000001
\tTotal devices 2 FS bytes used 1000
\tdevid    1 size 17179869184 used 2000 path /dev/sdb
\tdevid    2 size 17179869184 used 2000 path <missing disk> MISSING
"""
UUID = '1b2c3d4e-0000-4000-8000-000000000001'
POOL = {**REQUEST, 'array': f'btrfs-{UUID}'}
# After the replace: the new drive holds the missing device's number, and none is missing.
HEALED = SHOW.replace('path <missing disk> MISSING', 'path /dev/sdc')
FINISHED = (0, 'Started on 1.Dec 10:00:00, finished on 1.Dec 11:00:00, 0 write errs, 0 uncorr. read errs\n')


def shows(*after):
    """The pool as `btrfs filesystem show` says it: degraded at the job's look, then `after`."""
    return {f'btrfs filesystem show --raw {UUID}': [(0, SHOW), *((0, s) for s in after)]}


class Btrfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = by_id(Path(self.tmp.name) / 'dev', 'sdc')

    def tearDown(self):
        self.tmp.cleanup()

    def answers(self, **more):
        out = {
            f'btrfs filesystem show --raw {UUID}': (0, SHOW),
            'btrfs filesystem show --raw': (0, SHOW),
            'findmnt': (0, '/mnt/nekoserver-media\n'),
            'btrfs replace status': (0, 'Never started\n'),
        }
        out.update(more)
        return out

    def test_the_pool_says_its_missing_device(self):
        stand = Stand('/nonexistent', self.dev, answers=self.answers())
        pool = mirror_add.btrfs_pool(stand.run, UUID)
        self.assertEqual(
            (pool['devid'], pool['min_bytes'], pool['like'], pool['mount'], pool['rebuilding']),
            (2, 16 * GIB, '/dev/sdb', '/mnt/nekoserver-media', False),
        )
        self.assertEqual([a['id'] for a in mirror_add.degraded(stand.run, '/nonexistent')], [f'btrfs-{UUID}'])

    def test_a_pool_open_degraded_names_no_size_for_its_missing_device(self):
        """Job D's loop test (btrfs-progs v7.1): mounted degraded, as a replace needs it, the
        missing device is "size 0 used 0 path  MISSING". The room the new drive needs is
        then the largest device left (a raid1 member's), never 0 ("no drive left")."""
        opened = (
            "Label: none  uuid: 1b2c3d4e-0000-4000-8000-000000000001\n"
            '\tTotal devices 2 FS bytes used 163840\n'
            '\tdevid    1 size 332398592 used 109051904 path /dev/loop0p2\n'
            '\tdevid    2 size 0 used 0 path  MISSING\n'
        )
        said = {f'btrfs filesystem show --raw {UUID}': (0, opened), 'btrfs filesystem show --raw': (0, opened)}
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**said))
        pool = mirror_add.btrfs_pool(stand.run, UUID)
        self.assertEqual((pool['devid'], pool['min_bytes'], pool['like']), (2, 332398592, '/dev/loop0p2'))
        self.assertIsNone(mirror_add.why_not(pool))

    def test_a_pool_with_a_replace_at_work_is_refused(self):
        """The missing device is still listed until a replace ends: Btrfs is asked."""
        for said in ((0, '12.5% done, 0 write errs\n'), (0, 'Running\n'), (1, 'ERROR: cannot read\n')):
            stand = Stand('/nonexistent', self.dev, answers=self.answers(**{'btrfs replace status': said}))
            result = mirror_add.run_job(POOL, stand.ctx)
            self.assertIn('already being rebuilt', result['message'], said)
            self.assertEqual(stand.erased, [], said)
            self.assertEqual(mirror_add.degraded(stand.run, '/nonexistent'), [], said)

    def test_a_replacement_is_copied_onto_then_balanced(self):
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(
                **shows(HEALED),
                **{
                    'btrfs replace status': [
                        (0, 'Never started\n'),  # the look before the start
                        (0, 'Never started\n'),  # the start returned before the kernel began
                        (0, '45.5% done, 0 write errs\n'),
                        FINISHED,
                    ]
                },
            ),
        )
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('on both drives again', result['message'])
        self.assertIn('btrfs replace start 2 /dev/sdc /mnt/nekoserver-media', stand.asked)
        self.assertIn(
            'btrfs balance start -dconvert=raid1,soft -mconvert=raid1,soft /mnt/nekoserver-media', stand.asked
        )
        self.assertIn(('rebuild', 45.5), stand.steps)
        self.assertEqual(stand.phases, [('erase', False), ('add', False), ('rebuild', True)])
        self.assertEqual(stand.journals[2][2]['member'], 'ata-NEW_S9')

    def test_a_replace_that_cannot_be_followed_keeps_the_journal(self):
        """Btrfs may still be copying: no other job may be taken, so the journal stays."""
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(**{'btrfs replace status': [(0, 'Never started\n'), (1, 'timed out\n')]}),
        )
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('restart the server', result['message'])
        self.assertEqual(stand.cleared, 0)
        # If it never comes back, the person is told how to look themselves.
        self.assertIn('btrfs replace status /mnt/nekoserver-media', result['advice'])
        self.assertIn('RECOVERY.md', result['advice'])
        self.assertIn('timed out', result['advice'])

    def test_a_replace_that_never_starts_is_a_failure_not_a_copy(self):
        """`replace start` without -B returns before the kernel began or refused it: a
        replace still "Never started" after the wait copied nothing (T6-101-1)."""
        stand = Stand('/nonexistent', self.dev, answers=self.answers())
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('did not start copying', result['message'])
        self.assertNotIn('both drives', result['message'])
        self.assertIn('Never started', result['advice'])
        self.assertFalse(any(a.startswith('btrfs balance') for a in stand.asked))
        self.assertEqual(stand.slept, mirror_add.NEVER_LOOKS, 'waited, within a limit')
        self.assertEqual(stand.cleared, 1, 'nothing is copying: no journal holds the page')

    def test_every_replace_status_is_one_state(self):
        """btrfs-progs cmds/replace.c prints one line per kernel state (T6-101-4)."""
        at = 'Started on 24.Nov 10:00:00, {} on 24.Nov 10:30:00{}, 0 write errs, 0 uncorr. read errs\n'
        for rc, said, state, at_work in (
            (0, 'Never started\n', 'never', False),
            (0, '12.5% done, 0 write errs, 0 uncorr. read errs\n', 'running', True),
            (0, at.format('finished', ''), 'finished', False),
            (0, at.format('canceled', ' at 41.0%'), 'canceled', False),
            (0, at.format('suspended', ' at 41.0%'), 'suspended', True),
            (0, 'Unknown replace state\n', 'unknown', True),
            (1, 'ERROR: ioctl(DEV_REPLACE_STATUS) failed\n', 'unknown', True),
        ):
            self.assertEqual(mirror_add.replace_state(rc, said), state, said)
            stand = Stand('/nonexistent', self.dev, answers=self.answers(**{'btrfs replace status': (rc, said)}))
            self.assertEqual(mirror_add.btrfs_pool(stand.run, UUID)['rebuilding'], at_work, said)

    def test_every_end_of_a_replace_lets_go_of_the_page(self):
        """Over with the pool not whole: failed with its own words, journal cleared, no balance.
        Only an answer that can't be read keeps the journal (Btrfs may be copying)."""
        at = 'Started on 24.Nov 10:00:00, {} on 24.Nov 10:30:00 at 41.0%, 0 write errs, 0 uncorr. read errs\n'
        for said, words, waits in (
            ((0, at.format('canceled')), 'Add the new drive again', 0),
            ((0, at.format('suspended')), 'Restart the server', mirror_add.NEVER_LOOKS),
            ((0, 'Never started\n'), 'did not start copying', mirror_add.NEVER_LOOKS),
            (FINISHED, 'still misses a drive', 0),
        ):
            stand = Stand(
                '/nonexistent',
                self.dev,
                answers=self.answers(**shows(SHOW), **{'btrfs replace status': [(0, 'Never started\n'), said]}),
            )
            result = mirror_add.run_job(POOL, stand.ctx)
            self.assertEqual(result['state'], 'failed', said)
            self.assertIn(words, result['message'], said)
            self.assertEqual(stand.cleared, 1, said)
            self.assertFalse(any(a.startswith('btrfs balance') for a in stand.asked), said)
            self.assertEqual(stand.slept, waits, said)

    def test_a_rebuild_that_cannot_be_followed_holds_the_page_three_starts_at_most(self):
        stand = Stand('/nonexistent', self.dev, answers=self.answers(findmnt=(1, '')))
        cut = {'phase': 'rebuild', 'request': POOL, 'undo': {'array': f'btrfs-{UUID}', 'member': 'ata-NEW_S9'}}
        for runs in range(1, mirror_add.HOLD_RUNS + 1):
            result = mirror_add.finish(cut, stand.ctx)
            self.assertIn('Restart the server once more', result['message'])
            self.assertEqual(stand.cleared, 0)
            self.assertEqual(stand.journals[-1], ('rebuild', True, {**cut['undo'], 'runs': runs}))
            cut = {**cut, 'undo': stand.journals[-1][2]}
        last = mirror_add.finish(cut, stand.ctx)
        self.assertEqual(last['state'], 'failed')
        self.assertIn('lets go of it', last['message'])
        self.assertEqual(stand.cleared, 1)
        self.assertFalse(any(a.startswith('btrfs replace start') for a in stand.asked))

    def test_a_pool_whole_again_without_a_replace_record_is_finished(self):
        """Cut after the replace ended: the next mount keeps no record ("Never started"),
        but the pool holds the new drive and misses none, so it is balanced and done."""
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**shows(HEALED)))
        cut = {'phase': 'rebuild', 'request': POOL, 'undo': {'array': f'btrfs-{UUID}', 'member': 'ata-NEW_S9'}}
        result = mirror_add.finish(cut, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertNotIn('erased', result['message'])
        self.assertTrue(any(a.startswith('btrfs balance start') for a in stand.asked))

    def test_finished_is_done_only_when_the_pool_misses_no_drive(self):
        # The new drive not in the pool, or in it while a device is still missing.
        both = SHOW + '\tdevid    3 size 17179869184 used 0 path /dev/sdc\n'
        for after in (SHOW, both):
            stand = Stand(
                '/nonexistent',
                self.dev,
                answers=self.answers(**shows(after), **{'btrfs replace status': [(0, 'Never started\n'), FINISHED]}),
            )
            result = mirror_add.run_job(POOL, stand.ctx)
            self.assertEqual(result['state'], 'failed', result)
            self.assertIn('still misses a drive', result['message'])
            self.assertFalse(any(a.startswith('btrfs balance') for a in stand.asked))

    def test_a_replace_with_read_errors_does_not_say_all_is_copied(self):
        """A finished replace still reports what it could not read (T6-101-2)."""
        errs = (0, 'Started on 1.Dec, finished on 1.Dec, 0 write errs, 3 uncorr. read errs\n')
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(**shows(HEALED), **{'btrfs replace status': [(0, 'Never started\n'), errs]}),
        )
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertNotIn('both drives again', result['message'])
        self.assertIn('3 pieces could not be read', result['message'])
        self.assertIn('btrfs scrub start -B /mnt/nekoserver-media', result['advice'])
        self.assertEqual(mirror_add.read_errors(FINISHED[1]), 0)

    def test_a_pool_that_is_not_open_is_refused_before_anything(self):
        stand = Stand('/nonexistent', self.dev, answers=self.answers(findmnt=(1, '')))
        result = mirror_add.run_job(POOL, stand.ctx)
        self.assertIn('Restart the server', result['message'])
        self.assertEqual(stand.erased, [])

    def test_a_cut_off_rebuild_waits_while_the_pools_drive_is_away(self):
        """T1-18: at the start, what needs a drive that is away waits for it: the pool's
        balance writes to it, so the journal stays, and nothing is run on it."""
        journal = {'request': POOL, 'phase': 'rebuild', 'committed': True, 'undo': {'member': 'ata-NEW_S9'}}
        stand = Stand('/nonexistent', self.dev, answers=self.answers(**shows(HEALED)), away=['missing'])
        result = mirror_add.finish(journal, stand.ctx)
        self.assertEqual(result['state'], 'failed', result)
        self.assertIn('Plug it in (the same drive)', result['message'])
        self.assertEqual((stand.cleared, stand.journals), (0, []))  # not counted as a start
        self.assertFalse([a for a in stand.asked if a.startswith(('btrfs balance', 'btrfs replace start'))])
        self.assertEqual(stand.looked, ['/mnt/nekoserver-media'])

    def test_a_rebuild_cut_off_is_finished_never_erased_or_started_again(self):
        stand = Stand(
            '/nonexistent',
            self.dev,
            answers=self.answers(
                **shows(HEALED),
                **{'btrfs replace status': [(0, '80.0% done\n'), (0, '90.0% done\n'), FINISHED, FINISHED]},
            ),
        )
        cut = {'phase': 'rebuild', 'request': POOL, 'undo': {'array': f'btrfs-{UUID}', 'member': 'ata-NEW_S9'}}
        result = mirror_add.finish(cut, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertEqual(stand.erased, [])
        self.assertFalse(any(a.startswith('btrfs replace start') for a in stand.asked))
        self.assertTrue(any(a.startswith('btrfs balance start') for a in stand.asked))
        # resume, given a rebuild anyway, finishes it the same way (never {'again': ...},
        # which storage_jobs does not read: T6-101-3).
        again = mirror_add.resume(cut, stand.ctx)
        self.assertEqual(again['state'], 'done', again)
        self.assertFalse(any(a.startswith('btrfs replace start') for a in stand.asked))


if __name__ == '__main__':
    unittest.main()
