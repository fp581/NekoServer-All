#!/usr/bin/env python3
"""Bounded, read-only health probes. Publish only a sanitized local dashboard snapshot."""
import acceptance_summary
import argparse,fcntl,json,math,os,re,shutil,subprocess,time,itertools
from pathlib import Path
from neko_common import atomic_json,atomic_write,safe_path,mount_path,VERSION
APPS={'jellyfin':('Jellyfin','Movies & shows',8096),'immich':('Immich','Photos & memories',2283),'seerr':('Seerr','Discover & request',5055),'navidrome':('Navidrome','Your music library',4533),'sonarr':('Sonarr','Series automation',8989),'radarr':('Radarr','Movie automation',7878),'lidarr':('Lidarr','Music automation',8686),'prowlarr':('Prowlarr','Media sources',9696),'qbittorrent':('qBittorrent','Download queue',8080),'nextcloud':('Nextcloud','Your personal files',8088)}
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

def cpu_read(proc=Path('/proc')):
    parts=(proc/'stat').read_text().splitlines()[0].split()[1:9];values=[int(x) for x in parts]
    return {'total':sum(values),'idle':values[3]+values[4],'boot':(proc/'sys/kernel/random/boot_id').read_text().strip()}

def cpu_percent(current,previous):
    if not isinstance(previous,dict) or current.get('boot')!=previous.get('boot'):return None
    try:
        total=current['total']-previous['total'];idle=current['idle']-previous['idle']
        return round(100*(total-idle)/total,1) if total>0 and 0<=idle<=total else None
    except (KeyError,TypeError):return None

def metrics(previous,proc=Path('/proc'),sys=Path('/sys')):
    result={'cpu_percent':None,'memory_percent':None,'memory_used':None,'memory_total':None,'temperature':None,'uptime_seconds':None,'cpu_model':None};current={}
    try:current=cpu_read(proc);result['cpu_percent']=cpu_percent(current,previous)
    except (OSError,ValueError,IndexError):pass
    try:
        mem={line.split(':')[0]:int(line.split()[1])*1024 for line in (proc/'meminfo').read_text().splitlines() if ':' in line}
        total=mem['MemTotal'];used=total-mem['MemAvailable']
        if 0<=used<=total and total>0:result.update(memory_total=total,memory_used=used,memory_percent=round(100*used/total,1))
    except (OSError,ValueError,KeyError,IndexError):pass
    try:result['uptime_seconds']=number(float((proc/'uptime').read_text().split()[0]))
    except (OSError,ValueError,IndexError):pass
    try:
        for line in (proc/'cpuinfo').read_text().splitlines():
            if line.startswith('model name'):result['cpu_model']=line.split(':',1)[1].strip()[:100];break
    except (OSError,IndexError):pass
    readings=[]
    for hw in (sys/'class/hwmon').glob('hwmon*'):
        try:
            name=(hw/'name').read_text().strip()
            if name not in ('coretemp','k10temp','zenpower'):continue
            for sensor in hw.glob('temp*_input'):
                value=float(sensor.read_text())/1000
                if -10<=value<=150:readings.append(value)
        except (OSError,ValueError):continue
    if readings:result['temperature']=round(max(readings),1)
    return result,current

def mount_info(path,expected=None,fstype=None):
    if expected is not None and (not isinstance(expected,str) or not expected):return {'status':'not_configured'}
    code,text=run(['findmnt','-n','-o','UUID,FSTYPE,SOURCE','--mountpoint',str(path)])
    fields=text.strip().split()
    if code!=0 or len(fields)<3:return {'status':'missing'}
    if expected and (fields[0]!=expected or fields[1]!=fstype):return {'status':'wrong_drive'}
    return {'status':'mounted','source':fields[2].split('[')[0]}

