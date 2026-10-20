#!/usr/bin/env python3
"""Read-only deployment inspection. --save writes a report; --manual records witnessed tests."""
import argparse,hashlib,importlib.util,json,os,platform,re,shlex,subprocess,sys,time,uuid
from pathlib import Path
import dashboard_collect as health
import installer,recovery_tools
import gateway_checks,native_runtime
import guided_setup
import permission_checks
import os_checks
from modules import docker as docker_module,tv
from neko_common import VERSION,safe_path,atomic_json
from acceptance_summary import scope_digest
from media_libraries import NoRedirect
import urllib.request

STATES={'pass','fail','needs_setup','warning','blocked'}
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
ROOT=Path(__file__).resolve().parent


def peer_port(inspect):
    """qBittorrent's TORRENTING_PORT from a container's environment, if set."""
    for item in (inspect.get('Config',{}).get('Env') or []):
        if isinstance(item,str) and item.startswith('TORRENTING_PORT='):
            value=item.split('=',1)[1]
            return int(value) if value.isdigit() else None
    return None


def peer_bindings(peer,config):
    """The only non-loopback publications allowed: the configured qBittorrent peer port."""
    if peer is None or peer!=config.get('media',{}).get('torrent_port'):return set()
    return {f'{peer}/tcp',f'{peer}/udp'}

def item(key,name,status,detail):
    if status not in STATES:raise ValueError('Invalid acceptance state')
    return {'id':key,'name':name,'status':status,'detail':detail}

def boot_id():
    try:
        value=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return value if re.fullmatch(r'[a-f0-9-]{36}',value) else None
    except OSError:return None

