#!/usr/bin/env python3
"""Export only the LAN public root certificate without overwriting a destination."""

import argparse, hashlib, json, os, re, ssl
from pathlib import Path
import recovery_tools
from neko_common import safe_path, checked_settings, default_settings, mount_path

# The system's own folders: never a place for the exported copy.
SYSTEM_FOLDERS = ('/boot', '/dev', '/etc', '/proc', '/run', '/sys', '/usr', '/var')


def export_target(destination):
    """The destination as a checked path: a new .crt, .pem or .cer file (no parent steps,
    no symlinks on the way) in an existing folder that is not one of the system's."""
    target = safe_path(Path(destination).absolute())
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.(crt|pem|cer)', target.name) or any(
        top == target or top in target.parents for top in map(Path, SYSTEM_FOLDERS)
    ):
        raise RuntimeError('The export destination must be a new .crt, .pem or .cer file in an ordinary folder')
    if not target.parent.is_dir():
        raise RuntimeError('The export destination folder does not exist')
    return target


def export(config, destination):
    recovery_tools.mounts(config)
    source = safe_path(mount_path(config['data_mount']) / 'gateway/caddy-state/caddy/pki/authorities/local/root.crt')
    payload = source.read_bytes()
    if b'PRIVATE KEY' in payload or len(payload) > 32000:
        raise RuntimeError('Invalid public certificate')
    der = ssl.PEM_cert_to_DER_cert(payload.decode())
    target = export_target(destination)
    # A public certificate: the folder's default mode (usually readable by all) is right.
    with target.open('xb') as out:
        out.write(payload)
        out.flush()
        os.fsync(out.fileno())
    return hashlib.sha256(der).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    p.add_argument('--export', required=True, type=Path)
    a = p.parse_args()
    settings = safe_path(Path(a.config).absolute())
    if settings.suffix != '.json' or not settings.is_file():
        raise RuntimeError('--config names a settings file (my-config.json)')
    print('Public CA certificate exported. SHA-256 fingerprint: ' + export(json.loads(settings.read_text()), a.export))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit(
            'Certificate export failed; verify mounts, CA and a new destination. No private key is exported.'
        ) from None
