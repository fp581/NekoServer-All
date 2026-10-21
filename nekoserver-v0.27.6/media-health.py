#!/usr/bin/env python3
"""Read-only NekoServer media integration audit; never logs API keys."""
import argparse, json, re, subprocess, sys, urllib.request, urllib.error, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from neko_common import MEDIA_APPS, app_ports, atomic_json, local_opener, safe_path, verify_storage
import media_readiness

OPENER=local_opener()

ROOT=Path(__file__).resolve().parent
PORTS=app_ports(MEDIA_APPS)
ARR={'sonarr':'v3','radarr':'v3','lidarr':'v1','prowlarr':'v1'}

def docker(*args):
    p=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=15,check=False)
    if p.returncode: raise RuntimeError('Docker inspection unavailable')
    return p.stdout

def inspect(name):
    ids=docker('ps','-aq','--filter',f'label=com.docker.compose.project=neko-{name}', '--filter',f'label=com.docker.compose.service={name}').splitlines()
    if len(ids)!=1: return {'state':'missing','health':'unknown','network':False,'mounts':[]}
    item=json.loads(docker('inspect',ids[0]))[0]
    return {'state':item['State']['Status'],'health':item['State'].get('Health',{}).get('Status','not_defined'),
            'network':'nekoserver-media' in item['NetworkSettings']['Networks'],
            'mounts':[m['Source'] for m in item.get('Mounts',[])]}

def http_check(url,key=None):
    headers={'User-Agent':'NekoServer-MediaHealth/1.0'}
    if key: headers['X-Api-Key']=key
    req=urllib.request.Request(url,headers=headers)
    try:
        with OPENER.open(req,timeout=4) as r:
            r.read(1024)
            return r.status
    except urllib.error.HTTPError as e:
        code=e.code;e.close();return code
    except (OSError, ValueError):
        return None

def arr_key(path):
    try:config=safe_path(path/'config.xml')
    except RuntimeError:return None
    if not config.is_file() or config.is_symlink(): return None
    try:
        node=ET.parse(config).getroot().find('ApiKey')
        key=node.text.strip() if node is not None and node.text else ''
        return key if re.fullmatch(r'[A-Za-z0-9_-]{16,128}',key) else None
    except (ET.ParseError, OSError): return None

def fetch_arr(name,port,key,endpoint,version):
    url=f'http://127.0.0.1:{port}/api/{version}/{endpoint}'
    headers={'X-Api-Key':key,'User-Agent':'NekoServer-MediaHealth/1.0'}
    try:
        with OPENER.open(urllib.request.Request(url,headers=headers),timeout=5) as r:
            data=json.load(r)
            return data if isinstance(data,list) else []
    except urllib.error.HTTPError as exc:
        exc.close();return None
    except (OSError, ValueError, json.JSONDecodeError): return None

def configured(name,endpoint,items,selected):
    def fields(item):
        return {f.get('name'):f.get('value') for f in item.get('fields',[])}
    if endpoint=='rootfolder':
        expected={'sonarr':'/data/tv','radarr':'/data/movies','lidarr':'/data/music'}[name]
        return any(i.get('path')==expected and i.get('accessible',True) for i in items)
    if endpoint=='downloadclient':
        category={'sonarr':'tv','radarr':'movies','lidarr':'music'}[name]
        return any(i.get('enable') and str(i.get('implementation','')).lower()=='qbittorrent'
                   and fields(i).get('host')=='qbittorrent' and str(fields(i).get('port'))=='8080'
                   and fields(i).get('category')==category for i in items)
    if endpoint=='applications':
        expected=[a for a in ('sonarr','radarr','lidarr') if a in selected]
        return all(any(i.get('syncLevel') in ('addOnly','fullSync')
                       and fields(i).get('baseUrl','').rstrip('/')==f'http://{a}:{PORTS[a]}'
                       and fields(i).get('prowlarrUrl','').rstrip('/')=='http://prowlarr:9696'
                       and bool(fields(i).get('apiKey')) for i in items) for a in expected)
    if endpoint=='indexer':
        return any(i.get('enable') if name=='prowlarr' else
                   i.get('enableRss') or i.get('enableAutomaticSearch') or i.get('enableInteractiveSearch') for i in items)
    return bool(items)


