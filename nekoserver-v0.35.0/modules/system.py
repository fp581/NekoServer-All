"""Read-only host inventory and prerequisites."""
import os
import platform
import shutil
import subprocess

import host
from neko_common import host_timezone, local_timezone

DEPENDENCIES = []
DESCRIPTION = "Inspect distribution, hardware and basic prerequisites"

def timezone_note(config):
    if config.get('timezone', 'auto') == 'auto' and local_timezone() is None:
        return ('Timezone: this PC\'s timezone could not be read, so backups and updates run at UTC times '
                '(set "timezone" in my-config.json, e.g. "Europe/London", to change that)')
    return f'Timezone for backups and updates: {host_timezone(config)}'


def run(context):
    os_release = host.os_release()
    context.log(f"OS: {os_release.get('PRETTY_NAME', platform.system())}; kernel: {platform.release()}")
    context.log(f"Architecture: {platform.machine()}; available CPUs: {os.cpu_count()}")
    for cmd in context.config.get('required_commands', []):
        if not shutil.which(cmd):
            raise RuntimeError(f"Missing required command: {cmd}")
    if shutil.which('lspci'):
        p = subprocess.run(['lspci'], capture_output=True, text=True, timeout=15, check=False)
        for line in p.stdout.splitlines():
            if any(w in line.lower() for w in ('vga compatible', 'display controller', '3d controller')):
                context.log(f"GPU: {line}")
    else:
        context.log('GPU inspection skipped: lspci not installed')
    context.log(timezone_note(context.config))
    context.log('System check complete (read-only).')
