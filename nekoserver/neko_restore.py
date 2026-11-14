#!/usr/bin/env python3
"""neko-restore: list, check and restore the files of one NekoBackup card, without
NekoServer, its catalogue, a network or the other cards of the set.

One file, Python 3.8 or newer, nothing to install. NekoServer writes a copy onto every
card it enrols (NekoBackup/tools/neko-restore.py) and ships it in its release folder
(neko_restore.py); its own restore uses this same file. Run from a card, it finds the
card it is on:

    python3 /media/me/CARD/NekoBackup/tools/neko-restore.py info
    python3 .../neko-restore.py list --search 2021/beach
    python3 .../neko-restore.py verify
    python3 .../neko-restore.py restore ~/restored --search 2021/beach
    python3 .../neko-restore.py where beach        (which card holds what, from the card's inventory)

With --card PATH it reads another card: PATH is the card's top folder or its NekoBackup
folder, mounted where Linux mounts a card (under /media, /run/media or /mnt); a folder
anywhere else is refused, links included. Read the card as root, or as the user who owns
its files.

The card's format is "archive format 1" (NekoServer's plans/0.51.0-archive-format-v1.md).
Everything on the card is treated as untrusted: every metadata file is read with its size
and nesting limits and every field checked, the same way NekoServer's engine reads it; a
path is never joined as text, folders are opened one by one without following links.
Checksums find accidental damage; they cannot prove a card was not changed on purpose.

Of several versions of one file, the newest gets the file's name; the older ones are listed
as "older" (with --rename they go under _renamed/ too). Restoring never overwrites anything
unless you name the file with --overwrite, or it is an older version this restore put there
itself (named in the --progress file and unchanged since). A file
whose place is taken (a file already there, a name that differs only in upper and lower
case, a name the destination cannot hold) is skipped, or with --rename put under
_renamed/<record id>.<ext>; the report lists each file and where it went. Every restored
file is checked against its record twice: the card's bytes while copying, the restored
file after writing. Only then does it get its name. Modification times are restored;
owners, permissions and extended attributes are not part of the format.

Exit status: 0 all well, 1 something was damaged, missing, skipped or not restored,
2 the card or the command could not be used.
"""

import argparse
import dataclasses
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time

ROOT_NAME = 'NekoBackup'
SMALL_FILE = 16384
SMALL_DEPTH = 4
HEADER_LIMIT = 1048576
HEADER_DEPTH = 5
LINE_LIMIT = 16384
MAX_LINES = 5000000
MAX_BYTES = 9007199254740991
CHUNK = 1 << 20
RENAMED = '_renamed'
THERE_ALREADY = '%s is there already'
# A file already at its place with exactly the record's bytes (an earlier restore's).
SAME_FILE = 'same file'
# Said after a restore that skipped files without --rename: those are still only on the
# card, and the summary's count alone does not say how to get them.
SKIPPED_HINT = (
    'The skipped files are still only on the card. To restore them too, run the same '
    'command again with --rename: each then goes under _renamed/ (add --report <new file> '
    'to see which name is which file).'
)
# Where Linux mounts a removable card: a card given with --card must be below one of these
# (or be the card this tool runs from).
CARD_ROOTS = ('/media/', '/run/media/', '/mnt/')
UNUSABLE = 'unusable %s: %s'
PROGRESS_FORMAT = 'neko-restore-progress'
PROGRESS_LIMIT = 1 << 30
PROGRESS_LINE = 65536

UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
TIME = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
NS = re.compile(r'^-?\d+$')
EXTENSION = re.compile(r'^[a-z0-9]{1,8}$')


class Invalid(Exception):
    """A metadata file or field that breaks format 1: it is ignored, never trusted."""


class Unusable(Exception):
    """The card or the command cannot be used at all."""


# ---- reading with bounds (the engine's format.rs, field for field) -------------------


def _no_constants(name):
    raise Invalid('not JSON: ' + name)


def depth(value):
    if isinstance(value, dict):
        return 1 + max((depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(v) for v in value), default=0)
    return 0


def parse_bounded(data, limit, max_depth):
    """One metadata object: UTF-8 JSON without a byte-order mark, an object, bounded."""
    if len(data) > limit:
        raise Invalid('metadata file too large')
    if data.startswith(b'\xef\xbb\xbf'):
        raise Invalid('metadata file has a byte-order mark')
    try:
        value = json.loads(data.decode('utf-8'), parse_constant=_no_constants)
    except (ValueError, RecursionError):  # a UnicodeDecodeError is a ValueError
        raise Invalid('metadata file is not JSON') from None
    if depth(value) > max_depth:
        raise Invalid('metadata file nested too deep')
    if not isinstance(value, dict):
        raise Invalid('metadata file is not an object')
    return value


def bad(field):
    return Invalid('field %s missing or out of bounds' % field)


def utf8(s):
    """A string that UTF-8 can hold (JSON's escapes can make lone surrogates)."""
    try:
        s.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


