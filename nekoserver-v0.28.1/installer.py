#!/usr/bin/env python3
"""NekoServer modular, standard-library-only orchestration framework."""
import argparse
import fcntl
import stat
import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path
import capabilities
import host
from neko_common import atomic_json, mount_path, relative_path, VERSION, safe_path, ensure_directory, settings_path, pending_maintenance

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / '.local' / 'state' / 'nekoserver'

class InstallerError(Exception):
    pass

class Context:
    def __init__(self, config, logger, apply=False, config_path=None):
        self.config = config
        self.log = logger
        self.apply = apply
        self.config_path = Path(config_path) if config_path else None
        self.original_config_path = self.config_path

    def persist_config(self):
        for path in dict.fromkeys(p for p in (self.original_config_path,self.config_path) if p is not None):
            atomic_json(path,self.config)

def read_json(path):
    """A settings or state file (.json): absolute, no '..', no symlinks along the way."""
    try:
        checked = settings_path(path)
    except RuntimeError as exc:
        raise InstallerError(str(exc)) from None
    with checked.open(encoding='utf-8') as f:
        return json.load(f)

def load_modules(directory):
    modules = {}
    for path in sorted(directory.glob('*.py')):
        if path.name.startswith('_'):
            continue
        spec = importlib.util.spec_from_file_location(f'nekoserver_{path.stem}', path)
        obj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(obj)
        deps = list(getattr(obj, 'DEPENDENCIES', []))
        if not hasattr(obj, 'run'):
            raise InstallerError(f'Module {path.stem} has no run(context)')
        modules[path.stem] = {'object': obj, 'deps': deps}
    return modules

def ordered(modules, selected):
    result, visited = [], {}
    def visit(name):
        if name not in modules:
            raise InstallerError(f'Unknown module or dependency: {name}')
        if visited.get(name) == 'active':
            raise InstallerError(f'Dependency cycle at {name}')
        if visited.get(name) == 'done':
            return
        visited[name] = 'active'
        for dep in modules[name]['deps']:
            visit(dep)
        visited[name] = 'done'
        result.append(name)
    for name in selected:
        visit(name)
    return result

def atomic_json_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path,data)

def validate_config(config):
    if not isinstance(config,dict):raise InstallerError('Configuration must be an object')
    for group in ('runtime','modules','profiles','storage','media','photos','docker'):
        if not isinstance(config.get(group,{}),dict):raise InstallerError(group+' must be an object')
    # Saved pre-v0.18 configurations lack these switches. Apply current defaults
    # in memory so reusing confirmed disk settings cannot revive retired jobs.
    for group, defaults in (
        ('monitoring', {'enabled': False}),
        ('music', {'backup_enabled': False}),
        ('photos', {'backup_schedule': 'weekly'}),
    ):
        options = config.setdefault(group, {})
        if not isinstance(options, dict):raise InstallerError(group+' must be an object')
        for key, value in defaults.items():options.setdefault(key, value)
    for entry in config.get('modules',{}).values():
        if not isinstance(entry,dict) or type(entry.get('enabled',False)) is not bool:raise InstallerError('Module enable switches must be booleans')
    for names in config.get('profiles',{}).values():
        if not isinstance(names,list) or any(not isinstance(name,str) for name in names) or len(set(names))!=len(names):raise InstallerError('Profiles must contain unique module names')
    runtime=config.get("runtime",{})
    # "python" is accepted only so older configs still load; it now means Rust.
    if not isinstance(runtime,dict) or runtime.get("backend","rust") not in ("python","rust"):
        raise InstallerError("runtime.backend must be rust")
    data, media = mount_path(config['data_mount']), mount_path(config['media_mount'])
    if data == media or data in media.parents or media in data.parents:
        raise InstallerError('Data and media mounts must be separate and non-nested')
    backup = mount_path(config['external_backup_mount'])
    if any(backup == p or backup in p.parents or p in backup.parents for p in (data,media)):
        raise InstallerError('External backup mount must be separate from data and media')
    if config.get('docker',{}).get('persistent_root',str(data)) != str(data):
        raise InstallerError('Docker persistent_root must equal data_mount')
    for key, default in [('archive_dir','photos/archive'),('inbox_dir','photos/inbox')]:
        relative_path(config.get('photos',{}).get(key,default))
    archive = media/relative_path(config.get('photos',{}).get('archive_dir','photos/archive'))
    # Reserve the former HDD live tree too: never adopt it as an archive on upgrade.
    managed = media/'photos'/'immich-library'
    if archive == managed or archive in managed.parents or managed in archive.parents:
        raise InstallerError('Photo archive must be separate from the Immich upload tree')
    selected = config.get('media',{}).get('services',[])
    allowed = {'jellyfin','navidrome','sonarr','radarr','lidarr','prowlarr','seerr','qbittorrent'}
    if not isinstance(selected,list) or not selected or len(selected)!=len(set(selected)) or not set(selected)<=allowed:
        raise InstallerError('media.services must contain unique known services')


