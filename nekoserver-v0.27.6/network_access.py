"""Explicit RFC1918 interface/subnet selection; no routing or firewall mutations."""
import ipaddress,json,re,subprocess

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

def verify_lan(config):
    ip,net=lan(config)
    if not any(r['ip']==ip and ipaddress.ip_network(net).subnet_of(ipaddress.ip_network(r['cidr'])) for r in candidates()):raise RuntimeError('Configured LAN address/subnet is not on an active physical LAN interface')