def fingerprint(config,profile):
    # Retain a digest, never arbitrary configuration values/credentials in the report.
    clean={k:config.get(k) for k in ('data_mount','media_mount','external_backup_mount','storage')}
    for group,keys in {'media':('services','uid','gid','bind_ip'),'photos':('deploy_immich','immich_version','external_backup_uuid','external_backup_fstype','archive_dir'),'dashboard':('port',),'runtime':('backend',),'networking':('share_mode','tailnet_only','samba_media_write','shares'),'gateway':('services','lan_ip','lan_cidr','confirmed_services'),'tv':('user','keep_awake','bluetooth'),'heartbeat':('interval_seconds',),'host_backup':('user','steam','samba','extra_paths','minimum_free_gb','time'),'server_os':('hdd_idle_minutes','hdd_policy','journal_disk_mb','journal_ram_mb','journal_retention_days')}.items():
        clean[group]={k:config.get(group,{}).get(k) for k in keys}
    clean['profile']=profile;clean['modules']=config['profiles'].get(profile)
    return hashlib.sha256(json.dumps(clean,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def retired_timer(unit):
    rc,out=health.run(['systemctl','show',unit,'--property=LoadState,ActiveState,UnitFileState'])
    fields=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
    return fields.get('LoadState')=='not-found' or (rc==0 and fields.get('ActiveState')=='inactive' and fields.get('UnitFileState') in ('disabled','masked','static'))


def service(unit,timer=False):
    rc,out=health.run(['systemctl','show',unit,'--property=LoadState,ActiveState,UnitFileState'])
    fields=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
    return rc==0 and fields.get('LoadState')=='loaded' and fields.get('ActiveState')=='active' and fields.get('UnitFileState') in ('enabled','enabled-runtime')

def load_json(path):return health.read_json(path)

def docker_info(config):
    rc,out=health.run(recovery_tools.DOCKER+['info','--format','{{json .}}'],15)
    try:value=json.loads(out)
    except ValueError:return False,None
    if rc!=0 or not isinstance(value,dict):return False,None
    valid=value.get('DockerRootDir')==str(Path(config['data_mount'])/'docker-engine') and value.get('Driver')=='overlay2' and 'io.containerd.snapshotter' not in json.dumps(value.get('DriverStatus'))
    return valid,value.get('ServerVersion') if isinstance(value.get('ServerVersion'),str) else None

def guard_check(config):
    try:
        text=safe_path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf').read_text()
        command=next(line.split('=',1)[1] for line in text.splitlines() if line.startswith('ExecStartPre='));args=shlex.split(command)
        if len(args)!=4 or args[2]!='--config':return False
        if args[1]!='storage-guard':return False
        script=safe_path(args[0]);runtime=script.parent
        if script.name!=native_runtime.NAME or script.read_bytes()!=native_runtime.payload(ROOT,config):return False
        root=Path(config['data_mount'])/'installer/guards'
        if runtime.parent!=root or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',runtime.name) or args[3]!=str(runtime/'storage.json'):return False
        if load_json(runtime/'storage.json')!={k:config[k] for k in ('data_mount','media_mount','storage')}:return False
        if text!=docker_module.guard_contents(config,runtime):return False
        rc,out=health.run(['systemctl','show','docker.service','--property=ExecStartPre,RequiresMountsFor,BindsTo'])
        props=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
        units=[line.split('=',1)[1] for line in text.splitlines() if line.startswith('BindsTo=')][0].split()
        return rc==0 and command in props.get('ExecStartPre','') and 'ignore_errors=no' in props.get('ExecStartPre','') and all(config[k] in props.get('RequiresMountsFor','').split() for k in ('data_mount','media_mount')) and all(u in props.get('BindsTo','').split() for u in units)
    except (OSError,RuntimeError,ValueError,KeyError,IndexError,StopIteration,subprocess.SubprocessError):return False

def containers(config,photos=False):
    specs=[]
    if photos:
        specs=[('immich',s) for s in ('immich-server','database','redis','immich-machine-learning')]
    else:specs=[(a,a) for a in config.get('media',{}).get('services',[]) if a in health.APPS and a!='immich']
    results=[]
    for app,svc in specs:
        rc,out=health.run(recovery_tools.DOCKER+['ps','-aq','--filter','label=com.docker.compose.project=neko-'+app,'--filter','label=com.docker.compose.service='+svc],15)
        ids=out.splitlines();state='fail';mounts=False;ports=False
        if rc==0 and len(ids)==1 and re.fullmatch(r'[a-f0-9]{12,64}',ids[0]):
            rc,out=health.run(recovery_tools.DOCKER+['inspect',ids[0]],15)
            try:
                values=json.loads(out);v=values[0] if rc==0 and isinstance(values,list) and len(values)==1 else {};labels=v.get('Config',{}).get('Labels',{});running=v.get('State',{}).get('Status')=='running';healthy=v.get('State',{}).get('Health',{}).get('Status','not_defined') not in ('unhealthy','starting');exact=labels.get('com.docker.compose.project')=='neko-'+app and labels.get('com.docker.compose.service')==svc
                binds=[m for m in v.get('Mounts',[]) if isinstance(m,dict) and m.get('Type')=='bind']
                def bound(source,targets,readonly=False):return any(m.get('Source')==source and m.get('Destination') in targets and (not readonly or m.get('RW') is False) for m in binds)
                if app=='immich':
                    expected=str(Path(config['data_mount'])/'photos/immich-library') if svc=='immich-server' else str(Path(config['data_mount'])/'appdata/immich/postgres') if svc=='database' else None
                    targets=('/data','/usr/src/app/upload') if svc=='immich-server' else ('/var/lib/postgresql/data',)
                    mounts=expected is None or bound(expected,targets)
                else:
                    destination='/app/config' if app=='seerr' else '/data' if app=='navidrome' else '/config'
                    mounts=bound(str(Path(config['data_mount'])/'appdata'/app),(destination,))
                    if app in ('jellyfin','navidrome','sonarr','radarr','lidarr','qbittorrent'):
                        library=Path(config['data_mount'] if app in ('navidrome','lidarr') else config['media_mount'])/'library'
                        needed=[('movies','/data/movies',True),('tv','/data/tv',True)] if app=='jellyfin' else [('music','/music',True)] if app=='navidrome' else [('', '/data',False)]
                        mounts=mounts and all(bound(str(library/rel),(dest,),ro) for rel,dest,ro in needed)
                        if app=='qbittorrent':
                            mounts=mounts and bound(str(Path(config['data_mount'])/'library/downloads/music'),('/data/downloads/music',))
                network=v.get('NetworkSettings',{});bindings=network.get('Ports',{})
                peer=peer_port(v) if app=='qbittorrent' else None
                published=[p for key,rows in bindings.items() if rows and key not in peer_bindings(peer,config) for p in rows]
                ports=all(p.get('HostIp')=='127.0.0.1' for p in published)
                if not photos or svc=='immich-server':ports=ports and any(p.get('HostPort')==str(health.APPS[app][2]) for p in (bindings.get(str(health.APPS[app][2])+'/tcp') or []))
                if not photos:mounts=mounts and 'nekoserver-media' in network.get('Networks',{})
                state='pass' if exact and running and healthy and mounts and ports else 'fail'
            except (ValueError,TypeError,KeyError,AttributeError,IndexError):pass
        results.append(item('container:'+svc,health.APPS[app][0]+' / '+svc,state,'Running state, persistent source mounts, network and localhost published ports verified.' if state=='pass' else 'Inspect container health, bind mounts, network and published ports locally.'))
    return results

def media_checks(config):
    spec=importlib.util.spec_from_file_location('acceptance_media_health',ROOT/'media-health.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    # This audit performs GET/inspection only; do not invoke its output-writing CLI.
    try:report=module.audit(config)
    except Exception:return [item('media:audit','Current media API checks','fail','Current read-only media checks could not complete.')]
    out=[]
    for group in ('services','integrations','readiness'):
        values=report.get(group,{})
        if not isinstance(values,dict):continue
        for key,value in list(values.items())[:50]:
            if not isinstance(key,str) or not re.fullmatch(r'[a-z0-9_:]+',key) or not isinstance(value,dict):continue
            # End-to-end transfer/playback is handled by witnessed manual tests below.
            if key in ('end_to_end_media','qbittorrent:authenticated_playback_or_transfer','navidrome:authenticated_playback_or_transfer'):continue
            state=value.get('result')
            if state in STATES:out.append(item('media:'+group+':'+key,key.replace('_',' ').replace(':',' / '),state,'Current authenticated configuration inspection; complete setup or inspect locally when pending.'))
    return out or [item('media:audit','Current media API checks','needs_setup','No current media configuration checks are available.')]

def dashboard_check(port,now):
    try:
        with OPENER.open('http://127.0.0.1:'+str(port)+'/api/status',timeout=5) as response:
            if response.status!=200:return False
            payload=response.read(2000001)
        if len(payload)>2000000:return False
        value=json.loads(payload);stamp=health.number(value.get('generated_at'),0,now+60)
        return value.get('format')=='neko-dashboard-v1' and stamp is not None and now-stamp<=150
    except (OSError,ValueError,TypeError,AttributeError):return False

OPTIONAL=('tv','notifications','heartbeat')

def manual_tests(modules,config):
    out=[('missing_drive','Missing-drive startup protection','With independent backups and the server shut down, test a deliberately unavailable data drive. Verify dependent services are blocked, then shut down and reconnect. Never unplug a live irreplaceable drive.')]
    if 'media' in modules:out += [('media_workflow','Media request, import and playback','Using media you are allowed to acquire, complete the configured request/download/import/playback chain. Check Jellyfin hardware acceleration if wanted.'),('music_playback','Music import and streaming','Verify configured music imports and actual Navidrome playback from the intended client.')]
    if 'nextcloud' in modules:out += [('cloud_client','Personal cloud upload/download and sharing','Use the phone/desktop client through the intended HTTPS gateway; upload/download a disposable file, verify checksum and intended sharing permissions.')]
    if 'photos' in modules and config.get('photos',{}).get('deploy_immich'):out += [('photo_preservation','Original uploads and deletion safety','Use disposable test photos/videos. Verify original upload checksums, archive creation, and retention after phone/Immich deletions. Do not delete your only real originals.'),('independent_restore','Full photo restore from an independent copy','Restore a matched, independently copied snapshot into an isolated workspace. Open original photos/videos and albums; verify database and deletion behaviour. A started app alone does not pass.')]
    if 'photo_backup' in modules:out += [('external_backup','External copy and reconnect','Verify new files and checksums on the configured external drive. Disconnect/reconnect between safe runs, test interrupted-copy recovery and confirm deletions never propagate.')]
    if 'samba' in modules or 'tailscale' in modules:out += [('remote_permissions','Actual remote access and permissions','Test from the intended PC/phone and outside home where applicable. Confirm guest access is denied and protected archive shares reject writes. Selected applications use the separately verified gateway.')]
    if 'gateway_lan' in modules or 'gateway_tailnet' in modules:out += [('gateway_clients','Application gateway on real clients','On each intended phone/PC, trust the LAN CA where needed and verify login, permission denial, media streaming, WebSockets and original uploads. For Tailscale also test away from home and verify tailnet policy. Management interfaces must remain inaccessible through the gateway.')]
    if 'server_os' in modules:out += [('server_idle','Server idle and drive policy','After reboot, leave the server idle longer than desktop idle timers. Verify jobs/network access continue with screen off and observe the intended HDD standby behavior without powering down irreplaceable drives.')]
    if 'tv' in modules:out += [('tv_session','HDMI, sound, controller and Remote Play','Verify TV picture/audio, controller input, Steam login/Remote Play and browser/media launchers. Confirm idle AC settings keep server jobs running.')]
    if 'heartbeat' in modules:out += [('outage_delivery','Independent outage alert','Confirm receipt at the outside monitor, stop only nekoserver-heartbeat.timer until period plus grace has elapsed, witness an alert, re-enable/start it and witness recovery. Keep the monitor outside this PC and its power/network failure domain.')]
    if 'notifications' in modules:out += [('notification_delivery','Phone notification delivery','Subscribe the intended phone to the protected ntfy topic; run the native test command and witness receipt with the dashboard closed. Rehearse problem/recovery delivery and network interruption without touching irreplaceable drives.')]
    if 'host_backup' in modules:out += [('host_restore','Host and user restore rehearsal','Run host-recovery.py verify/restore into a new directory. Compare selected save/share files and retained Unix identity/Samba data. Rehearse restoration independently; links remain inert and no live settings are automatically applied.')]
    if 'app_backup' in modules:out += [('app_restore','Media application restore rehearsal','Run app-recovery.py verify and restore into a new directory. Independently rehearse app startup with blocked network access; verify accounts, settings and databases. Check failure/reboot restart recovery on the server.')]
    if 'recovery' in modules:out += [('os_recovery','OS replacement/recovery exercise','With independent verified backups, rehearse the documented supported OS recovery and confirm retained data/apps and reboot behaviour. Automatic Bazzite reattachment remains unsupported.')]
    return [item(key,name,'needs_setup',detail) for key,name,detail in out]

def log_rotation():
    """Containers whose logs have no size limit, or None if Docker cannot be read."""
    from modules import docker as docker_module
    rc,ids=health.run(['docker','ps','-aq','--no-trunc'],20)
    if rc:return None
    if not ids.split():return []
    rc,out=health.run(['docker','inspect',*ids.split()],30)
    try:return docker_module.unbounded_logs(json.loads(out)) if rc==0 else None
    except ValueError:return None

def inspect(config,profile='full',now=None):
    now=time.time() if now is None else now;run={'format':'neko-acceptance-v1','version':VERSION,'generated_at':now,'run_id':str(uuid.uuid4()),'profile':profile,'automatic':[],'manual':[],'scope':'selected deployment profile; manual results are user reported'}
    checks=run['automatic'];add=lambda *x:checks.append(item(*x))
    try:
        installer.validate_config(config)
        if profile not in config['profiles']:raise ValueError()
        modules=installer.ordered(installer.load_modules(ROOT/'modules'),config['profiles'][profile])
        # Optional extras you turned down ("wanted": false) have nothing to accept.
        modules=[m for m in modules if not (m in OPTIONAL and config.get(m,{}).get('wanted') is False)]
        run['config_fingerprint']=fingerprint(config,profile);run['dashboard_scope']=scope_digest(config)
    except (OSError,RuntimeError,ValueError,TypeError,KeyError,installer.InstallerError):
        add('configuration','Configuration and profile','fail','Configuration or selected dependency profile is invalid.');return summarize(run)
    add('configuration','Configuration and profile','pass','Selected profile and dependency plan are valid.')
    supported=recovery_tools.host_supported();add('host','Supported recovery host','pass' if supported else 'needs_setup','Nyarch/Arch x86_64 detected.' if supported else 'Supported Nyarch/Arch x86_64 installation required; Bazzite needs separate validation.')
    matched={}
    for role,name in (('data_ssd','Application SSD'),('media_hdd','Media HDD')):
        spec=config['storage'][role];path=config['data_mount' if role=='data_ssd' else 'media_mount'];value=health.mount_info(path,spec['uuid'],spec['fstype']);matched[role]=value['status']=='mounted';add('mount:'+role,name,'pass' if matched[role] else 'needs_setup' if value['status']=='not_configured' else 'fail','Configured UUID and filesystem verified.' if matched[role] else 'Configure and mount the intended drive; no private data is inspected on a mismatch.')
    drives=all(matched.values()) and config['storage']['data_ssd']['uuid']!=config['storage']['media_hdd']['uuid']
    if all(matched.values()) and not drives:add('mount:distinct','Separate storage identities','fail','SSD and HDD must have distinct filesystem UUIDs.')
    current_boot=boot_id();run['boot_id']=current_boot
    previous=load_json(Path(config['data_mount'])/'reports/acceptance/latest.json') if matched['data_ssd'] else {}
    prior=previous.get('format')=='neko-acceptance-v1' and previous.get('profile')==profile and previous.get('config_fingerprint')==run['config_fingerprint']
    verified=prior and current_boot is not None and previous.get('baseline_verified') is True and previous.get('reboot_boot_id')==current_boot and health.number(previous.get('reboot_verified_at'),0,now) is not None
    baseline=previous.get('baseline_boot_id') if prior and previous.get('baseline_verified') is True else current_boot
    run['baseline_verified']=drives
    observed=prior and previous.get('baseline_verified') is True and current_boot is not None and isinstance(baseline,str) and baseline!=current_boot and drives
    if (observed or verified) and drives:
        run.update(baseline_boot_id=baseline,reboot_boot_id=current_boot,reboot_verified_at=now if observed else previous['reboot_verified_at']);add('reboot','Check after a real reboot','pass','This profile has matching storage checks on a later boot than its saved baseline.')
    else:
        run['baseline_boot_id']=current_boot;add('reboot','Check after a real reboot','needs_setup','Save a baseline, reboot normally and rerun this profile. Same-boot inspections do not pass this check.')
    if drives:
        try:
            bundle=Path(config['data_mount'])/'installer/releases'/('v'+VERSION);recovery_tools.verify_bundle(bundle,config)
            add('bundle','Preserved current installer','pass','Current SSD recovery bundle and checksums verified.')
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):add('bundle','Preserved current installer','needs_setup','Apply recovery from this release to preserve its verified installer bundle.')
    else:add('bundle','Preserved current installer','blocked','Drive verification is required before inspecting private files.')
    if 'docker' in modules:
        if drives:
            valid,version=docker_info(config);add('docker:store','Docker persistent store','pass' if valid else 'fail','Running classic overlay2 store uses the intended SSD.' if valid else 'Verify Docker availability, SSD data-root and classic storage backend.')
            add('docker:service','Docker boot service','pass' if service('docker.service') else 'fail','Service must be active and enabled for boot.')
            add('docker:guard','Effective Docker storage guard','pass' if guard_check(config) else 'fail','Installed and loaded UUID/mount guard must match both data drives.')
            unbounded=log_rotation()
            add('docker:logs','Container log size limits','pass' if unbounded==[] else 'needs_setup',
                'Every container log is size-limited.' if unbounded==[] else
                'Unlimited container logs can fill the SSD'+(': '+', '.join(unbounded[:8]) if unbounded else '')+
                '. Containers made before the limit keep unlimited logs until they are recreated after Docker restarts (a reboot, then the monthly update or rerunning setup).')
            saved=load_json(Path(config['data_mount'])/'installer/host-checkpoint.json');expected={k:config[k] for k in ('storage','data_mount','media_mount')};good=valid and saved.get('format')=='neko-host-checkpoint-v1' and saved.get('docker_version')==version and saved.get('architecture')==platform.machine() and saved.get('driver')=='overlay2' and saved.get('docker_root')==str(Path(config['data_mount'])/'docker-engine') and all(saved.get(k)==v for k,v in expected.items()) and saved.get('uid')==config['media']['uid'] and saved.get('gid')==config['media']['gid'];add('checkpoint','Docker recovery checkpoint','pass' if good else 'needs_setup','Checkpoint must match the running engine, drives and configured ownership.')
            if 'media' in modules:checks.extend(containers(config));checks.extend(media_checks(config))
            if 'nextcloud' in modules:
                from cloud_checks import check
                checks.extend(check(config))
            if 'photos' in modules and config.get('photos',{}).get('deploy_immich'):checks.extend(containers(config,photos=True))
        else:add('docker:dependents','Docker/application inspection','blocked','Drive verification is required before reading Docker or application configuration.')
    if 'photo_preservation' in modules or 'photo_backup' in modules or 'app_backup' in modules or 'host_backup' in modules or 'music_backup' in modules:
        rows=health.backups(config,now,matched['data_ssd']);selected={'archive','database'} if 'photo_preservation' in modules else set()
        if 'photo_backup' in modules:selected.add('external')
        if 'app_backup' in modules:selected.add('applications')
        if 'music_backup' in modules and config.get('music',{}).get('backup_enabled',True):selected.add('music')
        if 'host_backup' in modules:selected.add('host')
        for job in rows:
            if job['id'] not in selected:continue
            status='pass' if job['status']=='pass' and job['last_success'] is not None and job['timer']['installed'] and job['timer']['active'] else 'fail' if job['status'] in ('fail','stale') else 'needs_setup'
            add('backup:'+job['id'],job['name'],status,'A recent successful updated job and active timer are required; checksums/restoration also need manual verification.')
        if 'photo_backup' in modules:
            spec=config.get('photos',{});external=health.mount_info(config['external_backup_mount'],spec.get('external_backup_uuid',''),spec.get('external_backup_fstype','ext4'));state=external['status'];add('external:identity','External backup identity','pass' if state=='mounted' else 'fail' if state=='wrong_drive' else 'needs_setup','Connect the intended external drive for this acceptance check; normal offline storage between runs is expected.')
    if drives:
        checks.extend(permission_checks.check(config, modules))
    if 'server_os' in modules:checks.extend(os_checks.check(config))
    if 'maintenance' in modules:
        report=load_json(Path(config['data_mount'])/'reports/jobs/maintenance.json') if drives else {}
        good=report.get('status')=='pass' and type(report.get('last_success')) in (int,float) and 0<=now-report['last_success']<=35*86400
        add('maintenance:timer','Monthly overnight backup and updates','pass' if service('nekoserver-maintenance.timer',True) else 'needs_setup','Timer must be enabled; missing external storage blocks updates.')
        add('maintenance:rehearsal','Recent restore rehearsal and live checks','pass' if good else 'fail' if report.get('status')=='fail' else 'needs_setup','A real successful monthly transaction is required; unit tests do not establish server acceptance.')
    if 'media_monitor' in modules and config.get('monitoring',{}).get('enabled',True):add('media:timer','Scheduled media checks','pass' if service('nekoserver-media-health.timer',True) else 'needs_setup','Scheduled read-only media timer must be active and enabled.')
    for module,enabled,unit in (
        ('media_monitor',config.get('monitoring',{}).get('enabled',True),'nekoserver-media-health.timer'),
        ('music_backup',config.get('music',{}).get('backup_enabled',True),'nekoserver-music-backup.timer'),
    ):
        if module in modules and not enabled:add('retired:'+unit,'Disabled '+unit,'pass' if retired_timer(unit) else 'needs_setup','Old managed timer must be absent or inactive and disabled.')
    if 'dashboard' in modules:
        port=config.get('dashboard',{}).get('port',8787);valid=type(port) is int and 1024<=port<=65535
        good=valid and dashboard_check(port,now);add('dashboard:status','Live dashboard status','pass' if good else 'fail','Local endpoint must return a current sanitized status snapshot.')
        for unit in ('nekoserver-dashboard.service','nekoserver-dashboard-collect.timer'):add(unit,unit,'pass' if service(unit) else 'fail','Dashboard web service and collection timer must be active and enabled.')
    if 'heartbeat' in modules:
        heartbeat=health.heartbeat_status(config,now)
        good=heartbeat['status']=='pass' and heartbeat['last_success'] is not None and heartbeat['timer']['installed'] and heartbeat['timer']['active']
        add('heartbeat:delivery','Heartbeat HTTPS acknowledgement','pass' if good else 'fail' if heartbeat['status'] in ('fail','stale') else 'needs_setup','A current successful HTTPS request and active timer are required; monitor registration and outside alerts remain witnessed.')
    if 'notifications' in modules:
        delivery=health.notification_status(now)
        good=delivery['timer']['installed'] and delivery['timer']['active']
        add('notifications:timer','Background notification schedule','pass' if good else 'needs_setup','Native notification timer must be installed and active.')
        add('notifications:delivery','Notification provider acceptance','fail' if delivery['status'] in ('fail','stale') else 'pass' if delivery['last_success'] is not None else 'needs_setup','A recorded provider-accepted delivery is required; actual phone receipt remains a witnessed check.')
    if 'tailscale' in modules:
        rc,out=health.run(['tailscale','status','--json'],15)
        try:value=json.loads(out);active=rc==0 and isinstance(value,dict) and value.get('BackendState')=='Running'
        except ValueError:active=False
        add('tailscale','Tailscale authentication','pass' if active and service('tailscaled.service') else 'needs_setup','Authenticated local backend and enabled service required; actual remote access remains a witnessed test.')
    if 'samba' in modules:add('samba','Samba boot service','pass' if service('smb.service') else 'needs_setup','Samba service must be active and enabled; share permissions require actual client tests.')
    for kind,module in (('lan','gateway_lan'),('tailnet','gateway_tailnet')):
        if module in modules:
            if drives:checks.extend(gateway_checks.check(config,kind))
            else:add('gateway:'+kind,'Application gateway','blocked','Drive verification is required before reading gateway state.')
            if kind=='lan':add('gateway:lan:service','LAN gateway boot service','pass' if service('nekoserver-gateway-lan.service') else 'needs_setup','LAN gateway must be active and enabled.')
    if 'tv' in modules:
        try:
            user,home=tv.desktop_user(config);saved=load_json(Path(config['data_mount'])/'reports/tv-setup.json') if matched['data_ssd'] else {};hashes=saved.get('launchers_sha256',{});good=saved.get('user')==user.pw_name and isinstance(hashes,dict) and len(hashes)>=3
            if good:
                for name,digest in hashes.items():
                    rel=Path(name)
                    if rel.is_absolute() or '..' in rel.parts or not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest) or hashlib.sha256(safe_path(home/rel).read_bytes()).hexdigest()!=digest:good=False;break
            add('tv:launchers','TV desktop launchers','pass' if good else 'needs_setup','Current desktop launcher files must match their installation report; controller/display/session tests remain manual.')
        except (OSError,RuntimeError,ValueError,KeyError,TypeError):add('tv:launchers','TV desktop launchers','needs_setup','TV user or managed launcher evidence unavailable; reapply TV setup.')
    if drives:
        try:checks.extend(native_runtime.acceptance_checks(config,modules))
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):add('runtime','Native runtime','needs_setup','Verify native build integrity and reapply the affected modules.')
    else:add('runtime','Native runtime','blocked','Drive verification is required before inspecting installed native workers.')
    run['manual']=manual_tests(modules,config);return summarize(run)

