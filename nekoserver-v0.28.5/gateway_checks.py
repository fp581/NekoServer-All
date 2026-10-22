"""Read-only gateway transport/configuration checks; client acceptance is separate."""
import ssl,urllib.request,urllib.error
from pathlib import Path
import gateway_tools as g
import status_checks as health
from neko_common import safe_path, local_opener

def check(config,kind):
    def row(key,status,detail):return {'id':'gateway:'+kind+':'+key,'name':kind+' gateway '+key,'status':status,'detail':detail}
    if kind=='tailnet':
        try:
            node=g.tailnode();expected=g.tailnet_routes(config,node);saved=health.read_json(Path(config['data_mount'])/'gateway/tailnet-owned.json');valid=saved.get('format')=='neko-tailnet-owned-v1' and saved.get('host')==node and saved.get('status')=='configured' and saved.get('owned')==expected and g.serve_state()==expected
            return [row('routes','pass' if valid else 'fail','Private owned HTTPS routes must match selected apps; Funnel is forbidden.')]
        except (OSError,RuntimeError,ValueError,KeyError,TypeError):return [row('routes','needs_setup','Inspect Tailscale login, HTTPS permission and private route ownership locally.')]
    out=[]
    try:
        g.verify_lan(config);saved=health.read_json(Path(config['data_mount'])/'gateway/lan-managed.json');expected=g.lan_model(config,Path('/usr/local/share/nekoserver-gateway/lan'));actual=health.read_json('/etc/nekoserver-gateway/lan.json');valid=actual==expected and bool(saved.get('sha256'))
        for name,digest in saved.get('sha256',{}).items():
            if g.hashes({safe_path(name):safe_path(name).read_bytes()})[name]!=digest:valid=False
        out.append(row('configuration','pass' if valid else 'fail','Managed LAN configuration and files must match the selected address, subnet and apps.'))
        ca=safe_path(Path(config['data_mount'])/'gateway/caddy-state/caddy/pki/authorities/local/root.crt');context=ssl.create_default_context(cafile=str(ca));opener=local_opener(urllib.request.HTTPSHandler(context=context));host=g.lan(config)[0]
        for app in ['portal',*g.selected(config)]:
            port=8442 if app=='portal' else g.APPS[app][2];code=None
            try:
                with opener.open('https://'+host+':'+str(port)+'/',timeout=5) as response:code=response.status;response.read(1024)
            except urllib.error.HTTPError as exc:code=exc.code;exc.close()
            except OSError:pass
            out.append(row(app,'pass' if code in (200,201,204,301,302,303,307,308,401,403) else 'fail','Trusted HTTPS transport must reach this listener; login/playback/upload still require client tests.'))
    except (OSError,RuntimeError,ValueError,KeyError,TypeError):out.append(row('setup','needs_setup','Verify LAN configuration, service and its private CA before testing client access.'))
    return out
