"""Run onboarding from the persistent installer after media deployment."""
import guided_setup
import json
import time
import account_store
import setup_progress
import subprocess
import sys
from pathlib import Path
from neko_common import MEDIA_APPS, VERSION, app_port, app_ports, atomic_json, safe_path

DEPENDENCIES=['media','recovery']
DESCRIPTION='Wait for media startup, collect sources and run guided configuration/readiness reporting'


def run(ctx):
    settings=ctx.config.get('onboarding',{})
    if not ctx.apply or not settings.get('enabled',True):
        ctx.log('Guided media onboarding deferred');return
    if account_store.enabled(ctx.config) and 'accounts' in getattr(ctx, 'plan', ()):
        required = account_store.selected(ctx.config, ['media'])
        if any(not account_store.get(ctx.config, app, verified=True) for app in required):
            return {'status': 'needs_setup', 'reason': 'Complete local media account setup before connecting applications'}
    release=safe_path(Path(ctx.config['data_mount'])/'installer'/'releases'/('v'+VERSION))
    script=release/'onboard.py'
    if not script.is_file():raise RuntimeError('Persistent onboarding script missing')
    config=release/'config.json'
    wait=int(settings.get('wait_seconds',180))
    if not 0<=wait<=600:raise RuntimeError('Onboarding wait must be 0..600 seconds')
    cmd=[sys.executable,str(script),'--config',str(config),'--wait',str(wait)]
    if sys.stdin.isatty():
        cmd+=['--configure','--apply']
        if settings.get('configure_libraries',True) and 'jellyfin' in ctx.config.get('media',{}).get('services',[]):cmd.append('--libraries')
        services=ctx.config.get('media',{}).get('services',[])
        if settings.get('configure_request_providers',True) and 'seerr' in services and set(services)&{'sonarr','radarr'}:cmd.append('--request-providers')
        if settings.get('collect_sources',True):cmd.append('--sources')
        if settings.get('configure_indexers',True) and 'prowlarr' in ctx.config.get('media',{}).get('services',[]):cmd.append('--indexers')
        # With the lock backups and monthly updates take: a nightly backup stopping the apps
        # while they are being connected would fail them half-way.
        from recovery_tools import lock
        with lock(ctx.config), setup_progress.in_terminal('Connecting the media apps: your download sources and sign-ins'):
            result=subprocess.run(cmd,check=False).returncode
    else:
        cmd.append('--report-only')
        result=subprocess.run(cmd,check=False).returncode
    if result:
        # Something to fix, said with its reason (on the finishing screen too), not a
        # failed step: what is left is finished in the apps, then setup runs again.
        why=first_problem(ctx.config)
        ports = app_ports(MEDIA_APPS)
        guided_setup.checkpoint(ctx, 'Finish media provider setup', [
            'Read the onboarding results above to identify the remaining configuration.',
            'Sign into the relevant opened applications using their saved app accounts. '
            'Finish selected provider/indexer credentials or any provider verification in its own page.',
            'Run  sudo bash install.sh  again afterwards: it checks the configuration again.'
        ], [('url', 'http://127.0.0.1:' + str(ports[a]) + '/')
            for a in ctx.config.get('media', {}).get('services', []) if a in ports])
        ctx.log('Media deployed; connecting the apps is not finished: '+why)
        return {'status':'needs_setup','reason':'Connecting the media apps is not finished: '+why+'. Fix that, then run  sudo bash install.sh  again'}
    ctx.log('Onboarding configuration checks complete')
    witness_playback(ctx)


# Said for the connector's row, which has no reason of its own: its messages are above.
CONNECTOR='the app connections (Prowlarr, the download client or the library folders; the messages above say which)'


def first_problem(config):
    """The first check onboarding's report (reports/onboarding.json on the SSD) did not
    pass, with its reason; a failed one before one still to set up. Tests only a person
    can do (a real playback) are left out: they never hold setup up."""
    from media_readiness import WITNESSED
    try:
        rows=json.loads((Path(config['data_mount'])/'reports'/'onboarding.json').read_text()).get('checks',[])
    except (OSError,ValueError,AttributeError):
        rows=[]
    rows=[r for r in rows if isinstance(r,dict) and r.get('status')!='pass' and r.get('check') not in WITNESSED]
    for wanted in ('fail','needs_setup'):
        row=next((r for r in rows if r.get('status')==wanted),None)
        if row is not None:return problem_line(row)
    return 'see the messages above'


def problem_line(row):
    """One report row said as "check: reason"."""
    if row.get('check')=='connector':return CONNECTOR
    return str(row.get('check'))+(': '+str(row['reason']) if row.get('reason') else '')


def playback_record(config):
    return Path(config['data_mount']) / 'reports/onboarding/playback.json'


def witness_playback(ctx, checkpoint=None):
    """Ask once for a real playback test. It is evidence only a person can give, so it
    never blocks setup: a yes is recorded, otherwise the finishing screen says it is
    still to do."""
    record = playback_record(ctx.config)
    if record.exists():
        return
    services = ctx.config.get('media', {}).get('services', [])
    try:
        done = (checkpoint or guided_setup.checkpoint)(ctx, 'Test playback (optional)', [
            'Using media you are allowed to use, request or add something, let it download and import, '
            'and play it in Jellyfin' + (' (and a song in Navidrome)' if 'navidrome' in services else '') + '.',
            'Answer yes only after it actually played. Press Ctrl+C to skip; setup still finishes '
            'and reminds you that playback is untested.'
        ], [('url', f"http://127.0.0.1:{app_port('jellyfin')}/")])
    except KeyboardInterrupt:
        done = False
    if done:
        atomic_json(record, {'confirmed_at': time.time(), 'evidence': 'user_reported'})
        ctx.log('Playback test confirmed')
    else:
        ctx.log('Playback verification pending (optional): test a real playback when convenient')