def audit(config, probe=http_check):
    selected=config.get('media',{}).get('services',list(PORTS))
    out={'generated_at':datetime.now(timezone.utc).isoformat(),'services':{},'integrations':{},'summary':{}}
    base=Path(config['data_mount'])/'appdata'
    for name in selected:
        if name not in PORTS: continue
        try: info=inspect(name)
        except (RuntimeError,subprocess.TimeoutExpired,ValueError,KeyError): info={'state':'unknown','health':'unknown','network':False,'mounts':[]}
        port=PORTS[name]
        status=probe(f'http://127.0.0.1:{port}/') if info['state']=='running' else None
        mounts=info['mounts']
        app_mount=base/name
        data_ok=any(Path(m)==app_mount for m in mounts)
        media_required=name in ('jellyfin','navidrome','sonarr','radarr','lidarr','qbittorrent')
        media_ok=(not media_required or name=='lidarr' or any(Path(m)==Path(config['media_mount'])/'library' or (Path(config['media_mount'])/'library') in Path(m).parents for m in mounts))
        if name=='navidrome':media_ok=str(Path(config['data_mount'])/'library/music') in mounts
        if name=='qbittorrent':media_ok=media_ok and str(Path(config['data_mount'])/'library/downloads/music') in mounts
        if name=='lidarr':media_ok=str(Path(config['data_mount'])/'library') in mounts
        level=('fail' if info['state']!='running' or not info['network'] or not data_ok or not media_ok or info['health']=='unhealthy' or status is None or status>=500 else
               'warning' if status>=400 and status not in (401,403) else 'pass')
        out['services'][name]={'result':level,'container':info['state'],'docker_health':info['health'],
             'network':info['network'],'appdata_mount':data_ok,'media_mount':media_ok,'http_status':status}
    for name,version in ARR.items():
        if name not in selected: continue
        key=arr_key(base/name)
        if not key:
            out['integrations'][name]={'result':'needs_setup','reason':'Application API key unavailable; configure application and rerun'}
            continue
        status=probe(f'http://127.0.0.1:{PORTS[name]}/api/{version}/system/status',key)
        record={'result':'pass' if status==200 else 'fail','api_status':status}
        if status==200:
            # Inspect expected routing without serializing provider records/secrets.
            endpoints=('downloadclient','indexer','rootfolder') if name!='prowlarr' else ('applications','indexer')
            for endpoint in endpoints:
                items=fetch_arr(name,PORTS[name],key,endpoint,version)
                valid=None if items is None else configured(name,endpoint,items,selected)
                record[endpoint+'_configured']=valid
                if valid is None: record['result']='warning'
                elif not valid and record['result']=='pass': record['result']='needs_setup'
            record['note']='Configuration checks only; live provider tests and imports/playback still required'
        out['integrations'][name]=record
    # Some integrations require application-specific authentication and must be verified during onboarding.
    try:checks=media_readiness.check(config)
    except (OSError,RuntimeError,ValueError,TypeError,KeyError):checks=[{'check':'current_readiness','status':'fail','reason':'Current readiness checks could not complete'}]
    out['readiness']={r['check']:{'result':r['status'],**({'reason':r['reason']} if r.get('reason') else {})} for r in checks}
    counts={s:sum(r['result']==s for group in ('services','integrations','readiness') for r in out[group].values()) for s in ('pass','warning','fail','needs_setup')}
    out['summary']=counts
    return out

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',default=str(ROOT/'config.json'))
    ap.add_argument('--json-out',type=Path)
    args=ap.parse_args(argv)
    try:
        config=json.loads(safe_path(args.config).read_text())
        verify_storage(config)
        report=audit(config)
        if args.json_out:
            verify_storage(config)
            atomic_json(args.json_out,report)
        print('NekoServer media health:',report['summary'])
        for category in ('services','integrations','readiness'):
            for name,rec in report[category].items(): print(f'  {category}/{name}: {rec["result"]}')
        return 2 if report['summary']['fail'] else 1 if report['summary']['needs_setup'] or report['summary']['warning'] else 0
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.TimeoutExpired) as e:
        print('Media audit failed:',type(e).__name__,file=sys.stderr)
        return 2
if __name__=='__main__': raise SystemExit(main())
