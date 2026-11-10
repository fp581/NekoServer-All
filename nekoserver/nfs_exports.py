"""A folder a network storage box (a NAS) shares over NFS, used as NekoServer's media
drive or its backup drive. network_storage.py finds the shared folders and asks which
one is which; this file holds what an NFS folder is to NekoServer.

NFS is not a disk. The NAS keeps the filesystem and this PC sees its files over the
network, so there is no device, nothing to format and no UUID:
- A shared folder is named by its address, "192.168.1.20:/volume1/media" (the NAS, then
  the folder as the NAS calls it). Where the rest of NekoServer names a drive by its
  UUID (the drive checks, the dashboard, each backup's records), a shared folder gets
  `identity(address)`: "nfs-" and the start of the address's SHA-256. The runtime works
  it out the same way from what is mounted (common.rs, nfs_identity).
- Only the media drive and the backup drive may be one. The application SSD (the apps'
  settings and databases) never is: a database needs a disk that does not go away.
- Before a shared folder is used it is tried out (`tried`): mounted privately, then
  written to and read back as root and as the apps' user, with the owner, the
  permissions and a file lock checked, and its free space read. A NAS set up in a way
  that would lose who owns a file, or that has no locks, is refused with what to change
  on the NAS.
- The media drive is mounted at every start once the network is up, and again by
  itself the first time it is used after the NAS was away (a systemd automount), so a
  NAS that is off never keeps this PC from starting. A write waits for a NAS that
  stopped answering (`hard`) instead of failing half-way.

NFS is slower than a drive connected to this PC, and than an iSCSI disk."""

import errno
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

TRANSPORT = 'nfs'
PORT = 2049  # NFS's port
# How Linux names a mounted shared folder: NFS version 4, or version 3.
FILESYSTEMS = ('nfs4', 'nfs')
# A shared folder as the NAS names it: /volume1/media. No spaces (a line of /etc/fstab
# cannot hold one plainly) and no "..".
FOLDER = re.compile(r'(/[A-Za-z0-9_+@~-][A-Za-z0-9_.+@~-]*)+')
HOST = re.compile(r'(\d{1,3}(\.\d{1,3}){3}|[A-Za-z0-9][A-Za-z0-9.-]{0,252})')

SLOWER = (
    'A folder shared over NFS is slower than a drive connected to this PC, and than an iSCSI disk on the same '
    'NAS: fine for films, music and backups, and the reason the apps and their databases stay on the SSD.'
)


def address(host, folder):
    """host and /folder as one address ("192.168.1.20:/volume1/media"), or None when
    either is not one NekoServer can use."""
    host, folder = str(host).strip(), str(folder).strip().rstrip('/')
    if not HOST.fullmatch(host) or not FOLDER.fullmatch(folder):
        return None
    return f'{host}:{folder}'


def parts(export):
    """(host, folder) of a shared folder's address; RuntimeError when it is not one."""
    host, _, folder = str(export).partition(':')
    if address(host, folder) != export:
        raise RuntimeError(f'Not the address of a folder shared over NFS (NAS:/folder): {export}')
    return host, folder


def identity(export):
    """What stands in for a drive's UUID: "nfs-" and 32 hex digits made from the
    address. The same address always gives the same one, here and in the runtime."""
    return 'nfs-' + hashlib.sha256(export.encode()).hexdigest()[:32]


def export_of(record):
    """The address of the shared folder a drive's saved record names (`network`:
    {"transport": "nfs", "export": ...}), or None for a drive that is not one."""
    network = record.get('network') if isinstance(record, dict) else None
    if not isinstance(network, dict) or network.get('transport') != TRANSPORT:
        return None
    return network.get('export')


def checked_record(record, what):
    """The address of the shared folder `record` names, with the record checked whole:
    its identity is the one made from the address and its type one NFS has. None for a
    drive that is not a shared folder. `what`: the setting, for the message."""
    export = export_of(record)
    if export is None:
        return None
    parts(export)
    if record.get('uuid') != identity(export) or record.get('fstype') not in FILESYSTEMS:
        raise RuntimeError(
            f'{what} names the shared folder {export}, but its "uuid" or "fstype" was changed by hand. Take '
            f'{what} out of my-config.json and run  sudo bash install.sh  again: setup asks for the folder anew.'
        )
    return export


def record_for(export, fstype):
    """What the settings keep for a shared folder used as a drive."""
    return {'uuid': identity(export), 'fstype': fstype, 'network': {'transport': TRANSPORT, 'export': export}}


