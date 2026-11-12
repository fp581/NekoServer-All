"""What is backed up, where to, when and how much of it is kept: the facts behind the
"Protect your data" page of setup (0.50.0), as plain functions that ask nothing and change
nothing on a drive.

    table(config)                    the rows the page shows
    count_field(config, group)       the "keep the latest [3]" field of a backup
    review(config, group, new, held) what changing a count would do, in words
    set_count(config, group, count)  the person's approved count, written to the settings
    note_earlier(config)             counts from before 0.50.0 marked as such, never changed

Two backups are counted by the 1-to-7 rule: the nightly app backups ("app_backup") and the
nightly settings backups ("host_backup"). Each has its own count; a count is a number of
backups of that one kind that finished, not of days. The weekly Immich database copies
and the monthly snapshots have counts of their own (4 and 3, unchanged). The photo
archive, the films, shows and music, and the offline copy are never counted: nothing is
removed from them because "only N are kept".

A count is applied by the scheduled job itself, only after a new backup of that kind was
finished and published, and never to the newest one (runtime, common::prune_snapshots).
Nothing here, and nothing in setup, removes a backup.
"""

import re
from pathlib import Path

import placement
from modules import app_backup, host_backup, photo_backup
from neko_common import job_times, media_group, media_services, music_backed_up

DEFAULT, LEAST, MOST = app_backup.KEEP_DEFAULT, app_backup.KEEP_LEAST, app_backup.KEEP_MOST
# The backups the 1-to-7 count applies to: (what is counted, their folder on the backup drive).
COUNTED = {
    'app_backup': (app_backup.APP_BACKUPS, 'backups/media-applications'),
    'host_backup': (host_backup.SETTINGS_BACKUPS, 'backups/host-user'),
}
# Set in the settings once the counts of a server from before 0.50.0 were looked at with
# the person (or there was nothing to look at): from then on a count outside 1 to 7 that
# is not marked as the earlier one is a mistake, and refused.
REVIEWED = 'backup_counts_reviewed'
SNAPSHOT = re.compile(r'^snapshot-\d+-\d+$')
DAYS = {'Mon': 'Monday', 'Tue': 'Tuesday', 'Wed': 'Wednesday', 'Thu': 'Thursday', 'Fri': 'Friday'}
DAYS.update(Sat='Saturday', Sun='Sunday')
NEVER_REMOVED = 'everything, in every version; nothing is ever removed to keep to a number'


def count(config, group):
    """(the count of a counted backup, whether it is an earlier setting outside 1 to 7)."""
    return app_backup.kept_count(config, group, COUNTED[group][0])


def note_earlier(config):
    """On a server set up before 0.50.0, not yet reviewed: a count outside 1 to 7 is marked
    as the earlier setting, so it goes on working as it did. The count itself is never
    changed here. True when a mark was added."""
    if config.get(REVIEWED) is True:
        return False
    added = False
    for group in COUNTED:
        options = config.get(group)
        if not isinstance(options, dict) or options.get(app_backup.KEEP_EARLIER) is True:
            continue
        keep = options.get(app_backup.KEEP, DEFAULT)
        outside = type(keep) is int and not LEAST <= keep <= MOST and 0 <= keep <= app_backup.EARLIER_MOST
        if outside:
            options[app_backup.KEEP_EARLIER] = True
            added = True
    return added


def earlier_groups(config):
    """The counted backups whose count is still the earlier one, outside 1 to 7."""
    return [group for group in COUNTED if count(config, group)[1]]


def set_count(config, group, new):
    """The count the person approved (a whole number from 1 to 7), written to the settings;
    the mark of an earlier setting goes. The scheduled job reads it at its next run and
    removes older backups only after that run's own backup succeeded."""
    if group not in COUNTED:
        raise RuntimeError(f'{group} has no count of backups to keep')
    if type(new) is not int or not LEAST <= new <= MOST:
        raise RuntimeError(
            f'Keep from {LEAST} to {MOST} {COUNTED[group][0]}: choose a whole number from {LEAST} to {MOST}, not {new!r}'
        )
    options = config.setdefault(group, {})
    options[app_backup.KEEP] = new
    options.pop(app_backup.KEEP_EARLIER, None)


def backup_drive(config):
    """(the drive the backups go to, by its name in the placement table; its folder when
    it is one of the two drives every server has, else None)."""
    drive = placement.drive_of(config, placement.BACKUP, placement.DATA)
    mounts = {placement.SSD: 'data_mount', placement.MEDIA: 'media_mount'}
    return drive, Path(config[mounts[drive]]) if drive in mounts and config.get(mounts[drive]) else None


