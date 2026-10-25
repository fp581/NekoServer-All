"""TV desktop setup (Steam Big Picture, a browser, Bluetooth), installed for a real
non-root desktop user, with the packages each Linux family has for it."""
import guided_setup
import hashlib
import os
import platform
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from neko_common import safe_path, atomic_write, atomic_json, find_program
import host

DEPENDENCIES=['system','recovery']
DESCRIPTION='Install Steam, graphics/controller support and TV launch shortcuts'
SCHEMA='org.gnome.settings-daemon.plugins.power'
POWER_KEY='sleep-inactive-ac-type'
REPORT='reports/tv-setup.json'
IDLE_CHECK='tv:idle_suspend'


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


def previous_user(config):
    """The account the TV was set up for in an earlier run (its report), or None."""
    try:
        import json
        name=json.loads((Path(config['data_mount'])/REPORT).read_text()).get('user')
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return None
    return name if isinstance(name,str) and re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?',name) else None


def desktop_user(config):
    """The person the TV, its shortcuts and the desktop shortcuts are for. "auto" is who
    ran sudo the first time; later runs keep that account (the report says who), whoever
    runs setup then. tv.user names another account on purpose."""
    selected=config.get('tv',{}).get('user','auto')
    if selected=='auto':
        sudo=previous_user(config) or os.environ.get('SUDO_USER')
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


def launchers(steam='/usr/bin/steam',browser='/usr/bin/firefox'):
    """The TV's own shortcuts, with the programs where this Linux keeps them. The
    dashboard, browser, Steam and app shortcuts come from the desktop step
    (modules/desktop.py), on every Linux."""
    return {'nekoserver-steam.desktop':desktop_entry('Steam Big Picture',steam+' -gamepadui','steam'),
            'nekoserver-youtube.desktop':desktop_entry('YouTube',browser+' --new-window https://www.youtube.com/','web-browser')}


def browser():
    """The web browser already on this PC (the desktop step's list), or None."""
    from modules.desktop import BROWSERS
    return next((found for found in (find_program(name) for name in BROWSERS) if found),None)


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


def sleep_blocked(run=command):
    """The server-OS step has masked every sleep target: no desktop setting can put the
    PC to sleep, whatever desktop (KDE, XFCE, GNOME) the TV user runs."""
    from modules.server_os import TARGETS
    result=run(['systemctl','is-enabled',*TARGETS])
    states=result.stdout.split()
    return len(states)==len(TARGETS) and all(s=='masked' for s in states)


def power_setup(user,home,run=command,blocked=None):
    """Keep the desktop from suspending the PC. The system-wide block (server_os) is what
    counts; GNOME's own suspend-on-AC setting is switched off too where it exists, so
    its power panel agrees. Other desktops have no such setting to change here."""
    if (blocked or (lambda:sleep_blocked(run)))():
        return {'check':IDLE_CHECK,'status':'pass','reason':'sleep is blocked system-wide'}
    if not shutil.which('gsettings') or not shutil.which('dbus-run-session'):
        return {'check':IDLE_CHECK,'status':'needs_setup','reason':'Disable automatic AC suspend in desktop power settings to keep the server running'}
    def call(args):return run(user_command(user,home,['dbus-run-session','--','gsettings',*args]))
    writable=call(['writable',SCHEMA,POWER_KEY])
    if writable.returncode or writable.stdout.strip()!='true':
        return {'check':IDLE_CHECK,'status':'needs_setup','reason':'GNOME AC suspend setting unavailable/locked; review desktop power settings'}
    result=call(['set',SCHEMA,POWER_KEY,'nothing']);saved=call(['get',SCHEMA,POWER_KEY])
    return {'check':IDLE_CHECK,'status':'pass' if not result.returncode and not saved.returncode and saved.stdout.strip()=="'nothing'" else 'needs_setup'}


def keep_awake(user,home,log):
    """The sleep check, plus GNOME's own setting aligned where it exists (a desktop
    without it is fine: the system-wide block is what counts)."""
    result=power_setup(user,home)
    if result['status']=='pass' and shutil.which('gsettings') and shutil.which('dbus-run-session'):
        gnome=power_setup(user,home,blocked=lambda:False)
        if gnome['status']!='pass':log('TV: no GNOME power setting to change (another desktop); sleep stays blocked system-wide')
    return result


