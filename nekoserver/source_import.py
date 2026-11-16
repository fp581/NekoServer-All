#!/usr/bin/env python3
"""Verified, add-only media copies from local folders or existing network mounts."""

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from stat import S_ISREG
import neko_common
from neko_common import (
    UnreadableEntries,
    findmnt,
    partial_name,
    safe_path,
    atomic_json,
    strict_files,
    verify_storage,
    rename_no_replace,
    checked_settings,
    default_settings,
)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class SourceGone(RuntimeError):
    """The source folder is not there (a drive unplugged after its import, say)."""


class NotMountedYet(SourceGone):
    """A network share set up to be mounted when it is first opened (automount) that did
    not connect when the import opened it (the NAS off or asleep, say)."""


def size_words(count):
    """A number of bytes as people say it: "1.5 GB"."""
    for unit, size in (('TB', 1000**4), ('GB', 1000**3), ('MB', 1000**2), ('kB', 1000)):
        if count >= size:
            return f'{count / size:.1f} {unit}'
    return f'{count} bytes'


class Said(RuntimeError):
    """Why a source was not copied, in words for the person and with no folder of theirs
    in it: the report keeps them as they are (other errors get one general reason)."""


class SourceChanged(RuntimeError):
    """A file changed while it was copied (still being written, say); names the file."""


# The kernel's names for SMB and NFS mounts: a share mounted with `mount -t smb3` (or
# smb3 in /etc/fstab) shows as smb3, not cifs.
NETWORK_TYPES = ('cifs', 'smb3', 'nfs', 'nfs4')


def source_list(path):
    """The sources in the source list file (config/media-sources.json, which the source
    wizard writes). A file that is not such a list (damaged, or changed by hand) is
    ValueError in words, never a traceback, and nothing is written over it."""
    fix = f'fix the file {path} or move it away, then run setup again'
    # The path comes from the command line or the settings: checked where it is read.
    # Only a .json file is read, by its real path, never through a link.
    real = os.path.realpath(safe_path(path))
    if not (real.startswith(os.sep) and real.endswith('.json')) or '\x00' in real:
        raise ValueError(f'The list of your media sources must be a .json file; {path} is not one')
    try:
        with open(real, encoding='utf-8') as handle:
            value = json.loads(handle.read())
    except ValueError:
        raise ValueError(f'The list of your media sources is damaged (it is not valid JSON); {fix}') from None
    sources = value.get('sources', []) if isinstance(value, dict) else None
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        raise ValueError(f'The list of your media sources is not in the form setup writes; {fix}')
    return sources


def source_key(source):
    """A source's identity in the record of finished imports (never its path itself)."""
    where = source.get('mounted_path') if source.get('type') == 'remote' else source.get('path')
    return hashlib.sha256((str(source.get('category')) + '\0' + str(where)).encode()).hexdigest()[:16]


def source_folder(source):
    if source.get('type') == 'indexer':
        return None
    if source.get('type') == 'remote':
        value = source.get('mounted_path')
        if not value:
            raise Said(
                'This network share is only noted: it has no folder on this PC yet. Mount it here, then add it '
                'again in the source wizard with the folder it is mounted on'
            )
        path = safe_path(value)
        kind = share_type(path)
        if kind == 'autofs':
            raise NotMountedYet('The network share is not mounted yet')
        if kind not in NETWORK_TYPES:
            # The share is not mounted there now (unmounted after its import, the NAS off):
            # the folder is then an empty one on this PC, never copied from. Gone, like an
            # unplugged drive, so a finished import is not reported as a step left to do.
            raise SourceGone('Remote folder is not on a mounted SMB/NFS share')
    elif source.get('type') == 'local':
        path = safe_path(source['path'])
    else:
        raise RuntimeError('Unknown source type')
    if not path.is_absolute() or not path.is_dir():
        raise SourceGone('Source folder is not accessible')
    return path


def share_type(path):
    """The filesystem type at `path`, after opening the folder once: a share set up with
    an automount (x-systemd.automount in /etc/fstab) is mounted only when something opens
    it, and until then shows as autofs."""
    try:
        with os.scandir(path) as listing:
            next(listing, None)
    except OSError:
        pass  # not there or not readable: findmnt and the checks after it say what it is
    return findmnt('FSTYPE', path, '--target')


# Filesystems whose inode numbers are made up again each time a file is looked up (FAT
# and exFAT drives, FUSE drivers, SMB shares without server inode numbers): a file
# planned early in a long import can come back with another number, unchanged. Those
# are identified by drive, size and date instead.
UNSTABLE_INODES = ('vfat', 'msdos', 'exfat', 'fuseblk', 'cifs', 'smb3')


def stable_inodes(path):
    """False when the filesystem at `path` does not keep its inode numbers (see above).
    An unknown type is taken to keep them, which is the stricter check."""
    kind = findmnt('FSTYPE', path, '--target') or ''
    return not (kind in UNSTABLE_INODES or kind.startswith('fuse.'))


