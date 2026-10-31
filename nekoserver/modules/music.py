"""Finish Navidrome listener setup (its desktop shortcut comes from modules/desktop.py)."""

import os
from pathlib import Path
import subprocess
import sys

import music_client
import setup_progress
from neko_common import VERSION, atomic_json

DEPENDENCIES = ['media', 'recovery', 'music_backup']
DESCRIPTION = 'Music library, private listener checks and browser/phone/desktop client handoff'


def options(config):
    opts = config.get('music', {})
    # create_desktop_shortcut: whether the desktop step adds a Music shortcut.
    if not isinstance(opts, dict) or type(opts.get('create_desktop_shortcut', True)) is not bool:
        raise RuntimeError('Invalid music desktop shortcut setting')
    return opts


def run(ctx):
    options(ctx.config)
    if 'navidrome' not in ctx.config.get('media', {}).get('services', []):
        # A server without the music player (taken out of media.services): nothing to do
        # here, and the rest of setup goes ahead. The music backup still keeps Lidarr's music.
        ctx.log('Music: Navidrome is not among your apps ("services" under "media"); no music player to set up')
        return
    if not ctx.apply:
        ctx.log('Music: Navidrome browser player, Amperfy on iPhone and optional Feishin desktop client')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    from neko_common import verify_storage

    verify_storage(ctx.config)
    result = music_client.check(ctx.config)
    if result['status'] != 'pass' and sys.stdin.isatty():
        release = Path(ctx.config['data_mount']) / 'installer/releases' / ('v' + VERSION)
        with setup_progress.in_terminal('Music listener setup'):
            code = subprocess.run(
                [sys.executable, str(release / 'music-setup.py'), '--config', str(ctx.config_path), '--configure'],
                check=False,
            ).returncode
        if code == 2:
            raise RuntimeError('Music listener setup failed; credentials/configuration preserved')
        result = music_client.check(ctx.config)
    atomic_json(Path(ctx.config['data_mount']) / 'reports/music-setup.json', result)
    ctx.log(
        'Music uses the SSD library/music folder read-only; Lidarr imports into the same folder. Music gateway: HTTPS port 8446. Phone/desktop client login and real playback remain acceptance steps.'
    )
    if result['status'] != 'pass':
        return {
            'status': 'needs_setup',
            'reason': 'Finish the Navidrome listener using music-setup.py --configure on the server',
        }
