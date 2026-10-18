"""Run real installer modules against redirected files and simulated OS commands.

No mount/package/service/container action reaches the host. Native Docker startup
verification and the official standalone Compose parser do execute as processes.
"""
import copy
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from contextlib import ExitStack,redirect_stdout
from unittest.mock import patch
from types import SimpleNamespace

import installer
from modules import photos

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parents[1]
COMPOSE=BASE/'compose-review/docker-compose'
UPSTREAM=BASE/'compose-review/immich-upstream.yaml'

SHIM='''#!/usr/bin/python3
import json,os,sys,subprocess,shlex
from pathlib import Path
root=Path(os.environ['NEKO_SIM_ROOT']);state=root/'commands-state.json';v=json.loads(state.read_text());args=sys.argv[1:];name=Path(sys.argv[0]).name
with (root/'commands.log').open('a') as f:f.write(json.dumps([name,*args])+'\\n')
def save():state.write_text(json.dumps(v))
def fields(target):
 disk=next((k for k,p in v['mounts'].items() if p==target),None)
 return disk
if name=='lsblk':
 print(json.dumps({'blockdevices':[{'name':'disk-'+k,'type':'disk','children':[{'name':k,'type':'part','uuid':k,'fstype':'exfat' if k=='USB-0001' else 'ext4','size':'512G' if k=='USB-0001' else '1T' if k=='SSD-0001' else '12T','mountpoint':v['mounts'].get(k)}]} for k in ('SSD-0001','HDD-0001','USB-0001')]}))
elif name=='findmnt':
 if '--mountpoint' in args:
  target=args[-1];disk=fields(target)
  if not disk:sys.exit(1)
  field=args[args.index('-o')+1] if '-o' in args else ''
  if field=='UUID,FSTYPE':print(('WRONG-UUID' if v.get('wrong') else disk)+' '+('exfat' if disk=='USB-0001' else 'ext4'))
  elif field=='TARGET':print(target)
  elif field=='SOURCE':print(root/'dev'/disk)
  else:print(target)
 else:
  target=args[-1];disk=fields(target)
  if '-o' in args and args[args.index('-o')+1]=='TARGET':print(target if disk else '/')
  else:print(root/'dev'/disk if disk else '/dev/os-fixture')
elif name=='blkid':print('ext4')
elif name=='mount':
 target=args[-1];disk='SSD-0001' if target==v['data'] else 'HDD-0001'
 v['mounts'][disk]=target;save()
elif name=='systemd-escape':
 target=args[-1].strip('/').replace('-','\\\\x2d').replace('/','-');print(target+'.mount')
elif name=='pacman':v['installed']=True;save()
elif name=='docker':
 if args[:2]==['--host','unix:///var/run/docker.sock']:args=args[2:]
 if args[0]=='info':
  if not v['running']:sys.exit(1)
  if args[-1]=='{{.DockerRootDir}}':print(v['data']+'/docker-engine')
  elif args[-1]=='{{.ServerVersion}}':print('28.0.0')
  elif args[-1]=='{{json .DriverStatus}}':print('[]')
  else:print(json.dumps({'ServerVersion':'28.0.0','DockerRootDir':v['data']+'/docker-engine','Driver':'overlay2','DriverStatus':[]}))
 elif args[0]=='compose':
  if args[1:] == ['version']:sys.exit(0)
  if 'config' in args:
   p=subprocess.run([os.environ['NEKO_REAL_COMPOSE'],*args[1:]],capture_output=True);sys.exit(p.returncode)
  if 'up' in args:v.setdefault('projects',[]).append(args[args.index('-p')+1]);save()
  else:sys.exit(1)
 elif args[:2]==['network','inspect']:
  if not v.get('network'):sys.exit(1)
 elif args[:2]==['network','create']:v['network']=True;save()
 elif args[0]=='ps':print('')
 else:sys.exit(1)
elif name=='systemctl':
 if args[0]=='is-active':sys.exit(0 if v['running'] else 3)
 if args[0]=='show':print('LoadState=not-found\\nActiveState=inactive')
 if args[0]=='mask':
  folder=root/'os/etc/systemd/system';folder.mkdir(parents=True,exist_ok=True)
  for name in args[1:]:
   p=folder/name
   if not p.is_symlink():p.symlink_to('/dev/null')
 if args[:2]==['enable','--now'] and args[-1]=='docker':
  path=root/'os/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf'
  command=next(s.split('=',1)[1] for s in path.read_text().splitlines() if s.startswith('ExecStartPre='))
  p=subprocess.run(shlex.split(command),capture_output=True)
  if p.returncode:sys.exit(p.returncode)
  v['running']=True;save()
elif name in ('smartctl','lspci'):pass
else:sys.exit(1)
'''