NO_STEAM_PACKAGE='no Steam package; Flatpak Steam is the way there, which NekoServer does not set up yet'
NO_DESKTOP='No desktop on this PC (a server install); the apps work on your other devices'
# A display server or a desktop session: without one of these nobody sees a desktop here.
DESKTOP_PROGRAMS=('Xorg','Xwayland','gnome-shell','startplasma-wayland','startplasma-x11','xfce4-session','cinnamon-session','mate-session','lxqt-session')
# What each Linux family needs switched on before its repositories offer Steam (and the
# 32-bit libraries Steam needs). NekoServer never changes these itself: they are the
# owner's choice, and setup says exactly what to run.
STEAM_SOURCES={
    'arch':'enable the multilib repository: in /etc/pacman.conf remove the # before [multilib] and its Include line, then run  sudo pacman -Syu',
    'debian':"allow 32-bit packages and Debian's contrib and non-free sections:  sudo dpkg --add-architecture i386, add  contrib non-free  to every Components: line in /etc/apt/sources.list.d/*.sources (or to each deb line of /etc/apt/sources.list), then  sudo apt update",
    'ubuntu':"allow 32-bit packages (and Ubuntu's multiverse section):  sudo dpkg --add-architecture i386 && sudo add-apt-repository multiverse && sudo apt update",
    'fedora':"enable RPM Fusion's Steam repository, Fedora's third-party source for it:  sudo dnf install fedora-workstation-repositories && sudo dnf config-manager setopt rpmfusion-nonfree-steam.enabled=1",
    'suse':"add openSUSE's non-OSS repository:  sudo zypper addrepo -f https://download.opensuse.org/tumbleweed/repo/non-oss/ repo-non-oss  (Slowroll: https://download.opensuse.org/slowroll/repo/non-oss/), then  sudo zypper refresh",
}

def has_desktop(which=shutil.which):
    return any(which(name) for name in DESKTOP_PROGRAMS)

def unavailable_reason(family):
    """Why this Linux can't have Steam from its repositories (None when it can): RHEL
    and its rebuilds don't package it, and openSUSE Leap 16 dropped it with the 32-bit
    libraries it needs (Tumbleweed and Slowroll have it, in non-OSS)."""
    if family.name=='rhel':return 'RHEL, Rocky Linux, AlmaLinux and CentOS Stream have '+NO_STEAM_PACKAGE
    if family.name=='suse' and family.release.get('ID')=='opensuse-leap':return 'openSUSE Leap has '+NO_STEAM_PACKAGE
    return None

def steam_source(family):
    """What to switch on before this family's repositories offer Steam."""
    if family.name=='debian' and family.ubuntu():return STEAM_SOURCES['ubuntu']
    return STEAM_SOURCES.get(family.name,'enable the repository your Linux keeps Steam in')

def steam_ready(family):
    """The repositories can supply Steam's 32-bit side. On Debian and Ubuntu the i386
    architecture must be allowed first: without it apt still lists steam-installer, and
    installing it fails on its i386 libraries ("held broken packages")."""
    return family.name!='debian' or 'i386' in family.foreign_architectures()

def available():
    """TV setup can run on this PC: its Linux has Steam and the PC has a desktop. The
    same decision as the step itself (not_possible), so acceptance never asks for TV
    checks on a PC the step skipped (a server without a desktop, say)."""
    return host.detect() is not None and unavailable_reason(host.current()) is None and has_desktop()

def not_possible(ctx):
    """Why TV setup can't run on this PC (an unavailable result), or None when it can."""
    if os.geteuid()!=0:raise RuntimeError('TV package installation requires sudo')
    if platform.machine()!='x86_64' or not host.supported():raise RuntimeError('TV installation is not supported on this system yet')
    reason=unavailable_reason(host.current())
    if reason:
        return {'status':'unavailable','reason':reason}
    if not shutil.which('runuser'):raise RuntimeError('runuser is required to configure the desktop user safely')
    if not has_desktop():
        # No desktop on this PC (a server install): there is nothing to show on a TV.
        ctx.log('TV setup: this PC has no desktop (no graphical session installed), so there is nothing to put on a TV; skipped')
        return {'status':'unavailable','reason':NO_DESKTOP}
    return None