# Files still being written by another program; never imported.
TEMPORARY = ('.neko-part', '.part', '.tmp', '.crdownload', '.!qB')
# What a download program leaves beside its downloads for its own use, never media and
# never finished: qBittorrent's ".<torrent>.parts" (pieces of the files you did not
# choose). They were copied into the library as films or music.
LEFTOVERS = ('.parts',)
# How to copy what is new in a source copied before (it is skipped otherwise).
AGAIN = 'sudo python3 source_import.py --config my-config.json --again --apply'


class DriveGone(Exception):
    """The destination drive is no longer the mounted drive it was: the whole import stops
    (not just this source), before anything more is written."""


# The kernel's table of what is mounted where (a fixed file, read fresh each time).
MOUNT_TABLE = Path('/proc/self/mountinfo')


def mount_id(root):
    """The kernel's ID of the mount at `root`, or None when nothing is mounted there. A
    new mount (the drive plugged in again, or another one) gets a new ID."""
    found = None
    for line in MOUNT_TABLE.read_text(encoding='utf-8', errors='replace').splitlines():
        fields = line.split()
        # Spaces and other odd characters appear as octal escapes (\040 for a space).
        point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]) if len(fields) > 4 else None
        if point == str(root):
            found = fields[0]  # the last one mounted there is the one in use
    return found


class DriveGuard:
    """The destination's own drive, remembered while it is mounted. Checked before each
    file and again before it is published: if the drive was unmounted, a copy would land
    in the empty folder underneath, on the system disk, and could fill it."""

    def __init__(self, root):
        self.root = Path(root)
        self.mount = mount_id(self.root)
        if self.mount is None:
            raise DriveGone('Destination drive is not mounted')
        self.device = None  # the drive's own device number, from the first folder checked

    def check(self, path):
        path = Path(path)
        while not path.exists() and path != self.root and self.root in path.parents:
            path = path.parent  # a folder still to be made: check where it will be made
        try:
            device = os.stat(path).st_dev
        except OSError:
            device = None
        if self.device is None:
            self.device = device
        self.same(device)

    def check_fd(self, fd):
        """The same check for an open folder or file (its handle, not its name)."""
        try:
            device = os.fstat(fd).st_dev
        except OSError:
            device = None
        if self.device is None:
            self.device = device
        self.same(device)

    def same(self, device):
        if mount_id(self.root) != self.mount or device is None or device != self.device:
            raise DriveGone('The destination drive is no longer mounted; the import stopped before writing more')


class IncompleteSource(RuntimeError):
    """Parts of a source could not be read; `paths` are relative to the source folder."""

    def __init__(self, paths):
        self.paths = paths
        super().__init__('Could not read: ' + ', '.join(paths[:5]))


