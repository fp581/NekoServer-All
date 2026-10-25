#!/usr/bin/env python3
"""Configure/check the local music listener; passwords never enter command arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

import music_client
from neko_common import atomic_json, verify_storage, checked_settings, default_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent))
    parser.add_argument('--configure', action='store_true')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run as root to protect the monitor token')
    config = json.loads(args.config.read_text())
    verify_storage(config)
    if 'navidrome' not in config.get('media', {}).get('services', []):
        parser.error('Select navidrome in media.services first')
    if args.configure:
        if not sys.stdin.isatty():
            parser.error('Credential setup requires an interactive local terminal')
        print('Open http://127.0.0.1:4533 on the server. Create your administrator, then a separate non-admin listener account.')
        user = input('Listener username: ').strip()
        password = getpass.getpass('Listener password (never saved as plaintext): ')
        value = music_client.credential(user, password)
        del password
        try:
            result = music_client.save(config, value)
        except (OSError, ValueError, RuntimeError):
            print('Listener verification failed; no credential was saved. Check the account and local music service.')
            return 2
    else:
        result = music_client.check(config)
    atomic_json(Path(config['data_mount'])/'reports/music-setup.json', result)
    print('Music setup: '+result['status'])
    if result.get('library_empty'):
        print('The library is empty. Import your music into the SSD library/music folder; Navidrome will scan it.')
    print('Phone: Amperfy (Subsonic/Navidrome). Desktop: Feishin or the built-in browser player.')
    print('For phone/remote clients use the configured HTTPS music gateway on port 8446, not localhost.')
    return 0 if result['status'] == 'pass' else 1 if result['status'] == 'needs_setup' else 2


if __name__ == '__main__':
    raise SystemExit(main())
