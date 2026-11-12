"""Media a person already has, used where it is (0.50.0, page P03).

A folder of films, shows or music on a drive or a network share is checked, mounted into
the library at a fixed place, and added to Jellyfin or Navidrome as a library of its own.
Nothing in it is copied, moved, renamed, chowned or chmodded: the folder stays the
person's. Copying instead goes through source_import.py, which keeps the source.

How a folder reaches the apps without a hand edit:
  the person's folder --(bind mount, /etc/fstab, made from the settings)-->
  <the group's drive>/library/existing/<category>
  --(the Compose files' fixed line)--> /data/existing/<category> in every app.
The apps that write (Sonarr, Radarr, Lidarr, qBittorrent) mount library/ as /data, so they
see the same /data/existing/<category>: no path mappings anywhere. The bind mount is
read-only unless new files are to be saved there.

The page calls: check(), copy_preview(), with_choice() / without_choice(), then setup
calls use_in_place() and libraries(). available() and guard() say whether a folder is
there now (the share may be away).
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import account_store
import media_libraries
import nfs_exports
import placement
import source_import
from neko_common import (
    app_port,
    atomic_json,
    atomic_write,
    files_root,
    findmnt,
    local_opener,
    music_downloads,
    verify_storage,
)

# category -> (its row of the placement table, the library's name, Jellyfin's kind).
# "tv" is what the page calls shows (the name source_import and the library folder use).
CATEGORIES = {
    'movies': ('movies', 'Movies (your folder)', 'movies'),
    'tv': ('movies', 'TV Shows (your folder)', 'tvshows'),
    'music': ('music', 'Music (your folder)', None),
}
IN_PLACE = 'in_place'
# A folder of the person's that only takes the new files (the one used in place is read-only).
NEW = 'new_files'
USES = (IN_PLACE, NEW)
# Where a folder used in place is mounted: under the library of its group's drive ...
EXISTING = 'existing'
# ... which every app sees here.
CONTAINER = '/data/existing'
FSTAB = Path('/etc/fstab')
# The lines setup keeps in /etc/fstab for these folders, between two marks.
BEGIN = '# NekoServer: existing media used where it is (written by setup from my-config.json)'
END = '# NekoServer: end of existing media'
# Never a media folder: the system's own.
SYSTEM = ('/proc', '/sys', '/dev', '/etc', '/boot', '/usr', '/bin', '/sbin', '/lib', '/lib64', '/var/lib/docker')
# Where drives and shares are mounted: a folder there with nothing mounted on it is a
# bare mount folder (the drive or the share is away), never somebody's films.
MOUNT_PARENTS = ('/mnt', '/media', '/run/media')
NETWORK = source_import.NETWORK_TYPES


class Refused(RuntimeError):
    """The folder cannot be used; `code` says which refusal (REFUSALS), str() says why
    in words for the person."""

    def __init__(self, code, **named):
        self.code = code
        super().__init__(REFUSALS[code].format(**named))


class Away(RuntimeError):
    """A folder used in place is not there now (its drive or share is away)."""


REFUSALS = {
    'not_a_folder': '{path} is not a folder on this PC. Type the folder as this PC sees it (for a NAS: where its '
    'shared folder is mounted here, not its network address).',
    'unsafe': '{path} cannot be used: {why}.',
    'own': '{path} is inside NekoServer\'s own folders ({inside}). Those are used already; choose a folder of yours.',
    'not_mounted': 'Nothing is mounted at {mount} now, so {path} is an empty folder on this PC, not your {what}. '
    'Switch the NAS or the drive on (or mount it), then try again. Nothing was changed.',
    'bare_mount_folder': '{path} is a folder on the disk Linux runs from, in the place where drives and shares are '
    'mounted, and nothing is mounted there now. Mount the drive or the share first, then try again.',
    'not_permanent': '{mount} is connected only until the next restart (it is not in /etc/fstab), so the apps would '
    'lose {path} then. Make it permanent first (Disks: "Mount at system startup", or a line in /etc/fstab), then '
    'try again.',
    'unreadable': 'The apps\' account ({uid}:{gid}) cannot read {path} ({said}). NekoServer never changes the '
    'permissions of your folder. {fix}',
    'whitespace_share': 'The shared folder {share} is mounted at a folder with a space in its name ({mount}); '
    'NekoServer cannot write that into /etc/fstab. Mount it at a folder without spaces, then try again.',
}
# Why "save new ones here too" is not offered for a folder that can be read.
NO_NEW_FILES = {
    'read_only': '{path} is read-only here ({share} is mounted or shared read-only), so new {what} cannot be saved '
    'there. Your existing files are still used. Choose another folder for new {what}, or share it for writing '
    'on the NAS and mount it again.',
    'no_write': 'The apps\' account ({uid}:{gid}) may read {path} but not write there ({said}). NekoServer never '
    'changes the permissions of your folder. {fix} Or choose another folder for new {what}.',
    'owner': 'A file the apps\' account ({uid}:{gid}) wrote to {path} belongs to {said} there: the NAS maps every '
    'account to one of its own, and the apps could not change their own files later. Switch that mapping off '
    'for this PC on the NAS, or choose another folder for new {what}.',
}
FIX_ON_NAS = 'On the NAS, give this PC (or the account {uid}:{gid}) access to the shared folder.'
FIX_LOCAL = 'Give the account {uid}:{gid} access to it yourself (for example add it to the folder\'s group).'
WHAT = {'movies': 'movies', 'tv': 'shows', 'music': 'music'}


def app_user(config):
    """The uid and gid the media apps' containers run as (media.uid, media.gid)."""
    settings = config.get('media') or {}
    uid, gid = int(settings.get('uid', 1000)), int(settings.get('gid', 1000))
    if uid <= 0 or gid <= 0:
        raise RuntimeError('media.uid and media.gid must be positive')
    return uid, gid