# Folders are opened one name at a time, each inside the one before, never following a
# symlink. A source folder (a download client's, say) and the library folders can be
# changed by the apps' containers while a copy runs: a file or folder swapped for a
# symlink between the checks and the copy could otherwise make NekoServer (which runs as
# root) read a file from outside the source, or write one outside the library.
FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def rename_at(folder, old, new):
    """Rename `old` to `new` inside the open `folder`, never over something already there
    (FileExistsError). Raises RuntimeError without Linux renameat2."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise RuntimeError('Linux renameat2 is needed to publish files safely')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(folder, os.fsencode(old), folder, os.fsencode(new), 1) != 0:  # RENAME_NOREPLACE
        code = ctypes.get_errno()
        raise (FileExistsError if code == errno.EEXIST else OSError)(code, os.strerror(code), new)


def make_folder(parent, name, mode, uid, gid):
    """Make the folder `name` inside the open `parent` with its owner and mode already set
    (made under a temporary name, then moved into place, as ensure_directory does)."""
    temp = '.' + name + '.neko-new'
    try:
        os.rmdir(temp, dir_fd=parent)  # left by a run cut off here (never a symlink: rmdir refuses one)
    except OSError:
        pass
    os.mkdir(temp, mode, dir_fd=parent)
    set_owner(parent, temp, mode, uid, gid)
    try:
        rename_at(parent, temp, name)
    except FileExistsError:
        os.rmdir(temp, dir_fd=parent)  # made meanwhile by something else: that one stays
    except (OSError, RuntimeError):
        # A filesystem without no-replace renames (FAT, say): made in place.
        os.rmdir(temp, dir_fd=parent)
        os.mkdir(name, mode, dir_fd=parent)
        set_owner(parent, name, mode, uid, gid)


def set_owner(parent, name, mode, uid, gid):
    """Owner and mode of a folder just made, through its own handle (not its name)."""
    folder = os.open(name, FOLDER, dir_fd=parent)
    try:
        if uid is not None:
            os.fchown(folder, uid, gid)
        os.fchmod(folder, mode)  # after the owner change, whatever the umask
    finally:
        os.close(folder)


def open_folder(path, mode=None, uid=None, gid=None):
    """A handle on the folder `path`, opened from / one name at a time without following
    any symlink (OSError if one is in the way). With `mode`, missing folders are made."""
    folder = os.open('/', FOLDER)
    try:
        for name in Path(path).parts[1:]:
            try:
                inner = os.open(name, FOLDER, dir_fd=folder)
            except FileNotFoundError:
                if mode is None:
                    raise
                make_folder(folder, name, mode, uid, gid)
                inner = os.open(name, FOLDER, dir_fd=folder)
            os.close(folder)
            folder = inner
    except BaseException:
        os.close(folder)
        raise
    return folder


def identity(info, stable=True):
    """What makes a file the one that was planned: its drive and inode, or on a
    filesystem without stable inode numbers its drive, size and date."""
    return (info.st_dev, info.st_ino) if stable else (info.st_dev, info.st_size, info.st_mtime_ns)


def open_source(current, stat, stable=True):
    """The source file, opened without following a symlink anywhere below the source
    folder, and checked to be the very file that was planned (see identity(), a regular
    file). Anything else means the source changed since it was checked."""
    parent = open_folder(current.parent)
    try:
        reader = os.open(current.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(reader)
    if not S_ISREG(info.st_mode) or identity(info, stable) != identity(stat, stable):
        os.close(reader)
        raise SourceChanged('Source changed during import')
    return reader


def fd_digest(fd):
    h = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, 1024 * 1024, offset):
        h.update(chunk)
        offset += len(chunk)
    return h.hexdigest()


def remove_partial(parent, temp):
    """A partial copy left by an interrupted run is removed (only a plain file)."""
    try:
        info = os.stat(temp, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not S_ISREG(info.st_mode):
        raise RuntimeError('Unsafe partial import')
    os.unlink(temp, dir_fd=parent)


def copy_into(reader, writer, stat):
    """Stream the source into the new file, then check both: the source kept its size
    and date while it was read, and the copy on disk holds the same bytes."""
    while chunk := os.read(reader, 1024 * 1024):
        write_all(writer, chunk)
    os.fsync(writer)
    # Dropped from memory, so the copy is read back from the drive: read from memory, a
    # drive that did not keep the bytes passed (as neko_common.copy_checked_into does).
    os.posix_fadvise(writer, 0, 0, os.POSIX_FADV_DONTNEED)
    after = os.fstat(reader)
    if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns or fd_digest(writer) != fd_digest(reader):
        raise SourceChanged('Source changed during import')


def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def publish_at(parent, temp, name, expected):
    """Publish the finished copy `temp` as `name` inside the open `parent`, never over an
    existing file: a hard link (no symlink followed), or a no-replace rename on drives
    without hard links (exFAT). `expected` is the copy's own (device, inode): a file
    put in its place meanwhile is never published."""
    try:
        os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        rename_at(parent, temp, name)
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (info.st_dev, info.st_ino) != expected:
        raise RuntimeError('Library folder changed during import')
    os.fsync(parent)


def copy_mode(original):
    """A copy's permissions: read and written by the library's owner and group (the media
    apps), and readable by other accounts only when the original was (Samba may use another
    account than the library's owner). A copy is never more open than its original."""
    return 0o660 | (original.st_mode & 0o004)


def import_file(current, dest, stat, uid, gid, guard=None, stable=True):
    """Copy one file next to its destination, check it, then publish it (never over an
    existing file). The destination drive is checked before writing and before publishing.
    Source and library folders are used through handles opened without following
    symlinks, so a folder or file swapped meanwhile can't send the copy elsewhere."""
    if guard:
        guard.check(dest.parent)
    reader = open_source(current, stat, stable)
    parent = None
    try:
        parent = open_folder(safe_path(dest.parent), 0o2775, uid, gid)
        if guard:
            guard.check_fd(parent)
        # A short name of its own: a 255-byte name plus a suffix would be too long.
        temp = partial_name(dest.name)
        remove_partial(parent, temp)
        writer = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        try:
            copy_into(reader, writer, stat)
            os.fchmod(writer, copy_mode(stat))
            if uid is not None:
                os.fchown(writer, uid, gid)
            # The original's date, so a later run recognises the copy without reading both.
            os.utime(writer, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            if guard:
                guard.check_fd(writer)
            mine = os.fstat(writer)
            publish_at(parent, temp, dest.name, (mine.st_dev, mine.st_ino))
        finally:
            os.close(writer)
            try:
                os.unlink(temp, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(reader)
        if parent is not None:
            os.close(parent)


def legacy_target(folder, media, category):
    """Where releases before 0.39.5 put this source: a folder of its own inside the
    library. Jellyfin read that folder as one TV show (with the real shows as its
    seasons), and the Arr apps as one movie, artist or show."""
    identity = hashlib.sha256((category + '\0' + str(folder.resolve())).encode()).hexdigest()[:16]
    return safe_path(Path(media) / 'library' / category / ('source-' + identity))


def import_target(folder, media, category):
    """The library folder itself (library/tv, say), checked not to overlap the source:
    each show, movie or artist folder of the source lands where Jellyfin, Navidrome and
    the Arr apps look for one. A file already there that differs is never replaced."""
    target = safe_path(Path(media) / 'library' / category)
    managed = target.resolve()
    here = folder.resolve()
    if here == managed or managed in here.parents or here in managed.parents:
        raise Said('This folder is inside the NekoServer library (or holds it), so it cannot be copied into it')
    return target


def move_legacy(folder, media, category, guard=None):
    """Move what an earlier release imported into this source's own folder up into the
    library, folder by folder (same drive: a rename, nothing copied). Names already in
    the library stay where they are; they are returned so the report can name them."""
    legacy = legacy_target(folder, media, category)
    if not legacy.is_dir():
        return []
    if guard:
        guard.check(legacy)
    left = []
    for item in sorted(legacy.iterdir()):
        if item.is_symlink():
            left.append(item.name)
            continue
        try:
            rename_no_replace(item, legacy.parent / item.name)
        except FileExistsError:
            left.append(item.name)
    if not left:
        legacy.rmdir()
    return left


SAMPLE = 1024 * 1024  # bytes of each piece same_samples compares

# Network filesystems (neko_common.NETWORK_FILESYSTEMS): their file times come from the
# NAS's own clock, so whether the source changed after its copy was made can't be told
# from them (see already_copied).


def same_samples(first, second, size):
    """Whether two files of the same `size` hold the same bytes at their start, middle and
    end (all of them when they are small). Used only for a source file that has not been
    written to since its copy was made (see already_copied): then a copy cut off and filled
    up, or another file put in its place, differs in one of these pieces."""
    places = sorted({0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)})
    try:
        with first.open('rb') as one, second.open('rb') as two:
            for place in places:
                one.seek(place)
                two.seek(place)
                if one.read(SAMPLE) != two.read(SAMPLE):
                    return False
    except OSError:
        return False
    return True


def same_clock(path, clocks):
    """Whether the file times of the filesystem at `path` come from this PC's clock (not
    a network share's). Asked once per drive: `clocks` keeps the answers."""
    drive = os.stat(path).st_dev
    if drive not in clocks:
        clocks[drive] = (findmnt('FSTYPE', path, '--target') or '') not in neko_common.NETWORK_FILESYSTEMS
    return clocks[drive]


def written_since_copied(want, have, clocks, current):
    """Whether the source file may have been written to after `have`, its copy, was made.
    Any write to a file moves its change time (ctime), even when a program puts its old
    date (mtime) back; the copy's change time is when it was made. On a network share the
    times come from another clock, so it always may have been."""
    return not same_clock(current.parent, clocks) or want.st_ctime_ns > have.st_ctime_ns


def already_copied(current, dest, apply=False, clocks=None):
    """True when `dest` is the copy of `current` an earlier run made; False when it is a
    different file (never replaced). Same size and date (copies keep their original's
    date), and the source not written to since the copy was made: three pieces of both are
    compared (same_samples), not the whole film again. Written to since (review of 0.51.3:
    a change outside those pieces was taken for "already imported"), or a copy from before
    0.39.5 (another date): the whole contents decide, and a match takes its original's
    date, so the next run doesn't read both again."""
    if dest.is_symlink() or not dest.is_file():
        return False  # a symlink there is never "the copy"
    have, want = dest.stat(), current.stat()
    if have.st_size != want.st_size:
        return False
    same_date = have.st_mtime_ns == want.st_mtime_ns
    if same_date and not written_since_copied(want, have, {} if clocks is None else clocks, current):
        return same_samples(current, dest, want.st_size)
    if digest(current) != digest(dest):
        return False
    if apply and not same_date:
        os.utime(dest, ns=(have.st_atime_ns, want.st_mtime_ns), follow_symlinks=False)
    return True


def planned_copies(folder, target, apply=False, conflicts=None, downloading=None, leftovers=None, links=None):
    """[(file, where it goes, its stat)] for every file not imported yet. Strict listing:
    a folder that cannot be read makes this source incomplete (nothing is copied),
    instead of silently leaving an album out. A different file already at a file's place
    in the library (another source's `folder.jpg`, one Sonarr replaced) is kept and the
    file left out: its path, relative to the source, goes into `conflicts`. A file still
    being downloaded (TEMPORARY) is left out too, its path in `downloading`; a download
    program's own leftover (LEFTOVERS) likewise, its path in `leftovers`.
    With `links` (a list), a file that has several names in the source (hard links, as
    download programs and the Arr apps make them) is planned once: each further name
    goes into `links` as (file, where it goes, its stat, where the first name goes), to
    be made a hard link in the library. Before, each name was counted for room and
    copied as a file of its own."""

    def unfinished(here, name):
        noted = leftovers if name.endswith(LEFTOVERS) else downloading if name.endswith(TEMPORARY) else None
        if not name.endswith(LEFTOVERS + TEMPORARY):
            return False
        if noted is not None:
            noted.append(shown_name((Path(here) / name).relative_to(folder)))
        return True

    try:
        found = strict_files(folder, unfinished)
    except UnreadableEntries as exc:
        raise IncompleteSource(
            [Path(p).relative_to(folder).as_posix() if Path(p).is_relative_to(folder) else '.' for p, _ in exc.problems]
        ) from None
    entries, first, stable, clocks = [], {}, {}, {}
    for found_path, _ in sorted(found):
        current = safe_path(found_path)
        dest = safe_path(target / current.relative_to(folder))
        # lstat: the file itself (strict_files found it a regular file, never a symlink).
        stat = current.lstat()
        same_file = several_names(stat, current, stable) if links is not None else None
        if dest.exists() or dest.is_symlink():
            if note_conflict(current, dest, folder, apply, conflicts, clocks) and same_file:
                first.setdefault(same_file, dest)  # copied by an earlier run: linked to as well
            continue
        if same_file in first:
            links.append((current, dest, stat, first[same_file]))
            continue
        if same_file:
            first[same_file] = dest
        entries.append((current, dest, stat))
    return entries


def several_names(stat, current, stable):
    """(drive, inode) of a file that has more than one name (hard links), else None.
    Only where inode numbers are kept (stable_inodes, asked once per drive: `stable`)."""
    if stat.st_nlink < 2:
        return None
    if stat.st_dev not in stable:
        stable[stat.st_dev] = stable_inodes(current.parent)
    return (stat.st_dev, stat.st_ino) if stable[stat.st_dev] else None


def note_conflict(current, dest, folder, apply, conflicts, clocks=None):
    """A file already at `dest`: an earlier copy of `current` (nothing to do: True), or a
    different file, kept and noted in `conflicts` (raised when no list is kept)."""
    if already_copied(current, dest, apply, clocks):
        return True
    if conflicts is None:
        raise RuntimeError('Existing imported file differs; refusing overwrite')
    conflicts.append(current.relative_to(folder).as_posix())
    return False


def link_copy(current, dest, stat, first, uid, gid, guard=None):
    """Make `dest` a hard link to `first`, the library's copy of the same source file
    under its first name, so the file is in the library once, as it is in the source.
    False when that can't be done (the first copy is not there, or the drive has no hard
    links): the caller then copies the file like any other."""
    now = current.lstat()
    if identity(now) != identity(stat) or (now.st_size, now.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise SourceChanged('Source changed during import')
    try:
        have = os.stat(first, follow_symlinks=False)
    except OSError:
        return False
    # Copies keep their original's size and date: anything else there is not that copy.
    if not S_ISREG(have.st_mode) or (have.st_size, have.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        return False
    if guard:
        guard.check(dest.parent)
    source = open_folder(first.parent)
    try:
        parent = open_folder(safe_path(dest.parent), 0o2775, uid, gid)
        try:
            if guard:
                guard.check_fd(parent)
            os.link(first.name, dest.name, src_dir_fd=source, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EMLINK):
                raise
            return False
        finally:
            os.close(parent)
    finally:
        os.close(source)
    return True


def hard_links_work(folder):
    """Whether the library drive at `folder` (or the nearest folder above it that is
    there) takes hard links: a file is made and linked there, and both are removed.
    False when that can't be tried (no right to write there, say), so room is never
    counted short."""
    place = Path(folder)
    while not place.is_dir() and place != place.parent:
        place = place.parent
    probe = place / f'.neko-link-test-{os.getpid()}'
    second = place / f'.neko-link-test-{os.getpid()}-2'
    try:
        os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
    except OSError:
        return False
    try:
        os.link(probe, second, follow_symlinks=False)
    except OSError:
        return False
    else:
        os.unlink(second)
        return True
    finally:
        os.unlink(probe)


def configured_folder(source):
    """The folder the source list names, whether or not it is there now."""
    value = source.get('mounted_path') if source.get('type') == 'remote' else source.get('path')
    return Path(value) if value and Path(value).is_absolute() else None


def legacy_left(left, folder, media, category):
    return {
        'status': 'needs_setup',
        'copied': 0,
        'reason': 'An earlier import of this source is in '
        + legacy_target(folder, media, category).name
        + ' in the library; these folders could not be moved up because the library already has a folder of that name: '
        + ', '.join(left[:10])
        + '. Compare and merge them by hand, then run setup again',
    }


def copy_source(source, media, apply=False, uid=None, gid=None, guard=None):
    category = source.get('category')
    if category not in ('movies', 'tv', 'music'):
        raise RuntimeError('Invalid media category')
    try:
        folder = source_folder(source)
    except SourceGone:
        earlier = while_unplugged(source, media, category, apply, guard)
        if earlier:
            return earlier
        raise
    if folder is None:
        return {'status': 'reference_only', 'copied': 0, 'reason': 'Configure this indexer through Prowlarr'}
    target = import_target(folder, media, category)
    left = move_legacy(folder, media, category, guard) if apply else []
    if left:
        return legacy_left(left, folder, media, category)
    conflicts, downloading, leftovers, links = [], [], [], []
    entries = planned_copies(folder, target, apply, conflicts, downloading, leftovers, links)
    # On a library drive that takes no hard links (exFAT, some NAS shares), each further
    # name is copied as a file of its own, so its room is counted too (review of 0.51.3:
    # it was counted once and said to take no room of its own).
    linkable = not links or hard_links_work(target)
    required = sum(stat.st_size for _, _, stat in entries)
    if not linkable:
        required += sum(stat.st_size for _, _, stat, _ in links)
    free = shutil.disk_usage(media).free
    if required > free:
        raise Said(
            # With how much is missing: rounded, both sizes could read "1.5 GB" (deep check 483).
            f'Nothing was copied: it needs {size_words(required)} and the library drive has {size_words(free)} free '
            f'({size_words(required - free)} too little). Free some room or take this folder out of the source list'
        )
    copied_again = []
    skipped = copy_entries(entries, folder, uid, gid, guard, links, copied_again) if apply else []
    copied = len(entries) + len(links) - len(skipped) if apply else 0
    result = {
        'status': 'copied' if apply else 'preview',
        'copied': copied,
        'new_files': len(entries) + len(links),
        'bytes': required,
        'destination': str(target),
    }
    result = with_downloading(with_skipped(with_conflicts(result, conflicts), skipped), downloading)
    if not apply and not linkable:
        copied_again = [stat for _, _, stat, _ in links]
    return with_links(with_leftovers(result, leftovers), links, copied_again)


def shown_name(path):
    """A file name as it can be printed and saved in the report, even one that is not
    UTF-8 (its odd bytes shown as \\xe9)."""
    return os.fsencode(str(path)).decode('utf-8', 'backslashreplace')


def copy_entries(entries, folder, uid, gid, guard, links=(), copied_again=None):
    """Copy each planned file, then give each further name of a file with several names
    (`links`, see planned_copies) its hard link; returns the ones skipped because their
    name could not be used (relative to the source), so one odd name never stops the
    whole source. A further name that could not be linked is copied, its stat added to
    `copied_again`; when the drive has no room left for that copy, the import stops with
    how much is missing."""
    skipped, stable = [], {}
    for current, dest, stat, first in [(*entry, None) for entry in entries] + list(links):
        if stat.st_dev not in stable:
            stable[stat.st_dev] = stable_inodes(current.parent)
        try:
            if first is None or not link_copy(current, dest, stat, first, uid, gid, guard):
                if first is not None:
                    room_for_one_more(first.parent, stat.st_size)
                    if copied_again is not None:
                        copied_again.append(stat)
                import_file(current, dest, stat, uid, gid, guard, stable[stat.st_dev])
        except UnicodeError:
            skipped.append(shown_name(current.relative_to(folder)))
        except SourceChanged:
            raise SourceChanged(shown_name(current.relative_to(folder))) from None
    return skipped


def room_for_one_more(folder, size):
    """Said (Said) when the library drive has no room for one more copy of `size` bytes:
    a further name the drive would not link, copied instead."""
    free = shutil.disk_usage(folder).free
    if size > free:
        raise Said(
            f'Copying stopped: the library drive does not take hard links, so a file the source holds under two '
            f'names needs {size_words(size)} more and the drive has {size_words(free)} free. The files before it '
            'were copied. Free some room, then copy again'
        )


def first_ten(names):
    """The first ten names, and how many more there are: a report row names at most ten."""
    more = f' and {len(names) - 10} more' if len(names) > 10 else ''
    return ', '.join(names[:10]) + more


def counted(names, one, many):
    """'1 file was ...' or '3 files were ...' (deep check 485: "1 files were")."""
    return neko_common.file_count(len(names)) + (f' {one}' if len(names) == 1 else f' {many}')


def with_skipped(result, skipped):
    """The report row, naming the files whose names could not be copied."""
    if skipped:
        result['skipped_names'] = len(skipped)
        said = (
            counted(skipped, 'was not copied because its name', 'were not copied because their names')
            + ' could not be used ('
            + ('rename it' if len(skipped) == 1 else 'rename them')
            + ', then copy again with --again): '
            + first_ten(skipped)
        )
        result['reason'] = result['reason'] + '. ' + said if result.get('reason') else said
    return result


def with_downloading(result, downloading):
    """The report row, naming the files left out because they were still being downloaded:
    the source counts as imported, so without this the finished download was never copied
    and nothing said so."""
    if downloading:
        result['still_downloading'] = len(downloading)
        said = (
            counted(
                downloading,
                'was still being downloaded (or written) and was not',
                'were still being downloaded (or written) and were not',
            )
            + ' copied: '
            + first_ten(downloading)
            + ('. When it has finished, copy it' if len(downloading) == 1 else '. When they have finished, copy them')
            + f' with  {AGAIN}  (in the NekoServer folder)'
        )
        result['reason'] = result['reason'] + '. ' + said if result.get('reason') else said
    return result


def with_leftovers(result, leftovers):
    """The report row, naming a download program's own files that were left out: they
    are no media, and there is nothing to do about them."""
    if leftovers:
        result['download_leftovers'] = len(leftovers)
        said = (
            "Left out, as a download program's own files and no films, shows or music (qBittorrent's .parts): "
            + first_ten(leftovers)
        )
        result['reason'] = result['reason'] + '. ' + said if result.get('reason') else said
    return result


def with_links(result, links, copied_again=()):
    """The report row, saying what became of files with several names: kept once (a link
    in the library, no room of its own), or copied again where the library drive takes no
    hard links (`copied_again`: their stats), with the room that takes."""
    if not links:
        return result
    result['hard_links'] = len(links) - len(copied_again)
    said = []
    if len(links) > len(copied_again):
        count = neko_common.file_count(len(links) - len(copied_again))
        said.append(
            f'Kept once in the library: {count} that the source holds under a second name (hard links, as download '
            'programs and Sonarr or Radarr make them). Each second name is a link there too and takes no room of its own'
        )
    if copied_again:
        result['copied_twice'] = len(copied_again)
        count = neko_common.file_count(len(copied_again))
        room = size_words(sum(stat.st_size for stat in copied_again))
        said.append(
            f'Copied again: {count} that the source holds under a second name, because the library drive does not '
            f'take hard links. Each second name is a copy of its own and takes room ({room} in all, counted above)'
        )
    for line in said:
        result['reason'] = result['reason'] + '. ' + line if result.get('reason') else line
    return result


def while_unplugged(source, media, category, apply, guard):
    """The row for a source that isn't there now but that a release before 0.39.5 (which
    kept no record) imported: its folder in the library moves up even with the source
    unplugged, and it was copied before. None when there was no earlier import."""
    named = configured_folder(source)
    if not named or not legacy_target(named, media, category).is_dir():
        return None
    left = move_legacy(named, media, category, guard) if apply else []
    if left:
        return legacy_left(left, named, media, category)
    return {
        'status': 'imported_earlier',
        'copied': 0,
        'reason': 'Source not connected now; its files were copied before',
    }


def with_conflicts(result, conflicts):
    """The report row, naming the files left out because the library has others there."""
    if conflicts:
        result['kept_library_version'] = len(conflicts)
        result['reason'] = (
            counted(conflicts, 'was', 'were')
            + ' left out because the library already has a different file at that place (kept as it is): '
            + first_ten(conflicts)
        )
    return result


def finished_imports(record_path):
    """{source key: category} of the sources whose import finished (empty when none or
    the record can't be read)."""
    try:
        finished = json.loads(record_path.read_text()).get('finished', {})
    except (OSError, ValueError, AttributeError):
        return {}
    return finished if isinstance(finished, dict) else {}


def save_finished(record_path, finished):
    record_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(record_path, {'format': 'neko-imported-sources-v1', 'finished': finished})


def gone(source, finished):
    """A source folder that isn't there: unplugged after its import finished is nothing
    left to do; never imported is a step left."""
    if source_key(source) in finished:
        return {
            'status': 'imported_earlier',
            'copied': 0,
            'reason': 'Source not connected now; its files were copied before',
        }
    return {
        'status': 'needs_setup',
        'reason': 'Source folder not found: connect the drive, or take it out of the source list',
    }


def not_mounted_yet(source, finished):
    """An automount share that did not connect: not "not found", since its folder is
    there; a finished import is still nothing left to do."""
    if source_key(source) in finished:
        return gone(source, finished)
    return {
        'status': 'needs_setup',
        'reason': 'The network share is not mounted yet: it did not connect when the import '
        'opened it. Check that the NAS is on and the share works (open the folder), then run setup again',
    }


def import_one(source, root, apply, owner, guard, finished, again=False):
    """One source copied into `root`'s library: its row for the report. A finished
    import is recorded in `finished`, and not done again unless asked (`again`): Sonarr,
    Radarr or Lidarr may since have renamed or replaced those files in the library, and a
    second copy would bring the originals back. DriveGone stops the whole import."""
    if source_key(source) in finished and not again:
        return {
            'status': 'imported_earlier',
            'copied': 0,
            'reason': f'Copied before; not copied again (to copy what is new in it, in the NekoServer folder: {AGAIN})',
        }
    try:
        result = copy_source(source, root, apply, *owner, guard)
    except NotMountedYet:
        return not_mounted_yet(source, finished)
    except SourceGone:
        return gone(source, finished)
    except SourceChanged as exc:
        return {
            'status': 'needs_setup',
            'reason': 'A file changed while it was being copied (another program may still be '
            'writing it): ' + str(exc) + '. The files before it were copied; run setup again when it is finished',
        }
    except Said as exc:
        return {'status': 'needs_setup', 'reason': str(exc)}
    except IncompleteSource as exc:
        # Only paths inside the source are reported, never the source's own location.
        return {
            'status': 'needs_setup',
            'reason': f'Nothing was copied: {len(exc.paths)} folders or files could not be read: '
            + ', '.join(exc.paths[:10]),
        }
    except (RuntimeError, OSError, KeyError, ValueError):
        return {
            'status': 'needs_setup',
            'reason': 'Source inaccessible, unmounted, overlapping, changed, unsafe or conflicting; inspect source before retry',
        }
    if apply and result['status'] in ('copied', 'imported_earlier'):
        finished[source_key(source)] = source.get('category')
    return result


def run(config, sources, apply=False, verify=None, again=False):
    media = neko_common.mount_path(config['media_mount'])
    data = neko_common.mount_path(config['data_mount'])
    if not os.path.ismount(media) or not os.path.ismount(data):
        raise RuntimeError('Mounted data SSD and media HDD required')
    uid, gid = int(config['media'].get('uid', 1000)), int(config['media'].get('gid', 1000))
    if apply and (uid <= 0 or gid <= 0):
        raise RuntimeError('Positive media UID/GID required')
    if apply and os.geteuid() != 0:
        raise RuntimeError('Use sudo to create media owned by the configured UID/GID')
    rows = []
    record_path = data / 'config' / 'imported-sources.json'
    finished = finished_imports(record_path)
    owner = (uid, gid) if apply else (None, None)
    # Music goes where setup put it (on the SSD or the media drive), where Navidrome and
    # Lidarr look; it always went to the SSD before. Films and shows go to the drive
    # their library is on (the media drive unless placed elsewhere, 0.50.0).
    music = neko_common.files_root(config, 'music')
    films = neko_common.files_root(config, 'movies')
    guards = {root: DriveGuard(root) for root in {data, media, music, films}} if apply else {}
    for index, source in enumerate(sources):
        root = music if source.get('category') == 'music' else films
        result = import_one(source, root, apply, owner, guards.get(root), finished, again)
        # Inventory may contain private URLs; reports never serialize source entries.
        rows.append(dict(source_number=index + 1, category=source.get('category'), **result))
    if verify:
        verify()
    if apply:
        save_finished(record_path, finished)
    atomic_json(data / 'reports' / 'source-import.json', {'apply': apply, 'sources': rows})
    return 1 if any(r['status'] == 'needs_setup' for r in rows) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=checked_settings, default=default_settings(Path(__file__).parent))
    parser.add_argument('--sources')
    parser.add_argument('--data-root')
    parser.add_argument('--media-root')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument(
        '--selected-only',
        action='store_true',
        help='Copy only inventory entries explicitly selected in the source wizard',
    )
    parser.add_argument(
        '--again', action='store_true', help='Copy what is new in sources copied before (they are skipped otherwise)'
    )
    args = parser.parse_args(argv)
    try:
        config = json.loads(safe_path(args.config).read_text())
        if args.data_root:
            config['data_mount'] = args.data_root
        if args.media_root:
            config['media_mount'] = args.media_root
        inventory = safe_path(args.sources or str(Path(config['data_mount']) / 'config' / 'media-sources.json'))
        sources = source_list(inventory)
        if args.selected_only:
            sources = [s for s in sources if s.get('copy_to_library') is True]
        if args.apply:
            verify_storage(config)
        result = run(
            config,
            sources,
            args.apply,
            verify=(lambda: verify_storage(config)) if args.apply else None,
            again=args.again,
        )
        if result:
            outcome = 'incomplete; check source-import report'
        elif args.apply:
            outcome = 'complete'
        else:
            outcome = 'preview complete'
        print('Source import:', outcome)
        return result
    except DriveGone as exc:
        print(
            'Source import stopped: '
            + str(exc)
            + '. Mount the drive again (or restart the computer), then run it again.',
            file=sys.stderr,
        )
        return 2
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print('Source import stopped: ' + stop_reason(exc) + '. Nothing was copied.', file=sys.stderr)
        return 2


def stop_reason(exc):
    """Why the import could not run, in words: a missing setting named as one (str() of a
    KeyError is the key alone), a system error with the file it was about."""
    if isinstance(exc, KeyError):
        return f'the setting {exc} is missing'
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror + (f' ({exc.filename})' if exc.filename else '')
    return str(exc) or type(exc).__name__


if __name__ == '__main__':
    sys.exit(main())
