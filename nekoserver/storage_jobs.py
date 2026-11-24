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
    shown = json.loads(run(['lsblk', '-J', '-o', 'PATH,TYPE,PKNAME,PARTN', like]))['blockdevices'][0]
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
    if not root.is_dir():
        return f'{place_words(to)} is not connected.'
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
        found.append({'to': to, 'words': place_words(to), 'ok': why is None, 'why_not': why})
    return found


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


# The page's own units, which only read: the Drives page keeps showing the job while the
# apps are stopped for the last copy. The control service is stopped with the rest: its
# app buttons (restart, update) would start an app in the middle of the move.
PAGE_UNITS = ('nekoserver-dashboard.service', 'nekoserver-dashboard-collect.service')


def left_alone():
    """The units a move of one kind leaves running: its own and the page's. A whole-drive
    move stops them all (the drive itself goes away)."""
    return (*OWN_UNITS, *PAGE_UNITS)


JOBS = ('look', 'use', 'release', 'move', 'back', 'delete_old', 'mirror_add')


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
            },
            self.state,
        )

    def done(self):
        for path in (self.state / JOURNAL, self.folder / PROGRESS):
            path.unlink(missing_ok=True)

    def save_settings(self):
        from neko_common import atomic_json

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


def next_drive(config):
    from neko_common import extra_drives

    taken = set(extra_drives(config))
    return next(f'd{n}' for n in range(3, 100) if f'd{n}' not in taken)


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
    name = next_drive(job.config)
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
    migrate.setup_again(job.config, job.settings_files[-1], log, out=job.out)


# ---- B: move one kind of data -----------------------------------------------------------------


def paths(config, kind, place, base=None):
    """The folders of a kind at a place (or inside `base`, a copy's folder there)."""
    root = Path(base) if base else root_of(config, place)
    return [root / rel for rel in kind_folders(kind)]


def gone_or_empty(folder):
    """Make way for a folder moved in: nothing there, or an empty folder (a mount point
    setup closed, chattr +i) taken away."""
    folder = Path(folder)
    if not folder.exists() and not folder.is_symlink():
        return
    if folder.is_symlink() or not folder.is_dir() or any(folder.iterdir()):
        raise Stop(f'{folder} is in the way: it is not empty. Nothing was changed.')
    run(['chattr', '-i', str(folder)]) if shutil_which('chattr') else None
    folder.rmdir()


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
    size = du(paths(job.config, kind, here))
    problem = target_problem(job.config, kind, to, size)
    if problem:
        raise Stop(problem + ' Nothing was changed.')
    staging = root_of(job.config, to) / NEW_COPY.format(kind=kind, id=job.id)
    new = paths(job.config, kind, to, staging)
    for folder in paths(job.config, kind, to):
        if folder.exists() and not os.path.ismount(folder) and any(folder.iterdir()):
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
    except Exception:  # noqa: BLE001  (no record of the steps: the check after the move is the dashboard's)
        return []


def copy_all(job, sources, targets_, size, final):
    import migrate

    job.progress('last_copy' if final else 'copy', bytes_total=size)
    for source, target in zip(sources, targets_):
        if not source.exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        migrate.copy(source, target, final=final, out=job.out, again=not final and any(target.iterdir()))


def switch_data(job, kind, here, to, sources, staged, size):
    """Copy while the apps run, stop them, copy what changed, compare, then switch."""
    import migrate

    try:
        copy_all(job, sources, staged, size, final=False)
        job.journal('stop', False)
        job.progress('stop')
        stopped = migrate.stop_writers(
            out=job.out, note=lambda units: job.journal('stop', False, stopped=units), own=left_alone()
        )
        copy_all(job, sources, staged, size, final=True)
        job.journal('compare', False)
        job.progress('compare')
        for source, target in zip(sources, staged):
            if source.exists() and migrate.differences(source, target):
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
    if kind == 'backups' and here != 'media':
        unbind_backups(job.config)
    for target in paths(job.config, kind, to):
        gone_or_empty(target)
    for copy_, target in zip(staged, paths(job.config, kind, to)):
        if copy_.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(copy_, target)
    job.commit('switch', [['locations', kind, None if kind == 'backups' and to == 'media' else to]])


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
    old = root_of(job.config, here) / OLD_NAME.format(kind=kind, id=job.id)
    for folder, rel in zip(paths(job.config, kind, here), kind_folders(kind)):
        if folder.exists() and not os.path.ismount(folder):
            (old / rel).parent.mkdir(parents=True, exist_ok=True)
            os.rename(folder, old / rel)
    staging = Path(job.undo().get('staging', ''))
    if staging.name.startswith('nekoserver-new-') and staging.is_dir():
        import shutil

        shutil.rmtree(staging)


def finish_move(job, kind, here, stopped):
    """After the commit point: setup run again, the old folders retired, the apps started."""
    import migrate

    job.journal('start', True)
    job.progress('start')
    try:
        if kind == 'backups':
            # Setup never mounts the backups' new place over files at the old one.
            retire_old(job, kind, here)
        setup_again(job)
        retire_old(job, kind, here)
    finally:
        migrate.start_again(stopped, job.out)
    job.progress('check')
    before = job.undo().get('before') or []
    problem = before and migrate.server_check(job.config, before)
    if problem:
        raise Stop(f'Your {KIND_WORDS[kind]} were moved, but the server check after it says: {problem}')


