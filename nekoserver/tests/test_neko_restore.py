"""0.51.0 step 4: the standalone restore tool (neko_restore.py, on every card as
NekoBackup/tools/neko-restore.py): list, verify and restore one card with nothing but
Python, never trusting the card, never overwriting without a named approval (V2 section
12, A27, A32 to A35). The cards here are written by hand in archive format 1; the Rust
tests run the same tool on cards the engine wrote."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_restore as tool  # noqa: E402

FIXTURES = ROOT / 'tests/fixtures/cardset/records'
SET = '00000000-0000-4000-8000-0000000000aa'
VOLUME = '00000000-0000-4000-8000-000000000001'
SOURCE = 'cccccccc-0000-4000-8000-000000000001'
VOLUME_2 = '00000000-0000-4000-8000-000000000002'
A_2001 = '2001/a.jpg'
X_A = 'x/a.jpg'
A_JPG = 'a.jpg'
JSON = '.json'
CARD_FLAG = '--card'
SEARCH = '--search'
VERSIONS = '.versions.jsonl'
SAND = b'sand'


def uuid(n, prefix='aaaaaaaa'):
    return '%s-0000-4000-8000-%012d' % (prefix, n)


class Card:
    """A format 1 card written by hand: identity.json, and records with their payloads."""

    def __init__(self, folder, seq=1, set_id=SET, volume=VOLUME):
        self.top = Path(folder)
        self.nb = self.top / 'NekoBackup'
        for sub in ('objects', 'records', 'staging', 'inventory', 'tools'):
            (self.nb / sub).mkdir(parents=True, exist_ok=True)
        self.volume = volume
        self.identity = {
            'format': 'neko-backup-volume',
            'archive_format_version': 1,
            'format_minor': 0,
            'backup_set_id': set_id,
            'volume_id': volume,
            'sequence_number': seq,
            'set_display_name': 'Example set',
            'volume_label': 'Example set - %03d' % seq,
            'created_at': '2001-02-03T04:05:06Z',
            'filesystem_type': 'ext4',
            'filesystem_uuid': 'example',
            'encrypted': False,
            'privacy_acknowledged_at': '2001-02-03T04:05:00Z',
            'written_by': 'test',
        }
        (self.nb / 'identity.json').write_text(json.dumps(self.identity) + '\n')
        self.n = 0

    def add(self, path, data, mtime_ns=981173106123456789):
        self.n += 1
        rid = uuid(self.n)
        ext = tool.safe_ext(path.rsplit('/', 1)[-1])
        archive = 'objects/%s/%s%s' % (rid[:2], rid, ext)
        (self.nb / archive).parent.mkdir(parents=True, exist_ok=True)
        (self.nb / archive).write_bytes(data)
        rec = {
            'schema_version': 1,
            'record_id': rid,
            'version_id': uuid(self.n, 'bbbbbbbb'),
            'replaces_record_id': None,
            'backup_set_id': SET,
            'volume_id': self.volume,
            'source_id': SOURCE,
            'source_display_name': 'Example photos',
            'original_relative_path': path,
            'original_file_name': path.rsplit('/', 1)[-1],
            'archive_relative_path': archive,
            'byte_size': len(data),
            'source_mtime_ns': str(mtime_ns),
            'source_identity': {'device': '1', 'inode': str(self.n), 'ctime_ns': '0'},
            'checksum_algorithm': 'sha256',
            'checksum': hashlib.sha256(data).hexdigest(),
            'source_observed_at': '2001-02-03T04:05:06Z',
            'copy_verified_at': '2001-02-03T04:05:07Z',
            'committed_at': '2001-02-03T04:05:08Z',
            'run_id': uuid(1, 'dddddddd'),
            'transaction_id': uuid(self.n, 'eeeeeeee'),
            'completion_state': 'committed',
            'consistency': {'mode': 'live-file-observation', 'snapshot': None},
            'written_by': 'test',
        }
        folder = self.nb / 'records' / rid[:2]
        folder.mkdir(exist_ok=True)
        (folder / (rid + JSON)).write_text(json.dumps(rec, ensure_ascii=False) + '\n')
        return rec

    def payload(self, rec):
        return self.nb / rec['archive_relative_path']


def progress_done(path):
    """The versions a progress file names as restored: its lines after the header."""
    lines = Path(path).read_text().splitlines()
    assert json.loads(lines[0]) == {'format': tool.PROGRESS_FORMAT, 'version': 2}, lines[0]
    return {json.loads(line)['version_id']: json.loads(line) for line in lines[1:]}


def said_text(case):
    """What the tool said in a test, as one text."""
    return '\n'.join(case.said)


def cards_under(folder):
    """The tool reads cards mounted under `folder` (a test's own) instead of /media and /mnt."""
    return mock.patch.object(tool, 'CARD_ROOTS', (os.path.realpath(folder) + '/',))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        roots = cards_under(self.tmp)
        roots.start()
        self.addCleanup(roots.stop)
        self.card = Card(self.tmp / 'card')
        self.dest = self.tmp / 'restored'
        self.dest.mkdir()
        self.said = []

    def run_tool(self, *args):
        self.said = []
        return tool.main([CARD_FLAG, str(self.card.top), *args], say=self.said.append)


class SameReaderTests(unittest.TestCase):
    def test_every_fixture_is_judged_as_its_name_says(self):
        # The Rust engine is held to the same files (tests_restore_tool.rs).
        names = sorted(os.listdir(FIXTURES))
        self.assertGreaterEqual(len(names), 30)
        for name in names:
            data = (FIXTURES / name).read_bytes()
            with self.subTest(fixture=name):
                if name.startswith('valid-'):
                    rec = tool.parse_record(data)
                    tool.check_relative_path(rec['original_relative_path'])
                else:
                    with self.assertRaises(tool.Invalid):
                        tool.parse_record(data)

    def test_another_archive_format_is_not_read(self):
        with tempfile.TemporaryDirectory() as d, cards_under(d):
            card = Card(Path(d) / 'card')
            card.identity['archive_format_version'] = 2
            (card.nb / 'identity.json').write_text(json.dumps(card.identity))
            said = []
            self.assertEqual(tool.main([CARD_FLAG, str(card.top), 'info'], say=said.append), 2)
            self.assertIn('archive format 2, not format 1', said[0])


class WhereACardMustBeTests(unittest.TestCase):
    """--card reads only a card mounted where Linux mounts one, every link resolved first
    (SonarCloud pythonsecurity:S8707: a path from the command line, proved before use)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.mounts = self.tmp / 'media'
        self.card = Card(self.mounts / 'me' / 'CARD')

    def info(self, path):
        said = []
        with cards_under(self.mounts):
            code = tool.main([CARD_FLAG, str(path), 'info'], say=said.append)
        return code, '\n'.join(said)

    def test_the_places_are_where_linux_mounts_a_card(self):
        self.assertEqual(tool.CARD_ROOTS, ('/media/', '/run/media/', '/mnt/'))

    def test_a_card_under_those_places_is_read(self):
        for path in (self.card.top, self.card.nb):  # its top folder or its NekoBackup folder
            code, said = self.info(path)
            self.assertEqual(code, 0, said)
            self.assertIn('Example set - 001', said)

    def test_a_card_mounted_on_one_of_those_places_itself_is_read(self):
        """The review of 0.51.0: a card mounted on /mnt itself was refused with "Mount the
        card under /media or /mnt"."""
        Card(self.mounts)
        code, said = self.info(self.mounts)
        self.assertEqual(code, 0, said)
        # A folder beside it whose name only starts the same is still elsewhere.
        Card(self.tmp / 'mediax')
        self.assertEqual(self.info(self.tmp / 'mediax')[0], 2)

    def test_a_folder_elsewhere_is_refused_in_plain_words(self):
        elsewhere = Card(self.tmp / 'home' / 'CARD')
        code, said = self.info(elsewhere.top)
        self.assertEqual(code, 2)
        self.assertIn('is not where a card is mounted. Mount the card under /media or /mnt', said)
        self.assertIn('or run this tool from the card', said)

    def test_a_link_out_of_those_places_is_refused(self):
        outside = Card(self.tmp / 'home' / 'CARD')
        link = self.mounts / 'link-to-home'
        link.symlink_to(outside.top)
        code, said = self.info(link)
        self.assertEqual(code, 2)
        self.assertIn(str(outside.top) + ' is not where a card is mounted', said)


class ListAndVerifyTests(Base):
    def test_info_list_and_verify_from_the_card_alone(self):
        self.card.add('2001/beach.jpg', SAND * 100)
        self.card.add('2002/city\nnight.png', b'lights')
        self.assertEqual(self.run_tool('info'), 0)
        self.assertIn('Card:        Example set - 001', said_text(self))
        self.assertIn('Not encrypted', said_text(self))
        self.assertEqual(self.run_tool('list'), 0)
        self.assertIn('2002/city\\nnight.png', said_text(self), 'a control character is shown escaped')
        self.assertEqual(self.run_tool('list', SEARCH, 'beach'), 0)
        self.assertIn('1 file(s)', said_text(self))
        self.assertEqual(self.run_tool('verify'), 0)
        self.assertIn('ok 2, damaged 0, missing 0', said_text(self))

    def test_damage_missing_files_and_unusable_records_are_all_said(self):
        good = self.card.add(A_JPG, b'one')
        damaged = self.card.add('b.jpg', b'two')
        missing = self.card.add('c.jpg', b'three')
        self.card.payload(damaged).write_bytes(b'tw0')
        self.card.payload(missing).unlink()
        # A record that names another card, and one that is not a record at all.
        other = self.card.add('d.jpg', b'four')
        path = self.card.nb / 'records' / other['record_id'][:2] / (other['record_id'] + JSON)
        path.write_text(path.read_text().replace(VOLUME, VOLUME_2))
        (self.card.nb / 'records/zz').mkdir()
        (self.card.nb / 'records/zz/junk.json').write_text('{"schema_version": 1}')
        self.assertEqual(self.run_tool('verify'), 1)
        text = said_text(self)
        self.assertIn('damaged  b.jpg', text)
        self.assertIn('missing  c.jpg', text)
        self.assertIn('it names another card or set than this one', text)
        self.assertIn('records/zz/junk.json', text)
        self.assertIn('ok 1, damaged 1, missing 1, unusable records 2', text)
        self.assertEqual(tool.check_copy(tool.Card(str(self.card.top)), good), 'ok')

    def test_links_on_the_card_are_never_followed(self):
        # A27: a payload that is a link to a file elsewhere is not read through it.
        secret = self.tmp / 'elsewhere.txt'
        secret.write_bytes(b'not on the card')
        rec = self.card.add('x.jpg', b'not on the card')
        self.card.payload(rec).unlink()
        self.card.payload(rec).symlink_to(secret)
        self.assertEqual(self.run_tool('verify'), 1)
        self.assertIn('damaged  x.jpg', said_text(self))
        # A NekoBackup folder that is a link is not a card.
        linked = self.tmp / 'linked'
        linked.mkdir()
        (linked / 'NekoBackup').symlink_to(self.card.nb)
        said = []
        self.assertEqual(tool.main([CARD_FLAG, str(linked), 'info'], say=said.append), 2)
        self.assertIn('no NekoBackup folder', said[0])

    def test_run_from_the_card_it_finds_the_card(self):
        on_card = self.card.nb / 'tools/neko-restore.py'
        shutil.copy(ROOT / 'neko_restore.py', on_card)
        import subprocess

        out = subprocess.run([sys.executable, str(on_card), 'info'], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('Example set - 001', out.stdout)


class RestoreTests(Base):
    def test_files_come_back_with_their_bytes_and_times_in_their_folders(self):
        a = self.card.add('2001/deep/beach.jpg', SAND * 1000)
        self.card.add('2002/שלום \U0001f408.png', b'cat')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 0)
        back = self.dest / '2001/deep/beach.jpg'
        self.assertEqual(back.read_bytes(), SAND * 1000)
        self.assertEqual(os.stat(back).st_mtime_ns, int(a['source_mtime_ns']))
        self.assertEqual((self.dest / '2002/שלום \U0001f408.png').read_bytes(), b'cat')
        self.assertIn('restored 2', said_text(self))
        leftovers = [p for p in self.dest.rglob('*') if p.name.startswith('.neko-restore-')]
        self.assertEqual(leftovers, [])

    def test_a_damaged_file_leaves_no_output(self):
        # A33.
        rec = self.card.add('broken.jpg', b'original bytes')
        self.card.payload(rec).write_bytes(b'Original bytes')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
        self.assertIn('failed      broken.jpg', said_text(self))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_nothing_is_overwritten_without_a_named_approval(self):
        # A34: a file there, a name that differs only in case, a link in the way.
        self.card.add(A_2001, b'card a')
        self.card.add('2001/b.jpg', b'card b')
        self.card.add('2001/c.jpg', b'card c')
        (self.dest / '2001').mkdir()
        (self.dest / A_2001).write_bytes(b'mine')
        (self.dest / '2001/B.JPG').write_bytes(b'mine too')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
        self.assertEqual((self.dest / A_2001).read_bytes(), b'mine')
        self.assertFalse((self.dest / '2001/b.jpg').exists())
        self.assertEqual((self.dest / '2001/c.jpg').read_bytes(), b'card c')
        self.assertIn('skipped     2001/b.jpg -> b.jpg is there under the name B.JPG', said_text(self))
        # --rename puts each taken one under _renamed/ and says where.
        self.assertEqual(self.run_tool('restore', str(self.dest), '--rename', SEARCH, '2001/b'), 0)
        self.assertIn('renamed     2001/b.jpg -> _renamed/aaaaaaaa-0000-4000-8000-000000000002.jpg', said_text(self))
        # --overwrite names the one file that may be replaced.
        self.assertEqual(self.run_tool('restore', str(self.dest), SEARCH, '2001/a', '--overwrite', A_2001), 0)
        self.assertEqual((self.dest / A_2001).read_bytes(), b'card a')

    def test_a_link_in_the_destination_is_never_followed(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        self.card.add(A_2001, b'card a')
        (self.dest / '2001').symlink_to(outside)
        self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
        self.assertEqual(list(outside.iterdir()), [], 'nothing written through the link')
        self.assertIn('skipped', said_text(self))

    def test_a_restore_over_several_cards_or_after_a_stop_never_copies_twice(self):
        # A35: the progress file keeps what is done; a file that failed is not in it.
        self.card.add(A_JPG, b'one')
        broken = self.card.add('b.jpg', b'two')
        good_bytes = self.card.payload(broken).read_bytes()
        self.card.payload(broken).write_bytes(b'tw0')
        progress = self.tmp / 'progress.json'
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 1)
        done = progress_done(progress)
        self.assertEqual(list(done), [uuid(1, 'bbbbbbbb')])
        first = os.stat(self.dest / A_JPG).st_mtime_ns
        self.card.payload(broken).write_bytes(good_bytes)  # the same file, from another card
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        self.assertIn('done before a.jpg -> a.jpg', said_text(self))
        self.assertIn('restored    b.jpg -> b.jpg', said_text(self))
        self.assertEqual(os.stat(self.dest / A_JPG).st_mtime_ns, first, 'not copied again')
        self.assertEqual(len(progress_done(progress)), 2)

    def test_the_report_names_every_file_and_where_it_went(self):
        self.card.add(X_A, b'a')
        report = self.tmp / 'report.json'
        self.assertEqual(self.run_tool('restore', str(self.dest), '--report', str(report)), 0)
        data = json.loads(report.read_text())
        self.assertEqual(data['card'], 'Example set - 001')
        self.assertEqual(data['files'][0]['original_path'], X_A)
        self.assertEqual(data['files'][0]['where'], X_A)
        # The report never replaces a file that is there: said before anything is restored.
        self.assertEqual(self.run_tool('restore', str(self.dest), '--report', str(report)), 2)
        self.assertIn('is there already', said_text(self))


def version(card, rec, n, mtime_ns, committed_at):
    """Make `rec` (already on `card`) version `n` of its file, seen at `mtime_ns`."""
    rec.update(
        version_id=uuid(n, 'ffffffff'),
        source_mtime_ns=str(mtime_ns),
        committed_at=committed_at,
    )
    folder = card.nb / 'records' / rec['record_id'][:2]
    (folder / (rec['record_id'] + JSON)).write_text(json.dumps(rec) + '\n')
    return rec


OLD_TIME, NEW_TIME = 981173106000000000, 1700000000000000000
DOC = 'docs/plan.txt'


class VersionTests(Base):
    """The review of 0.51.0: a file edited between two backups has two versions in the
    set. The tool restored the oldest under the file's name and skipped the newer one as
    "a file is there already"; over two cards with a progress file, too."""

    def test_the_newest_version_on_a_card_gets_the_files_name(self):
        new = version(self.card, self.card.add(DOC, b'second draft'), 2, NEW_TIME, '2023-11-14T22:13:21Z')
        old = version(self.card, self.card.add(DOC, b'first draft'), 1, OLD_TIME, '2001-02-03T04:05:08Z')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 0, said_text(self))
        self.assertEqual((self.dest / DOC).read_bytes(), b'second draft')
        self.assertIn('older       %s -> a newer version is restored as %s' % (DOC, DOC), said_text(self))
        # With --rename the older one is kept too, under _renamed/.
        self.dest = self.tmp / 'again'
        self.dest.mkdir()
        self.assertEqual(self.run_tool('restore', str(self.dest), '--rename'), 0, said_text(self))
        self.assertEqual((self.dest / DOC).read_bytes(), b'second draft')
        kept = self.dest / RENAMED_DIR / (old['record_id'] + '.txt')
        self.assertEqual(kept.read_bytes(), b'first draft')
        self.assertNotEqual(new['record_id'], old['record_id'])

    def test_over_two_cards_the_newest_wins_in_either_order(self):
        second = Card(self.tmp / 'card2', seq=2, volume=VOLUME_2)
        version(self.card, self.card.add(DOC, b'first draft'), 1, OLD_TIME, '2001-02-03T04:05:08Z')
        version(second, second.add(DOC, b'second draft'), 2, NEW_TIME, '2023-11-14T22:13:21Z')
        for order in ((self.card, second), (second, self.card)):
            dest = self.tmp / ('dest-%d' % order[0].identity['sequence_number'])
            dest.mkdir()
            progress = self.tmp / ('progress-%d.json' % order[0].identity['sequence_number'])
            for card in order:
                self.said = []
                code = tool.main([CARD_FLAG, str(card.top), 'restore', str(dest), '--progress', str(progress)],
                                 say=self.said.append)  # fmt: skip
                self.assertEqual(code, 0, said_text(self))
            with self.subTest(first=order[0].identity['sequence_number']):
                self.assertEqual((dest / DOC).read_bytes(), b'second draft')

    def test_a_file_changed_after_it_was_restored_is_not_replaced(self):
        second = Card(self.tmp / 'card2', seq=2, volume=VOLUME_2)
        version(self.card, self.card.add(DOC, b'first draft'), 1, OLD_TIME, '2001-02-03T04:05:08Z')
        version(second, second.add(DOC, b'second draft'), 2, NEW_TIME, '2023-11-14T22:13:21Z')
        progress = str(self.tmp / 'progress.json')
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', progress), 0)
        (self.dest / DOC).write_bytes(b'my own edit')
        self.said = []
        code = tool.main([CARD_FLAG, str(second.top), 'restore', str(self.dest), '--progress', progress],
                         say=self.said.append)  # fmt: skip
        self.assertEqual(code, 1)
        self.assertEqual((self.dest / DOC).read_bytes(), b'my own edit')

    def test_a_second_copy_of_the_same_version_is_no_failure(self):
        """A version whose first copy was damaged gets a new copy, on the same card too."""
        first = version(self.card, self.card.add(DOC, b'the text'), 1, OLD_TIME, '2001-02-03T04:05:08Z')
        version(self.card, self.card.add(DOC, b'the text'), 1, OLD_TIME, '2002-02-03T04:05:08Z')
        self.card.payload(first).write_bytes(b'the tEXt')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 0, said_text(self))
        self.assertEqual((self.dest / DOC).read_bytes(), b'the text')