def physical_device(source):
    if not isinstance(source,str) or not re.fullmatch(r'/dev/(sd[a-z]+[0-9]*|nvme[0-9]+n[0-9]+(?:p[0-9]+)?|vd[a-z]+[0-9]*)',source):return None
    code,text=run(['lsblk','-n','-o','PKNAME',source]);parent=text.strip().splitlines()
    value='/dev/'+parent[0] if code==0 and parent and parent[0] else source
    return value if re.fullmatch(r'/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+)',value) else None

def smart_summary(data,code):
    result={'status':'unknown','temperature':None,'wear_percent':None,'reason':'SMART unavailable or drive sleeping; no wake requested'}
    health=data.get('smart_status',{});passed=health.get('passed') if isinstance(health,dict) else None
    if type(passed) is not bool:return result
    result.update(status='pass' if passed else 'fail',reason='SMART health passed' if passed else 'SMART health failed')
    temp_info=data.get('temperature',{});temp=temp_info.get('current') if isinstance(temp_info,dict) else None;result['temperature']=number(temp,0,150)
    nvme=data.get('nvme_smart_health_information_log',{})
    if isinstance(nvme,dict):
        result['wear_percent']=number(nvme.get('percentage_used'),0,1000)
        if number(nvme.get('critical_warning'),0,255) not in (None,0):result.update(status='fail',reason='NVMe critical warning')
        elif result['status']=='pass' and result['wear_percent'] is not None and result['wear_percent']>=90:result.update(status='warning',reason='NVMe endurance usage is high')
    attributes=data.get('ata_smart_attributes',{});table=attributes.get('table',[]) if isinstance(attributes,dict) else []
    if isinstance(table,list):
        for item in table:
            if not isinstance(item,dict):continue
            raw_info=item.get('raw',{});raw=number(raw_info.get('value')) if isinstance(raw_info,dict) else None
            if raw and item.get('id') in (197,198):result.update(status='fail',reason='Pending or uncorrectable sectors')
            elif raw and item.get('id')==5 and result['status']=='pass':result.update(status='warning',reason='Reallocated sectors detected')
    if type(code) is int and code & 0b11111000 and result['status']=='pass':result.update(status='warning',reason='SMART reports historical/current health errors; inspect the disk')
    return result

def smart(device,identity,cache,now,interval=1800):
    if not device:return {'status':'unknown','reason':'Physical disk mapping unavailable','checked_at':None}
    key=identity+'|'+device;old=cache.get(key,{})
    if isinstance(old,dict) and old.get('status') in ('pass','warning','fail','unknown') and number(old.get('checked_at'),0,now) is not None and now-old['checked_at']<interval:
        reasons={'SMART health passed','SMART health failed','NVMe critical warning','NVMe endurance usage is high','Pending or uncorrectable sectors','Reallocated sectors detected','SMART reports historical/current health errors; inspect the disk','SMART unavailable or drive sleeping; no wake requested'}
        return {'status':old['status'],'temperature':number(old.get('temperature'),0,150),'wear_percent':number(old.get('wear_percent'),0,1000),'checked_at':old['checked_at'],'reason':old.get('reason') if isinstance(old.get('reason'),str) and old['reason'] in reasons else 'Inspect drive health'}
    code,text=run(['smartctl','--json','--health','--attributes','--nocheck=standby',device],15)
    try:data=json.loads(text)
    except ValueError:data={}
    result=smart_summary(data if isinstance(data,dict) else {},code);result['checked_at']=now;cache[key]=result
    return result

