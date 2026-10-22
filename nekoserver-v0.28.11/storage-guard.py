#!/usr/bin/env python3
"""Fail Docker startup unless the configured SSD and HDD are actually mounted."""
import argparse
import json
import subprocess
import sys
from neko_common import findmnt, mount_path


def verify(config):
    identities = []
    for key, name in [('data_ssd', 'data_mount'), ('media_hdd', 'media_mount')]:
        path = mount_path(config[name])
        expected = config['storage'][key]['uuid']
        if not expected:
            raise RuntimeError(f'{key}: no configured UUID')
        fields = (findmnt('UUID,FSTYPE', path) or '').split()
        if fields != [expected, config['storage'][key]['fstype']]:
            raise RuntimeError(f'{key}: missing or wrong filesystem at {path}')
        identities.append(expected)
    if identities[0] == identities[1]:
        raise RuntimeError('SSD and HDD must be different filesystems')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        with open(args.config) as stream:
            verify(json.load(stream))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print('Storage guard FAILED:', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
