"""What lives on which drive: the one table every part of NekoServer reads (0.50.0).

The person chooses, row by row, the drive something is installed on and the drive its
data is on (the design is in the plans folder, 0.50.0-placement-design.md):

    NekoServer itself and everything else   installed on
    Photos (Immich)                         installed on, its photos on
    Movies and shows (Jellyfin ...)         installed on, its films and shows on
    Music (Navidrome)                       installed on, its music on
    Nextcloud                               installed on, its files on
    Backup                                  the copies go to
    Offline backup (optional)               a removable drive, or none

The table is "placement" in the settings, naming drives listed under "drives". A server
set up before 0.50.0 has neither: its table is worked out from what it has ("storage",
"locations", the USB drive), with no question and no change on disk (`derived`).

The folders themselves do not move: /srv/nekoserver/appdata/jellyfin is Jellyfin's
settings on whatever drive the table names. This module says which drive that is, which
folders belong to a row, and which rows share a drive; it changes nothing by itself.
"""

from neko_common import (
    APP_GROUPS,
    DOWNLOADS,
    INSTALL_SECTION,
    LOCATIONS,
    MEDIA_GROUPS,
    extra_drives,
    media_group,
)

# The two drives every server before 0.50.0 has, by the names the table uses for them,
# and where each is in the settings ("storage") and mounted.
SSD, MEDIA = 'ssd', 'media'
OLD_DRIVES = {SSD: ('data_ssd', 'data_mount'), MEDIA: ('media_hdd', 'media_mount')}
# The removable drive of the weekly offline copy.
OFFLINE_DRIVE = 'usb'

CORE, CLOUD, BACKUP, OFFLINE = 'core', 'cloud', 'backup', 'offline'
# Every row, in the order the page shows them: (its name in the table, what it is called,
# whether it has an installation drive, what its data is called or None).
ROWS = (
    (CORE, 'NekoServer itself and everything else', True, None),
    ('photos', 'Photos (Immich)', True, 'its photos'),
    ('movies', 'Movies and shows (Jellyfin, Seerr and the download apps)', True, 'its films and shows'),
    ('music', 'Music (Navidrome)', True, 'its music'),
    (CLOUD, 'Nextcloud', True, 'its files'),
    (BACKUP, 'Backup', False, 'the backup copies'),
    (OFFLINE, 'Offline backup (optional)', False, 'a copy on a removable drive'),
)
INSTALL, DATA = 'install', 'data'

# What a row's installation drive holds, under the app folder (data_mount): for the
# groups, the settings and database of each of its apps (their programs are Docker
# images, kept with NekoServer itself).
CORE_FOLDERS = ('docker-engine', 'installer', 'reports', 'secrets', 'appdata/assistant', 'compose/assistant')
# What a row's data drive holds, as folders of the drive it is on by default.
DATA_FOLDERS = {
    'photos': LOCATIONS['photos'][1],
    'movies': LOCATIONS['movies'][1],
    'music': LOCATIONS['music'][1],
    CLOUD: LOCATIONS['cloud'][1],
    BACKUP: ('backups', 'photos/archive'),
}
# Where a row's data is when nothing says otherwise: as every server before 0.50.0 has it.
DEFAULT_DATA = {
    'photos': LOCATIONS['photos'][0],
    'movies': LOCATIONS['movies'][0],
    'music': LOCATIONS['music'][0],
    CLOUD: LOCATIONS['cloud'][0],
    BACKUP: MEDIA,
}
# The rows whose data place a server before 0.50.0 chose itself ("locations").
OLD_LOCATIONS = {'photos': 'photos', 'movies': 'movies', 'music': 'music', CLOUD: 'cloud'}


def apps_of(row):
    """The apps whose settings a row's installation drive holds. Prowlarr and
    qBittorrent, which both groups' finding uses, are installed with Movies and shows."""
    if row == CLOUD:
        return ['nextcloud']
    if row not in MEDIA_GROUPS:
        return []
    home = {app: 'movies' if groups == (DOWNLOADS,) else groups[0] for app, groups in APP_GROUPS.items() if groups}
    return [app for app, group in home.items() if group == row]


