"""Finish Navidrome listener setup (its desktop shortcut comes from modules/desktop.py)."""

import os
from pathlib import Path
import sys

import music_client
import setup_progress
from neko_common import VERSION, atomic_json, media_group, media_services, music_library

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
    if not media_group(ctx.config, 'music'):
        ctx.log('Music: switched off (Music (Navidrome) in setup); no music player to set up')
        return
    if 'navidrome' not in media_services(ctx.config):
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
        # Inside setup's own process: its two questions are asked in the setup window too.
        code = setup_progress.run_program(release / 'music-setup.py', ['--config', ctx.config_path, '--configure'])
        if code == 2:
            raise RuntimeError('Music listener setup failed; credentials/configuration preserved')
        result = music_client.check(ctx.config)
    atomic_json(Path(ctx.config['data_mount']) / 'reports/music-setup.json', result)
    ctx.log(
        f'Music uses {music_library(ctx.config)} read-only; Lidarr imports into the same folder. Music gateway: the HTTPS address setup printed for Music (port 8446 unless another program had it; the phone setup page lists it). Phone/desktop client login and real playback remain acceptance steps.'
    )
    if result['status'] != 'pass':
        return {
            'status': 'needs_setup',
            'reason': 'Finish the Navidrome listener using music-setup.py --configure on the server',
        }
