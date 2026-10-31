"""Preserve an immutable installer bundle and current config on the data SSD."""

import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from neko_common import VERSION, safe_path, atomic_write, atomic_json, plain_folder

DEPENDENCIES = ['storage', 'capacity']
DESCRIPTION = 'Preserve installer, templates and current configuration on the application SSD'


def bundle_files(project):
    """Every file a reinstall from the preserved bundle needs (the tests are left out)."""
    files = list(project.glob('*.py')) + list(project.glob('*.md')) + [project / 'install.sh', project / 'uninstall.sh']
    for directory, pattern in [('modules', '*.py'), ('compose', '*.yaml')]:
        files.extend((project / directory).glob(pattern))
    # The whole dashboard folder: its page, icons and app manifest are all installed.
    files.extend(p for p in (project / 'dashboard').iterdir() if p.is_file())
    files.extend(p for p in (project / 'setup-gui').iterdir() if p.is_file())  # the setup window's page
    files.extend(
        [
            project / 'runtime/build.py',
            project / 'runtime/Cargo.toml',
            project / 'runtime/Cargo.lock',
            project / 'runtime/build.json',
            project / 'runtime/bin/nekoserver-runtime',
        ]
    )
    files.extend((project / 'runtime/src').glob('*.rs'))
    files.append(project / 'runtime/THIRD-PARTY-NOTICES.md')
    files.extend(p for p in (project / 'runtime/licenses').rglob('*') if p.is_file())
    return files


MANIFEST = 'release-manifest.json'  # in the kept copy: the checksum of every file as NekoServer wrote it
SET_ASIDE = '.replaced-'  # v<VERSION>.replaced-<time>: the kept copy another build of the version replaced
SETTINGS_FILE = 'config.json'  # in the kept copy: the settings this installation runs with


def own_untouched(dest):
    """True when every file of the kept copy is what its manifest recorded: NekoServer
    wrote it and nothing changed it since."""
    try:
        recorded = json.loads((dest / MANIFEST).read_text()).get('sha256')
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(recorded, dict) or not recorded:
        return False
    return all(
        (dest / rel).is_file() and hashlib.sha256((dest / rel).read_bytes()).hexdigest() == digest
        for rel, digest in recorded.items()
    )


def set_aside(dest, log):
    """The kept copy moved to v<VERSION>.replaced-<time> (an earlier one set aside for this
    version goes: one is enough to look at, and the settings it kept are carried over
    first, so none is lost). Returns the new place."""
    for earlier in dest.parent.glob(dest.name + SET_ASIDE + '*'):
        carry_settings(earlier, dest)
        shutil.rmtree(safe_path(earlier))
    aside = dest.with_name(dest.name + SET_ASIDE + time.strftime('%Y%m%d-%H%M%S'))
    dest.rename(aside)
    # The settings it kept (the ones from before a reinstall, say) stay where recovery and
    # RECOVERY.md look for them: in the copy that takes its place.
    carry_settings(aside, dest)
    log(
        f'The copy of NekoServer {VERSION} kept on the SSD was from other sources than this one (the same version, built or '
        f'downloaded again): it is set aside at {aside} and this one takes its place'
    )
    return aside


def settings_files(folder):
    """The settings a release folder keeps: config.json and its earlier copies."""
    folder = Path(folder)
    return (
        sorted(p for p in folder.glob('config*.json') if p.is_file() and not p.is_symlink()) if folder.is_dir() else []
    )


def free_name(folder, stem):
    """folder/<stem>.json, or <stem>-2.json, -3, ... when that name is taken."""
    target, count = folder / (stem + '.json'), 1
    while target.exists():
        count += 1
        target = folder / f'{stem}-{count}.json'
    return target