def storage(config,cache,now):
    disks=[];data_ok=False
    rows=[('os','OS SSD',Path('/'),None,None),('data_ssd','Application SSD',mount_path(config['data_mount']),config['storage']['data_ssd']['uuid'],config['storage']['data_ssd']['fstype']),('media_hdd','Media HDD',mount_path(config['media_mount']),config['storage']['media_hdd']['uuid'],config['storage']['media_hdd']['fstype'])]
    for key,label,path,expected,fs in rows:
        info=mount_info(path,expected,fs);disk={'id':key,'name':label,'mount':str(path),'status':info['status'],'total':None,'used':None,'used_percent':None,'smart':{'status':'unknown','checked_at':None}}
        if info['status']=='mounted':
            try:
                usage=shutil.disk_usage(path);disk.update(total=usage.total,used=usage.used,used_percent=round(100*usage.used/usage.total,1))
            except OSError:disk['status']='unavailable'
            device=physical_device(info.get('source'));disk['smart']=smart(device,expected or info.get('source','os'),cache,now,config.get('dashboard',{}).get('smart_interval_seconds',1800))
        if key=='data_ssd':data_ok=disk['status']=='mounted'
        disks.append(disk)
    return disks,data_ok

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

def media_report(config,now,data_ok):
    if config.get("monitoring",{}).get("enabled",True) is False:return {"status":"disabled","counts":{},"checks":[],"age_seconds":None}
    saved=read_json(Path(config['data_mount'])/'reports/media/latest.json') if data_ok else {}
    try:
        from datetime import datetime
        age=max(0,now-datetime.fromisoformat(saved.get('generated_at','').replace('Z','+00:00')).timestamp())
    except (ValueError,TypeError):age=None
    result={'status':'unknown','age_seconds':age,'counts':{},'checks':[]}
    if age is not None:result['status']='stale' if age>max(7200,config.get('monitoring',{}).get('interval_minutes',60)*120) else 'current'
    for group in ('services','integrations','readiness'):
        values=saved.get(group,{})
        if not isinstance(values,dict):continue
        for key,item in list(values.items())[:50]:
            if not isinstance(key,str) or not re.fullmatch(r'[a-z0-9_:]+',key) or not isinstance(item,dict):continue
            state=item.get('result')
            if state not in ('pass','warning','fail','needs_setup'):continue
            result['checks'].append({'name':key.replace('_',' ').replace(':',' · '),'status':state});result['counts'][state]=result['counts'].get(state,0)+1
    return result

def recovery_status(config,data_ok,media_ok=True):
    result={'status':'needs_setup','reason':'Full restore acceptance still required','latest_rehearsal':None,'checkpoint':False}
    if data_ok:result['checkpoint']=read_json(Path(config['data_mount'])/'installer/host-checkpoint.json').get('format')=='neko-host-checkpoint-v1'
    candidates=[]
    for root in (Path(config['data_mount']),Path(config['media_mount'])):
        if (root==Path(config['data_mount']) and not data_ok) or (root==Path(config['media_mount']) and not media_ok):continue
        try:
            folder=safe_path(root/'recovery-tests')
            if not folder.is_dir():continue
            for item in itertools.islice(folder.iterdir(),100):
                report=safe_path(item/'restore-report.json')
                if report.is_file():candidates.append((report.stat().st_mtime,report))
        except (OSError,RuntimeError):continue
    if candidates:
        when,path=max(candidates);saved=read_json(path,64000)
        if saved.get('status')=='needs_setup':result['latest_rehearsal']={'at':when,'database_tables':number(saved.get('database_tables'),0,100000),'http_ready':saved.get('http_ready') is True}
    return result

