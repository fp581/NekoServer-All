"""Job D of the Drives page (1.0.1 contract §7): a mirror that lost a drive gets its
replacement from the dashboard.

storage_jobs.py runs it as root (`run` for the job, `resume` at boot, `offers` for its look)
and hands it its own two functions: `refusal(disk_row, minimum_bytes)` and `erase(by_id,
serial, size_bytes, layout, like, label)`. This file has no refusal list and writes no
partition table of its own: the drive is checked and laid out there, by the one entry point.

- mdadm: the drive laid out like a member that is still there (`like`), then `mdadm <md>
  --add <it>`. The rebuild runs on by itself (and on after a restart): the job ends once it
  has started, and the dashboard shows the array's "rebuilding x %".
- Btrfs: `btrfs replace start <missing devid> <it> <mount>`, waited for, then the balance
  RECOVERY.md names (mirrors anything written while the drive was missing). A power cut
  during either: the boot job starts this job again, which waits for the replace (Btrfs goes
  on with it by itself) and runs the balance.

Commands are lists, never a shell. Tests give `run` and the /sys folder: no disk is touched.
"""

import os
import re
import time
from pathlib import Path

from neko_common import SHARED

FACTS = SHARED['storage_jobs']
ARRAY = re.compile(r'(md)-([A-Za-z0-9_-]{1,64})|(btrfs)-([0-9a-fA-F-]{8,64})')
BALANCE = ['-dconvert=raid1,soft', '-mconvert=raid1,soft']
# md's sync_action: only an idle (or frozen) array takes a new member; recover, resync,
# repair, reshape, check, or a word not known yet, counts as at work (nothing offered).
MD_QUIET = ('idle', 'frozen')
# A partition table needs room beyond the partition itself (GPT's copy at the end).
TABLE_ROOM = 1 << 20
# mdadm metadata 0.90 and 1.0 keep their superblock at the end of a member, past the data.
END_ROOM = 4 << 20
END_METADATA = ('0.90', '1.0')
# The longest a look at a Btrfs replace may take: a hung one never holds the job for ever.
STATUS_LIMIT = 120
# How many looks 10 s apart a replace may still say "Never started" after its start.
NEVER_LOOKS = 30
# How many starts a Btrfs rebuild's journal may hold the page while its state can't be read.
HOLD_RUNS = 3
# `btrfs replace status` words, in the order they are looked for, and the states that are over.
REPLACE_WORDS = (
    ('never started', 'never'),
    ('finished on', 'finished'),
    ('canceled on', 'canceled'),
    ('cancelled on', 'canceled'),
    ('suspended on', 'suspended'),
    ('% done', 'running'),
)
OVER = ('never', 'finished', 'canceled')
# What a replace that is over left behind when the pool still misses a drive.
NOT_WHOLE = {
    'never': 'Btrfs did not start copying onto the new drive, so nothing was changed in the pool. The new drive was erased; see RECOVERY.md, "A drive of a Btrfs pool failed".',
    'canceled': 'The copy onto the new drive was cancelled, so the pool still misses a drive. Add the new drive again from the Drives page.',
    'suspended': 'Btrfs paused the copy onto the new drive. Restart the server: Btrfs goes on with it when the pool opens, and the dashboard shows the pool rebuilding until it is done.',
    'finished': 'Btrfs says the copy ended, but the pool still misses a drive, so your data is not on two drives yet. See RECOVERY.md, "A drive of a Btrfs pool failed".',
}
# Kernel names and Btrfs's numbers are ASCII digits (str.isdigit() also takes "²").
DIGITS = '0123456789'