def category_of(category):
    if category not in CATEGORIES:
        raise RuntimeError(f'Not a kind of media: {category!r} (movies, tv or music)')
    return CATEGORIES[category]


def own_folders(config):
    """NekoServer's own folders: a folder inside one is in use already."""
    found = []
    for key in ('data_mount', 'media_mount'):
        if isinstance(config.get(key), str) and config[key].startswith('/'):
            found.append(os.path.realpath(config[key]))
    return found


def checked_folder(config, value):
    """The folder a person gave, as the real folder it names: absolute, no control
    characters, not the system's, not NekoServer's own. Refused otherwise."""
    if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 or c == '\\' for c in value):
        raise Refused('not_a_folder', path=value)
    real = os.path.realpath(value)
    if real == '/' or any(real == top or real.startswith(top + os.sep) for top in SYSTEM):
        raise Refused('unsafe', path=real, why='it is one of the system\'s own folders')
    for own in own_folders(config):
        if real == own or real.startswith(own + os.sep) or own.startswith(real + os.sep):
            raise Refused('own', path=real, inside=own)
    if not os.path.isdir(real):
        raise Refused('not_a_folder', path=real)
    return real


def unescaped(field):
    """A field of /etc/fstab as the path it names (\\040 is a space)."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), field)


def escaped(path):
    """A path as a field of /etc/fstab."""
    return str(path).replace(' ', '\\040').replace('\t', '\\011')


def fstab_mounts(text):
    """{mount folder: what is mounted there} for every line of /etc/fstab."""
    found = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith('#') and fields[1].startswith('/'):
            found[unescaped(fields[1])] = unescaped(fields[0])
    return found


def within(path, top):
    return path == top or path.startswith(top.rstrip('/') + '/')


def where(path, find=findmnt, kind=source_import.share_type, fstab_text='', what='files'):
    """What `path` is on: {'mount', 'share', 'fstype', 'persistent'}. Refused when the
    drive or share it should be on is not mounted (the folder is then an empty one on
    this PC). `persistent`: it is mounted again at every start (/etc/fstab)."""
    fstype = kind(path)  # opens the folder once: a share mounted on first use connects now
    mount = find('TARGET', path, '--target')
    share = find('SOURCE', path, '--target')
    listed = fstab_mounts(fstab_text)
    expected = max((m for m in listed if m != '/' and within(path, m)), key=len, default=None)
    if fstype == 'autofs' or not mount or (expected and mount != expected and not within(mount, expected)):
        raise Refused('not_mounted', mount=expected or mount or path, path=path, what=what)
    if mount == '/' and any(within(path, top) for top in MOUNT_PARENTS):
        raise Refused('bare_mount_folder', path=path)
    persistent = mount == '/' or mount in listed
    return {'mount': mount, 'share': share or '', 'fstype': fstype or '', 'persistent': persistent}


# What the apps' user does in the folder, in a process of its own running as that user:
# lists it, reads the start of the first file it finds, and (to save new files there)
# writes a file of its own, checks who owns it and removes it. One line: "ok", or the
# step that failed and why.
AS_THE_APP = r"""
import os, sys
folder, write = sys.argv[1], sys.argv[2] == 'write'
def first_file(top, depth):
    for entry in sorted(os.scandir(top), key=lambda e: e.name)[:200]:
        if entry.is_file(follow_symlinks=False):
            return entry.path
        if depth and entry.is_dir(follow_symlinks=False):
            found = first_file(entry.path, depth - 1)
            if found:
                return found
    return None
try:
    found = first_file(folder, 3)
    if found:
        with open(found, 'rb') as stream:
            stream.read(4096)
except OSError as exc:
    sys.exit('read ' + (exc.strerror or str(exc)))
if write:
    name = os.path.join(folder, '.nekoserver-write-test-%d' % os.getpid())
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        sys.exit('write ' + (exc.strerror or str(exc)))
    said = ''
    try:
        os.write(fd, b'NekoServer checks that new files can be saved here\n')
        os.fsync(fd)
        seen = os.fstat(fd)
        if seen.st_uid != os.getuid():
            said = 'owner %d:%d' % (seen.st_uid, seen.st_gid)
    except OSError as exc:
        said = 'write ' + (exc.strerror or str(exc))
    finally:
        os.close(fd)
        try:
            os.unlink(name)
        except OSError as exc:
            said = said or 'remove ' + (exc.strerror or str(exc))
    if said:
        sys.exit(said)
