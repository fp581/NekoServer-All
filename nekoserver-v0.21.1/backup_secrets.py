#!/usr/bin/env python3
"""Keep the app password files out of plain sight in monthly backups.

The password folder (secrets/accounts) is stored in each monthly snapshot only as
an OpenSSL CMS envelope, ``secrets.tar.cms``. The unattended backup encrypts it to
a public certificate, so it needs no password at night. The matching private key
is protected by the administrator's sudo password (PBKDF2-SHA256, 600,000
iterations) and a copy travels with every snapshot, so restoring needs only that
password. Changing the sudo password later does not re-encrypt older backups:
they keep opening with the password that was current when the key was made.

Usage:
  sudo python3 backup_secrets.py --setup            # create the key (asks once)
  sudo python3 backup_secrets.py --restore RUN --to NEW_DIR
"""
import argparse
import getpass
import os
from pathlib import Path
import pwd
import subprocess
import sys

from neko_common import atomic_write, safe_path

KEY_DIR = Path('/etc/nekoserver-backups')
KEY_NAME = 'secrets-key.p8'
CERT_NAME = 'secrets-recipient.pem'
ARCHIVE_NAME = 'secrets.tar.cms'
SUBJECT = '/CN=NekoServer backup secrets'


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
        result = subprocess.run(['openssl', *args], input=data, capture_output=True, pass_fds=fds, timeout=timeout)
    finally:
        for fd in fds:
            os.close(fd)
    if result.returncode:
        raise RuntimeError('openssl ' + args[0] + ' failed')
    return result.stdout


def configured(key_dir=KEY_DIR):
    return (key_dir / KEY_NAME).is_file() and (key_dir / CERT_NAME).is_file()


def sudo_user():
    name = os.environ.get('SUDO_USER', '')
    if not name or name == 'root':
        raise RuntimeError('Run through sudo from your own account so your sudo password can protect the backup key')
    pwd.getpwnam(name)
    return name


def is_sudo_password(user, password):
    """True when ``password`` lets ``user`` run sudo (forced fresh check, no cache)."""
    try:
        result = subprocess.run(['runuser', '-u', user, '--', 'sudo', '-k', '-S', '-p', '', '-v'],
                                input=(password + '\n').encode(), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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


def setup(ask=getpass.getpass, out=print, key_dir=KEY_DIR, user=None, check=is_sudo_password, tries=3):
    """Interactively create the key, protected by the verified sudo password."""
    if configured(key_dir):
        out('Backup password-file key already set up.')
        return
    user = user or sudo_user()
    out('One-time setup: monthly backups store the app password list encrypted.')
    out('You are asked for this only now. Nightly and monthly backups run on their own and never ask.')
    out(f'It will open with the sudo password of {user}. Write that password down somewhere safe:')
    out('without it the password list in your backups cannot be recovered.')
    for _ in range(tries):
        password = ask(f'Sudo password for {user}: ')
        if check(user, password):
            create_key(password, key_dir)
            out('Backup key created. Restoring the password list will ask for this password.')
            return
        out('That is not the sudo password for ' + user + '.')
    raise RuntimeError('Backup key not created: sudo password not confirmed')


def restore(snapshot, destination, password):
    """Decrypt a snapshot's password folder into a new private directory."""
    snapshot, destination = safe_path(Path(snapshot)), safe_path(Path(destination))
    archive, key, cert = (snapshot / ARCHIVE_NAME, snapshot / KEY_NAME, snapshot / CERT_NAME)
    if not archive.is_file():
        raise RuntimeError('This snapshot holds no encrypted password files')
    if not key.is_file() or not cert.is_file():
        raise RuntimeError('Snapshot key copy is missing')
    if not destination.is_absolute() or destination.exists():
        raise RuntimeError('Choose a new absolute directory for the restored password files')
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
    parser.add_argument('--setup', action='store_true', help='Create the backup key, protected by your sudo password')
    parser.add_argument('--restore', type=Path, metavar='SNAPSHOT', help='Monthly snapshot directory (backups/monthly/RUN)')
    parser.add_argument('--to', type=Path, metavar='NEW_DIR', help='New directory for the decrypted password files')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo')
    try:
        if args.setup:
            setup()
        elif args.restore and args.to:
            done = restore(args.restore, args.to, getpass.getpass('Password that protected this backup (your sudo password at the time): '))
            print(f'Password files restored to {done}')
        else:
            parser.error('Use --setup, or --restore SNAPSHOT --to NEW_DIR')
    except RuntimeError as exc:
        sys.exit(str(exc))


if __name__ == '__main__':
    main()