def install_of(config, row):
    """The drive a row's apps keep their settings and databases on: the apps' drive, or
    the drive "install" in the settings names for the row. Only a drive listed under
    "storage" is taken, and never for NekoServer itself, which stays on the apps' drive."""
    saved = config.get(INSTALL_SECTION)
    drive = saved.get(row) if isinstance(saved, dict) else None
    if drive is None or drive == SSD:
        return SSD
    placeable = {name for name, _words, installs, _data in ROWS if installs} - {CORE}
    if row not in placeable or not isinstance(drive, str) or drive not in extra_drives(config):
        raise RuntimeError(
            f'"{row}" under "{INSTALL_SECTION}" in my-config.json names {drive!r}: it must be "ssd" or a drive listed '
            'under "storage", for Photos, Movies and shows, Music or Nextcloud. Take the line out and run setup again.'
        )
    return drive


def apps_elsewhere(config):
    """{app: drive} for the apps whose settings and database are on another drive than
    the apps' own: the apps of every row this server has that "install" places there."""
    found = {}
    saved = config.get(INSTALL_SECTION)
    for row in saved if isinstance(saved, dict) else ():
        install_of(config, row)  # every line of the table is checked, also of a group that is off
    for row in rows(config):
        if row == CORE or not has(row, INSTALL):
            continue
        drive = install_of(config, row)
        if drive != SSD:
            found.update({app: drive for app in apps_of(row)})
    return found


def install_folders(row):
    """What a row's installation drive holds, as folders of the app folder."""
    if row == CORE:
        return list(CORE_FOLDERS)
    return [f'{kind}/{app}' for app in apps_of(row) for kind in ('compose', 'appdata')]


def applies(config, row):
    """The row is part of this server: its group is on (Nextcloud: switched on)."""
    if row == 'movies':
        # The download apps live in this row too (music found by Lidarr, with Movies and shows off).
        return media_group(config, row) or media_group(config, DOWNLOADS)
    if row in MEDIA_GROUPS:
        return media_group(config, row)
    if row == CLOUD:
        return config.get('nextcloud', {}).get('enabled', True) is True
    return True


def rows(config):
    """The rows this server has, in the page's order."""
    return [row for row, *_ in ROWS if applies(config, row)]


def name_of(row):
    return next(name for key, name, *_ in ROWS if key == row)


def has(row, choice):
    """Whether a row has that choice at all (Backup has no installation drive)."""
    _, _, installs, data = next(entry for entry in ROWS if entry[0] == row)
    return installs if choice == INSTALL else data is not None


def derived_drives(config):
    """The drives of a server set up before 0.50.0, as the table names them."""
    storage = config.get('storage', {})
    drives = {}
    for name, (key, _mount) in OLD_DRIVES.items():
        record = storage.get(key)
        if isinstance(record, dict) and record.get('uuid'):
            drives[name] = {k: record[k] for k in ('uuid', 'fstype', 'network', 'subvolume', 'folder') if record.get(k)}
    # Every other drive listed under "storage" (0.50.0), by the name it has there.
    for name, record in extra_drives(config).items():
        drives[name] = {k: record[k] for k in ('uuid', 'fstype', 'network', 'subvolume', 'folder') if record.get(k)}
    photos = config.get('photos', {})
    if photos.get('external_backup_uuid'):
        drives[OFFLINE_DRIVE] = {
            'uuid': photos['external_backup_uuid'],
            **{
                key: photos[source]
                for key, source in (('fstype', 'external_backup_fstype'), ('network', 'external_backup_network'))
                if photos.get(source)
            },
        }
    return drives


