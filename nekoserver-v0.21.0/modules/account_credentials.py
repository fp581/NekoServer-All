"""Collect account choices once before app deployment; never print passwords."""
import sys
import account_store
from neko_common import verify_storage
from recovery_tools import lock

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Console per-app login/password choices and private credential inventory'


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
        return {'status': 'needs_setup', 'reason': 'Run interactively in Konsole to choose per-app account credentials'}
    with lock(ctx.config):
        account_store.collect(ctx.config, apps, allow_corrections=sys.stdin.isatty())
    ctx.log('Account choices saved under the private SSD secrets/accounts directory')