def carry_settings(source, dest):
    """The settings kept in `source` that `dest` does not hold yet (by content) are copied
    into dest, with their dates, so the history follows a kept copy that is set aside: its
    config.json as dest's config.json when dest has none yet, otherwise as an earlier copy
    (config.previous-...). Returns the copies made."""
    dest = plain_folder(dest) if Path(dest).exists() else Path(dest)
    held = {p.read_bytes() for p in settings_files(dest)}
    made = []
    for path in settings_files(plain_folder(source)):
        payload = path.read_bytes()
        if payload in held:
            continue
        target = dest / path.name
        if target.exists():
            target = free_name(
                dest, 'config.previous-' + time.strftime('%Y%m%d-%H%M%S', time.localtime(path.stat().st_mtime))
            )
        dest.mkdir(parents=True, exist_ok=True)
        atomic_write(target, payload, 0o600)
        os.utime(target, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        held.add(payload)
        made.append(target)
    return made


def preserve(data, project, config, log=print):
    """This release's files and the settings, kept on the SSD (installer/releases/v<VERSION>)
    for recovery. A kept copy from other sources of the same version is set aside when it
    is NekoServer's own and untouched (as after `git pull` into the same folder within a
    version); one someone changed is never overwritten, and setup says so."""
    data = safe_path(data)
    project = safe_path(project)
    if not os.path.ismount(data):
        raise RuntimeError('Mounted data SSD required for installer preservation')
    dest = safe_path(data / 'installer' / 'releases' / ('v' + VERSION))
    if not (project / 'runtime/bin/nekoserver-runtime').is_file() or not (project / 'runtime/build.json').is_file():
        raise RuntimeError(
            'The runtime is not built yet (runtime/bin, runtime/build.json): run  sudo bash install.sh, which builds it, then setup again'
        )
    files = bundle_files(project)
    payloads = {str(file.relative_to(project)): safe_path(file).read_bytes() for file in files}
    # Inspect all installed files before changing any of them.
    differing = [
        rel
        for rel, payload in payloads.items()
        if safe_path(dest / rel).exists() and (dest / rel).read_bytes() != payload
    ]
    if differing:
        if not own_untouched(dest):
            raise RuntimeError(
                f'The copy of NekoServer {VERSION} kept on the SSD differs from this one ({differing[0]}) and is not '
                f'as NekoServer wrote it (changed since, or without its manifest): if this one is the newer download, '
                f'move the old copy away (sudo mv {dest} {dest}.old) and run setup again; a copy you changed yourself is kept'
            )
        set_aside(dest, log)
    for rel, payload in payloads.items():
        target = safe_path(dest / rel)
        if not target.exists():
            atomic_write(
                target,
                payload,
                0o750 if target.suffix in ('.py', '.sh') or target.name == 'nekoserver-runtime' else 0o640,
            )
    atomic_json(
        dest / MANIFEST,
        {'version': VERSION, 'sha256': {rel: hashlib.sha256(payload).hexdigest() for rel, payload in payloads.items()}},
    )
    keep_previous(dest / SETTINGS_FILE, config, log)
    atomic_json(dest / SETTINGS_FILE, config)
    return dest


PREVIOUS_KEPT = 5


def keep_previous(saved, config, log=print, now=None):
    """Settings different from the copy already kept keep that copy beside it, as
    config.previous-<time>.json: the oldest one always (after Linux is reinstalled, setup
    makes new settings before it can see the SSD and copies them here, maybe on every
    run since: the oldest is the one from before, which recovery needs) and the newest
    four after it."""
    # The release folder on the SSD, checked (absolute, no links, not a system folder).
    folder = plain_folder(Path(saved).parent)
    try:
        earlier = json.loads((folder / SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return
    if earlier == config:
        return
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    previous = free_name(folder, 'config.previous-' + stamp)  # two changes in one second: each keeps its own copy
    atomic_json(previous, earlier)
    # The copy keeps the date of the settings it holds, so "the oldest" stays the oldest
    # settings, also for ones carried over from a set-aside copy.
    with contextlib.suppress(OSError):
        os.utime(previous, ns=(time.time_ns(), (folder / SETTINGS_FILE).stat().st_mtime_ns))
    kept = sorted(folder.glob('config.previous-*.json'), key=lambda p: p.stat().st_mtime_ns)
    for old in kept[1 : -(PREVIOUS_KEPT - 1)] if len(kept) > PREVIOUS_KEPT else []:
        old.unlink()
    log(f'The settings kept on the SSD were different; the earlier ones are kept at {previous}')


def run(ctx):
    if not ctx.apply:
        ctx.log('Installer preservation available with --apply')
        return
    dest = preserve(Path(ctx.config['data_mount']), Path(__file__).resolve().parents[1], ctx.config, ctx.log)
    ctx.log(f'Installer preserved: {dest}/install.sh; config: {dest}/config.json')
    # Future modules and retries use the persistent configuration copy.
    ctx.config_path = dest / SETTINGS_FILE