class Fields:
    def __init__(self, data):
        self.data = data

    def str(self, f):
        v = self.data.get(f)
        if not isinstance(v, str) or not utf8(v):
            raise bad(f)
        return v

    def checked(self, f, pattern):
        v = self.str(f)
        if not pattern.match(v):
            raise bad(f)
        return v

    def uuid(self, f):
        return self.checked(f, UUID)

    def opt_uuid(self, f):
        if f not in self.data:
            raise bad(f)
        return None if self.data[f] is None else self.uuid(f)

    def time(self, f):
        v = self.checked(f, TIME)
        try:
            time.strptime(v, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise bad(f) from None
        return v

    def text(self, f, most):
        v = self.str(f)
        if len(v.encode('utf-8')) > most or '\0' in v:
            raise bad(f)
        return v

    def ns(self, f):
        v = self.str(f)
        if len(v) > 20 or not NS.match(v):
            raise bad(f)
        n = int(v)
        if not -(2**63) <= n < 2**63:
            raise bad(f)
        return n

    def int(self, f, lo, hi):
        v = self.data.get(f)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise bad(f)
        return v

    def bytes(self, f):
        return self.int(f, 0, MAX_BYTES)

    def exact(self, f, want):
        if self.str(f) != want:
            raise bad(f)


def check_part(name):
    if name in ('', '.', '..') or '/' in name or '\0' in name or len(name.encode('utf-8')) > 255:
        raise Invalid('a path part is not allowed')


def check_relative_path(path):
    """Below a source root: parts split at /, none empty, . or .., no NUL, each at most
    255 bytes and the whole at most 4096."""
    if not path or len(path.encode('utf-8')) > 4096:
        raise Invalid('path empty or too long')
    for part in path.split('/'):
        check_part(part)


def unsigned_text(inner, name):
    s = inner.str(name)
    if not s or len(s) > 20 or not s.isascii() or not s.isdigit():
        raise bad(name)
    if int(s) >= 2**64:
        raise bad(name)
    return int(s)


def parse_identity(data):
    m = parse_bounded(data, SMALL_FILE, SMALL_DEPTH)
    f = Fields(m)
    f.exact('format', 'neko-backup-volume')
    version = m.get('archive_format_version')
    if version != 1 or isinstance(version, bool):
        raise Unusable('this card is archive format %r, not format 1: this tool does not read it' % (version,))
    f.int('format_minor', 0, 999)
    if m.get('encrypted') is not False:
        raise bad('encrypted')
    fs_type = f.str('filesystem_type')
    if fs_type not in ('ext4', 'exfat'):
        raise bad('filesystem_type')
    ident = {
        'backup_set_id': f.uuid('backup_set_id'),
        'volume_id': f.uuid('volume_id'),
        'sequence_number': f.int('sequence_number', 1, 9999),
        'set_display_name': f.text('set_display_name', 64),
        'created_at': f.time('created_at'),
        'filesystem_type': fs_type,
        'filesystem_uuid': f.text('filesystem_uuid', 64),
        'privacy_acknowledged_at': f.time('privacy_acknowledged_at'),
    }
    label = '%s - %03d' % (ident['set_display_name'], ident['sequence_number'])
    if not ident['set_display_name'] or f.str('volume_label') != label:
        raise bad('volume_label')
    ident['label'] = label
    return ident


def check_archive_path(record_id, archive):
    prefix = 'objects/%s/%s' % (record_id[:2], record_id)
    if not archive.startswith(prefix):
        raise bad('archive_relative_path')
    ext = archive[len(prefix) :]
    if ext and not (ext.startswith('.') and EXTENSION.match(ext[1:])):
        raise bad('archive_relative_path')


def parse_record(data):
    """A committed record, every field checked as the engine checks it."""
    m = parse_bounded(data, SMALL_FILE, SMALL_DEPTH)
    f = Fields(m)
    f.exact('completion_state', 'committed')
    f.int('schema_version', 1, 1)
    f.exact('checksum_algorithm', 'sha256')
    identity = m.get('source_identity')
    if not isinstance(identity, dict):
        raise bad('source_identity')
    inner = Fields(identity)
    rec = {
        'transaction_id': f.uuid('transaction_id'),
        'run_id': f.uuid('run_id'),
        'record_id': f.uuid('record_id'),
        'version_id': f.uuid('version_id'),
        'replaces_record_id': f.opt_uuid('replaces_record_id'),
        'backup_set_id': f.uuid('backup_set_id'),
        'volume_id': f.uuid('volume_id'),
        'source_id': f.uuid('source_id'),
        'original_relative_path': f.text('original_relative_path', 4096),
        'archive_relative_path': f.text('archive_relative_path', 64),
        'source_mtime_ns': f.ns('source_mtime_ns'),
        'source_identity': {
            'device': unsigned_text(inner, 'device'),
            'inode': unsigned_text(inner, 'inode'),
            'ctime_ns': inner.ns('ctime_ns'),
        },
    }
    check_relative_path(rec['original_relative_path'])
    check_archive_path(rec['record_id'], rec['archive_relative_path'])
    rec.update(
        source_display_name=f.text('source_display_name', 64),
        byte_size=f.bytes('byte_size'),
        checksum=f.checked('checksum', SHA256),
        source_observed_at=f.time('source_observed_at'),
        copy_verified_at=f.time('copy_verified_at'),
        committed_at=f.time('committed_at'),
    )
    if f.text('original_file_name', 255) != rec['original_relative_path'].rsplit('/', 1)[-1]:
        raise bad('original_file_name')
    return rec


# ---- the card, opened folder by folder ------------------------------------------------

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_dir(name, dir_fd=None):
    return os.open(name, DIR_FLAGS, dir_fd=dir_fd)


def read_limited(dir_fd, name, limit):
    fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Invalid('%s is not a plain file' % name)
        data = b''
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > limit:
        raise Invalid('%s is too large' % name)
    return data


def show(text):
    """A name as it can be printed: control characters escaped, nothing hidden."""
    out = []
    for c in text:
        if ord(c) < 32 or ord(c) == 127 or c == '\\':
            out.append(c.encode('unicode_escape').decode('ascii') if c != '\\' else '\\\\')
        else:
            out.append(c)
    return ''.join(out)


class Card:
    """One NekoBackup card, read only. Its top folder is opened once; everything below
    it is opened relative to an open folder, never through a link."""

    def __init__(self, path):
        # The card's folder with every link resolved, then held to the places Linux mounts
        # a removable card (or the card this tool runs from): only that folder is opened.
        top = os.path.realpath(path)
        if os.path.basename(top) == ROOT_NAME:
            top = os.path.dirname(top)
        own = default_card()
        # /mnt itself too (`sudo mount /dev/sdb1 /mnt` is a usual way to mount a card).
        if not (top == own or any((top + '/').startswith(root) for root in CARD_ROOTS)):
            raise Unusable(
                '%s is not where a card is mounted. Mount the card under /media or /mnt (as Linux does '
                'by itself), or run this tool from the card' % show(top)
            )
        try:
            self.top = open_dir(top)
            self.nb = open_dir(ROOT_NAME, self.top)
        except OSError as e:
            raise Unusable('no NekoBackup folder at %s (%s)' % (show(top), e.strerror)) from None
        self.path = top
        try:
            self.identity = parse_identity(read_limited(self.nb, 'identity.json', SMALL_FILE))
        except Invalid as e:
            raise Unusable('identity.json is not usable: %s' % e) from None
        except OSError as e:
            raise Unusable('identity.json cannot be read: %s' % e.strerror) from None

    def close(self):
        for fd in (self.nb, self.top):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sub(self, name):
        try:
            return open_dir(name, self.nb)
        except FileNotFoundError:
            return None

    def records(self):
        """Every record on the card: (record or None, where, why not)."""
        top = self._sub('records')
        if top is None:
            return
        try:
            for hh in sorted(os.listdir(top)):
                try:
                    d = open_dir(hh, top)
                except OSError as e:
                    yield None, 'records/' + show(hh), 'not a folder (%s)' % e.strerror
                    continue
                try:
                    for name in sorted(os.listdir(d)):
                        where = 'records/%s/%s' % (show(hh), show(name))
                        try:
                            rec = parse_record(read_limited(d, name, SMALL_FILE))
                        except (Invalid, OSError) as e:
                            yield None, where, str(e)
                            continue
                        problem = self._contradiction(rec, hh, name)
                        if problem:
                            yield None, where, problem
                        else:
                            yield rec, where, None
                finally:
                    os.close(d)
        finally:
            os.close(top)

    def _contradiction(self, rec, hh, name):
        if name != rec['record_id'] + '.json' or hh != rec['record_id'][:2]:
            return 'its name is not its record ID'
        if rec['volume_id'] != self.identity['volume_id'] or rec['backup_set_id'] != self.identity['backup_set_id']:
            return 'it names another card or set than this one'
        return None

    def open_payload(self, rec):
        """The archived file of `rec`, opened read only: a plain file, never a link."""
        objects = self._sub('objects')
        if objects is None:
            raise FileNotFoundError(errno.ENOENT, 'no objects folder')
        try:
            d = open_dir(rec['record_id'][:2], objects)
        finally:
            os.close(objects)
        try:
            fd = os.open(rec['archive_relative_path'].rsplit('/', 1)[-1], FILE_FLAGS, dir_fd=d)
        finally:
            os.close(d)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise Invalid('the archived file is not a plain file')
        return fd


def hash_fd(fd, limit):
    """(sha256, size) of an open file, reading at most limit + 1 bytes."""
    h = hashlib.sha256()
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(CHUNK, limit + 1 - size))
        if not chunk:
            break
        h.update(chunk)
        size += len(chunk)
    return h.hexdigest(), size