def summarize(report):
    report['summary']={group:{s:sum(x['status']==s for x in report[group]) for s in STATES} for group in ('automatic','manual')}
    report['acceptance_complete']=bool(report['automatic']) and all(x['status']=='pass' for group in ('automatic','manual') for x in report[group])
    report['status']='complete' if report['acceptance_complete'] else 'failed' if any(x['status']=='fail' for group in ('automatic','manual') for x in report[group]) else 'pending'
    return report

def record_manual(report,prompt=input):
    for check in report['manual']:
        print('\n'+check['name']+'\n'+check['detail'])
        value=prompt('Type PASS '+check['id']+', FAIL '+check['id']+', or Enter to leave pending: ').strip()
        if value in ('PASS '+check['id'],'FAIL '+check['id']):check.update(status='pass' if value.startswith('PASS ') else 'fail',confirmed_at=time.time(),evidence='user_reported')
    return summarize(report)

def guided_manual(config, report):
    """Yes/no handoffs for witnessed tests; never replace automatic findings."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(config=config, apply=True)
    links = load_json(Path(config['data_mount'])/'reports/gateway-tailnet.json').get('links', {})
    for check in report['manual']:
        key = check['id']
        targets = []
        if key.startswith('tv'):
            targets = [('steam', '')]
        elif any(word in key for word in ('media', 'music', 'cloud', 'photo', 'remote', 'gateway')):
            targets = [('url', url) for url in links.values() if guided_setup.valid_url(url)]
            if not targets:
                targets = [('url', 'http://127.0.0.1:8096/'), ('url', 'http://127.0.0.1:2283/')]
        if any(word in key for word in ('notification', 'alert')):
            targets = [('url', 'https://ntfy.sh/docs/subscribe/phone/')]
        if 'heartbeat' in key or 'outage' in key:
            targets = [('url', 'https://healthchecks.io/docs/')]
        done = guided_setup.checkpoint(ctx, check['name'], [
            check['detail'],
            'Carry out the test on the named device/service. A browser launched here '
            'cannot operate a different phone/PC or prove hardware behavior.',
            'Do not perform destructive storage/restore tests on your only live data. '
            'Use the documented isolated recovery workflow and independent copies.',
            'Answer yes only after witnessing this specific result. No repeats the instructions '
            'and reopens any relevant local pages/apps; Ctrl+C leaves it pending.'
        ], targets)
        if done:
            check.update(status='pass', confirmed_at=time.time(), evidence='user_reported')
    return summarize(report)


def save(config,report):
    recovery_tools.mounts(config)  # Recheck immediately before any SSD report write.
    path=safe_path(Path(config['data_mount'])/'reports/acceptance/latest.json');atomic_json(path,report);return path

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,default=ROOT/'config.json');parser.add_argument('--profile',default='full');parser.add_argument('--save',action='store_true');parser.add_argument('--manual',action='store_true');a=parser.parse_args(argv)
    if a.manual and (not a.save or not sys.stdin.isatty()):parser.error('--manual requires --save and an interactive terminal')
    if os.geteuid()!=0:print('Run with sudo for complete local inspection.',file=sys.stderr);return 2
    try:
        config=load_json(a.config);report=inspect(config,a.profile)
        if a.manual:
            if config.get('guided_setup', {}).get('enabled', True):guided_manual(config, report)
            else:record_manual(report)
        if a.save:save(config,report)
        print('NekoServer acceptance · '+report['profile']+' · '+report['status'])
        for group in ('automatic','manual'):
            for check in report[group]:print(group+' / '+check['id']+': '+check['status']+' — '+check['detail'])
        print('Manual passes are user-reported. This report applies only to the selected profile and current inspection.')
        return 0 if report['acceptance_complete'] else 2 if report['status']=='failed' else 1
    except (OSError,RuntimeError,ValueError,KeyError,TypeError,installer.InstallerError,KeyboardInterrupt):print('Acceptance check could not finish; inspect configuration and local services. No application state was changed.',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
