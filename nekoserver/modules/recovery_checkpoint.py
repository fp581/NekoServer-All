"""Record verified Docker identity after successful foundation setup."""

from recovery_tools import checkpoint
from neko_common import VERSION

DEPENDENCIES = ['docker']
DESCRIPTION = 'Save a verified Docker store checkpoint for later OS reattachment'


def run(ctx):
    if not ctx.apply:
        ctx.log('Recovery checkpoint is recorded after Docker setup with --apply')
        return
    try:
        checkpoint(ctx.config, apply=True)
    except RuntimeError as exc:
        # Say why: the reason is what tells you what to fix.
        tool = f"{ctx.config.get('data_mount', '/srv/nekoserver')}/installer/releases/v{VERSION}/recover.py"
        ctx.log(
            f'Recovery checkpoint not saved ({exc}); check Docker with  sudo python3 {tool} checkpoint  before replacing the OS'
        )
        return {'status': 'needs_setup', 'reason': f'Docker recovery checkpoint not saved: {exc}'}
    ctx.log('Verified Docker/store/storage identity saved on the application SSD')