def check_copy(card, rec):
    """'ok', 'missing' or 'damaged': the archived file against its record."""
    try:
        fd = card.open_payload(rec)
    except FileNotFoundError:
        return 'missing'
    except (OSError, Invalid):
        return 'damaged'
    try:
        digest, size = hash_fd(fd, rec['byte_size'])
    except OSError:
        return 'damaged'
    finally:
        os.close(fd)
    return 'ok' if (digest, size) == (rec['checksum'], rec['byte_size']) else 'damaged'


def selected(card, search=None, record_ids=None):
    """The valid records chosen, and the unusable record files (always reported)."""
    chosen, unusable = [], []
    for rec, where, why in card.records():
        if rec is None:
            unusable.append((where, why))
        elif record_ids and rec['record_id'] not in record_ids:
            continue
        elif search and search not in rec['original_relative_path']:
            continue
        else:
            chosen.append(rec)
    chosen.sort(key=lambda r: (r['source_display_name'], r['original_relative_path'], r['committed_at']))
    return chosen, unusable


# ---- restoring --------------------------------------------------------------------------


class Taken(Exception):
    """The place of a restored file is taken, or the destination cannot hold its name."""


def safe_ext(name):
    stem, dot, ext = name.rpartition('.')
    ext = ext.lower()
    return '.' + ext if dot and stem and EXTENSION.match(ext) else ''


class Listing:
    """The names of each destination folder, folded for upper and lower case: listed once
    per restore and kept up to date as the restore adds names. Listing a folder again for
    every file made a restore of a folder with many thousand photos take hours."""

    def __init__(self):
        self.folded = {}

    def of(self, dir_fd):
        st = os.fstat(dir_fd)
        key = (st.st_dev, st.st_ino)
        if key not in self.folded:
            folded = {}
            for other in os.listdir(dir_fd):
                folded.setdefault(other.casefold(), set()).add(other)
            self.folded[key] = folded
        return self.folded[key]

    def add(self, dir_fd, name):
        self.of(dir_fd).setdefault(name.casefold(), set()).add(name)