def line(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def number(path):
    try:
        return int(line(path) or '')
    except ValueError:
        return None


# ---- what a mirror needs ---------------------------------------------------------------


def md_array(sys_block, name):
    """An mdadm array as /sys says it: degraded or not, a member still there (`like`, the
    layout the new drive copies), and the room the new drive needs."""
    md = Path(sys_block) / name / 'md'
    if not md.is_dir():
        return None
    action = line(md / 'sync_action') or 'idle'
    members = md_members(md)
    component = number(md / 'component_size')  # KiB each member holds
    need = None
    like = None
    if members and component:
        kname, offset = members[0]
        like = f'/dev/{kname}'
        need = md_need(sys_block, md, kname, offset, component)
    return {
        'id': f'md-{name}',
        'kind': 'md',
        'dev': f'/dev/{name}',
        'degraded': (number(md / 'degraded') or 0) > 0,
        'rebuilding': action not in MD_QUIET,
        'like': like,
        'min_bytes': need,
    }


def md_members(md):
    """The members still working: (kernel name, data offset in sectors)."""
    members = []
    for dev in sorted(md.glob('dev-*')):
        if 'faulty' in (line(dev / 'state') or ''):
            continue
        members.append((dev.name[len('dev-') :], number(dev / 'offset') or 0))
    return members


def disk_of(kname):
    """The disk a partition's kernel name belongs to: sdb1 → sdb, nvme0n1p2 → nvme0n1,
    mmcblk0p1 → mmcblk0 (the "p" only after a digit, so sdp1 → sdp). Plain string steps, no
    regular expression: a name read from /sys never costs more than one pass."""
    base = kname.rstrip(DIGITS)
    if base == kname:
        return kname
    if base.endswith('p') and len(base) > 1 and base[-2] in DIGITS:
        return base[:-1]
    return base


def percent_done(said):
    """The x of "x% done" in a replace status, or None."""
    end = said.find('% done')
    if end < 0:
        return None
    start = end
    while start > 0 and said[start - 1] in DIGITS + '.':
        start -= 1
    try:
        return float(said[start:end])
    except ValueError:
        return None


def md_need(sys_block, md, kname, offset, component):
    """The bytes a new member needs, laid out like the member `kname`."""
    partition = Path(sys_block) / disk_of(kname) / kname
    start = number(partition / 'start') if partition.is_dir() else None
    if start is not None:
        # A partition: the new drive gets one of the same size at the same place.
        return (start + (number(partition / 'size') or 0)) * 512 + TABLE_ROOM
    version = (line(md / 'metadata_version') or '').split(':')[-1]
    return offset * 512 + component * 1024 + (END_ROOM if version in END_METADATA else 0)


def btrfs_pool(run, uuid):
    """A Btrfs pool by its UUID: the missing device's number and size, a device still there,
    and where the pool is mounted (it must be, degraded, to be repaired)."""
    rc, said = run(['btrfs', 'filesystem', 'show', '--raw', uuid])
    if rc != 0:
        return None
    missing, present = None, None
    for found in re.finditer(r'devid\s+(\d+)\s+size\s+(\d+)\s+used\s+\d+\s+path\s+(.*)', said):
        devid, size, path = int(found[1]), int(found[2]), found[3].strip()
        if 'MISSING' in path or path.startswith('<missing'):
            missing = missing or (devid, size)
        elif present is None and path.startswith('/dev/'):
            present = path
    rc, mounted = run(['findmnt', '-n', '-o', 'TARGET', '--source', f'UUID={uuid}'])
    mount = mounted.splitlines()[0].strip() if rc == 0 and mounted.strip() else None
    # A replace at work still lists the missing device until it ends: ask Btrfs itself.
    # Anything but a replace that is over (or never was) counts as at work.
    rebuilding = False
    if mount:
        rc, said = run(['btrfs', 'replace', 'status', '-1', mount], timeout=STATUS_LIMIT)
        rebuilding = replace_state(rc, said) not in OVER
    return {
        'id': f'btrfs-{uuid}',
        'kind': 'btrfs',
        'dev': None,
        'degraded': missing is not None,
        'rebuilding': rebuilding,
        'devid': missing[0] if missing else None,
        'like': present,
        'mount': mount,
        'min_bytes': missing[1] if missing else None,
    }


def find(array_id, run, sys_block):
    found = ARRAY.fullmatch(array_id or '')
    if not found:
        return None
    if found[1]:
        return md_array(sys_block, found[2])
    return btrfs_pool(run, found[4])


def degraded(run, sys_block):
    """The mirrors that lost a drive and are not being rebuilt: mdadm's from /sys, Btrfs
    pools from `btrfs filesystem show`."""
    out = []
    for md in sorted(Path(sys_block).glob('md*')):
        array = md_array(sys_block, md.name)
        if array:
            out.append(array)
    rc, said = run(['btrfs', 'filesystem', 'show', '--raw'])
    if rc == 0:
        for uuid in re.findall(r'uuid:\s*([0-9a-fA-F-]{8,64})', said):
            pool = btrfs_pool(run, uuid)
            if pool:
                out.append(pool)
    return [a for a in out if a['degraded'] and not a['rebuilding']]


def offers(disks, arrays, refusal):
    """look.json's `mirror_offers`: for each mirror that lost a drive, which disks may be its
    replacement, by storage_jobs' one refusal function with the room the mirror needs."""
    out = {}
    for array in arrays:
        rows = []
        for disk in disks:
            refused = (
                refusal(disk, array['min_bytes'] or 0)
                if array['min_bytes']
                else {
                    'code': 'unknown',
                    'words': 'The mirror does not say how much room its drives need.',
                }
            )
            rows.append(
                {
                    'by_id': disk.get('by_id'),
                    'ok': refused is None,
                    'words': None if refused is None else refused['words'],
                }
            )
        out[array['id']] = rows
    return out


# ---- the job ------------------------------------------------------------------------------


def why_not(array):
    """Why this mirror takes no replacement now, or None."""
    if array is None:
        return 'That mirror is not on this server (any more). Press Look again.'
    if array['rebuilding']:
        return 'This mirror is already being rebuilt. Wait until the dashboard shows it healthy.'
    if not array['degraded']:
        return 'This mirror has all its drives, so it needs no replacement.'
    if not array['like'] or not array['min_bytes']:
        return 'No drive of this mirror is left to copy from. Its data cannot be rebuilt from the dashboard; see RECOVERY.md.'
    if array['kind'] == 'btrfs' and not array['mount']:
        return 'The pool is not open. Restart the server (it opens the pool on the drives left), then try again.'
    return None


def run_job(request, ctx):
    """The job from request.json. Returns result.json's fields (state, message, advice)."""
    array = find(request.get('array'), ctx.run, ctx.sys_block)
    refused = why_not(array)
    if refused:
        return {'state': 'failed', 'message': refused, 'advice': None}
    disk = request['disk']
    ctx.journal('erase', committed=False, undo={'disk': disk})
    ctx.progress('erase')
    try:
        target = ctx.erase(
            disk['by_id'],
            disk['serial'],
            disk['size_bytes'],
            layout='mirror_member',
            like=array['like'],
            minimum_bytes=array['min_bytes'],
        )
    except ctx.refused as e:
        ctx.clear()
        return {'state': 'failed', 'message': str(e), 'advice': None}
    # The new member by its stable name: kernel names change across a restart.
    member = member_name(disk['by_id'], target, ctx.dev)
    if member is None:
        # Without its stable name, a restart could not tell whether the add happened.
        ctx.clear()
        return {
            'state': 'failed',
            'message': 'The new drive was erased, but the server could not find it again by its name, so nothing was changed in it. Restart the server, then try again.',
            'advice': None,
        }
    ctx.journal('add', committed=False, undo={'disk': disk, 'member': member})
    ctx.progress('add')
    return add(array, target, member, ctx)


def resolve(dev, name):
    """The kernel name a /dev/disk/by-id name leads to now, or None."""
    if not name:
        return None
    try:
        return Path(os.readlink(Path(dev) / 'disk' / 'by-id' / name)).name
    except OSError:
        return None


def member_name(by_id, target, dev):
    """The by-id name of what erase laid out: the disk, or its partition ("-partN")."""
    disk = resolve(dev, by_id)
    name = Path(target).name
    if disk is None:
        return None
    if name == disk:
        return by_id
    found = re.fullmatch(re.escape(disk) + r'p?(\d+)', name)
    part = f'{by_id}-part{found[1]}' if found else None
    # Only a name resume can follow back to this very partition.
    return part if part and resolve(dev, part) == name else None


def add(array, target, member, ctx):
    if array['kind'] == 'md':
        rc, said = ctx.run(['mdadm', array['dev'], '--add', target])
        if rc != 0:
            ctx.clear()
            return {
                'state': 'failed',
                'message': 'The mirror did not take the new drive, so nothing was changed in it. The new drive was erased.',
                'advice': (said.strip().splitlines() or [''])[-1][:300] or None,
            }
        ctx.clear()
        return {
            'state': 'done',
            'message': 'The new drive is in the mirror, and your data is being copied onto it. The dashboard shows how far; keep the server on until it finishes (a restart in between is fine).',
            'advice': None,
        }
    rc, said = ctx.run(['btrfs', 'replace', 'start', str(array['devid']), target, array['mount']])
    if rc != 0:
        ctx.clear()
        return {
            'state': 'failed',
            'message': 'The pool did not take the new drive, so nothing was changed in it. The new drive was erased.',
            'advice': (said.strip().splitlines() or [''])[-1][:300] or None,
        }
    ctx.journal('rebuild', committed=True, undo={'array': array['id'], 'member': member})
    return finish_btrfs(array, member, ctx)


def last_words(said):
    return (said.strip().splitlines() or [''])[-1][:300] or 'nothing'


def replace_state(rc, said):
    """What `btrfs replace status -1` says (btrfs-progs cmds/replace.c, one line per state):
    'never' ("Never started"), 'running' ("x% done"), 'finished' ("Started on ..., finished
    on ..."), 'canceled' ("..., canceled on ... at x%"), 'suspended' ("..., suspended on ...
    at x%"), or 'unknown' (no answer, or one not in that list)."""
    if rc != 0:
        return 'unknown'
    text = said.lower()
    for words, state in REPLACE_WORDS:
        if words in text:
            return state
    return 'unknown'


def wait_replace(array, ctx):
    """Follow Btrfs's replace until it is over: (its state, what Btrfs said).

    `btrfs replace start` without -B returns before the kernel has begun (or refused) the
    replace, so "Never started" right after it means "not yet", and a suspended one may go on
    by itself: only one that stays so for NEVER_LOOKS looks is taken as it is."""
    waits = 0
    while True:
        rc, said = ctx.run(['btrfs', 'replace', 'status', '-1', array['mount']], timeout=STATUS_LIMIT)
        state = replace_state(rc, said)
        if state == 'running':
            done = percent_done(said)
            if done is not None:
                ctx.progress('rebuild', percent=done)
        elif state in ('never', 'suspended') and waits < NEVER_LOOKS:
            waits += 1
        else:
            return state, said
        ctx.sleep(10)


def read_errors(said):
    """The uncorrectable read and write errors a finished replace reports (0 when not said)."""
    errors = 0
    for part in said.split(','):
        words = part.split()
        # ", N write errs, M uncorr. read errs" (plain string steps, no regular expression)
        if len(words) >= 2 and words[0].isdecimal() and words[1] in ('write', 'uncorr.'):
            errors += int(words[0])
    return errors


def healed(array, member, ctx):
    """Whether the pool now misses no device and holds the new one."""
    kname = resolve(ctx.dev, member)
    rc, said = ctx.run(['btrfs', 'filesystem', 'show', '--raw', array['id'][len('btrfs-') :]])
    return (
        kname is not None
        and rc == 0
        and 'missing' not in said.lower()
        and re.search(rf'path\s+/dev/{re.escape(kname)}\s*$', said, re.MULTILINE) is not None
    )


def finish_btrfs(array, member, ctx):
    """Wait for Btrfs's replace, then mirror what was written meanwhile (RECOVERY.md).
    Done only on the kernel's own "finished" and a pool that misses no device."""
    how, said = wait_replace(array, ctx)
    if how == 'unknown':
        # Btrfs may well go on copying: the journal stays, so no other job is taken, and
        # the next start finishes this one (`finish`).
        return {
            'state': 'failed',
            'message': 'The copy onto the new drive could not be followed. Btrfs goes on with it by itself; restart the server, and the dashboard then finishes it.',
            # The journal holds every other Drives job until this finishes: if it never
            # does, the person can look for themselves.
            'advice': (
                f'If this comes back after a restart, run `sudo btrfs replace status {array["mount"]}` '
                '(RECOVERY.md, "A drive of a Btrfs pool failed"). Btrfs said: ' + last_words(said)
            ),
        }
    # Every other state is over for this job: the journal goes, so the page is never held.
    ctx.clear()
    # A pool whole again counts, whatever the replace record says: a cut after the replace
    # ended leaves no record at the next mount ("Never started").
    if not healed(array, member, ctx):
        return {'state': 'failed', 'message': NOT_WHOLE[how], 'advice': 'Btrfs said: ' + last_words(said)}
    ctx.progress('rebuild', percent=100.0)
    return balanced(array, read_errors(said), ctx)


def balanced(array, errors, ctx):
    """The balance after a replace, and the result it ends with."""
    rc, said = ctx.run(['btrfs', 'balance', 'start', *BALANCE, array['mount']])
    if errors:
        return {
            'state': 'done',
            'message': f'The new drive is in the pool, but {errors} pieces could not be read from the drive left, so some files may be damaged. Check the pool as RECOVERY.md says.',
            'advice': f'Run `sudo btrfs scrub start -B {array["mount"]}` to find which files.',
        }
    if rc != 0:
        return {
            'state': 'done',
            'message': 'The new drive is in the pool. Mirroring what was written while the drive was missing did not finish; restart the server, then run the balance RECOVERY.md names.',
            'advice': last_words(said),
        }
    return {
        'state': 'done',
        'message': 'The new drive is in the pool and your data is on both drives again. Restart the server once, so the pool opens the ordinary way.',
        'advice': None,
    }


def resume(journal, ctx):
    """At boot, a job cut off by a power cut: before the add, nothing was changed in the
    mirror (undone); after it, the rebuild goes on by itself (resumed). A Btrfs rebuild cut
    off (committed, so storage_jobs calls `finish` itself) is finished the same way here."""
    request = journal.get('request') or {}
    undo = journal.get('undo') or {}
    phase = journal.get('phase')
    if phase == 'rebuild':
        return finish(journal, ctx)
    array = find(request.get('array'), ctx.run, ctx.sys_block)
    member = array is not None and is_member(array, undo.get('member'), ctx)
    ctx.clear()
    if member:
        return {
            'state': 'resumed',
            'message': 'The server stopped while the new drive was added. It is in the mirror, and the copy onto it goes on by itself; the dashboard shows how far.',
            'advice': None,
            'power_cut': True,
        }
    erased = phase == 'add'
    return {
        'state': 'undone',
        'message': 'The server stopped before the new drive was added, so nothing was changed in the mirror.'
        + (
            ' The new drive was erased; add it again from the Drives page.'
            if erased
            else ' Try again from the Drives page.'
        ),
        'advice': None,
        'power_cut': True,
    }


def finish(journal, ctx):
    """A Btrfs rebuild started before a restart: waited for and balanced. Nothing is erased
    and no replace is started. A pool not open, or a replace that cannot be read, keeps the
    journal for the next start, but for HOLD_RUNS starts at most: the page is never held for
    good (a replace still at work keeps the pool shown as rebuilding, so nothing is offered)."""
    undo = journal.get('undo') or {}
    runs = undo.get('runs', 0) + 1
    if runs > HOLD_RUNS:
        ctx.clear()
        return {
            'state': 'failed',
            'message': f'The copy onto the new drive could not be followed in {HOLD_RUNS} starts, so the Drives page lets go of it. See RECOVERY.md, "A drive of a Btrfs pool failed".',
            'advice': 'Run `sudo btrfs replace status <the pool folder>` to see how it stands.',
        }
    ctx.journal('rebuild', committed=True, undo={**undo, 'runs': runs})
    array = find((journal.get('request') or {}).get('array'), ctx.run, ctx.sys_block)
    if array is None or array['kind'] != 'btrfs' or not array['mount']:
        return {
            'state': 'failed',
            'message': 'The pool is not open, so the copy onto the new drive cannot be finished now. Restart the server once more.',
            'advice': None,
        }
    return finish_btrfs(array, undo.get('member'), ctx)


def is_member(array, member, ctx):
    """Whether the new member (its by-id name) is in the mirror now."""
    kname = resolve(ctx.dev, member)
    if kname is None:
        return False
    if array['kind'] == 'md':
        return (Path(ctx.sys_block) / Path(array['dev']).name / 'md' / f'dev-{kname}').is_dir()
    rc, said = ctx.run(['btrfs', 'filesystem', 'show', '--raw', array['id'][len('btrfs-') :]])
    return rc == 0 and re.search(rf'path\s+/dev/{re.escape(kname)}\s*$', said, re.MULTILINE) is not None


class Context:
    """What the job is given by storage_jobs: its runner, its erase and the exception that
    erase raises, its journal, progress and clear (journal removed), the /sys and /dev
    folders, and a clock (tests give their own). `run(argv, timeout=None)` never uses a shell."""

    def __init__(
        self, run, erase, refused, journal, progress, clear, sys_block='/sys/block', dev='/dev', sleep=time.sleep
    ):
        self.run = run
        self.refused = refused  # the exception storage_jobs' erase raises, in the person's words
        self.erase = erase
        self.journal = journal
        self.progress = progress
        self.clear = clear
        self.sys_block = sys_block
        self.dev = dev
        self.sleep = sleep
