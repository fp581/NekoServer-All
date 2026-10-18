"""Independent home-network HTTPS access to selected applications."""
import gateway_tools
DEPENDENCIES=['storage','recovery']
DESCRIPTION='Install LAN HTTPS gateway for selected apps; no Tailscale needed'
def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:ctx.log('LAN gateway: private HTTPS after LAN selection and secured app-account confirmation');return
    if not gateway_tools.configure(ctx,'lan'):return {'status':'needs_setup','reason':'Choose gateway.lan_ip/lan_cidr and confirm secured gateway.confirmed_services on the actual server'}
    result=gateway_tools.apply_lan(ctx.config);ctx.log('LAN gateway: '+result['portal']);return result
