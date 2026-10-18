"""Install Tailscale without storing credentials or auto-publishing services."""
import os,shutil,subprocess,json
from pathlib import Path
DEPENDENCIES=['system']
DESCRIPTION='Install and enable Tailscale on Arch/Nyarch; interactive login remains explicit'

def detect_arch():
    values={}
    p=Path('/etc/os-release')
    for line in p.read_text().splitlines() if p.exists() else ():
        if '=' in line:
            key,value=line.split('=',1);values[key]=value.strip().strip('"').lower()
    return values.get('ID') in ('arch','nyarch') or 'arch' in values.get('ID_LIKE','').split()

def run_cmd(args,timeout=240):
    p=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError(f"{args[0]} failed: {p.stderr[-500:]}")
    return p.stdout.strip()

def run(context):
    if not context.apply:
        context.log('Tailscale install deferred; run --module tailscale --apply')
        return
    if os.geteuid()!=0:raise RuntimeError('Root required for Tailscale installation')
    if not detect_arch():raise RuntimeError('Tailscale auto-install currently supports Arch/Nyarch only; Bazzite requires separate backend')
    if not shutil.which('tailscale'):
        if not shutil.which('pacman'):raise RuntimeError('pacman required')
        run_cmd(['pacman','-S','--needed','--noconfirm','tailscale'],timeout=900)
    run_cmd(['systemctl','enable','--now','tailscaled'],timeout=45)
    status=subprocess.run(['tailscale','status','--json'],text=True,capture_output=True,timeout=20)
    authenticated= status.returncode==0 and json.loads(status.stdout).get("BackendState")=="Running" if status.stdout.strip() else False
    if not authenticated:
        context.log('Tailscale installed; complete sign-in manually using: sudo tailscale up')
        return {'status':'needs_setup','reason':'Tailscale sign-in and tailnet access policy still need completion'}
    else:context.log('Tailscale appears active; check tailnet policy and reachable peers')
