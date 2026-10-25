"""Preserve an immutable installer bundle and current config on the data SSD."""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from neko_common import VERSION, safe_path, atomic_write, atomic_json

DEPENDENCIES=['storage','capacity']
DESCRIPTION='Preserve installer, templates and current configuration on the application SSD'


def bundle_files(project):
    """Every file a reinstall from the preserved bundle needs (the tests are left out)."""
    files=list(project.glob('*.py'))+list(project.glob('*.md'))+[project/'install.sh',project/'uninstall.sh']
    for directory,pattern in [('modules','*.py'),('compose','*.yaml')]:files.extend((project/directory).glob(pattern))
    # The whole dashboard folder: its page, icons and app manifest are all installed.
    files.extend(p for p in (project/'dashboard').iterdir() if p.is_file())
    files.extend(p for p in (project/'setup-gui').iterdir() if p.is_file())   # the setup window's page
    files.extend([project/'runtime/build.py',project/'runtime/Cargo.toml',project/'runtime/Cargo.lock',project/'runtime/build.json',project/'runtime/bin/nekoserver-runtime'])
    files.extend((project/'runtime/src').glob('*.rs'))
    files.append(project/'runtime/THIRD-PARTY-NOTICES.md')
    files.extend(p for p in (project/'runtime/licenses').rglob('*') if p.is_file())
    return files


MANIFEST='release-manifest.json'   # in the kept copy: the checksum of every file as NekoServer wrote it
SET_ASIDE='.replaced-'             # v<VERSION>.replaced-<time>: the kept copy another build of the version replaced

def own_untouched(dest):
    """True when every file of the kept copy is what its manifest recorded: NekoServer
    wrote it and nothing changed it since."""
    try:recorded=json.loads((dest/MANIFEST).read_text()).get('sha256')
    except (OSError,ValueError,AttributeError):return False
    if not isinstance(recorded,dict) or not recorded:return False
    return all((dest/rel).is_file() and hashlib.sha256((dest/rel).read_bytes()).hexdigest()==digest for rel,digest in recorded.items())

def set_aside(dest,log):
    """The kept copy moved to v<VERSION>.replaced-<time> (an earlier one set aside for this
    version goes: one is enough to look at). Returns the new place."""
    for earlier in dest.parent.glob(dest.name+SET_ASIDE+'*'):shutil.rmtree(safe_path(earlier))
    aside=dest.with_name(dest.name+SET_ASIDE+time.strftime('%Y%m%d-%H%M%S'))
    dest.rename(aside)
    log(f'The copy of NekoServer {VERSION} kept on the SSD was from other sources than this one (the same version, built or '
        f'downloaded again): it is set aside at {aside} and this one takes its place')
    return aside

def preserve(data,project,config,log=print):
    """This release's files and the settings, kept on the SSD (installer/releases/v<VERSION>)
    for recovery. A kept copy from other sources of the same version is set aside when it
    is NekoServer's own and untouched (as after `git pull` into the same folder within a
    version); one someone changed is never overwritten, and setup says so."""
    data=safe_path(data);project=safe_path(project)
    if not os.path.ismount(data):raise RuntimeError('Mounted data SSD required for installer preservation')
    dest=safe_path(data/'installer'/'releases'/('v'+VERSION))
    if not (project/'runtime/bin/nekoserver-runtime').is_file() or not (project/'runtime/build.json').is_file():
        raise RuntimeError('The runtime is not built yet (runtime/bin, runtime/build.json): run  sudo bash install.sh, which builds it, then setup again')
    files=bundle_files(project)
    payloads={str(file.relative_to(project)):safe_path(file).read_bytes() for file in files}
    # Inspect all installed files before changing any of them.
    differing=[rel for rel,payload in payloads.items() if safe_path(dest/rel).exists() and (dest/rel).read_bytes()!=payload]
    if differing:
        if not own_untouched(dest):
            raise RuntimeError(f'The copy of NekoServer {VERSION} kept on the SSD differs from this one ({differing[0]}) and is not '
                               f'as NekoServer wrote it (changed since, or without its manifest): if this one is the newer download, '
                               f'move the old copy away (sudo mv {dest} {dest}.old) and run setup again; a copy you changed yourself is kept')
        set_aside(dest,log)
    for rel,payload in payloads.items():
        target=safe_path(dest/rel)
        if not target.exists():atomic_write(target,payload,0o750 if target.suffix in ('.py','.sh') or target.name=='nekoserver-runtime' else 0o640)
    atomic_json(dest/MANIFEST,{'version':VERSION,'sha256':{rel:hashlib.sha256(payload).hexdigest() for rel,payload in payloads.items()}})
    keep_previous(dest/'config.json',config,log)
    atomic_json(dest/'config.json',config)
    return dest


PREVIOUS_KEPT=5


def keep_previous(saved,config,log=print,now=None):
    """Settings different from the copy already kept keep that copy beside it, as
    config.previous-<time>.json (the newest five). After Linux is reinstalled, setup makes
    new settings before it can see the SSD, then copies them here, maybe more than once:
    the earlier ones, the ones recovery needs, stay."""
    try:
        earlier=json.loads(safe_path(saved).read_text())
    except (OSError,ValueError):
        return
    if earlier==config:return
    stamp=time.strftime('%Y%m%d-%H%M%S',time.localtime(now))
    previous=saved.with_name('config.previous-'+stamp+'.json')
    atomic_json(previous,earlier)
    for old in sorted(saved.parent.glob('config.previous-*.json'))[:-PREVIOUS_KEPT]:
        old.unlink()
    log(f'The settings kept on the SSD were different; the earlier ones are kept at {previous}')


def run(ctx):
    if not ctx.apply:
        ctx.log('Installer preservation available with --apply');return
    dest=preserve(Path(ctx.config['data_mount']),Path(__file__).resolve().parents[1],ctx.config,ctx.log)
    ctx.log(f'Installer preserved: {dest}/install.sh; config: {dest}/config.json')
    # Future modules and retries use the persistent configuration copy.
    ctx.config_path=dest/'config.json'
