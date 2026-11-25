#!/usr/bin/env python3
"""Storage from the dashboard (1.0.1): the root jobs behind the Drives page.

The control service checks a request from the page, writes request.json and starts
nekoserver-storage.service, which runs this tool as root (contract:
plans/1.0.1-contract.md; names and words in shared.json "storage_jobs"):

  storage_jobs.py run --config <my-config.json> --request <request.json>
  storage_jobs.py resume --config <my-config.json>      at boot: finish or undo a cut-off job

This file holds the one list of reasons a disk is never offered (`refusal`) and the one
way a disk is erased (`erase`): the mirror's rebuild (mirror_add.py) uses both, and no
other code writes a partition table or a filesystem."""

import json
import re
import os
import sqlite3
import time
from pathlib import Path

from neko_common import SHARED

# Disks are told apart by their names under /dev/disk/by-id, never by /dev/sdX alone.
BY_ID = Path('/dev/disk/by-id')
SYS_BLOCK = Path('/sys/class/block')
# A name under by-id that stands for a partition, not a whole disk.
PART_SUFFIX = '-part'
# Filesystems a drive may be taken over with as it is.
FILESYSTEMS = ('ext4', 'xfs', 'btrfs')
# Signatures of a disk that is part of a set (RAID, LVM, encryption, ZFS): erasing one of
# them would break that set, assembled or not.
SET_SIGNATURES = ('linux_raid_member', 'LVM2_member', 'crypto_LUKS', 'zfs_member', 'ddf_raid_member', 'isw_raid_member')
# What Linux itself runs from (as migrate.SYSTEM_MOUNTS, swap apart).
SYSTEM_MOUNTS = {'/', '/boot', '/boot/efi', '/efi', '/usr', '/var', '/home'}
LSBLK = 'NAME,KNAME,PATH,TYPE,SIZE,SERIAL,MODEL,TRAN,UUID,PARTUUID,FSTYPE,LABEL,MOUNTPOINTS,PKNAME,MAJ:MIN,FSUSED'
# Under the test flag only loop devices are disks, and only they may ever be erased.
TEST_FLAG = 'NEKOSERVER_STORAGE_TEST'

WORDS = SHARED.get('storage_jobs', {}).get('role_words', {})


def testing():
    return os.environ.get(TEST_FLAG) == '1'


# ---- what is plugged in ------------------------------------------------------------------


def walk(rows, parent=None):
    for row in rows:
        yield row, parent
        yield from walk(row.get('children') or [], row)


def by_id_names(folder=BY_ID):
    """{kernel name (sdc): [its names under by-id]}, whole disks only."""
    names = {}
    try:
        entries = sorted(os.listdir(folder))
    except OSError:
        return names
    for entry in entries:
        if PART_SUFFIX in entry:
            continue
        target = os.path.basename(os.path.realpath(os.path.join(folder, entry)))
        names.setdefault(target, []).append(entry)
    return names


def preferred(names):
    """The by-id name a disk is shown and asked for by: the most telling kind first."""
    for prefix in ('nvme-', 'ata-', 'usb-', 'scsi-', 'mmc-', 'virtio-', 'wwn-', 'nvme-eui.'):
        for name in names:
            if name.startswith(prefix) and not name.startswith('nvme-eui.'):
                return name
    return names[0] if names else None


def holders(kname, sys_block=SYS_BLOCK):
    try:
        return sorted(os.listdir(Path(sys_block) / kname / 'holders'))
    except OSError:
        return []


def holders_of(world, kname):
    """What Linux built on this device (an md array, LVM, an open encrypted drive)."""
    if world.get('holders') is not None:
        return world['holders'].get(kname, [])
    return holders(kname)


def mounted_devices(proc=Path('/proc')):
    """ "major:minor" of every device mounted anywhere: this namespace, binds, and the
    containers' own (each process's mountinfo)."""
    found = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdecimal()]
    except OSError:
        pids = []
    for name in ['self', *pids]:
        try:
            text = (Path(proc) / name / 'mountinfo').read_text()
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) > 2:
                found.add(parts[2])
    return found


def swap_devices(swaps=Path('/proc/swaps')):
    try:
        lines = Path(swaps).read_text().splitlines()[1:]
    except OSError:
        return set()
    return {os.path.realpath(line.split()[0]) for line in lines if line.split()}


def card_uuids(config):
    """The filesystems of the backup cards the catalogue enrolled (0.52's set), read only."""
    from neko_common import mount_path

    try:
        data = os.path.realpath(mount_path(config.get('data_mount') or ''))
    except (RuntimeError, TypeError):
        return set()
    # The catalogue only as a file of the apps drive's own folder (no link out of it).
    catalogue = os.path.realpath(os.path.join(data, SHARED['cards']['folder'], SHARED['cards']['catalogue']))
    if not catalogue.startswith(data + os.sep) or not os.path.isfile(catalogue):
        return set()
    try:
        with sqlite3.connect(f'file:{catalogue}?mode=ro', uri=True) as db:
            return {row[0] for row in db.execute('SELECT filesystem_uuid FROM volumes') if row[0]}
    except sqlite3.Error:
        return set()


def settings_uuids(config):
    """{filesystem UUID: refusal code} of every drive the settings name, plugged in or not."""
    named = {}
    storage = config.get('storage') if isinstance(config.get('storage'), dict) else {}
    for key, record in storage.items():
        if not isinstance(record, dict):
            continue
        if record.get('uuid'):
            named[record['uuid']] = 'role' if key in ('data_ssd', 'media_hdd') else 'settings'
        previous = record.get('previous_drive')
        if isinstance(previous, dict) and previous.get('uuid'):
            named.setdefault(previous['uuid'], 'migrate_old')
    backup = (config.get('photos') or {}).get('external_backup_uuid')
    if backup:
        named.setdefault(backup, 'offline_backup')
    for uuid in card_uuids(config):
        named.setdefault(uuid, 'cards')
    return named


def world_now(config):
    """Everything `describe` reads, as Linux shows it now."""
    import subprocess

    shown = subprocess.run(['lsblk', '-J', '-b', '-o', LSBLK], capture_output=True, text=True, timeout=60, check=True)
    return {
        'lsblk': json.loads(shown.stdout).get('blockdevices', []),
        'by_id': by_id_names(),
        'holders': None,  # read from /sys as each device is described
        'mounted': mounted_devices(),
        'swaps': swap_devices(),
        'named': settings_uuids(config),
    }


def describe(world):
    """One row per disk: what `refusal` needs to know of it, and what the page shows."""
    disk_type = 'loop' if testing() else 'disk'
    counts = {}
    for disk in world['lsblk']:
        counts[identity(disk)] = counts.get(identity(disk), 0) + 1
    world = dict(world, mounted_uuids=mounted_uuids(world))
    rows = []
    for disk in world['lsblk']:
        if disk.get('type') != disk_type:
            continue
        rows.append(disk_row(disk, world, counts))
    return rows


def loop_serial(disk, sys_block=SYS_BLOCK):
    """While testing, a loop device's serial is its backing file's name (it has none)."""
    if not testing() or disk.get('type') != 'loop':
        return ''
    try:
        return Path((Path(sys_block) / disk['kname'] / 'loop' / 'backing_file').read_text().strip()).name
    except (OSError, KeyError):
        return ''


def identity(disk):
    """(serial, model): what tells one disk from another."""
    return serial_of(disk), (disk.get('model') or '').strip()


def serial_of(disk):
    return (disk.get('serial') or '').strip() or loop_serial(disk)


def mounted_uuids(world):
    """The filesystem UUIDs mounted now. A Btrfs pool of several devices is mounted from
    one of them (mountinfo names an anonymous 0:N device): the others carry its UUID."""
    return {
        row['uuid']
        for top in world['lsblk']
        for row, _ in walk([top])
        if row.get('uuid') and (row.get('maj:min') in world['mounted'] or any(row.get('mountpoints') or []))
    }


def in_use(everything, world):
    """Whether any of these devices is mounted (in any namespace), a device of a mounted
    filesystem, or used as swap."""
    held = world.get('mounted_uuids') or set()
    mounted = any(
        r.get('maj:min') in world['mounted'] or any(r.get('mountpoints') or []) or r.get('uuid') in held
        for r in everything
    )
    swap = any(os.path.realpath(r['path']) in world['swaps'] for r in everything if r.get('path'))
    return mounted, swap


def disk_row(disk, world, counts):
    kname = disk.get('kname') or disk.get('name')
    by_id = preferred(world['by_id'].get(kname, []))
    parts = [row for row, _ in walk(disk.get('children') or [])]
    everything = [disk, *parts]
    mounted, swap = in_use(everything, world)
    return {
        'by_id': by_id,
        # Two disks with one serial (some USB cases say the same for every disk) would share
        # their by-id name: neither can be told apart, so neither is offered.
        'unique': bool(by_id) and counts.get(identity(disk)) == 1,
        'serial': serial_of(disk),
        'size_bytes': int(disk.get('size') or 0),
        'model': (disk.get('model') or '').strip(),
        'dev': kname,
        'path': disk.get('path') or f'/dev/{kname}',
        'tran': disk.get('tran'),
        'system': any(SYSTEM_MOUNTS & set(r.get('mountpoints') or []) for r in everything),
        'named': sorted({world['named'][r['uuid']] for r in everything if r.get('uuid') in world['named']}),
        'signatures': sorted({r['fstype'] for r in everything if r.get('fstype') in SET_SIGNATURES}),
        'holders': sorted({h for r in everything for h in holders_of(world, r.get('kname') or r.get('name') or '')}),
        'mounted': mounted,
        'swap': swap,
        'filesystems': [
            {k: r.get(k) for k in ('path', 'fstype', 'label', 'uuid', 'partuuid', 'size', 'fsused')}
            for r in everything
            if r.get('fstype')
        ],
        'partitions': len(parts),
    }


# ---- the one list of refusals ------------------------------------------------------------

NAMED_WORDS = {
    'role': 'NekoServer keeps your apps or your media and backups on it.',
    'settings': 'It is a drive NekoServer already uses (an extra drive in your settings).',
    'offline_backup': 'It is your offline backup drive.',
    'cards': 'It is one of your backup cards.',
    'migrate_old': 'It is the drive a move came from: NekoServer keeps it until you no longer can go back.',
}


def refusal(disk, minimum_bytes):
    """Why this disk (a `describe` row) may never be used, erased or added to a mirror, or
    None. The one list: the Drives page's offers and the mirror's rebuild both come from
    here, and `erase` asks it again right before it writes."""
    if not disk.get('by_id') or not disk.get('unique') or not disk.get('serial'):
        return refused(
            'unknown', 'NekoServer cannot tell this disk apart from every other one for sure (no serial number).'
        )
    if disk.get('system'):
        return refused('system', 'This is the disk the system runs from.')
    for code in ('role', 'settings', 'offline_backup', 'cards', 'migrate_old'):
        if code in disk.get('named', ()):
            return refused(code, NAMED_WORDS[code])
    if 'linux_raid_member' in disk.get('signatures', ()) and disk.get('holders'):
        return refused('mirror_member', 'It is a drive of a RAID array that is running.')
    if disk.get('holders'):
        return refused('holders', 'Linux uses it as part of something else (an array, LVM or an encrypted drive).')
    if disk.get('signatures'):
        return refused(
            'signature',
            'It holds a part of a RAID array, LVM or an encrypted or ZFS drive. Erasing it could break that set, '
            'so NekoServer leaves it alone.',
        )
    if disk.get('swap'):
        return refused('swap', 'Linux uses it as swap.')
    if disk.get('mounted'):
        return refused('mounted', 'It is in use: something on it is mounted.')
    if int(disk.get('size_bytes') or 0) < int(minimum_bytes or 0):
        return refused('small', f'It is too small: at least {size_words(minimum_bytes)} is needed.')
    return None


def refused(code, words):
    return {'code': code, 'words': words}


def size_words(size):
    """A size the way a disk's box says it: 4 TB, 256 GB, 1.5 TB."""
    size = int(size or 0)
    for unit, scale in (('TB', 10**12), ('GB', 10**9), ('MB', 10**6)):
        if size >= scale:
            value = size / scale
            shown = f'{value:.1f}' if value < 10 else f'{value:.0f}'
            return f'{shown.removesuffix(".0")} {unit}'
    return f'{size} bytes'


def confirm_words(disk):
    """What the person types to erase this disk: its size and its serial's last four, so
    two disks of the same size never share them."""
    return f'{size_words(disk["size_bytes"])} {disk["serial"][-4:]}'


def same_words(typed, wanted):
    """The confirm compared as the contract says: case and runs of spaces ignored."""
    return ' '.join(str(typed).split()).casefold() == ' '.join(str(wanted).split()).casefold()


