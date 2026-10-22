"""Explicit RFC1918 interface/subnet selection; no routing or firewall mutations."""
import ipaddress,json,re,subprocess,sys

def command(args):
    try:p=subprocess.run(args,capture_output=True,text=True,timeout=10,check=False)
    except (OSError,subprocess.TimeoutExpired):raise RuntimeError("LAN interface inspection unavailable") from None
    if p.returncode:raise RuntimeError("LAN interface inspection failed")
    return p.stdout

def private_ip(value):
    try:ip=ipaddress.IPv4Address(value)
    except (ValueError,TypeError):raise RuntimeError('A specific RFC1918 LAN IPv4 address is required') from None
    if not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):raise RuntimeError('Gateway LAN address must be RFC1918 IPv4')
    return str(ip)

def lan(config):
    opts=config.get('gateway',{});ip=private_ip(opts.get('lan_ip'))
    try:net=ipaddress.IPv4Network(opts.get('lan_cidr'),strict=True)
    except (ValueError,TypeError):raise RuntimeError('Explicit canonical LAN subnet required') from None
    if not 16<=net.prefixlen<=30 or ipaddress.ip_address(ip) not in net or ipaddress.ip_address(ip) in (net.network_address,net.broadcast_address) or not any(net.subnet_of(ipaddress.ip_network(n)) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):raise RuntimeError('LAN subnet must be a restricted RFC1918 network containing the server')
    return ip,str(net)

def candidates():
    try:rows=json.loads(command(['ip','-j','address','show','up']))
    except (ValueError,RuntimeError):return []
    out=[]
    for row in rows:
        name=row.get('ifname','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name.startswith(('lo','docker','br','veth','virbr','tailscale','tun','tap','wg')):continue
        for info in row.get('addr_info',[]):
            if info.get('family')!='inet' or info.get('scope')!='global':continue
            try:ip=private_ip(info.get('local'));net=str(ipaddress.ip_network(ip+'/'+str(info['prefixlen']),strict=False));lan({'gateway':{'lan_ip':ip,'lan_cidr':net}})
            except (RuntimeError,ValueError,KeyError):continue
            out.append({'interface':name,'ip':ip,'cidr':net})
    return out

def save_settings(ctx):
    """Keep a choice made during setup in the settings file, for the next steps and runs."""
    if hasattr(ctx,'persist_config'):ctx.persist_config()
    elif getattr(ctx,'config_path',None):
        from neko_common import atomic_json
        atomic_json(ctx.config_path,ctx.config)

def choose_lan(ctx,ask=input,interactive=None):
    """The home-network address and subnet (gateway.lan_ip / lan_cidr), chosen once and
    shared by every step that needs it: LAN folder sharing (Samba) and the LAN HTTPS
    gateway. Whichever runs first asks; the other uses the answer. Settings that name
    them already are kept. False when they are "auto" and nobody is there to choose."""
    opts=ctx.config.setdefault('gateway',{})
    if opts.get('lan_ip') not in (None,'auto') and opts.get('lan_cidr') not in (None,'auto'):return True
    if not (sys.stdin.isatty() if interactive is None else interactive):return False
    rows=candidates()
    if not rows:raise RuntimeError('No usable LAN address found; configure gateway.lan_ip and lan_cidr')
    print('Which network connects this server to your home network (your router)?')
    for i,row in enumerate(rows,1):print(str(i)+'. '+row['interface']+' · '+row['ip']+' · '+row['cidr'])
    choice=ask('Choose your home-network interface number: ').strip()
    if not choice.isdigit() or not 1<=int(choice)<=len(rows):raise RuntimeError('Invalid LAN selection')
    row=rows[int(choice)-1];opts.update(lan_ip=row['ip'],lan_cidr=row['cidr'])
    save_settings(ctx)
    return True

def verify_lan(config):
    ip,net=lan(config)
    if not any(r['ip']==ip and ipaddress.ip_network(net).subnet_of(ipaddress.ip_network(r['cidr'])) for r in candidates()):raise RuntimeError('Configured LAN address/subnet is not on an active physical LAN interface')