def run(root):
    root=Path(root);osroot=root/'os';commands=root/'bin';commands.mkdir()
    state=root/'commands-state.json'
    data=root/'ssd';media=root/'hdd';external=root/'usb'
    cfg=copy.deepcopy(json.loads((ROOT/'config.json').read_text()))
    cfg.update(data_mount=str(data),media_mount=str(media),external_backup_mount=str(external))
    cfg['docker']['persistent_root']=str(data);cfg['storage']={'data_ssd':{},'media_hdd':{}}
    cfg['server_os']['hdd_policy']='unchanged';cfg['photos']['external_backup_uuid']=''
    cfg['profiles']['audit_install']=['system','storage','recovery','server_os','docker','recovery_checkpoint','media','photos','photo_preservation','photo_backup','media_monitor','dashboard']
    config=root/'config.json';config.write_text(json.dumps(cfg))
    state.write_text(json.dumps({'data':str(data),'media':str(media),'mounts':{},'running':False,'installed':False}))
    for name in ('lsblk','findmnt','blkid','mount','systemd-escape','systemctl','pacman','docker','smartctl','lspci'):
        p=commands/name;p.write_text(SHIM);p.chmod(0o755)
    devices=root/'dev';devices.mkdir();links=devices/'disk/by-uuid';links.mkdir(parents=True)
    for uuid in ('SSD-0001','HDD-0001'):
        p=devices/uuid;p.touch();(links/uuid).symlink_to(p)
    fstab=osroot/'etc/fstab';fstab.parent.mkdir(parents=True);fstab.write_text('# fixture existing fstab\n')
    names=installer.load_modules(ROOT/'modules')
    modules={name:importlib.import_module('modules.'+name) for name in names}
    real_path=Path
    def mapped(value,*args):
        p=real_path(value,*args)
        if str(p).startswith(('/etc/','/usr/local/','/var/lib/docker')):return osroot/str(p).lstrip('/')
        if str(p).startswith('/dev/disk/by-uuid'):return devices/str(p).removeprefix('/dev/')
        return p
    def source(uuid):return devices/uuid
    real_which=__import__('shutil').which
    def which(name):
        if name=='docker' and not json.loads(state.read_text())['installed']:return None
        return real_which(name)
    args=SimpleNamespace(config=str(config),state_dir=str(root/'state'),profile='audit_install',module=None,retry_failed=False,check=False,dry_run=False,apply=True)
    calls=[]
    version=json.loads((BASE/'compose-review/immich-source.json').read_text())['version']
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{'PATH':str(commands)+':'+os.environ['PATH'],'NEKO_SIM_ROOT':str(root),'NEKO_REAL_COMPOSE':str(COMPOSE)}))
        # Imported os/shutil modules are shared by installer modules. Mounts and
        # non-root ownership are modelled, not actual kernel mounting/chown.
        stack.enter_context(patch.object(os.path,'ismount',side_effect=lambda p:str(p) in json.loads(state.read_text())['mounts'].values()))
        stack.enter_context(patch.object(os,'chown',side_effect=lambda p,u,g:calls.append((str(p),u,g))))
        stack.enter_context(patch.object(__import__('shutil'),'which',side_effect=which))
        for name,mod in modules.items():
            if hasattr(mod,'Path'):stack.enter_context(patch.object(mod,'Path',side_effect=mapped))
        stack.enter_context(patch.object(modules['storage'],'source_path',side_effect=source))
        install_fstab=modules['storage'].install_fstab
        stack.enter_context(patch.object(modules['storage'],'install_fstab',side_effect=lambda specs:install_fstab(specs,fstab)))
        stack.enter_context(patch.object(photos,'resolve_immich_version',return_value=version))
        stack.enter_context(patch.object(photos.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(UPSTREAM.read_bytes())))
        stack.enter_context(patch.object(installer,'load_modules',return_value={k:{'object':m,'deps':names[k]['deps']} for k,m in modules.items()}))
        stdin=io.StringIO('1\nSSD-0001\n1\nHDD-0001\n1\nUSB-0001\n');stdin.isatty=lambda:True
        log=io.StringIO()
        with patch.object(sys,'stdin',stdin),redirect_stdout(log):first=installer.execute(args)
        logtext=log.getvalue()
        if first:return {'first':first,'log':logtext,'state':json.loads((root/'state/state.json').read_text())}
        with patch.object(sys,'stdin',io.StringIO('')),redirect_stdout(io.StringIO()):second=installer.execute(args)
        original=json.loads(config.read_text())
        release=json.loads((data/'installer/releases/v0.21.0/config.json').read_text())
        manifest=data/'compose/sonarr/compose.yaml';manifest.write_text(manifest.read_text()+'\n# user customization\n');before=manifest.read_bytes()
        with patch.object(sys,'stdin',io.StringIO('')),redirect_stdout(io.StringIO()):custom=installer.execute(args)
        private=json.loads(state.read_text());private['wrong']=True;state.write_text(json.dumps(private))
        guard=data/'installer/guards/v0.21.0/nekoserver-runtime'
        failed=subprocess.run([str(guard),'storage-guard','--config',str(guard.parent/'storage.json')],capture_output=True)
        return {'first':first,'rerun':second,'custom_manifest_run':custom,'custom_manifest_preserved':manifest.read_bytes()==before,'wrong_uuid_guard':failed.returncode,'source_usb_uuid':original['photos']['external_backup_uuid'],'saved_usb_uuid':release['photos']['external_backup_uuid'],'root':str(root),'ownership_calls_simulated':len(calls),'log':logtext,'scope':'Real modules/files/native guard/official Compose parser; mount/package/systemd/Docker/ownership actions simulated'}