def descend(dest_fd, parts, make=True, listing=None):
    """The folder of parts[:-1] below the open destination, opened one by one without
    following links, made when missing. Raises Taken when a link, a file or a name that
    differs only in case is in the way. The caller closes the returned descriptor."""
    fd = os.dup(dest_fd)
    try:
        for part in parts:
            clash(fd, part, listing)
            try:
                nxt = open_dir(part, fd)
            except FileNotFoundError:
                if not make:
                    raise Taken('its folder %s is not there' % show(part)) from None
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    os.fsync(fd)
                    if listing is not None:
                        listing.add(fd, part)
                except OSError as e:
                    raise Taken('the folder %s cannot be made here (%s)' % (show(part), e.strerror)) from None
                nxt = open_dir(part, fd)
            except OSError as e:
                raise Taken('%s is in the way (%s)' % (show(part), e.strerror)) from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def clash(dir_fd, name, listing=None):
    """Another entry whose name differs from `name` only in case: on a destination that
    ignores case they would be one file, so it counts as taken."""
    folded = name.casefold()
    if listing is None:
        others = [other for other in os.listdir(dir_fd) if other.casefold() == folded]
    else:
        others = sorted(listing.of(dir_fd).get(folded, ()))
    for other in others:
        if other != name:
            raise Taken('%s is there under the name %s' % (show(name), show(other)))


def exists(dir_fd, name):
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def write_checked(src, rec, dir_fd, temp):
    """The archived bytes into the new file `temp`, hashed on the way and fsynced; raises
    Invalid when they are not the record's."""
    out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
    try:
        h = hashlib.sha256()
        size = 0
        while size <= rec['byte_size']:
            chunk = os.read(src, CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(out, view) :]
        os.fsync(out)
    finally:
        os.close(out)
    if (h.hexdigest(), size) != (rec['checksum'], rec['byte_size']):
        raise Invalid('the archived file does not match its record (damaged on the card)')


def from_the_drive(fd):
    """The next reads of `fd` come from the drive, not from Linux's copy in memory: the
    file written out, then its cached pages dropped. Read back without this, a USB stick
    that does not keep what it is given (a fake-capacity one) passed the second check."""
    os.fsync(fd)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass  # a system without it: the check reads what Linux has, as before


