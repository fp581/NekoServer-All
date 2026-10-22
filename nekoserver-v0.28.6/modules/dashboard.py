"""Local visual health dashboard with a privileged collector and unprivileged web server."""
import native_runtime
from acceptance_summary import scope_digest
import getpass,grp,importlib.util,json,os,platform,re,shutil,subprocess,sys,time
from pathlib import Path
import host
from neko_common import APP_PORTS,DASHBOARD_DEFAULT_PORT,MANAGED_UNIT,SYSTEMD_UNITS,VERSION,atomic_json,atomic_write,check_units,mount_path,safe_path,write_units
DEPENDENCIES=['storage','recovery']
DESCRIPTION='Install the localhost visual dashboard, read-only collector and local alerts'
# The page's files; runtime/src/serve.rs serves exactly these (checked by a test).
ASSETS=('index.html','style.css','app.js','sw.js','logo.svg','manifest.webmanifest','icon-192.png','icon-512.png','apple-touch-icon.png')
# Opens the dashboard in its own window from the desktop's app menu.
LAUNCHER=Path('/usr/local/bin/nekoserver-dashboard')
DESKTOP_ENTRY=Path('/usr/share/applications/nekoserver-dashboard.desktop')
MANAGED='X-NekoServer-Managed=true'
SERVICE='nekoserver-dashboard.service'
SERVICE_UNIT=SYSTEMD_UNITS/SERVICE
# Fan and lighting changes: the only root part, reached by the web server over a socket.
CONTROL='nekoserver-control.service'
CONTROL_GROUP='nekoserver-control'
CONTROL_CONFIG=Path('/etc/nekoserver-control')
OPENRGB='nekoserver-openrgb.service'
OPENRGB_BINARY=Path('/usr/bin/openrgb')
# Memory and motherboard lighting is reached over i2c (SMBus).
I2C_MODULES=Path('/etc/modules-load.d/nekoserver-i2c.conf')

def options(cfg):
    values=cfg.get('dashboard',{});port=values.get('port',DASHBOARD_DEFAULT_PORT);interval=values.get('interval_seconds',60);smart=values.get('smart_interval_seconds',1800)
    if type(port) is not int or not 1024<=port<=65535 or port in APP_PORTS or port==cfg.get('media',{}).get('torrent_port'):raise RuntimeError(f'Dashboard port {port} is taken or not allowed: choose a free port from 1024 to 65535 that no app uses (Nextcloud uses 8088, Jellyfin 8096, Immich 2283)')
    if type(interval) is not int or not 30<=interval<=300 or type(smart) is not int or not 300<=smart<=86400:raise RuntimeError('Dashboard collection/SMART intervals are out of range')
    if 'install_smartmontools' in values and type(values['install_smartmontools']) is not bool:raise RuntimeError('SMART package switch must be a boolean')
    for key in ('fan_control','rgb'):
        if key in values and type(values[key]) is not bool:raise RuntimeError(f'dashboard.{key} must be true or false')
    return port,interval,smart

def control_options(cfg):
    """(fan and light control on, OpenRGB lighting on). Lighting needs control."""
    values=cfg.get('dashboard',{});control=values.get('fan_control',True)
    return control,control and values.get('rgb',True)

def tailnet_name(cfg,known=None):
    """This server's Tailscale name, so the dashboard also answers its private Tailscale
    address (your devices only). None when remote access is switched off. When Tailscale
    can't be asked right now (not connected yet, restarting), the name already in use
    (`known`) is kept: a passing failure must not take the phone's access away."""
    if cfg.get('dashboard',{}).get('remote',True) is not True:return None
    try:
        import gateway_tools
        return gateway_tools.tailnode()
    except (RuntimeError,OSError,ValueError,KeyError):return known

def public_host_of(unit):
    """The Tailscale name a web service definition answers (None without one)."""
    for line in (unit or '').split('\n'):
        words=line.split()
        if line.startswith('ExecStart=') and 'serve' in words and '--public-host' in words[:-1]:
            return words[words.index('--public-host')+1]
    return None

def installed_unit():
    try:return SERVICE_UNIT.read_text()
    except OSError:return None

