#!/usr/bin/env python3
"""Configure/check the local music listener; passwords never enter command arguments."""

import argparse
import getpass
import os
from pathlib import Path
import sys

import music_client
from neko_common import (
    SETTINGS_HELP,
    read_settings,
    atomic_json,
    verify_storage,
    checked_settings,
    default_settings,
    media_services,
    music_library,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--config',
        type=lambda v: Path(checked_settings(v)),
        default=default_settings(Path(__file__).parent),
        help=SETTINGS_HELP,
    )
    parser.add_argument(
        '--configure',
        action='store_true',
        help='Ask for the login the music check signs in with and save it (without it: only check)',
    )
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run as root to protect the monitor token')
    try:
        config = read_settings(args.config)
        verify_storage(config)
    except RuntimeError as exc:  # a drive not connected, or the runtime not built yet
        print(f'Music setup stopped: {exc}. Nothing was changed.', file=sys.stderr)
        return 2
    if 'navidrome' not in media_services(config):
        parser.error('Select navidrome in media.services first (and the Music group: "music" under "media_apps")')
    if args.configure:
        if not sys.stdin.isatty():
            parser.error('Credential setup requires an interactive local terminal')
        print(
            'Open http://127.0.0.1:4533 on the server. Create your administrator, then a separate non-admin listener account.'
        )
        user = input('Listener username: ').strip()
        password = getpass.getpass('Listener password (never saved as plaintext): ')
        value = music_client.credential(user, password)
        del password
        try:
            result = music_client.save(config, value)
        except RuntimeError as exc:
            # The check's own words say what to change (they hold no secret).
            print(f'Listener verification failed: {exc}. No credential was saved.')
            return 2
        except (OSError, ValueError):
            print('Listener verification failed; no credential was saved. Check the account and local music service.')
            return 2
    else:
        result = music_client.check(config)
    atomic_json(Path(config['data_mount']) / 'reports/music-setup.json', result)
    print('Music setup: ' + result['status'])
    if result.get('library_empty'):
        print(f'The library is empty. Import your music into {music_library(config)}; Navidrome will scan it.')
    print('Phone: Amperfy (Subsonic/Navidrome). Desktop: Feishin or the built-in browser player.')
    print(
        'For phone/remote clients use the HTTPS music address setup printed (port 8446 unless another '
        'program had it; the phone setup page lists it), not localhost.'
    )
    if result['status'] == 'pass':
        return 0
    return 1 if result['status'] == 'needs_setup' else 2


if __name__ == '__main__':
    raise SystemExit(main())
