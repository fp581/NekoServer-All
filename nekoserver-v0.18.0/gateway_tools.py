"""Selected-app HTTPS gateways; never publish Funnel or change Docker bindings."""
import native_runtime
import permission_checks
import hashlib,html,ipaddress,json,os,pwd,re,shutil,socket,subprocess,sys,tempfile
from pathlib import Path
import dashboard_collect as health
import recovery_tools
from network_access import private_ip,lan,candidates,verify_lan
from neko_common import VERSION,safe_path,atomic_write,atomic_json,ensure_directory
APPS={'jellyfin':('Jellyfin',8096,8443),'seerr':('Seerr',5055,8444),'immich':('Immich',2283,8445),'navidrome':('Music',4533,8446),'nextcloud':('Nextcloud',8088,8447)}
ROOT=Path(__file__).resolve().parent

def command(args,timeout=30,env=None):
    try:p=subprocess.run(args,capture_output=True,text=True,timeout=timeout,env=env)
    except (OSError,subprocess.TimeoutExpired):raise RuntimeError('Gateway command unavailable or timed out; inspect locally') from None
    if p.returncode:raise RuntimeError('Gateway command failed; inspect local setup and HTTPS permissions')
    return p.stdout

def selected(config):
    apps=config.get('gateway',{}).get('services',list(APPS))
    if not isinstance(apps,list) or not apps or len(apps)!=len(set(apps)) or not set(apps)<=set(APPS):raise RuntimeError('Unknown gateway application')
    for app in apps:
        if app=='nextcloud':
            if not config.get('nextcloud',{}).get('enabled'):raise RuntimeError('Nextcloud deployment is disabled')
        elif app=='immich':
            if not config.get('photos',{}).get('deploy_immich'):raise RuntimeError('Selected Immich deployment is disabled')
        elif app not in config.get('media',{}).get('services',[]):raise RuntimeError('Gateway application is not selected for deployment')
    return apps

def configure(ctx,kind):
    cfg=ctx.config;apps=selected(cfg);opts=cfg.setdefault('gateway',{});changed=False
    if 'services' not in opts:opts['services']=list(apps);changed=True
    if kind=='lan' and (opts.get('lan_ip') in (None,'auto') or opts.get('lan_cidr') in (None,'auto')):
        if not sys.stdin.isatty():return False
        rows=candidates()
        if not rows:raise RuntimeError('No usable LAN address found; configure gateway.lan_ip and lan_cidr')
        for i,row in enumerate(rows,1):print(str(i)+'. '+row['interface']+' · '+row['ip']+' · '+row['cidr'])
        choice=input('Choose your home-network interface number: ').strip()
        if not choice.isdigit() or not 1<=int(choice)<=len(rows):raise RuntimeError('Invalid LAN selection')
        row=rows[int(choice)-1];opts.update(lan_ip=row['ip'],lan_cidr=row['cidr']);changed=True
    if opts.get('confirmed_services')!=apps:
        if not sys.stdin.isatty():return False
        print('Create administrator accounts, disable unwanted public signup and verify login/access permissions for: '+', '.join(apps))
        if input('After verifying those settings, type SECURED to enable access: ').strip()!='SECURED':return False
        opts['confirmed_services']=list(apps);changed=True
    if changed:
        if hasattr(ctx,'persist_config'):ctx.persist_config()
        elif getattr(ctx,'config_path',None):atomic_json(ctx.config_path,cfg)
    return True

