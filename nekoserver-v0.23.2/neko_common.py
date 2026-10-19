"""Shared local filesystem safety; standard library only."""
import ctypes
import errno
import json
import os
import re
import tempfile
from pathlib import Path

VERSION = '0.23.2'

TIMEZONE_PATTERN = re.compile(r'UTC|[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+')
ZONEINFO = Path('/usr/share/zoneinfo')


def host_timezone(config):
    """IANA zone used for container TZ and systemd timer schedules.

    Order: top-level ``timezone``, the legacy ``media.timezone`` and
    ``maintenance.timezone`` keys, then the host's /etc/localtime, then UTC.
    ``"auto"`` (the default in a new config) means "use this PC's timezone".
    """
    value = config.get('timezone')
    automatic = value == 'auto'
    if automatic:
        value = None
    for section in ('media', 'maintenance'):
        if value is None and not automatic:
            group = config.get(section, {})
            value = group.get('timezone') if isinstance(group, dict) else None
    if value is None:
        try:
            link = os.readlink('/etc/localtime')
            value = link.split('zoneinfo/', 1)[1] if 'zoneinfo/' in link else None
        except OSError:
            value = None
    if value is None:
        value = 'UTC'
    if not isinstance(value, str) or not TIMEZONE_PATTERN.fullmatch(value):
        raise RuntimeError('timezone must be an IANA zone such as Europe/London')
    if ZONEINFO.is_dir() and not (ZONEINFO / value).is_file():
        raise RuntimeError(f'timezone {value} is not installed under {ZONEINFO}')
    return value


def safe_path(path):
    path = Path(path)
    if '..' in path.parts:
        raise RuntimeError('Parent traversal is not allowed')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError(f'Refusing symlink path: {item}')
    return path


def mount_path(value):
    path = safe_path(value)
    if (not path.is_absolute() or str(path) in ('/', '/mnt', '/srv', '/home', '/var', '/tmp')
            or '..' in path.parts or not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(value))):
        raise RuntimeError(f'Unsafe mount path: {value}')
    return path


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or str(path) == '.' or not re.fullmatch(r'[A-Za-z0-9_./-]+', str(value)):
        raise RuntimeError(f'Unsafe relative path: {value}')
    return path


def atomic_write(path, payload, mode=0o600):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload.encode('utf-8') if isinstance(payload, str) else payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path, payload):
    atomic_write(path, json.dumps(payload, indent=2) + '\n')


def publish_no_replace(temp, target):
    """Atomic publication; hard link on Linux FS, no-replace rename on exFAT.

    If the kernel/filesystem lacks either primitive, fail without overwriting.
    Never substitute ordinary rename(), which replaces existing files.
    """
    temp, target = safe_path(temp), safe_path(target)
    try:
        os.link(temp, target)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise RuntimeError('Filesystem needs Linux renameat2 for safe backup publication') from None
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temp), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
    else:
        temp.unlink()
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def ensure_directory(path, mode=0o755, uid=None, gid=None):
    """Create parents with explicit access, preserving existing ownership/modes.

    Only directories created by this call are changed. Never recursively chmod
    a populated tree to make a service appear healthy.
    """
    path = safe_path(path)
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    if not parent.is_dir():
        raise RuntimeError('Directory parent is not a directory')
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        if uid is not None:
            os.chown(directory, uid, gid)
        directory.chmod(mode)  # Apply after ownership changes, independent of umask.
    if not path.is_dir():
        raise RuntimeError('Expected directory')
    return path


def verify_storage(config, keys=('data_ssd', 'media_hdd')):
    """Fail closed unless dedicated mounts match saved UUID/filesystem identities."""
    import subprocess
    identities = []
    for key in keys:
        mount_key = 'data_mount' if key == 'data_ssd' else 'media_mount'
        path = mount_path(config[mount_key])
        expected = config['storage'][key]
        if not expected.get('uuid') or not expected.get('fstype'):
            raise RuntimeError('Storage identity is not configured')
        result = subprocess.run(
            ['findmnt', '-n', '-o', 'UUID,FSTYPE', '--mountpoint', str(path)],
            capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.split() != [expected['uuid'], expected['fstype']]:
            raise RuntimeError('Storage identity mismatch; refusing private data access')
        identities.append(expected['uuid'])
    if len(set(identities)) != len(identities):
        raise RuntimeError('Storage filesystems must be distinct')


def compose_image_override(manifest):
    """Use maintenance's private immutable image pins when rerunning deployment."""
    path = safe_path(Path(manifest).parent/'maintenance-images.json')
    if not path.exists():
        return []
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
        raise RuntimeError('Maintenance image pins must be private and root-owned')
    return ['-f', str(path)]