print('ok')
"""


def as_app(uid, gid, folder, write=False, run=subprocess.run):
    """The apps' user's try at the folder: '' when it works, else what failed ("read
    Permission denied", "write Read-only file system", "owner 65534:65534")."""
    switch = {'user': uid, 'group': gid, 'extra_groups': []} if os.geteuid() == 0 else {}
    if not switch and (uid, gid) != (os.getuid(), os.getgid()):
        return 'read only root can try a folder as another account (run setup with sudo)'
    try:
        done = run(
            [sys.executable, '-I', '-c', AS_THE_APP, str(folder), 'write' if write else 'read'],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            **switch,
        )
    except subprocess.TimeoutExpired:
        return ('write' if write else 'read') + ' no answer within 90 seconds'
    if done.returncode == 0 and done.stdout.strip() == 'ok':
        return ''
    said = (done.stderr or done.stdout or '').strip().splitlines()
    return said[-1] if said else f'read stopped (exit {done.returncode})'


def read_only(path, statvfs=os.statvfs):
    return bool(statvfs(path).f_flag & os.ST_RDONLY)


def new_files_answer(real, found, category, uid, gid, as_user, statvfs):
    """Whether new files can be saved in the folder: {'ok': True}, or why not."""
    named = {'path': real, 'share': found['share'] or found['mount'], 'what': WHAT[category], 'uid': uid, 'gid': gid}
    fix = (FIX_ON_NAS if found['fstype'] in NETWORK else FIX_LOCAL).format(uid=uid, gid=gid)
    if read_only(real, statvfs):
        return {'ok': False, 'code': 'read_only', 'reason': NO_NEW_FILES['read_only'].format(**named)}
    said = as_user(uid, gid, real, True)
    if not said:
        return {'ok': True}
    kind, _, detail = said.partition(' ')
    if kind == 'write' and 'read-only' in detail.lower():
        return {'ok': False, 'code': 'read_only', 'reason': NO_NEW_FILES['read_only'].format(**named)}
    code = 'owner' if kind == 'owner' else 'no_write'
    return {'ok': False, 'code': code, 'reason': NO_NEW_FILES[code].format(said=detail or said, fix=fix, **named)}


def check(
    config,
    category,
    folder,
    new_files=False,
    *,
    as_user=as_app,
    find=findmnt,
    kind=None,
    fstab=FSTAB,
    statvfs=os.statvfs,
):
    """Check a folder a person gave for `category` ('movies', 'tv', 'music').

    Returns {'path' (the real folder), 'mount', 'share', 'fstype', 'read_only',
    'persistent', 'add_to_fstab' (a line that makes an NFS share permanent, or None),
    'hardlinks' (always False, see COPIED_TWICE), 'new_files': None when not asked,
    {'ok': True, 'note': what to say about room and time}, or {'ok': False, 'code',
    'reason'}}.
    Raises Refused (code, words) when the folder itself cannot be used. Never changes
    the folder: the only thing written is the apps' own test file, removed at once."""
    category_of(category)
    uid, gid = app_user(config)
    real = checked_folder(config, folder)
    text = fstab.read_text(encoding='utf-8', errors='replace') if fstab.is_file() else ''
    found = where(real, find, kind or source_import.share_type, text, WHAT[category])
    add = None
    if not found['persistent']:
        if found['fstype'] not in nfs_exports.FILESYSTEMS:
            raise Refused('not_permanent', mount=found['mount'], path=real)
        if any(c.isspace() for c in found['mount']):
            raise Refused('whitespace_share', share=found['share'], mount=found['mount'])
        add = nfs_exports.fstab_line(found['share'], found['mount'], found['fstype'])
    said = as_user(uid, gid, real, False)
    if said:
        fix = (FIX_ON_NAS if found['fstype'] in NETWORK else FIX_LOCAL).format(uid=uid, gid=gid)
        raise Refused('unreadable', uid=uid, gid=gid, path=real, said=said.partition(' ')[2] or said, fix=fix)
    answer = new_files_answer(real, found, category, uid, gid, as_user, statvfs) if new_files else None
    if answer and answer['ok']:
        answer['note'] = new_files_note(config, category, real)
    return {
        'path': real,
        **found,
        'read_only': read_only(real, statvfs),
        'add_to_fstab': add,
        'hardlinks': False,
        'new_files': answer,
    }


# ---- the choice in the settings -----------------------------------------------------------
# my-config.json, in the group's row of the placement table:
#   "placement": {"movies": {..., "extra": [
#       {"path": "/mnt/tank/films", "holds": "movies", "use": "in_place", "new_files": false,
#        "mount": "/mnt/tank", "share": "nas:/tank", "fstype": "nfs4"},
#       {"path": "/mnt/big/new-films", "holds": "movies", "use": "new_files", "new_files": true, ...}]}}
# For each of movies, tv and music: one folder used where it is ("in_place"), and at most
# one place new files are saved: that folder itself ("new_files": true), or a folder of
# its own for them ("use": "new_files", when the first is read-only), or neither (new
# files go to NekoServer's own library). "mount", "share" and "fstype" are what the folder
# was on when it was chosen: later checks want the same thing mounted there.


def entry_for(category, found, new_files=False, use=IN_PLACE):
    """The settings entry for a folder check() passed (`found`: what check() returned).
    `use`: IN_PLACE, or NEW for a folder of its own that only takes the new files."""
    if use not in USES:
        raise RuntimeError(f'Not a way to use a folder: {use!r}')
    new_files = bool(new_files) or use == NEW
    if new_files and not (found.get('new_files') or {}).get('ok'):
        raise RuntimeError('New files cannot be saved in this folder; choose another place for them')
    return {
        'path': found['path'],
        'holds': category,
        'use': use,
        'new_files': new_files,
        'mount': found['mount'],
        'share': found['share'],
        'fstype': found['fstype'],
    }


def with_choice(config, entry):
    """A copy of the settings with this folder for its category and use (it takes the
    place of an earlier one). There is one place for new files: a folder that takes them
    ends an earlier choice for them (the other folder stays used, read-only)."""
    row = category_of(entry['holds'])[0]
    changed = copy.deepcopy(config)
    table = copy.deepcopy(placement.table(config))
    kept = []
    for other in table[row].get('extra') or []:
        if not isinstance(other, dict) or other.get('holds') != entry['holds']:
            kept.append(other)
        elif other.get('use') == entry['use'] or (entry['new_files'] and other.get('use') == NEW):
            continue  # replaced, or a folder that was only for the new files
        elif entry['new_files']:
            kept.append({**other, 'new_files': False})
        else:
            kept.append(other)
    table[row]['extra'] = [*kept, dict(entry)]
    changed['placement'] = table
    extras(changed)  # refused here rather than at the next start
    return changed


def without_choice(config, category, use=None):
    """A copy of the settings without the folders for `category` (only the one used as
    `use`, when given)."""
    row = category_of(category)[0]
    changed = copy.deepcopy(config)
    if isinstance(changed.get('placement'), dict) and isinstance(changed['placement'].get(row), dict):
        listed = changed['placement'][row].get('extra') or []
        changed['placement'][row]['extra'] = [
            e for e in listed if e.get('holds') != category or (use is not None and e.get('use') != use)
        ]
    return changed


def listed_extras(saved, row):
    """The "extra" folders a row of the saved table lists; [] when it lists none."""
    listed = (saved.get(row) or {}).get('extra') if isinstance(saved.get(row), dict) else None
    if listed is None:
        return []
    if not isinstance(listed, list):
        raise RuntimeError(f'"extra" of "{row}" under "placement" in my-config.json must be a list')
    return listed


def usable_saved_path(config, holds, path):
    """The path as saved must be the real folder check() returned, outside the system's
    and NekoServer's own folders: checked again, it is only a settings value."""
    real = os.path.realpath(path)
    unsafe = real != path or not path.startswith('/') or any(ord(c) < 32 or c == '\\' for c in path)
    if unsafe or real == '/' or any(within(real, top) for top in (*SYSTEM, *own_folders(config))):
        raise RuntimeError(f'The "extra" folder for {holds} in my-config.json is not a folder that may be used')


def checked_extra(config, row, entry, seen, writable):
    """One "extra" folder of a row, as far as a settings file can be trusted. `seen`:
    the (kind, use) pairs taken already; `writable`: the kinds that have their place for
    new files. Both are added to."""
    if not isinstance(entry, dict) or entry.get('use') not in USES:
        raise RuntimeError(f'An "extra" folder of "{row}" in my-config.json has no "use" NekoServer knows')
    holds, path, use = entry.get('holds'), entry.get('path'), entry['use']
    if holds not in CATEGORIES or CATEGORIES[holds][0] != row or (holds, use) in seen:
        raise RuntimeError(f'"holds" of an "extra" folder of "{row}" in my-config.json is wrong or given twice')
    new_files = entry.get('new_files', use == NEW)
    if not isinstance(path, str) or type(new_files) is not bool or (use == NEW and not new_files):
        raise RuntimeError(f'The "extra" folder for {holds} in my-config.json needs "path" and "new_files"')
    if new_files and holds in writable:
        raise RuntimeError(f'my-config.json names two places for new {holds}; there is one')
    missing = [key for key in ('mount', 'share', 'fstype') if not isinstance(entry.get(key), str)]
    if missing:
        raise RuntimeError(f'The "extra" folder for {holds} in my-config.json needs "{missing[0]}"')
    usable_saved_path(config, holds, path)
    seen.add((holds, use))
    if new_files:
        writable.add(holds)
    return {**entry, 'new_files': new_files}


def extras(config):
    """The person's folders, from the settings, each checked as far as a settings file can
    be trusted: [{'path', 'holds', 'use', 'new_files', 'mount', 'share', 'fstype'}]."""
    saved = config.get('placement')
    if not isinstance(saved, dict):
        return []
    seen, writable = set(), set()
    return [
        checked_extra(config, row, entry, seen, writable)
        for row in ('movies', 'music')
        for entry in listed_extras(saved, row)
    ]


def slot(category, use=IN_PLACE):
    """The folder's name under library/existing and /data/existing: the kind, or
    "<kind>-new" for a folder that only takes the new files."""
    category_of(category)
    if use not in USES:
        raise RuntimeError(f'Not a way to use a folder: {use!r}')
    return category if use == IN_PLACE else category + '-new'


def mount_point(config, category, use=IN_PLACE):
    """Where the folder for `category` is mounted on this PC: library/existing/<slot>
    on its group's drive."""
    root = os.path.realpath(files_root(config, category_of(category)[0]))
    point = os.path.realpath(os.path.join(root, 'library', EXISTING, slot(category, use)))
    # Under the group's own drive, whatever the settings say.
    if not point.startswith(root + os.sep):
        raise RuntimeError(f'The folder for existing {category} would be outside {root}')
    return Path(point)


def container_path(category, use=IN_PLACE):
    """Where every app sees the folder for `category`."""
    return f'{CONTAINER}/{slot(category, use)}'


def new_files_entry(config, category):
    """The person's folder new films, shows or music are saved in, or None when they go
    to NekoServer's own library."""
    category_of(category)
    return next((e for e in extras(config) if e['holds'] == category and e['new_files']), None)


def new_files_folder(config, category):
    """The ONE folder new films, shows or music are saved in, as the apps see it: the
    person's folder when one was chosen for them (/data/existing/movies, or
    /data/existing/movies-new), else the library's own (/data/movies, /data/tv,
    /data/music). Sonarr's, Radarr's and Lidarr's root folder, and Seerr's."""
    entry = new_files_entry(config, category)
    return container_path(category, entry['use']) if entry else f'/data/{category}'


def not_backed_up(config):
    """[(category, folder, why)]: the person's own folders are never in a NekoServer backup
    (often larger than the backup drive). For the list of what no job copies
    (backup_plan.not_copied) and the dashboard."""
    return [
        (e['holds'], e['path'], 'your own folder, used where it is: NekoServer does not copy it; keep your own copy')
        for e in extras(config)
    ]


# ---- new files in the person's folder ------------------------------------------------------
# A download is saved in the library's own downloads folder (/data/downloads), then the
# importer (Radarr, Sonarr, Lidarr) puts it into the folder for new files. The person's
# folder is a mount of its own inside /data, and Linux makes no hard link from one mount
# to another, even on one drive: the importer copies the file and removes the download.
# So a hard link is never promised here; the room is needed twice until the copy is done.

COPIED_TWICE = (
    'New {what} are downloaded to {downloads} first and then copied into {path}; the download is removed after '
    'the copy. Each one needs room in both places for that moment, and the copy takes as long as the file is big.'
)


def downloads_folder(config, category):
    """Where downloads of this kind are saved on this PC before they are imported."""
    if category_of(category)[0] == 'music':
        return music_downloads(config)
    return files_root(config, 'movies') / 'library' / 'downloads'


def new_files_note(config, category, path):
    """What to say when new files are to be saved in a folder of the person's."""
    return COPIED_TWICE.format(what=WHAT[category], downloads=downloads_folder(config, category), path=path)


def new_files_ready(config, category, **how):
    """Before an app is told to save new files in the person's folder: the folder as the
    apps see it. Away when it is not there now (nothing is then written in its place).
    None when new files go to NekoServer's library."""
    entry = new_files_entry(config, category)
    if entry is None:
        return None
    row = next(r for r in available(config, **how) if (r['holds'], r['use']) == (category, entry['use']))
    if not row['ready']:
        raise Away(row['reason'])
    return row['container']


# ---- mounting it ---------------------------------------------------------------------------


def fstab_lines(config):
    """The /etc/fstab lines that bind each of the person's folders onto its mount point:
    read-only unless new files go there; `nofail`, and only once the folder's own drive
    or share is mounted (x-systemd.requires-mounts-for), so a share that is away leaves
    the mount point as it is (root's, closed to the apps) instead of an empty folder."""
    lines = []
    for entry in extras(config):
        point = mount_point(config, entry['holds'], entry['use'])
        mode = 'rw' if entry['new_files'] else 'ro'
        source, root = escaped(entry['path']), escaped(point.parents[2])
        needs = f'x-systemd.requires-mounts-for={source},x-systemd.requires-mounts-for={root}'
        lines.append(f'{source} {escaped(point)} none bind,{mode},nofail,{needs} 0 0')
    return lines


def fstab_with(text, lines, extra=()):
    """/etc/fstab's text with NekoServer's block replaced by these lines (no block when
    there are none). `extra`: lines to add once outside the block (an NFS share made
    permanent), left alone afterwards."""
    kept, inside = [], False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            inside = True
        elif line.strip() == END:
            inside = False
        elif not inside:
            kept.append(line)
    have = {tuple(line.split()[:2]) for line in kept}
    kept += [line for line in extra if tuple(line.split()[:2]) not in have]
    if lines:
        kept += [BEGIN, *lines, END]
    return '\n'.join(kept) + '\n'


def still_there(entry, find=findmnt, kind=source_import.share_type):
    """The folder is on what it was on when it was chosen: the same drive or share,
    mounted at the same place (not the empty folder left when a share is away)."""
    if kind(entry['path']) == 'autofs' or not os.path.isdir(entry['path']):
        return False
    return find('TARGET', entry['path'], '--target') == entry['mount'] and (
        not entry['share'] or find('SOURCE', entry['path'], '--target') == entry['share']
    )


def use_in_place(
    config,
    permanent=(),
    *,
    run=None,
    fstab=FSTAB,
    find=findmnt,
    kind=source_import.share_type,
    mounted=source_import.mount_id,
    log=print,
):
    """Mount every one of the person's folders, now and at every start, from the settings.

    Writes NekoServer's block in /etc/fstab (and `permanent`: the 'add_to_fstab' lines
    check() gave), makes each mount point (root's, closed to the apps, so nothing can be
    written into it while the folder is away) and mounts it. A folder that is away now is
    left for the next start and reported. Returns [{'holds', 'use', 'path', 'mounted',
    'reason'}]. Run again after the settings change; with no folders left it removes its
    block."""
    run = run or must_run
    wanted = extras(config)
    lines = fstab_lines(config)
    before = fstab.read_text(encoding='utf-8') if fstab.is_file() else ''
    after = fstab_with(before, lines, permanent)
    if after != before:
        atomic_write(fstab, after, 0o644)
        run(['systemctl', 'daemon-reload'], 60)
    return [mounted_row(config, entry, (run, find, kind, mounted), log) for entry in wanted]


def mounted_row(config, entry, tools, log):
    """One of the person's folders mounted where the apps see it (read-only unless it
    takes the new files), or left for the next start when it is away: its row."""
    run, find, kind, mounted = tools
    point = mount_point(config, entry['holds'], entry['use'])
    if not point.is_dir():
        point.mkdir(parents=True, mode=0o555)
        os.chmod(point, 0o555)  # root's and closed: the apps cannot write into the bare folder
    row = {'holds': entry['holds'], 'use': entry['use'], 'path': entry['path'], 'mounted': False, 'reason': None}
    if not still_there(entry, find, kind):
        row['reason'] = away_words(entry)
        log(row['reason'])
        return row
    mode = 'rw' if entry['new_files'] else 'ro'
    if mounted(point) is None:
        run(['mount', str(point)], 120)
    else:
        run(['mount', '-o', f'remount,bind,{mode}', str(point)], 120)
    row['mounted'] = True
    return row


AWAY = (
    'Your {what} in {path} are not there now ({share} is not connected). Nothing is written in their place; '
    'they come back by themselves when it is connected again.'
)


def away_words(entry):
    return AWAY.format(what=WHAT[entry['holds']], path=entry['path'], share=entry['share'] or entry['mount'])


def must_run(argv, timeout):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode:
        raise RuntimeError(f'{" ".join(argv[:3])} failed: {(done.stderr or done.stdout)[-300:].strip()}')
    return done.stdout


def available(config, find=findmnt, kind=source_import.share_type, mounted=source_import.mount_id):
    """Each of the person's folders, and whether it is there now: [{'holds', 'use', 'path',
    'container', 'new_files', 'ready', 'reason'}]. Ready: the folder is on the drive or
    share it was chosen on, and it is mounted in the library. For the checkup, the
    dashboard and anything about to write."""
    rows = []
    for entry in extras(config):
        there = still_there(entry, find, kind)
        bound = mounted(mount_point(config, entry['holds'], entry['use'])) is not None
        reason = None
        if not there:
            reason = away_words(entry)
        elif not bound:
            reason = f'{entry["path"]} is there, but not connected to the library yet; run setup again (or restart)'
        rows.append(
            {
                'holds': entry['holds'],
                'use': entry['use'],
                'path': entry['path'],
                'container': container_path(entry['holds'], entry['use']),
                'new_files': entry['new_files'],
                'ready': there and bound,
                'reason': reason,
            }
        )
    return rows


def guard(config, category, **how):
    """Before anything is written to (or a library made for) the person's folders of
    `category`: Away when one is not there now."""
    for row in available(config, **how):
        if row['holds'] == category and not row['ready']:
            raise Away(row['reason'])


# ---- the libraries in the apps -------------------------------------------------------------


NAME_TAKEN = '{app} already has a library called "{name}" for another folder; rename it in {app}, then run setup again'
API_FAILED = 'Library API/path check failed; existing settings preserved'
TOO_OLD = (
    'This Navidrome cannot have a second music folder (it needs version 0.58 or newer); update it, then run setup again'
)


def library_name(entry):
    """What the library for one of the person's folders is called in the app."""
    name = category_of(entry['holds'])[1]
    return name if entry['use'] == IN_PLACE else name.replace('(your folder)', '(new, your folder)')


def ready_row(config, entry, how):
    """Away unless this one folder is there now."""
    for row in available(config, **how):
        if (row['holds'], row['use']) == (entry['holds'], entry['use']) and not row['ready']:
            raise Away(row['reason'])


def library_row(check_name, make, too_old=()):
    """One library's row: what `make()` returns, or why it could not be made. A folder
    that is away and a step left for the person are said as they are; `too_old`: the
    errors that mean the app has no such thing yet."""
    try:
        return make()
    except (Away, media_libraries.SetupRequired) as exc:
        return media_libraries.row(check_name, 'needs_setup', str(exc))
    except too_old:
        return media_libraries.row(check_name, 'needs_setup', TOO_OLD)
    except (RuntimeError, ValueError, TypeError, KeyError):
        return media_libraries.row(check_name, 'fail', API_FAILED)


def jellyfin_library(client, config, entry, kind, apply, how):
    """Jellyfin's library for one folder: there already, or made and read back."""
    name, check_name = library_name(entry), f"jellyfin:existing:{slot(entry['holds'], entry['use'])}"
    ready_row(config, entry, how)
    path = container_path(entry['holds'], entry['use'])

    def there():
        return any(path in [str(p).rstrip('/') for p in x['Locations']] for x in media_libraries.folders(client))

    values = media_libraries.folders(client)
    if any(path in [str(p).rstrip('/') for p in x['Locations']] for x in values):
        return media_libraries.row(check_name, 'pass')
    if any(str(x.get('Name', '')).casefold() == name.casefold() for x in values):
        raise media_libraries.SetupRequired(NAME_TAKEN.format(app='Jellyfin', name=name))
    if not apply:
        return media_libraries.row(check_name, 'needs_setup', 'Preview: library would be created')
    asked = {'name': name, 'collectionType': kind, 'paths': path, 'refreshLibrary': 'true'}
    client.call('/Library/VirtualFolders?' + urllib.parse.urlencode(asked), 'POST', {})
    if not there():
        raise RuntimeError('Library creation was not retained')
    return media_libraries.row(check_name, 'pass')


def jellyfin_libraries(client, config, apply=False, **how):
    """A Jellyfin library for each of the person's folders of films and shows, pointing at
    where Jellyfin sees it (/data/existing/movies). `client`: a media_libraries.Client
    signed in as administrator. Nothing is made while the folder is away. Rows as
    media_libraries.row gives them."""
    rows = []
    for entry in extras(config):
        kind = category_of(entry['holds'])[2]
        if kind is None:
            continue
        check_name = f"jellyfin:existing:{slot(entry['holds'], entry['use'])}"
        rows.append(
            library_row(
                check_name, lambda entry=entry, kind=kind: jellyfin_library(client, config, entry, kind, apply, how)
            )
        )
    return rows


def navidrome_listed(call):
    """Navidrome's libraries, as its API lists them."""
    listed = call('GET', '/api/library', None)
    if not isinstance(listed, list) or any(not isinstance(x, dict) for x in listed):
        raise RuntimeError('Unsupported Navidrome library response')
    return listed


def navidrome_one(call, config, entry, apply, how):
    """Navidrome's library for one music folder: there already, or made and read back."""
    name, path = library_name(entry), container_path('music', entry['use'])
    check_name = f"navidrome:existing:{slot('music', entry['use'])}"
    ready_row(config, entry, how)
    listed = navidrome_listed(call)
    if any(str(x.get('path', '')).rstrip('/') == path for x in listed):
        return media_libraries.row(check_name, 'pass')
    if any(str(x.get('name', '')).casefold() == name.casefold() for x in listed):
        raise media_libraries.SetupRequired(NAME_TAKEN.format(app='Navidrome', name=name))
    if not apply:
        return media_libraries.row(check_name, 'needs_setup', 'Preview: library would be created')
    call('POST', '/api/library', {'name': name, 'path': path})
    again = call('GET', '/api/library', None)
    if not isinstance(again, list) or not any(isinstance(x, dict) and x.get('path') == path for x in again):
        raise RuntimeError('Library creation was not retained')
    return media_libraries.row(check_name, 'pass')


def navidrome_library(call, config, apply=False, **how):
    """A Navidrome library for each of the person's music folders (/data/existing/music).
    `call(method, endpoint, payload)`: Navidrome's own API as its administrator, returning
    the decoded answer (GET /api/library lists, POST /api/library adds); navidrome_api()
    makes one. A Navidrome without several libraries answers 404 (LookupError): said as a
    step left, never an error."""
    rows = []
    for entry in extras(config):
        if entry['holds'] != 'music':
            continue
        check_name = f"navidrome:existing:{slot('music', entry['use'])}"
        rows.append(
            library_row(check_name, lambda entry=entry: navidrome_one(call, config, entry, apply, how), (LookupError,))
        )
    return rows


NAVIDROME = f"http://127.0.0.1:{app_port('navidrome')}"


def navidrome_send(opener, address, method, payload, headers):
    """One request to Navidrome's API and its decoded answer. LookupError for a 404 (this
    Navidrome has no such thing); RuntimeError for any other failure."""
    body = json.dumps(payload).encode() if payload is not None else None
    asked = urllib.request.Request(
        address, data=body, method=method, headers={'Content-Type': 'application/json', **headers}
    )
    try:
        with opener.open(asked, timeout=12) as answer:
            data = answer.read(3_000_001)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 404:
            raise LookupError('navidrome: HTTP 404') from None
        raise RuntimeError(f'navidrome: HTTP {code}') from None
    except OSError:
        raise RuntimeError('navidrome: unavailable') from None
    if len(data) > 3_000_000:
        raise RuntimeError('API response too large')
    return json.loads(data) if data.strip() else {}


def navidrome_api(username, password, base=NAVIDROME, opener=None):
    """Navidrome's own API as its administrator: call(method, endpoint, payload) for
    navidrome_library. Signs in once (POST /auth/login) and sends the token it is given
    with every request; the password is not kept. Local only, no redirects or proxies.
    SetupRequired when the login is refused or is not an administrator's."""
    opener = opener or local_opener()
    try:
        login = {'username': username, 'password': password}
        signed = navidrome_send(opener, base + '/auth/login', 'POST', login, {})
    except (RuntimeError, LookupError):
        raise media_libraries.SetupRequired(
            'Navidrome did not accept its administrator login; run setup again once the saved login works'
        ) from None
    if not isinstance(signed, dict) or not signed.get('token') or signed.get('isAdmin') is not True:
        raise media_libraries.SetupRequired('Navidrome\'s saved login is not an administrator\'s; a library needs one')
    token = {'X-ND-Authorization': 'Bearer ' + str(signed['token'])}
    return lambda method, endpoint, payload=None: navidrome_send(opener, base + endpoint, method, payload, token)


def music_libraries(config, apply=False, api=navidrome_api, **how):
    """Navidrome's libraries for the person's music folders, with the administrator login
    setup saved (account_store). [] with no such folder; a step left when there is no
    verified login yet."""
    if not any(e['holds'] == 'music' for e in extras(config)):
        return []
    saved = account_store.get(config, 'navidrome', verified=True)
    try:
        if not saved:
            raise media_libraries.SetupRequired('Navidrome\'s administrator login is not set up yet')
        call = api(saved['username'], saved['password'])
    except media_libraries.SetupRequired as exc:
        return [media_libraries.row('navidrome:existing:music', 'needs_setup', str(exc))]
    return navidrome_library(call, config, apply, **how)


# ---- copying instead -----------------------------------------------------------------------


def copy_preview(config, category, folder, *, find=findmnt, kind=None, fstab=FSTAB, usage=shutil.disk_usage):
    """What copying the folder into the NekoServer library would do, before anything is
    copied: {'source', 'destination', 'files', 'bytes', 'free', 'fits', 'conflicts' (files
    the library already has differently: kept, the source's left out), 'source_kept':
    True, 'entry' (the source-list entry copy_now and source_import use)}.
    Raises Refused as check() does; source_import.IncompleteSource when part of the
    folder cannot be read (nothing would be copied)."""
    row = category_of(category)[0]
    real = checked_folder(config, folder)
    text = fstab.read_text(encoding='utf-8', errors='replace') if fstab.is_file() else ''
    found = where(real, find, kind or source_import.share_type, text, WHAT[category])
    root = files_root(config, row)
    target = source_import.import_target(Path(real), root, category)
    conflicts = []
    entries = source_import.planned_copies(Path(real), target, False, conflicts)
    needed = sum(stat.st_size for _, _, stat in entries)
    free = usage(root).free
    remote = found['fstype'] in NETWORK
    entry = {'type': 'remote' if remote else 'local', 'category': category, 'copy_to_library': True}
    entry['mounted_path' if remote else 'path'] = real
    return {
        'source': real,
        'destination': str(target),
        'files': len(entries),
        'bytes': needed,
        'free': free,
        'fits': needed <= free,
        'conflicts': conflicts,
        'source_kept': True,
        'entry': entry,
    }


NO_ROOM = (
    'Copying {source} needs {need} and {destination} has {free} free, so nothing was copied. Free some room, '
    'choose fewer folders, or use the files where they are.'
)


NO_REPORT = 'The copy left no report; look at the library and run setup again'


def size_words(count):
    """A number of bytes as people say it: "1.5 GB"."""
    for unit, size in (('TB', 1000**4), ('GB', 1000**3), ('MB', 1000**2), ('kB', 1000)):
        if count >= size:
            return f'{count / size:.1f} {unit}'
    return f'{count} bytes'


def sources_file(config):
    """The list of sources the import reads (<app SSD>/config/media-sources.json)."""
    root = os.path.realpath(config['data_mount'])
    path = os.path.realpath(os.path.join(root, 'config', 'media-sources.json'))
    if not path.startswith(root + os.sep):
        raise RuntimeError('The source list would be outside the app SSD')
    return Path(path)


def listed_source(config, entry):
    """The entry put into the source list (once), so a later run of the import knows the
    folder and that it was copied. Returns the list as saved."""
    path = sources_file(config)
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    sources = saved.get('sources') if isinstance(saved, dict) and isinstance(saved.get('sources'), list) else []
    if not any(isinstance(s, dict) and source_import.source_key(s) == source_import.source_key(entry) for s in sources):
        sources = [*sources, dict(entry)]
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, {'schema_version': 1, 'sources': sources})
    return sources