def readiness(config):
    apps=selected(config)
    if config.get('gateway',{}).get('confirmed_services')!=apps:raise RuntimeError('Explicit secured-account confirmation required for selected applications')
    from media_libraries import NoRedirect
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    for app,endpoint,key in [('jellyfin','/System/Info/Public','StartupWizardCompleted'),('seerr','/api/v1/settings/public','initialized')]:
        if app not in apps:continue
        try:
            with opener.open('http://127.0.0.1:'+str(APPS[app][1])+endpoint,timeout=5) as r:value=json.loads(r.read(64000))
            if not isinstance(value,dict) or value.get(key) is not True:raise RuntimeError('Initial application account setup is incomplete')
        except (OSError,ValueError):raise RuntimeError('Application onboarding could not be verified; finish setup locally') from None
    from acceptance import containers
    scoped=dict(config);scoped['media']={**config.get('media',{}),'services':[a for a in apps if a not in ('immich','nextcloud')]}
    checked=containers(scoped) if scoped['media']['services'] else []
    if 'immich' in apps:checked+=containers(config,photos=True)
    if 'nextcloud' in apps:
        from cloud_checks import check
        checked+=check(config)
    if any(r['status']!='pass' for r in checked):raise RuntimeError('Selected containers must use the verified persistent mounts, networks and localhost ports')
    states=health.applications(config)
    if any(not any(r['id']==app and r['state']=='running' and r['health'] not in ('unhealthy','starting') for r in states) for app in apps):raise RuntimeError('Selected application containers must be running and healthy before exposure')