# ---- the one way a disk is erased ----------------------------------------------------------
# Nothing else in NekoServer writes a partition table or makes a filesystem. Before each
# write the disk is found again by its by-id name, its serial and size are read again, the
# refusals are asked again, and it is opened exclusively once (a device something holds or
# has mounted cannot be): a disk swapped or taken into use meanwhile stops the erase.

LABEL = 'neko-drive'
# GPT's type for a Linux filesystem partition.
LINUX_DATA = '0FC63DAF-8483-4772-8E79-3D69D8477DE4'


class Refused(RuntimeError):
    """A disk that may not be written to, with the refusal's code."""

    def __init__(self, code, words):
        super().__init__(words)
        self.code = code


def run(argv, stdin=None, timeout=600):
    import subprocess

    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        said = (done.stderr or done.stdout).strip().splitlines()
        raise RuntimeError(f'{argv[0]} failed: {said[-1] if said else f"exit code {done.returncode}"}')
    return done.stdout


def device_of(by_id, folder=BY_ID):
    """/dev/<name> of the disk with this by-id name, or None when it is not plugged in."""
    if not by_id or '/' in by_id or by_id in ('.', '..'):
        return None
    link = Path(folder) / by_id
    if not link.is_symlink():
        return None
    return os.path.realpath(link)


def recheck(by_id, serial, size_bytes, minimum_bytes, config, folder=BY_ID, look=None):
    """The disk as it is right now, or Refused: the same disk (by-id, serial and size), no
    refusal, and nothing holding it."""
    path = device_of(by_id, folder)
    if not path:
        raise Refused('unknown', 'The disk is not plugged in any more.')
    if testing() != os.path.basename(path).startswith('loop'):
        raise Refused('unknown', 'Only loop devices are written to while testing, and never one otherwise.')
    world = (look or world_now)(config)
    if folder != BY_ID:
        world['by_id'] = by_id_names(folder)
    rows = [row for row in describe(world) if row['dev'] == os.path.basename(path)]
    if len(rows) != 1:
        raise Refused('unknown', 'The disk could not be found again.')
    row = rows[0]
    if row['by_id'] != by_id or row['serial'] != serial or row['size_bytes'] != int(size_bytes):
        raise Refused('unknown', 'Another disk is plugged in where this one was: nothing was erased.')
    why = refusal(row, minimum_bytes)
    if why:
        raise Refused(why['code'], why['words'])
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_EXCL))
    except OSError as error:
        raise Refused('busy', 'Linux is using the disk right now: nothing was erased.') from error
    return row


def settle():
    run(['udevadm', 'settle', '--timeout=60'])


def partition_path(by_id, number, folder=BY_ID):
    """/dev/<partition> number `number` of the disk, once udev has made it."""
    settle()
    link = Path(folder) / f'{by_id}-part{number}'
    if link.is_symlink():
        return os.path.realpath(link)
    disk = device_of(by_id, folder)
    shown = f'{disk}p{number}' if disk[-1].isdecimal() else f'{disk}{number}'
    if not Path(shown).exists():
        raise RuntimeError(f'The new partition {number} of {by_id} did not appear')
    return shown


def member_table(like_disk):
    """The partition table of a mirror's present member, as sfdisk writes it to the new
    disk: the same partitions, with new ids, and its end where the new disk ends."""
    kept = []
    for line in run(['sfdisk', '--dump', like_disk]).splitlines():
        if line.startswith(('label-id:', 'device:', 'last-lba:')):
            continue
        if line.startswith('/dev/'):
            fields = [f.strip() for f in line.split(':', 1)[1].split(',')]
            line = ', '.join(f for f in fields if not f.startswith('uuid='))
        kept.append(line)
    return '\n'.join(kept) + '\n'


def erase(
    by_id,
    serial,
    size_bytes,
    layout='data',
    like=None,
    label=None,
    *,
    config=None,
    minimum_bytes=None,
    journal=None,
    folder=BY_ID,
    look=None,
):
    """Erase the disk named by `by_id` (its `serial` and `size_bytes` as the person saw
    them) and lay it out; returns the device to use: the new ext4 partition (`data`), or
    for a mirror (`mirror_member`, `like` a present member's /dev path) a partition like
    that member's or, when the members are whole disks, the disk itself. `journal(fields)`
    is told each step's new ids before the step writes, so a cut-off erase is recognised
    and never run again. Raises Refused before anything is written to a disk that may not be."""
    import uuid as uuids

    config = config or {}
    minimum = FACTS['min_bytes'] if minimum_bytes is None else minimum_bytes
    tell = journal or (lambda fields: None)

    def checked():
        return recheck(by_id, serial, size_bytes, minimum, config, folder, look)

    row = checked()
    disk = row['path']
    if layout == 'mirror_member':
        return mirror_layout(by_id, disk, like, checked, tell, folder)
    if layout != 'data':
        raise ValueError(f'unknown layout {layout}')
    partuuid, fsuuid = str(uuids.uuid4()), str(uuids.uuid4())
    tell({'phase': 'erase', 'partuuid': partuuid, 'uuid': fsuuid})
    for found in reversed(row['filesystems']):
        if found.get('path') and found['path'] != disk:
            checked()
            run(['wipefs', '--all', found['path']])
    checked()
    run(['wipefs', '--all', disk])
    tell({'phase': 'partition', 'partuuid': partuuid, 'uuid': fsuuid})
    checked()
    run(
        ['sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', disk],
        stdin=f'label: gpt\ntype={LINUX_DATA}, uuid={partuuid}\n',
    )
    part = partition_path(by_id, 1, folder)
    tell({'phase': 'format', 'partuuid': partuuid, 'uuid': fsuuid, 'partition': part})
    checked()
    run(['mkfs.ext4', '-q', '-L', (label or LABEL)[:16], '-U', fsuuid, part])
    settle()
    return part


def mirror_layout(by_id, disk, like, checked, tell, folder):
    """The new disk laid out like a present member `like` of the mirror (/dev path)."""
    if not like:
        raise ValueError('a mirror member is laid out like a present one: name it (like=)')
    # Asked of a running member, lsblk (util-linux 2.42) lists the array it holds first:
    # only the member's own row says how it is laid out. Without one, nothing is erased
    # (the first row would be the array, and the new disk laid out whole).
    rows = json.loads(run(['lsblk', '-J', '-o', 'PATH,TYPE,PKNAME,PARTN', like]))['blockdevices']
    real = os.path.realpath(like)
    shown = next((row for row in rows if os.path.realpath(row.get('path') or '') == real), None)
    if shown is None:
        raise Refused('unknown', f'Could not read how {like} is laid out: nothing was erased.')
    tell({'phase': 'erase', 'like': like})
    checked()
    run(['wipefs', '--all', disk])
    if shown.get('type') != 'part':
        return disk
    number = int(shown['partn'])
    tell({'phase': 'partition', 'like': like})
    checked()
    run(
        ['sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', disk],
        stdin=member_table(f'/dev/{shown["pkname"]}'),
    )
    return partition_path(by_id, number, folder)


# ---- the kinds of data and their places --------------------------------------------------

FACTS = SHARED.get('storage_jobs', {})
FORMAT = 'neko-storage-v1'
STATE = Path(FACTS.get('state', '/etc/nekoserver-backups/storage'))
KINDS = ('backups', 'photos', 'movies', 'music', 'cloud')
KIND_WORDS = {
    'backups': 'backups',
    'photos': 'photos',
    'movies': 'films and shows',
    'music': 'music',
    'cloud': 'Nextcloud files',
}
PLACE_WORDS = {'ssd': 'Apps drive', 'media': 'Media and backups drive'}
# Room left free on the new drive beyond what is moved (as migrate.MARGIN).
MARGIN = 1.05
OLD_COPY = re.compile(r'nekoserver-old-(backups|photos|movies|music|cloud)-([0-9a-f]{16})')


def place_words(place):
    return PLACE_WORDS.get(place) or f'Extra drive {place}'


AWAY_WORDS = {
    'missing': '{drive} is not connected (nothing is mounted at {root}).',
    'other': '{drive} is not the drive NekoServer set up: another disk is mounted at {root}.',
}
PLUG_IN = 'Plug {drive} in (the same drive) and try again.'


class Away(RuntimeError):
    """A drive the job needs is not there (T1-18). Before the commit point the job is
    undone; after it, the journal stays and the start finishes it without that drive."""

    def __init__(self, place, words):
        super().__init__(words)
        self.place, self.advice = place, PLUG_IN.format(drive=place_words(place))


def away(config, place):
    """Why the drive `place` can't be used now, in words, or None (neko_common.drive_away)."""
    from neko_common import drive_away

    why = drive_away(config, place)
    if why is None:
        return None
    try:
        root = root_of(config, place)
    except RuntimeError:
        root = place
    return AWAY_WORDS[why].format(drive=place_words(place), root=root)


def need(job, *places):
    """Each drive the next step reads or writes is there, or Away: checked before a job
    starts and again before each write, as a drive can go away in the middle of a job."""
    for place in dict.fromkeys(p for p in places if p):
        words = away(job.config, place)
        if words:
            raise Away(place, words)


def place_of(config, kind):
    """Where a kind of data is: "ssd", "media" or an extra drive's name."""
    from neko_common import location

    if kind == 'backups':
        return (config.get('locations') or {}).get('backups', 'media')
    return location(config, kind)


def kind_folders(kind):
    """The folders of a kind, as folders of the drive it is on."""
    from placement import BACKUP, DATA_FOLDERS

    return tuple(DATA_FOLDERS[BACKUP if kind == 'backups' else kind])


def inner_names(kind, rel):
    """The names in a kind's folder `rel` that are another kind's folders (T1-20: the films'
    downloads folder holds the music's downloads folder, which qBittorrent needs on the
    music's drive): never copied, compared, retired or moved with it, and never in the
    way of it."""
    names = set()
    for other in KINDS:
        if other != kind:
            for folder in kind_folders(other):
                if folder.startswith(rel + '/'):
                    names.add(folder[len(rel) + 1 :].split('/')[0])
    return tuple(sorted(names))


def kind_skips(kind):
    """inner_names for each folder of a kind, in the order of kind_folders."""
    return [inner_names(kind, rel) for rel in kind_folders(kind)]


def own_entries(folder, skip=()):
    """What a kind's folder holds that is its own (another kind's folder in it left out)."""
    try:
        return [entry for entry in Path(folder).iterdir() if entry.name not in skip]
    except OSError:
        return []


def copy_skip(skip):
    """The keyword migrate.copy and migrate.differences take for `skip` (none when empty)."""
    return {'skip': skip} if skip else {}


def move_folder(source, target, skip=()):
    """A kind's folder moved on one drive: one rename, or, when another kind's folder is in
    it or at the target (`skip`), its own entries one by one, the other kind's left where it
    is. Done again after a power cut, it goes on with what is left."""
    import stat

    source, target = Path(source), Path(target)
    if not skip or (not target.exists() and not any((source / name).exists() for name in skip)):
        os.rename(source, target)
        return
    if not target.exists():
        held = source.stat()
        target.mkdir()
        os.chmod(target, stat.S_IMODE(held.st_mode))
        if os.geteuid() == 0:
            os.chown(target, held.st_uid, held.st_gid, follow_symlinks=False)
    for entry in own_entries(source, skip):
        if (target / entry.name).exists() or (target / entry.name).is_symlink():
            raise RuntimeError(f'{target / entry.name} is in the way: it is there already.')
        os.rename(entry, target / entry.name)
    if not any(source.iterdir()):
        source.rmdir()


def places(config):
    from neko_common import extra_drives

    return ['ssd', 'media', *sorted(extra_drives(config))]


def root_of(config, place):
    from neko_common import drive_folder

    return Path(drive_folder(config, place))


def du(paths):
    """Bytes the folders hold (one filesystem each; a missing folder holds nothing)."""
    import subprocess

    there = [str(p) for p in paths if Path(p).exists()]
    if not there:
        return 0
    shown = subprocess.run(['du', '-s', '-b', '-x', '-c', *there], capture_output=True, text=True, check=False)
    lines = shown.stdout.strip().splitlines()
    return int(lines[-1].split()[0]) if lines else 0


def free_bytes(folder):
    found = os.statvfs(folder)
    return found.f_bavail * found.f_frsize


