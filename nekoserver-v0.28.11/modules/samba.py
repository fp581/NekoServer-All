"""Explicit LAN or tailnet Samba shares, no guest access or destructive configuration replacement."""
import os,pwd,shutil,subprocess,sys
import network_access
from pathlib import Path
import host
from neko_common import safe_path, atomic_write, findmnt, SYSTEMD_UNITS
DEPENDENCIES=['storage','system']
DESCRIPTION='Set up explicit LAN or tailnet SMB shares with no guest access'

def validate_share(name,relative):
    if not name or not name.replace('_','').isalnum():raise RuntimeError('Invalid SMB share name')
    p=Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts or str(p)=='.':raise RuntimeError('SMB share path must be a relative safe path')
    return p

def share_config(settings,media,user):
    shares=settings.get('shares',{})
    if not shares:raise RuntimeError('At least one share is required')
    mode=settings.get('share_mode','tailnet')
    if mode not in ('lan','tailnet'):raise RuntimeError('Samba share_mode must be lan or tailnet')
    ip,net=network_access.lan({'gateway':{'lan_ip':settings.get('lan_ip'),'lan_cidr':settings.get('lan_cidr')}}) if mode=='lan' else ('tailscale0','100.64.0.0/10')
    out=['# Managed by NekoServer; never expose Samba to public interfaces', '[global]', '   server role = standalone server', '   security = user', '   map to guest = Never', '   guest ok = no', '   interfaces = lo '+ip, '   bind interfaces only = yes', '   hosts allow = 127. '+net, '   hosts deny = ALL', '   smb ports = 445', '   disable netbios = yes', '   server min protocol = SMB2_10', '']
    for name,rel in shares.items():
        p=validate_share(name,rel)
        out += [f'[{name}]',f'   path = {media/p}', '   browseable = yes', '   guest ok = no', f'   valid users = {user}', f'   read only = {"no" if name=="Media" and settings.get("samba_media_write",False) else "yes"}', '   follow symlinks = no','']
    return '\n'.join(out)

def wanted_shares(config):
    """The shares to set up: the settings' list, without the default Photos share when
    Immich isn't set up (its photo archive only exists with Immich). A share you added
    or changed yourself stays, and must exist."""
    shares=dict(config.get('networking',{}).get('shares',{}))
    archive=config.get('photos',{}).get('archive_dir','photos/archive')
    if not config.get('photos',{}).get('deploy_immich',False) and shares.get('Photos')==archive:
        del shares['Photos']
    return shares

MANAGED_HEADER='# Managed by NekoServer'
SHARE_FILE='/etc/samba/nekoserver.conf'   # NekoServer's own share file, included by smb.conf

def check_rewrite(current,mode,settings):
    """NekoServer's own share file is rewritten when the settings change (a share added,
    Immich switched on, write access, another account). Two cases stop setup: a file
    someone else wrote, and widening tailnet-only sharing to the home network, which
    you confirm with "allow_tailnet_to_lan": true under "networking"."""
    if not current.startswith(MANAGED_HEADER):
        raise RuntimeError('/etc/samba/nekoserver.conf was not written by NekoServer; review it, move it away and run setup again')
    was_tailnet='   interfaces = lo tailscale0' in current
    if mode=='lan' and was_tailnet and settings.get('allow_tailnet_to_lan') is not True:
        raise RuntimeError('Shared folders were reachable through Tailscale only and would now be open to your home network too. '
                           'If that is what you want, set "allow_tailnet_to_lan": true under "networking" and run setup again')

def previous_account(path=Path(SHARE_FILE)):
    """The account the shares already belong to (from NekoServer's own share file), or
    None before the first setup."""
    try:
        text=path.read_text()
    except OSError:
        return None
    if not text.startswith(MANAGED_HEADER):return None
    users={line.split('=',1)[1].strip() for line in text.splitlines() if line.strip().startswith('valid users =')}
    return users.pop() if len(users)==1 else None

def samba_account(settings,previous=None):
    """The account the shares belong to. "auto" (the default) is the person who ran sudo
    the first time; later runs keep that account, whoever runs setup then (another
    administrator, or sudo from a second account), instead of silently switching the
    shares to them. networking.samba_user names another account on purpose."""
    chosen=settings.get('samba_user','auto')
    if chosen!='auto':return chosen
    return (previous_account() if previous is None else previous) or os.environ.get('SUDO_USER','')

def call(args,timeout=180):
    p=subprocess.run(args,text=True,capture_output=True,timeout=timeout, check=False)
    if p.returncode:raise RuntimeError(f'{args[0]} failed: {p.stderr[-500:]}')
    return p.stdout.strip()

def samba_password(user,run=subprocess.run,tries=3):
    """smbpasswd asks for the password twice on the terminal. Two that differ (or an
    empty one) make it fail; that is a typo, not a broken setup, so it is asked again."""
    for attempt in range(tries):
        if run(['smbpasswd','-a',user],check=False).returncode==0:
            return True
        if attempt<tries-1:
            print('The two passwords did not match (or one was empty). Try again.')
    return False

def includes_ours(text):
    """smb.conf loads NekoServer's share file: the tiny file setup writes, or your own
    file with that include line added (comments and spacing aside)."""
    for line in text.splitlines():
        key, _, value = line.partition('=')
        if key.strip().lower() == 'include' and value.strip() == SHARE_FILE:
            return True
    return False

