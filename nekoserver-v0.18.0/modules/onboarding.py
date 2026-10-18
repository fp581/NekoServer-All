"""Run onboarding from the persistent installer after media deployment."""
import os
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
        ctx.log('Media deployed; onboarding has remaining account/provider/playback tasks')
        return {'status':'needs_setup','reason':'Complete application onboarding and playback checks; rerun the SSD onboarding script'}
    ctx.log('Onboarding configuration/readiness checks complete')
