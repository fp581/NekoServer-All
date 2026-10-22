"""Install private ntfy delivery with a Rust timer independent of SSD availability."""
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

DEPENDENCIES = ['dashboard']
DESCRIPTION = 'Background ntfy alerts, grouped retries and recovery notices (Rust)'
PRIVATE_CONFIG = '/etc/nekoserver-notifications/config.json'


def options(config):
    opts = config.get('notifications', {})
    if not isinstance(opts, dict):
        raise RuntimeError('notifications must be an object')
    result = {}
    for key, default, low, high in (
        ('debounce_seconds', 180, 0, 3600),
        ('repeat_seconds', 43200, 3600, 604800),
        ('minimum_interval_seconds', 300, 60, 3600),
    ):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid notification timing')
        result[key] = value
    return result


def endpoint(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::([0-9]{1,5}))?/[A-Za-z0-9_-]{1,128}', value)
    return bool(match and (match[1] is None or 0 < int(match[1]) <= 65535))


def valid_token(token):
    return isinstance(token, str) and len(token) <= 512 and re.fullmatch(r'[A-Za-z0-9._-]*', token) is not None


def definitions(binary, config):
    return {
        'nekoserver-notifications.service': (
            '[Unit]\nDescription=NekoServer background health notifications\n'
            'Wants=nekoserver-dashboard-collect.service\nAfter=nekoserver-dashboard-collect.service network-online.target\n'
            '[Service]\nType=oneshot\nUser=root\nUMask=0077\n'
            'StateDirectory=nekoserver-notifications\nStateDirectoryMode=0700\n'
            'RuntimeDirectory=nekoserver-notifications\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n'
            f'ExecStart={binary} notify --config {config} --snapshot /run/nekoserver-dashboard/status.json '
            '--state /var/lib/nekoserver-notifications --runtime /run/nekoserver-notifications\n'
            'TimeoutStartSec=45\nNoNewPrivileges=yes\nProtectHome=yes\nProtectSystem=strict\nPrivateTmp=yes\n'
            'PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n'
            'InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock\n'
        ),
        'nekoserver-notifications.timer': (
            '[Unit]\nDescription=NekoServer scheduled health notifications\n'
            '[Timer]\nOnBootSec=90s\nOnUnitInactiveSec=60s\n'
            '[Install]\nWantedBy=timers.target\n'
        ),
    }


def run(context):
    timing = options(context.config)
    if not context.apply:
        context.log('Background notifications available with --apply; configure your own HTTPS ntfy topic')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Notification installation requires root')
    private = safe_path(PRIVATE_CONFIG)
    existing = json.loads(private.read_text()) if private.exists() else {}
    address = context.config.get('notifications', {}).get('endpoint', '') or existing.get('endpoint', '')
    token = existing.get('token', '')
    if not address and guided_setup.wanted(context, 'notifications', 'Phone alerts when something fails', [
            'NekoServer can send a message to your phone when a backup or app fails.',
            'It uses ntfy, a free notification app. Setting it up takes about 10 minutes.',
            'You can skip this; the dashboard on the server still shows everything.',
    ]) is False:
        context.log('Phone alerts: skipped by choice')
        return
    if not address and sys.stdin.isatty():
        if not guided_setup.checkpoint(context, 'Set up phone notifications', [
            'Use your chosen access-controlled ntfy server/topic. The opened documentation explains phone subscriptions.',
            'On your phone, install/open the ntfy client and subscribe to that topic with its required authentication.',
            'Keep the full private HTTPS topic URL and access token ready. Enter them in the terminal after confirming.',
            'No notification is sent by this browser step. Actual phone receipt must still be tested.'
        ], [('url', 'https://ntfy.sh/docs/subscribe/phone/')]):
            return {'status': 'needs_setup', 'reason': 'Notification subscription setup pending'}
        context.log('Subscribe your phone to an access-controlled ntfy topic. Enter its full HTTPS URL; the access token stays outside your installer config.')
        address = guided_setup.private_entry(
            'Private ntfy topic URL (blank defers): ', endpoint,
            'That is not a topic URL. It starts with https:// and ends with the topic name, '
            'such as https://ntfy.example.com/my-server-alerts. Copy it again and paste it.')
        if address:
            token = guided_setup.private_entry(
                'ntfy access token (blank for no token): ', valid_token,
                'That is not an access token: it has only letters, digits, ".", "_" and "-" '
                '(such as tk_abc123...). Copy it again from ntfy and paste it.')
    if not address:
        return {'status': 'needs_setup', 'reason': 'Set a private HTTPS ntfy topic through interactive notification setup; no destination was guessed'}
    if not endpoint(address) or not valid_token(token):
        raise RuntimeError('Invalid private notification destination or token')
    if existing.get('endpoint') and address != existing['endpoint']:
        raise RuntimeError('Configured destination differs; existing private destination preserved. Review it explicitly before replacing it.')
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, context.config, Path('/usr/local/lib/nekoserver-notifications') / ('v' + VERSION))
    if not shutil.which('curl'):
        raise RuntimeError('Install curl and system CA certificates before enabling HTTPS delivery')
    units = definitions(binary, private)
    check_units(units, 'notification')
    atomic_json(private, {'endpoint': address, 'token': token, **timing})
    write_units(units)
    for cmd in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'nekoserver-notifications.timer']):
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    context.log('Background notification timer enabled; verify delivery on your subscribed phone using the documented native test command.')
