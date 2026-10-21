#!/usr/bin/env python3
"""Interactive account setup for already deployed NekoServer apps."""
import argparse
import json
import os
from pathlib import Path
import sys

import account_store
import account_setup
from modules.accounts import finish_pending
from types import SimpleNamespace
from neko_common import safe_path, verify_storage
from recovery_tools import lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
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
    with lock(config):
        account_store.collect(config, apps)
        pending = account_setup.provision(config, apps)
    pending = finish_pending(SimpleNamespace(config=config, apply=True, log=print), pending)
    print('Private credential inventory: ' + str(account_store.folder(config) / 'APP-CREDENTIALS.txt'))
    return 1 if pending else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyboardInterrupt, EOFError):
        print('Account setup incomplete; saved choices preserved. No credentials printed.', file=sys.stderr)
        raise SystemExit(1) from None