def undo_move(job):
    """Before the commit point: the new copy taken out of the apps' places again and
    deleted; the old folders were never touched."""
    import shutil

    journal = read_state(JOURNAL, job.state) or {}
    saved, switching = journal.get('undo', {}), journal.get('phase') == 'switch'
    staging = Path(saved.get('staging', ''))
    kind, to, here = job.request['kind'], saved.get('to'), saved.get('here')
    if to and switching:
        for copy_, target in zip(paths(job.config, kind, to, staging), paths(job.config, kind, to)):
            if target.exists() and not os.path.ismount(target) and not copy_.exists():
                copy_.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, copy_)
    if kind == 'backups' and here not in (None, 'media') and switching:
        rebind_backups(job.config, job.out)
    if job.action == 'move' and staging.name.startswith('nekoserver-new-') and staging.is_dir():
        shutil.rmtree(staging)
    if job.action == 'back':
        restore_back_copy(job, journal)


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
    staging = root_of(job.config, to) / NEW_COPY.format(kind=kind, id=job.id)
    job.journal('copy', False, staging=str(staging), here=here, to=to, before=steps_working(job.config))
    os.rename(found['path'], staging)
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

    return mirror_add.Context(
        run=ran,
        erase=lambda *given, **named: erase(*given, config=job.config, **named),
        refused=Refused,
        journal=lambda phase, committed=False, undo=None: job.journal(phase, committed, **(undo or {})),
        progress=job.progress,
        clear=lambda: (job.state / JOURNAL).unlink(missing_ok=True),
    )


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
    if read_state(JOURNAL, job.state):
        job.result('failed', 'A job that was cut off is not finished yet.', 'It is finished at the next start.')
        return 1
    held = hold_operation(OPERATION_LOCK)
    if held is None:
        job.result('failed', FACTS.get('busy', 'Another job is at work.'), 'Try again later.')
        return 1
    with held:
        return worked(job)


def stopped_state(saved):
    """A job that ended with Stop: done after its commit point; failed before it changed
    anything (no journal yet, or still copying); undone when it put things back."""
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
    except Exception as error:  # noqa: BLE001  (said on the page; the journal decides what the boot does)
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


def resume(config, settings_files, state=None, folder=None, out=print):
    """At the start of the computer, before Docker: a job a power cut stopped is undone
    (before its commit point) or finished (after it). A finish that needs Docker (setup run
    again) is handed to a unit that runs after it."""
    saved = read_state(JOURNAL, state)
    if not saved:
        return 0
    job = Job(saved['request'], config, settings_files, state, folder, out)
    job.started = saved.get('started_at', job.started)
    missing = missing_drive(config, saved)
    if missing:
        job.result('failed', f'The job is finished when {place_words(missing)} is back.', 'Plug it in and restart.')
        return 1
    if job.action == 'mirror_add' and not saved.get('committed'):
        import mirror_add

        return mirror_result(job, mirror_add.resume(saved, mirror_context(job)), power_cut=True)
    if not saved.get('committed') and not undo_cut_off(job, saved):
        job.result('undone', UNDONE_WORDS.get(job.action, UNDONE_WORDS['move']), power_cut=True)
        job.done()
        return 0
    job.reapply(read_state(JOURNAL, job.state) or saved)
    if job.action in ('move', 'back', 'use', 'mirror_add'):
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
        return 0
    finish_cut_off(job, saved)
    return 0


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


def missing_drive(config, saved):
    """A drive the cut-off job needs that is not there now."""
    for place in {saved.get('undo', {}).get('here'), saved.get('undo', {}).get('to')} - {None}:
        try:
            if not root_of(config, place).is_dir():
                return place
        except RuntimeError:
            return place
    return None


def undo_cut_off(job, saved):
    if job.action in ('move', 'back'):
        undo_move(job)
    elif job.action == 'use':
        return added_after_all(job, saved)
    return False


def restore_back_copy(job, saved):
    """A "back" undone: the old copy it was copying into keeps its old name again."""
    staging = Path(saved['undo'].get('staging', ''))
    if staging.is_dir():
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


def finish(config, settings_files, state=None, folder=None, out=print):
    """After Docker, at the start: a cut-off move or added drive finished (setup run again,
    then the old folders retired and the apps started)."""
    import migrate

    saved = read_state(JOURNAL, state)
    if not saved or not saved.get('committed'):
        return 0
    job = Job(saved['request'], config, settings_files, state, folder, out)
    job.reapply(saved)
    kind = job.request.get('kind')
    try:
        if job.action == 'mirror_add':
            return finish_mirror(job, saved)
        if job.action == 'use':
            setup_again(job)
        else:
            here = saved['undo']['here']
            stopped = migrate.stop_writers(
                out=out, note=lambda units: job.journal('start', True, stopped=units), own=left_alone()
            )
            for old, new in zip(paths(config, kind, here), paths(config, kind, saved['undo']['to'])):
                if old.exists() and not os.path.ismount(old) and new.exists():
                    migrate.copy(old, new, final=True, out=out)
            finish_move(job, kind, here, stopped)
    except Exception as error:  # noqa: BLE001
        job.result('failed', f'Finishing the job at the start stopped: {error}', 'It is tried again at the next start.')
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

    from neko_common import read_settings

    parser = argparse.ArgumentParser(description='The Drives page\'s root jobs (storage from the dashboard).')
    parser.add_argument('action', choices=('run', 'resume', 'finish'))
    parser.add_argument('--config', action='append', required=True)
    parser.add_argument('--request', default=str(REQUEST))
    given = parser.parse_args(argv)
    config = read_settings(given.config[0])
    if given.action == 'run':
        return run_request(read_request(request_path(given.request)), config, given.config)
    return (resume if given.action == 'resume' else finish)(config, given.config)


if __name__ == '__main__':
    raise SystemExit(main())
