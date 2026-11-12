"""Everything connected to this PC, as setup shows it before it asks for a drive (0.50.0).

One table serves every drive question. The terminal prints it and the setup window
draws it; each role (the app SSD, the media HDD, the USB backup drive) is chosen by a
row's number (nothing is typed back: choosing formats nothing). A row says what the drive is in
plain words, and every later message names the drive the same way, with the number it
had: never by its Linux name alone, since sdb can be sdc at the next start. NekoServer
itself remembers a drive by its UUID, which the table shows and nobody has to type.

Nothing here changes a drive. It reads the list lsblk gave (modules/storage.inventory_now)
and looks at how full a drive is.
"""

import contextlib
import os
import re
import shutil
import subprocess

import setup_progress
from neko_common import extra_drives, media_apps_wanted, typed

# The roles a drive can have: (what setup calls it, what goes on it, the same with the
# media apps left out).
ROLES = {
    'data_ssd': ('the app SSD', 'your apps, photos and music', 'your apps and their settings'),
    'media_hdd': (
        'the media HDD',
        'your movies, shows, Nextcloud files and the backups',
        'your Nextcloud files and the backups',
    ),
    'usb': ('the USB backup drive', 'the weekly copy of your photos and files', 'the weekly copy of your files'),
}
NAMES_CHANGE = (
    'The short names (sdb1, nvme0n1p2) are Linux\'s own and can change at the next start. NekoServer '
    'remembers a drive by its UUID, which you never have to type.'
)
NEVER_FORMATS = 'Setup never formats a drive and changes nothing by showing this list.'


# Where each app keeps your files, as chosen on the storage page before the approval
# (modules/storage.choose_ahead), for the storage step of the same run: {'photos': 'ssd' |
# 'media' | a row of this list}. Kept here because the installer loads its steps as
# modules of their own, and this one is shared by all of them.
PLACES_AHEAD = {}


def role(key):
    """'the app SSD', 'the media HDD' or 'the USB backup drive'."""
    return ROLES[key][0]


def holds(config, key):
    """What goes on the drive of this role, on this server."""
    return ROLES[key][1 if media_apps_wanted(config) else 2]


def asked_for(config, key):
    """The role as every drive question names it: what goes on the drive, then its name."""
    return f'the drive for {holds(config, key)} ({role(key)})'


# ----- what each row is


def kind_of(disk):
    """A disk's type in a word or two, from how it is connected and whether it spins."""
    how, name = disk.get('tran'), str(disk.get('name', ''))
    if how == 'iscsi':
        return 'network disk (iSCSI)'
    if how == 'usb':
        return 'USB drive'
    if how == 'nvme' or name.startswith('nvme'):
        return 'NVMe SSD'
    if name.startswith('mmcblk'):
        return 'SD card or eMMC'
    if disk.get('rota') in (False, 0, '0'):
        return 'SSD'
    if disk.get('rota') in (True, 1, '1'):
        return 'HDD'
    return 'drive'


def still_that_drive(name, uuid, by_uuid=None):
    """The device lsblk listed under this UUID is still the one Linux has under it."""
    from modules.storage import BY_UUID, DEV

    try:
        return bool(uuid) and os.path.realpath((by_uuid or BY_UUID) + uuid) == os.path.realpath(DEV + name)
    except OSError:
        return False


