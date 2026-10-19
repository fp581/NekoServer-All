"""Preserve an immutable installer bundle and current config on the data SSD."""
import hashlib
import os
from pathlib import Path
from neko_common import VERSION, safe_path, atomic_write, atomic_json

DEPENDENCIES=['storage','capacity']
DESCRIPTION='Preserve installer, templates and current configuration on the application SSD'


def preserve(data,project,config):
    data=safe_path(data);project=safe_path(project)
    if not os.path.ismount(data):raise RuntimeError('Mounted data SSD required for installer preservation')
    dest=safe_path(data/'installer'/'releases'/('v'+VERSION))
    files=list(project.glob('*.py'))+list(project.glob('*.md'))+[project/'install.sh']
    for directory,pattern in [('modules','*.py'),('compose','*.yaml')]:files.extend((project/directory).glob(pattern))
    for suffix in ('*.html','*.css','*.js','*.svg'):files.extend((project/'dashboard').glob(suffix))
    files.extend([project/'runtime/build.py',project/'runtime/Cargo.toml',project/'runtime/Cargo.lock',project/'runtime/build.json',project/'runtime/bin/nekoserver-runtime'])
    files.extend((project/'runtime/src').glob('*.rs'))
    files.append(project/'runtime/THIRD-PARTY-NOTICES.md')
    files.extend(p for p in (project/'runtime/licenses').rglob('*') if p.is_file())
    payloads={str(file.relative_to(project)):safe_path(file).read_bytes() for file in files}
    # Inspect all installed files before changing any of them.
    for rel,payload in payloads.items():
        target=safe_path(dest/rel)
        if target.exists() and target.read_bytes()!=payload:
            raise RuntimeError(f'Installed release differs; refusing overwrite: {rel}')
    for rel,payload in payloads.items():
        target=safe_path(dest/rel)
        if not target.exists():atomic_write(target,payload,0o750 if target.suffix in ('.py','.sh') or target.name=='nekoserver-runtime' else 0o640)
    atomic_json(dest/'release-manifest.json',{'version':VERSION,'sha256':{rel:hashlib.sha256(payload).hexdigest() for rel,payload in payloads.items()}})
    atomic_json(dest/'config.json',config)
    return dest


def run(ctx):
    if not ctx.apply:
        ctx.log('Installer preservation available with --apply');return
    dest=preserve(Path(ctx.config['data_mount']),Path(__file__).resolve().parents[1],ctx.config)
    ctx.log(f'Installer preserved: {dest}/install.sh; config: {dest}/config.json')
    # Future modules and retries use the persistent configuration copy.
    ctx.config_path=dest/'config.json'