def held(config, group):
    """How many finished backups of that kind are on the backup drive now; None when that
    can't be told (the drive is not connected, or it is a drive this can't look at)."""
    folder = backup_drive(config)[1]
    if folder is None:
        return None
    try:
        found = [path for path in (folder / COUNTED[group][1]).iterdir() if SNAPSHOT.match(path.name)]
        return sum(1 for path in found if (path / 'manifest.json').is_file())
    except OSError:
        return None


def kept_words(keep, what):
    return f'every one of the {what} (none is ever removed)' if keep == 0 else f'the newest {keep} {what}'


def review(config, group, new=DEFAULT, held=None):
    """What changing a counted backup's count to `new` would do, for the person to read
    before they approve it; None when `new` is what the settings already keep. `held`:
    how many there are now (see `held`), when known. Nothing is changed by asking."""
    what = COUNTED[group][0]
    keep, earlier = count(config, group)
    if keep == new:
        return None
    said = f'This server keeps {kept_words(keep, what)}.'
    said += f' New servers keep {DEFAULT}, and the most that can be chosen from now on is {MOST}.' if earlier else ''
    if held is not None:
        said += f' There {"is" if held == 1 else "are"} {held} now.'
    if keep != 0 and new > keep:
        return said + f' With {new}, more are kept from now on. Nothing is removed.'
    if held is None:
        effect = f'the ones older than the newest {new} are removed'
    elif held > new:
        effect = f'the {held - new} oldest {"is" if held - new == 1 else "are"} removed'
    else:
        return said + f' With {new}, none of them is removed, as there are not more than {new}.'
    return (
        said + f' If you change it to {new}, {effect}, but only after the next backup that succeeds. Nothing is '
        'removed now, a backup that fails removes nothing, and the newest backup always stays. Your photos, '
        'videos, films and music are not touched by this number.'
    )


def count_field(config, group, held=None):
    """The "keep the latest [N]" field of a counted backup: its value, the limits, and,
    for an earlier setting outside 1 to 7, the review to show beside it."""
    keep, earlier = count(config, group)
    return {
        'group': group,
        'what': COUNTED[group][0],
        'value': keep,
        'least': LEAST,
        'most': MOST,
        'default': DEFAULT,
        'earlier': earlier,
        'review': review(config, group, DEFAULT, held) if earlier else None,
    }


def covers(keep, every_days, unit):
    """Roughly how far back `keep` backups made every `every_days` days reach, in words.
    A count is a number of backups, not of days: a night the PC is off, or a backup that
    fails, adds none, so the same count then reaches further back."""
    if keep == 0:
        return 'as far back as the first backup', None
    days = keep * every_days
    return f'about the last {keep} {unit}{"" if keep == 1 else "s"}, when every backup runs', days


def weekly(times, day='usb_day', time='usb'):
    return f'every week, {DAYS[times[day]]} at {times[time]}'


def same_drive(config, row, choice, drive):
    """Whether what a backup protects is on the very drive the backup goes to (by the
    drives' names in the placement table): such a backup is no help when that drive fails."""
    return row in placement.rows(config) and placement.drive_of(config, row, choice) == drive


def table(config, held=None):
    """The rows the page shows, in order: for each backup this server has, what it
    protects, where it goes, when it runs, how many are kept, roughly how far back that
    reaches (words, and days when it can be counted), and whether the 1-to-7 count applies
    ("group", else None). "same_drive": it goes to the drive that holds what it protects.
    `held`: {group: how many are there now}, for an earlier count's review."""
    times = job_times(config)
    drive, _folder = backup_drive(config)
    photos = config.get('photos', {})
    rows = []

    def row(key, what, where, when, kept, reach, group=None, same=False, note=None):
        words, days = reach
        rows.append(
            {
                'key': key,
                'what': what,
                'drive': where[0],
                'folder': where[1],
                'when': when,
                'kept': kept,
                'covers': words,
                'covers_days': days,
                'group': group,
                'field': count_field(config, group, (held or {}).get(group)) if group else None,
                'same_drive': same,
                'note': note,
            }
        )

    def counted(group, what, time):
        keep, _earlier = count(config, group)
        row(
            group,
            what,
            (drive, COUNTED[group][1]),
            f'every night at {times[time]}',
            kept_words(keep, COUNTED[group][0]),
            covers(keep, 1, 'night'),
            group,
            same_drive(config, placement.CORE, placement.INSTALL, drive),
        )

    if media_services(config):
        counted('app_backup', 'The settings and databases of the film, show and music apps', 'app')
    counted('host_backup', "This PC's own settings: shared folders (Samba), Steam saves, system settings", 'host')
    if media_group(config, 'photos'):
        often = (
            weekly(times)
            if photos.get('backup_schedule', 'weekly') == 'weekly'
            else f"every {photos.get('archive_interval_minutes', 360)} minutes"
        )
        row(
            'photo_archive',
            'Your photos and videos (Immich)',
            (drive, photos.get('archive_dir', 'photos/archive')),
            often,
            NEVER_REMOVED,
            ('as far back as the first backup', None),
            same=same_drive(config, 'photos', placement.DATA, drive),
        )
        keep = photos.get('database_keep', 4)
        row(
            'photo_database',
            "Immich's database: albums, people, faces and settings",
            (drive, 'backups/immich-database'),
            weekly(times, 'photo_database_weekly_day', 'photo_database_weekly'),
            kept_words(keep, 'weekly copies'),
            covers(keep, 7, 'week'),
            same=same_drive(config, 'photos', placement.INSTALL, drive),
            note='This count is its own (4 on a new server); the 1-to-7 number does not apply to it.',
        )
    keep = config.get('maintenance', {}).get(app_backup.KEEP, 3)
    row(
        'monthly',
        'The settings and databases of every app, taken before the monthly check and the updates',
        (drive, 'backups/monthly'),
        f"every month, on the 1st at {times['monthly']}",
        kept_words(keep, 'monthly snapshots'),
        covers(keep, 30, 'month'),
        same=same_drive(config, placement.CORE, placement.INSTALL, drive),
        note='This count is its own (3 on a new server); the 1-to-7 number does not apply to it.',
    )
    offline_row(config, times, row)
    if music_backed_up(config):
        music_row(config, times, drive, row)
    for key, what, why in not_copied(config):
        # The page then shows only "never" and why.
        row(key, what, (None, None), 'never', None, (None, None), note=why)
    return rows


