"""Independent home-network HTTPS access to selected applications."""

import gateway_tools

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Install LAN HTTPS gateway for selected apps; no Tailscale needed'


def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:
        ctx.log('LAN gateway: private HTTPS after LAN selection and secured app-account confirmation')
        return
    if not gateway_tools.configure(ctx, 'lan'):
        return {
            'status': 'needs_setup',
            'reason': 'Choose gateway.lan_ip/lan_cidr and confirm secured gateway.confirmed_services on the actual server',
        }
    gateway_tools.choose_lan_ports(ctx)
    result = gateway_tools.apply_lan(ctx.config)
    ctx.log('LAN gateway: ' + result['portal'])
    for note in result.pop('notes', []):
        ctx.log(note)
    # The home addresses use this exact IP; a router that hands out another one after a
    # restart breaks them (the Tailscale addresses are not affected).
    ctx.log(
        'Tip: in your router, reserve '
        + ctx.config['gateway']['lan_ip']
        + ' for this server ("DHCP reservation" or "static lease") so the home addresses keep working after a restart.'
    )
    return result
