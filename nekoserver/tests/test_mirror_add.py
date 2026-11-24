"""Job D of the Drives page (mirror_add.py, 1.0.1 contract §7): a replacement drive added
to a mirror that lost one, mdadm and Btrfs, and the boot job after a power cut.

On a stand-in /sys folder and a stand-in runner that notes every command: no disk, no
mdadm, no btrfs is touched. The drive is checked and erased by storage_jobs' one entry
point, here a stand-in that notes what it was asked."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mirror_add  # noqa: E402

GIB = 1 << 30


class Refused(Exception):
    pass


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{text}\n')


def md_sys(root, degraded=1, action='idle', partition=True):
    """md0 of two members; sdb1 still there, the other one gone."""
    md = root / 'md0' / 'md'
    write(md / 'degraded', degraded)
    write(md / 'sync_action', action)
    write(md / 'component_size', 8 * GIB // 1024)
    if partition:
        write(md / 'dev-sdb1' / 'state', 'in_sync')
        write(md / 'dev-sdb1' / 'offset', 2048)
        write(root / 'sdb' / 'sdb1' / 'start', 2048)
        write(root / 'sdb' / 'sdb1' / 'size', 8 * GIB // 512 + 4096)
    else:
        write(md / 'dev-sdb' / 'state', 'in_sync')
        write(md / 'dev-sdb' / 'offset', 264192)
    return root


class Stand:
    """storage_jobs' side: the runner, erase, journal, progress and clear."""

    def __init__(self, sys_block, answers=None, refuse=None):
        self.asked = []
        self.answers = answers or {}
        self.refuse = refuse
        self.phases = []
        self.steps = []
        self.cleared = 0
        self.erased = []
        self.ctx = mirror_add.Context(
            run=self.run,
            erase=self.erase,
            refused=Refused,
            journal=lambda phase, committed, undo: self.phases.append((phase, committed)),
            progress=lambda step, percent=None: self.steps.append((step, percent)),
            clear=self.clear,
            sys_block=sys_block,
            sleep=lambda s: None,
        )

    def run(self, argv):
        self.asked.append(' '.join(argv))
        for prefix, answer in self.answers.items():
            if ' '.join(argv).startswith(prefix):
                return answer.pop(0) if isinstance(answer, list) else answer
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

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_array_says_what_the_new_drive_needs(self):
        array = mirror_add.md_array(md_sys(self.root), 'md0')
        self.assertEqual(array['id'], 'md-md0')
        self.assertTrue(array['degraded'])
        self.assertEqual(array['like'], '/dev/sdb1')
        # A partition member: the same partition, where it is, and the table's room.
        self.assertEqual(array['min_bytes'], (2048 + 8 * GIB // 512 + 4096) * 512 + (1 << 20))
        whole = mirror_add.md_array(md_sys(self.root / 'w', partition=False), 'md0')
        self.assertEqual(whole['like'], '/dev/sdb')
        self.assertEqual(whole['min_bytes'], 264192 * 512 + 8 * GIB, "the array's used size, not the old disk's")

    def test_a_replacement_is_erased_by_the_one_entry_point_then_added(self):
        stand = Stand(md_sys(self.root))
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('being copied onto it', result['message'])
        array = mirror_add.md_array(self.root, 'md0')
        self.assertEqual(
            stand.erased, [('ata-NEW_S9', 'S9', 16 * GIB, 'mirror_member', '/dev/sdb1', array['min_bytes'])]
        )
        self.assertIn('mdadm /dev/md0 --add /dev/sdc1', stand.asked)
        # The journal before each change, on the near side of the commit point (the add).
        self.assertEqual(stand.phases, [('erase', False), ('add', False)])
        self.assertEqual(stand.cleared, 1)

    def test_nothing_is_erased_for_a_mirror_that_needs_no_drive(self):
        for name, sys_block, words in (
            ('healthy', md_sys(self.root / 'h', degraded=0), 'all its drives'),
            ('rebuilding', md_sys(self.root / 'r', action='recover'), 'already being rebuilt'),
        ):
            stand = Stand(sys_block)
            result = mirror_add.run_job(REQUEST, stand.ctx)
            self.assertEqual(result['state'], 'failed', name)
            self.assertIn(words, result['message'])
            self.assertEqual(stand.erased, [], name)
            self.assertEqual(stand.phases, [], name)
        stand = Stand(md_sys(self.root / 'x'))
        gone = mirror_add.run_job({**REQUEST, 'array': 'md-md9'}, stand.ctx)
        self.assertIn('not on this server', gone['message'])
        self.assertEqual(stand.erased, [])
        for bad in ('md0', 'md-../x', 'btrfs-; rm', None):
            self.assertIsNone(mirror_add.find(bad, stand.run, self.root), bad)

    def test_a_refused_drive_changes_nothing(self):
        stand = Stand(md_sys(self.root), refuse='This disk holds your offline backup.')
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result, {'state': 'failed', 'message': 'This disk holds your offline backup.', 'advice': None})
        self.assertFalse(any(a.startswith('mdadm') for a in stand.asked))
        self.assertEqual(stand.cleared, 1)

    def test_a_failed_add_says_so_and_ends_the_journal(self):
        stand = Stand(md_sys(self.root), answers={'mdadm': (1, 'mdadm: add new device failed for /dev/sdc1\n')})
        result = mirror_add.run_job(REQUEST, stand.ctx)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('nothing was changed in it', result['message'])
        self.assertIn('add new device failed', result['advice'])
        self.assertEqual(stand.cleared, 1)

    def test_offers_use_the_one_refusal_function_with_the_mirrors_size(self):
        array = mirror_add.md_array(md_sys(self.root), 'md0')
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
        md_sys(self.root)
        stand = Stand(self.root)
        before = mirror_add.resume({'phase': 'erase', 'request': REQUEST, 'undo': {'disk': REQUEST['disk']}}, stand.ctx)
        self.assertEqual(before['state'], 'undone')
        self.assertTrue(before['power_cut'])
        self.assertIn('nothing was changed in the mirror', before['message'])
        cut = {'phase': 'add', 'request': REQUEST, 'undo': {'target': '/dev/sdc1'}}
        self.assertEqual(mirror_add.resume(cut, stand.ctx)['state'], 'undone')
        self.assertIn('was erased', mirror_add.resume(cut, stand.ctx)['message'])
        # md took the drive before the power went: the rebuild goes on by itself.
        write(self.root / 'md0' / 'md' / 'dev-sdc1' / 'state', 'spare')
        after = mirror_add.resume(cut, stand.ctx)
        self.assertEqual(after['state'], 'resumed')
        self.assertFalse(any(a.startswith('mdadm') for a in stand.asked), 'never added twice')
        self.assertEqual(stand.erased, [], 'never erased again')


SHOW = """Label: 'media'  uuid: 1b2c3d4e-0000-4000-8000-000000000001
\tTotal devices 2 FS bytes used 1000
\tdevid    1 size 17179869184 used 2000 path /dev/sdb
\tdevid    2 size 17179869184 used 2000 path <missing disk> MISSING
"""
UUID = '1b2c3d4e-0000-4000-8000-000000000001'


class Btrfs(unittest.TestCase):
    def answers(self, **more):
        out = {
            f'btrfs filesystem show --raw {UUID}': (0, SHOW),
            'btrfs filesystem show --raw': (0, SHOW),
            'findmnt': (0, '/mnt/nekoserver-media\n'),
        }
        out.update(more)
        return out

    def test_the_pool_says_its_missing_device(self):
        stand = Stand('/nonexistent', answers=self.answers())
        pool = mirror_add.btrfs_pool(stand.run, UUID)
        self.assertEqual(
            (pool['devid'], pool['min_bytes'], pool['like'], pool['mount']),
            (2, 16 * GIB, '/dev/sdb', '/mnt/nekoserver-media'),
        )
        self.assertEqual([a['id'] for a in mirror_add.degraded(stand.run, '/nonexistent')], [f'btrfs-{UUID}'])

    def test_a_replacement_is_copied_onto_then_balanced(self):
        stand = Stand(
            '/nonexistent',
            answers=self.answers(
                **{
                    'btrfs replace status': [
                        (0, '45.5% done, 0 write errs\n'),
                        (0, 'Started on 1.Dec, finished on 1.Dec\n'),
                    ]
                }
            ),
        )
        result = mirror_add.run_job({**REQUEST, 'array': f'btrfs-{UUID}'}, stand.ctx)
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('btrfs replace start 2 /dev/sdc /mnt/nekoserver-media', stand.asked)
        self.assertIn(
            'btrfs balance start -dconvert=raid1,soft -mconvert=raid1,soft /mnt/nekoserver-media', stand.asked
        )
        self.assertIn(('rebuild', 45.5), stand.steps)
        self.assertEqual(stand.phases, [('erase', False), ('add', False), ('rebuild', True)])

    def test_a_pool_that_is_not_open_is_refused_before_anything(self):
        stand = Stand('/nonexistent', answers=self.answers(findmnt=(1, '')))
        result = mirror_add.run_job({**REQUEST, 'array': f'btrfs-{UUID}'}, stand.ctx)
        self.assertIn('Restart the server', result['message'])
        self.assertEqual(stand.erased, [])

    def test_a_rebuild_cut_off_is_started_again_not_undone(self):
        stand = Stand('/nonexistent', answers=self.answers())
        cut = {'phase': 'rebuild', 'request': {**REQUEST, 'array': f'btrfs-{UUID}'}, 'undo': {'target': '/dev/sdc'}}
        self.assertEqual(mirror_add.resume(cut, stand.ctx), {'again': True})
        self.assertEqual(stand.cleared, 0, 'the journal stays for the job started again')


if __name__ == '__main__':
    unittest.main()