def same_drive(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def target_problem(config, kind, to, size):
    """Why `kind` (holding `size` bytes) can't move to the place `to`, or None."""
    root = root_of(config, to)
    if kind == 'backups' and to == 'ssd':
        return 'Your backups are copies of the Apps drive: they must be on another drive.'
    gone = away(config, to)
    if gone:
        return gone
    if same_drive(root, root_of(config, place_of(config, kind))):
        return f'It is the drive your {KIND_WORDS[kind]} are on already.'
    need, free = int(size * MARGIN), free_bytes(root)
    if free < need:
        return f'Not enough room: needs {size_words(need)}, {size_words(free)} free.'
    return None


def targets(config, kind, size):
    found = []
    for to in places(config):
        if to == place_of(config, kind):
            continue
        why = target_problem(config, kind, to, size)
        beside = beside_backups(config, kind, to) if why is None else []
        found.append(
            {
                'to': to,
                'words': place_words(to),
                'ok': why is None,
                'why_not': why,
                'beside': beside,
                'beside_words': beside_words(config, beside) if beside else None,
            }
        )
    return found


# What the backups protect on the drive they go to: the photos (their archive) and the
# music (its nightly copy). A backup on the drive it protects is no help when that drive
# is lost (backup_plan.same_drive, the backups page's "same drive").
BACKED_UP = ('photos', 'music')


def backed_up_here(config, kind):
    """This server backs `kind` up onto the backups' drive."""
    from neko_common import media_group, music_backed_up

    return media_group(config, 'photos') if kind == 'photos' else music_backed_up(config)


def on_one_drive(config, a, b):
    try:
        return same_drive(root_of(config, a), root_of(config, b))
    except (RuntimeError, KeyError, TypeError):
        return False


def beside_backups(config, kind, to):
    """The kinds that would be backed up on the drive they are on once `kind` is on `to`
    and are not now: the backups moved onto the photos' or the music's drive, or the
    photos or the music moved onto the backups' drive."""
    if kind == 'backups':
        return [
            k
            for k in BACKED_UP
            if backed_up_here(config, k)
            and on_one_drive(config, place_of(config, k), to)
            and not on_one_drive(config, place_of(config, k), place_of(config, 'backups'))
        ]
    if kind in BACKED_UP and backed_up_here(config, kind):
        backups = place_of(config, 'backups')
        if on_one_drive(config, to, backups) and not on_one_drive(config, place_of(config, kind), backups):
            return [kind]
    return []


def beside_now(config):
    """The kinds backed up on the drive they are on now (the Drives page's "same drive")."""
    backups = place_of(config, 'backups')
    return [k for k in BACKED_UP if backed_up_here(config, k) and on_one_drive(config, place_of(config, k), backups)]


def weekly_copy_drive(config):
    import placement

    try:
        return placement.drive_of(config, placement.OFFLINE, placement.DATA)
    except (KeyError, RuntimeError):
        return None


def beside_words(config, kinds):
    """What the move window says of a move that backs `kinds` up on their own drive."""
    names = ' and '.join(KIND_WORDS[k] for k in kinds)
    said = (
        f'Your {names} would then be backed up on the drive they are on. That backup protects you from a '
        'mistake (a file deleted or changed), not from losing that drive.'
    )
    if 'photos' in kinds:
        said += ' The weekly copy on the USB drive is the one that survives losing it' + (
            '.' if weekly_copy_drive(config) else ', and no USB drive is set up for it yet.'
        )
    if 'music' in kinds:
        said += ' Music is not in the weekly USB copy: losing that drive loses your music and its backup.'
    return said


def old_copies(config, measure=du):
    """The copies a move left behind, on every drive: one folder each."""
    found = []
    for place in places(config):
        try:
            entries = sorted(root_of(config, place).iterdir())
        except OSError:
            continue
        for entry in entries:
            matched = OLD_COPY.fullmatch(entry.name)
            if not matched or entry.is_symlink() or not entry.is_dir():
                continue
            kind, copy = matched.groups()
            found.append(
                {
                    'kind': kind,
                    'copy': copy,
                    'place': place,
                    'place_words': place_words(place),
                    'path': str(entry),
                    'bytes': measure([entry]),
                    'at': entry.stat().st_mtime,
                    'delete_words': f'delete {KIND_WORDS[kind]}',
                }
            )
    return found


def release_problem(config, drive, copies):
    """Why the extra drive `drive` can't be given up now, or None."""
    from neko_common import INSTALL_SECTION

    for kind in KINDS:
        if place_of(config, kind) == drive:
            return f'Your {KIND_WORDS[kind]} are on it. Move them first.'
    if drive in (config.get(INSTALL_SECTION) or {}).values():
        return 'Apps keep their settings on it. Move them in setup first.'
    if any(c['place'] == drive for c in copies):
        return 'An old copy is on it. Delete it or move it back first.'
    if folders_on(config, drive):
        return (
            'Films, shows or music NekoServer uses are in a folder on it. Take it out on the Media folders page first.'
        )
    return None


def folders_on(config, drive):
    """The person's own media folders (setup's existing-media question and the Media folders
    page: "placement" … "extra") that are on this drive: their binds name a path there."""
    from existing_media import listed_extras

    root = Path(os.path.realpath(root_of(config, drive)))
    saved = config.get('placement') if isinstance(config.get('placement'), dict) else {}
    found = []
    for row in ('movies', 'music'):
        for entry in listed_extras(saved, row):
            path = entry.get('path') if isinstance(entry, dict) else entry
            if isinstance(path, str) and root in (Path(os.path.realpath(path)), *Path(os.path.realpath(path)).parents):
                found.append(path)
    return found


# ---- the files the page reads --------------------------------------------------------------


def write_state(name, payload, folder=None):
    from neko_common import atomic_json, ensure_directory

    folder = Path(folder or STATE)
    ensure_directory(folder, 0o700)
    atomic_json(folder / name, {'format': FORMAT, **payload})


def read_state(name, folder=None):
    from neko_common import read_journal

    return read_journal(Path(folder or STATE) / name, FORMAT)


def on_it(disk):
    """What is on a disk, in a few words: '2 partitions: NTFS "Photos" (1.2 TB used), unknown 16 MB'."""
    shown = []
    for found in disk['filesystems']:
        words = found['fstype'].upper() if found['fstype'] in ('ntfs', 'exfat', 'vfat') else found['fstype']
        if found.get('label'):
            words += f' "{found["label"]}"'
        if found.get('fsused'):
            words += f' ({size_words(found["fsused"])} used)'
        shown.append(words)
    if not disk['partitions'] and not shown:
        return 'Nothing: the disk is blank.'
    count = disk['partitions']
    plural = '' if count == 1 else 's'
    head = f'{count} partition{plural}: ' if count else ''
    return head + (', '.join(shown) or 'no filesystem NekoServer can read')


def taken_over(disk):
    """The one filesystem a disk may be taken over with as it is (ext4, XFS or Btrfs, small
    partitions such as an EFI or reserved one aside), or None: then it can only be erased."""
    useful = [f for f in disk['filesystems'] if f['fstype'] in FILESYSTEMS]
    others = [f for f in disk['filesystems'] if f['fstype'] not in FILESYSTEMS]
    if len(useful) == 1 and all(int(f.get('size') or 0) <= 2**30 for f in others):
        return useful[0]
    return None


def extra_drive_of(config, disk):
    """The extra drive (d3...) a disk is, by the UUIDs on it, or None."""
    from neko_common import extra_drives

    uuids = {f.get('uuid') for f in disk['filesystems']}
    return next((name for name, record in sorted(extra_drives(config).items()) if record.get('uuid') in uuids), None)


def disk_entry(config, disk, minimum, copies):
    why = refusal(disk, minimum)
    fs = taken_over(disk)
    drive = extra_drive_of(config, disk)
    problem = release_problem(config, drive, copies) if drive else None
    return {
        'by_id': disk['by_id'],
        'serial': disk['serial'],
        'size_bytes': disk['size_bytes'],
        'model': disk['model'],
        'dev': disk['dev'],
        'offer': offer_of(why, fs),
        'refused': why,
        'confirm_words': confirm_words(disk) if disk['serial'] else None,
        'on_it': on_it(disk),
        'fs': fs
        and {'type': fs['fstype'], 'label': fs.get('label'), 'uuid': fs.get('uuid'), 'used_bytes': fs.get('fsused')},
        'drive': drive,
        'can_release': bool(drive) and problem is None,
        'why_not': problem,
    }


def degraded_arrays(sys_block=SYS_BLOCK):
    """{"md-<name>": the size each member needs, in bytes} of every md array that lost a
    drive and is not rebuilding."""
    found = {}
    for md in sorted(Path(sys_block).glob('md*/md')):
        try:
            degraded = int((md / 'degraded').read_text().strip() or 0)
            action = (md / 'sync_action').read_text().strip()
            size = int((md / 'component_size').read_text().strip()) * 1024
        except (OSError, ValueError):
            continue
        if degraded and action == 'idle':
            found[f'md-{md.parent.name}'] = size
    return found


def offer_of(why, fs):
    """What the page offers for a disk: nothing when it is refused, else take it over as it
    is (one filesystem NekoServer can use) or erase it."""
    if why:
        return None
    return 'take_over' if fs else 'erase'


def look(config, world=None, measure=du, arrays=None):
    """look.json: every disk with its offer or refusal, each kind's size, where it may go,
    the old copies, and the disks each array that lost a drive may take."""
    world = dict(world or world_now(config), named=settings_uuids(config))
    disks = describe(world)
    copies = old_copies(config, measure)
    sizes, kinds_targets = {}, {}
    for kind in KINDS:
        root = root_of(config, place_of(config, kind))
        size = measure([root / folder for folder in kind_folders(kind)])
        sizes[kind] = {'bytes': size, 'at': time.time()}
        kinds_targets[kind] = targets(config, kind, size)
    mirrors = {
        array: [
            {'by_id': d['by_id'], 'ok': refusal(d, size) is None, 'words': (refusal(d, size) or {}).get('words')}
            for d in disks
        ]
        for array, size in (degraded_arrays() if arrays is None else arrays).items()
    }
    minimum = FACTS['min_bytes']
    return {
        'at': time.time(),
        'disks': [disk_entry(config, d, minimum, copies) for d in disks],
        'mirror_offers': mirrors,
        'sizes': sizes,
        'targets': kinds_targets,
        'old_copies': copies,
        'same_drive': beside_now(config),
    }


# ---- the jobs ------------------------------------------------------------------------------
# Each job writes its journal before every step that changes anything, with the side of the
# job's commit point the step is on: a power cut before it is undone at the next start, one
# after it is finished. For a move the commit point is the settings write.

JOURNAL = 'journal.json'
RESULT = 'result.json'
LOOK = 'look.json'
PROGRESS = 'progress.json'
RUN_FOLDER = Path(FACTS.get('folder', '/run/nekoserver-control/storage'))
NEW_COPY = 'nekoserver-new-{kind}-{id}'
OLD_NAME = 'nekoserver-old-{kind}-{id}'
DELETING = 'nekoserver-deleting-{kind}-{id}'
# The units a Drives job runs in (the job, the boot's resume, the finish after Docker):
# one job at a time, under the operation lock, so the one at work is this job.
FINISH_UNIT = 'nekoserver-storage-finish'
OWN_UNITS = (
    FACTS.get('unit', 'nekoserver-storage.service'),
    FACTS.get('resume', 'nekoserver-storage-resume.service'),
    FINISH_UNIT + '.service',
)


# The page's own units: the Drives page keeps showing the job while the apps are stopped
# for the last copy. The dashboard's socket too: the service needs it (Requires=), so
# stopping the socket stopped the page, and setup run again a moment later found the port
# held by the page's closed connections and gave the dashboard another one (T1-10). The
# control service answers the page's questions about the job (storage.rs); its app
# buttons (restart, update) refuse while the operation lock is held (control.rs), which a
# Drives job holds from start to end (T1-11).
PAGE_UNITS = (
    'nekoserver-dashboard.socket',
    'nekoserver-dashboard.service',
    'nekoserver-dashboard-collect.service',
    'nekoserver-control.service',
)


def left_alone():
    """The units a move of one kind leaves running: its own and the page's. A whole-drive
    move stops them all (the drive itself goes away)."""
    return (*OWN_UNITS, *PAGE_UNITS)


JOBS = ('look', 'use', 'release', 'move', 'back', 'delete_old', 'mirror_add', 'retry', 'give_up')


class Stop(RuntimeError):
    """A job that ends without a change, with the advice the page shows."""

    def __init__(self, message, advice=None):
        super().__init__(message)
        self.advice = advice


class Job:
    """One request at work: its files (journal, progress, result) and its settings."""

    def __init__(self, request, config, settings_files, state=None, folder=None, out=print):
        self.request, self.config, self.settings_files = request, config, list(settings_files)
        self.state, self.folder, self.out = Path(state or STATE), Path(folder or RUN_FOLDER), out
        # Where the places are recorded on the OS disk (T1-17); a test's own state folder
        # keeps its record beside its journal.
        self.record = Path(state) / 'places.json' if state else None
        self.id, self.action = request['id'], request['job']
        self.started = time.time()

    def words(self, step):
        kind = FACTS.get('kind_words', {}).get(self.request.get('kind'), '')
        return FACTS.get('step_words', {}).get(step, step).replace('{kind}', kind)

    def progress(self, step, **counts):
        from neko_common import atomic_json, ensure_directory

        ensure_directory(self.folder, 0o700)
        shown = {'bytes_done': 0, 'bytes_total': 0, 'files_done': 0, 'files_total': 0, 'percent': None, **counts}
        atomic_json(
            self.folder / PROGRESS,
            {'format': FORMAT, 'id': self.id, 'job': self.action, 'step': step, 'step_words': self.words(step)}
            | shown
            | {'at': time.time()},
        )

    def journal(self, phase, committed, **undo):
        saved = read_state(JOURNAL, self.state) or {}
        kept = saved.get('undo', {}) if saved.get('id') == self.id else {}
        write_state(
            JOURNAL,
            {
                'id': self.id,
                'job': self.action,
                'phase': phase,
                'committed': committed,
                'boot': this_boot(),
                'started_at': self.started,
                'request': self.request,
                'undo': kept | undo,
            },
            self.state,
        )

    def undo(self):
        return (read_state(JOURNAL, self.state) or {}).get('undo', {})

    def result(self, state, message, advice=None, power_cut=False):
        if state != 'working':
            # What the result says is on the drives before it is said (T1-14: a delete said
            # "done" and the old copy was back after a power cut a second later).
            sync_drives(*job_drives(self))
        write_state(
            RESULT,
            {
                'id': self.id,
                'job': self.action,
                **{k: self.request.get(k) for k in ('kind', 'drive', 'to')},
                'state': state,
                'message': message,
                'advice': advice,
                'started_at': self.started,
                'finished_at': None if state == 'working' else time.time(),
                'power_cut': power_cut,
                # How long the apps were paused for the last copy and the switch (said on
                # the page, and before the next move).
                'paused_seconds': getattr(self, 'paused_seconds', None),
            },
            self.state,
        )

    def done(self):
        for path in (self.state / JOURNAL, self.folder / PROGRESS):
            path.unlink(missing_ok=True)

    def save_settings(self):
        from neko_common import atomic_json, write_places

        # The record first: a my-config.json this job does not know of (where a release
        # was unpacked) is brought up to date from it by the next tool that reads it (T1-17).
        write_places(self.config, self.record)
        for path in self.settings_files:
            atomic_json(path, self.config)

    def commit(self, phase, change, **undo):
        """The commit point: the settings change written in the journal first, then into
        every settings file. A cut between them is finished from the journal (reapply)."""
        self.journal(phase, True, change=change, **undo)
        apply_change(self.config, change)
        self.save_settings()

    def reapply(self, saved):
        """A committed job's settings change made again (the same each time), so finishing
        it never works from the settings as they were before."""
        change = (saved.get('undo') or {}).get('change')
        if saved.get('committed') and change:
            apply_change(self.config, change)
            self.save_settings()


def sync_drives(*folders):
    """Every write on the filesystems these folders are on put on the disk: Linux keeps
    writes in memory for a while, and a power cut drops what it kept, so a step that counts
    on them (a commit point, a "done") comes after this (T1-14: a move's copy and rename on
    the new drive were lost while the settings, written and fsynced, said the switch).
    syncfs, once per filesystem; sync() where the C library has no syncfs."""
    import ctypes

    try:
        syncfs = ctypes.CDLL(None, use_errno=True).syncfs
    except (OSError, AttributeError):
        syncfs = None
    seen = set()
    for folder in folders:
        try:
            fd = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
        except (OSError, TypeError):
            continue
        try:
            device = os.fstat(fd).st_dev
            if device not in seen:
                seen.add(device)
                if syncfs is None or syncfs(fd) != 0:
                    os.sync()
        finally:
            os.close(fd)


def job_drives(job):
    """The folders of the drives a job wrote on (its journal says which)."""
    undo = job.undo()
    found = []
    for place in (undo.get('here'), undo.get('to')):
        if place:
            try:
                found.append(root_of(job.config, place))
            except RuntimeError:
                pass
    if undo.get('deleting'):
        found.append(Path(undo['deleting']).parent)
    return found


def apply_change(config, change):
    """[[section, key, value]]: value None takes the key out."""
    for section, key, value in change:
        part = config.setdefault(section, {})
        if value is None:
            part.pop(key, None)
        else:
            part[key] = value


def this_boot():
    import migrate

    return migrate.this_boot()


# ---- A: add a drive, give one up ------------------------------------------------------------


def next_drive(config, fstab=None, record=None):
    """The next Extra drive's name free of every trace of an earlier one: the settings, the
    places record, a mount folder, an fstab line (T1-20: a d4 dropped from the settings,
    its mount left, made the new disk's d4 fail "Wrong filesystem already mounted")."""
    from modules import storage
    from neko_common import extra_drives, read_places

    taken = set(extra_drives(config)) | set((read_places(record) or {}).get('storage') or {})
    try:
        lines = Path(fstab or storage.FSTAB).read_text().splitlines()
    except OSError:
        lines = []
    in_fstab = {
        storage.fstab_folder(line.split()[1])
        for line in lines
        if len(line.split()) >= 2 and not line.lstrip().startswith('#')
    }
    root = extra_mount_root()
    for n in range(3, 100):
        name = f'd{n}'
        if name not in taken and not os.path.lexists(root / name) and str(root / name) not in in_fstab:
            return name
    raise Stop('No name is free for another drive. Nothing was changed.')


def chosen_disk(job, minimum):
    """The disk the request names, as it is now, or Stop: the same by-id, serial and size
    the page showed, and no refusal."""
    asked = job.request['disk']
    rows = describe(dict(world_now(job.config), named=settings_uuids(job.config)))
    found = [d for d in rows if d['by_id'] == asked['by_id']]
    if len(found) != 1 or found[0]['serial'] != asked['serial'] or found[0]['size_bytes'] != asked['size_bytes']:
        raise Stop('That disk is not plugged in the way the page showed it. Nothing was changed.', 'Look again.')
    why = refusal(found[0], minimum)
    if why:
        raise Stop(f'NekoServer does not use that disk: {why["words"]} Nothing was changed.')
    return found[0]


def use(job):
    """Job A "use": a disk not in use becomes the next extra drive, taken over as it is or
    erased first; mounted at /mnt/nekoserver-drives/<name> by setup run again."""
    row = chosen_disk(job, FACTS['min_bytes'])
    name = next_drive(job.config, record=job.record)
    if job.request['how'] == 'take_over':
        fs = taken_over(row)
        if not fs:
            raise Stop('That disk has no single filesystem NekoServer can use as it is. Nothing was changed.')
        uuid, fstype = fs['uuid'], fs['fstype']
    else:
        if not same_words(job.request.get('confirm', ''), confirm_words(row)):
            raise Stop(
                'The words typed do not match the disk. Nothing was erased.',
                f'Type exactly: {confirm_words(row)}',
            )
        job.progress('erase')
        disk = job.request['disk']

        def tell(fields):
            job.journal(fields['phase'], False, drive=name, erased=fields)
            job.progress(fields['phase'])

        erase(disk['by_id'], disk['serial'], disk['size_bytes'], label=f'neko-{name}', config=job.config, journal=tell)
        uuid, fstype = job.undo()['erased']['uuid'], 'ext4'
    add_drive(job, name, uuid, fstype)
    return f'{place_words(name)} is ready to use: {row["model"] or "the disk"}, {size_words(row["size_bytes"])}.'


def add_drive(job, name, uuid, fstype):
    """The commit point of "use": the settings name the drive; setup run again mounts it."""
    add_settings(job, name, uuid, fstype)
    job.progress('mount')
    setup_again(job)


def add_settings(job, name, uuid, fstype):
    job.commit('mount', [['storage', name, {'uuid': uuid, 'fstype': fstype, 'added': True}]], drive=name)


def release(job):
    """Job A "release": an extra drive that holds nothing of NekoServer's is given up: its
    line taken out of /etc/fstab, unmounted, and taken out of the settings."""
    from neko_common import extra_drives

    name = job.request['drive']
    if name not in extra_drives(job.config):
        raise Stop(f'{place_words(name)} is not one of your drives. Nothing was changed.')
    problem = release_problem(job.config, name, old_copies(job.config))
    if problem:
        raise Stop(problem + ' Nothing was changed.')
    job.commit('release', [['storage', name, None]], drive=name)
    drop_fstab_line(extra_mount_root() / name)
    if os.path.ismount(extra_mount_root() / name):
        run(['umount', str(extra_mount_root() / name)])
    return f'{place_words(name)} is no longer used. You can unplug it.'


def extra_mount_root():  # read when used: tests point it elsewhere
    from neko_common import EXTRA_MOUNTS

    return Path(EXTRA_MOUNTS)


def drop_fstab_line(mountpoint, fstab=None):
    """The line setup wrote for this drive taken out of /etc/fstab (nothing else)."""
    from modules import storage
    from neko_common import atomic_write

    fstab = Path(fstab or storage.FSTAB)
    lines = fstab.read_text().splitlines(keepends=True)
    kept = [
        line
        for line in lines
        if not (
            len(line.split()) == 6
            and storage.fstab_folder(line.split()[1]) == str(mountpoint)
            and storage.own_line(line.split())
        )
    ]
    if kept != lines:
        atomic_write(fstab, ''.join(kept), 0o644)
        run(['systemctl', 'daemon-reload'])


def setup_again(job):
    """Setup without questions, so every service and backup knows the new place
    (migrate.setup_again: the way an upgrade and a drive move run it)."""
    import migrate

    log = job.state / f'setup-{job.id}.log'
    # The job's unit keeps its own files private (UMask=0077); setup makes folders others
    # use (the drives' mount folders, the apps' folders), so it runs with the system's
    # umask, the one it has when started by hand, not this one.
    wanted = system_umask()
    before = None if wanted is None else os.umask(wanted)
    try:
        migrate.setup_again(job.config, job.settings_files[-1], log, out=job.out)
    finally:
        if before is not None:
            os.umask(before)


# Where the system's umask is read: the one a login starts with, so the one setup has when
# started by hand (022 on Debian, Ubuntu, Fedora, openSUSE and Arch). Not PID 1's: that is
# 0000 (systemd's own; its services get DefaultUMask). openSUSE keeps only the vendor copy.
LOGIN_DEFS = (Path('/etc/login.defs'), Path('/usr/etc/login.defs'))


def system_umask():
    """The system's umask (login.defs UMASK), or None when it is not said or would let the
    group or others write (the job's own private umask then stays)."""
    text = next((path.read_text(errors='replace') for path in LOGIN_DEFS if path.is_file()), '')
    found = re.search(r'^[ \t]*UMASK[ \t]+(0?[0-7]{3})[ \t]*$', text, re.MULTILINE)
    if not found:
        return None
    mask = int(found[1], 8)
    return mask if mask & 0o022 == 0o022 else None


# ---- B: move one kind of data -----------------------------------------------------------------


def paths(config, kind, place, base=None):
    """The folders of a kind at a place (or inside `base`, a copy's folder there)."""
    root = Path(base) if base else root_of(config, place)
    return [root / rel for rel in kind_folders(kind)]


def gone_or_empty(folder, skip=()):
    """Make way for a folder moved in: nothing there, or an empty folder (a mount point
    setup closed, chattr +i) taken away; one that holds only another kind's folders (`skip`)
    stays, and the folder moved in joins them."""
    folder = Path(folder)
    if not folder.exists() and not folder.is_symlink():
        return
    if skip and folder.is_dir() and not folder.is_symlink() and any(folder.iterdir()) and not own_entries(folder, skip):
        return
    if folder.is_symlink() or not folder.is_dir() or any(folder.iterdir()):
        raise Stop(f'{folder} is in the way: it is not empty. Nothing was changed.')
    run(['chattr', '-i', str(folder)]) if shutil_which('chattr') else None
    folder.rmdir()


def parents_like(target, here_root, to_root):
    """The folders above a kind's folder at its new place that are not there yet, made
    with the mode and owner of the same folders at the old place (of the folder above it
    at the new place, where the old place has none): the job's unit makes its files
    private (UMask=0077), and a 0700 library/ kept Sonarr and Radarr, which do not run as
    root, out of their folders. Each is made under a hidden name and renamed once it has
    its mode, so a power cut never leaves one with the unit's 0700 (T5 on 4c34bb6)."""
    import stat

    missing = []
    folder = Path(target).parent
    while folder != to_root and to_root in folder.parents and not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for folder in reversed(missing):
        like = here_root / folder.relative_to(to_root)
        seen = (like if like.is_dir() else folder.parent).stat()
        making = folder.with_name(f'.{folder.name}.making')
        if making.is_dir() and not making.is_symlink():
            making.rmdir()  # an empty one a power cut left
        making.mkdir()
        os.chmod(making, stat.S_IMODE(seen.st_mode))
        if (seen.st_uid, seen.st_gid) != (making.stat().st_uid, making.stat().st_gid):
            os.chown(making, seen.st_uid, seen.st_gid, follow_symlinks=False)
        os.rename(making, folder)


def shutil_which(name):
    import shutil

    return shutil.which(name)


def move(job):
    """Job B "move": a kind of data to another drive, the old copy kept (migrate.py's way,
    for one kind's folders)."""

    kind, to = job.request['kind'], job.request['to']
    here = place_of(job.config, kind)
    if to == here:
        raise Stop(f'Your {KIND_WORDS[kind]} are on {place_words(to)} already. Nothing was changed.')
    need(job, here, to)
    size = du(paths(job.config, kind, here))
    problem = target_problem(job.config, kind, to, size)
    if problem:
        raise Stop(problem + ' Nothing was changed.')
    beside = beside_backups(job.config, kind, to)
    if beside and job.request.get('beside_ok') is not True:
        raise Stop(
            beside_words(job.config, beside) + ' Tick that you understand in the move window to move them anyway. '
            'Nothing was changed.'
        )
    staging = root_of(job.config, to) / NEW_COPY.format(kind=kind, id=job.id)
    new = paths(job.config, kind, to, staging)
    for folder, skip in zip(paths(job.config, kind, to), kind_skips(kind)):
        if folder.exists() and not os.path.ismount(folder) and own_entries(folder, skip):
            raise Stop(f'{folder} holds files already. Nothing was changed.')
    job.journal('copy', False, staging=str(staging), here=here, to=to, before=steps_working(job.config))
    switch_data(job, kind, here, to, paths(job.config, kind, here), new, size)
    return (
        f'Your {KIND_WORDS[kind]} are on {place_words(to)} now. The old copy stays on {place_words(here)} until you '
        'delete it.'
    )


def steps_working(config):
    import migrate

    try:
        return migrate.steps_working(config)
    except Exception:  # noqa: BLE001
        # No record of the steps: the check after the move is the dashboard's.
        return []


def copy_all(job, sources, targets_, size, final):
    import migrate

    job.progress('last_copy' if final else 'copy', bytes_total=size)
    for source, target, skip in zip(sources, targets_, kind_skips(job.request['kind'])):
        need(job, *job_places(job))
        if not source.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        migrate.copy(
            source, target, final=final, out=job.out, again=not final and any(target.iterdir()), **copy_skip(skip)
        )


def switch_data(job, kind, here, to, sources, staged, size):
    """Copy while the apps run, stop them, copy what changed, compare, then switch."""
    import migrate

    try:
        copy_all(job, sources, staged, size, final=False)
        job.journal('stop', False, paused_at=time.time())
        job.progress('stop')
        stopped = migrate.stop_writers(
            out=job.out, note=lambda units: job.journal('stop', False, stopped=units), own=left_alone()
        )
        copy_all(job, sources, staged, size, final=True)
        # How many files each new folder holds: a resume after the switch checks they are
        # there before it finishes (T1-14).
        job.journal('compare', False, copied=[file_count(folder) for folder in staged])
        job.progress('compare')
        for source, target, skip in zip(sources, staged, kind_skips(kind)):
            if source.exists() and migrate.differences(source, target, **copy_skip(skip)):
                raise Stop(
                    f'The copy of your {KIND_WORDS[kind]} is not the same as the original. Nothing was changed; '
                    'the apps are running as before.',
                    'Check the new drive (its cable) and try again.',
                )
        put_in_place(job, kind, here, to, staged)
    except BaseException:
        if not (read_state(JOURNAL, job.state) or {}).get('committed'):
            undo_move(job)
            migrate.start_again(job.undo().get('stopped'), job.out)
        raise
    finish_move(job, kind, here, stopped)


def put_in_place(job, kind, here, to, staged):
    """The switch: the new copy moved to its place and the settings written (the commit
    point). The old folders stay where they are until setup has run again."""
    job.journal('switch', False)
    job.progress('switch')
    need(job, *job_places(job))
    if kind == 'backups' and here != 'media':
        unbind_backups(job.config)
    for target, skip in zip(paths(job.config, kind, to), kind_skips(kind)):
        gone_or_empty(target, skip)
    for copy_, target, skip in zip(staged, paths(job.config, kind, to), kind_skips(kind)):
        if copy_.exists():
            parents_like(target, root_of(job.config, here if here in job_places(job) else to), root_of(job.config, to))
            move_folder(copy_, target, skip)
    # The copy and its new names on the disk before the settings say them (T1-14).
    sync_drives(root_of(job.config, to), root_of(job.config, here))
    was = (job.config.get('locations') or {}).get(kind)
    job.commit('switch', [['locations', kind, None if kind == 'backups' and to == 'media' else to]], was=was)


def backup_binds(config):
    from modules import storage

    return storage.backup_binds(config)


def fstab_binds(config, fstab=None):
    """Setup's block of bind lines written for these settings now (setup writes the same
    when it runs again), so no start of the computer mounts the old place over the new."""
    from modules import storage
    from neko_common import atomic_write

    fstab = Path(fstab or storage.FSTAB)
    text = fstab.read_text() if fstab.exists() else ''
    lines = [
        storage.installed_line(source, target, drive, storage.bind_parent(config, target))
        for _, source, target, drive in storage.installed_binds(config)
    ]
    wanted = storage.with_block(text, storage.INSTALL_BEGIN, storage.INSTALL_END, lines)
    if wanted != text and (lines or storage.INSTALL_BEGIN in text):
        atomic_write(fstab, wanted, 0o644)
        run(['systemctl', 'daemon-reload'])


def unbind_backups(config, fstab=None):
    """The backups' folders on an extra drive no longer mounted at their places on the
    media drive (their lines out of /etc/fstab too)."""
    binds = backup_binds(config)
    without = json.loads(json.dumps(config))
    without['locations'].pop('backups', None)
    fstab_binds(without, fstab)
    for _, _source, target, _drive in binds:
        if os.path.ismount(target):
            run(['umount', str(target)])


def rebind_backups(config, out=print):
    """An undone backups move from an extra drive: their folders there mounted again."""
    import types

    from modules import storage

    storage.mount_installed(types.SimpleNamespace(config=config, log=out))


def retire_old(job, kind, here):
    """The old folders renamed into one old copy, out of every app's way."""
    if away(job.config, here):
        # Its folders are retired when it is back (T1-18): nothing is written on it now.
        wait_for(job.state, {'job': 'retire', 'kind': kind, 'here': here, 'id': job.id})
    else:
        retire_folders(job.config, kind, here, job.id)
    staging = Path(job.undo().get('staging', ''))
    if staging.name.startswith('nekoserver-new-') and staging.is_dir():
        # Only its empty folders: what is still in it was never put in place, and may be the
        # only copy (T1-14: a lost rename left the files there, and they were deleted).
        if not remove_empty(staging):
            job.out(f'{staging} still holds files, so it is kept.')


def retire_folders(config, kind, here, id_):
    old = root_of(config, here) / OLD_NAME.format(kind=kind, id=id_)
    for folder, rel, skip in zip(paths(config, kind, here), kind_folders(kind), kind_skips(kind)):
        if folder.exists() and not os.path.ismount(folder) and (not skip or own_entries(folder, skip)):
            (old / rel).parent.mkdir(parents=True, exist_ok=True)
            move_folder(folder, old / rel, skip)


def job_places(job):
    """The drives a move or a "back" reads or writes now: both, or only the one it goes to
    when it leaves a drive that is lost (a "Move back" from it)."""
    undo = job.undo()
    if undo.get('here_away'):
        return [undo.get('to')]
    return [p for p in (undo.get('here'), undo.get('to')) if p]


WAITING = 'waiting.json'


def delete_waits(config, undo):
    """The waiting entry of an old copy's delete: the folder, and the drive that holds it
    (the one it is deleted on, and only there, once it is back)."""
    from neko_common import place_holding

    deleting = undo.get('deleting', '')
    return {'job': 'delete_old', 'deleting': deleting, 'place': place_holding(config, Path(deleting).parent)}


def wait_for(state, entry):
    """Work kept for a drive that is away (its old folders retired, an old copy deleted),
    done when it is back (waiting_work); it never holds up another job."""
    entries = waiting(state)
    if entry not in entries:
        write_state(WAITING, {'entries': [*entries, entry]}, state)


def waiting(state=None):
    return list((read_state(WAITING, state) or {}).get('entries') or [])


def waiting_work(config, state=None, out=print):
    """What waits for a drive that is back now, done; the rest kept."""
    kept = []
    for entry in waiting(state):
        place = entry.get('here') or entry.get('place')
        try:
            if not place or away(config, place):
                kept.append(entry)
                continue
            do_waiting(config, entry, place)
        except (OSError, RuntimeError, KeyError) as error:
            out(f'What waited for {place} could not be done yet: {error}')
            kept.append(entry)
    if kept:
        write_state(WAITING, {'entries': kept}, state)
    elif read_state(WAITING, state) is not None:
        (Path(state or STATE) / WAITING).unlink(missing_ok=True)


def do_waiting(config, entry, place):
    """One waiting entry done, its drive `place` there."""
    import shutil

    from neko_common import place_holding

    if entry.get('job') == 'retire' and place_of(config, entry['kind']) != place:
        retire_folders(config, entry['kind'], place, entry['id'])
    elif entry.get('job') == 'delete_old':
        gone = Path(entry.get('deleting', ''))
        # Only the folder this job named, on the drive it was on (review of cdcbc14).
        if (
            gone.name.startswith('nekoserver-deleting-')
            and place_holding(config, gone.parent) == place
            and gone.exists()
        ):
            shutil.rmtree(gone)


def remove_empty(folder):
    """The empty folders in `folder`, and it, taken away; True when it is gone."""
    for left in sorted(
        (p for p in folder.rglob('*') if p.is_dir() and not p.is_symlink()), key=lambda p: -len(p.parts)
    ):
        if not any(left.iterdir()):
            left.rmdir()
    if any(folder.iterdir()):
        return False
    folder.rmdir()
    return True


def file_count(folder):
    """The files (anything but a folder) in a folder and below it, links not followed; 0
    for a folder that is not there."""
    total = 0
    for top, folders, files in os.walk(folder, followlinks=False):
        total += len(files) + sum(1 for name in folders if os.path.islink(os.path.join(top, name)))
    return total


def switch_lost(job, saved):
    """Whether the new place of a committed move or "back" lost what was put there (a
    power cut before the drive had it, T1-14): a new folder with fewer files than were
    copied into it while nothing has run since (phase "switch"), or an empty one while the
    old place has files."""
    undo = saved.get('undo') or {}
    kind, here, to = job.request.get('kind'), undo.get('here'), undo.get('to')
    if saved.get('phase') not in ('switch', 'start') or not (kind and here and to):
        return False
    copied = undo.get('copied') or []
    old = root_of(job.config, here) / OLD_NAME.format(kind=kind, id=job.id)
    for n, (was, now, rel) in enumerate(
        zip(paths(job.config, kind, here), paths(job.config, kind, to), kind_folders(kind))
    ):
        there = file_count(now)
        if saved.get('phase') == 'switch' and n < len(copied) and there < copied[n]:
            return True
        if there == 0 and max(file_count(was), file_count(old / rel)) > 0:
            return True
    return False


def undo_lost_switch(job, kind, here):
    """A switch the power cut lost undone from the old place, which still has everything
    (it is retired only after the new place is on the disk): the settings name it again,
    folders retired early (the backups') given back, whatever is left at the new place moved
    out of the apps' way (a "back" gives its old copy its name again). Never copies the new
    place over the old one (an unswitch does: here the new place is the one that lost
    files). Done again from the start until it ends (lost_undone)."""
    to = job.undo()['to']
    if not job.undo().get('lost_undone'):
        job.journal('lost', True, change=[['locations', kind, job.undo().get('was')]])
        if kind == 'backups' and to != 'media':
            forward = json.loads(json.dumps(job.config))
            apply_change(forward, [['locations', kind, to]])
            unbind_backups(forward)
        unretire(job, kind, here)
        apply_change(job.config, job.undo()['change'])
        job.save_settings()
        undo_move(job)
        sync_drives(root_of(job.config, here), root_of(job.config, to))
        job.journal('lost', True, lost_undone=True)


AWAY_LOST_WORDS = (
    '{gone} So your {kind} stay on {here}, as before the move to it. What the apps wrote there after the '
    'switch stays on {to}.'
)
LOST_WORDS = (
    'The power went off just as your {kind} were being switched to {to}, and the copy there did not survive it. '
    'They stay on {here}, as before. Nothing was changed.'
)


def finish_lost(job, kind, here):
    """After Docker: the undone switch's apps on the old place again (setup), then Stop."""
    import migrate

    undo_lost_switch(job, kind, here)
    stopped = migrate.stop_writers(out=job.out, note=lambda units: None, own=left_alone())
    also = ''
    try:
        setup_again(job)
    except Exception as error:  # noqa: BLE001
        # Said; the files and the settings are as before the move.
        also = f' Setup, run again for {place_words(here)}, stopped: {error}'
    finally:
        migrate.start_again(stopped, job.out)
    kept, to = job.undo().get('kept'), job.undo()['to']
    words = AWAY_LOST_WORDS if job.undo().get('away') == to else LOST_WORDS
    gone = away(job.config, to) or f'{place_words(to)} is not connected.'
    raise Stop(
        words.format(kind=KIND_WORDS[kind], to=place_words(to), here=place_words(here), gone=gone) + also,
        'Move them again from the Drives page.'
        + (f' What was at the new place is kept in {kept}: look there for anything added since.' if kept else ''),
    )


def finish_move(job, kind, here, stopped):
    """After the commit point: setup run again, the old folders retired, the apps started."""
    import migrate

    job.journal('start', True)
    job.progress('start')
    to = job.undo()['to']
    if away(job.config, to):
        # The new place went away after the switch: back to the old one, which has it all.
        job.journal('start', True, away=to)
        migrate.start_again(stopped, job.out)
        finish_lost(job, kind, here)
    gone = away(job.config, here)
    other = None
    try:
        if kind == 'backups':
            # Setup never mounts the backups' new place over files at the old one.
            sync_drives(root_of(job.config, job.undo()['to']))
            retire_old(job, kind, here)
        setup_again(job)
    except Exception as error:  # noqa: BLE001
        # A refusal of the place undoes the move, never half; never onto a drive that is
        # away (T1-18: the "Move back" from a lost drive is the way out).
        if refused_place(job, kind) and not gone:
            unswitch(job, kind, here, stopped, error)
        other = error
    try:
        # What the apps have at the new place on the disk before the old one is retired.
        sync_drives(root_of(job.config, job.undo()['to']))
        retire_old(job, kind, here)
    finally:
        migrate.start_again(stopped, job.out)
        paused(job)
    if other and gone:
        raise Stop(
            f'Your {KIND_WORDS[kind]} are on {place_words(to)} now. {gone} Setup, run again for the move, '
            f'stopped: {other}',
            f'Run setup again (sudo bash install.sh) once {place_words(here)} is back, or once nothing else is on '
            'it and it is given up on the Drives page.',
        )
    if other:
        raise Stop(
            f'Your {KIND_WORDS[kind]} are on {place_words(job.undo()["to"])} now and their apps use them there. '
            f'Setup, run again for the move, ended with a step that does not use them failing: {other}',
            'Run setup again (sudo bash install.sh) once that is fixed.',
        )
    job.progress('check')
    before = job.undo().get('before') or []
    problem = before and migrate.server_check(job.config, before)
    if problem:
        raise Stop(f'Your {KIND_WORDS[kind]} were moved, but the server check after it says: {problem}')


def paused(job):
    """The time from the apps' stop to their start again, kept for the result."""
    since = job.undo().get('paused_at')
    if isinstance(since, (int, float)):
        job.paused_seconds = max(0, round(time.time() - since))


def moved_here(config, kind, state=None):
    """While setup runs again for a Drives job's move of `kind` (its journal past the
    commit point: phase "start", or "unswitch" while the move is undone): (the folder of the
    drive the kind left, the folder of the drive the settings name now); None any other
    time. Setup's checks that an app's own record names the folder the settings do take
    the first for the second then, and write the second (T1-9: Immich's .env)."""
    saved = read_state(JOURNAL, state)
    if not saved or not saved.get('committed') or saved.get('job') not in ('move', 'back'):
        return None
    undo = saved.get('undo') or {}
    ends = {'start': (undo.get('here'), undo.get('to')), 'unswitch': (undo.get('to'), undo.get('here'))}
    left, now = ends.get(saved.get('phase'), (None, None))
    try:
        if (saved.get('request') or {}).get('kind') != kind or not left or now != place_of(config, kind):
            return None
        return root_of(config, left), root_of(config, now)
    except RuntimeError:
        return None


# The setup steps that use a kind's folders, or the drives themselves. After a move's
# switch, a setup run that fails is taken as a refusal of the new place, and the move is
# undone (unswitch), unless its log shows the run finished and each of these steps that
# it has PASSED (or cannot be had on this PC, or waits for the person): then the failure was another step's (an
# image download, the gateway), the move is finished and that failure said (T5 on
# f58aa9b). A step the run held up (BLOCKED) or never reached is no pass (T5 on 751ac7c:
# Immich's .env still naming the old folder when its step waited on the gateway).
PLACE_STEPS = ('storage', 'capacity', 'docker')
KIND_STEPS = {
    'photos': ('photos', 'photo_backup', 'photo_preservation'),
    'cloud': ('nextcloud',),
    'movies': ('media', 'samba'),
    'music': ('media', 'music', 'music_backup', 'samba'),
    'backups': ('app_backup', 'host_backup', 'photo_backup', 'maintenance', 'backup_cards'),
}
STEP_LINE = re.compile(r'(?:^|\] )(START|PASS|FAILED|NEEDS SETUP|UNAVAILABLE|BLOCKED) ([a-z_]+)')
FINE = ('PASS', 'UNAVAILABLE')
# NEEDS SETUP is a pass only for the backup steps (their drive not plugged in): a step that
# points an app at its folders may stop for the person before it wrote them (T5 on 2515163).
BACKUP_STEPS = ('photo_backup', 'music_backup', 'backup_cards', 'app_backup', 'host_backup')


def refused_place(job, kind):
    """Whether the failed setup run of this move (its log, setup-<id>.log) refused the new
    place: True unless it ran to its end and every one of the kind's and the drives' steps
    it had ended PASS (a step it did not have is not in its plan)."""
    try:
        text = (job.state / f'setup-{job.id}.log').read_text(errors='replace')
    except OSError:
        return True
    last, ended = {}, False
    for line in text.splitlines():
        found = STEP_LINE.search(line)
        if found:
            last[found[2]], ended = found[1], False
        elif 'Finished:' in line:
            ended = True
    return not ended or any(
        last.get(step, 'PASS') not in (*FINE, *(('NEEDS SETUP',) if step in BACKUP_STEPS else ()))
        for step in (*PLACE_STEPS, *KIND_STEPS.get(kind, ()))
    )


def unswitch(job, kind, here, stopped, why):
    """Setup refused the new place after the switch (T1-9: Immich's .env named the old
    folder): the move undone, so the server is as it was, never half. What the apps wrote
    in the new folders meanwhile is copied back into the old ones (still there: they are
    retired after setup), the settings name the old place again, the new copy goes (a
    "back" keeps its old copy) and setup runs again for the old place. The journal says
    "unswitch" first: a power cut in it goes on with it at the next start. Ends with Stop:
    the job failed and nothing was changed."""
    import migrate

    to = job.undo()['to']
    back_change = [['locations', kind, job.undo().get('was')]]
    job.journal('unswitch', True, change=back_change, why=str(why))
    job.progress('unswitch')
    earlier = job.undo().get('stopped_again') or []
    again = migrate.stop_writers(
        out=job.out,
        note=lambda units: job.journal('unswitch', True, stopped_again=[*earlier, *units]),
        own=left_alone(),
    )
    stopped = list(dict.fromkeys([*(stopped or []), *earlier, *again]))
    if not job.undo().get('files_back'):
        unswitch_files(job, kind, here)
    else:
        # Undone at the start already, before Docker (resume): only what an app wrote in a
        # new folder since (one its record still named) is added, nothing deleted.
        for old, new, skip in zip(paths(job.config, kind, here), paths(job.config, kind, to), kind_skips(kind)):
            if new.is_dir() and not os.path.ismount(new) and own_entries(new, skip):
                old.mkdir(parents=True, exist_ok=True)
                migrate.copy(new, old, out=job.out, **copy_skip(skip))
    also = ''
    try:
        setup_again(job)
    except Exception as error:  # noqa: BLE001
        # Said; the files and the settings are as before.
        also = f' Setup run again for {place_words(here)} stopped too: {error}'
    finally:
        migrate.start_again(stopped, job.out)
        paused(job)
    raise Stop(
        f'Your {KIND_WORDS[kind]} stay on {place_words(here)}: setup could not use them on {place_words(to)} '
        f'({why}). Nothing was changed.' + also,
        f'Setup\'s log says why: {job.state / f"setup-{job.id}.log"}',
    )


def unswitch_files(job, kind, here):
    """unswitch's part on the drives: the backups' new place unmounted, folders retired
    before setup given back, what the apps wrote in the new folders copied back into the
    old ones (the old ones then equal them), the settings back, the new copy out of the way
    (undo_move). Done again from the start until it ends (files_back): at a resume it runs
    before Docker, so no app has written at the old place yet (T5 on f58aa9b)."""
    import migrate

    to = job.undo()['to']
    need(job, here)
    if kind == 'backups' and to != 'media':
        forward = json.loads(json.dumps(job.config))
        apply_change(forward, [['locations', kind, to]])
        unbind_backups(forward)
    unretire(job, kind, here)
    for old, new, skip in zip(paths(job.config, kind, here), paths(job.config, kind, to), kind_skips(kind)):
        if not away(job.config, to) and new.is_dir() and not os.path.ismount(new):
            old.mkdir(parents=True, exist_ok=True)
            migrate.copy(new, old, final=True, out=job.out, **copy_skip(skip))
    sync_drives(root_of(job.config, here))
    apply_change(job.config, job.undo()['change'])
    job.save_settings()
    undo_move(job)
    sync_drives(root_of(job.config, here), root_of(job.config, to))
    job.journal('unswitch', True, files_back=True)


def unretire(job, kind, here):
    """Folders retired before setup ran (the backups') given their places back."""
    if away(job.config, here):
        return
    old = root_of(job.config, here) / OLD_NAME.format(kind=kind, id=job.id)
    for folder, rel, skip in zip(paths(job.config, kind, here), kind_folders(kind), kind_skips(kind)):
        free = not own_entries(folder, skip) if skip else not folder.exists()
        if free and (old / rel).exists():
            folder.parent.mkdir(parents=True, exist_ok=True)
            move_folder(old / rel, folder, skip)
    if old.is_dir():
        for left in sorted((p for p in old.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(left.iterdir()):
                left.rmdir()
        if not any(old.iterdir()):
            old.rmdir()


def undo_move(job):
    """Before the commit point: the new copy taken out of the apps' places again and
    deleted; the old folders were never touched."""
    journal = read_state(JOURNAL, job.state) or {}
    saved, switching = journal.get('undo', {}), journal.get('phase') in ('switch', 'unswitch', 'lost')
    staging = Path(saved.get('staging', ''))
    kind, to, here = job.request['kind'], saved.get('to'), saved.get('here')
    # Nothing on a new place that is away: what was copied there stays in its
    # nekoserver-new- folder, which no app uses (T1-18).
    there = not (to and away(job.config, to))
    if to and switching and there:
        take_out_of_place(job, kind, to, staging)
    if kind == 'backups' and here not in (None, 'media') and switching:
        rebind_backups(job.config, job.out)
    if there and job.action == 'move' and staging.name.startswith('nekoserver-new-') and staging.is_dir():
        drop_staging(job, staging, journal.get('phase') == 'lost')
    if job.action == 'back':
        restore_back_copy(job, journal)


def take_out_of_place(job, kind, to, staging):
    """The new copy taken out of the apps' places again, into its staging folder."""
    for copy_, target, skip in zip(paths(job.config, kind, to, staging), paths(job.config, kind, to), kind_skips(kind)):
        ours = own_entries(target, skip) if skip else not copy_.exists()
        if target.exists() and not os.path.ismount(target) and ours:
            copy_.parent.mkdir(parents=True, exist_ok=True)
            move_folder(target, copy_, skip)


def drop_staging(job, staging, lost):
    """A move's new copy, taken out of the apps' places, deleted. Undoing a lost switch,
    what was at the new place may hold an app's writes since the start (an upload from a
    phone): only its empty folders go, and the journal says where the rest is kept (T5 on
    5d6eaed)."""
    import shutil

    if not lost:
        shutil.rmtree(staging)
    elif not remove_empty(staging):
        job.journal('lost', True, kept=str(staging))


def old_copy(job):
    """The old copy the request names, or Stop."""
    kind, copy = job.request['kind'], job.request['copy']
    found = [c for c in old_copies(job.config, measure=lambda _paths: 0) if (c['kind'], c['copy']) == (kind, copy)]
    if len(found) != 1:
        raise Stop('That old copy is not there any more. Nothing was changed.')
    return found[0]


def back(job):
    """Job B "back": while the old copy exists, the kind goes back to it, with a fresh copy
    of what changed since (the old copy is the target of the copy)."""
    kind = job.request['kind']
    found = old_copy(job)
    here, to = place_of(job.config, kind), found['place']
    need(job, to)
    lost = away(job.config, here)
    staging = root_of(job.config, to) / NEW_COPY.format(kind=kind, id=job.id)
    job.journal(
        'copy', False, staging=str(staging), here=here, to=to, before=steps_working(job.config), here_away=bool(lost)
    )
    os.rename(found['path'], staging)
    if lost:
        # The way out when the drive they were moved to is lost (T1-18): the old copy is put
        # back as it is, and nothing is read or written on the lost drive.
        switch_data(job, kind, here, to, [], paths(job.config, kind, to, staging), 0)
        return (
            f'Your {KIND_WORDS[kind]} are back on {place_words(to)}, as the old copy had them. {lost} What was '
            f'added to them there after the move is not in it; it stays on {place_words(here)} if that drive '
            'comes back.'
        )
    size = du(paths(job.config, kind, here))
    switch_data(job, kind, here, to, paths(job.config, kind, here), paths(job.config, kind, to, staging), size)
    return (
        f'Your {KIND_WORDS[kind]} are back on {place_words(to)}. The copy on {place_words(here)} stays until you '
        'delete it.'
    )


def delete_old(job):
    """Job B "delete_old": the old copy renamed to its "being deleted" name first (never
    offered again), then deleted."""
    import shutil

    found = old_copy(job)
    need(job, found['place'])
    if not same_words(job.request.get('confirm', ''), found['delete_words']):
        raise Stop('The words typed do not match. Nothing was deleted.', f'Type exactly: {found["delete_words"]}')
    gone = Path(found['path']).with_name(DELETING.format(kind=found['kind'], id=found['copy']))
    job.journal('delete', True, deleting=str(gone))
    job.progress('delete')
    os.rename(found['path'], gone)
    shutil.rmtree(gone)
    return f'The old copy of your {KIND_WORDS[found["kind"]]} on {found["place_words"]} is deleted.'


WORK = {'use': use, 'release': release, 'move': move, 'back': back, 'delete_old': delete_old}


# ---- D: a mirror's replacement drive (mirror_add.py, T2's; given this file's erase) ---------


def mirror_context(job):
    import mirror_add

    def ran(argv, timeout=None):
        import subprocess

        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout + done.stderr

    named = {
        'run': ran,
        'erase': lambda *given, **more: erase(*given, config=job.config, **more),
        'refused': Refused,
        'journal': lambda phase, committed=False, undo=None: job.journal(phase, committed, **(undo or {})),
        'progress': job.progress,
        'clear': lambda: (job.state / JOURNAL).unlink(missing_ok=True),
        # T1-18 for job D (T2, 57eb40c): the mirror's drive checked before the erase, the
        # add and a cut-off finish. Always given, so a Context without it fails loudly.
        'away': lambda folder: mirror_drive_away(job.config, folder),
    }
    return mirror_add.Context(**named)


def mirror_drive_away(config, folder):
    """None when the drive that holds `folder` (a mirror's mount) is there and the one the
    settings name; "missing" or "other" when not; None for a folder on no drive of theirs."""
    from neko_common import drive_away, place_holding

    place = place_holding(config, folder)
    return drive_away(config, place) if place else None


def mirror_job(job):
    """Job D: the disk is the one the page showed and the words typed are its words, then
    mirror_add.py lays it out like the member still there (this file's erase) and adds it."""
    import mirror_add

    row = chosen_disk(job, 0)
    if not same_words(job.request.get('confirm', ''), confirm_words(row)):
        raise Stop('The words typed do not match the disk. Nothing was erased.', f'Type exactly: {confirm_words(row)}')
    return mirror_add.run_job(job.request, mirror_context(job))


# ---- running a request, and the boot's resume ------------------------------------------------


REQUEST = RUN_FOLDER / FACTS.get('request', 'request.json')


def request_path(value):
    """The request the unit names: only the control service's own request file, which it
    writes after checking each field (storage.rs), never another file."""
    wanted = os.path.realpath(REQUEST)
    if os.path.realpath(str(value)) != wanted:
        raise SystemExit(f'The request must be {wanted}')
    return wanted


def read_request(path):
    from neko_common import read_journal

    request = read_journal(path, FORMAT)
    if not request or request.get('job') not in JOBS or not re.fullmatch(r'[0-9a-f]{16}', str(request.get('id'))):
        raise SystemExit(f'{path}: not a storage request')
    return request


def run_request(request, config, settings_files, state=None, folder=None, out=print):
    """One request, start to end. look only reads; every other job holds the operation lock."""
    from neko_common import OPERATION_LOCK, hold_operation

    job = Job(request, config, settings_files, state, folder, out)
    if job.action == 'look':
        job.progress('look')
        write_state(LOOK, look(config), job.state)
        (job.folder / PROGRESS).unlink(missing_ok=True)
        return 0
    stuck = read_state(JOURNAL, job.state)
    if job.action in ('retry', 'give_up'):
        if not stuck:
            job.result('failed', 'No job is waiting to be finished. Nothing was changed.')
            return 1
        return (retry if job.action == 'retry' else give_up)(job, stuck)
    if stuck:
        job.result('failed', STUCK_WORDS, STUCK_ADVICE)
        return 1
    held = hold_operation(OPERATION_LOCK)
    if held is None:
        job.result('failed', FACTS.get('busy', 'Another job is at work.'), 'Try again later.')
        return 1
    with held:
        waiting_work(job.config, job.state, job.out)
        return worked(job)


STUCK_WORDS = 'A job that was cut off is not finished yet.'
STUCK_ADVICE = (
    'It is finished at the next start, or now with "Try again" (once its drives are back), or "Give it up": '
    'what is done stays, and the rest is said.'
)


def retry(job, stuck):
    """A job the start could not finish, tried again now (its drives back): the start's own
    steps, the resume then the finish, in this run, under the operation lock and with the
    apps stopped, as at the start before Docker (review of cdcbc14: the resume copies the
    new place back and deletes the staging, and moves folders the apps use)."""
    import migrate
    from neko_common import OPERATION_LOCK, hold_operation

    held = hold_operation(OPERATION_LOCK)
    if held is None:
        job.result('failed', FACTS.get('busy', 'Another job is at work.'), 'Try again later.')
        return 1
    with held:
        try:
            stopped = migrate.stop_writers(out=job.out, note=lambda units: None, own=left_alone())
        except RuntimeError as error:
            job.result('failed', f'{error}. Nothing was changed.', 'Try again later.')
            return 1
        try:
            code = resume(job.config, job.settings_files, job.state, job.folder, job.out, hand_off=False)
            saved = read_state(JOURNAL, job.state)
            if code or not saved or not saved.get('committed'):
                return code
            config = job.config
            return finish_held(Job(saved['request'], config, job.settings_files, job.state, job.folder, job.out), saved)
        finally:
            migrate.start_again(stopped, out=job.out)


def give_up(job, stuck):
    """A job that can not be finished (its drive lost) let go, under the operation lock:
    before its commit point it is undone as far as the drives that are there allow; after
    it the settings stay as the switch wrote them, and what it could not do is said (and
    done later when it needs a drive that is away)."""
    from neko_common import OPERATION_LOCK, hold_operation

    held = hold_operation(OPERATION_LOCK)
    if held is None:
        job.result('failed', FACTS.get('busy', 'Another job is at work.'), 'Try again later.')
        return 1
    with held:
        cut = Job(stuck['request'], job.config, job.settings_files, job.state, job.folder, job.out)
        undo, kind = stuck.get('undo') or {}, (stuck.get('request') or {}).get('kind')
        places = cut_places(job.config, stuck)
        if places and not any(away(job.config, place) for place in places):
            # Given up with every drive there, the settings would go back without what the
            # apps wrote at the new place (review of cdcbc14): this is for a lost drive.
            job.result('failed', 'Its drives are all here: use "Try again". Nothing was changed.', STUCK_ADVICE)
            return 1
        if not stuck.get('committed'):
            undo_cut_off(cut, stuck)
            words = 'The job was given up and undone: everything is as it was before it.'
        elif cut.action in ('move', 'back'):
            cut.reapply(stuck)
            if undo.get('here') and place_of(cut.config, kind) != undo['here']:
                wait_for(cut.state, {'job': 'retire', 'kind': kind, 'here': undo['here'], 'id': cut.id})
            words = (
                f'The job was given up. Your {KIND_WORDS.get(kind, kind)} are on '
                f'{place_words(place_of(cut.config, kind))}, as the settings say. Run setup again '
                '(sudo bash install.sh) so every app uses that place.'
            )
        elif cut.action == 'delete_old':
            wait_for(cut.state, delete_waits(cut.config, undo))
            words = 'The job was given up. The old copy is deleted when its drive is back.'
        elif cut.action == 'use' and undo.get('drive'):
            words = give_up_drive(cut, undo['drive'])
        elif cut.action == 'mirror_add':
            words = mirror_given_up(undo)
        else:
            cut.reapply(stuck)
            words = 'The job was given up. Run setup again (sudo bash install.sh) so every app is as the settings say.'
        cut.result('failed', words)
        cut.done()
    return 1


def mirror_given_up(undo):
    """The words for a mirror's rebuild let go (T5, review of 6940d3f: its pool may never
    come back, and setup does not rebuild a mirror): the recovery page's row for it."""
    said = 'The job was given up: the new drive was not made part of the mirror, which goes on with the drives it has.'
    # Each row named whole, so the docs test finds it in RECOVERY.md.
    if str(undo.get('array') or '').startswith('md-'):
        return said + ' To replace the failed drive, see RECOVERY.md, "A drive of a RAID array failed".'
    return said + ' To replace the failed drive, see RECOVERY.md, "A drive of a Btrfs pool failed".'


def give_up_drive(job, drive):
    """An added drive whose setup never worked, given up (T1-20): out of the settings and
    the record, its own fstab line taken out and unmounted; the disk is left as it is."""
    if any(place_of(job.config, kind) == drive for kind in KINDS):
        job.reapply(read_state(JOURNAL, job.state))
        return f'The job was given up. {place_words(drive)} holds your data now, so it stays in the settings.'
    (job.config.get('storage') or {}).pop(drive, None)
    job.save_settings()
    drop_fstab_line(extra_mount_root() / drive)
    if os.path.ismount(extra_mount_root() / drive):
        run(['umount', str(extra_mount_root() / drive)])
    return (
        f'The job was given up: {place_words(drive)} was not added. Its disk is left as it is; '
        'add it again from the Drives page.'
    )


def stopped_state(saved):
    """A job that ended with Stop: done after its commit point; failed before it changed
    anything (no journal yet, or still copying) or when setup refused the new place and
    the move was undone (unswitch); undone when it put things back."""
    if saved.get('phase') in ('unswitch', 'lost'):
        return 'failed'
    if saved.get('committed'):
        return 'done'
    if saved.get('phase') in (None, 'copy'):
        return 'failed'
    return 'undone'


def worked(job):
    job.result('working', '')
    try:
        if job.action == 'mirror_add':
            return mirror_result(job, mirror_job(job))
        message = WORK[job.action](job)
    except Stop as stop:
        saved = read_state(JOURNAL, job.state) or {}
        state = stopped_state(saved)
        job.result(state, str(stop), stop.advice)
        job.done()
        return 0 if state == 'done' else 1
    except Refused as refused:
        job.result('failed', f'{refused} Nothing was changed.')
        job.done()
        return 1
    except Away as gone:
        saved = read_state(JOURNAL, job.state)
        if saved and saved.get('committed'):
            job.result('failed', f'{gone} The job is finished at the next start, without it if it is still away.')
            return 1
        job.result('failed', f'{gone} Nothing was changed.', gone.advice)
        job.done()
        return 1
    except Exception as error:  # noqa: BLE001
        # Said on the page; the journal decides what the boot does.
        saved = read_state(JOURNAL, job.state)
        if saved and saved.get('committed'):
            job.result('failed', f'The job stopped after the switch: {error}', 'It is finished at the next start.')
            return 1
        job.result('failed', f'The job stopped: {error}. Nothing was changed.')
        job.done()
        return 1
    job.result('done', message)
    job.done()
    write_state(LOOK, look(job.config), job.state)
    return 0


def mirror_result(job, fields, power_cut=False):
    job.result(fields['state'], fields['message'], fields.get('advice'), fields.get('power_cut', power_cut))
    job.done()
    return 0 if fields['state'] in ('done', 'resumed') else 1


def resume(config, settings_files, state=None, folder=None, out=print, hand_off=True):
    """At the start of the computer, before Docker: a job a power cut stopped is undone
    (before its commit point) or finished (after it). A finish that needs Docker (setup run
    again) is handed to a unit that runs after it."""
    waiting_work(config, state, out)
    saved = read_state(JOURNAL, state)
    if not saved:
        return 0
    job = Job(saved['request'], config, settings_files, state, folder, out)
    job.started = saved.get('started_at', job.started)
    gone = [p for p in cut_places(config, saved) if away(config, p)]
    if gone:
        saved = without_drive(job, saved, gone)
        if saved is None:
            return 1 if read_state(JOURNAL, job.state) else 0
    if job.action == 'mirror_add' and not saved.get('committed'):
        import mirror_add

        return mirror_result(job, mirror_add.resume(saved, mirror_context(job)), power_cut=True)
    if not saved.get('committed') and not undo_cut_off(job, saved):
        job.result('undone', UNDONE_WORDS.get(job.action, UNDONE_WORDS['move']), power_cut=True)
        job.done()
        return 0
    saved = committed_before_docker(job, saved)
    if job.action in ('move', 'back', 'use', 'mirror_add'):
        if hand_off:
            hand_to_finish(settings_files)
        return 0
    finish_cut_off(job, saved)
    return 0


def committed_before_docker(job, saved):
    """A committed job's part that must be done before an app starts: a lost switch undone
    (T1-14), the settings as the journal has them, an undo's files copied back."""
    if job.action in ('move', 'back') and (saved.get('phase') == 'lost' or switch_lost(job, saved)):
        # Before Docker, so no app starts on the new place that lost its files (T1-14).
        undo_lost_switch(job, job.request['kind'], saved['undo']['here'])
        saved = read_state(JOURNAL, job.state) or saved
    job.reapply(read_state(JOURNAL, job.state) or saved)
    if saved.get('phase') == 'unswitch' and job.action in ('move', 'back') and not saved['undo'].get('files_back'):
        unswitch_files(job, job.request['kind'], saved['undo']['here'])
    return saved


def hand_to_finish(settings_files):
    """The finish that needs Docker (setup run again), in a unit that runs after it."""
    run(
        [
            'systemd-run',
            '--no-block',
            f'--unit={FINISH_UNIT}',
            '--property=After=docker.service',
            '--property=TimeoutStartSec=infinity',
            sys_executable(),
            '-Es',
            str(Path(__file__).resolve()),
            'finish',
            *[f'--config={p}' for p in settings_files],
        ]
    )


UNDONE_WORDS = {
    'move': 'The power went off during the job. Everything is as it was before.',
    'use': (
        'The power went off while the disk was being erased, so it was not added. Its files are gone; erase it '
        'again from the Drives page to use it.'
    ),
}


def added_after_all(job, saved):
    """An erase cut off after the filesystem was made with the ids the journal holds: the
    drive is added after all (the commit point written now). Never erased again."""
    erased = saved.get('undo', {}).get('erased') or {}
    if erased.get('phase') != 'format' or not uuid_there(erased.get('uuid')):
        return False
    add_settings(job, saved['undo']['drive'], erased['uuid'], 'ext4')
    return True


def uuid_there(uuid):
    if not uuid:
        return False
    import subprocess

    subprocess.run(['udevadm', 'settle'], capture_output=True, timeout=60, check=False)
    found = subprocess.run(['blkid', '-U', uuid], capture_output=True, text=True, timeout=60, check=False)
    return found.returncode == 0 and bool(found.stdout.strip())


def sys_executable():
    import sys

    return sys.executable


def cut_places(config, saved):
    """The drives a cut-off job still needs: a move's or a "back"'s two (one, leaving a
    lost drive), the drive an old copy is deleted on."""
    from neko_common import place_holding

    undo = saved.get('undo') or {}
    if saved.get('job') == 'delete_old':
        return [p for p in (place_holding(config, Path(undo.get('deleting') or '/').parent),) if p]
    if saved.get('job') not in ('move', 'back'):
        return []
    if undo.get('here_away'):
        return [undo['to']] if undo.get('to') else []
    return [p for p in (undo.get('here'), undo.get('to')) if p]


def without_drive(job, saved, gone):
    """At the start, a cut-off job whose drive is away (T1-18), done as far as the drives
    that are there allow, never waiting on the one that is not: undone before its commit
    point; after it finished on the drive that is there (forward when the drive it left is
    away, back when the one it went to is); a delete kept for the drive. The journal for
    the resume to go on with, or None when the job ended here (or must wait for both)."""
    undo, words = saved.get('undo') or {}, ' '.join(away(job.config, p) for p in gone)
    here, to = undo.get('here'), undo.get('to')
    if not saved.get('committed'):
        undo_cut_off(job, saved)
        job.result('undone', f'The power went off during the job. {words} Nothing was changed.', power_cut=True)
        job.done()
        return None
    if job.action == 'delete_old':
        wait_for(job.state, delete_waits(job.config, undo))
        job.result('failed', f'{words} The old copy is deleted when it is back.', 'Plug it in.', power_cut=True)
        job.done()
        return None
    if len(gone) > 1 or job.action not in ('move', 'back'):
        job.result('failed', f'The job is finished when its drives are back. {words}', STUCK_ADVICE, power_cut=True)
        return None
    if gone == [here]:
        if saved.get('phase') in ('unswitch', 'lost'):
            forward_again(job, saved)
        job.journal(read_state(JOURNAL, job.state)['phase'], True, here_away=True)
        return read_state(JOURNAL, job.state)
    # The drive it went to is away: back to the one it left, which has everything.
    undo_lost_switch(job, job.request['kind'], here)
    job.journal('lost', True, away=to)
    return read_state(JOURNAL, job.state)


def forward_again(job, saved):
    """An undo (unswitch) that has to go to a drive that is lost now turned round: the new
    folders it had moved out of the way go back to their places, and the switch is kept."""
    kind, undo = job.request['kind'], saved['undo']
    to, staging = undo['to'], Path(undo.get('staging', ''))
    kept = staging.with_name(OLD_NAME.format(kind=kind, id=job.request.get('copy', '')))
    if job.action == 'back' and not staging.is_dir() and kept.is_dir():
        os.rename(kept, staging)
    for copy_, target, skip in zip(paths(job.config, kind, to, staging), paths(job.config, kind, to), kind_skips(kind)):
        free = not own_entries(target, skip) if skip else not target.exists()
        if copy_.exists() and free:
            target.parent.mkdir(parents=True, exist_ok=True)
            move_folder(copy_, target, skip)
    forward = [['locations', kind, None if kind == 'backups' and to == 'media' else to]]
    job.journal('start', True, change=forward, files_back=False)
    job.reapply(read_state(JOURNAL, job.state))


def undo_cut_off(job, saved):
    if job.action in ('move', 'back'):
        undo_move(job)
    elif job.action == 'use':
        return added_after_all(job, saved)
    return False


def restore_back_copy(job, saved):
    """A "back" undone: the old copy it was copying into keeps its old name again."""
    staging = Path(saved['undo'].get('staging', ''))
    if staging.is_dir() and not away(job.config, saved['undo'].get('to')):
        os.rename(staging, staging.with_name(OLD_NAME.format(kind=job.request['kind'], id=job.request['copy'])))


def finish_cut_off(job, saved):
    """After the commit point: the rest of the job, which never changes its outcome."""
    import shutil

    if job.action == 'delete_old':
        gone = Path(saved['undo'].get('deleting', ''))
        if gone.name.startswith('nekoserver-deleting-') and gone.exists():
            shutil.rmtree(gone)
        original = gone.with_name(gone.name.replace('nekoserver-deleting-', 'nekoserver-old-', 1))
        if original.name.startswith('nekoserver-old-') and original.exists():
            shutil.rmtree(original)
    elif job.action == 'release':
        drop_fstab_line(extra_mount_root() / saved['undo']['drive'])
    job.result('resumed', 'The power went off during the job; it was finished at the start.', power_cut=True)
    job.done()


def finish_cut_move(job, saved):
    """A move or a "back" cut off after its switch: finished (what was written at the old
    place meanwhile copied again, then setup, the old folders retired, the apps started),
    or, cut off while it was being undone (unswitch), undone."""
    import migrate

    kind, here = job.request.get('kind'), saved['undo']['here']
    if saved.get('phase') == 'lost' or switch_lost(job, saved):
        finish_lost(job, kind, here)
    if saved['undo'].get('here_away') and saved.get('phase') == 'unswitch':
        forward_again(job, saved)
        saved = read_state(JOURNAL, job.state)
    if saved.get('phase') == 'unswitch':
        unswitch(job, kind, here, saved['undo'].get('stopped'), saved['undo'].get('why', ''))
    stopped = migrate.stop_writers(
        out=job.out, note=lambda units: job.journal('start', True, stopped=units), own=left_alone()
    )
    if not away(job.config, here):
        news = paths(job.config, kind, saved['undo']['to'])
        for old, new, skip in zip(paths(job.config, kind, here), news, kind_skips(kind)):
            if old.exists() and not os.path.ismount(old) and new.exists():
                migrate.copy(old, new, final=True, out=job.out, **copy_skip(skip))
    finish_move(job, kind, here, stopped)


# How often the finish after Docker tries for the operation lock, 10 s apart (a job a
# timer started as the PC came up may hold it for a few minutes).
FINISH_LOCK_TRIES = 60


def finish(config, settings_files, state=None, folder=None, out=print, sleep=time.sleep):
    """After Docker, at the start: a cut-off move or added drive finished (setup run again,
    then the old folders retired and the apps started), under the operation lock as the
    job itself was, so the control service's app buttons wait for it too (control.rs)."""
    from neko_common import OPERATION_LOCK, hold_operation

    saved = read_state(JOURNAL, state)
    if not saved or not saved.get('committed'):
        return 0
    job = Job(saved['request'], config, settings_files, state, folder, out)
    for _ in range(FINISH_LOCK_TRIES):
        held = hold_operation(OPERATION_LOCK)
        if held is not None:
            with held:
                return finish_held(job, saved)
        sleep(10)
    job.result('failed', FACTS.get('busy', 'Another job is at work.'), 'It is tried again at the next start.')
    return 1


def finish_held(job, saved):
    config = job.config
    job.reapply(saved)
    try:
        if job.action == 'mirror_add':
            return finish_mirror(job, saved)
        if job.action == 'use':
            setup_again(job)
        else:
            finish_cut_move(job, saved)
    except Stop as stop:
        state = stopped_state(read_state(JOURNAL, job.state) or saved)
        job.result(state, str(stop), stop.advice, power_cut=True)
        job.done()
        write_state(LOOK, look(config), job.state)
        return 0 if state == 'done' else 1
    except Exception as error:  # noqa: BLE001
        job.result('failed', f'Finishing the job at the start stopped: {error}', STUCK_ADVICE)
        return 1
    job.result('resumed', 'The power went off during the job; it was finished at the start.', power_cut=True)
    job.done()
    write_state(LOOK, look(config), job.state)
    return 0


def finish_mirror(job, saved):
    """A Btrfs replacement a power cut stopped: Btrfs goes on with it by itself; it is
    waited for here (after the start, never holding it up) and the balance run. The journal
    is mirror_add's to clear: a pool that is not open keeps it for the next start."""
    import mirror_add

    fields = mirror_add.finish(saved, mirror_context(job))
    state = 'resumed' if fields['state'] == 'done' else fields['state']
    job.result(state, fields['message'], fields.get('advice'), power_cut=True)
    (job.folder / PROGRESS).unlink(missing_ok=True)
    return 0 if state == 'resumed' else 1


def main(argv=None):
    import argparse

    from neko_common import read_settings, take_places

    parser = argparse.ArgumentParser(description='The Drives page\'s root jobs (storage from the dashboard).')
    parser.add_argument('action', choices=('run', 'resume', 'finish'))
    parser.add_argument('--config', action='append', required=True)
    parser.add_argument('--request', default=str(REQUEST))
    given = parser.parse_args(argv)
    config = read_settings(given.config[0])
    take_places(config, given.config)
    if given.action == 'run':
        return run_request(read_request(request_path(given.request)), config, given.config)
    return (resume if given.action == 'resume' else finish)(config, given.config)


if __name__ == '__main__':
    raise SystemExit(main())
