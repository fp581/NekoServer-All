"""What can be known before the long install starts (0.50.0, the review page of setup).

    check(config, look=None)   what stops the install, what is worth a warning, each
                               drive's numbers, and roughly how long setup will take

It reads the chosen apps and the placement table (placement.py) and looks at this PC:
the drives connected, their free space, the memory, and whether the places setup downloads
from answer. Looking only: nothing is installed, mounted, written or changed.

Each finding says what is wrong in plain words and what to do about it:

    {'about': 'drive' | 'space' | 'downloads' | 'memory', 'problem': ..., 'do': ...}

The space a drive needs is the drive space step's own numbers (modules/capacity.py),
summed over everything the table places on that drive, plus the AI assistant's download
when it is chosen (modules/assistant.py), so this check and that step never disagree.
"""

import json
import shutil
import socket
import subprocess

import capabilities
import placement
from modules import assistant, capacity
from neko_common import ASSISTANT, media_group

GIB = capacity.GIB
# The kinds of formatting NekoServer keeps its files on (modules/storage.py).
USABLE = ('ext4', 'xfs', 'btrfs')
# What each place in the table needs, as the drive space step counts it: the apps and the
# files kept with them by default (photos, music) need what it asks of the app SSD;
# films and shows, Nextcloud files and the backups need what it asks of the media drive.
APP_SIDE, MEDIA_SIDE = 'apps', 'media'
SIDES = {
    APP_SIDE: (capacity.SSD, 'the apps, their settings, photos and music'),
    MEDIA_SIDE: (capacity.HDD, 'films, shows, Nextcloud files and the backups'),
}
MEDIA_DATA = ('movies', placement.CLOUD, placement.BACKUP)
# Where setup downloads from, and what comes from there.
SOURCES = (
    ('download.docker.com', 'Docker, the program that runs the apps'),
    ('registry-1.docker.io', 'most of the apps'),
    ('ghcr.io', 'the rest of the apps'),
)
MODEL_SOURCE = ('registry.ollama.ai', 'the AI assistant and its model')
# Rough minutes of setup on a usual home connection, by what is chosen. Estimates, not
# measurements: the gate's timed installs should replace them.
MINUTES = {'base': 15, 'photos': 10, 'movies': 10, 'music': 3, placement.CLOUD: 8, ASSISTANT: 10}