RENAMED_DIR = '_renamed'


class ManyFilesTests(Base):
    """The review of 0.51.0: every file restored listed its whole destination folder (to
    find a name that differs only in case), so a folder of 8,000 photos took 23 seconds
    and one of 100,000 about an hour. Each folder is now listed once per restore."""

    def test_a_folder_is_listed_once_however_many_files_go_in(self):
        for n in range(300):
            self.card.add('DCIM/IMG_%04d.jpg' % n, b'x')
        listed = []
        real = os.listdir

        def counting(path='.'):
            listed.append(path)
            return real(path)

        with mock.patch.object(tool.os, 'listdir', side_effect=counting):
            self.assertEqual(self.run_tool('restore', str(self.dest)), 0)
        self.assertEqual(len(list((self.dest / 'DCIM').iterdir())), 300)
        self.assertLess(len(listed), 20, 'listings for 300 files')

    def test_the_progress_file_is_added_to_not_written_again_for_every_file(self):
        """The same review: the whole progress file was written again after every file, so
        8,000 files with --progress took seven minutes."""
        for n in range(300):
            self.card.add('DCIM/IMG_%04d.jpg' % n, b'x')
        progress = self.tmp / 'progress.json'
        real = os.replace
        replaced = []

        def counting(*args, **kwargs):
            replaced.append(args)
            return real(*args, **kwargs)

        with mock.patch.object(tool.os, 'replace', side_effect=counting):
            self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        self.assertLessEqual(len(replaced), 1)
        self.assertEqual(len(progress_done(progress)), 300)
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        self.assertIn('done before 300', said_text(self))

    def test_a_progress_file_of_the_earlier_tool_is_read_and_carried_on(self):
        self.card.add(A_JPG, b'one')
        self.card.add('b.jpg', b'two')
        progress = self.tmp / 'progress.json'
        self.assertEqual(self.run_tool('restore', str(self.dest), SEARCH, A_JPG, '--progress', str(progress)), 0)
        old = {'format': tool.PROGRESS_FORMAT, 'version': 1, 'done': progress_done(progress)}
        progress.write_text(json.dumps(old, indent=1) + '\n')
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        self.assertIn('done before a.jpg -> a.jpg', said_text(self))
        self.assertEqual(len(progress_done(progress)), 2)

    def test_a_line_cut_off_by_a_stop_is_not_counted(self):
        self.card.add(A_JPG, b'one')
        progress = self.tmp / 'progress.json'
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        with open(progress, 'a') as f:
            f.write('{"version_id": "cut off')
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 0)
        self.assertIn('done before a.jpg', said_text(self))
        # A broken line in the middle is no cut-off: the file is refused.
        progress.write_text(progress.read_text().replace('{"version_id": "cut off', '') + 'garbage\n{}\n')
        self.assertEqual(self.run_tool('restore', str(self.dest), '--progress', str(progress)), 2)
        self.assertIn('not usable', said_text(self))

    def test_a_name_an_exfat_stick_cannot_hold_is_skipped_or_renamed(self):
        """Seen restoring onto exFAT (through FUSE): a name with ":" or "?" failed with "No
        such file or directory", --rename or not. exFAT through FUSE says ENOENT to such a
        name, Linux's own FAT and exFAT EINVAL; neither has links (EPERM)."""
        self.card.add('DCIM/meeting 10:30.jpg', b'colon')
        self.card.add('DCIM/fine.jpg', b'fine')
        real_rename, real_link = os.rename, os.link

        def like_exfat(src, dst, **kwargs):
            if ':' in dst:
                raise FileNotFoundError(2, 'No such file or directory')
            return real_rename(src, dst, **kwargs)

        def no_links(*args, **kwargs):
            raise PermissionError(1, 'Operation not permitted')

        with mock.patch.object(tool.os, 'rename', like_exfat), mock.patch.object(tool.os, 'link', no_links):
            self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
            self.assertIn(
                'skipped     DCIM/meeting 10:30.jpg -> this destination cannot hold the name', said_text(self)
            )
            self.assertEqual((self.dest / 'DCIM/fine.jpg').read_bytes(), b'fine')
            self.assertEqual(self.run_tool('restore', str(self.dest), '--rename'), 0, said_text(self))
        self.assertIn('renamed     DCIM/meeting 10:30.jpg -> _renamed/', said_text(self))
        self.assertEqual([p.read_bytes() for p in (self.dest / RENAMED_DIR).iterdir()], [b'colon'])
        self.assertEqual([p for p in self.dest.rglob('.neko-restore-*')], [])

    def test_a_file_already_restored_counts_as_done_without_a_progress_file(self):
        """Seen on the same stick: the same restore run again (no --progress) skipped every
        file it had restored, or with --rename made a second copy of each."""
        self.card.add(A_2001, b'card a')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 0)
        self.assertEqual(self.run_tool('restore', str(self.dest), '--rename'), 0, said_text(self))
        self.assertIn('same file   2001/a.jpg -> 2001/a.jpg', said_text(self))
        self.assertFalse((self.dest / RENAMED_DIR).exists())
        # A file of the same name with other bytes is still not the same file.
        (self.dest / A_2001).write_bytes(b'card A')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
        self.assertIn('skipped     2001/a.jpg -> a file is there already', said_text(self))

    def test_a_name_restored_earlier_in_the_same_run_still_clashes_by_case(self):
        self.card.add('DCIM/IMG_1.JPG', b'upper')
        self.card.add('DCIM/img_1.jpg', b'lower')
        self.assertEqual(self.run_tool('restore', str(self.dest)), 1)
        self.assertEqual([p.name for p in (self.dest / 'DCIM').iterdir()], ['IMG_1.JPG'])
        self.assertIn('img_1.jpg is there under the name IMG_1.JPG', said_text(self))


