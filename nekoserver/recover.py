#!/usr/bin/env python3
"""Read-only by default. Explicit recovery changes never format or replace stores."""

import argparse, json, sys, subprocess
from pathlib import Path
import recovery_tools as recovery
from neko_common import checked_settings, default_settings


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=checked_settings, default=default_settings(Path(__file__).parent))
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('check', 'checkpoint', 'reattach', 'snapshot', 'restore-test', 'resume', 'install-docker'):
        cmd = sub.add_parser(name)
        if name not in ('check', 'resume'):
            cmd.add_argument('--apply', action='store_true')
        if name in ('check', 'reattach'):
            cmd.add_argument('--bundle', type=Path, default=Path(__file__).parent)
        if name in ('reattach', 'restore-test'):
            cmd.add_argument('--confirm', default='')
        if name == 'snapshot':
            cmd.add_argument('--destination', required=True, type=Path)
        if name == 'restore-test':
            cmd.add_argument('--snapshot', required=True, type=Path)
            cmd.add_argument('--workspace', required=True, type=Path)
            cmd.add_argument('--port', type=int, default=2284)
    a = p.parse_args(argv)
    try:
        config = json.loads(Path(a.config).read_text())
        result = run(a, config)
        print(json.dumps(result, indent=2))
        return 1 if result.get('status') == 'needs_setup' else 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        # Exception details can contain paths or API data; never echo secrets or SQL.
        print('Recovery failed: ' + failure_text(exc), file=sys.stderr)
        print(AFTER_FAILURE, file=sys.stderr)
        return 2


AFTER_FAILURE = (
    'Nothing was formatted, and no backup or snapshot was overwritten. Fix what it names and run the same '
    'command again. The guided menu for getting files back is  sudo bash install.sh --recover ; '
    'RECOVERY.md explains each command.'
)


def failure_text(exc):
    """What went wrong, in words, without the details that can hold secrets (a
    Compose error can include passwords, SQL personal data): our own messages, a file's
    name and the system's reason, or what kind of problem it was."""
    if isinstance(exc, RuntimeError):
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
