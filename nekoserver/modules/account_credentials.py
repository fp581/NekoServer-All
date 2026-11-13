"""Collect account choices once before app deployment; never print passwords."""

import os
import sys
import account_store
from neko_common import ADMINISTRATOR, mount_path, verify_storage
from recovery_tools import lock

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Console per-app login/password choices and private credential inventory'


def navidrome_is_new(config):
    """Navidrome has no data on the SSD yet: its logins are made by this release."""
    data = os.path.realpath(mount_path(config['data_mount']))
    folder = os.path.realpath(os.path.join(data, 'appdata', 'navidrome'))
    # The SSD's path comes from the settings file: the folder is looked at only once it
    # is proved to lie on the SSD with every link followed.
    if not folder.startswith(data + os.sep):
        raise RuntimeError(account_store.OUTSIDE)
    return not os.path.isdir(folder) or not os.listdir(folder)


def administrator_is_your_music_login(ctx):
    """On a server whose Navidrome logins are made now, the login the person is given
    for Navidrome is the administrator, as in every other app (neko_common.
    navidrome_login). Saved before the logins are made, so a run cut off in between
    still knows."""
    music = ctx.config.setdefault('music', {})
    if isinstance(music, dict) and music.get('your_login') != ADMINISTRATOR:
        music['your_login'] = ADMINISTRATOR
        if hasattr(ctx, 'persist_config'):
            ctx.persist_config()


def run(ctx):
    if not account_store.enabled(ctx.config):
        return
    if not ctx.apply:
        ctx.log('Account credentials: prompt/generate only during apply; no passwords in config/logs')
        return
    verify_storage(ctx.config)
    plan = getattr(ctx, 'plan', ('media', 'photos', 'nextcloud'))
    apps = account_store.selected(ctx.config, plan)
    if not apps:
        apps = account_store.selected(ctx.config, ('media', 'photos', 'nextcloud'))
    missing = set(apps) - account_store.load(ctx.config)['apps'].keys()
    if missing and not sys.stdin.isatty():
        return {
            'status': 'needs_setup',
            'reason': 'Run  sudo bash install.sh  in a terminal window to choose each app\'s login and password',
        }
    if 'navidrome' in missing and navidrome_is_new(ctx.config):
        administrator_is_your_music_login(ctx)
    with lock(ctx.config):
        account_store.collect(ctx.config, apps, allow_corrections=sys.stdin.isatty())
    ctx.log('Account choices saved under the private SSD secrets/accounts directory')
