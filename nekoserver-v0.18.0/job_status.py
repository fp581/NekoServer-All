"""Private job outcomes; reports contain no exceptions, paths or credentials."""
import json
import os
from pathlib import Path
import time

from neko_common import atomic_json, safe_path


def execute(data, name, operation, clock=time.time, verify=None):
    if name not in ('archive', 'database', 'external'):
        raise RuntimeError('Unknown backup status job')
    if data is None:
        return operation()
    data = safe_path(data)

    def check_storage():
        if verify:
            verify()
        if not os.path.ismount(data):
            raise RuntimeError('Application SSD must be mounted before recording a backup outcome')

    check_storage()
    path = safe_path(data / 'reports' / 'jobs' / f'{name}.json')
    previous = {}
    try:
        if path.stat().st_size < 64000:
            previous = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    if not isinstance(previous, dict):
        previous = {}
    last = previous.get('last_success')
    if type(last) not in (int, float) or not 0 <= last <= clock():
        last = None
    row = {'job': name, 'status': 'running', 'started_at': clock(),
           'finished_at': None, 'last_success': last}
    check_storage()
    atomic_json(path, row)
    try:
        result = operation()
    except BaseException:
        row.update(status='fail', finished_at=clock())
        check_storage()
        atomic_json(path, row)
        raise
    row.update(status='pass', finished_at=clock(), last_success=clock())
    check_storage()
    atomic_json(path, row)
    return result
