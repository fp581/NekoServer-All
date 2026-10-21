"""Explicit LAN or tailnet Samba shares, no guest access or destructive configuration replacement."""
import os,pwd,shutil,subprocess,sys
import network_access
from pathlib import Path
import host
from neko_common import safe_path, atomic_write, SYSTEMD_UNITS
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

def call(args,timeout=180):
    p=subprocess.run(args,text=True,capture_output=True,timeout=timeout, check=False)
    if p.returncode:raise RuntimeError(f'{args[0]} failed: {p.stderr[-500:]}')
    return p.stdout.strip()

def run(context):
    if not context.apply:
        context.log('Samba setup deferred; run --module samba --apply')
        return
    if os.geteuid()!=0:raise RuntimeError('Root required to install Samba')
    family=host.current();service=family.service('samba')
    settings=context.config.get('networking',{})
    settings=dict(settings);mode=settings.get('share_mode','tailnet')
    if mode not in ('lan','tailnet'):raise RuntimeError('Samba share_mode must be lan or tailnet')
    if mode=='lan':
        settings['lan_ip']=context.config.get('gateway',{}).get('lan_ip');settings['lan_cidr']=context.config.get('gateway',{}).get('lan_cidr')
        if settings.get('lan_ip') in (None,'auto') or settings.get('lan_cidr') in (None,'auto'):
            return {'status':'needs_setup','reason':'Select the physical LAN address/subnet through gateway_lan setup before installing LAN shares'}
        network_access.verify_lan({'gateway':settings})
    elif settings.get('tailnet_only') is not True:raise RuntimeError('Tailnet sharing requires tailnet_only=true')
    user=settings.get('samba_user','auto')
    if user=='auto': user=os.environ.get('SUDO_USER','')
    if not user or user=='root' or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for ch in user):
        raise RuntimeError('Run with sudo from a real desktop account, or configure networking.samba_user explicitly')
    pwd.getpwnam(user)
    media=Path(context.config['media_mount'])
    if media.is_symlink() or not media.is_dir():raise RuntimeError('Media mount missing or symlink')
    if call(['findmnt','-n','-o','TARGET','--mountpoint',str(media)])!=str(media):raise RuntimeError('Media HDD is not a dedicated mount')
    if mode=='tailnet':
        if not shutil.which('tailscale') or not Path('/sys/class/net/tailscale0').exists():raise RuntimeError('Authenticate Tailscale before tailnet Samba setup')
    for name,rel in settings.get('shares',{}).items():
        path=safe_path(media/validate_share(name,rel))
        if not path.is_dir() or path.is_symlink():raise RuntimeError(f'Share directory missing or unsafe: {path}')
    if not shutil.which('smbd'):
        family.install(['samba'])
    main=safe_path('/etc/samba/smb.conf'); managed=safe_path('/etc/samba/nekoserver.conf')
    desired=share_config(settings,media,user)
    # Safest: don't modify a pre-existing unmanaged Samba installation.
    if main.exists() and main.read_text()!='[global]\n   include = /etc/samba/nekoserver.conf\n':
        raise RuntimeError('Existing unmanaged /etc/samba/smb.conf; refusing to alter it. Review manually.')
    if managed.exists() and managed.read_text()!=desired:
        legacy=dict(settings);legacy['share_mode']='tailnet'
        if not (mode=='lan' and settings.get('allow_tailnet_to_lan') is True and managed.read_text()==share_config(legacy,media,user)):raise RuntimeError('Existing Samba config differs; review it or explicitly authorize a recognized tailnet-to-LAN transition')
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
        subprocess.run(['smbpasswd','-a',user],check=True)
        account=subprocess.run(['pdbedit','-L','-u',user],capture_output=True,text=True,timeout=15,check=False)
        if account.returncode or not any(line.startswith(user+':') for line in account.stdout.splitlines()):
            raise RuntimeError('Samba user was not retained; inspect local authentication setup')
    context.log(f'Samba configured for {mode}, authenticated user {user}; existing password preserved')

