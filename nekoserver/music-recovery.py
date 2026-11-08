#!/usr/bin/env python3
"""Verify a music backup inventory, optionally restoring to a new directory."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from neko_common import safe_path


def restore(root, snapshot, destination=None):
    root = safe_path(Path(root))
    snapshot = safe_path(Path(snapshot))
    if snapshot.parent != root / 'snapshots' or snapshot.stat().st_size > 128_000_000:
        raise RuntimeError('Select an inventory directly inside backups/music/snapshots')
    value = json.loads(snapshot.read_text())
    if value.get('format') != 'neko-music-backup-v1' or not isinstance(value.get('files'), list):
        raise RuntimeError('Invalid music backup inventory')
    seen = set()
    verified = []
    for row in value['files']:
        name = row.get('path')
        if not isinstance(name, str) or not name or '\x00' in name:
            raise RuntimeError('Invalid music path')
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or str(rel) != name or name == '.':
            raise RuntimeError('Unsafe music path')
        if str(rel) in seen:
            raise RuntimeError('Duplicate music path')
        seen.add(str(rel))
        checksum = row['sha256']
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise RuntimeError('Invalid music checksum')
        source = safe_path(root / 'objects' / checksum[:2] / checksum)
        if type(row.get('size')) is not int or row['size'] < 0 or not source.is_file():
            raise RuntimeError('Invalid music object')
        with source.open('rb') as stream:
            if source.stat().st_size != row['size'] or hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                raise RuntimeError('Music backup checksum mismatch')
        verified.append((source, rel, checksum))
    if destination is not None:
        dest = safe_path(Path(destination))
        if not dest.is_absolute() or dest.exists() or dest == root or root in dest.parents or dest in root.parents:
            raise RuntimeError('Use a new, separate restore directory')
        dest.mkdir(mode=0o700)
        # Every file is written through the folder made just now, opened once (its parent
        # may be writable by others): a folder swapped in at its path meanwhile gets no
        # part of the copy, where writing each file by its path split the files between
        # the two folders while the count said all were restored (the review of 0.47.9;
        # monthly-recovery.py stages a snapshot the same way).
        folder = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for source, rel, checksum in verified:
                write_copy(folder, source, rel, checksum)
            here = os.stat(dest, follow_symlinks=False)
            made = os.fstat(folder)
            if (here.st_dev, here.st_ino) != (made.st_dev, made.st_ino):
                raise RuntimeError(
                    f'{dest} was replaced while the music was restored; nothing there is a checked copy. '
                    'Choose a new folder and restore again.'
                )
        finally:
            os.close(folder)
    return len(verified)


def write_copy(folder, source, rel, checksum):
    """One music file copied to `rel` under the open `folder` (its descriptor): its
    subfolders made and the file created and read back through that descriptor, then
    compared with its checksum."""
    for parent in reversed(rel.parents):
        if str(parent) != '.':
            try:
                os.mkdir(str(parent), 0o700, dir_fd=folder)
            except FileExistsError:
                pass
    out = os.open(str(rel), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
    with source.open('rb') as inp, os.fdopen(out, 'wb') as stream:
        shutil.copyfileobj(inp, stream)
    back = os.open(str(rel), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder)
    with os.fdopen(back, 'rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
            raise RuntimeError('Restored music checksum mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--destination')
    args = parser.parse_args()
    count = restore(args.backup, args.snapshot, args.destination)
    print(
        f'{count} music files verified; '
        + ('restored to the new directory' if args.destination else 'no live files changed')
    )


if __name__ == '__main__':
    main()
