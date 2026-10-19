#!/usr/bin/env python3
"""Verify a music backup inventory, optionally restoring to a new directory."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

from neko_common import safe_path


def restore(root, snapshot, destination=None):
    root = safe_path(Path(root))
    snapshot = safe_path(Path(snapshot))
    if snapshot.parent != root/'snapshots' or snapshot.stat().st_size > 128_000_000:
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
        source = safe_path(root/'objects'/checksum[:2]/checksum)
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
        for source, rel, checksum in verified:
            target = safe_path(dest/rel)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with source.open('rb') as inp, target.open('xb') as out:
                target.chmod(0o600)
                shutil.copyfileobj(inp, out)
            with target.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
                    raise RuntimeError('Restored music checksum mismatch')
    return len(verified)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--destination')
    args = parser.parse_args()
    count = restore(args.backup, args.snapshot, args.destination)
    print(f'{count} music files verified; '+('restored to the new directory' if args.destination else 'no live files changed'))


if __name__ == '__main__':
    main()
