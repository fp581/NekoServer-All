"""Optional away-from-home HTTPS access through private Tailscale Serve."""
import gateway_tools
DEPENDENCIES=['storage','recovery','tailscale']
DESCRIPTION='Configure optional private Tailscale HTTPS access; never enables Funnel'
def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:ctx.log('Optional private Tailscale Serve gateway after authentication and secured app-account confirmation');return
    if not gateway_tools.configure(ctx,'tailnet'):return {'status':'needs_setup','reason':'Confirm secured gateway.confirmed_services before enabling remote access'}
    result=gateway_tools.apply_tailnet(ctx.config);ctx.log('Private Tailscale application links saved in reports/gateway-tailnet.json');return result
