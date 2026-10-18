"""Run onboarding from the persistent installer after media deployment."""
import guided_setup
import account_store
import subprocess
import sys
from pathlib import Path
from neko_common import VERSION, safe_path

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
    wait=int(settings.get('wait_seconds',60))
    if not 0<=wait<=300:raise RuntimeError('Onboarding wait must be 0..300 seconds')
    cmd=[sys.executable,str(script),'--config',str(config),'--wait',str(wait)]
    if sys.stdin.isatty():
        cmd+=['--configure','--apply']
        if settings.get('configure_libraries',True) and 'jellyfin' in ctx.config.get('media',{}).get('services',[]):cmd.append('--libraries')
        services=ctx.config.get('media',{}).get('services',[])
        if settings.get('configure_request_providers',True) and 'seerr' in services and set(services)&{'sonarr','radarr'}:cmd.append('--request-providers')
        if settings.get('collect_sources',True):cmd.append('--sources')
        if settings.get('configure_indexers',True) and 'prowlarr' in ctx.config.get('media',{}).get('services',[]):cmd.append('--indexers')
    else:cmd.append('--report-only')
    result=subprocess.run(cmd,check=False).returncode
    if result==2:raise RuntimeError('Onboarding has failures; see SSD reports and rerun onboard.py')
    if result:
        ports = {'jellyfin':8096, 'navidrome':4533, 'sonarr':8989, 'radarr':7878,
                 'lidarr':8686, 'prowlarr':9696, 'seerr':5055, 'qbittorrent':8080}
        guided_setup.checkpoint(ctx, 'Finish media provider and playback setup', [
            'Read the onboarding results above to identify the remaining configuration.',
            'Sign into the relevant opened applications using their saved app accounts. '
            'Finish selected provider/indexer credentials or any provider verification in its own page.',
            'Using permitted media, test a request, download, import and playback. '
            'Also test music playback if selected.',
            'Your confirmation does not replace pending machine checks. Reapply onboarding '
            'after fixing configuration so the installer can check it again.'
        ], [('url', 'http://127.0.0.1:' + str(ports[a]) + '/')
            for a in ctx.config.get('media', {}).get('services', []) if a in ports])
        ctx.log('Media deployed; onboarding has remaining account/provider/playback tasks')
        return {'status':'needs_setup','reason':'Complete application onboarding and playback checks; rerun the SSD onboarding script'}
    ctx.log('Onboarding configuration/readiness checks complete')