def control_units(code,lighting,fans=True,data=None):
    """The root control service (fans, lights, and restarting or closing apps from the
    dashboard) and, with lighting, OpenRGB's server. It runs whenever the dashboard is
    installed: with fan control switched off it leaves every fan to the motherboard.
    OpenRGB answers this computer only: systemd drops any other network traffic."""
    binary=str(code/"nekoserver-runtime")
    after='After=systemd-modules-load.service docker.service'+(' '+OPENRGB+'\nWants='+OPENRGB if lighting else '')
    found={CONTROL:'[Unit]\nDescription=NekoServer fan, light and app control\n'+after+'\nStartLimitIntervalSec=0\n[Service]\nType=simple\n'
        # Lighting off: the service never reads, asks or changes any lighting. Fan control
        # off: it never takes a fan over. The settings say which apps may be restarted.
        f'ExecStart={binary} control'+('' if lighting else ' --no-lights')+('' if fans else ' --no-fans')+' --config '+str(COLLECTOR_SETTINGS)+'\n'
        # Whenever it stops (a crash too), every fan goes back to the motherboard.
        f'ExecStopPost={binary} control --restore\n'
        # An app being restarted from the dashboard is stopped and started again
        # before the service ends (up to 200 s), so it is never left half stopped.
        'TimeoutStopSec=240\n'
        'Restart=always\nRestartSec=3\nGroup='+CONTROL_GROUP+'\nUMask=0077\n'
        'RuntimeDirectory=nekoserver-control\nRuntimeDirectoryMode=0750\nRuntimeDirectoryPreserve=yes\n'
        # Docker's command-line tool keeps its own settings here, not in root's home.
        'Environment=DOCKER_CONFIG=/run/nekoserver-control/docker\n'
        # The shared lock that backups and monthly updates hold (an app restart waits for none).
        'NoNewPrivileges=yes\nProtectHome=yes\nProtectSystem=strict\nReadWritePaths='+str(CONTROL_CONFIG)+(' -'+str(Path(data)/'installer') if data else '')+'\nPrivateTmp=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
        # CAP_KILL: closing an app someone opened on the desktop (their own user's process).
        'CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE CAP_KILL\nRestrictAddressFamilies=AF_UNIX AF_INET\nIPAddressDeny=any\nIPAddressAllow=localhost\n'
        '[Install]\nWantedBy=multi-user.target\n'}
    if lighting:
        found[OPENRGB]=('[Unit]\nDescription=NekoServer lighting (OpenRGB server)\nAfter=systemd-modules-load.service\nStartLimitIntervalSec=0\n[Service]\nType=simple\n'
            f'ExecStart={OPENRGB_BINARY} --server --server-port 6742\n'
            'Environment=QT_QPA_PLATFORM=offscreen HOME=/var/lib/nekoserver-openrgb XDG_CONFIG_HOME=/var/lib/nekoserver-openrgb\nStateDirectory=nekoserver-openrgb\n'
            'Restart=on-failure\nRestartSec=10\nNoNewPrivileges=yes\nProtectHome=yes\nPrivateTmp=yes\nIPAddressDeny=any\nIPAddressAllow=localhost\n'
            '[Install]\nWantedBy=multi-user.target\n')
    return found

