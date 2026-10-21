"""Read-only host inventory and prerequisites."""
import os
import platform
import shutil
import subprocess

import host

DEPENDENCIES = []
DESCRIPTION = "Inspect distribution, hardware and basic prerequisites"

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
    context.log('System check complete (read-only).')