def remove_leftover(dir_fd, temp):
    """Remove `temp` when an earlier restore left it: one stopped hard (killed, a power
    cut) while writing this file cannot remove it, and the next run then failed the file
    with "File exists". The name is this tool's own and never holds a verified file."""
    try:
        os.unlink(temp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def copy_verified(card, rec, dir_fd, temp):
    """The archived file copied into `temp` (new, in the destination folder) while it is
    hashed; then the written file read back and hashed. Raises Invalid on any mismatch,
    after removing `temp`: no unverified output is left."""
    # Said as verify says it: the card's copy missing, or damaged; the system's words ("No
    # such file or directory", "Too many levels of symbolic links") sounded like the
    # destination's fault.
    try:
        src = card.open_payload(rec)
    except FileNotFoundError:
        raise Invalid('its copy is missing from the card') from None
    except OSError:
        raise Invalid('its copy on the card is damaged (not a plain file)') from None
    try:
        remove_leftover(dir_fd, temp)
        write_checked(src, rec, dir_fd, temp)
        back = os.open(temp, FILE_FLAGS, dir_fd=dir_fd)
        try:
            from_the_drive(back)
            if hash_fd(back, rec['byte_size']) != (rec['checksum'], rec['byte_size']):
                raise Invalid('the restored file does not read back as written')
        finally:
            os.close(back)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(src)
    ns = rec['source_mtime_ns']
    try:
        os.utime(temp, ns=(ns, ns), dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        pass  # a destination without times: the file is still the right one


# A filesystem without links (FAT, exFAT) says one of these to os.link.
NO_LINKS = (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOTSUP)


def replace_named(dir_fd, temp, name):
    """`temp` over the plain file `name`, which the person named for overwriting."""
    if exists(dir_fd, name) and not stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
        raise Taken('%s is not a plain file: it is not overwritten' % show(name))
    os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def give_name(dir_fd, temp, name):
    """`temp` as `name`, which must not exist: a new link where the filesystem has links,
    else a check and a rename."""
    try:
        os.link(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        return
    except FileExistsError:
        raise Taken(THERE_ALREADY % show(name)) from None
    except OSError as e:
        if e.errno not in NO_LINKS:
            raise
    if exists(dir_fd, name):
        raise Taken(THERE_ALREADY % show(name))
    os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


# What a destination says to a name it cannot hold (":" or "?" on exFAT and FAT, say):
# EINVAL from Linux's own drivers, ENOENT from exFAT through FUSE.
NAME_REFUSED = (errno.EINVAL, errno.ENOENT, errno.EILSEQ, errno.ENAMETOOLONG)


def publish(dir_fd, temp, name, overwrite, listing=None):
    """`temp` under its final name. Never over an existing file unless `overwrite` (a
    plain file the person named). Raises Taken, with `temp` removed, when it is taken or
    the destination cannot hold the name."""
    try:
        try:
            if overwrite:
                replace_named(dir_fd, temp, name)
            else:
                give_name(dir_fd, temp, name)
        except OSError as e:
            if e.errno not in NAME_REFUSED or not exists(dir_fd, temp):
                raise
            raise Taken('this destination cannot hold the name %s' % show(name)) from None
        os.fsync(dir_fd)
        if listing is not None:
            listing.add(dir_fd, name)
    finally:
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


class Progress:
    """The versions already restored, kept in a file of the person's choice, so that a
    restore over several cards, or one that was interrupted, never copies a verified
    file twice and never reports a file as restored that was not.

    The file is one line of JSON for its header, then one line for each file restored,
    added and flushed to the disk as it is restored; the last line of a version counts.
    Rewriting the whole file for every file made a large restore take hours. A file of
    an earlier tool (one JSON object) is read, and written again in this form."""

    def __init__(self, path):
        self.path = path
        self.done = {}
        self.at = {}  # (source ID, original path) -> the version IDs restored of that file
        self.out = None
        if os.path.exists(path):
            try:
                with open(path, 'rb') as f:
                    data = f.read(PROGRESS_LIMIT + 1)
            except OSError as e:
                raise Unusable('the progress file %s is not usable: %s' % (path, e.strerror)) from None
            if len(data) > PROGRESS_LIMIT:
                raise Unusable('the progress file %s is too large' % path)
            if self._read(data):
                self._open('a')
                return
        self._open('w')

    def _read(self, data):
        """Read the file; True when it is in this tool's form and can be added to."""
        lines = data.split(b'\n')
        try:
            header = parse_bounded(lines[0], PROGRESS_LINE, 1)
        except Invalid:
            header = None
        if header is not None and header.get('format') == PROGRESS_FORMAT and header.get('version') == 2:
            for n, raw in enumerate(lines[1:], 2):
                if raw:
                    self._line(raw, last=n == len(lines))
            return True
        try:
            old = parse_bounded(data, PROGRESS_LIMIT, 3)
        except Invalid as e:
            raise Unusable('the progress file %s is not usable: %s' % (self.path, e)) from None
        if old.get('format') != PROGRESS_FORMAT or not isinstance(old.get('done'), dict):
            raise Unusable('%s is not a restore progress file' % self.path)
        for version_id, entry in old['done'].items():
            if isinstance(entry, dict):
                self._note(version_id, entry)
        return False

    def _line(self, raw, last):
        try:
            entry = parse_bounded(raw, PROGRESS_LINE, 1)
        except Invalid:
            if last:
                return  # cut off while it was written (a stop, a power cut): not counted
            raise Unusable('the progress file %s has a line that is not usable' % self.path) from None
        if isinstance(entry.get('version_id'), str):
            self._note(entry['version_id'], entry)

    def _note(self, version_id, entry):
        self.done[version_id] = entry
        if isinstance(entry.get('original_path'), str):
            self.at.setdefault((entry.get('source_id'), entry['original_path']), set()).add(version_id)

    def _open(self, mode):
        """The file opened to add lines; a new file (or one of an earlier tool) starts
        with the header and the entries known so far, written to a new file first."""
        if mode == 'a':
            self.out = open(self.path, 'a', encoding='utf-8')  # noqa: SIM115 (closed by close())
            return
        temp = self.path + '.new'
        with open(temp, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'format': PROGRESS_FORMAT, 'version': 2}) + '\n')
            for version_id, entry in self.done.items():
                f.write(json.dumps(dict(entry, version_id=version_id), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path)
        self.out = open(self.path, 'a', encoding='utf-8')  # noqa: SIM115 (closed by close())

    def close(self):
        if self.out is not None:
            self.out.close()
            self.out = None

    def _entry(self, version_id):
        """A usable entry: its path below the destination, never an absolute one."""
        entry = self.done.get(version_id)
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or entry.get('superseded_by'):
            return None
        try:
            check_relative_path(entry['path'])
        except Invalid:
            return None
        return entry

    def has(self, dest_fd, rec):
        entry = self._entry(rec['version_id'])
        if entry is None:
            return False
        try:
            st = os.stat(entry['path'], dir_fd=dest_fd, follow_symlinks=False)
        except (OSError, ValueError):
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == rec['byte_size']

    def at_original(self, rec):
        """(version ID, entry) of another version of rec's file restored under its own name
        by an earlier run (from another card, say), or None."""
        for version_id in sorted(self.at.get((rec['source_id'], rec['original_relative_path']), ())):
            entry = self._entry(version_id)
            if version_id != rec['version_id'] and entry is not None and entry['path'] == rec['original_relative_path']:
                return version_id, entry
        return None

    def _write(self, version_id, entry):
        self._note(version_id, entry)
        self.out.write(json.dumps(dict(entry, version_id=version_id), ensure_ascii=False) + '\n')
        self.out.flush()
        os.fsync(self.out.fileno())

    def supersede(self, old_version_id, rec):
        self._write(old_version_id, dict(self.done[old_version_id], superseded_by=rec['version_id']))

    def add(self, rec, where):
        self._write(
            rec['version_id'],
            {
                'path': where,
                'sha256': rec['checksum'],
                'record_id': rec['record_id'],
                'source_id': rec['source_id'],
                'original_path': rec['original_relative_path'],
                'source_mtime_ns': rec['source_mtime_ns'],
                'committed_at': rec['committed_at'],
            },
        )


def same_file(dir_fd, name, rec):
    """The plain file `name` holds exactly the bytes of `rec` (its size, then its checksum)."""
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != rec['byte_size']:
            return False
        return hash_fd(fd, rec['byte_size']) == (rec['checksum'], rec['byte_size'])
    except OSError:
        return False
    finally:
        os.close(fd)


def restore_one(card, rec, dest_fd, rename, overwrite, listing=None):
    """One record restored below the open destination. Returns (outcome, where):
    outcome is restored, same file (exactly this file is there already), renamed, skipped or failed."""
    parts = rec['original_relative_path'].split('/')
    temp = '.neko-restore-%s.part' % rec['record_id']
    target = rec['original_relative_path']
    try:
        d = descend(dest_fd, parts[:-1], listing=listing)
        try:
            clash(d, parts[-1], listing)
            named = target in overwrite
            if exists(d, parts[-1]) and not named:
                if same_file(d, parts[-1], rec):
                    return SAME_FILE, target  # restored before (by a run without --progress)
                raise Taken('a file is there already')
            copy_verified(card, rec, d, temp)
            publish(d, temp, parts[-1], named, listing)
        finally:
            os.close(d)
        return 'restored', target
    except Taken as why:
        if not rename:
            return 'skipped', str(why)
    except Invalid as why:
        return 'failed', str(why)
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENAMETOOLONG, errno.EILSEQ) or not rename:
            return 'failed', '%s (%s)' % (e.strerror, show(target))
    # The destination cannot take it under its own name: _renamed/<record id><.ext>.
    return renamed_copy(card, rec, dest_fd, temp, listing)


def renamed_copy(card, rec, dest_fd, temp, listing=None):
    """`rec` restored as _renamed/<record id><.ext>."""
    other = rec['record_id'] + safe_ext(rec['original_relative_path'].rsplit('/', 1)[-1])
    try:
        d = descend(dest_fd, [RENAMED], listing=listing)
        try:
            if exists(d, other):
                if same_file(d, other, rec):
                    return SAME_FILE, '%s/%s' % (RENAMED, other)
                return 'skipped', THERE_ALREADY % (RENAMED + '/' + other)
            copy_verified(card, rec, d, temp)
            publish(d, temp, other, False, listing)
        finally:
            os.close(d)
    except (Taken, Invalid) as why:
        return 'failed', str(why)
    except OSError as e:
        return 'failed', e.strerror
    return 'renamed', '%s/%s' % (RENAMED, other)


# What became of a record that leaves the file in place: no failure.
KEPT = ('restored', 'renamed', 'done before', SAME_FILE)
FINE = KEPT + ('older', 'same version')


def age(rec):
    """How new a version is: the source file's modification time when it was copied, then
    when its copy was committed. A new copy of an old version (its first copy was damaged)
    keeps that version's age."""
    return rec['source_mtime_ns'], rec['committed_at']


def entry_age(entry):
    """The age of a version a progress file names, or None (a file of an earlier tool)."""
    mtime, committed = entry.get('source_mtime_ns'), entry.get('committed_at')
    if isinstance(mtime, int) and not isinstance(mtime, bool) and isinstance(committed, str):
        return mtime, committed
    return None


def by_file(records):
    """The records grouped by the file they are a version of, the newest version first;
    the groups in the order of the records."""
    groups = {}
    for rec in records:
        groups.setdefault((rec['source_id'], rec['original_relative_path']), []).append(rec)
    for recs in groups.values():
        recs.sort(key=age, reverse=True)
    return list(groups.values())


def holds(dest_fd, path, checksum):
    """Whether the plain file at `path` below the destination has exactly that checksum."""
    parts = path.split('/')
    try:
        d = descend(dest_fd, parts[:-1], make=False)
    except (Taken, OSError):
        return False
    try:
        fd = os.open(parts[-1], FILE_FLAGS, dir_fd=d)
    except OSError:
        return False
    finally:
        os.close(d)
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode) and hash_fd(fd, MAX_BYTES)[0] == checksum
    except OSError:
        return False
    finally:
        os.close(fd)


