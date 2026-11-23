"""Read-only Unix permission checks for the accounts used by installed services.

These checks inspect mode bits and known group membership. Actual container,
ACL and Samba client behavior still belongs in hardware acceptance.
"""

import os
from pathlib import Path
import pwd

from neko_common import files_root, media_services, music_downloads, safe_path


def mode_shift(stat, uid, groups):
    """Where the bits for this account are in a file's mode: the owner's, the group's or
    everyone else's."""
    if stat.st_uid == uid:
        return 6
    if stat.st_gid in groups:
        return 3
    return 0


def access(path, uid, groups, mask, bind_root=False):
    """Check traversal and requested access without changing any files."""
    path = safe_path(path)
    # The settings name the folder: it is looked at only by its real path, and only when
    # that is the path asked for (a link on the way is refused, as safe_path refuses it).
    real = os.path.realpath(path)
    if not real.startswith(os.sep) or real != os.path.abspath(path):
        raise RuntimeError(f'Refusing symlink path: {path}')
    path = Path(real)
    for item in (path,) if bind_root else (*reversed(path.parents), path):
        stat = item.stat()
        wanted = mask if item == path else 1
        if (stat.st_mode >> mode_shift(stat, uid, groups)) & wanted != wanted:
            return False
        if item != path and not item.is_dir():
            return False
    return path.is_dir()


def inspected(name, path, owner, groups, writable, bind_root=False):
    """The row for one folder: whether the account may reach it and use it as asked."""
    try:
        good = access(path, owner, groups, 7 if writable else 5, bind_root)
    except (OSError, RuntimeError, TypeError):
        good = False
    return {
        'id': 'permission:' + name,
        'name': name + ' filesystem access',
        'status': 'pass' if good else 'needs_setup',
        'detail': 'Bound-root access for appdata, or full host traversal for shares/libraries, and requested Unix mode-bit access checked; existing ownership and permissions are preserved. Verify actual container/ACL/client access on the server.',
    }


def media_rows(config):
    """The media apps' own folders and the libraries they write to."""
    data = Path(config['data_mount'])
    uid = config.get('media', {}).get('uid', 1000)
    gid = config.get('media', {}).get('gid', 1000)
    rows = []
    for app in media_services(config):
        # Every app, Seerr included, runs as media.uid/gid (since v0.21.1).
        rows.append(inspected(app + ':appdata', data / 'appdata' / app, uid, {gid}, True, bind_root=True))
    for category in ('movies', 'tv', 'music', 'downloads'):
        library = files_root(config, 'music' if category == 'music' else 'movies') / 'library' / category
        rows.append(inspected('media:' + category, library, uid, {gid}, True))
    rows.append(inspected('media:music-downloads', music_downloads(config), uid, {gid}, True))
    return rows


def samba_rows(config):
    """The shared folders, for the Samba account."""
    from modules.samba import samba_account

    user = samba_account(config.get('networking', {}))
    try:
        record = pwd.getpwnam(user)
        groups = set(os.getgrouplist(user, record.pw_gid))
    except (KeyError, OSError):
        return [
            {
                'id': 'permission:samba:user',
                'name': 'Samba account access',
                'status': 'needs_setup',
                'detail': 'Configure the intended non-root Samba account and verify authenticated client access.',
            }
        ]
    settings = config['networking']
    from modules.samba import placed_shares, wanted_shares, writable as share_writable

    media = Path(config['media_mount'])
    folders = {name: media / relative for name, relative in wanted_shares(config).items()}
    return [
        inspected('samba:' + name, path, record.pw_uid, groups, share_writable(name, settings))
        for name, path in {**folders, **placed_shares(config)}.items()
    ]


def check(config, modules):
    rows = []
    if 'media' in modules and media_services(config):
        rows.extend(media_rows(config))
    if 'samba' in modules:
        rows.extend(samba_rows(config))
    return rows
