#!/usr/bin/env python3
"""Keep the app password files out of plain sight in monthly backups.

The password folder (secrets/accounts) is stored in each monthly snapshot only as
an OpenSSL CMS envelope, ``secrets.tar.cms``. The unattended backup encrypts it to
a public certificate, so it needs no password at night. The matching private key
is protected by a backup password chosen once during setup and typed twice
(PBKDF2-SHA256, 600,000 iterations), and a copy travels with every snapshot, so
restoring needs only that password. Keys made before 0.28.5 open with the sudo
password that was current then (setup accepted it after a sudo check, which a
passwordless sudo rule let through unchecked, so it is no longer used).

Usage:
  sudo python3 backup_secrets.py --setup            # choose the backup password (asks once)
  sudo python3 backup_secrets.py --restore RUN --to NEW_DIR
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from neko_common import atomic_write, safe_path

KEY_DIR = Path('/etc/nekoserver-backups')
KEY_NAME = 'secrets-key.p8'
CERT_NAME = 'secrets-recipient.pem'
ARCHIVE_NAME = 'secrets.tar.cms'
SUBJECT = '/CN=NekoServer backup secrets'
MIN_LENGTH = 8


def openssl(args, data=b'', password=None, timeout=120):
    """Run openssl, handing any password over a pipe (never argv or environment)."""
    fds = ()
    if password is not None:
        if '\n' in password or '\r' in password:
            raise RuntimeError('Password cannot contain line breaks')
        read, write = os.pipe()
        os.write(write, password.encode() + b'\n')
        os.close(write)
        fds = (read,)
        args = [a.replace('{fd}', str(read)) for a in args]
    try:
        result = subprocess.run(['openssl', *args], input=data, capture_output=True, pass_fds=fds, timeout=timeout, check=False)
    finally:
        for fd in fds:
            os.close(fd)
    if result.returncode:
        raise RuntimeError('openssl ' + args[0] + ' failed')
    return result.stdout


def configured(key_dir=KEY_DIR):
    return (key_dir / KEY_NAME).is_file() and (key_dir / CERT_NAME).is_file()


def create_key(password, key_dir=KEY_DIR):
    """Make the key pair. The private key never touches disk unencrypted."""
    if not password:
        raise RuntimeError('An empty password cannot protect the backup key')
    plain = openssl(['genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072'])
    try:
        locked = openssl(['pkcs8', '-topk8', '-v2', 'aes-256-cbc', '-v2prf', 'hmacWithSHA256',
                          '-iter', '600000', '-passout', 'fd:{fd}'], plain, password)
    finally:
        del plain
    cert = openssl(['req', '-new', '-x509', '-key', '/dev/stdin', '-passin', 'fd:{fd}',
                    '-subj', SUBJECT, '-days', '36500'], locked, password)
    safe_path(key_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(key_dir / KEY_NAME, locked, 0o600)
    atomic_write(key_dir / CERT_NAME, cert, 0o600)


def unlocks(password, key_path):
    """True when ``password`` opens the private key at ``key_path``."""
    try:
        openssl(['pkey', '-in', str(key_path), '-passin', 'fd:{fd}', '-noout'], b'', password)
        return True
    except RuntimeError:
        return False


def setup(ask=getpass.getpass, out=print, key_dir=KEY_DIR, tries=3):
    """Interactively create the key, protected by a backup password typed twice: a typo
    can't become the password without being noticed. (sudo's own check is not used: a
    passwordless sudo rule lets any text through.)"""
    if configured(key_dir):
        out('Backup password-file key already set up.')
        return
    out('One-time setup: monthly backups store the app password list encrypted.')
    out('Choose a backup password for it (at least 8 characters; your login password is fine too).')
    out('You are asked for this only now. Nightly and monthly backups run on their own and never ask.')
    out('Write it down somewhere safe: without it the password list in your backups cannot be recovered.')
    for _ in range(tries):
        password = ask('Backup password: ')
        if len(password) < MIN_LENGTH:
            out(f'Use at least {MIN_LENGTH} characters.')
            continue
        if ask('Same backup password again: ') != password:
            out('The two passwords differ. Try again.')
            continue
        create_key(password, key_dir)
        out('Backup key created. Restoring the password list will ask for this backup password.')
        return
    raise RuntimeError('Backup key not created: no backup password was confirmed. Run  sudo python3 backup_secrets.py --setup  to try again')


RESTORE_PROMPT = ('Backup password that protected this backup '
                  '(for backups made before 0.28.5: your sudo password at the time): ')


def absolute(value, what):
    """An absolute, symlink-free path without '..'. Absolute paths start with '/',
    so they can never be read as a command-line option by openssl or tar."""
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise RuntimeError(f'{what} must be an absolute path without ".."')
    return safe_path(path)


def snapshot_files(snapshot):
    """Check that ``snapshot`` is a monthly snapshot whose encrypted password archive matches
    its recorded checksum, and return its three files."""
    snapshot = absolute(snapshot, 'Snapshot')
    if not snapshot.is_dir():
        raise RuntimeError('Snapshot directory not found')
    try:
        manifest = json.loads((snapshot / 'manifest.json').read_text())
    except (OSError, ValueError):
        raise RuntimeError('Not a monthly snapshot: manifest.json missing or unreadable') from None
    if not isinstance(manifest, dict) or manifest.get('format') != 'neko-maintenance-v1':
        raise RuntimeError('Not a monthly snapshot')
    files = []
    for name in (ARCHIVE_NAME, KEY_NAME, CERT_NAME):
        path = safe_path(snapshot / name)
        if not path.is_file():
            raise RuntimeError('This snapshot holds no encrypted password files' if name == ARCHIVE_NAME
                               else 'Snapshot key copy is missing')
        files.append(path)
    # CMS decryption does not detect damage: a changed byte gives changed passwords.
    # The checksum recorded when the snapshot was made is checked before anything else.
    expected = manifest.get('sha256', {}).get('secrets') if isinstance(manifest.get('sha256'), dict) else None
    if not isinstance(expected, str) or len(expected) != 64:
        raise RuntimeError('This snapshot has no checksum for its encrypted password files; refusing to restore them')
    with files[0].open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
            raise RuntimeError('The encrypted password files in this snapshot are damaged (checksum mismatch); '
                               'nothing was restored. Use another monthly snapshot.')
    return files


def restore(snapshot, destination, password):
    """Decrypt a snapshot's password folder into a new private directory."""
    archive, key, cert = snapshot_files(snapshot)
    destination = absolute(destination, 'Restore directory')
    if destination.exists() or not destination.parent.is_dir():
        raise RuntimeError('Choose a new directory, inside an existing folder, for the restored password files')
    if not unlocks(password, key):
        raise RuntimeError('Wrong password for this backup')
    plain = openssl(['cms', '-decrypt', '-binary', '-inform', 'DER', '-in', str(archive), '-inkey', str(key),
                     '-passin', 'fd:{fd}', '-recip', str(cert)], b'', password, timeout=600)
    destination.mkdir(mode=0o700)
    subprocess.run(['tar', '--extract', '--file', '-', '--no-same-owner', '--no-same-permissions', '-C', str(destination)],
                   input=plain, check=True, timeout=600)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--setup', action='store_true', help='Create the backup key, protected by a backup password you choose')
    parser.add_argument('--restore', type=Path, metavar='SNAPSHOT', help='Monthly snapshot directory (backups/monthly/RUN)')
    parser.add_argument('--to', type=Path, metavar='NEW_DIR', help='New directory for the decrypted password files')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    try:
        if args.setup:
            setup()
        elif args.restore and args.to:
            done = restore(args.restore, args.to, getpass.getpass(RESTORE_PROMPT))
            print(f'Password files restored to {done}')
        else:
            parser.error('Use --setup, or --restore SNAPSHOT --to NEW_DIR')
    except RuntimeError as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
