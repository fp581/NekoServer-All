#!/usr/bin/env python3
"""NekoServer media onboarding: safe orchestration of the existing connector.

Runs locally, never exports secrets, and distinguishes configuration from validation.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from neko_common import MEDIA_APPS, app_ports, safe_path, atomic_json
import media_libraries
import seerr_providers
import media_readiness

APPS = ('jellyfin','navidrome','sonarr','radarr','lidarr','prowlarr','seerr','qbittorrent')
PORTS = app_ports(MEDIA_APPS)
LIBRARIES = {'movies':'/data/movies', 'tv':'/data/tv', 'music':'/music'}

def mount_ok(path):
    return Path(path).is_dir() and not Path(path).is_symlink() and os.path.ismount(path)

def report(root, media, check_http=True, apps=APPS):
    import urllib.request
    results = []
    for app in apps:
        cfg = safe_path(root/'appdata'/app)
        results.append({'check':f'appdata:{app}', 'status':'pass' if cfg.is_dir() and not cfg.is_symlink() else 'needs_setup'})
        if check_http:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{PORTS[app]}/',timeout=3) as res:
                    status = 'pass' if res.status < 500 else 'fail'
            except Exception:
                status='needs_setup'
            results.append({'check':f'http:{app}', 'status':status})
    for cat in ('movies','tv','music','downloads'):
        p=safe_path((root if cat=='music' else media)/'library'/cat)
        results.append({'check':f'library:{cat}', 'status':'pass' if p.is_dir() and not p.is_symlink() else 'needs_setup'})
    return results

def safe_report(output, results):
    payload={'generated_utc':datetime.now(timezone.utc).isoformat(), 'checks':results,
             'note':'HTTP reachability does not verify API authentication, content acquisition, imports, or playback.'}
    atomic_json(output,payload)


def readiness_exit(findings):
    """2: a check failed; 1: an automatic check still needs setup; 0: every automatic
    check passed. Witnessed tests (a real playback) are reported but never counted:
    no rerun of this script could change them."""
    automatic=[x for x in findings if x.get('check') not in media_readiness.WITNESSED]
    if any(x['status']=='fail' for x in automatic):
        return 2
    if any(x['status']!='pass' for x in automatic):
        return 1
    return 0


def wait_ready(apps,seconds,probe=None,clock=time.monotonic,sleep=time.sleep):
    def default_probe(app):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORTS[app]}/',timeout=1) as response:return response.status<500
        except urllib.error.HTTPError as exc:return exc.code in (401,403)
        except (OSError,ValueError):return False
    probe=probe or default_probe
    deadline=clock()+seconds
    pending=set(apps)
    while pending:
        pending={app for app in pending if not probe(app)}
        if not pending or clock()>=deadline:break
        sleep(min(1,max(0,deadline-clock())))
    return sorted(pending)


def main(argv=None):
    p=argparse.ArgumentParser(description='NekoServer interactive media onboarding and status')
    p.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
    p.add_argument('--data-root')
    p.add_argument('--media-root')
    p.add_argument('--apply',action='store_true',help='Allow the selected setup/import steps to save changes')
    p.add_argument('--configure',action='store_true',help='Run Prowlarr catalog, app and download-client connector')
    p.add_argument('--libraries',action='store_true',help='Authenticate existing Jellyfin admin, create missing libraries, check Seerr link and sync libraries')
    p.add_argument('--request-providers',action='store_true',help='Test and provision absent Seerr Sonarr/Radarr request providers after Arr setup')
    p.add_argument('--indexers',action='store_true',help='Also offer indexer selection')
    p.add_argument('--report-only',action='store_true')
    p.add_argument('--skip-http',action='store_true')
    p.add_argument('--wait',type=int,default=0)
    p.add_argument('--sources',action='store_true',help='Collect source inventory on the SSD')
    p.add_argument('--import-sources',action='store_true',help='Copy accessible sources into HDD libraries; --apply enables copies')
    a=p.parse_args(argv)
    if a.apply and (not (a.configure or a.import_sources or a.libraries or a.request_providers) or a.report_only): p.error('--apply requires a setup/import action, without --report-only')
    if a.report_only and (a.libraries or a.request_providers):p.error('--report-only cannot configure libraries or request providers')
    if a.indexers and not a.configure: p.error('--indexers requires --configure')
    config=json.loads(Path(a.config).read_text())
    apps=tuple(config.get('media',{}).get('services',APPS))
    if not set(apps)<=set(APPS): p.error('Unknown media service in config')
    if not 0<=a.wait<=300:p.error('--wait must be 0..300')
    root=safe_path(a.data_root or config['data_mount']); media=safe_path(a.media_root or config['media_mount'])
    if not mount_ok(root) or not mount_ok(media) or root.resolve()==media.resolve():
        p.error('Expected two distinct mounted data/media filesystems; refusing to proceed')
    if not a.skip_http and a.wait:
        pending=wait_ready(apps,a.wait)
        if pending:print('Startup wait ended; still unavailable:',', '.join(pending))
    findings=report(root,media,not a.skip_http,apps)
    output=root/'reports'/'onboarding.json'
    safe_report(output,findings)
    for r in findings:
        print(f"{r['status']:>11}  {r['check']}")
    print('Report:',output)
    source_results=[]
    if a.report_only and (a.sources or a.import_sources):p.error('--report-only cannot configure or import sources')
    if a.sources:
        wizard=Path(__file__).with_name('source-wizard.py')
        result=subprocess.call([sys.executable,str(wizard),'--config',str(Path(a.config).resolve()),'--output',str(root/'config'/'media-sources.json')])
        source_results.append({'check':'source_inventory','status':'pass' if result==0 else 'fail'})
    if a.import_sources or a.sources:
        script=Path(__file__).with_name('source_import.py')
        cmd=[sys.executable,str(script),'--config',str(Path(a.config).resolve()),'--sources',str(root/'config'/'media-sources.json'),'--data-root',str(root),'--media-root',str(media)]
        if a.sources and not a.import_sources:cmd.append('--selected-only')
        if a.apply:cmd.append('--apply')
        result=subprocess.call(cmd)
        source_results.append({'check':'source_import','status':'pass' if result==0 else 'needs_setup' if result==1 else 'fail'})
    findings+=source_results
    library_results=[]
    if a.libraries:
        library_results=media_libraries.configure(root,media,a.apply,apps)
        if a.request_providers:library_results=[r for r in library_results if r['check']!='seerr:request_providers']
        findings+=library_results
        for r in library_results:print(f"{r['status']:>11}  {r['check']}" + (': '+r['reason'] if r.get('reason') else ''))
    if a.report_only or not a.configure:
        if a.request_providers:
            provider_results=seerr_providers.configure(root,a.apply,apps)
            findings+=provider_results
            for r in provider_results:print(f"{r['status']:>11}  {r['check']}" + (': '+r['reason'] if r.get('reason') else ''))
        print('Library/source steps complete. Use --configure for Arr/download-client setup.' if a.libraries or a.import_sources or a.sources else 'No API configuration changed. Use --configure or --libraries for interactive setup.')
        if a.skip_http and set(apps)&{'jellyfin','navidrome','seerr'}:
            findings.append({'check':'accounts_libraries_playback','status':'needs_setup','reason':'Authenticated accounts, libraries and playback still require server validation'})
        if not a.skip_http:findings+=media_readiness.check({**config,'data_mount':str(root),'media_mount':str(media)})
        safe_report(output,findings)
        return readiness_exit(findings)
    script=Path(__file__).with_name('media-connect.py')
    if not script.is_file():
        p.error('media-connect.py is missing')
    cmd=[sys.executable,str(script),'--config',str(Path(a.config).resolve()),'--data-root',str(root)]
    managers={'sonarr','radarr','lidarr'} & set(apps)
    if managers:cmd+=['--root-folders','--media-root',str(media)]
    if managers and 'prowlarr' in apps: cmd.append('--applications')
    if managers and 'qbittorrent' in apps: cmd.append('--download-clients')
    has_connector=any(x in cmd for x in ('--applications','--download-clients','--root-folders')) or a.indexers
    if not has_connector and not (a.libraries or a.request_providers):
        # Only apps with nothing to connect (Navidrome, or qBittorrent on its own): their
        # account and readiness checks below are all there is to do.
        print('No app connections apply to the selected apps; checking their accounts and readiness.')
    if a.indexers:cmd.append('--indexers')
    if a.apply:cmd.append('--apply')
    result=0
    if has_connector:
        print('Configuring selected Arr root folders, Prowlarr and download-client connections.')
        print('Indexer selection is optional; existing sources will be preserved.')
        result=subprocess.call(cmd)
    provider_results=[]
    if a.request_providers:
        provider_results=seerr_providers.configure(root,a.apply,apps)
        for r in provider_results:print(f"{r['status']:>11}  {r['check']}" + (': '+r['reason'] if r.get('reason') else ''))
    findings=report(root,media,not a.skip_http,apps)
    findings+=source_results
    findings+=library_results
    findings+=provider_results
    if has_connector:findings.append({'check':'connector','status':'pass' if result==0 else 'fail'})
    if a.skip_http:findings.append({'check':'end_to_end_media','status':'needs_setup','reason':'Complete remaining accounts/request providers and verify imports/playback on the server'})
    if not a.skip_http:findings+=media_readiness.check({**config,'data_mount':str(root),'media_mount':str(media)})
    safe_report(output,findings)
    return max(readiness_exit(findings),2 if result else 0)

if __name__=='__main__':
    try: sys.exit(main())
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        print('Onboarding failed:',type(exc).__name__,file=sys.stderr);sys.exit(2)