# --- Mounting -----------------------------------------------------------------------

# hard: a write waits for a NAS that stopped answering, and goes on when it is back
# (with soft it would fail half-way, and a backup or a download would keep a cut-off
# file). nodev, nosuid: nothing on a shared folder is a device or runs as root.
OPTIONS = 'hard,nodev,nosuid'
# In /etc/fstab: mounted once the network is up (_netdev), never holding up the start
# (nofail), and mounted when first used after the NAS was away (automount); an attempt
# gives up after 30 seconds instead of making whatever asked wait for minutes.
AT_START = '_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30s'


def options(fstype):
    """The mount options of a shared folder mounted as `fstype` (nfs4, or nfs for
    version 3): the version it was tried out with, so it is the same one at every start."""
    return f"nfsvers={'3' if fstype == 'nfs' else '4'},{OPTIONS}"


def fstab_line(export, mountpoint, fstype):
    """The line that mounts the media drive's shared folder at every start."""
    if any(c.isspace() for c in mountpoint):
        raise RuntimeError('Mountpoint cannot contain whitespace')
    return f'{export} {mountpoint} nfs {options(fstype)},{AT_START} 0 0'


def own_line(fields):
    """These fields of an /etc/fstab line are one NekoServer wrote for a shared folder."""
    return (
        len(fields) == 6
        and fields[2] == 'nfs'
        and fields[4:] == ['0', '0']
        and fields[3] in tuple(f'{options(fstype)},{AT_START}' for fstype in FILESYSTEMS)
    )


def mounted(found):
    """(identity, type) of a mounted shared folder from findmnt's "FSTYPE SOURCE" (its
    UUID column is empty for one), or None when what is mounted is not a shared folder."""
    fields = (found or '').split()
    if len(fields) == 2 and fields[0] in FILESYSTEMS:
        return identity(fields[1]), fields[0]
    return None


# --- Finding the shared folders -------------------------------------------------------


def run_tool(argv, timeout=60, run=subprocess.run):
    return run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def answers(host, timeout=5, port=PORT):
    """Something answers on the NAS's NFS port (a connection opened and closed again)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def this_pc(host, port=PORT):
    """This PC's address as the NAS sees it (the one its NFS permissions must allow), or
    '' when it can't be told. No packet is sent: a UDP socket is only pointed at the NAS."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((host, port))
            return probe.getsockname()[0]
    except OSError:
        return ''


def shared_folders(host, run=subprocess.run):
    """The folders the NAS says it shares: [(folder, who may use it)], from `showmount -e`.
    Empty when it gives no list: a NAS that speaks only NFS version 4 has none to give,
    and the folder is then typed as the NAS's own page shows it."""
    try:
        done = run_tool(['showmount', '-e', '--no-headers', host], 30, run)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode:
        return []
    return listed(done.stdout)


def listed(text):
    """showmount's lines ("/volume1/media 192.168.1.0/24,192.168.1.30") as [(folder,
    who)]; a folder NekoServer could not name in /etc/fstab (a space in it) is left out."""
    found = []
    for line in text.splitlines():
        # The folder, then who may use it (one word, the last on the line).
        folder, _, who = line.strip().rpartition(' ')
        folder = folder.strip().rstrip('/')
        if FOLDER.fullmatch(folder):
            found.append((folder, who))
    return found


# --- Trying a shared folder out -------------------------------------------------------


class Refused(RuntimeError):
    """A shared folder NekoServer cannot use: the reason, and what to change on the NAS."""


# For the test only: a NAS that stops answering in the middle fails the test after
# about half a minute instead of making setup wait for it.
TEST_OPTIONS = 'soft,timeo=100,retrans=2,nodev,nosuid'
TEST_FOLDER = Path('/run/nekoserver-nfs-test')


def tried(export, uid, gid, run=subprocess.run, folder=TEST_FOLDER):
    """The shared folder mounted privately and tried out as root and as the apps' user
    (uid, gid). Returns {'fstype': 'nfs4' or 'nfs', 'size': bytes, 'free': bytes};
    raises Refused with the first thing that would not work, and what to change."""
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fstype = mount_for_test(export, folder, run)
    try:
        return {'fstype': fstype, **try_out(folder, export, uid, gid)}
    finally:
        if run_tool(['umount', str(folder)], 60, run).returncode:
            run_tool(['umount', '-l', str(folder)], 60, run)
        try:
            folder.rmdir()
        except OSError:
            pass


