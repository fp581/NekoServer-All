#!/usr/bin/env python3
"""Safe NekoServer media checks; --live inspects Docker only, never changes services."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVICES = ('jellyfin','navidrome','sonarr','radarr','lidarr','prowlarr','seerr','qbittorrent')
LINKS = {
    'Seerr -> Jellyfin': ('seerr','jellyfin','8096'),
    'Seerr -> Sonarr': ('seerr','sonarr','8989'),
    'Seerr -> Radarr': ('seerr','radarr','7878'),
    'Prowlarr -> Sonarr': ('prowlarr','sonarr','8989'),
    'Prowlarr -> Radarr': ('prowlarr','radarr','7878'),
    'Prowlarr -> Lidarr': ('prowlarr','lidarr','8686'),
    'Sonarr -> qBittorrent': ('sonarr','qbittorrent','8080'),
    'Radarr -> qBittorrent': ('radarr','qbittorrent','8080'),
    'Lidarr -> qBittorrent': ('lidarr','qbittorrent','8080'),
}

def command(*cmd):
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'Failed: {cmd}')
    return result.stdout.strip()

def inspect(name):
    try:
        obj = json.loads(command('docker','inspect', f'neko-{name}-{name}-1'))[0]
    except Exception:
        # Compose container names are not contractual; locate using compose labels.
        ids = command('docker','ps','-aq','--filter',f'label=com.docker.compose.project=neko-{name}', '--filter',f'label=com.docker.compose.service={name}').splitlines()
        if len(ids) != 1:
            return {'status':'absent','network':False,'mounts':[]}
        obj = json.loads(command('docker','inspect',ids[0]))[0]
    state=obj['State']
    health=state.get('Health',{}).get('Status')
    return {'status':state.get('Status'), 'health':health, 'network':'nekoserver-media' in obj['NetworkSettings']['Networks'],
            'mounts':[m['Source'] for m in obj['Mounts']]}

def analyze(config, records):
    issues=[]
    data=config['data_mount']; media=config['media_mount']
    for name,r in records.items():
        if r['status'] != 'running': issues.append(f'{name}: not running ({r["status"]})')
        if not r['network']: issues.append(f'{name}: missing shared network')
        if r.get('health') == 'unhealthy': issues.append(f'{name}: unhealthy')
        sources=r.get('mounts',[])
        if not any(Path(x)==Path(data)/'appdata'/name for x in sources): issues.append(f'{name}: appdata not mounted from SSD')
        if name=='navidrome' and str(Path(data)/'library/music') not in sources:
            issues.append(f'{name}: music not mounted from SSD')
        if name=='qbittorrent' and str(Path(data)/'library/downloads/music') not in sources:
            issues.append(f'{name}: music downloads not mounted from SSD')
        if name=='lidarr' and str(Path(data)/'library') not in sources:
            issues.append(f'{name}: SSD music library not mounted')
        if name in ('jellyfin','sonarr','radarr','qbittorrent') and not any(x.startswith(media+'/library/') or x == media+'/library' for x in sources):
            issues.append(f'{name}: media not mounted from HDD')
    return issues

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default=str(ROOT/'config.json'))
    parser.add_argument('--live',action='store_true', help='Read-only Docker inspection')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text())
    selected=config['media']['services']
    print('NekoServer media integration audit')
    print('Configured:', ', '.join(selected))
    print('Docker network: nekoserver-media (shared network, separate Compose projects)')
    for k,(a,b,port) in LINKS.items():
        if a in selected and b in selected:
            print(f'  {k}: http://{b}:{port} (API key or account setup required)')
    if not args.live:
        print('STATIC ONLY: run --live on the server to check actual containers. API credentials and cross-app authorization still require onboarding.')
        return 0
    try:
        records={n:inspect(n) for n in selected}
        issues=analyze(config,records)
    except (RuntimeError, OSError, ValueError, KeyError) as e:
        print('CHECK ERROR:',e)
        return 2
    for name,record in records.items():
        print(f'  {name}: {record["status"]}, network={record["network"]}, health={record.get("health","not-defined")}')
    for issue in issues: print('ISSUE:',issue)
    print('IMPORTANT: Docker inspection cannot verify API keys or application-level integrations.')
    return 1 if issues else 0
if __name__=='__main__': raise SystemExit(main())
