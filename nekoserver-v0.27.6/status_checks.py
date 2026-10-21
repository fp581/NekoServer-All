"""Small, bounded, read-only checks of this server (commands, saved reports, drives,
apps, backup schedules) used by setup, the gateway and the acceptance checker.
The dashboard itself is read by the Rust collector (runtime/src/collect.rs)."""
import json,math,os,subprocess
from pathlib import Path
from neko_common import APPS,safe_path
JOBS={'music':('Music library backup','nekoserver-music-backup',172800),'archive':('Photo archive','nekoserver-photo-preservation',10800),'database':('Immich database','nekoserver-immich-db-backup',172800),'external':('External photo copy','nekoserver-photo-backup',864000),'applications':('Media application snapshot','nekoserver-app-backup',172800),'host':('Host and user snapshot','nekoserver-host-backup',172800),'maintenance':('Monthly backup and updates','nekoserver-maintenance',3024000)}

def run(args,timeout=8):
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout,env={**os.environ,'LC_ALL':'C','TZ':'UTC'})
        return p.returncode,p.stdout[:2000000]
    except (OSError,subprocess.TimeoutExpired):return None,''

def read_json(path,limit=2000000):
    try:
        path=safe_path(path)
        if path.stat().st_size>limit:return {}
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,RuntimeError,ValueError):return {}

def number(value,lo=0,hi=1e18):
    return value if type(value) in (int,float) and math.isfinite(value) and lo<=value<=hi else None

def mount_info(path,expected=None,fstype=None):
    if expected is not None and (not isinstance(expected,str) or not expected):return {'status':'not_configured'}
    code,text=run(['findmnt','-n','-o','UUID,FSTYPE,SOURCE','--mountpoint',str(path)])
    fields=text.strip().split()
    if code!=0 or len(fields)<3:return {'status':'missing'}
    if expected and (fields[0]!=expected or fields[1]!=fstype):return {'status':'wrong_drive'}
    return {'status':'mounted','source':fields[2].split('[')[0]}

def applications(config):
    code,text=run(['docker','--host','unix:///var/run/docker.sock','ps','-a','--format','{{json .}}'],15);records=[]
    if code==0:
        for line in text.splitlines():
            try:
                row=json.loads(line);labels=dict(pair.split('=',1) for pair in row.get('Labels','').split(',') if '=' in pair)
                records.append((labels,row.get('State'),row.get('Status','')))
            except (ValueError,TypeError,AttributeError):continue
    selected=config.get('media',{}).get('services',[])
    if config.get('photos',{}).get('deploy_immich'):selected=[*selected,'immich']
    if config.get('nextcloud',{}).get('enabled'):selected=[*selected,'nextcloud']
    out=[]
    for app in APPS:
        if app not in selected:continue
        project='neko-'+app;service='immich-server' if app=='immich' else app
        found=[(state,status) for labels,state,status in records if labels.get('com.docker.compose.project')==project and labels.get('com.docker.compose.service')==service]
        state='unknown' if code!=0 else 'missing';health='unknown'
        if len(found)==1:
            state,status=found[0];state=state if state in ('running','exited','restarting','paused','created','dead') else 'unknown';health='unhealthy' if '(unhealthy)' in status else 'starting' if '(health: starting)' in status else 'healthy' if '(healthy)' in status else 'not_defined'
        elif len(found)>1:state='ambiguous'
        name,description,port=APPS[app];out.append({'id':app,'name':name,'description':description,'state':state,'health':health,'url':f'http://127.0.0.1:{port}/'})
    return out

def timer_status(unit):
    code,text=run(['systemctl','show',unit+'.timer','--property=LoadState,ActiveState,NextElapseUSecRealtime'])
    fields=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
    return {'installed':code==0 and fields.get('LoadState')=='loaded','active':fields.get('ActiveState')=='active','next_run':fields.get('NextElapseUSecRealtime','')[:80] or None}

def backups(config,now,data_ok):
    rows=[]
    for key,(name,unit,maximum) in JOBS.items():
        if key=='music' and config.get('music',{}).get('backup_enabled',True) is False:continue
        weekly=config.get('photos',{}).get('backup_schedule')=='weekly'
        report_key=key
        if weekly and key in ('archive','database'):
            maximum=864000;report_key='database';unit='nekoserver-immich-db-backup'
        interval=config.get('photos',{}).get('archive_interval_minutes',60)
        if key=='archive' and not weekly and type(interval) is int and 15<=interval<=10080:maximum=max(maximum,interval*120)
        saved=read_json(Path(config['data_mount'])/'reports/jobs'/f'{report_key}.json',64000) if data_ok else {}
        status=saved.get('status') if saved.get('status') in ('running','pass','fail') else 'unknown';success=number(saved.get('last_success'),0,now+60)
        timer=timer_status(unit);state=status
        started=number(saved.get('started_at'),0,now+60)
        if status=='running' and started is not None and now-started>maximum:state='stale'
        elif success is not None and now-success>maximum and status!='running':state='stale'
        rows.append({'id':key,'name':name,'status':state,'last_success':success,'last_attempt':number(saved.get('started_at'),0,now+60),'timer':timer,'reason':'Last run failed; inspect its local service logs' if status=='fail' else 'No recorded successful run' if success is None else 'Last success is overdue' if state=='stale' else None})
    return rows

def heartbeat_status(config,now):
    saved=read_json('/run/nekoserver-heartbeat/status.json',64000)
    timer=timer_status('nekoserver-heartbeat')
    stamp=number(saved.get('last_attempt'),0,now+30)
    state=saved.get('status') if saved.get('format')=='neko-heartbeat-v1' and saved.get('status') in ('pass','fail') else 'unknown'
    interval=config.get('heartbeat',{}).get('interval_seconds',300)
    if type(interval) is not int or not 60<=interval<=3600:interval=300
    if timer['installed'] and (stamp is None or now-stamp>interval+90):state='stale'
    return {'status':state,'timer':timer,'last_attempt':stamp,'last_success':number(saved.get('last_success'),0,now+30),'provider_notifications_verified':False}

def notification_status(now):
    saved=read_json('/run/nekoserver-notifications/delivery.json',64000)
    timer=timer_status('nekoserver-notifications')
    stamp=number(saved.get('generated_at'),0,now+30)
    state=saved.get('status') if saved.get('format')=='neko-delivery-v1' and saved.get('status') in ('pass','fail','unknown') else 'unknown'
    if timer['installed'] and (stamp is None or now-stamp>180):state='stale'
    return {'status':state,'timer':timer,'last_success':number(saved.get('last_success'),0,now+30),'last_attempt':number(saved.get('last_attempt'),0,now+30),'pending':number(saved.get('pending'),0,100)}