def ext_fullness(name, run=subprocess.run):
    """How full an ext2, ext3 or ext4 drive that is not open is, in percent, read from its
    own header (dumpe2fs -h, which changes nothing), or None when that can't be read."""
    from modules.storage import DEV

    try:
        result = run(['dumpe2fs', '-h', DEV + name], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found = dict(re.findall(r'^(Block count|Free blocks):\s+(\d+)\s*$', result.stdout, re.MULTILINE))
    total, free = int(found.get('Block count', 0)), int(found.get('Free blocks', 0))
    return round((total - free) * 100 / total) if total else None


def look_inside(name, uuid, fstype, mountpoint):
    """(how full in percent, whether it holds files) for one filesystem; either is None
    when setup cannot tell without opening the drive, which it does not do here."""
    if mountpoint:
        try:
            usage = shutil.disk_usage(mountpoint)
            with os.scandir(mountpoint) as entries:
                files = any(entry.name != 'lost+found' for entry in entries)
        except OSError:
            return None, None
        return (round(usage.used * 100 / usage.total) if usage.total else None), files
    if fstype in ('ext2', 'ext3', 'ext4') and still_that_drive(name, uuid):
        return ext_fullness(name), None
    return None, None


# What lsblk lists that is no drive of yours: files attached as disks, CD drives, memory.
NOT_A_DRIVE = re.compile(r'(loop|sr|zram|ram|nbd|fd)\d|mmcblk\d+(boot|rpmb)')
# What a partition holds when it is part of something else (listed under it), or swap.
PART_OF_ANOTHER = ('LVM2_member', 'crypto_LUKS', 'linux_raid_member', 'swap')
HOW_TO_FORMAT = (
    'format it as ext4 yourself with the Disks app (GNOME) or KDE Partition Manager, or in a terminal with  '
    'sudo mkfs.ext4 /dev/{name}  (this erases what is on it; setup never formats a drive)'
)


def entry(dev, disk, **more):
    """One row of the table from one lsblk row and the disk it is on."""
    from modules.storage import mount_points

    return {
        'name': str(dev.get('name', '')),
        'size': dev.get('size'),
        'size_note': '',
        'type': kind_of(disk),
        'model': str(dev.get('model') or disk.get('model') or '').strip(),
        'label': dev.get('label') or '',
        'fstype': dev.get('fstype') or '',
        'uuid': dev.get('uuid') or '',
        'mountpoint': dev.get('mountpoint') or next(iter(sorted(mount_points(dev))), None),
        'disks': [str(disk.get('name', ''))],
        'full': None,
        'files': None,
        'system': False,
        'problem': '',  # why it can be no drive of NekoServer's at all
        'export': '',  # the address, for a folder a NAS shares (NFS)
        'device': None,  # as the app SSD or the media HDD (modules/storage.candidate)
        'backup': None,  # as the USB backup drive (modules/photo_backup.eligible)
        **more,
    }


def filesystem_row(dev, disk, usable, backups, look):
    """The row of one filesystem: as setup would use it when it can, as lsblk shows it otherwise."""
    row = entry(dev, disk)
    device = usable.get(row['uuid'])
    if device:
        # A pool or an array over several drives: its name, size and disks as setup counts them.
        row.update(
            name=device['name'],
            size=device.get('size'),
            size_note=device.get('size_note') or '',
            mountpoint=device.get('mountpoint'),
            disks=list(device.get('physical_disks') or row['disks']),
            device=device,
        )
    row['backup'] = backups.get(row['uuid'])
    row['full'], row['files'] = look(row['name'], row['uuid'], row['fstype'], row['mountpoint'])
    return row


def shared_rows(config):
    """The folders a NAS shares over NFS that the network storage step chose: each is a
    row, so the list is everything this server keeps files on."""
    from modules.photo_backup import shared_backup_folder
    from modules.storage import shared_folders

    # A record that is wrong is left out here: the step that uses it says what is wrong with it.
    found = {}
    try:
        found.update(shared_folders(config))
    except RuntimeError:
        pass
    try:
        backup = shared_backup_folder(config)
    except RuntimeError:
        backup = None
    if backup:
        found.setdefault(config.get('photos', {}).get('external_backup_uuid'), backup)
    return [
        entry({'name': address, 'uuid': identity, 'fstype': 'nfs'}, {}, type='network folder (NFS)', export=address)
        for identity, address in found.items()
    ]


def with_children(dev, disk):
    """A device and everything under it, each with the disk it is on."""
    yield dev, disk
    for child in dev.get('children', []):
        yield from with_children(child, disk)


def rows_of(top, seen, usable, backups, look):
    """The rows of one disk that is not the system's: every filesystem on it (one that
    spans several disks once, under the first), or the disk itself with the reason when
    nothing on it can be used."""
    from modules import storage

    found = []
    for dev, _ in with_children(top, top):
        fstype = dev.get('fstype')
        if fstype and fstype not in PART_OF_ANOTHER:
            identity = dev.get('uuid') or dev.get('name')
            if identity not in seen:  # a pool or an array is listed under each of its disks
                seen.add(identity)
                found.append(filesystem_row(dev, top, usable, backups, look))
        elif storage.empty_disk(dev):
            how = HOW_TO_FORMAT.format(name=dev.get('name', ''))
            found.append(entry(dev, top, problem='it has no filesystem yet. To use it, ' + how))
    if not found and top.get('type') == 'disk':
        found.append(entry(top, top, problem='nothing on it is a filesystem Linux apps can keep files on'))
    return found


def not_a_drive(top):
    """A loop device, an optical drive, or an empty card-reader slot (a disk of no size)."""
    return bool(
        NOT_A_DRIVE.match(str(top.get('name', ''))) or top.get('type') in ('loop', 'rom') or top.get('size') == '0B'
    )


def rows(inventory, config, items=None, look=None):
    """Every drive connected to this PC, in lsblk's order, each with the number it is
    chosen by: the disk Linux runs from (one row), every filesystem on the other disks,
    every disk with nothing on it yet, and the folders a NAS shares. `items`: the drives
    setup may use as it found them already (modules/storage.discover_filesystems)."""
    from modules import storage
    from modules.photo_backup import eligible

    look = look or look_inside
    usable = {v['uuid']: v for v in (storage.discover_filesystems(inventory) if items is None else items)}
    backups = {v['uuid']: v for v in (eligible(config, inventory) if config.get('external_backup_mount') else [])}
    tops = inventory.get('blockdevices', [])
    placed = [pair for top in tops for pair in with_children(top, top.get('name', ''))]
    os_disks, _ = storage.system_disks(placed)
    found, seen = [], set()
    for top in tops:
        if not_a_drive(top):
            continue
        if str(top.get('name', '')) in os_disks:
            found.append(entry(top, top, system=True, fstype='', uuid='', label='', mountpoint=None))
        else:
            found += rows_of(top, seen, usable, backups, look)
    found += shared_rows(config)
    on_disks = {row['uuid']: row['disks'] for row in found if row['uuid']}
    for number, row in enumerate(found, 1):
        row['number'] = number
        row['on_disks'] = on_disks  # every drive's disks: two roles never share one
        for device in (row['device'], row['backup']):
            if device is not None:
                device['said'] = brief(row)
    return found


# ----- how a row is said


def size_of(row):
    from modules.storage import size_words

    return size_words(row.get('size')) if row.get('size') else ''


def words(row):
    """What the drive is: its size and type, its model, the name it was given (its label)."""
    if row['export']:
        return 'a folder the NAS shares (NFS)'
    said = [' '.join(part for part in (size_of(row), row['type']) if part)]
    if row['model']:
        said.append(row['model'])
    if row['label']:
        said.append(f'named "{row["label"]}"')
    if len(row['disks']) > 1:
        said.append('over ' + ' and '.join(row['disks']))
    return ', '.join(said)


def brief(row):
    """The drive as every message after the list names it: its number there, its Linux
    name and what it is."""
    return f"drive {row['number']} ({row['name']}: {words(row)})"


def said(device, first=False):
    """A chosen drive as the list named it (`first`: to start a sentence with); its Linux
    name alone when it came from no list."""
    named = device.get('said')
    if not named:
        return str(device.get('name', ''))
    return named[0].upper() + named[1:] if first else named


def fullness(row):
    """'about 40 % full, holds files', 'empty', or '' when setup cannot tell."""
    if row['files'] is False:
        return 'empty'
    known = [] if row['full'] is None else [f"about {row['full']} % full"]
    return ', '.join(known + (['holds files'] if row['files'] else []))


def inside(row):
    """The filesystem's facts: its type and UUID, where it is open, how full it is."""
    if row['export'] or not row['fstype']:
        return ''
    from modules.storage import NOT_LINUX

    facts = [f"{NOT_LINUX.get(row['fstype'], row['fstype'])}, UUID {row['uuid'] or 'none'}"]
    if (row['device'] or {}).get('wwn') and row['device'].get('network'):
        facts.append(f"the NAS disk {row['device']['wwn']}")
    facts.append(f"open at {row['mountpoint']}" if row['mountpoint'] else 'not open')
    if fullness(row):
        facts.append(fullness(row))
    if row['size_note']:
        facts.append(row['size_note'])
    return '; '.join(facts)


def system_roles(config):
    """The roles kept on the disk Linux runs from (a folder of it each)."""
    storage = config.get('storage', {})
    return [
        key for key in ('data_ssd', 'media_hdd') if isinstance(storage.get(key), dict) and storage[key].get('folder')
    ]


def role_of(row, config):
    """The role this drive has in the settings now ('data_ssd', 'media_hdd', 'usb'), or None.
    The disk Linux runs from may have both of the first two: the first is named."""
    if row['system']:
        return next(iter(system_roles(config)), None)
    if not row['uuid']:
        return None
    storage = config.get('storage', {})
    for key in ('data_ssd', 'media_hdd'):
        if (storage.get(key) or {}).get('uuid') == row['uuid']:
            return key
    return 'usb' if config.get('photos', {}).get('external_backup_uuid') == row['uuid'] else None


# Said with the disk Linux runs from wherever it can be chosen (0.50.0: a PC with one
# drive, or with one drive beside it, is a server too).
SYSTEM_DISK = (
    'It is the disk Linux runs from: NekoServer keeps this in a folder of it (/var/lib/nekoserver). If that disk '
    'fails, Linux and what is kept there are lost together, and a disk that fills up stops Linux too. A drive of '
    'its own is the safer choice.'
)
# Said when the backups go to the disk the apps are on.
SAME_DISK_BACKUPS = (
    'The backups are then on the same disk as what they copy. They still help after a mistake or a broken app, '
    'but not when that disk fails: an offline backup on a removable drive is what covers that.'
)
SYSTEM_NOT_OFFLINE = (
    'it is the disk Linux runs from; the offline backup must be a drive of its own that you can take away'
)
SYSTEM_FILESYSTEMS = ('ext4', 'xfs', 'btrfs')


def system_verdict(key, config):
    """Why the disk Linux runs from cannot have the role `key`, or '': it can hold the
    apps, the media and backups, or both, when its filesystem is one the apps can use and
    it has room for what is put there (the minimums of both roles when both are on it)."""
    from modules import storage

    if key == 'usb':
        return SYSTEM_NOT_OFFLINE
    try:
        _uuid, fstype = storage.system_filesystem()
        free = storage.system_free_gb()
    except (OSError, RuntimeError):
        return 'setup cannot read the filesystem Linux runs from'
    if fstype not in SYSTEM_FILESYSTEMS:
        return f'Linux runs from a {fstype} filesystem, which the apps cannot keep their files on'
    needed = sum(storage.sizes(role_key)[0] for role_key in {key, *system_roles(config)})
    if free < needed:
        both = ' together with what is already kept there' if set(system_roles(config)) - {key} else ''
        return f'it has too little room: {free} GB free, and {role(key)} needs at least {needed} GB there{both}'
    return ''


def in_use(row, folder):
    """Why a drive open somewhere of its own cannot be taken, or ''. One the desktop opened
    by itself (/run/media, /media), or one open where setup would put it, is free."""
    where = row['mountpoint']
    if not where or where == folder or str(where).startswith(('/run/media/', '/media/')):
        return ''
    return f'it is in use at {where}. To use it, unmount it first (sudo umount {where}, and take out its line in /etc/fstab if you added one)'


def verdict(row, key, config):
    """Why this drive cannot have the role `key`, in words that follow "cannot be the app
    SSD: "; '' when it can."""
    if row['system']:
        return system_verdict(key, config)
    if row['problem']:
        return row['problem']
    has = role_of(row, config)
    if has and has != key:
        return f'it is already {role(has)}'
    if row['uuid'] and any(record.get('uuid') == row['uuid'] for record in extra_drives(config).values()):
        return "it already holds an app's files (chosen as a drive of their own)"
    if row['export']:
        return 'it is a folder the NAS shares; the network storage step chooses those'
    return backup_verdict(row, config) if key == 'usb' else drive_verdict(row, key, config)


def drive_verdict(row, key, config):
    """Why a filesystem cannot be the app SSD or the media HDD, or ''."""
    from modules import storage

    device = row['device']
    if device is None:
        if row['fstype'] in ('ext4', 'xfs', 'btrfs'):
            return 'it is not a drive, a partition, an LVM volume, a RAID array or an encrypted drive'
        return (
            f"it is formatted {storage.NOT_LINUX.get(row['fstype'], row['fstype'])}, which Linux apps can't keep "
            'their files on. To use it, copy its files elsewhere first, then ' + HOW_TO_FORMAT.format(name=row['name'])
        )
    busy = in_use(row, config.get('data_mount' if key == 'data_ssd' else 'media_mount'))
    if busy:
        return busy
    if key == 'data_ssd' and device.get('network'):
        return (
            'it is on the network storage box, and the app SSD must be connected to this PC (by USB 3, or '
            'inside it): it holds the databases and is what makes the apps fast'
        )
    other = (config.get('storage', {}).get('media_hdd' if key == 'data_ssd' else 'data_ssd') or {}).get('uuid')
    if other and set(row['disks']) & set(row['on_disks'].get(other, ())):
        return 'it is on the same disk as ' + role('media_hdd' if key == 'data_ssd' else 'data_ssd')
    smallest, _ = storage.sizes(key)
    if not storage.big_enough(device, smallest):
        counted = f" ({row['size_note']})" if row['size_note'] else ''
        return (
            f'it is too small{counted}: {role(key)} needs a drive of at least {storage.drive_needed(smallest)} GB '
            f'({smallest} GB free once formatted)'
        )
    return ''


def backup_verdict(row, config):
    """Why a filesystem cannot be the USB backup drive, or ''."""
    from modules.photo_backup import BACKUP_FILESYSTEMS, UNSUITABLE

    if row['backup'] is not None:
        return ''
    if row['fstype'] not in BACKUP_FILESYSTEMS:
        return (
            f"it is formatted {UNSUITABLE.get(row['fstype'], row['fstype'])}; a backup drive must be exFAT or ext4 "
            '(FAT32 cannot hold files over 4 GB, such as videos). Copy off anything you need from it, then format '
            'it yourself (this erases it) with the Disks app (GNOME) or KDE Partition Manager'
        )
    busy = in_use(row, config.get('external_backup_mount'))
    if busy:
        return busy
    return (
        'it must be a drive of its own: this one is on the same disk as the system, the app SSD or the media '
        'HDD, and one disk failing would then lose both copies'
    )


def usable(table, key, config):
    """The rows that can have this role."""
    return [row for row in table if not verdict(row, key, config)]


def can_be(row, config):
    """What the full list says under a drive: the role it has, the roles it can have, or
    why it can have none."""
    has = role_of(row, config)
    if has:
        return f'Chosen as {role(has)}.'
    roles = [role(key) for key in ROLES if not verdict(row, key, config)]
    if not roles:
        return f"Cannot be used: {verdict(row, 'media_hdd', config)}."
    return 'Can be ' + (', '.join(roles[:-1]) + ' or ' + roles[-1] if len(roles) > 1 else roles[0]) + '.'


_last_shown = None  # the list last printed in this run (show_once)


def forget_shown():
    """The next show_once prints the list whatever was printed before (a new run)."""
    global _last_shown
    _last_shown = None


def show_once(table, config, out_fn=print):
    """The full list, printed before the first drive question of a run and again only
    when what is connected has changed since."""
    global _last_shown
    now = [(row['name'], row['uuid']) for row in table]
    if now != _last_shown:
        _last_shown = now
        for line in shown(table, config):
            out_fn(line)


def shown(table, config):
    """The full list as the terminal prints it before the first drive question."""
    lines = ['Everything connected to this PC:']
    for row in table:
        lines.append(f"  {row['number']}) {row['name']}: {words(row)}")
        if inside(row):
            lines.append('       ' + inside(row))
        lines.append('       ' + can_be(row, config))
    return [*lines, NAMES_CHANGE, NEVER_FORMATS]


def needs(key):
    """What a drive of this role must have, said with its question ('' for the USB drive)."""
    if key == 'usb':
        return ''
    from modules import storage

    smallest, recommended = storage.sizes(key)
    return (
        f'It needs at least {smallest} GB free, so a drive of {storage.drive_needed(smallest)} GB or more '
        f'({recommended} GB or more is recommended).'
    )


def listed(table, key, config):
    """The lines before one drive question: what is asked, then every drive by its number,
    each with why it cannot be this one when it cannot."""
    lines = [' '.join(part for part in (f'Now choose {asked_for(config, key)}.', needs(key)) if part)]
    for row in table:
        why = verdict(row, key, config)
        facts = ', '.join(
            part for part in (words(row), row['fstype'] if not row['export'] else '', fullness(row)) if part
        )
        # The reason in short: how to make the drive usable is in the full list above, and
        # said again when that drive is chosen all the same.
        short = why.split('. ')[0]
        lines.append(f"  {row['number']}) {row['name']}: {facts}" + (f'  - not this one: {short}' if why else ''))
    return lines


def for_window(table, key, config):
    """The same list as the setup window draws it: one card for each drive."""
    return [
        {
            'number': row['number'],
            'name': row['name'],
            'what': words(row),
            'inside': inside(row),
            'role': (f'Chosen as {role(has)}' if (has := role_of(row, config)) else ''),
            'why': verdict(row, key, config),
            # What choosing it means, said before it is chosen (the disk Linux runs from).
            'caution': SYSTEM_DISK if row['system'] and key != 'usb' and not verdict(row, key, config) else '',
        }
        for row in table
    ]


def none_usable(table, key, config):
    """Why no drive here can have this role, drive by drive, and what to do."""
    lines = [f"  - {brief(row)}: {verdict(row, key, config)}" for row in table]
    found = (
        '\n'.join(lines) if lines else '  No drive was found: check that it is connected and shows up in the Disks app.'
    )
    return (
        f'No drive here can be {role(key)} ({holds(config, key)}). NekoServer never formats a drive by itself, '
        f'and never takes a drive that is in use elsewhere.\n{found}\n'
        'Then run  sudo bash install.sh  again. Nothing was changed.'
    )


# ----- the question


def numbered(table, answer):
    """The row with the number typed, or None."""
    return table[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(table) else None


STORAGE_PAGE = 'storage'  # the page every drive question of the drives step is on (modules/storage.py)


def size_bytes(row):
    from modules import storage

    return storage.size_bytes(row.get('size')) or 0


def has_role_already(table, config):
    """The drive each role has in the settings, where it is in the list: a role that has
    its drive keeps it."""
    saved = config.get('storage', {})
    uuids = {
        key: saved[key].get('uuid') if isinstance(saved.get(key), dict) else None for key in ('data_ssd', 'media_hdd')
    }
    uuids['usb'] = config.get('photos', {}).get('external_backup_uuid')
    return {key: next((row for row in table if uuid and row['uuid'] == uuid), None) for key, uuid in uuids.items()}


def free_for_a_role(row, chosen):
    """Not chosen for another role, and on none of their disks."""
    taken = [other for other in chosen.values() if other is not None]
    return row not in taken and not any(set(row['disks']) & set(other['disks']) for other in taken)


def drives_of_their_own(table, key, config, chosen):
    """The drives besides the disk Linux runs from that may have the role and have none yet."""
    return [
        row for row in table if not row['system'] and free_for_a_role(row, chosen) and not verdict(row, key, config)
    ]


def best_for(table, key, config, chosen, order):
    """The first drive of its own in `order`; the disk Linux runs from only where there is none."""
    own = drives_of_their_own(table, key, config, chosen)
    if own:
        return sorted(own, key=order)[0]
    return next((row for row in table if row['system'] and not verdict(row, key, config)), None)


def in_this_pc_first(row):
    return bool((row['device'] or {}).get('network')), -size_bytes(row)


def ssd_first(row):
    return 'SSD' not in row['type'], -size_bytes(row)


def recommended(table, config):
    """The drive setup would choose for each role, so that a person who changes nothing
    gets a sensible layout: {'data_ssd': row or None, 'media_hdd': ..., 'usb': ...}.

    The media and backups get the biggest drive of their own that can hold them (one in
    this PC before one on a storage box); the apps get an SSD among the others, the
    biggest, else any other drive; the offline backup gets a USB drive that is neither.
    With three drives that is the layout every server before 0.50.0 has. The disk Linux
    runs from is recommended only where no drive of its own can have the role. A role
    that has its drive already keeps it, and that drive is offered for no other."""
    chosen = has_role_already(table, config)
    if chosen['media_hdd'] is None:
        chosen['media_hdd'] = best_for(table, 'media_hdd', config, chosen, in_this_pc_first)
    if chosen['data_ssd'] is None:
        chosen['data_ssd'] = best_for(table, 'data_ssd', config, chosen, ssd_first)
    if chosen['usb'] is None:
        usb = [row for row in drives_of_their_own(table, 'usb', config, chosen) if row['type'] == 'USB drive']
        chosen['usb'] = usb[0] if usb else None
    return chosen


def field(key, input_fn, prompt):
    """One answer of a page: what the setup window sent for it with an earlier question
    of the page, else the question itself, asked in the terminal and in the window.
    Ctrl+D, and the window's Skip, are an empty answer."""
    sent = setup_progress.page_answer(key)
    if sent is not None:
        return '' if sent[0] == 'skip' else sent[1].strip()
    setup_progress.page_asks(key)
    answer = typed(input_fn, prompt)
    setup_progress.page_answer(key)  # the window sends every field: this one's copy is not asked for twice
    return answer


def own_page(table, key, config, skippable):
    """The page of a role asked alone; nothing more on the storage page, which shows
    every role at once. Returns (the page, the name of this role's field on it)."""
    shown = setup_progress.page_shown()
    if shown and shown.get('form') == STORAGE_PAGE:
        return contextlib.nullcontext(), key
    page = setup_progress.page(
        'drives',
        {'choice': ''},
        what=f'Choose {asked_for(config, key)}.',
        needs=needs(key),
        rows=for_window(table, key, config),
        skippable=skippable,
        names=NAMES_CHANGE,
    )
    return page, 'choice'


def say_taken(row, key, config, out_fn):
    """Say the choice back in words, with what the disk Linux runs from means."""
    chosen = said({'said': brief(row)}, first=True)
    out_fn(f'{chosen} is {role(key)}. Nothing on it is changed by choosing it.')
    if row['system']:
        out_fn(SYSTEM_DISK)
        if key == 'media_hdd' and 'data_ssd' in system_roles(config):
            out_fn(SAME_DISK_BACKUPS)
    return row


def answered(table, key, config, answer, out_fn):
    """The row an answer names when it may have the role; None, with the reason said,
    for a number that is not in the list or a drive that cannot have it."""
    row = numbered(table, answer)
    if row is None:
        out_fn(f'Type a number from 1 to {len(table)}, as the list shows them.')
        return None
    why = verdict(row, key, config)
    if why:
        refused = said({'said': brief(row)}, first=True)
        out_fn(f'{refused} cannot be {role(key)}: {why}. Choose another one from the list.')
        return None
    return say_taken(row, key, config, out_fn)


def ask(table, key, config, input_fn=input, out_fn=print, attempts=3, skippable=False):
    """The drive for the role `key`, chosen by its number in the list. Nothing is typed
    back: choosing a drive formats nothing and changes nothing on it, and the choice is
    said in words before anything is mounted. A drive that cannot have the role is
    refused at once with the reason, and another asked for. Enter alone (or Ctrl+D)
    stops: None when the choice may be left for later (`skippable`), otherwise an error
    that says to run setup again.

    On the storage page (modules/storage.py) the window shows every role at once and
    sends them together; asked alone (the weekly backup step), the role is a page of
    its own."""
    what = asked_for(config, key)
    best = recommended(table, config).get(key)
    mark = f"  {best['number']}) " if best is not None else None
    for line in listed(table, key, config):
        out_fn(line + ('  (recommended)' if mark and line.startswith(mark) else ''))
    leave = 'Enter to skip for now' if skippable else 'Enter alone stops setup here'
    if skippable:
        best = None  # Enter leaves it for later
    if best is not None:
        # Enter takes the drive setup would choose; nothing is changed by choosing it.
        leave = f"Enter for {best['number']}, the recommended one"
    stopped = (
        f'No drive was chosen for {role(key)}. Run  sudo bash install.sh  again to choose it; nothing was changed.'
    )
    page, name = own_page(table, key, config, skippable)
    with page:
        for _ in range(attempts):
            answer = field(name, input_fn, f'Number of {what} ({leave}): ')
            if not answer and best is not None:
                answer = str(best['number'])
            if not answer and skippable:
                return None
            if not answer:
                raise RuntimeError(stopped)
            row = answered(table, key, config, answer, out_fn)
            if row is not None:
                return row
    raise RuntimeError(stopped)


def last_time(row, key):
    """What a rerun says about a drive chosen before."""
    chosen = brief(row)
    return f"Last time you chose {chosen} as {role(key)} (UUID {row['uuid']}). It is used again."


def saved_row(table, uuid):
    """The row of the drive saved under this UUID, or None when it is not connected."""
    return next((row for row in table if row['uuid'] == uuid), None)
