#!/usr/bin/env python3
"""Verify and stage an independent monthly copy without replacing live data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from neko_common import safe_path, atomic_json


def verify(snapshot):
    snapshot = safe_path(snapshot)
    manifest = json.loads(safe_path(snapshot/'manifest.json').read_text())
    if manifest.get('format') != 'neko-maintenance-v1':
        raise RuntimeError('Unknown monthly snapshot')
    for name in ('data', 'media', 'images'):
        path = safe_path(snapshot/(name+'.tar'))
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest != manifest.get('sha256', {}).get(name):
            raise RuntimeError('Snapshot checksum mismatch: '+name)
    return manifest


def stage(snapshot, destination):
    snapshot, destination = safe_path(snapshot), safe_path(destination)
    verify(snapshot)
    if not destination.is_absolute() or destination.exists() or snapshot == destination or snapshot in destination.parents:
        raise RuntimeError('Choose a new, separate absolute recovery directory')
    destination.mkdir(mode=0o700)
    for name in ('data', 'media'):
        target = destination/name
        target.mkdir(mode=0o700)
        subprocess.run(['tar', '--extract', '--file', str(snapshot/(name+'.tar')), '--acls', '--xattrs', '--numeric-owner', '--same-owner', '-C', str(target)], check=True)
        subprocess.run(['tar', '--compare', '--file', str(snapshot/(name+'.tar')), '-C', str(target)], check=True)
    atomic_json(destination/'verified.json', {'format': 'neko-monthly-staged-v1', 'snapshot': str(snapshot), 'live_data_replaced': False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--load-images', action='store_true', help='Load verified old Docker images for disaster recovery; does not start containers')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Root is required to preserve private database ownership')
    verify(args.snapshot)
    if args.stage:
        stage(args.snapshot, args.stage)
    if args.load_images:
        subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', 'image', 'load', '--input', str(safe_path(args.snapshot/'images.tar'))], check=True)
    print('Monthly backup verified; live data and applications were not replaced.')


if __name__ == '__main__':
    main()
