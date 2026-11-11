"""Explicit RFC1918 interface/subnet selection; no routing or firewall mutations."""

import ipaddress, json, re, subprocess


def network(*octets, bits):
    """A network from its first address's four bytes and its prefix length."""
    return ipaddress.IPv4Network((bytes(octets), bits))


# The home networks (RFC 1918: 10/8, 172.16/12, 192.168/16) a gateway address may be on,
# and Tailscale's own range (100.64/10).
PRIVATE_NETWORKS = (network(10, 0, 0, 0, bits=8), network(172, 16, 0, 0, bits=12), network(192, 168, 0, 0, bits=16))
TAILNET = str(network(100, 64, 0, 0, bits=10))


def command(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("LAN interface inspection unavailable") from None
    if p.returncode:
        raise RuntimeError("LAN interface inspection failed")
    return p.stdout


def private_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise RuntimeError('A specific RFC1918 LAN IPv4 address is required') from None
    if not any(ip in n for n in PRIVATE_NETWORKS):
        raise RuntimeError('Gateway LAN address must be RFC1918 IPv4')
    return str(ip)


def lan(config):
    opts = config.get('gateway', {})
    ip = private_ip(opts.get('lan_ip'))
    try:
        net = ipaddress.IPv4Network(opts.get('lan_cidr'), strict=True)
    except (ValueError, TypeError):
        raise RuntimeError('Explicit canonical LAN subnet required') from None
    if (
        not 16 <= net.prefixlen <= 30
        or ipaddress.ip_address(ip) not in net
        or ipaddress.ip_address(ip) in (net.network_address, net.broadcast_address)
        or not any(net.subnet_of(n) for n in PRIVATE_NETWORKS)
    ):
        raise RuntimeError('LAN subnet must be a restricted RFC1918 network containing the server')
    return ip, str(net)


# Interfaces that are never the home network: the loopback, Docker's (docker0, and
# br-<12 hex> for each Docker network), Podman's, libvirt's virtual networks, container
# veths, Tailscale and VPN tunnels. A bridge of your own that carries the home network
# (br0, bridge0, br-lan) is not one of them. (startup.rs internal_interface is the same.)
# neko-rt-: an isolated restore test's own network (recovery_tools.test_bridge).
INTERNAL_PREFIXES = ('lo', 'docker', 'veth', 'virbr', 'podman', 'cni-', 'tailscale', 'tun', 'tap', 'wg', 'neko-rt-')
DOCKER_NETWORK_BRIDGE = re.compile(r'br-[0-9a-f]{12}')


def internal_interface(name):
    return name.startswith(INTERNAL_PREFIXES) or DOCKER_NETWORK_BRIDGE.fullmatch(name) is not None


def candidates():
    try:
        rows = json.loads(command(['ip', '-j', 'address', 'show', 'up']))
    except (ValueError, RuntimeError):
        return []
    out = []
    for row in rows:
        name = row.get('ifname', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or internal_interface(name):
            continue
        for info in row.get('addr_info', []):
            if info.get('family') != 'inet' or info.get('scope') != 'global':
                continue
            try:
                ip = private_ip(info.get('local'))
                net = str(ipaddress.ip_network(ip + '/' + str(info['prefixlen']), strict=False))
                lan({'gateway': {'lan_ip': ip, 'lan_cidr': net}})
            except (RuntimeError, ValueError, KeyError):
                continue
            out.append({'interface': name, 'ip': ip, 'cidr': net})
    return out


def save_settings(ctx):
    """Keep a choice made during setup in the settings file, for the next steps and runs."""
    if hasattr(ctx, 'persist_config'):
        ctx.persist_config()
    elif getattr(ctx, 'config_path', None):
        from neko_common import atomic_json

        atomic_json(ctx.config_path, ctx.config)


def shown(row):
    """A network as listed: "enp3s0 · 192.168.1.20 · 192.168.1.0/24"."""
    return row['interface'] + ' · ' + row['ip'] + ' · ' + row['cidr']


def wireless(interface, sys_net='/sys/class/net'):
    """The connection is Wi-Fi (Linux keeps a "wireless" folder for such a one)."""
    import os

    return os.path.isdir(os.path.join(sys_net, interface, 'wireless'))


def plainly(row, is_wireless=None):
    """A connection as a person knows it: "your cable connection, 192.168.1.50" or "your
    Wi-Fi connection, 192.168.1.73"."""
    kind = 'Wi-Fi' if (is_wireless or wireless)(row['interface']) else 'cable'
    return f"your {kind} connection, {row['ip']}"


def default_route(run=command):
    """The connection this PC reaches the internet through (the default route's), or None."""
    try:
        routes = json.loads(run(['ip', '-j', 'route', 'show', 'default']))
    except (ValueError, RuntimeError):
        return None
    return next((route.get('dev') for route in routes if isinstance(route, dict) and route.get('dev')), None)


def chosen(rows, is_wireless=None, route=None):
    """The home network among several connections, without asking (the user, 2026-11-11:
    a home PC is on its home network): a cable before Wi-Fi, and among equals the one
    this PC reaches the internet through; else the first Linux lists."""
    route = default_route() if route is None else route
    is_wireless = is_wireless or wireless
    return min(rows, key=lambda row: (is_wireless(row['interface']), row['interface'] != route))


NOT_CONNECTED = (
    'This PC is not connected. Plug in a network cable, or join Wi-Fi from this PC\'s own settings, then try again.'
)


def connected(run=command):
    """This PC has a way out to the internet (a default route): checked before the first
    thing setup downloads, so an offline PC is told so plainly instead of failing there."""
    return default_route(run) is not None


def choose_lan(ctx, ask=input, interactive=None, output=print):
    """The home-network address and subnet (gateway.lan_ip / lan_cidr), chosen once and
    shared by every step that needs it: LAN folder sharing (Samba) and the LAN HTTPS
    gateway. Nothing is asked: with several connections setup takes the cable before
    Wi-Fi (`chosen`) and says which it uses. Settings that name them already are kept
    while the server still has that address, so nothing moves under a server that is set
    up; when the router gave it another one, the choice is made again and said."""
    opts = ctx.config.setdefault('gateway', {})
    if keep_saved(ctx.config, opts, output):
        return True
    rows = candidates()
    if not rows:
        raise RuntimeError(
            NOT_CONNECTED + ' (Setup found no home-network address on this PC; a VPN alone is not one. To name '
            'the address yourself, set "lan_ip" and "lan_cidr" under "gateway" in my-config.json.)'
        )
    row = chosen(rows)
    output('Home network: using ' + plainly(row))
    opts.update(lan_ip=row['ip'], lan_cidr=row['cidr'])
    save_settings(ctx)
    return True


def keep_saved(config, opts, output=print):
    """True: the saved choice is still this server's address. False: there is none yet, or
    the router gave the server another address (said, and the choice is made again)."""
    if opts.get('lan_ip') in (None, 'auto') or opts.get('lan_cidr') in (None, 'auto'):
        return False
    if still_here(config):
        return True
    output(
        'This server\'s home-network address is no longer '
        + str(opts['lan_ip'])
        + ': the router gave it another one. Reserve the new address for this server in your router, so '
        'the addresses of your apps stay the same.'
    )
    return False


def still_here(config):
    try:
        verify_lan(config)
    except RuntimeError:
        return False
    return True


def verify_lan(config):
    ip, net = lan(config)
    if not any(
        r['ip'] == ip and ipaddress.ip_network(net).subnet_of(ipaddress.ip_network(r['cidr'])) for r in candidates()
    ):
        raise RuntimeError('Configured LAN address/subnet is not on an active physical LAN interface')