def run(ctx):
    if not ctx.apply:ctx.log('TV setup available through the tv/full profile with --apply');return
    impossible=not_possible(ctx)
    if impossible:return impossible
    family=host.current()
    if guided_setup.wanted(ctx,'tv','Use a TV connected to this server',[
            'If a TV is plugged into this PC, NekoServer installs Steam Big Picture and adds',
            'its shortcut and YouTube\'s, and helps you test a controller and the TV picture.',
            'Skip it if this server has no TV; your apps work on your other devices either way.',
    ]) is False:
        ctx.log('TV setup: skipped by choice');return
    cfg=ctx.config;opts=cfg.get('tv',{});user,home=desktop_user(cfg)
    if any(key in opts and type(opts[key]) is not bool for key in ('desktop_shortcuts','bluetooth','keep_awake')):raise RuntimeError('TV switches must be JSON booleans')
    root=safe_path(cfg['data_mount'])
    if not os.path.ismount(root):raise RuntimeError('Mounted application SSD required for the TV report')
    desktop=desktop_folder(user,home) if opts.get('desktop_shortcuts',True) else None
    # The browser already here opens YouTube; Firefox is installed only where there is none.
    steam_program=find_program('steam') or ('/usr/games/steam' if family.name=='debian' else '/usr/bin/steam')
    browser_program=browser() or ('/usr/bin/firefox-esr' if family.name=='debian' and not family.ubuntu() else '/usr/bin/firefox')
    files=launchers(steam_program,browser_program);destinations={home/'.local/share/applications'/name:text for name,text in files.items()}
    if desktop:destinations.update({desktop/name:text for name,text in files.items()})
    # Inspect every launcher/ancestor before any packages, power settings or files change.
    for given,text in destinations.items():
        path=safe_path(given)
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
        ctx.log('TV: no Intel or AMD graphics found'+(" (NVIDIA needs its maker's driver, installed your Linux's own way)" if 'nvidia' in vendors else '')+'; installing Steam without a Vulkan driver package')
    packages=['steam','steam-devices',*vulkan_packages(vendors)]
    if not browser():packages.append('firefox')
    if opts.get('bluetooth',True):packages+=['bluez','bluez-utils']
    missing=[p for p in packages if not family.installed(p)]
    unavailable=[p for p in missing if not family.available(p) or (p=='steam' and not steam_ready(family))]
    if unavailable:
        ctx.log('TV packages not offered by the repositories switched on here: '+', '.join(family.names(p)[0] for p in unavailable)
                +'. To get them, '+steam_source(family)+', then run setup again. NekoServer changes no repository itself.')
        return {'status':'needs_setup','reason':'Steam and its packages are not in the repositories switched on; see the setup log for what to enable (nothing was changed)'}
    if missing:
        ctx.log('Installing TV packages: '+', '.join(missing))
        try:family.install(missing,timeout=1800)
        except RuntimeError as exc:raise RuntimeError('TV package installation failed; update the system normally and retry ('+str(exc)+')') from None
    if not all(family.installed(p) for p in packages):raise RuntimeError('TV package installation was not retained')
    steam_program=find_program('steam') or steam_program;browser_program=browser() or browser_program
    for binary in (steam_program,browser_program):
        if not Path(binary).is_file():raise RuntimeError('Required TV launcher executable missing: '+binary)
    files=launchers(steam_program,browser_program)
    destinations={path:files[path.name] for path in destinations}
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
        results.append(keep_awake(user,home,ctx.log))
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
                        'reason': 'Confirmed by you in the terminal; not an independent hardware probe'})
    atomic_json(root/REPORT,{'user':user.pw_name,'checks':results,'launchers_sha256':{str(path.relative_to(home)):hashlib.sha256(text.encode()).hexdigest() for path,text in destinations.items()}})
    ctx.log('TV launchers installed for '+user.pw_name+'; complete Steam login, Remote Play and controller/HDMI tests. Report: '+str(root/REPORT))
    if any(row['status'] == 'needs_setup' for row in results):
        return {'status':'needs_setup','reason':'TV software installed; unfinished desktop/hardware handoffs remain'}
    return {'status':'configured', 'reason':'TV setup and user-witnessed tests completed; see tv-setup.json'}
