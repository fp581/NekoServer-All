"""Install Tailscale and guide browser authorization without storing auth secrets."""
import json
import os
import shutil
import subprocess
from urllib.parse import urlsplit

import guided_setup
from neko_common import arch_host

DEPENDENCIES = ['system']
DESCRIPTION = 'Install Tailscale; open browser authorization and verify the connection'


def run_cmd(args, timeout=240):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(args[0] + ' failed; inspect the local service/package status')
    return result.stdout.strip()


def connected():
    try:
        result = subprocess.run(['tailscale', 'status', '--json'], text=True,
                                capture_output=True, timeout=20)
        return result.returncode == 0 and json.loads(result.stdout).get('BackendState') == 'Running'
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return False


def login_targets():
    # tailscale up emits consecutive JSON objects, including AuthURL before login.
    # Bound its wait; the daemon retains the approval flow after this CLI exits.
    result = subprocess.run(['tailscale', 'up', '--json', '--timeout=3s'],
                            text=True, capture_output=True, timeout=12)
    remaining = result.stdout.strip()
    decoder = json.JSONDecoder()
    while remaining:
        try:
            value, end = decoder.raw_decode(remaining)
        except ValueError:
            break
        remaining = remaining[end:].lstrip()
        if not isinstance(value, dict):
            continue
        url = value.get('AuthURL', '')
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed and guided_setup.valid_url(url) and parsed.scheme == 'https' and
                parsed.hostname == 'login.tailscale.com' and parsed.port in (None, 443)):
            return [('url', url)]
    if connected():
        return [('url', 'https://console.tailscale.com/admin/machines')]
    print('No authorization link available. Run sudo tailscale up in another Konsole, '
          'follow its sign-in link, then return. Existing preferences were not reset.')
    return [('url', 'https://console.tailscale.com/admin/machines')]


def run(context):
    if not context.apply:
        context.log('Tailscale preview: interactive browser sign-in runs only with --apply')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required for Tailscale installation')
    if not arch_host():
        raise RuntimeError('Tailscale auto-install supports Arch/Nyarch; Bazzite needs a separate backend')
    if not shutil.which('tailscale'):
        if not shutil.which('pacman'):
            raise RuntimeError('pacman required')
        run_cmd(['pacman', '-S', '--needed', '--noconfirm', 'tailscale'], timeout=900)
    run_cmd(['systemctl', 'enable', '--now', 'tailscaled'], timeout=45)
    if not connected():
        done = guided_setup.checkpoint(context, 'Tailscale: connect this server', [
            'What this is: Tailscale lets your phone and other devices reach this server '
            'from anywhere, as if you were at home, without opening your router to the '
            'internet. It is free for personal use.',
            'A Tailscale sign-in page opened in the browser. Sign in (or create a free '
            'account) with Google, Microsoft, Apple or GitHub. Use the account you will '
            'also use on your phone.',
            'Click "Connect" (or "Authorize") to add this server to your account.',
            'If no page opened: open a second terminal, run  sudo tailscale up , open the '
            'link it prints, and come back here.',
            'Answer yes. The installer checks that this server is connected.',
        ], prepare=login_targets, verify=connected)
        if not done:
            return {'status': 'needs_setup', 'reason': 'Tailscale authorization pending; reapply interactively or run sudo tailscale up'}
    context.log('Tailscale local backend connected; client access still needs a real phone test')