class Look:
    """What the check looks at on this PC, each a read that changes nothing. A test
    stands in for it with the same three functions."""

    def drives(self):
        """{UUID: {'fstype', 'size' (bytes), 'free' (bytes, None when not connected to a
        folder), 'mount'}} for every formatted drive and partition Linux sees."""
        try:
            listed = subprocess.run(
                ['lsblk', '-J', '-b', '-o', 'UUID,FSTYPE,SIZE,FSAVAIL,MOUNTPOINT'],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            rows = json.loads(listed.stdout)['blockdevices'] if listed.returncode == 0 else []
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return {}
        found = {}
        while rows:
            row = rows.pop()
            rows += row.get('children') or []
            if row.get('uuid'):
                free = row.get('fsavail')
                found[row['uuid']] = {
                    'fstype': row.get('fstype') or '',
                    'size': int(row.get('size') or 0),
                    'free': int(free) if free is not None else None,
                    'mount': row.get('mountpoint'),
                }
        return found

    def folder_free(self, folder):
        """Free bytes where a folder is (a shared folder of a NAS), or None."""
        try:
            return shutil.disk_usage(folder).free
        except OSError:
            return None

    def reaches(self, host):
        """Whether a download place answers (a connection to its HTTPS port, then closed)."""
        try:
            socket.create_connection((host, 443), timeout=8).close()
            return True
        except OSError:
            return False

    def memory(self):
        """This PC's memory in GiB (0 when it can't be read)."""
        return capabilities.memory_gib()


def finding(about, problem, do):
    return {'about': about, 'problem': problem, 'do': do}


def model_gb(config):
    """(GB the AI assistant downloads at the least, at the most, what is said about it);
    (0, 0, None) when it is not chosen. A model picked later ("auto") may be any of the
    ones offered: the smallest counts for the minimum, the biggest for the recommendation."""
    options = assistant.options(config)
    if options.get('wanted') is not True:
        return 0, 0, None
    named = options.get('model', 'auto')
    sizes = {name: size for name, size, _memory, _words in assistant.MODELS}
    if named == 'auto':
        low, high = min(sizes.values()), max(sizes.values())
    else:
        low = high = sizes.get(named, assistant.UNKNOWN_MODEL_GB)
    return low + assistant.IMAGES_GB, high + assistant.IMAGES_GB, 'the AI assistant and its model'


def side_of(row, choice):
    """Which of the drive space step's two sets of numbers a place in the table counts under."""
    return MEDIA_SIDE if choice == placement.DATA and row in MEDIA_DATA else APP_SIDE


def needed(config):
    """{drive: {'least', 'recommended' (GB), 'holds' [what, in words]}} for every drive the
    table uses but the removable one: the drive space step's numbers for each side placed
    on it, added up, with the AI assistant's download on the drive NekoServer itself is on."""
    wanted = {}
    for drive, places in placement.used(config).items():
        if drive == placement.drive_of(config, placement.OFFLINE, placement.DATA):
            continue
        sides = sorted({side_of(row, choice) for row, choice in places})
        entry = {
            'least': sum(SIDES[side][0][0] for side in sides),
            'recommended': sum(SIDES[side][0][1] for side in sides),
            'holds': [SIDES[side][1] for side in sides],
        }
        if (placement.CORE, placement.INSTALL) in places:
            low, high, what = model_gb(config)
            if what:
                entry['least'] += low
                entry['recommended'] += high
                entry['holds'].append(what)
        wanted[drive] = entry
    return wanted


def gb(size):
    return capacity.gib(size)


def drive_facts(config, name, record, seen, look):
    """One drive of the table as this PC has it now: {'name', 'present', 'fstype', 'free',
    'size', 'shared'} (free None when it is not connected to a folder yet)."""
    if record.get('network') and str(record.get('fstype', '')).startswith('nfs'):
        mounts = {placement.SSD: 'data_mount', placement.MEDIA: 'media_mount'}
        folder = config.get(mounts.get(name, ''))
        free = look.folder_free(folder) if folder else None
        return {'name': name, 'present': True, 'fstype': record['fstype'], 'free': free, 'size': None, 'shared': True}
    found = seen.get(record.get('uuid'))
    if found is None:
        return {'name': name, 'present': False, 'fstype': '', 'free': None, 'size': None, 'shared': False}
    return {'name': name, 'present': True, 'shared': False, **{key: found[key] for key in ('fstype', 'free', 'size')}}


def drive_findings(name, facts, record, optional):
    """Whether a chosen drive is there and of a kind NekoServer can use: (blocking, warnings)."""
    if not facts['present']:
        problem = f'The drive chosen as "{name}" is not connected to this PC (or is switched off).'
        if optional:
            return [], [
                finding(
                    'drive',
                    problem + ' It is the removable backup drive, so setup goes on without it, and the copy to '
                    'it waits.',
                    'Plug it in before the first weekly copy. Until then your photos and files have no copy '
                    'outside this PC.',
                )
            ]
        return [finding('drive', problem, 'Connect it and check its cable and power, then look again.')], []
    if facts['shared']:
        return [], []
    kind = facts['fstype']
    if kind not in USABLE:
        return [
            finding(
                'drive',
                f'The drive chosen as "{name}" is formatted as {kind or "nothing NekoServer knows"}. NekoServer '
                f'keeps its files only on drives formatted as {", ".join(USABLE[:-1])} or {USABLE[-1]}.',
                'Choose another drive, or format this one yourself first (formatting erases it; setup never '
                'formats a drive).',
            )
        ], []
    if record.get('fstype') and record['fstype'] != kind:
        return [
            finding(
                'drive',
                f'The drive chosen as "{name}" is formatted as {kind} now, but it was {record["fstype"]} when it '
                'was chosen. It may have been formatted again, or be another drive.',
                'Choose the drives again, so setup sees what is on it now.',
            )
        ], []
    return [], []


def space_findings(name, facts, need):
    """A drive's free space against what is placed on it: (blocking, warnings), with the
    exact numbers. A drive not connected to a folder yet has no free space to read: its
    size then says whether it can ever be enough."""
    least, recommended = need['least'], need['recommended']
    holds = ', '.join(need['holds'])
    free = facts['free']
    if free is None:
        size = facts['size']
        if size is not None and size < least * GIB:
            return [
                finding(
                    'space',
                    f'The drive "{name}" is {gb(size)} in all. What you placed on it ({holds}) needs at least '
                    f'{least} GB free.',
                    'Choose a bigger drive for it, or place some of it on another drive.',
                )
            ], []
        return [], [
            finding(
                'space',
                f'The free space on the drive "{name}" cannot be read before setup connects it. What you '
                f'placed on it ({holds}) needs at least {least} GB free.',
                'Nothing to do now: setup measures it right after connecting the drive, before it installs '
                'the apps, and stops there if it is too little.',
            )
        ]
    if free < least * GIB:
        return [
            finding(
                'space',
                f'The drive "{name}" has {gb(free)} free. What you placed on it ({holds}) needs at least {least} GB.',
                'Free up space on it, choose a bigger drive, or place some of it on another drive.',
            )
        ], []
    # A recommendation is a drive size as sold: an empty drive of that size passes (capacity.py).
    if free < recommended * capacity.SOLD_AS_FREE:
        return [], [
            finding(
                'space',
                f'The drive "{name}" has {gb(free)} free. What you placed on it ({holds}) needs at least '
                f'{least} GB, and {recommended} GB or more is recommended.',
                'You can go on: it works, and it will fill up sooner. A bigger drive can take its place later.',
            )
        ]
    return [], []


def download_findings(config, look):
    """The places setup downloads from that do not answer now: blocking, as nothing can be installed."""
    wanted = SOURCES + ((MODEL_SOURCE,) if model_gb(config)[2] else ())
    silent = [(host, what) for host, what in wanted if not look.reaches(host)]
    if not silent:
        return []
    if len(silent) == len(wanted):
        return [
            finding(
                'downloads',
                'This PC cannot reach the internet places setup downloads from (none of them answered).',
                'Check the network cable or Wi-Fi and that other websites open on this PC, then look again.',
            )
        ]
    return [
        finding(
            'downloads',
            f'{host} does not answer. Setup downloads {what} from there.',
            'It may be down for a while: look again in a few minutes. If it stays so, check whether your '
            'network or provider blocks it.',
        )
        for host, what in silent
    ]


def memory_findings(config, look):
    """Too little memory for the chosen apps: warnings (they run, and can be stopped by
    Linux under load); the AI assistant alone cannot be set up below what its smallest model needs."""
    total = look.memory()
    if not total:
        return []
    found = []
    if total < capabilities.ENOUGH_MEMORY_GIB:
        found.append(
            finding(
                'memory',
                f'This PC has {total:.1f} GB of memory. The apps use about 2 GB when idle and more while Immich '
                'looks at new photos or Jellyfin converts a video; with less than 8 GB those can be stopped '
                'for lack of memory.',
                'You can go on. Add memory if you can, or choose fewer apps.',
            )
        )
    if model_gb(config)[2]:
        smallest = min(memory for _name, _size, memory, _words in assistant.MODELS)
        room = total - assistant.reserve_gib(total)
        if room < smallest:
            found.append(
                finding(
                    'memory',
                    f'The AI assistant needs {smallest} GB of memory for its smallest model, beside what the '
                    f'other apps keep. This PC has {total:.1f} GB, which leaves {max(room, 0):.1f} GB for it.',
                    'Setup goes on without the assistant; everything else is installed. Switch it off, or add '
                    'memory and run setup again.',
                )
            )
    return found


def time_needed(config):
    """Roughly how long setup takes with what is chosen: {'low', 'high' (minutes), 'words'}."""
    chosen = [group for group in ('photos', 'movies', 'music') if media_group(config, group)]
    chosen += [placement.CLOUD] * (placement.CLOUD in placement.rows(config))
    chosen += [ASSISTANT] * bool(model_gb(config)[2])
    low = MINUTES['base'] + sum(MINUTES[part] for part in chosen)
    high = 2 * low
    return {
        'low': low,
        'high': high,
        'words': f'about {low} to {high} minutes on a usual home connection, longer on a slow one. Most of it is '
        'downloading; the PC can be left alone meanwhile, but must stay on.',
    }


def check(config, look=None):
    """Everything known before the install starts:

    {'blocking': [finding], 'warnings': [finding], 'drives': [each used drive's facts and
    numbers, for the review page], 'time': time_needed(config)}

    The install should not be started while 'blocking' is not empty. Nothing here
    changes anything; calling it again looks again."""
    look = look or Look()
    blocking, warnings, shown = [], [], []
    records = placement.drives(config)
    needs = needed(config)
    offline = placement.drive_of(config, placement.OFFLINE, placement.DATA)
    seen = look.drives()
    for name in placement.used(config):
        record = records.get(name)
        if record is None:
            blocking.append(
                finding(
                    'drive',
                    f'"{name}" is chosen in the table of drives, but no such drive is in the settings.',
                    'Choose the drives again.',
                )
            )
            continue
        facts = drive_facts(config, name, record, seen, look)
        stop, warn = drive_findings(name, facts, record, optional=name == offline)
        need = needs.get(name)
        if not stop and facts['present'] and need:
            more_stop, more_warn = space_findings(name, facts, need)
            stop, warn = stop + more_stop, warn + more_warn
        blocking += stop
        warnings += warn
        shown.append({**facts, 'least_gb': need and need['least'], 'recommended_gb': need and need['recommended']})
        shown[-1]['holds'] = need['holds'] if need else ['a copy of your photos, files and password lists']
    blocking += download_findings(config, look)
    warnings += memory_findings(config, look)
    return {'blocking': blocking, 'warnings': warnings, 'drives': shown, 'time': time_needed(config)}