def units(data,code,config,port,interval,public=None,control=False):
    serve=str(code/"nekoserver-runtime")+" serve"
    collect=str(code/"nekoserver-runtime")+" collect"
    common='NoNewPrivileges=yes\nProtectHome=yes\nProtectSystem=strict\nPrivateTmp=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
    return {
    'nekoserver-dashboard-collect.service':'[Unit]\nDescription=NekoServer dashboard collection\n[Service]\nType=oneshot\nRuntimeDirectory=nekoserver-dashboard\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\nExecStart='+collect+' --config '+str(config)+' --output /run/nekoserver-dashboard/status.json --state '+str(data/'dashboard-state')+'\nTimeoutStartSec=180\n'+common+'ReadWritePaths=/run/nekoserver-dashboard -'+str(data/'dashboard-state')+'\nRestrictAddressFamilies=AF_UNIX\n',
    'nekoserver-dashboard-collect.timer':f'[Unit]\nDescription=NekoServer periodic dashboard collection\n[Timer]\nOnBootSec=15s\nOnUnitInactiveSec={interval}s\nUnit=nekoserver-dashboard-collect.service\n[Install]\nWantedBy=timers.target\n',
    # Starts with the computer and comes back by itself if it ever stops.
    SERVICE:'[Unit]\nDescription=NekoServer local dashboard\nWants=nekoserver-dashboard-collect.service\nAfter=network.target nekoserver-dashboard-collect.service\nStartLimitIntervalSec=0\n[Service]\nType=simple\nDynamicUser=yes\nExecStart='+serve+' --assets '+str(code/'dashboard')+f' --status /run/nekoserver-dashboard/status.json --port {port}'+(' --public-host '+public if public else '')+'\nRestart=always\nRestartSec=3\n'+common+'PrivateDevices=yes\nRestrictAddressFamilies=AF_INET AF_UNIX\nInaccessiblePaths=-/run/docker.sock -/var/run/docker.sock '+str(data)+' '+str(config.parent)+' -'+str(CONTROL_CONFIG)+'\n'
        # Only the control service's socket: changes are passed on, never made here.
        +('SupplementaryGroups='+CONTROL_GROUP+'\n' if control else '')+'[Install]\nWantedBy=multi-user.target\n'}

def control_group():
    try:grp.getgrnam(CONTROL_GROUP)
    except KeyError:subprocess.run(['groupadd','--system',CONTROL_GROUP],capture_output=True,check=True,timeout=30)

def install_openrgb(log):
    """OpenRGB from Arch's own repository, plus the i2c driver it uses for memory and
    motherboard lights. False (lighting skipped) when it can't be installed."""
    if not OPENRGB_BINARY.exists():
        family=host.current()
        if not family.available('openrgb'):
            log('Lighting: OpenRGB is not available for this system, so RGB control is skipped');return False
        family.install(['openrgb'])
    if write_managed(I2C_MODULES,'# Managed by NekoServer: lets OpenRGB reach memory and motherboard lighting.\ni2c-dev\n',0o644,log):
        subprocess.run(['modprobe','i2c-dev'],capture_output=True,timeout=30,check=False)
    return True

def choose_pin(binary,log,ask=getpass.getpass):
    """Phones and tablets need a PIN to change fans and lights (the server itself
    doesn't), and restarting or closing apps needs it everywhere. Asked once; skipped
    without a keyboard. Only a slow salted hash is kept."""
    if (CONTROL_CONFIG/'pin.json').exists():return False
    if not sys.stdin.isatty():
        log('Dashboard: no PIN chosen, so fans and lights change on the server only and apps can\'t be restarted or closed from it (run setup in a terminal to choose one)');return False
    print('\nChoose a PIN (4 to 8 digits) for the dashboard: it lets you change fans and lights from your phone or tablet,\n'
          'and restart or close apps (on every device, the server too).\nPress Enter to skip: fans and lights then change on the server itself only, and apps can\'t be restarted or closed from the dashboard.')
    for _ in range(3):
        pin=ask('PIN: ').strip()
        if not pin:log('Dashboard: no PIN chosen; fans and lights change on the server only, and app controls stay off');return False
        if not (pin.isdigit() and 4<=len(pin)<=8):print('A PIN is 4 to 8 digits.');continue
        if ask('Same PIN again: ').strip()!=pin:print('The two PINs differ.');continue
        subprocess.run([str(binary),'control','--set-pin'],input=pin+'\n',text=True,capture_output=True,check=True,timeout=120)
        log('Dashboard: PIN saved');return True
    log('Dashboard: no PIN chosen; fans and lights change on the server only, and app controls stay off');return False

