"""The old-release guard as NekoServer 0.47.9 shipped it (capabilities.newer_installed,
copied unchanged from the v0.47.9-beta tag), for the test of the way back from a failed
upgrade (test_upgrade): a 0.47.9 folder put back runs this guard, which knows nothing of
the upgrade's record, so the newer release's runtime must not be seen while it runs.
Kept as a copy because CI checks out without tags."""

import json
from pathlib import Path


def newer_installed(root=None, version=None, units=None):
    """The NekoServer release this server already runs when it is newer than this folder's,
    else None. An upgrade from the dashboard puts the new release on the SSD and sets it up
    from there; the folder you downloaded still holds the old one, so running setup from it
    again would set the old release up over the new one (seen on a VM upgraded from 0.47.6
    to 0.47.8). The release running is the newest runtime copy on the SSD
    (installer/runtime/v<release>) that an installed service still points at."""
    import native_runtime
    from neko_common import VERSION

    root = Path(root or Path(__file__).resolve().parent)
    units = units or native_runtime.SYSTEMD_UNITS
    try:
        settings = next(p for p in (root / 'my-config.json', root / 'config.json') if p.is_file())
        data = Path(json.loads(settings.read_text())['data_mount'])
        runtimes = native_runtime.release_folders(data / 'installer/runtime')
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return None
    running = [native_runtime.release_number(p.name) for p in runtimes if native_runtime.in_use(p, units)]
    this = native_runtime.release_number('v' + (version or VERSION))
    newest = max(running, default=None)
    if newest and this and newest > this:
        return '.'.join(str(n) for n in newest)
    return None