def derived(config):
    """The table of a server that has none: everything installed on the app SSD, each
    row's data where "locations" (or the fixed layout) has it, the backups on the media
    drive, the offline copy on the USB drive when one is chosen. Nothing is asked and
    nothing on disk changes: it is the layout the server already has, written down."""
    chosen = config.get('locations') or {}
    table = {}
    for row, _name, installs, data in ROWS:
        entry = {}
        if installs:
            entry[INSTALL] = install_of(config, row)
        if row == OFFLINE:
            entry[DATA] = OFFLINE_DRIVE if config.get('photos', {}).get('external_backup_uuid') else None
        elif data is not None:
            entry[DATA] = chosen.get(OLD_LOCATIONS.get(row), DEFAULT_DATA[row])
        table[row] = entry
    return table


def table(config):
    """This server's table: the one in its settings, checked; else the one worked out."""
    saved = config.get('placement')
    if saved is None:
        return derived(config)
    return checked(saved, drives(config))


def drives(config):
    """The drives the table may name: the ones in the settings ("drives"), else those of
    a server set up before 0.50.0."""
    saved = config.get('drives')
    if saved is None:
        return derived_drives(config)
    if not isinstance(saved, dict) or not all(isinstance(value, dict) for value in saved.values()):
        raise RuntimeError('"drives" in my-config.json must be an object of drives, each an object')
    return saved


def checked(saved, known):
    """A table from the settings file, as far as it can be trusted: every row an object,
    every choice a drive that is listed (or none, for the offline backup alone)."""
    if not isinstance(saved, dict):
        raise RuntimeError('"placement" in my-config.json must be an object')
    for row, _name, installs, data in ROWS:
        entry = saved.get(row)
        if not isinstance(entry, dict):
            raise RuntimeError(f'"placement" in my-config.json has no row "{row}" (or it is not an object)')
        for choice, wanted in ((INSTALL, installs), (DATA, data is not None)):
            if not wanted:
                continue
            drive = entry.get(choice)
            if drive is None and row == OFFLINE:
                continue
            if drive not in known:
                raise RuntimeError(
                    f'"{choice}" of "{row}" under "placement" names the drive {drive!r}, which is not under "drives" '
                    'in my-config.json'
                )
    return saved


def drive_of(config, row, choice):
    """The drive (its name in the table) a row is installed on or keeps its data on;
    None for the offline backup when there is none."""
    if not has(row, choice):
        raise KeyError(f'{row} has no {choice} drive')
    return table(config)[row].get(choice)


def used(config):
    """{drive: [(row, choice)]} for the rows this server has: what is on each drive."""
    found = {}
    current = table(config)
    for row in rows(config):
        for choice in (INSTALL, DATA):
            if has(row, choice) and current[row].get(choice):
                found.setdefault(current[row][choice], []).append((row, choice))
    return found


def data_of(config, row):
    """A row's data as (drive, its folders on that drive)."""
    return drive_of(config, row, DATA), list(DATA_FOLDERS.get(row, ()))


def is_default(config):
    """The table is the layout every server before 0.50.0 has (the app SSD and the media
    drive, each a drive of its own): nothing is mounted over a folder, and every unit and
    settings file is what it always was."""
    current = table(config)
    for row, _name, installs, data in ROWS:
        if installs and current[row].get(INSTALL) != SSD:
            return False
        if data is not None and row != OFFLINE and current[row].get(DATA) not in (SSD, MEDIA):
            return False
    return {SSD, MEDIA} <= set(drives(config))


def said(config):
    """The table in words, one line a row, for the log and for a rerun's "Last time ...":
    the rows this server has, each with its drives by their names in the table."""
    current = table(config)
    lines = []
    for row in rows(config):
        _, name, installs, data = next(entry for entry in ROWS if entry[0] == row)
        parts = []
        if installs:
            parts.append(f'installed on {current[row][INSTALL]}')
        if data is not None:
            parts.append(f"{data} on {current[row].get(DATA) or 'none'}")
        lines.append(f'{name}: ' + ', '.join(parts))
    return lines