@dataclasses.dataclass(frozen=True)
class Choices:
    """What the person chose for one restore, handed to each file's steps together."""

    rename: bool  # --rename: a file whose place is taken goes under _renamed/
    overwrite: frozenset  # --overwrite: the paths the person named to be replaced
    progress: object  # the Progress file (--progress), or None
    listing: Listing  # the names in each destination folder, listed once


def older(card, rec, dest_fd, choices, newer_where):
    """A version older than the one restored under the file's name: kept under _renamed/
    with --rename, else named and left on the card."""
    if choices.rename:
        return renamed_copy(card, rec, dest_fd, '.neko-restore-%s.part' % rec['record_id'], choices.listing)
    return 'older', 'a newer version is restored as %s' % newer_where


def one_version(card, rec, dest_fd, choices, chosen):
    """One record of a file, after the newer versions of it were handled. `chosen` is the
    (record, where) restored for that file in this run, or None."""
    progress = choices.progress
    if progress is not None and progress.has(dest_fd, rec):
        return 'done before', progress.done[rec['version_id']]['path']
    if chosen is not None:
        chosen_rec, chosen_where = chosen
        if chosen_rec['version_id'] == rec['version_id']:
            return 'same version', chosen_where
        return older(card, rec, dest_fd, choices, chosen_where)
    # A version of this file an earlier run restored under its name (from another card).
    earlier = progress.at_original(rec) if progress is not None else None
    replacing_ours = False
    if earlier is not None and entry_age(earlier[1]) is not None:
        earlier_version, earlier_entry = earlier
        if entry_age(earlier_entry) > age(rec):
            return older(card, rec, dest_fd, choices, earlier_entry['path'])
        # An older version this restore put there itself, unchanged since: replaced.
        replacing_ours = holds(dest_fd, earlier_entry['path'], earlier_entry.get('sha256'))
    overwrite = choices.overwrite | {rec['original_relative_path']} if replacing_ours else choices.overwrite
    outcome, where = restore_one(card, rec, dest_fd, choices.rename, overwrite, choices.listing)
    if outcome == 'restored' and replacing_ours:
        progress.supersede(earlier_version, rec)
    return outcome, where


def inside(dir_fd, top_fd):
    """Whether the open folder `dir_fd` is `top_fd` or below it, by device and inode,
    walking up through ".." to the root (a link or another name of the card counts too)."""
    want = os.fstat(top_fd)
    want = (want.st_dev, want.st_ino)
    fd = os.dup(dir_fd)
    try:
        seen = None
        while True:
            st = os.fstat(fd)
            here = (st.st_dev, st.st_ino)
            if here == want:
                return True
            if here == seen:
                return False  # the root: its ".." is itself
            seen = here
            up = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = up
    finally:
        os.close(fd)


