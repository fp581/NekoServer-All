#!/usr/bin/env python3
"""Verify a host/user snapshot or restore it into a NEW, isolated directory.

This ad hoc tool never starts containers or writes to live OS, user or Samba paths.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys

from neko_common import safe_path, atomic_json, ensure_directory

FORMAT = 'neko-host-snapshot-v1'


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise RuntimeError('Invalid snapshot path')
    path = PurePosixPath(value)
    if not path.parts or value == '.' or path.is_absolute() or '..' in path.parts or '.' in path.parts or str(path) != value:
        raise RuntimeError('Unsafe snapshot path')
    if not path.parts[0].startswith('source') or not path.parts[0][6:].isdigit():
        raise RuntimeError('Unmanaged snapshot path')
    return Path(*path.parts)


def verify(snapshot):
    snapshot = safe_path(Path(snapshot))
    manifest_path = safe_path(snapshot / 'manifest.json')
    if not manifest_path.is_file() or manifest_path.stat().st_size > 128_000_000:
        raise RuntimeError('Missing or oversized snapshot manifest')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('format') != FORMAT:
        raise RuntimeError('Unsupported snapshot format')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > 200_000:
        raise RuntimeError('Invalid file inventory')
    inventory = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError('Invalid inventory entry')
        rel = relative(row.get('path'))
        if str(rel) in inventory or row.get('kind') not in ('file', 'directory', 'symlink'):
            raise RuntimeError('Duplicate or invalid inventory entry')
        for key, maximum in (('uid', 2**32-2), ('gid', 2**32-2), ('mode', 0o7777), ('size', 2**63-1)):
            if type(row.get(key)) is not int or not 0 <= row[key] <= maximum:
                raise RuntimeError('Invalid file metadata')
        # Setuid/setgid file execution is never needed for app configuration.
        if row['kind'] == 'file' and row['mode'] & 0o6000:
            raise RuntimeError('Privileged executable metadata refused')
        path = safe_path(snapshot / 'payload' / rel)
        metadata = path.lstat()
        if row['kind'] == 'directory':
            if not path.is_dir():
                raise RuntimeError('Snapshot directory missing')
        elif not path.is_file() or metadata.st_nlink != 1 or metadata.st_size != row['size'] or digest(path) != row.get('sha256'):
            raise RuntimeError('Snapshot file checksum or size mismatch')
        if row['kind'] == 'symlink':
            if not isinstance(row.get('link_target'), str) or path.read_text() != row['link_target']:
                raise RuntimeError('Link inventory mismatch')
        inventory[str(rel)] = row
    payload = safe_path(snapshot / 'payload')
    actual = set()
    for parent, dirs, files in os.walk(payload, followlinks=False):
        for name in dirs + files:
            path = safe_path(Path(parent) / name)
            actual.add(str(path.relative_to(payload)))
    # Top-level synthetic parents are created privately by the worker.
    synthetic = set()
    if actual - synthetic != set(inventory) - synthetic:
        raise RuntimeError('Snapshot contains missing or unlisted entries')
    return manifest


def restore(snapshot, destination, preserve_owners=False):
    snapshot = safe_path(Path(snapshot))
    destination = safe_path(Path(destination))
    if not destination.is_absolute() or destination.exists():
        raise RuntimeError('Restore needs a new absolute directory')
    if destination == snapshot or snapshot in destination.parents or destination in snapshot.parents:
        raise RuntimeError('Restore directory must be separate from snapshot')
    manifest = verify(snapshot)
    if preserve_owners and os.geteuid() != 0:
        raise RuntimeError('Root required to restore original ownership')
    destination.mkdir(mode=0o700)  # Never create missing parents or reuse an existing directory.
    rows = manifest['files']
    for row in rows:
        rel = relative(row['path'])
        target = safe_path(destination / rel)
        ensure_directory(target.parent, 0o700)
        if row['kind'] == 'directory':
            ensure_directory(target, 0o700)
        else:
            with safe_path(snapshot / 'payload' / rel).open('rb') as source, target.open('xb') as output:
                os.chmod(target, 0o600)
                shutil.copyfileobj(source, output, 65536)
                output.flush()
                os.fsync(output.fileno())
            if digest(target) != row['sha256']:
                raise RuntimeError('Restored file verification failed; partial directory retained')
    # Privately retain evidence before applying original directory permissions.
    atomic_json(destination / 'restore-report.json', {
        'format': 'neko-host-restore-v1', 'files': len(rows),
        'checksums_verified': True, 'original_owners_restored': preserve_owners,
        'services_started': False, 'application_startup_verified': False,
        'sources': manifest.get('sources', []), 'missing_optional': manifest.get('missing_optional', []),
        'links_are_inert_text': True, 'live_paths_changed': False,
    })
    if preserve_owners:
        for row in sorted(rows, key=lambda r: len(relative(r['path']).parts), reverse=True):
            target = safe_path(destination / relative(row['path']))
            os.chown(target, row['uid'], row['gid'])
            os.chmod(target, row['mode'] if row['kind'] != 'symlink' else 0o600)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'restore'))
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--preserve-owners', action='store_true')
    args = parser.parse_args()
    if args.command == 'verify':
        verify(args.snapshot)
        print('Snapshot inventory and file checksums verified.')
    else:
        if not args.destination:
            parser.error('restore requires --destination')
        restore(args.snapshot, args.destination, args.preserve_owners)
        print('Files restored into a new private directory; symbolic links remain inert text. Live service startup remains untested.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print('Snapshot verification or restoration failed. Live OS/user/Samba data was not changed.', file=sys.stderr)
        sys.exit(1)
