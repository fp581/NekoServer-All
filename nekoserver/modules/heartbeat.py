"""Configure an OS-local Rust heartbeat to an independently hosted monitor."""
import guided_setup
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import native_runtime
from neko_common import VERSION, atomic_json, safe_path, check_units, write_units

DEPENDENCIES = ['system', 'recovery']
DESCRIPTION = 'Rust HTTPS heartbeat for independent missing-server alerts'
PRIVATE = '/etc/nekoserver-heartbeat/config.json'


def endpoint(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::([0-9]{1,5}))?/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', value)
    return bool(match and len(value) <= 2048 and value.rsplit('/',1)[-1] not in ('start','fail') and (match[1] is None or 0 < int(match[1]) <= 65535))


def interval(config):
    opts = config.get('heartbeat', {})
    if not isinstance(opts, dict):
        raise RuntimeError('heartbeat must be an object')
    value = opts.get('interval_seconds', 300)
    if type(value) is not int or not 60 <= value <= 3600:
        raise RuntimeError('heartbeat interval must be 60..3600 seconds')
    return value


def definitions(config, binary, private):
    return {
        'nekoserver-heartbeat.service': (
            '[Unit]\nDescription=NekoServer independent availability heartbeat\nAfter=network-online.target\n'
            '[Service]\nType=oneshot\nUser=root\nUMask=0077\n'
            'RuntimeDirectory=nekoserver-heartbeat\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\n'
            f'ExecStart={binary} heartbeat --config {private}\n'
            'TimeoutStartSec=40\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
            'PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n'
            'InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock\n'
        ),
        'nekoserver-heartbeat.timer': (
            '[Unit]\nDescription=NekoServer availability heartbeat schedule\n'
            f'[Timer]\nOnBootSec=90s\nOnUnitInactiveSec={interval(config)}s\nAccuracySec=1s\n'
            '[Install]\nWantedBy=timers.target\n'
        ),
    }


def saved_endpoint(private):
    """The ping URL set up before ('' when none), from its root-only file."""
    if not private.exists():
        return ''
    if private.stat().st_uid != 0 or private.stat().st_mode & 0o077:
        raise RuntimeError('Existing heartbeat configuration must be root-owned and private')
    return json.loads(private.read_text()).get('endpoint', '')


def run(context):
    interval(context.config)
    if not context.apply:
        context.log('Independent heartbeat available with --apply; configure a monitor outside this PC')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Heartbeat requires root')
    from modules._schedules import switched_off
    if switched_off(context.config, 'heartbeat', 'nekoserver-heartbeat', context.log):
        context.log('Outage monitor: off by choice. This server no longer checks in, so pause (or delete) the '
                    'check at your outside monitor too, or it will report an outage it is expecting.')
        return
    private = safe_path(PRIVATE)
    address = saved_endpoint(private)
    if not address and guided_setup.wanted(context, 'heartbeat', 'Outage alerts if the server goes offline', [
            'An outside service (such as Healthchecks.io) can email you when this server stops',
            'checking in, for example after a power cut. It needs a free account there.',
            'This is for advanced users; most people can skip it.',
    ]) is False:
        context.log('Outage monitor: skipped by choice')
        return
    if not address and sys.stdin.isatty():
        if not guided_setup.checkpoint(context, 'Set up an independent outage monitor', [
            'Choose a monitor outside this PC, such as Healthchecks.io or your own independently hosted compatible service.',
            'Create a periodic check and set the period to match heartbeat.interval_seconds '
            '(default five minutes) with an appropriate grace period, such as ten minutes.',
            'Configure its intended alert recipient/integration directly with the provider. '
            'The installer does not choose a recipient or send a test message in this step.',
            'Copy the base success-ping HTTPS URL. After confirming, paste it into the terminal (it is not shown). '
            'Later verify pings in the provider and actual missed-ping/recovery alerts.'
        ], [('url', 'https://healthchecks.io/docs/')]):
            return {'status': 'needs_setup', 'reason': 'Independent monitor setup pending'}
        context.log('Create an independent missing-ping monitor, set its period/grace and alert recipient, then enter its private success-ping HTTPS URL.')
        address = guided_setup.private_entry(
            'Private monitor ping URL (blank defers): ', endpoint,
            'That is not a ping URL. It starts with https:// and looks like '
            'https://hc-ping.com/1a2b3c4d-... Copy it again from the monitor and paste it.')
    if not address:
        return {'status':'needs_setup','reason':'Configure an independent monitor and enter its private ping URL through interactive heartbeat setup'}
    if not endpoint(address):
        raise RuntimeError('Invalid heartbeat destination')
    if not shutil.which('curl'):
        raise RuntimeError('Install curl and system CA certificates before enabling heartbeat')
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, context.config, Path('/usr/local/lib/nekoserver-heartbeat')/('v'+VERSION))
    units = definitions(context.config, binary, private)
    check_units(units, 'heartbeat')
    atomic_json(private, {'endpoint':address})
    write_units(units)
    for args in (['systemctl','daemon-reload'], ['systemctl','enable','--now','nekoserver-heartbeat.timer']):
        subprocess.run(args,check=True,capture_output=True,timeout=30)
    context.log('Independent heartbeat timer enabled. Verify the monitor receives pings and sends a missed-ping/recovery alert; provider alert configuration is separate.')
