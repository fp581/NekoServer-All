"""Record verified Docker identity after successful foundation setup."""
from recovery_tools import checkpoint
DEPENDENCIES=['docker']
DESCRIPTION='Save a verified Docker store checkpoint for later OS reattachment'
def run(ctx):
    if not ctx.apply:ctx.log('Recovery checkpoint is recorded after Docker setup with --apply');return
    try:checkpoint(ctx.config,apply=True)
    except RuntimeError:
        ctx.log('Recovery checkpoint unavailable; verify Docker storage backend with recover.py checkpoint before OS replacement')
        return {'status':'needs_setup','reason':'Verified classic overlay2 Docker checkpoint still required for automatic recovery'}
    ctx.log('Verified Docker/store/storage identity saved on the application SSD')