def restore(card, records, destination, rename=False, overwrite=(), progress=None, say=print):
    """Restore `records` into `destination` (an existing folder). Of several versions of
    one file, the newest gets the file's name. Returns the report: one row per record with
    its original path, what happened and where it went."""
    try:
        dest_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as e:
        raise Unusable('the destination %s cannot be opened (%s)' % (show(destination), e.strerror)) from None
    # Never onto the card itself: it is the backup, maybe the last copy of these files.
    if inside(dest_fd, card.top):
        os.close(dest_fd)
        raise Unusable(
            '%s is on the card itself. Restore onto another drive: the card is your backup, and '
            'is kept as it is' % show(destination)
        )
    report = []
    choices = Choices(rename, frozenset(overwrite), progress, Listing())
    try:
        for recs in by_file(records):
            chosen = None
            for rec in recs:
                outcome, where = one_version(card, rec, dest_fd, choices, chosen)
                if outcome in ('restored', 'renamed', SAME_FILE) and progress is not None:
                    progress.add(rec, where)
                if chosen is None and outcome in KEPT:
                    chosen = (rec, where)
                report.append(
                    {
                        'record_id': rec['record_id'],
                        'version_id': rec['version_id'],
                        'original_path': rec['original_relative_path'],
                        'source': rec['source_display_name'],
                        'outcome': outcome,
                        'where': where,
                    }
                )
                say('%-11s %s -> %s' % (outcome, show(rec['original_relative_path']), show(where)))
    finally:
        os.close(dest_fd)
    return report


# ---- the card's inventory: what the other cards held when this one was written ----------


def inventory_snapshots(card):
    """The valid snapshot headers on the card ((header, versions file name)), and the
    ones refused with the reason."""
    inv = card._sub('inventory')
    found, refused = [], []
    if inv is None:
        return found, refused, None
    for name in sorted(os.listdir(inv)):
        if not name.endswith('.json') or not UUID.match(name[:-5]):
            continue
        try:
            found.append(checked_snapshot(card, inv, name))
        except (Invalid, OSError) as e:
            refused.append((name, str(e)))
    return found, refused, inv


def checked_snapshot(card, inv, name):
    """One snapshot header, checked against this card and against its version list
    (size, line count and checksum); raises Invalid when they disagree."""
    header = parse_bounded(read_limited(inv, name, HEADER_LIMIT), HEADER_LIMIT, HEADER_DEPTH)
    f = Fields(header)
    f.int('schema_version', 1, 1)
    if f.uuid('snapshot_id') != name[:-5]:
        raise Invalid('its name is not its snapshot ID')
    if f.uuid('written_to_volume_id') != card.identity['volume_id']:
        raise Invalid('it names another card')
    vf = header.get('versions_file')
    if not isinstance(vf, dict):
        raise bad('versions_file')
    vff = Fields(vf)
    if vff.str('name') != name[:-5] + '.versions.jsonl':
        raise bad('versions_file')
    want = (vff.checked('sha256', SHA256), vff.bytes('byte_size'), vff.int('lines', 0, MAX_LINES))
    if list_facts(inv, vff.str('name'), want[1]) != want:
        raise Invalid('its version list does not match its header')
    f.int('revision', 1, MAX_BYTES)
    f.uuid('catalogue_epoch_id')
    f.time('exported_at')
    return header


def list_facts(inv, name, size_named):
    """(sha256, size, line count) of a version list, reading at most size_named + 1 bytes."""
    fd = os.open(name, FILE_FLAGS, dir_fd=inv)
    try:
        digest, size = hash_fd(fd, size_named)
        os.lseek(fd, 0, os.SEEK_SET)
        lines = 0
        read = 0
        while read <= size_named:
            chunk = os.read(fd, min(CHUNK, size_named + 1 - read))
            if not chunk:
                break
            read += len(chunk)
            lines += chunk.count(b'\n')
    finally:
        os.close(fd)
    return digest, size, lines


def where(card, text, say=print):
    """Which card of the set held each version whose path holds `text`, as the newest
    inventory on this card knew it. It may be out of date: a card written later knows more."""
    found, refused, inv = inventory_snapshots(card)
    for name, why in refused:
        say('inventory %s refused: %s' % (name, why))
    if not found:
        say('This card has no usable inventory: only its own files are known (use list).')
        if inv is not None:
            os.close(inv)
        return 1
    try:
        header = max(found, key=lambda h: (h['exported_at'], h['revision']))
        labels = card_labels(header)
        say(
            'From the inventory of %s, revision %s: a card written later may know more.'
            % (header['exported_at'], header['revision'])
        )
        fd = os.open(header['versions_file']['name'], FILE_FLAGS, dir_fd=inv)
        with os.fdopen(fd, 'rb') as f:
            for raw in iter(lambda: f.readline(LINE_LIMIT + 2), b''):
                if len(raw) > LINE_LIMIT + 1:
                    say('a line of the inventory is too long: stopped')
                    return 1
                said = version_said(raw, text, labels)
                if said:
                    say(said)
    finally:
        os.close(inv)
    return 0


def card_labels(header):
    """{volume ID: 'Set - NNN (state)'} for the cards a snapshot knows."""
    labels = {}
    for v in header.get('volumes', []):
        if not (isinstance(v, dict) and isinstance(v.get('volume_id'), str)):
            continue
        seq = v.get('sequence_number')
        labels[v['volume_id']] = '%s - %03d (%s)' % (
            show(str(header.get('set_display_name', ''))),
            seq if isinstance(seq, int) else 0,
            show(str(v.get('state', ''))),
        )
    return labels


