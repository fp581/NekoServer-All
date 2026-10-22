"""Retire only recognized NekoServer units, preserving custom administrator units."""
from neko_common import safe_path


def retire(paths, run):
    existing = []
    for given in paths:
        path = safe_path(given)
        if not path.exists():
            continue
        lines = path.read_text().splitlines()
        owner = path.name.rsplit('.', 1)[0]
        if not any(line.startswith('Description=NekoServer ') or line in (
            'Description='+owner, 'Description='+owner+' timer') for line in lines):
            raise RuntimeError('Unmanaged schedule preserved: '+path.name)
        existing.append(path)
    # Validate every file before stopping any service or timer.
    for path in sorted(existing, key=lambda p: p.suffix != '.timer'):
        action = 'disable' if path.suffix == '.timer' else 'stop'
        args = ['systemctl', action]
        if action == 'disable':
            args.append('--now')
        run([*args, path.name], check=True, capture_output=True, timeout=60)


def remove(paths, run, log=print):
    """Retire recognized NekoServer schedules (as retire does) and delete their unit files,
    so nothing is left behind to run or to show as overdue. Returns the names removed."""
    retire(paths, run)
    removed = [safe_path(p) for p in paths if safe_path(p).exists()]
    for path in removed:
        path.unlink()
    if removed:
        run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=60)
        log('Removed schedules: ' + ', '.join(p.name for p in removed))
    return [p.name for p in removed]