def _execute(args, root=ROOT):
    config = read_json(Path(args.config))
    validate_config(config)
    modules = load_modules(root / 'modules')
    enabled = [k for k in modules if config.get('modules', {}).get(k, {}).get('enabled', False)]
    state_dir = Path(args.state_dir).expanduser()
    if not state_dir.is_absolute() or '..' in state_dir.parts:
        raise InstallerError('--state-dir must be an absolute path without ".."')
    state_dir = safe_path(state_dir)
    if state_dir==DEFAULT_STATE and os.path.ismount(config['data_mount']):
        state_dir=Path(config['data_mount'])/'installer'/'state'
    state_path = safe_path(state_dir / 'state.json')
    safe_path(state_dir / 'install.log')
    previous = read_json(state_path) if state_path.exists() else {}
    if not isinstance(previous,dict) or any(not isinstance(value,dict) for value in previous.values()):
        raise InstallerError('Installer state must map module names to state objects')
    if args.retry_failed and not previous:
        raise InstallerError('No prior installer state; mount the data SSD with the foundation profile before retrying')
    if args.profile:
        if args.profile not in config.get('profiles', {}):raise InstallerError('Unknown profile: '+args.profile)
        selected = config['profiles'][args.profile]
    elif args.module:
        selected = [args.module]
    elif args.retry_failed:
        selected = [name for name in modules if previous.get(name, {}).get('status') in ('failed', 'blocked','needs_setup','running')]
    else:
        selected = enabled
    plan = ordered(modules, selected)
    from config_checks import validate
    validate(config,plan)
    if args.apply and set(plan)&{'docker','server_os','dashboard','gateway_lan','media_monitor','photo_preservation','photo_backup','app_backup','host_backup','notifications','heartbeat','maintenance'}:
        from native_runtime import payload
        payload(ROOT,config)
    if args.check or args.dry_run:
        print(f'NekoServer v{VERSION} — read-only plan (no changes):')
        for name in plan:
            print(f"  {name}: {getattr(modules[name]['object'], 'DESCRIPTION', '')}")
        if args.check:
            print('Configuration and dependency graph valid.')
        return 0
    ensure_directory(state_dir,0o700)
    if not args.apply:
        state_path=safe_path(state_dir/'inspection-state.json')
    log_path = safe_path(state_dir / ('install.log' if args.apply else 'inspection.log'))
    states = dict(previous) if args.apply else {}
    complete_status='success' if args.apply else 'inspected'
    failures = []
    unavailable = []
    def log(message):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        text = f'[{stamp}] {message}'
        print(text)
        with safe_path(log_path).open('a', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(),0o600)
            handle.write(text + '\n')
    ctx = Context(config, log, apply=args.apply, config_path=args.config)
    ctx.plan = plan
    for name in plan:
        bad_deps = [d for d in modules[name]['deps'] if states.get(d, {}).get('status') != complete_status]
        if bad_deps:
            states[name] = {'status': 'blocked', 'reason': f'Dependencies not successful: {bad_deps}'}
            log(f'BLOCKED {name}: {bad_deps}')
            failures.append(name)
            atomic_json_write(state_path, states)
            continue
        states[name]={'status':'running','version':VERSION,'started_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json_write(state_path,states)
        log(f'START {name}')
        try:
            result=modules[name]['object'].run(ctx)
        except Exception as exc:
            states[name] = {'status': 'failed', 'reason': str(exc)}
            log(f'FAILED {name}: {exc}')
            # Avoid tracebacks from providers that may contain sensitive arguments.
            failures.append(name)
        else:
            if isinstance(result,dict) and result.get('status')=='needs_setup':
                states[name]={'status':'needs_setup','reason':result.get('reason','Setup incomplete')}
                failures.append(name);log(f'NEEDS SETUP {name}')
            elif isinstance(result,dict) and result.get('status')==UNAVAILABLE:
                # An optional extra this Linux can't have yet: neither done nor waiting.
                states[name]={'status':UNAVAILABLE,'reason':result.get('reason','Not available on this system')}
                unavailable.append(name);log(f'UNAVAILABLE {name}: {states[name]["reason"]}')
            else:
                states[name] = {'status': complete_status, 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
                log(f'PASS {name}')
        if name=='storage' and states[name]['status']=='success' and args.apply and Path(args.state_dir).expanduser()==DEFAULT_STATE:
            persistent=Path(config['data_mount'])/'installer'/'state'
            ensure_directory(persistent,0o700)
            next_state=persistent/'state.json'
            previous_persistent=read_json(next_state) if next_state.exists() else {}
            states={**previous_persistent,**states}
            old_log=log_path
            state_dir=persistent;state_path=next_state;log_path=persistent/'install.log'
            if old_log!=log_path and old_log.exists():
                with safe_path(log_path).open('a') as stream:
                    os.fchmod(stream.fileno(),0o600)
                    stream.write(old_log.read_text())
            os.chmod(log_path,0o600)
        atomic_json_write(state_path, states)
    for name in REFRESHED:
        if args.apply and states.get(name, {}).get('status') == complete_status and name in modules:
            # Later steps (or a newer release) can change what these keep: written again
            # from the final configuration, even when only another step ran.
            try:
                modules[name]['object'].refresh(config, log)
            except (OSError, RuntimeError, ValueError) as exc:
                log(f'{name}: settings not refreshed: {exc}')
    log(f'Finished: {len(plan) - len(failures) - len(unavailable)} passed; '
        + (f'{len(unavailable)} not available on this system; ' if unavailable else '')
        + f'{len(failures)} failed/blocked/incomplete; log={log_path}')
    return 1 if failures else 0

# What an optional step returns when this Linux can't have it (yet).
UNAVAILABLE = 'unavailable'

# Steps whose saved settings are brought up to date after every run.
REFRESHED = ('dashboard', 'host_backup')

def execute(args, root=ROOT):
    if not args.apply:
        return _execute(args, root)
    if os.geteuid()!=0:
        raise InstallerError('--apply requires root')
    blocked=pending_maintenance()
    if blocked:
        raise InstallerError(blocked)
    found=capabilities.detect()
    capabilities.report(found)
    capabilities.require(found)
    # Packages an earlier installation left half-done are tidied before any step runs.
    host.current().reconcile()
    path=safe_path('/run/nekoserver-install.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        metadata=os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode&0o077:
            raise InstallerError('Installer lock must be a private root-owned regular file')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise InstallerError('Another NekoServer installation is active') from None
        return _execute(args, root)
    finally:
        os.close(fd)

def main(argv=None):
    parser = argparse.ArgumentParser(description='NekoServer safe phase-one installer framework')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true', help='Run all enabled modules (default)')
    mode.add_argument('--profile', metavar='NAME', help='Run named module profile')
    mode.add_argument('--module', metavar='NAME', help='Run a module and its dependencies')
    mode.add_argument('--retry-failed', action='store_true', help='Retry failed or blocked modules and their dependencies')
    mode.add_argument('--wizard', action='store_true', help='Guided setup of everything (what `sudo bash install.sh` runs)')
    mode.add_argument('--recover', action='store_true', help='Guided recovery from your backups')
    parser.add_argument('--apply', action='store_true', help='Allow explicitly supported system changes (requires root for Docker install)')
    parser.add_argument('--check', action='store_true', help='Validate configuration and dependency graph without running')
    parser.add_argument('--dry-run', action='store_true', help='Preview execution; writes nothing')
    parser.add_argument('--config', help='Settings file (default: config.json here; the wizard and recovery use my-config.json)')
    parser.add_argument('--state-dir', default=str(DEFAULT_STATE))
    args = parser.parse_args(argv)
    if not (args.wizard or args.recover):
        # The wizard and recovery default to my-config.json themselves.
        args.config = args.config or str(ROOT / 'config.json')
    if args.config:
        try:
            args.config = str(settings_path(args.config))
        except RuntimeError as exc:
            parser.error(str(exc))
    if args.apply and (args.check or args.dry_run):
        parser.error('--apply cannot be combined with --check or --dry-run')
    try:
        if args.wizard:
            import wizard
            return wizard.run(execute, Path(args.state_dir), config=args.config)
        if args.recover:
            import recovery_wizard
            return recovery_wizard.run(config_path=args.config)
        return execute(args)
    except (InstallerError, RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