def version_said(raw, text, labels):
    """One version line of an inventory as a line for a person, or None when its path
    does not hold `text` or the line is not usable."""
    try:
        line = parse_bounded(raw.rstrip(b'\n'), LINE_LIMIT, 3)
        path = Fields(line).text('path', 4096)
        check_relative_path(path)
    except Invalid:
        return None
    if text not in path:
        return None
    copies = line.get('copies') if isinstance(line.get('copies'), list) else []
    held = ', '.join(
        '%s: %s' % (labels.get(c.get('volume_id'), 'an unknown card'), show(str(c.get('state'))))
        for c in copies
        if isinstance(c, dict)
    )
    return '%s (%s bytes): %s' % (show(path), line.get('byte_size'), held or 'no copy known')


# ---- the commands ---------------------------------------------------------------------


def default_card():
    """The card this file is on, when it is run from NekoBackup/tools/."""
    here = os.path.dirname(os.path.realpath(__file__))
    if os.path.basename(here) == 'tools' and os.path.basename(os.path.dirname(here)) == ROOT_NAME:
        return os.path.dirname(os.path.dirname(here))
    return None


def cmd_info(card, args, say):
    i = card.identity
    good = bad_records = 0
    for rec, _, _ in card.records():
        if rec is None:
            bad_records += 1
        else:
            good += 1
    say('Card:        %s' % show(i['label']))
    say('Set:         %s' % i['backup_set_id'])
    say('Card ID:     %s' % i['volume_id'])
    say('Enrolled:    %s (%s)' % (i['created_at'], i['filesystem_type']))
    say('Files:       %d' % good)
    if bad_records:
        say('Unusable record files: %d (list shows them)' % bad_records)
    say('Not encrypted: anyone holding this card can read it.')
    return 0


def cmd_list(card, args, say):
    records, unusable = selected(card, args.search, args.record)
    for rec in records:
        say(
            '%s  %12d  %s  %s: %s'
            % (
                rec['record_id'],
                rec['byte_size'],
                rec['committed_at'],
                show(rec['source_display_name']),
                show(rec['original_relative_path']),
            )
        )
    for where_, why in unusable:
        say(UNUSABLE % (where_, why))
    say('%d file(s)' % len(records))
    return 1 if unusable else 0


def cmd_verify(card, args, say):
    records, unusable = selected(card, args.search, args.record)
    counts = {'ok': 0, 'missing': 0, 'damaged': 0}
    for rec in records:
        result = check_copy(card, rec)
        counts[result] += 1
        if result != 'ok':
            say('%-8s %s' % (result, show(rec['original_relative_path'])))
    for where_, why in unusable:
        say(UNUSABLE % (where_, why))
    say('ok %(ok)d, damaged %(damaged)d, missing %(missing)d' % counts + ', unusable records %d' % len(unusable))
    return 0 if counts['ok'] == len(records) and not unusable else 1


def cmd_restore(card, args, say):
    if args.report and os.path.lexists(args.report):
        raise Unusable('the report file %s is there already: name a new one' % show(args.report))
    records, unusable = selected(card, args.search, args.record)
    if not records:
        say('Nothing on this card matches.')
        return 1
    progress = Progress(args.progress) if args.progress else None
    try:
        report = restore(card, records, args.destination, args.rename, args.overwrite or (), progress, say)
    finally:
        if progress is not None:
            progress.close()
    if args.report:
        with open(args.report, 'x', encoding='utf-8') as f:
            json.dump({'card': card.identity['label'], 'restored_into': args.destination, 'files': report}, f, indent=1)
            f.write('\n')
    counts = {}
    for row in report:
        counts[row['outcome']] = counts.get(row['outcome'], 0) + 1
    say(', '.join('%s %d' % kv for kv in sorted(counts.items())))
    if counts.get('skipped') and not args.rename:
        say(SKIPPED_HINT)
    for where_, why in unusable:
        say(UNUSABLE % (where_, why))
    fine = all(row['outcome'] in FINE for row in report)
    return 0 if fine and not unusable else 1


def cmd_where(card, args, say):
    return where(card, args.text, say)


def main(argv=None, say=print):
    parser = argparse.ArgumentParser(prog='neko-restore', description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--card',
        help='the card: its top folder or its NekoBackup folder, mounted under /media, /run/media or /mnt '
        '(without --card: the card this tool is on)',
    )
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='what this card is')
    for name, helptext in (('list', 'the files on this card'), ('verify', 'check every file against its record')):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('--search', help='only paths that contain this text')
        p.add_argument('--record', action='append', help='only this record ID (may be repeated)')
    p = sub.add_parser('restore', help='restore files into a folder')
    p.add_argument('destination', help='an existing folder')
    p.add_argument('--search', help='only paths that contain this text')
    p.add_argument('--record', action='append', help='only this record ID (may be repeated)')
    p.add_argument('--rename', action='store_true', help='a file whose place is taken goes under _renamed/')
    p.add_argument('--overwrite', action='append', help='overwrite this one file (its original path); may be repeated')
    p.add_argument('--progress', help='a file that remembers what was restored (for several cards, or after a stop)')
    p.add_argument('--report', help='write the restore report (JSON) into this new file')
    p = sub.add_parser('where', help='which card held a file, from this card\'s inventory')
    p.add_argument('text', help='a part of the path')
    args = parser.parse_args(argv)
    path = args.card or default_card()
    if not path:
        say('Name the card with --card (its top folder), or run this tool from the card.')
        return 2
    commands = {'info': cmd_info, 'list': cmd_list, 'verify': cmd_verify, 'restore': cmd_restore, 'where': cmd_where}
    try:
        with Card(path) as card:
            return commands[args.command](card, args, say)
    except Unusable as e:
        say('Cannot use this: %s' % e)
        return 2
    except KeyboardInterrupt:
        say('Stopped. Files restored so far are complete; a file that was being written was removed.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
