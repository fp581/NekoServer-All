#!/usr/bin/env python3
"""Interactive account setup for already deployed NekoServer apps."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import account_store
import account_setup
from modules.accounts import finish_pending
from types import SimpleNamespace
from neko_common import safe_path, verify_storage, checked_settings, default_settings
from recovery_tools import lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print('Read-only: use --apply interactively to choose and configure accounts. No secrets printed.')
        return 0
    if os.geteuid() != 0 or not sys.stdin.isatty():
        parser.error('Run with sudo in an interactive terminal')
    config = json.loads(safe_path(args.config).read_text())
    config['accounts'] = {**config.get('accounts', {}), 'enabled': True}
    verify_storage(config)
    apps = []
    for app in account_store.selected(config, ('media', 'photos', 'nextcloud')):
        deployment = 'navidrome' if app == 'navidrome_listener' else app
        if safe_path(Path(config['data_mount']) / 'compose' / deployment / 'compose.yaml').is_file():
            apps.append(app)
    if not apps:
        parser.error('No selected deployed app manifests found; run the installer first')
    before = account_store.get(config, 'qbittorrent')
    with lock(config):
        account_store.collect(config, apps)
        pending = account_setup.provision(config, apps)
    pending = finish_pending(SimpleNamespace(config=config, apply=True, log=print), pending)
    passed_on = pass_on_qbittorrent(args.config, config, apps, before)
    print('Private credential inventory: ' + str(account_store.folder(config) / 'APP-CREDENTIALS.txt'))
    return 1 if pending or not passed_on else 0


def pass_on_qbittorrent(settings, config, apps, before, run=subprocess.run):
    """A qBittorrent login changed here (c: enter the current login) is passed on to the
    download clients setup made in Sonarr, Radarr and Lidarr. They keep their own copy of
    it and would go on using the old one: downloads would stop without a word. True when
    nothing was left to do or it was passed on."""
    now = account_store.get(config, 'qbittorrent', verified=True)
    if 'qbittorrent' not in apps or not before or not now or \
            (now['username'], now['password']) == (before['username'], before['password']):
        return True
    managers = [app for app in ('sonarr', 'radarr', 'lidarr') if app in config.get('media', {}).get('services', [])]
    if not managers:
        return True
    print('qBittorrent: its login changed; passing it on to the download client in ' + ', '.join(managers))
    script = Path(__file__).with_name('media-connect.py')
    if run([sys.executable, str(script), '--config', str(settings), '--pass-on-login', '--apply'], check=False).returncode:
        print('qBittorrent: the new login is not in every app yet (see above). Enter it in each app '
              '(Settings, Download Clients, Neko-qBittorrent), or run  sudo bash install.sh  again')
        return False
    return True


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyboardInterrupt, EOFError):
        print('Account setup incomplete; saved choices preserved. No credentials printed.', file=sys.stderr)
        raise SystemExit(1) from None
