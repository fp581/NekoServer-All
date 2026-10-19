"""Finish Navidrome listener setup and its local desktop entry."""
import os
from pathlib import Path
import subprocess
import sys

import music_client
from modules import tv
from neko_common import VERSION, atomic_json, atomic_write, safe_path

DEPENDENCIES = ['media', 'recovery', 'music_backup']
DESCRIPTION = 'Music library, private listener checks and browser/phone/desktop client handoff'


def options(config):
    opts = config.get('music', {})
    if not isinstance(opts, dict) or type(opts.get('create_desktop_shortcut', True)) is not bool:
        raise RuntimeError('Invalid music desktop shortcut setting')
    if 'navidrome' not in config.get('media', {}).get('services', []):
        raise RuntimeError('Music setup requires navidrome in media.services')
    return opts


def run(ctx):
    opts = options(ctx.config)
    if not ctx.apply:
        ctx.log('Music: Navidrome browser player, Amperfy on iPhone and optional Feishin desktop client')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    from neko_common import verify_storage
    verify_storage(ctx.config)
    shortcut='disabled'
    if opts.get('create_desktop_shortcut', True):
        try:
            if not Path('/usr/bin/firefox').is_file():raise FileNotFoundError('Default browser launcher unavailable')
            user, home = tv.desktop_user(ctx.config)
            folder = safe_path(home/'.local/share/applications')
            target = safe_path(folder/'nekoserver-navidrome.desktop')
            content = tv.desktop_entry('Music', '/usr/bin/firefox --new-window http://127.0.0.1:4533/', 'applications-multimedia')
            if target.exists() and (target.read_text() != content or target.stat().st_uid != user.pw_uid):
                raise RuntimeError('Customized music launcher preserved')
            tv.ensure_user_dir(folder, home, user)
            if not target.exists():
                atomic_write(target, content, 0o755)
                os.chown(target, user.pw_uid, user.pw_gid)
            shortcut='checked'
        except (KeyError, FileNotFoundError):
            # A convenience only: music works in any browser and on phones without it.
            shortcut='skipped'
            ctx.log('Music desktop shortcut skipped (no Firefox or desktop user found); open http://127.0.0.1:4533 in any browser instead')
    result = music_client.check(ctx.config)
    if result['status'] != 'pass' and sys.stdin.isatty():
        release = Path(ctx.config['data_mount'])/'installer/releases'/('v'+VERSION)
        code = subprocess.run([sys.executable, str(release/'music-setup.py'), '--config', str(ctx.config_path), '--configure'], check=False).returncode
        if code == 2:
            raise RuntimeError('Music listener setup failed; credentials/configuration preserved')
        result = music_client.check(ctx.config)
    result['desktop_shortcut']=shortcut
    atomic_json(Path(ctx.config['data_mount'])/'reports/music-setup.json', result)
    ctx.log('Music uses the SSD library/music folder read-only; Lidarr imports into the same folder. Music gateway: HTTPS port 8446. Phone/desktop client login and real playback remain acceptance steps.')
    if result['status'] != 'pass':
        return {'status': 'needs_setup', 'reason': 'Finish the Navidrome listener using music-setup.py --configure on the server'}