def mount_for_test(export, folder, run=subprocess.run):
    """Mounted at `folder`: NFS version 4, or 3 when the NAS has no 4. Returns the type
    Linux mounted it as."""
    said = []
    for version, fstype in (('4', 'nfs4'), ('3', 'nfs')):
        mount = ['mount', '-t', 'nfs', '-o', f'nfsvers={version},{TEST_OPTIONS}', export, str(folder)]
        try:
            done = run_tool(mount, 90, run)
        except subprocess.TimeoutExpired:
            said.append('no answer from the NAS within 90 seconds')
            continue
        if done.returncode == 0:
            return fstype
        lines = (done.stderr or done.stdout or '').strip().splitlines()
        said.append(lines[-1].strip()[:200] if lines else f'exit {done.returncode}')
    raise Refused(mount_refused(export, said))


def mount_refused(export, said):
    """Why the NAS would not let this PC mount the folder, with what to change there."""
    host, folder = parts(export)
    words = ' / '.join(dict.fromkeys(said))
    me = this_pc(host)
    me = f" ({me})" if me else ''
    low = words.lower()
    if 'access denied' in low or 'permission denied' in low or 'operation not permitted' in low:
        fix = f"On the NAS, allow this PC's address{me} to use that shared folder over NFS, with read and write access"
    elif 'no such file' in low:
        fix = (
            f'On the NAS, check the name of the shared folder as its NFS settings show it (its full path, such as '
            f'/volume1/media; {folder} was not found)'
        )
    else:
        fix = 'On the NAS, check that NFS is switched on (version 4, or 3) and that this PC may use that shared folder'
    return f'The NAS would not let this PC open {export} ({words}). {fix}, then run setup again. Nothing was changed.'


def try_out(folder, export, uid, gid, as_user=None):
    """Every check, in a folder of NekoServer's own made for it and removed after."""
    spot = Path(folder) / f'.nekoserver-check-{os.getpid()}'
    try:
        root_writes(spot, export)
        owner_kept(spot, export, uid, gid)
        apps_user_works(spot, export, uid, gid, as_user or run_as)
        return room(folder, export)
    finally:
        shutil.rmtree(spot, ignore_errors=True)


AGAIN = 'then run setup again. Nothing was changed.'


def root_writes(spot, export):
    """Root makes a folder and a file there, reads the file back, and still owns it."""
    host, _ = parts(export)
    me = this_pc(host)
    try:
        spot.mkdir(mode=0o755)
        note = spot / 'root-file'
        with note.open('wb') as out:
            out.write(CONTENT)
            out.flush()
            os.fsync(out.fileno())
        back = note.read_bytes()
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise Refused(
                f'This PC may read {export} but not write to it ({exc.strerror}). On the NAS, give this PC'
                f"{' (' + me + ')' if me else ''} read and write access to that shared folder in its NFS "
                f'permissions, and do not turn its administrator into a guest ("root squash"), {AGAIN}'
            ) from None
        raise Refused(f'Writing to {export} failed ({exc.strerror}). Check the NAS, {AGAIN}') from None
    if back != CONTENT:
        raise Refused(f'A file written to {export} did not read back the same. Check the NAS and its disks, {AGAIN}')
    # Setup runs as root: on a NAS that squashes root the file belongs to its guest account.
    if note.stat().st_uid != os.geteuid():
        raise Refused(squashed(export))


CONTENT = b'NekoServer checks that this shared folder keeps files as they are written\n'


def squashed(export):
    return (
        f'The NAS turns this PC\'s administrator (root) into a guest on {export} ("root squash"), so a backup '
        'kept there could not record who owns each file, and a restore would hand every file to the wrong '
        'account. On the NAS, switch that off for this PC in the shared folder\'s NFS permissions (it is called '
        f'"no_root_squash", "Squash: no mapping" or "Maproot user: root", depending on the NAS), {AGAIN}'
    )