def launcher(port):
    """A command that opens the dashboard in its own window, without browser tabs or
    bars: an installed Chromium-family browser in app mode, otherwise the default
    browser."""
    url=f'http://127.0.0.1:{port}/'
    return ('#!/bin/sh\n# Managed by NekoServer: opens the dashboard in its own window.\n'
            f'url={url}\n'
            'for browser in chromium google-chrome-stable google-chrome brave microsoft-edge-stable vivaldi-stable; do\n'
            '  if command -v "$browser" >/dev/null 2>&1; then exec "$browser" --app="$url" --class=NekoServer; fi\n'
            'done\n'
            'if command -v firefox >/dev/null 2>&1; then exec firefox --new-window "$url"; fi\n'
            'exec xdg-open "$url"\n')


def desktop_entry(code):
    return ('[Desktop Entry]\nType=Application\nName=NekoServer Dashboard\nComment=Your home server: apps, backups, hardware and sensors\n'
            f'Exec={LAUNCHER}\nIcon={code/"dashboard"/"icon-512.png"}\nTerminal=false\nCategories=System;Monitor;\nStartupWMClass=NekoServer\n{MANAGED}\n')


def write_managed(path,text,mode,log):
    """Write a file NekoServer owns; one someone else made at that place is kept."""
    path=safe_path(path)
    if path.exists() and 'Managed by NekoServer' not in path.read_text(errors='replace') and MANAGED not in path.read_text(errors='replace'):
        log(f'Dashboard: kept your own {path}');return False
    if not path.exists() or path.read_text()!=text:atomic_write(path,text,mode)
    return True


COLLECTOR_SETTINGS=Path('/etc/nekoserver-dashboard/config.json')

def collector_settings(cfg,smart,interval=60):
    """What the dashboard collector reads, including the fingerprint of the settings an
    acceptance report must match."""
    clean={k:cfg[k] for k in ('data_mount','media_mount','external_backup_mount','storage')};clean['media']={'services':cfg['media']['services']};clean['photos']={'deploy_immich':cfg.get('photos',{}).get('deploy_immich',False),'archive_interval_minutes':cfg.get('photos',{}).get('archive_interval_minutes',60),'backup_schedule':cfg.get('photos',{}).get('backup_schedule','legacy')};clean['music']={'backup_enabled':cfg.get('music',{}).get('backup_enabled',True)};clean['monitoring']={'enabled':cfg.get('monitoring',{}).get('enabled',True),'interval_minutes':cfg.get('monitoring',{}).get('interval_minutes',60)};clean['dashboard']={'smart_interval_seconds':smart,'interval_seconds':interval};clean['heartbeat']={'interval_seconds':cfg.get('heartbeat',{}).get('interval_seconds',300)};clean['runtime']={'backend':cfg.get('runtime',{}).get('backend')};clean['gateway']={k:cfg.get('gateway',{}).get(k) for k in ('services','lan_ip','lan_cidr')}
    clean['nextcloud']={'enabled':cfg.get('nextcloud',{}).get('enabled',False)}
    clean['acceptance_scope']=scope_digest(cfg)
    return clean

def with_public_host(unit,public):
    """The web service's definition with its Tailscale name set (or removed)."""
    lines=unit.split('\n')
    for i,line in enumerate(lines):
        if line.startswith('ExecStart=') and ' serve ' in line:
            plain=re.sub(r' --public-host \S+','',line)
            lines[i]=plain+(' --public-host '+public if public else '')
    return '\n'.join(lines)

def refresh_public_host(cfg,log,unit_path=None):
    """Remote access set up after the dashboard: the web service must also answer its
    Tailscale name, or the phone gets E41. Rewritten and restarted only on a change."""
    unit_path=unit_path or SERVICE_UNIT
    if not unit_path.is_file():return False
    current=unit_path.read_text()
    if not current.startswith(MANAGED_UNIT):return False
    wanted=with_public_host(current,tailnet_name(cfg,public_host_of(current)))
    if wanted==current:return False
    atomic_write(unit_path,wanted,0o644)
    systemctl('daemon-reload',timeout=30)
    systemctl('restart',SERVICE)
    log('Dashboard: now also answers its Tailscale address')
    return True