def alerts(snapshot,now):
    out=[]
    def add(id,level,title,detail):out.append({'id':id,'level':level,'title':title,'detail':detail})
    for disk in snapshot['storage']:
        if disk['status']!='mounted':add('mount:'+disk['id'],'critical',disk['name']+' unavailable','Mount missing or identity mismatch. Check the drive before restarting dependent services.')
        elif disk.get('used_percent') is not None and disk['used_percent']>=90:add('space:'+disk['id'],'critical' if disk['used_percent']>=97 else 'warning',disk['name']+' is filling up','Review free space before uploads or backups.')
        state=disk['smart'].get('status')
        if state in ('fail','warning'):add('smart:'+disk['id'],'critical' if state=='fail' else 'warning',disk['name']+' health warning',disk['smart'].get('reason','Inspect drive health.'))
    used=snapshot['metrics'].get('memory_percent')
    if used is not None and used>=90:add('memory-pressure','critical' if used>=97 else 'warning','Available RAM is low','Inspect application memory usage and swap; cached memory alone is normal.')
    temp=snapshot['metrics'].get('temperature')
    if temp is not None and temp>=80:add('temperature','critical' if temp>=95 else 'warning','CPU temperature is high','Check cooling and current workload.')
    for app in snapshot['applications']:
        if app['state'] in ('exited','restarting','dead','missing','ambiguous','paused','created') or app['health']=='unhealthy':add('app:'+app['id'],'critical' if app['health']=='unhealthy' else 'warning',app['name']+' needs attention','Check its container and local application logs.')
    for job in snapshot['backups']:
        if job['status'] in ('fail','stale'):add('backup:'+job['id'],'critical' if job['status']=='fail' else 'warning',job['name']+' needs attention',job['reason'] or 'Review the backup schedule.')
    if snapshot['media']['status']=='stale':add('media-stale','warning','Media checks are stale','Reapply media_monitor or inspect its scheduled job.')
    if snapshot['media']['counts'].get('fail',0):add('media-failure','warning','Media connection check failed','Open the media checks below and inspect the relevant service.')
    return out

def collect(config,public,internal,now=None):
    now=time.time() if now is None else now;public=safe_path(public);internal=safe_path(internal)
    for key in ('data_mount','media_mount','external_backup_mount'):mount_path(config[key])
    expected=config['storage']['data_ssd'];data_probe=mount_info(config['data_mount'],expected['uuid'],expected['fstype'])
    previous=read_json(internal/'collector.json') if data_probe['status']=='mounted' else {}
    old_cache=previous.get('smart',{});cache=dict(list(old_cache.items())[-20:]) if isinstance(old_cache,dict) else {}
    disks,data_ok=storage(config,cache,now)
    host,current=metrics(previous.get('cpu',{}));history=previous.get('history',[])
    if not isinstance(history,list):history=[]
    history=[{'at':x['at'],'cpu':number(x.get('cpu'),0,100),'memory':number(x.get('memory'),0,100),'temperature':number(x.get('temperature'),-10,150)} for x in history[-119:] if isinstance(x,dict) and number(x.get('at'),0,now) is not None and now-x['at']<=7200]
    history.append({'at':now,'cpu':host['cpu_percent'],'memory':host['memory_percent'],'temperature':host['temperature']})
    result={'format':'neko-dashboard-v1','version':VERSION,'generated_at':now,'metrics':host,'history':history,'storage':disks,'applications':applications(config),'backups':backups(config,now,data_ok),'media':media_report(config,now,data_ok),'recovery':recovery_status(config,data_ok,any(d['id']=='media_hdd' and d['status']=='mounted' for d in disks)),'heartbeat':heartbeat_status(config,now),'acceptance':acceptance_summary.summarize(read_json(Path(config['data_mount'])/'reports/acceptance/latest.json') if data_ok else {},config,now)}
    result['notifications']=notification_status(now)
    result['alerts']=alerts(result,now)
    if data_ok:
        internal.mkdir(parents=True,exist_ok=True,mode=0o700);atomic_json(internal/'collector.json',{'cpu':current,'history':history,'smart':cache})
    public.parent.mkdir(parents=True,exist_ok=True,mode=0o755);atomic_write(public,json.dumps(result,separators=(',',':'))+'\n',0o644)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--output',default='/run/nekoserver-dashboard/status.json');p.add_argument('--state',required=True);a=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Collector requires root for local drive/container inspection')
    lockpath=Path(a.output).parent/'collector.lock';safe_path(lockpath).parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    with lockpath.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);collect(read_json(a.config),Path(a.output),Path(a.state))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Dashboard collection failed; inspect the local collector service')