def run(context):
    if not context.apply:
        context.log('Samba setup deferred; run --module samba --apply')
        return
    if os.geteuid()!=0:raise RuntimeError('Root required to install Samba')
    family=host.current();service=family.service('samba')
    settings=context.config.get('networking',{})
    settings=dict(settings,shares=wanted_shares(context.config));mode=settings.get('share_mode','tailnet')
    if mode not in ('lan','tailnet'):raise RuntimeError('Samba share_mode must be lan or tailnet')
    if mode=='lan':
        # Chosen here when Samba runs first; the LAN gateway (HTTPS) is not needed for it.
        if not network_access.choose_lan(context):
            return {'status':'needs_setup','reason':'Choose the home-network address: run setup again in a terminal on the server (it asks which network connects to your router), or set gateway.lan_ip and gateway.lan_cidr'}
        settings['lan_ip']=context.config['gateway']['lan_ip'];settings['lan_cidr']=context.config['gateway']['lan_cidr']
        network_access.verify_lan({'gateway':settings})
    elif settings.get('tailnet_only') is not True:raise RuntimeError('Tailnet sharing requires tailnet_only=true')
    user=samba_account(settings)
    if not user or user=='root' or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for ch in user):
        raise RuntimeError('Run with sudo from a real desktop account, or configure networking.samba_user explicitly')
    pwd.getpwnam(user)
    media=Path(context.config['media_mount'])
    if media.is_symlink() or not media.is_dir():raise RuntimeError('Media mount missing or symlink')
    if findmnt('TARGET',media)!=str(media):raise RuntimeError('Media HDD is not a dedicated mount')
    if mode=='tailnet':
        if not shutil.which('tailscale') or not Path('/sys/class/net/tailscale0').exists():raise RuntimeError('Authenticate Tailscale before tailnet Samba setup')
    for name,rel in settings.get('shares',{}).items():
        path=safe_path(media/validate_share(name,rel))
        if not path.is_dir() or path.is_symlink():raise RuntimeError(f'Share directory missing or unsafe: {path}')
    if not shutil.which('smbd'):
        family.install(['samba'])
    main=safe_path('/etc/samba/smb.conf'); managed=safe_path(SHARE_FILE)
    desired=share_config(settings,media,user)
    # Safest: don't modify a pre-existing unmanaged Samba installation.
    if main.exists() and not includes_ours(main.read_text()):
        if family.packaged_example(str(main)):
            # Installed before NekoServer (with smbclient, say) and never edited.
            raise RuntimeError('/etc/samba/smb.conf is the Samba package\'s unchanged example, not your settings. '
                               'If you don\'t use Samba for anything else, move it aside '
                               '(sudo mv /etc/samba/smb.conf /etc/samba/smb.conf.packaged) and run setup again.')
        raise RuntimeError('/etc/samba/smb.conf exists and was not written by NekoServer, so it is left alone. '
                           'NekoServer keeps its shares in /etc/samba/nekoserver.conf. If you don\'t use Samba for '
                           'anything else, move your file aside (sudo mv /etc/samba/smb.conf /etc/samba/smb.conf.mine) '
                           'and run setup again. To keep your own shares as well, add the line\n'
                           '   include = /etc/samba/nekoserver.conf\nto the [global] section of your smb.conf and '
                           'run setup again; your file then stays as it is.')
    if managed.exists() and managed.read_text()!=desired:
        check_rewrite(managed.read_text(),mode,settings)
    managed.parent.mkdir(parents=True,exist_ok=True)
    if not managed.exists() or managed.read_text()!=desired:atomic_write(managed,desired,0o644)
    # Default samba configuration is a tiny [global] include, only when no config exists.
    if not main.exists():atomic_write(main,'[global]\n   include = /etc/samba/nekoserver.conf\n',0o644)
    call(['testparm','-s',str(main)],timeout=30)
    unit=call(['systemd-escape','--path','--suffix=mount',str(media)])
    override=SYSTEMD_UNITS/(service+'.d')/'10-nekoserver-storage.conf'
    content=f'# Managed by NekoServer\n[Unit]\nRequiresMountsFor={media}\nBindsTo={unit}\nAfter={unit} {family.service("tailscale") if mode=="tailnet" else "network-online.target"}\n'
    if override.exists() and not safe_path(override).read_text().startswith('# Managed by NekoServer\n'):
        raise RuntimeError('Unmanaged Samba storage guard')
    atomic_write(override,content,0o644)
    call(['systemctl','daemon-reload'])
    call(['systemctl','enable','--now',service],timeout=40)
    call(['systemctl','restart',service],timeout=40)
    account=subprocess.run(['pdbedit','-L','-u',user],capture_output=True,text=True,timeout=15,check=False)
    has_account=account.returncode==0 and any(line.startswith(user+':') for line in account.stdout.splitlines())
    if not has_account:
        if not sys.stdin.isatty():
            context.log('Samba service installed; authenticated user setup is still required')
            return {'status':'needs_setup','reason':'Run interactively to create the Samba password, or use sudo smbpasswd -a '+user}
        # Samba prompts directly on the terminal; NekoServer does not store/log the password.
        print('\nShared folders: choose a password for opening the server\'s folders from other '
              'computers (user: '+user+'). Type it twice; nothing is shown while you type.')
        if not samba_password(user):
            return {'status':'needs_setup','reason':'No Samba password was set (the two did not match three times). Run setup again, or use  sudo smbpasswd -a '+user}
        account=subprocess.run(['pdbedit','-L','-u',user],capture_output=True,text=True,timeout=15,check=False)
        if account.returncode or not any(line.startswith(user+':') for line in account.stdout.splitlines()):
            raise RuntimeError('Samba user was not retained; inspect local authentication setup')
    context.log(f'Samba configured for {mode}, authenticated user {user}; existing password preserved')