def refresh(cfg,log,target=None):
    """After every setup step: settings chosen after the dashboard step (TV, heartbeat,
    remote access, ...) are part of its fingerprint, so its collector settings are
    written again from the final configuration, and the web service learns a Tailscale
    name set up later. Nothing happens when the dashboard is not installed."""
    target=target or COLLECTOR_SETTINGS
    if not target.is_file():return False
    refresh_public_host(cfg,log)
    _port,interval,smart=options(cfg)
    wanted=collector_settings(cfg,smart,interval)
    try:
        current=json.loads(target.read_text())
    except (OSError,ValueError):
        current=None
    if current==wanted:return False
    atomic_json(target,wanted);log('Dashboard: settings refreshed with the final setup choices')
    return True

def check_payloads(payloads):
    for path,payload in payloads.items():
        safe_path(path)
        if not path.exists():continue
        if path.read_bytes()!=payload:raise RuntimeError('Installed dashboard release differs; custom files preserved')
        if path.name==native_runtime.NAME and path.stat().st_mode&0o005!=0o005:raise RuntimeError('Dashboard native executable must be readable/executable by its unprivileged account; review its permissions')

def check_folders(folders):
    for folder in folders:
        safe_path(folder)
        if folder.exists() and (not folder.is_dir() or folder.stat().st_mode&0o005!=0o005):raise RuntimeError('Existing dashboard asset directory permissions block its unprivileged account; review locally')

def check_existing(payloads,definitions,folders):
    """Stop before changing anything when files, units or folders someone else changed
    are in the way."""
    check_payloads(payloads);check_units(definitions,'dashboard');check_folders(folders)

def install_smart(cfg,log):
    if not cfg.get('dashboard',{}).get('install_smartmontools',True) or shutil.which('smartctl'):return
    family=host.current()
    if not family.available('smartmontools'):raise RuntimeError('smartmontools unavailable; update the system normally and retry')
    family.install(['smartmontools'])

def prepare_control(code,definitions,lighting,log,fans=True,data=None):
    """The control group and OpenRGB; lighting is left out when OpenRGB can't be had.
    Returns whether lighting is on."""
    control_group()
    if lighting and not install_openrgb(log):
        del definitions[OPENRGB];definitions.update(control_units(code,False,fans,data))
        return False
    return lighting

def write_files(folders,payloads,definitions):
    for folder in folders:
        if not folder.exists():
            folder.mkdir(parents=True,mode=0o755);folder.chmod(0o755)
    for path,payload in payloads.items():
        if not path.exists():atomic_write(path,payload,0o755 if path.name==native_runtime.NAME else 0o644)
    write_units(definitions)

def systemctl(*args,timeout=60):
    subprocess.run(['systemctl',*args],capture_output=True,check=True,timeout=timeout)

# Fans NekoServer took over but couldn't hand back are recorded here (kept on stop).
CONTROL_JOURNALS=(Path('/run/nekoserver-control/originals.json'),Path('/run/nekoserver-control/originals-detect.json'))

def stop_control(units):
    """Fan and light control (or just lighting) switched off: the services must really
    stop, and when fan control stops every fan must be back with the motherboard,
    before setup carries on."""
    result=subprocess.run(['systemctl','disable','--now',*units],capture_output=True,text=True,timeout=120, check=False)
    if result.returncode:
        raise RuntimeError('Fan and light control could not be switched off ('+(result.stderr or '').strip()[:200]+'). Check: systemctl status '+' '.join(units))
    for unit in units:
        if subprocess.run(['systemctl','is-active','--quiet',unit],timeout=30,check=False).returncode==0:
            raise RuntimeError(f'{unit} is still running after being switched off. Check: systemctl status {unit}')
    # Fan records are only left over when the fan service itself stopped: while it runs
    # (lighting switched off on its own) they belong to the fans it still controls.
    if CONTROL in units and any(p.exists() for p in CONTROL_JOURNALS):
        raise RuntimeError('Fan control stopped, but some fans did not go back to the motherboard. Check the fans, then run: sudo systemctl start nekoserver-control.service (it keeps trying), or restart the computer')

def fans_handed_back(wait=15):
    """True once no fan record is left (the service removes each record as the fan goes
    back to the motherboard); checked for up to `wait` seconds."""
    for _ in range(wait*2):
        if not any(p.exists() for p in CONTROL_JOURNALS):return True
        time.sleep(0.5)
    return False