def copy_now(config, category, folder, *, run=source_import.run, verify=verify_storage, again=False, **how):
    """Copy the folder into the NekoServer library, the source kept as it is (after the
    person saw copy_preview and said yes). The copy is source_import's: add-only, a file
    the library already has differently is kept and the source's left out, a drive that
    goes away stops it. Refused (as copy_preview) when the folder is away; RuntimeError
    with the sizes when it does not fit, before anything is copied.

    Returns the import's row for this folder ({'status': 'copied', 'copied', 'bytes',
    'destination', 'reason' ...}) with 'source' and 'source_kept': True. Needs root and
    the drives mounted, as source_import.run does."""
    shown = copy_preview(config, category, folder, **how)
    if not shown['fits']:
        raise RuntimeError(
            NO_ROOM.format(
                source=shown['source'],
                need=size_words(shown['bytes']),
                destination=shown['destination'],
                free=size_words(shown['free']),
            )
        )
    listed_source(config, shown['entry'])
    run(config, [shown['entry']], True, lambda: verify(config), again)
    report = os.path.join(os.path.realpath(config['data_mount']), 'reports', 'source-import.json')
    try:
        rows = json.loads(Path(report).read_text(encoding='utf-8')).get('sources') or []
    except (OSError, ValueError, AttributeError):
        rows = []
    row = rows[0] if rows and isinstance(rows[0], dict) else {'status': 'needs_setup', 'reason': NO_REPORT}
    return {**row, 'source': shown['source'], 'source_kept': True}