def offline_row(config, times, row):
    """The offline copy's row: weekly only with a drive and something to take (photo_backup:
    Immich or Nextcloud); a drive chosen without them gets no timer, so no copy is promised
    (gate run C7)."""
    offline = placement.drive_of(config, placement.OFFLINE, placement.DATA)
    cloud = placement.CLOUD in placement.rows(config)
    copied = ['your photos and videos'] * media_group(config, 'photos') + ['your Nextcloud files'] * cloud
    what = 'A second copy on a drive you can unplug: ' + ', '.join(copied + ['the app password lists'])
    takes = any(photo_backup.copies(config))
    if offline and takes:
        row('offline', what, (offline, None), weekly(times), NEVER_REMOVED, ('as far back as the first copy', None))
        return
    when = 'never: ' + ('no such drive is set up' if takes else NOTHING_TO_COPY)
    note = None
    if takes and not offline:
        note = OFFLINE_MISSING + (CLOUD_UNPROTECTED if cloud else '')
    row('offline', what, (offline, None), when, 'nothing', ('nothing', None), note=note)


def music_row(config, times, drive, row):
    """The nightly music copy's row (G7: with the person's own folder on a share when asked)."""
    import existing_media

    own = ' and your own music folder on the network share' if existing_media.backed_up(config) else ''
    row(
        'music',
        'Your music (the library' + own + ')',
        (drive, 'backups/music'),
        f"every night at {times['music']}",
        NEVER_REMOVED,
        ('as far back as the first backup', None),
        same=same_drive(config, 'music', placement.DATA, drive),
    )


NOTHING_TO_COPY = 'there is nothing for it to copy (no Immich, no Nextcloud)'
DOWNLOADABLE = (
    'Not backed up: no backup job copies them (they are often larger than any backup drive). What Radarr '
    'and Sonarr found can be downloaded again; keep your own copy of anything else.'
)
MUSIC_NOT_COPIED = (
    'Not backed up: the nightly music copy is switched off on this server. What Lidarr found can be '
    'downloaded again; keep your own copy of anything else.'
)


def not_copied(config):
    """[(key, what, why)]: what this server keeps that no backup copies, said on the page
    beside the backups (gate run C7: the films, shows and music were in no job and in no
    list). The person's own folders are listed by existing_media.not_backed_up."""
    import existing_media

    found = []
    if media_group(config, 'movies'):
        found.append(('not_copied_movies', 'Your films and shows (the library)', DOWNLOADABLE))
    if media_group(config, 'music') and not music_backed_up(config):
        found.append(('not_copied_music', 'Your music (the library)', MUSIC_NOT_COPIED))
    for category, folder, why in existing_media.not_backed_up(config):
        found.append(
            (f'not_copied_own_{category}', f'Your own folder of {category}: {folder}', 'Not backed up: ' + why)
        )
    return found


OFFLINE_MISSING = (
    'Not set up. Every backup is then inside this PC (or on its NAS): a fire, a theft or a power surge '
    'can take the backups with it.'
)
CLOUD_UNPROTECTED = ' Nextcloud files are backed up only to this drive, so without it they have no backup at all.'
