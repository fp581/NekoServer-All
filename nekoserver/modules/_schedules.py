"""Retire only recognized NekoServer units, preserving custom administrator units."""

import subprocess

from neko_common import SYSTEMD_UNITS, safe_path

TIMER = '.timer'


def retire(paths, run):
    existing = []
    for given in paths:
        path = safe_path(given)
        if not path.exists():
            continue
        lines = path.read_text().splitlines()
        owner = path.name.rsplit('.', 1)[0]
        if not any(
            line.startswith('Description=NekoServer ')
            or line in ('Description=' + owner, 'Description=' + owner + ' timer')
            for line in lines
        ):
            raise RuntimeError('Unmanaged schedule preserved: ' + path.name)
        existing.append(path)
    # Validate every file before stopping any service or timer.
    for path in sorted(existing, key=lambda p: p.suffix != TIMER):
        action = 'disable' if path.suffix == TIMER else 'stop'
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


def switched_off(config, section, name, log, run=None):
    """An optional extra switched off by choice ("wanted": false under `section`): its
    installed schedule `name` (.timer and .service) is stopped and switched off, before
    any saved destination is read or used again. The unit files and the saved destination
    stay, so switching it back on needs no new setup. True when it is switched off."""
    if config.get(section, {}).get('wanted') is not False:
        return False
    units = [SYSTEMD_UNITS / (name + TIMER), SYSTEMD_UNITS / (name + '.service')]
    if any(safe_path(u).exists() for u in units):
        retire(units, run or subprocess.run)
        log(name + ': switched off by choice; its schedule no longer runs')
    return True
