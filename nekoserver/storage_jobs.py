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
import os
import sqlite3
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
        pids = [p for p in os.listdir(proc) if p.isdigit()]
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
    data = config.get('data_mount')
    if not data:
        return set()
    catalogue = Path(data) / SHARED['cards']['folder'] / SHARED['cards']['catalogue']
    if not catalogue.is_file():
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


def in_use(everything, world):
    """Whether any of these devices is mounted (in any namespace) or used as swap."""
    mounted = any(r.get('maj:min') in world['mounted'] or any(r.get('mountpoints') or []) for r in everything)
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
    shown = f'{disk}p{number}' if disk[-1].isdigit() else f'{disk}{number}'
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
    minimum = SHARED['storage_jobs']['min_bytes'] if minimum_bytes is None else minimum_bytes
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
