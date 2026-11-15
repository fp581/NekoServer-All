#!/usr/bin/env python3
"""Read-only by default. Explicit recovery changes never format or replace stores."""

import argparse, json, sys, subprocess
from pathlib import Path
import recovery_tools as recovery
from installer import InstallerError  # setup's check of the settings (loaded with recovery_tools anyway)
import neko_common
from neko_common import checked_settings, default_settings


# The commands, each in a line: what it does without --apply, and with it. RECOVERY.md,
# "What each operation does", has the same list.
COMMANDS = {
    'check': 'Check that the right drives are mounted and the kept installer files are whole. Changes nothing',
    'checkpoint': 'Look at Docker and where it keeps the apps; --apply saves that record on the SSD',
    'reattach': 'After replacing Linux: check that the apps on the SSD can be taken over; --apply does it',
    'snapshot': 'Show what a matched snapshot of Immich (photos and database) would copy; --apply makes it',
    'restore-test': 'Check a snapshot and show the separate test copy of Immich; --apply starts that copy',
    'resume': 'Start Immich again after a snapshot that was cut off',
    'install-docker': 'After replacing Linux: show which Docker would be installed; --apply installs it, stopped',
}
CONFIRM = {
    'reattach': "the UUID of the app SSD, typed to confirm that this is the drive to take over",
    'restore-test': 'the word RESTORE-ISOLATED, typed to confirm',
}
EPILOG = (
    'COMMAND --help shows a command\'s own options. RECOVERY.md, "What each operation does", explains each '
    'command; to get files back from a backup, use the menu instead: sudo bash install.sh --recover'
)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, epilog=EPILOG)
    p.add_argument(
        '--config',
        type=checked_settings,
        default=default_settings(Path(__file__).parent),
        help=neko_common.SETTINGS_HELP,
    )
    sub = p.add_subparsers(dest='action', required=True, metavar='COMMAND')
    for name, what in COMMANDS.items():
        cmd = sub.add_parser(name, help=what, description=what)
        # Also after the command ("check --config my.json"), the order people type it in:
        # there it was not read, and the default's error said to name a file with --config.
        cmd.add_argument('--config', type=checked_settings, default=argparse.SUPPRESS, help=neko_common.SETTINGS_HELP)
        if name not in ('check', 'resume'):
            cmd.add_argument(
                '--apply', action='store_true', help='Do it. Without it the command only shows what it would do'
            )
        if name in ('check', 'reattach'):
            cmd.add_argument(
                '--bundle',
                type=Path,
                default=Path(__file__).parent,
                help='The folder of installer files to check (default: the folder this command is in)',
            )
        if name in ('reattach', 'restore-test'):
            cmd.add_argument('--confirm', default='', help='With --apply: ' + CONFIRM[name])
        if name == 'snapshot':
            cmd.add_argument(
                '--destination',
                required=True,
                type=Path,
                help='A new folder for the snapshot, directly under recovery-snapshots on the SSD or the HDD',
            )
        if name == 'restore-test':
            cmd.add_argument('--snapshot', required=True, type=Path, help='The snapshot folder to try')
            cmd.add_argument(
                '--workspace',
                required=True,
                type=Path,
                help='A new folder for the test copy, directly under recovery-tests on the SSD or the HDD',
            )
            cmd.add_argument(
                '--port', type=int, default=2284, help='Where the test copy answers on this PC (default: 2284)'
            )
    a = p.parse_args(argv)
    config, problem = read_settings(a.config)
    if problem:
        print('Recovery stopped: ' + problem, file=sys.stderr)
        print(AFTER_FAILURE, file=sys.stderr)
        return 2
    # A closed terminal or a dropped SSH connection stops the command as Ctrl+C does: a
    # snapshot stops Immich for hours and starts it again in a finally block, which the
    # signal alone skipped (Immich then stayed off until the next restart of the PC).
    try:
        with neko_common.stopping_on_hangup():
            result = run(a, config)
        print(json.dumps(result, indent=2))
        return 1 if result.get('status') == 'needs_setup' else 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        InstallerError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        # Exception details can contain paths or API data; never echo secrets or SQL.
        print('Recovery failed: ' + failure_text(exc), file=sys.stderr)
        print(AFTER_FAILURE, file=sys.stderr)
        return 2


def read_settings(path):
    """(the settings, None), or (None, what is wrong with the file, named)."""
    try:
        return neko_common.read_settings(path), None
    except RuntimeError as exc:
        return None, f'{exc}.'


AFTER_FAILURE = (
    'Nothing was formatted, and no backup or snapshot was overwritten. Fix what it names and run the same '
    'command again. The guided menu for getting files back is  sudo bash install.sh --recover ; '
    'RECOVERY.md explains each command.'
)


def failure_text(exc):
    """What went wrong, in words, without the details that can hold secrets (a
    Compose error can include passwords, SQL personal data): our own messages, a file's
    name and the system's reason, or what kind of problem it was."""
    if isinstance(exc, (RuntimeError, InstallerError)):
        return str(exc)
    if isinstance(exc, KeyboardInterrupt):
        return 'stopped with Ctrl+C.'
    if isinstance(exc, FileNotFoundError):
        return (
            f'a file or folder it needs does not exist: {exc.filename}. Check the path. A folder it is to put '
            'things in must exist already: make it first with  sudo mkdir -p  followed by the folder.'
        )
    if isinstance(exc, OSError):
        return (
            f'{exc.strerror or "a file could not be read or written"}'
            + (f' ({exc.filename})' if exc.filename else '')
            + '.'
        )
    if isinstance(exc, KeyError):
        return f'the settings have no "{exc.args[0] if exc.args else ""}" entry. Use the settings setup made (--config /path/to/my-config.json).'
    if isinstance(exc, (ValueError, TypeError)):
        return 'a settings or record file could not be read (it is damaged, or not the file this expects).'
    return 'a program it ran did not finish.'


def check(config, bundle):
    """The drives and the kept installer, and whether its program runs on this Linux
    (Docker's start guard is that program: reattach needs it to run)."""
    recovery.mounts(config)
    version = recovery.verify_bundle(bundle, config)
    problem = recovery.runtime_problem(bundle)
    if problem:
        return {'status': 'needs_setup', 'reason': problem, 'bundle_version': version}
    return {
        'status': 'pass',
        'reason': 'Storage identities and preserved installer verified; this is not restore acceptance',
        'bundle_version': version,
    }


def checkpoint(config, apply):
    """The checkpoint, with what was done and what comes next."""
    result = recovery.checkpoint(config, apply)
    if apply:
        return {
            **result,
            'reason': 'Saved on the SSD as installer/host-checkpoint.json. Keep a copy off this PC too '
            '(RECOVERY.md, "Prepare while the current server works").',
        }
    return {
        **result,
        'reason': 'Preview only: nothing was saved. To save it, run the same command with  --apply  added.',
    }


def run(a, config):
    if a.action == 'check':
        return check(config, a.bundle)
    if a.action == 'checkpoint':
        return checkpoint(config, a.apply)
    if a.action == 'reattach':
        return recovery.reattach(config, a.bundle, a.apply, a.confirm)
    if a.action == 'snapshot':
        return recovery.snapshot(config, a.destination, a.apply, a.config)
    if a.action == 'resume':
        return recovery.resume(config)
    if a.action == 'install-docker':
        return recovery.install_docker(config, a.apply)
    return recovery.restore_test(config, a.snapshot, a.workspace, a.port, a.apply, a.confirm)


if __name__ == '__main__':
    raise SystemExit(main())
