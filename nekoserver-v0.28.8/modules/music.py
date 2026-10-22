"""Finish Navidrome listener setup (its desktop shortcut comes from modules/desktop.py)."""
import os
from pathlib import Path
import subprocess
import sys

import music_client
from neko_common import VERSION, atomic_json

DEPENDENCIES = ['media', 'recovery', 'music_backup']
DESCRIPTION = 'Music library, private listener checks and browser/phone/desktop client handoff'


def options(config):
    opts = config.get('music', {})
    # create_desktop_shortcut: whether the desktop step adds a Music shortcut.
    if not isinstance(opts, dict) or type(opts.get('create_desktop_shortcut', True)) is not bool:
        raise RuntimeError('Invalid music desktop shortcut setting')
    if 'navidrome' not in config.get('media', {}).get('services', []):
        raise RuntimeError('Music setup requires navidrome in media.services')
    return opts


def run(ctx):
    options(ctx.config)
    if not ctx.apply:
        ctx.log('Music: Navidrome browser player, Amperfy on iPhone and optional Feishin desktop client')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    from neko_common import verify_storage
    verify_storage(ctx.config)
    result = music_client.check(ctx.config)
    if result['status'] != 'pass' and sys.stdin.isatty():
        release = Path(ctx.config['data_mount'])/'installer/releases'/('v'+VERSION)
        code = subprocess.run([sys.executable, str(release/'music-setup.py'), '--config', str(ctx.config_path), '--configure'], check=False).returncode
        if code == 2:
            raise RuntimeError('Music listener setup failed; credentials/configuration preserved')
        result = music_client.check(ctx.config)
    atomic_json(Path(ctx.config['data_mount'])/'reports/music-setup.json', result)
    ctx.log('Music uses the SSD library/music folder read-only; Lidarr imports into the same folder. Music gateway: HTTPS port 8446. Phone/desktop client login and real playback remain acceptance steps.')
    if result['status'] != 'pass':
        return {'status': 'needs_setup', 'reason': 'Finish the Navidrome listener using music-setup.py --configure on the server'}

