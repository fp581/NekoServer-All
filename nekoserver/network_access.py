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

# Interfaces that are never the home network: the loopback, Docker's (docker0, and
# br-<12 hex> for each Docker network), Podman's, libvirt's virtual networks, container
# veths, Tailscale and VPN tunnels. A bridge of your own that carries the home network
# (br0, bridge0, br-lan) is not one of them. (startup.rs internal_interface is the same.)
INTERNAL_PREFIXES=('lo','docker','veth','virbr','podman','cni-','tailscale','tun','tap','wg')
DOCKER_NETWORK_BRIDGE=re.compile(r'br-[0-9a-f]{12}')

def internal_interface(name):
    return name.startswith(INTERNAL_PREFIXES) or DOCKER_NETWORK_BRIDGE.fullmatch(name) is not None

def candidates():
    try:rows=json.loads(command(['ip','-j','address','show','up']))
    except (ValueError,RuntimeError):return []
    out=[]
    for row in rows:
        name=row.get('ifname','')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or internal_interface(name):continue
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

def choose_lan(ctx,ask=input,interactive=None,output=print):
    """The home-network address and subnet (gateway.lan_ip / lan_cidr), chosen once and
    shared by every step that needs it: LAN folder sharing (Samba) and the LAN HTTPS
    gateway. Whichever runs first asks; the other uses the answer. Settings that name
    them already are kept, unless the server no longer has that address (the router gave
    it another one): then it is asked again. False when nobody is there to choose."""
    opts=ctx.config.setdefault('gateway',{})
    interactive=sys.stdin.isatty() if interactive is None else interactive
    if keep_saved(ctx.config,opts,interactive,output):return True
    if not interactive:return False
    rows=candidates()
    if not rows:raise RuntimeError('This PC has no home-network address setup can use (it is not connected to a router by cable or Wi-Fi, or only to a VPN). Connect it to your home network and run setup again; to name the address yourself, set "lan_ip" and "lan_cidr" under "gateway" in my-config.json')
    output('Which network connects this server to your home network (your router)?')
    for i,row in enumerate(rows,1):output(str(i)+'. '+row['interface']+' · '+row['ip']+' · '+row['cidr'])
    row=pick(rows,ask,output=output)
    if row is None:return False
    opts.update(lan_ip=row['ip'],lan_cidr=row['cidr'])
    save_settings(ctx)
    return True

def keep_saved(config,opts,interactive,output=print):
    """True: the saved choice is still this server's address. False: there is none yet, or
    it moved and someone can choose again. Raises when it moved and nobody can."""
    if opts.get('lan_ip') in (None,'auto') or opts.get('lan_cidr') in (None,'auto'):return False
    if still_here(config):return True
    moved=('This server\'s home-network address is no longer '+str(opts['lan_ip'])+' (gateway.lan_ip): the router gave '
           'it another one. ')
    if not interactive:raise RuntimeError(moved+'Run  sudo bash install.sh  in a terminal to choose the home network again')
    output(moved+'Choose the home network again; afterwards, reserve the new address for this server in your router.')
    return False

def pick(rows,ask,attempts=3,output=print):
    """The row whose number is typed; None after three wrong numbers or Ctrl+D."""
    for _ in range(attempts):
        try:
            choice=ask('Choose your home-network interface number: ').strip()
        except EOFError:
            return None
        if choice.isdigit() and 1<=int(choice)<=len(rows):return rows[int(choice)-1]
        output('Please type one of the numbers shown.')
    return None

def still_here(config):
    try:
        verify_lan(config)
    except RuntimeError:
        return False
    return True

def verify_lan(config):
    ip,net=lan(config)
    if not any(r['ip']==ip and ipaddress.ip_network(net).subnet_of(ipaddress.ip_network(r['cidr'])) for r in candidates()):raise RuntimeError('Configured LAN address/subnet is not on an active physical LAN interface')