def start_services(definitions,code,control,log,fans=True):
    systemctl('daemon-reload',timeout=30)
    systemctl('start','nekoserver-dashboard-collect.service',timeout=190)
    started=[u for u in (OPENRGB,CONTROL) if u in definitions]
    stopped=[u for u in (CONTROL,OPENRGB) if u not in definitions and (SYSTEMD_UNITS/u).exists()]
    if stopped:stop_control(stopped)
    if control:
        CONTROL_CONFIG.mkdir(mode=0o700,exist_ok=True)
        choose_pin(code/native_runtime.NAME,log)
    for unit in ('nekoserver-dashboard-collect.timer',*started,SERVICE):systemctl('enable','--now',unit)
    for unit in (*started,SERVICE):systemctl('restart',unit)
    # Fan control switched off: the restart handed every fan back (or the service keeps
    # trying, at full speed meanwhile). Setup says so instead of carrying on. The service
    # hands them back as it starts, so it gets a moment to do so.
    if CONTROL in started and not fans and not fans_handed_back():
        raise RuntimeError('Fan control is switched off, but some fans did not go back to the motherboard. Check the fans, '
                           'then run: sudo systemctl restart nekoserver-control.service (it keeps trying), or restart the computer')

def asset_payloads(project,code):
    """The dashboard page's files from a release folder (this one, or the copy preserved
    on the SSD for reinstalling), keyed by where they are installed."""
    return {code/'dashboard'/name:(project/'dashboard'/name).read_bytes() for name in ASSETS}

def run(ctx):
    port,interval,smart=options(ctx.config)
    if not ctx.apply:ctx.log('Local dashboard available with --apply at http://127.0.0.1:'+str(port));return
    if os.geteuid()!=0:raise RuntimeError('Dashboard installation requires sudo')
    if not shutil.which('systemctl') or platform.system()!='Linux':raise RuntimeError('Dashboard installation requires Linux/systemd')
    cfg=ctx.config;data=mount_path(cfg['data_mount']);project=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('dashboard_storage_guard',project/'storage-guard.py');guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard);guard.verify(cfg)
    code=safe_path(Path('/usr/local/lib/nekoserver-dashboard')/('v'+VERSION));config=safe_path(COLLECTOR_SETTINGS)
    payloads=asset_payloads(project,code)
    payloads[code/native_runtime.NAME]=native_runtime.payload(project,cfg)
    public=tailnet_name(cfg,public_host_of(installed_unit()))
    control,lighting=control_options(cfg)
    # The control service always runs: app controls need it even with fan control off.
    definitions=units(data,code,config,port,interval,public,True)
    definitions.update(control_units(code,lighting,control,data))
    folders=(code.parent,code,code/'dashboard')
    check_existing(payloads,definitions,folders)
    install_smart(cfg,ctx.log)
    lighting=prepare_control(code,definitions,lighting,ctx.log,control,data)
    write_files(folders,payloads,definitions)
    atomic_json(config,collector_settings(cfg,smart,interval));safe_path(data/'dashboard-state').mkdir(mode=0o700,exist_ok=True)
    start_services(definitions,code,True,ctx.log,fans=control)
    write_managed(LAUNCHER,launcher(port),0o755,ctx.log)
    write_managed(DESKTOP_ENTRY,desktop_entry(code),0o644,ctx.log)
    ctx.log(f'NekoServer dashboard: http://127.0.0.1:{port}/ · backup history starts after updated jobs run')
    if control:ctx.log('Fans & lights: set fan curves'+(' and RGB lighting' if lighting else '')+' on the dashboard\'s "Fans & lights" page')
    ctx.log('Apps: right-click an app (Your apps) or a program (Processes) on the dashboard to restart or close it; it asks for your PIN')
    ctx.log('Open it from the app menu ("NekoServer Dashboard") for its own window; on a phone or tablet, use "Install app" / "Add to Home Screen"')
    if public:ctx.log(f'Dashboard on your phone (Tailscale on): https://{public}:8448/')