class WhereTests(Base):
    def snapshot(self, lines):
        body = b''.join(json.dumps(line).encode() + b'\n' for line in lines)
        sid = uuid(7, 'ffffffff')
        (self.card.nb / 'inventory' / (sid + VERSIONS)).write_bytes(body)
        header = {
            'schema_version': 1,
            'snapshot_id': sid,
            'catalogue_epoch_id': uuid(1, '99999999'),
            'revision': 3,
            'exported_at': '2001-02-03T05:00:00Z',
            'backup_set_id': SET,
            'set_display_name': 'Example set',
            'written_to_volume_id': VOLUME,
            'includes_transactions_through': '2001-02-03T04:59:00Z',
            'sources': [],
            'volumes': [
                {'volume_id': VOLUME, 'sequence_number': 1, 'state': 'in_use'},
                {'volume_id': VOLUME_2, 'sequence_number': 2, 'state': 'in_use'},
            ],
            'versions_file': {
                'name': sid + VERSIONS,
                'lines': len(lines),
                'byte_size': len(body),
                'sha256': hashlib.sha256(body).hexdigest(),
            },
        }
        (self.card.nb / 'inventory' / (sid + JSON)).write_text(json.dumps(header))
        return sid

    def test_the_card_needed_is_named_from_the_inventory(self):
        self.snapshot(
            [
                {
                    'version_id': uuid(1, 'bbbbbbbb'),
                    'source_id': SOURCE,
                    'path': '2003/beach.jpg',
                    'byte_size': 5,
                    'sha256': '0' * 64,
                    'copies': [
                        {
                            'record_id': uuid(9),
                            'volume_id': VOLUME_2,
                            'state': 'committed',
                        }
                    ],
                }
            ]
        )
        self.assertEqual(self.run_tool('where', 'beach'), 0)
        self.assertIn('2003/beach.jpg (5 bytes): Example set - 002 (in_use): committed', said_text(self))
        self.assertIn('a card written later may know more', said_text(self))

    def test_a_tampered_inventory_is_refused(self):
        sid = self.snapshot([{'path': 'x.jpg', 'copies': []}])
        (self.card.nb / 'inventory' / (sid + VERSIONS)).write_bytes(b'{"path": "y.jpg"}\n')
        self.assertEqual(self.run_tool('where', 'x'), 1)
        self.assertIn('does not match its header', said_text(self))


if __name__ == '__main__':
    unittest.main()