def portal(config,host):
    apps=selected(config);cards=''.join('<a href="https://'+host+':'+str(APPS[a][2])+'/">'+html.escape(APPS[a][0])+' <span>↗</span></a>' for a in apps)
    logo=(ROOT/'dashboard/logo.svg').read_text()
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NekoServer · Your apps</title><style>body{margin:0;background:#11101b;color:#f3efff;font:18px system-ui}main{max-width:850px;margin:8vh auto;padding:30px}svg{width:96px;height:96px}h1{font-size:42px;margin-bottom:8px}p{color:#b4aabd;line-height:1.6}nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;margin:40px 0}a{padding:35px;border:1px solid #43354f;border-radius:20px;background:#211a30;color:#e5c6fb;text-decoration:none;font-weight:600}a:hover,a:focus-visible{outline:2px solid #d1a1f2;background:#30223f}a span{float:right;color:#e8aac9}small{color:#b4aabd}</style><main>'+logo+'<h1>Your world, under one roof.</h1><p>Welcome home. Pick an app and sign in with its account.</p><nav aria-label="Your applications">'+cards+'</nav><small>NekoServer · selected apps only · management panels stay local</small></main></html>'

def lan_model(config,portal_root):
    ip,net=lan(config);servers={}
    for app in ['portal',*selected(config)]:
        port=8442 if app=='portal' else APPS[app][2]
        handler={'handler':'file_server','root':str(portal_root),'hide':['.*']} if app=='portal' else {'handler':'reverse_proxy','upstreams':[{'dial':'127.0.0.1:'+str(APPS[app][1])}]}
        match={'host':[ip],'remote_ip':{'ranges':[net]}}
        if app=='portal':match['path']=['/','/index.html']
        servers[app]={'listen':[ip+':'+str(port)],'protocols':['h1','h2'],'automatic_https':{'disable_redirects':True},'routes':[{'match':[match],'handle':[handler],'terminal':True},{'handle':[{'handler':'static_response','status_code':403}],'terminal':True}]}
    return {'admin':{'disabled':True},'apps':{'pki':{'certificate_authorities':{'local':{'install_trust':False}}},'tls':{'automation':{'policies':[{'subjects':[ip],'issuers':[{'module':'internal','ca':'local'}]}]}},'http':{'servers':servers}}}

def unit(config,code,state):
    data=config['data_mount'];media=config['media_mount']
    guard=(str(code/'nekoserver-runtime')+' gateway-guard') if native_runtime.backend(config)=='rust' else ('/usr/bin/python3 '+str(code/'gateway_guard.py'))
    return '[Unit]\nDescription=NekoServer LAN HTTPS gateway\nWants=network-online.target\nAfter=network-online.target\nRequiresMountsFor='+data+' '+media+'\n[Service]\nUser=caddy\nGroup=caddy\nEnvironment=XDG_DATA_HOME='+str(state)+'\nEnvironment=XDG_CONFIG_HOME='+str(state)+'\nExecStartPre=+'+guard+' --config /etc/nekoserver-gateway/storage.json\nExecStart=/usr/bin/caddy run --config /etc/nekoserver-gateway/lan.json\nRestart=on-failure\nRestartSec=10s\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nPrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\nReadWritePaths='+str(state)+'\nInaccessiblePaths=-/run/docker.sock -/var/run/docker.sock '+data+'/appdata '+data+'/docker-engine '+media+'\nRestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK\n[Install]\nWantedBy=multi-user.target\n'

def hashes(payloads):return {str(p):hashlib.sha256(v if isinstance(v,bytes) else v.encode()).hexdigest() for p,v in payloads.items()}

def preflight(payloads,ledger):
    previous=health.read_json(ledger).get('sha256',{})
    for p,content in payloads.items():
        p=safe_path(p);desired=content if isinstance(content,bytes) else content.encode()
        if p.exists() and p.read_bytes()!=desired and hashlib.sha256(p.read_bytes()).hexdigest()!=previous.get(str(p)):raise RuntimeError('Unmanaged or modified gateway file preserved; inspect before applying')

def port_preflight(config):
    ip,_=lan(config);owned=set();path=Path('/etc/nekoserver-gateway/lan.json');ledger=health.read_json(Path(config['data_mount'])/'gateway/lan-managed.json');old=health.read_json(path)
    rc,_=health.run(['systemctl','is-active','--quiet','nekoserver-gateway-lan.service'])
    if rc==0 and path.is_file() and ledger.get('sha256',{}).get(str(path))==hashlib.sha256(path.read_bytes()).hexdigest():
        for server in old.get('apps',{}).get('http',{}).get('servers',{}).values():owned.update(server.get('listen',[]))
    for port in [8442,*[APPS[a][2] for a in selected(config)]]:
        if ip+':'+str(port) in owned:continue
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:probe.bind((ip,port))
        except OSError:raise RuntimeError('LAN gateway address/port unavailable; no existing listener will be replaced') from None

def _apply_lan(config):
    recovery_tools.require_root();recovery_tools.mounts(config);verify_lan(config);readiness(config)
    data=Path(config['data_mount']);state=safe_path(data/'gateway/caddy-state');code=safe_path(Path('/usr/local/lib/nekoserver-gateway')/('v'+VERSION));web=Path('/usr/local/share/nekoserver-gateway/lan');ledger=data/'gateway/lan-managed.json'
    payloads={Path('/etc/nekoserver-gateway/lan.json'):json.dumps(lan_model(config,web),indent=2)+'\n',Path('/etc/systemd/system/nekoserver-gateway-lan.service'):unit(config,code,state),web/'index.html':portal(config,lan(config)[0])}
    payloads.update({code/n:(ROOT/n).read_bytes() for n in ('gateway_guard.py','network_access.py','storage-guard.py','neko_common.py')})
    binary=native_runtime.payload(ROOT,config)
    if binary is not None:payloads[code/native_runtime.NAME]=binary
    preflight(payloads,ledger);port_preflight(config)
    if not shutil.which('caddy'):
        if not shutil.which('pacman') or not recovery_tools.host_supported():raise RuntimeError('Install official Caddy for this OS; automatic installation supports Nyarch/Arch')
        command(['pacman','-Si','caddy']);command(['pacman','-S','--needed','--noconfirm','caddy'],900)
    user=pwd.getpwnam('caddy')
    if user.pw_uid==0 or user.pw_gid==0:raise RuntimeError('Caddy needs an unprivileged package service account')
    if state.exists() and (state.stat().st_uid!=user.pw_uid or state.stat().st_gid!=user.pw_gid):raise RuntimeError('Persistent TLS-state owner differs; preserve files and plan recovery manually')
    # Package Caddy runs without root credentials. Fresh public ancestors must
    # survive a private installer umask; existing restrictive paths are preserved.
    public=(data/'gateway',code.parent,code,web.parent,web,Path('/etc/nekoserver-gateway'))
    for folder in public:
        safe_path(folder)
        existing=folder
        while not existing.exists():existing=existing.parent
        if not permission_checks.access(existing,user.pw_uid,{user.pw_gid},5 if existing==folder else 1):
            raise RuntimeError('Existing gateway directory blocks service traversal; review its permissions')
    with tempfile.TemporaryDirectory(prefix='neko-caddy-validate-') as folder:
        test=Path(folder)/'lan.json';test.write_text(payloads[Path('/etc/nekoserver-gateway/lan.json')]);command(['caddy','validate','--config',str(test)],env={**os.environ,'XDG_DATA_HOME':folder,'XDG_CONFIG_HOME':folder})
    # Configuration validity is checked before replacing managed files or restarting our service.
    recovery_tools.mounts(config);verify_lan(config)
    for folder in public:ensure_directory(folder,0o755)
    state.mkdir(parents=True,exist_ok=True,mode=0o700);os.chown(state,user.pw_uid,user.pw_gid)
    for p,content in payloads.items():atomic_write(p,content,0o750 if p.name==native_runtime.NAME else 0o644)
    clean={k:config[k] for k in ('data_mount','media_mount','storage')};clean['gateway']={k:config['gateway'][k] for k in ('lan_ip','lan_cidr')};atomic_json(Path('/etc/nekoserver-gateway/storage.json'),clean)
    atomic_json(ledger,{'sha256':hashes(payloads),'uid':user.pw_uid,'gid':user.pw_gid})
    configure_cloud_origin(config,lan(config)[0],8447)
    command(['systemctl','daemon-reload']);command(['systemctl','enable','--now','nekoserver-gateway-lan.service']);
    try:command(['systemctl','restart','nekoserver-gateway-lan.service'])
    except RuntimeError:
        command(['systemctl','stop','nekoserver-gateway-lan.service']);raise
    report={'format':'neko-gateway-v1','mode':'lan','portal':'https://'+lan(config)[0]+':8442/','services':selected(config),'status':'configured','client_acceptance':'needs_setup','reason':'Trust the private LAN CA on each client, then verify login, uploads, streaming and forbidden-source access.'};atomic_json(data/'reports/gateway-lan.json',report);return report

def canonical(value):
    if not isinstance(value,dict):raise RuntimeError('Cannot inspect Tailscale Serve configuration')
    funnel=value.get('AllowFunnel') or {}
    if not isinstance(funnel,dict) or any(type(v) is not bool for v in funnel.values()):raise RuntimeError('Malformed Funnel state; no Tailscale route changed')
    if any(k not in ('TCP','Web','AllowFunnel','Foreground','Services','ETag') for k in value) or value.get('Foreground') or value.get('Services') or any(funnel.values()):raise RuntimeError('Unsupported/Funnel/foreground Tailscale routes preserved; manual review required')
    return {k:value.get(k) or {} for k in ('TCP','Web')}

def desired(host,apps):
    ports={str(APPS[a][2]):'http://127.0.0.1:'+str(APPS[a][1]) for a in apps}
    return {'TCP':{p:{'HTTPS':True} for p in ports},'Web':{host+':'+p:{'Handlers':{'/':{'Proxy':target}}} for p,target in ports.items()}}

def serve_state():return canonical(json.loads(command(['tailscale','serve','status','--json'])))

def subset(current,owned):
    return all(isinstance(current[k],dict) and isinstance(owned.get(k),dict) and all(owned[k].get(key)==value for key,value in current[k].items()) for k in ('TCP','Web')) and len(current['TCP'])==len(current['Web'])

def tailnode():
    v=json.loads(command(['tailscale','status','--json']));name=v.get('Self',{}).get('DNSName','').rstrip('.')
    if v.get('BackendState')!='Running' or not re.fullmatch(r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\.ts\.net',name):raise RuntimeError('Authenticate Tailscale and enable tailnet HTTPS/MagicDNS first')
    return name

def gateway_owned(host,owned):
    try:
        if not isinstance(owned,dict) or set(owned)!= {'TCP','Web'}:return False
        ports={str(v[2]):app for app,v in APPS.items()}
        if not isinstance(owned['TCP'],dict) or not set(owned['TCP'])<=set(ports):return False
        return owned==desired(host,[ports[p] for p in owned['TCP']])
    except (ValueError,TypeError,KeyError):return False

def _apply_tailnet(config):
    recovery_tools.require_root();recovery_tools.mounts(config);readiness(config);host=tailnode();apps=selected(config);target=desired(host,apps);state=serve_state();ledger=safe_path(Path(config['data_mount'])/'gateway/tailnet-owned.json');saved=health.read_json(ledger);owned=saved.get('owned',{'TCP':{},'Web':{}})
    allowed=gateway_owned(host,owned)
    if ledger.exists() and (saved.get('format')!='neko-tailnet-owned-v1' or saved.get('status') not in ('pending','configured') or not allowed):raise RuntimeError('Malformed gateway ownership journal preserved; review it locally')
    if state!={'TCP':{},'Web':{}} and (saved.get('host')!=host or not subset(state,owned)):raise RuntimeError('Existing Tailscale routes are not owned by NekoServer; no route changed')
    if saved and saved.get('host')!=host:raise RuntimeError('Tailscale node identity changed; inspect preserved ownership before adopting routes')
    configure_cloud_origin(config,host,8447)
    # Journal ownership before requests; interrupted/partly completed configuration remains identifiable.
    union={k:{**owned.get(k,{}),**target[k]} for k in ('TCP','Web')};atomic_json(ledger,{'format':'neko-tailnet-owned-v1','host':host,'owned':union,'status':'pending'})
    try:
        for port in list(state['TCP']):
            if port not in target['TCP']:command(['tailscale','serve','--bg','--https='+port,'off'])
        for port in target['TCP']:
            if state['Web'].get(host+':'+port)==target['Web'][host+':'+port] and state['TCP'].get(port)==target['TCP'][port]:continue
            command(['tailscale','serve','--bg','--https='+port,target['Web'][host+':'+port]['Handlers']['/']['Proxy']],45)
        if serve_state()!=target:raise RuntimeError('Tailnet routes did not match the planned private configuration')
    except (RuntimeError,ValueError,TypeError):
        # Fail closed for our routes when their complete current configuration is still recognizable.
        current=serve_state()
        if subset(current,union):
            for port in current['TCP']:command(['tailscale','serve','--bg','--https='+port,'off'])
        raise RuntimeError('Tailnet gateway setup failed; inspect the pending journal and current Serve routes locally') from None
    atomic_json(ledger,{'format':'neko-tailnet-owned-v1','host':host,'owned':target,'status':'configured'})
    result={'format':'neko-gateway-v1','mode':'tailnet','status':'configured','client_acceptance':'needs_setup','services':apps,'links':{a:'https://'+host+':'+str(APPS[a][2])+'/' for a in apps},'reason':'Restrict tailnet policy to intended users/devices and verify each application login and streaming/upload from a client.'};atomic_json(Path(config['data_mount'])/'reports/gateway-tailnet.json',result);return result

def apply_lan(config):
    recovery_tools.require_root();recovery_tools.mounts(config)
    with recovery_tools.lock(config):return _apply_lan(config)

def apply_tailnet(config):
    recovery_tools.require_root();recovery_tools.mounts(config)
    with recovery_tools.lock(config):return _apply_tailnet(config)


def configure_cloud_origin(config, host, port):
    """Set the canonical HTTPS origin and explicit trusted hosts through upstream occ."""
    if 'nextcloud' not in selected(config):
        return
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise RuntimeError('Invalid Nextcloud gateway host')
    from neko_common import compose_image_override
    folder=Path(config['data_mount'])/'compose/nextcloud'
    base=['docker','--host','unix:///var/run/docker.sock','compose','-p','neko-nextcloud','-f',str(folder/'compose.yaml'),'--env-file',str(folder/'.env'),*compose_image_override(folder/'compose.yaml'),'exec','-T','--user','www-data','nextcloud','php','occ']
    try:values=json.loads(command(base+['config:system:get','trusted_domains','--output=json']))
    except ValueError:raise RuntimeError('Cannot inspect Nextcloud trusted hosts') from None
    if not isinstance(values,list):raise RuntimeError('Cannot inspect Nextcloud trusted hosts')
    if host not in values:command(base+['config:system:set','trusted_domains',str(len(values)),'--value='+host])
    for key,value in [('overwriteprotocol','https'),('overwritehost',host+':'+str(port)),('overwrite.cli.url','https://'+host+':'+str(port))]:
        command(base+['config:system:set',key,'--value='+value])