def owner_kept(spot, export, uid, gid):
    """A file given to the apps' user stays theirs, with the permissions set on it."""
    mine = spot / 'apps'
    try:
        mine.mkdir(mode=0o700)
        os.chown(mine, uid, gid, follow_symlinks=False)
        os.chmod(mine, 0o2770)
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES, errno.EINVAL):
            raise Refused(squashed(export)) from None
        raise Refused(
            f'Setting who owns a folder on {export} failed ({exc.strerror}). Check the NAS, {AGAIN}'
        ) from None
    seen = mine.stat()
    if (seen.st_uid, seen.st_gid) != (uid, gid):
        raise Refused(
            f'The NAS does not keep who owns a file on {export}: one given to the apps\' account ({uid}:{gid}) came '
            f'back as {seen.st_uid}:{seen.st_gid} (it stores every file as one account, "all squash" or "map all '
            f'users"). On the NAS, switch that mapping off for this PC in the shared folder\'s NFS permissions, {AGAIN}'
        )
    if seen.st_mode & 0o7777 != 0o2770:
        raise Refused(
            f'The NAS does not keep the permissions of a file on {export} (set to 2770, read back as '
            f'{seen.st_mode & 0o7777:o}): it is most likely a Windows-style (NTFS, exFAT or FAT) disk shared over '
            f'NFS. Share a folder on a Linux-style volume of the NAS (ext4, Btrfs, XFS or ZFS), {AGAIN}'
        )


# What the apps' user does there, in a process of its own running as that user. It says
# one word for what failed (below), or "ok".
AS_THE_APPS = r"""
import fcntl, os, sys
folder, uid, gid = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
path = os.path.join(folder, 'apps-file')
try:
    with open(path, 'wb') as out:
        out.write(b'written by the apps\n')
        out.flush()
        os.fsync(out.fileno())
    with open(path, 'rb') as back:
        if back.read() != b'written by the apps\n':
            sys.exit('read')
except OSError as exc:
    sys.exit('write ' + (exc.strerror or str(exc)))
seen = os.stat(path)
if (seen.st_uid, seen.st_gid) != (uid, gid):
    sys.exit('owner %d:%d' % (seen.st_uid, seen.st_gid))
held = open(path, 'rb')
try:
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    sys.exit('lock ' + (exc.strerror or str(exc)))
child = os.fork()
if child == 0:
    # Another program asking for the same lock must be told it is taken.
    try:
        fcntl.flock(open(path, 'rb'), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os._exit(0)
    except OSError:
        os._exit(2)
    os._exit(1)
_, status = os.waitpid(child, 0)
if os.waitstatus_to_exitcode(status) != 0:
    sys.exit('shared')
print('ok')
"""


def run_as(uid, gid, folder, run=subprocess.run):
    """The apps' user's part of the test: (what it said, '' when all is well)."""
    try:
        done = run(
            [sys.executable, '-I', '-c', AS_THE_APPS, str(folder), str(uid), str(gid)],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            user=uid,
            group=gid,
            extra_groups=[],
        )
    except subprocess.TimeoutExpired:
        return 'lock no answer within 90 seconds'
    if done.returncode == 0 and done.stdout.strip() == 'ok':
        return ''
    said = (done.stderr or done.stdout or '').strip().splitlines()
    return said[-1] if said else f'stopped (exit {done.returncode})'


def apps_user_works(spot, export, uid, gid, as_user):
    """The apps' user writes a file of its own, reads it back, owns it, and can lock it."""
    said = as_user(uid, gid, spot / 'apps')
    if not said:
        return
    kind, _, detail = said.partition(' ')
    if kind == 'owner':
        raise Refused(
            f'A file the apps\' account ({uid}:{gid}) wrote to {export} belongs to {detail} there: the NAS maps '
            'every account to one of its own ("all squash" or "map all users"). On the NAS, switch that mapping '
            f'off for this PC in the shared folder\'s NFS permissions, {AGAIN}'
        )
    if kind in ('lock', 'shared'):
        why = detail or 'a lock one program held was given to another as well'
        raise Refused(
            f'File locks do not work on {export} ({why}). The apps and the backups use them to keep two '
            'programs from writing one file at once. On the NAS, switch on NFS version 4 (4.1 where it is '
            f'offered), or its lock service for version 3 (NLM, "lockd"), {AGAIN}'
        )
    raise Refused(
        f'The apps\' account ({uid}:{gid}) could not use a folder of its own on {export} ({said}). On the NAS, '
        'check the shared folder\'s NFS permissions, and that its top level lets other accounts pass '
        f'(permissions 755), {AGAIN}'
    )


def room(folder, export):
    """The size and the free space the NAS reports, when they make sense."""
    seen = os.statvfs(folder)
    size, free = seen.f_blocks * seen.f_frsize, seen.f_bavail * seen.f_frsize
    if size <= 0 or free > size or size >= 2**62:
        raise Refused(
            f'The NAS reports no usable size for {export} (size {size} bytes, free {free}), so NekoServer could '
            f'not tell when it runs full. Check the shared folder (and its quota) on the NAS, {AGAIN}'
        )
    return {'size': size, 'free': free}
