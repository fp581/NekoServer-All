"""Nyarch/Arch TV desktop setup, installed for a real non-root desktop user."""
import guided_setup
import hashlib
import os
import platform
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from neko_common import app_port, safe_path, atomic_write, atomic_json, arch_host

DEPENDENCIES=['system','recovery']
DESCRIPTION='Install native Steam, graphics/controller support and TV launch shortcuts on Nyarch/Arch'
SCHEMA='org.gnome.settings-daemon.plugins.power'
POWER_KEY='sleep-inactive-ac-type'
REPORT='reports/tv-setup.json'


def command(argv, timeout=30):
    return subprocess.run(argv,capture_output=True,text=True,timeout=timeout,check=False)


from gpu import graphics, vulkan_packages  # noqa: E402  (shared with hardware_video)


def previous_checks(root):
    """TV hardware tests already confirmed in an earlier run."""
    try:
        import json
        report=json.loads((Path(root)/REPORT).read_text())
        return {row['check'] for row in report.get('checks',[]) if isinstance(row,dict) and row.get('status')=='user_confirmed'}
    except (OSError,ValueError,KeyError,TypeError):
        return set()


def desktop_user(config):
    selected=config.get('tv',{}).get('user','auto')
    if selected=='auto':
        sudo=os.environ.get('SUDO_USER')
        user=pwd.getpwnam(sudo) if sudo and sudo!='root' else pwd.getpwuid(int(config.get('media',{}).get('uid',1000)))
    else:
        if not isinstance(selected,str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?',selected):raise RuntimeError('Invalid TV desktop user')
        user=pwd.getpwnam(selected)
    home=safe_path(user.pw_dir)
    if user.pw_uid<1000 or user.pw_name=='root' or user.pw_shell.endswith(('nologin','false')) or not home.is_absolute() or home==Path('/') or not home.is_dir() or home.stat().st_uid!=user.pw_uid:
        raise RuntimeError('TV setup needs an existing non-root desktop user with an owned home directory; set tv.user')
    return user,home


def desktop_entry(name,exec_line,icon):
    return f'[Desktop Entry]\nType=Application\nName=NekoServer {name}\nExec={exec_line}\nIcon={icon}\nTerminal=false\nCategories=AudioVideo;Game;\nX-NekoServer-Managed=true\n'


def launchers(config):
    files={'nekoserver-steam.desktop':desktop_entry('Steam Big Picture','/usr/bin/steam -gamepadui','steam'),
           'nekoserver-youtube.desktop':desktop_entry('YouTube','/usr/bin/firefox --new-window https://www.youtube.com/','web-browser')}
    for app,name in [('jellyfin','Jellyfin'),('navidrome','Music'),('seerr','Requests')]:
        port=app_port(app)
        if app in config.get('media',{}).get('services',[]):files['nekoserver-'+app+'.desktop']=desktop_entry(name,f'/usr/bin/firefox --new-window http://127.0.0.1:{port}/','applications-multimedia')
    port=config.get('dashboard',{}).get('port',8787)
    if type(port) is int and 1024<=port<=65535:
        # The dashboard's own launcher opens it in an app window (see modules/dashboard.py).
        from modules.dashboard import LAUNCHER
        files['nekoserver-dashboard.desktop']=desktop_entry('Control Room',str(LAUNCHER) if LAUNCHER.is_file() else f'/usr/bin/firefox --new-window http://127.0.0.1:{port}/','utilities-system-monitor')
    return files


def user_command(user,home,argv):
    return ['runuser','-u',user.pw_name,'--','env','HOME='+str(home),'XDG_CONFIG_HOME='+str(home/'.config'),'XDG_DATA_HOME='+str(home/'.local/share'),*argv]


def desktop_folder(user,home):
    if not shutil.which('xdg-user-dir'):return home/'Desktop'
    result=command(user_command(user,home,['xdg-user-dir','DESKTOP']))
    if result.returncode:return home/'Desktop'
    value=safe_path(result.stdout.strip())
    if not value.is_absolute() or home not in value.parents:raise RuntimeError('Desktop location must be a non-symlink directory inside the user home')
    return value


def ensure_user_dir(path,home,user):
    path=safe_path(path)
    if path!=home and home not in path.parents:raise RuntimeError('User directory outside the selected home')
    for item in reversed((path,*path.parents)):
        if item==home or home in item.parents:
            if item.exists():
                if not item.is_dir() or item.stat().st_uid!=user.pw_uid:raise RuntimeError('Existing TV user directory has incompatible ownership/type')
            else:item.mkdir(mode=0o755);os.chown(item,user.pw_uid,user.pw_gid)


def power_setup(user,home):
    if not shutil.which('gsettings') or not shutil.which('dbus-run-session'):
        return {'check':'tv:idle_suspend','status':'needs_setup','reason':'Disable automatic AC suspend in desktop power settings to keep the server running'}
    def call(args):return command(user_command(user,home,['dbus-run-session','--','gsettings',*args]))
    writable=call(['writable',SCHEMA,POWER_KEY])
    if writable.returncode or writable.stdout.strip()!='true':
        return {'check':'tv:idle_suspend','status':'needs_setup','reason':'GNOME AC suspend setting unavailable/locked; review desktop power settings'}
    result=call(['set',SCHEMA,POWER_KEY,'nothing']);saved=call(['get',SCHEMA,POWER_KEY])
    return {'check':'tv:idle_suspend','status':'pass' if not result.returncode and not saved.returncode and saved.stdout.strip()=="'nothing'" else 'needs_setup'}


def run(ctx):
    if not ctx.apply:ctx.log('TV setup available through the tv/full profile with --apply');return
    if os.geteuid()!=0:raise RuntimeError('TV package installation requires sudo')
    if platform.machine()!='x86_64' or not arch_host() or not shutil.which('pacman'):raise RuntimeError('TV installation currently supports x86_64 Nyarch/Arch; Bazzite backend remains pending')
    if not shutil.which('runuser'):raise RuntimeError('runuser is required to configure the desktop user safely')
    if guided_setup.wanted(ctx,'tv','Use a TV connected to this server',[
            'If a TV is plugged into this PC, NekoServer installs Steam Big Picture, adds app',
            'shortcuts to your desktop, and helps you test a controller and the TV picture.',
            'Skip it if this server has no TV; your apps work on your other devices either way.',
    ]) is False:
        ctx.log('TV setup: skipped by choice');return
    cfg=ctx.config;opts=cfg.get('tv',{});user,home=desktop_user(cfg)
    if any(key in opts and type(opts[key]) is not bool for key in ('desktop_shortcuts','autostart_big_picture','bluetooth','keep_awake')):raise RuntimeError('TV switches must be JSON booleans')
    root=safe_path(cfg['data_mount'])
    if not os.path.ismount(root):raise RuntimeError('Mounted application SSD required for the TV report')
    desktop=desktop_folder(user,home) if opts.get('desktop_shortcuts',True) else None
    files=launchers(cfg);destinations={home/'.local/share/applications'/name:text for name,text in files.items()}
    if desktop:destinations.update({desktop/name:text for name,text in files.items()})
    autostart=safe_path(home/'.config/autostart/nekoserver-steam.desktop')
    if opts.get('autostart_big_picture',False):destinations[autostart]=files['nekoserver-steam.desktop']
    # Inspect every launcher/ancestor before any packages, power settings or files change.
    for path,text in destinations.items():
        path=safe_path(path)
        if path.exists() and (not path.is_file() or path.read_text()!=text):raise RuntimeError('Existing TV launcher differs; custom shortcut preserved')
        if path.exists() and (path.stat().st_uid!=user.pw_uid or not path.stat().st_mode & 0o100):raise RuntimeError('Existing TV launcher ownership/executable mode needs review; file preserved')
        for ancestor in (path.parent,*path.parent.parents):
            if ancestor==home or home in ancestor.parents:
                if ancestor.exists() and (not ancestor.is_dir() or ancestor.stat().st_uid!=user.pw_uid):raise RuntimeError('TV shortcut directory is not owned by the desktop user')
    if opts.get('keep_awake',True):
        power_dir=safe_path(home/'.config')
        if power_dir.exists() and (not power_dir.is_dir() or power_dir.stat().st_uid!=user.pw_uid):raise RuntimeError('Desktop power configuration directory must be owned by the selected user')
    vendors=graphics()
    if not vulkan_packages(vendors):
        ctx.log('TV: no Intel or AMD graphics found'+(' (NVIDIA needs its own driver; see the Arch wiki "NVIDIA" page)' if 'nvidia' in vendors else '')+'; installing Steam without a Vulkan driver package')
    packages=['steam','steam-devices',*vulkan_packages(vendors),'firefox']
    if opts.get('bluetooth',True):packages+=['bluez','bluez-utils']
    missing=[p for p in packages if command(['pacman','-Q',p]).returncode]
    unavailable=[p for p in missing if command(['pacman','-Si',p]).returncode]
    if unavailable:
        ctx.log('TV packages unavailable: '+', '.join(unavailable)+'; enable Arch multilib if Steam/lib32 packages are missing, update the system normally, then retry')
        return {'status':'needs_setup','reason':'Required TV packages unavailable in configured repositories; no repository files changed'}
    if missing:
        ctx.log('Installing TV packages: '+', '.join(missing))
        result=command(['pacman','-S','--needed','--noconfirm',*missing],timeout=1800)
        if result.returncode:raise RuntimeError('TV package installation failed; update Nyarch normally and retry')
    if any(command(['pacman','-Q',p]).returncode for p in packages):raise RuntimeError('TV package installation was not retained')
    for binary in ('/usr/bin/steam','/usr/bin/firefox'):
        if not Path(binary).is_file():raise RuntimeError('Required TV launcher executable missing')
    results=[{'check':'tv:packages','status':'pass'}]
    if opts.get('bluetooth',True):
        enabled=command(['systemctl','enable','--now','bluetooth.service'])
        active=command(['systemctl','is-active','--quiet','bluetooth.service'])
        results.append({'check':'tv:bluetooth_service','status':'pass' if not enabled.returncode and not active.returncode else 'needs_setup'})
    for path,text in destinations.items():
        ensure_user_dir(path.parent,home,user)
        if not path.exists():atomic_write(path,text,0o755);os.chown(path,user.pw_uid,user.pw_gid)
    if opts.get('keep_awake',True):
        ensure_user_dir(home/'.config',home,user)
        results.append(power_setup(user,home))
    if not opts.get('autostart_big_picture',False) and safe_path(autostart).exists():
        results.append({'check':'tv:autostart','status':'needs_setup','reason':'Existing Steam autostart preserved; review it if startup is unwanted'})
    results.append({'check':'tv:launchers','status':'pass'})
    handoffs = [
        ('steam_login_remote_play', 'Steam sign-in and Remote Play', [
            'In Steam on this server, sign into your own account. Approve any external sign-in/2FA directly in Steam.',
            'On your MAIN gaming PC, sign into Steam and enable Remote Play in Steam settings. '
            'The installer cannot open settings on that other PC.',
            'On this server, start a Remote Play stream from that PC and test a game.'
        ], [('steam', '')]),
        ('controller', 'Pair and test the Xbox controller', [
            'Connect the controller by USB, or put it into Bluetooth pairing mode and pair it in settings.',
            'In Steam, open controller settings and test buttons, sticks and navigation. '
            'Some dongle/controller models need an additional compatible driver.'
        ], [('bluetooth', ''), ('steam', '')] if opts.get('bluetooth', True) else [('steam', '')]),
        ('display_audio_playback', 'Test TV picture, audio and playback', [
            'On the connected TV, check the intended HDMI resolution and refresh rate in display settings.',
            'Play a permitted sample in the browser, check TV audio, and use the controller to navigate.',
            'Confirm yes only after testing this real display/audio/controller combination.'
        ], [('settings', ''), ('url', 'https://www.youtube.com/')]),
    ]
    confirmed = previous_checks(root)
    for key, title, instructions, targets in handoffs:
        done = ('tv:' + key) in confirmed or guided_setup.checkpoint(ctx, title, instructions, targets)
        results.append({'check': 'tv:' + key,
                        'status': 'user_confirmed' if done else 'needs_setup',
                        'reason': 'Witnessed Konsole confirmation; not an independent hardware probe'})
    atomic_json(root/REPORT,{'user':user.pw_name,'checks':results,'launchers_sha256':{str(path.relative_to(home)):hashlib.sha256(text.encode()).hexdigest() for path,text in destinations.items()}})
    ctx.log('TV launchers installed for '+user.pw_name+'; complete Steam login, Remote Play and controller/HDMI tests. Report: '+str(root/REPORT))
    if any(row['status'] == 'needs_setup' for row in results):
        return {'status':'needs_setup','reason':'TV software installed; unfinished desktop/hardware handoffs remain'}
    return {'status':'configured', 'reason':'TV setup and user-witnessed tests completed; see tv-setup.json'}
