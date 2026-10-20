"""Verify a nightly snapshot, or restore it into a NEW, isolated directory.

Shared by app-recovery.py (app settings) and host-recovery.py (Steam, Samba and other
host files). Nothing here starts containers or services, or writes to live paths.
"""
import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys

from neko_common import safe_path, atomic_json, ensure_directory, ensure_room


@dataclass(frozen=True)
class Kind:
    """What differs between the two nightly snapshot kinds."""
    format: str
    restore_format: str
    roots: tuple = ()                  # allowed first path parts; empty = sourceN
    links: bool = False                # symlinks are stored as inert text files
    report: dict = field(default_factory=dict)
    restored_message: str = ''
    failed_message: str = ''

    def root_ok(self, first):
        if self.roots:
            return first in self.roots
        return first.startswith('source') and first[6:].isdigit()

    def kinds(self):
        return ('file', 'directory', 'symlink') if self.links else ('file', 'directory')


APPS = Kind(
    format='neko-media-snapshot-v1', restore_format='neko-media-restore-v1',
    roots=('appdata', 'compose', 'installer'),
    report={'containers_started': False, 'application_startup_verified': False},
    restored_message='Files restored into a new directory; live application startup remains untested.',
    failed_message='Snapshot verification or restoration failed. Live app data was not changed.')

HOST = Kind(
    format='neko-host-snapshot-v1', restore_format='neko-host-restore-v1', links=True,
    report={'services_started': False, 'application_startup_verified': False,
            'links_are_inert_text': True, 'live_paths_changed': False},
    restored_message='Files restored into a new private directory; symbolic links remain inert text. '
                     'Live service startup remains untested.',
    failed_message='Snapshot verification or restoration failed. Live OS/user/Samba data was not changed.')


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def relative(kind, value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise RuntimeError('Invalid snapshot path')
    path = PurePosixPath(value)
    if not path.parts or value == '.' or path.is_absolute() or '..' in path.parts or '.' in path.parts or str(path) != value:
        raise RuntimeError('Unsafe snapshot path')
    if not kind.root_ok(path.parts[0]):
        raise RuntimeError('Unmanaged snapshot path')
    return Path(*path.parts)


def check_metadata(row):
    for key, maximum in (('uid', 2**32-2), ('gid', 2**32-2), ('mode', 0o7777), ('size', 2**63-1)):
        if type(row.get(key)) is not int or not 0 <= row[key] <= maximum:
            raise RuntimeError('Invalid file metadata')
    # Setuid/setgid file execution is never needed for restored settings.
    if row['kind'] == 'file' and row['mode'] & 0o6000:
        raise RuntimeError('Privileged executable metadata refused')


def check_row(kind, snapshot, row, inventory):
    if not isinstance(row, dict):
        raise RuntimeError('Invalid inventory entry')
    rel = relative(kind, row.get('path'))
    if str(rel) in inventory or row.get('kind') not in kind.kinds():
        raise RuntimeError('Duplicate or invalid inventory entry')
    check_metadata(row)
    path = safe_path(snapshot / 'payload' / rel)
    metadata = path.lstat()
    if row['kind'] == 'directory':
        if not path.is_dir():
            raise RuntimeError('Snapshot directory missing')
    elif not path.is_file() or metadata.st_nlink != 1 or metadata.st_size != row['size'] or digest(path) != row.get('sha256'):
        raise RuntimeError('Snapshot file checksum or size mismatch')
    if row['kind'] == 'symlink' and (not isinstance(row.get('link_target'), str) or path.read_text() != row['link_target']):
        raise RuntimeError('Link inventory mismatch')
    return rel


def verify(kind, snapshot):
    snapshot = safe_path(Path(snapshot))
    manifest_path = safe_path(snapshot / 'manifest.json')
    if not manifest_path.is_file() or manifest_path.stat().st_size > 128_000_000:
        raise RuntimeError('Missing or oversized snapshot manifest')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('format') != kind.format:
        raise RuntimeError('Unsupported snapshot format')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > 200_000:
        raise RuntimeError('Invalid file inventory')
    inventory = {}
    for row in rows:
        inventory[str(check_row(kind, snapshot, row, inventory))] = row
    payload = safe_path(snapshot / 'payload')
    actual = set()
    for parent, dirs, files in os.walk(payload, followlinks=False):
        for name in dirs + files:
            actual.add(str(safe_path(Path(parent) / name).relative_to(payload)))
    # Top-level parents (app snapshots) are created privately by the worker, unlisted.
    synthetic = set(kind.roots)
    if actual - synthetic != set(inventory) - synthetic:
        raise RuntimeError('Snapshot contains missing or unlisted entries')
    return manifest


def restore(kind, snapshot, destination, preserve_owners=False):
    snapshot = safe_path(Path(snapshot))
    destination = safe_path(Path(destination))
    if not destination.is_absolute() or destination.exists():
        raise RuntimeError('Restore needs a new absolute directory')
    if destination == snapshot or snapshot in destination.parents or destination in snapshot.parents:
        raise RuntimeError('Restore directory must be separate from snapshot')
    manifest = verify(kind, snapshot)
    ensure_room(destination.parent, sum(r['size'] for r in manifest['files'] if r['kind'] != 'directory'))
    if preserve_owners and os.geteuid() != 0:
        raise RuntimeError('Root required to restore original ownership')
    destination.mkdir(mode=0o700)  # Never create missing parents or reuse an existing directory.
    rows = manifest['files']
    for row in rows:
        copy_row(kind, snapshot, destination, row)
    # Privately retain evidence before applying original directory permissions.
    report = {'format': kind.restore_format, 'files': len(rows), 'checksums_verified': True,
              'original_owners_restored': preserve_owners, **kind.report}
    if kind.links:
        report.update(sources=manifest.get('sources', []), missing_optional=manifest.get('missing_optional', []))
    atomic_json(destination / 'restore-report.json', report)
    if preserve_owners:
        apply_owners(kind, destination, rows)
    return destination


def copy_row(kind, snapshot, destination, row):
    """Copy one inventory entry into the new folder privately, verifying its checksum."""
    rel = relative(kind, row['path'])
    target = safe_path(destination / rel)
    ensure_directory(target.parent, 0o700)
    if row['kind'] == 'directory':
        ensure_directory(target, 0o700)
        return
    with safe_path(snapshot / 'payload' / rel).open('rb') as source, target.open('xb') as output:
        os.chmod(target, 0o600)
        shutil.copyfileobj(source, output, 65536)
        output.flush()
        os.fsync(output.fileno())
    if digest(target) != row['sha256']:
        raise RuntimeError('Restored file verification failed; partial directory retained')


def apply_owners(kind, destination, rows):
    """Original owners and modes, deepest entries first so parents stay writable."""
    for row in sorted(rows, key=lambda r: len(relative(kind, r['path']).parts), reverse=True):
        target = safe_path(destination / relative(kind, row['path']))
        os.chown(target, row['uid'], row['gid'])
        os.chmod(target, row['mode'] if row['kind'] != 'symlink' else 0o600)


def main(kind, description, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('command', choices=('verify', 'restore'))
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--preserve-owners', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            verify(kind, args.snapshot)
            print('Snapshot inventory and file checksums verified.')
        else:
            if not args.destination:
                parser.error('restore requires --destination')
            restore(kind, args.snapshot, args.destination, args.preserve_owners)
            print(kind.restored_message)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print(kind.failed_message, file=sys.stderr)
        return 1
    return 0
