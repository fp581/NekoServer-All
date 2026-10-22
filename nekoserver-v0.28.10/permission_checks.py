"""Read-only Unix permission checks for the accounts used by installed services.

These checks inspect mode bits and known group membership. Actual container,
ACL and Samba client behavior still belongs in hardware acceptance.
"""
import os
from pathlib import Path
import pwd

from neko_common import safe_path


def access(path, uid, groups, mask, bind_root=False):
    """Check traversal and requested access without changing any files."""
    path = safe_path(path)
    for item in ((path,) if bind_root else (*reversed(path.parents), path)):
        stat = item.stat()
        wanted = mask if item == path else 1
        shift = 6 if stat.st_uid == uid else 3 if stat.st_gid in groups else 0
        if (stat.st_mode >> shift) & wanted != wanted:
            return False
        if item != path and not item.is_dir():
            return False
    return path.is_dir()


def check(config, modules):
    rows = []
    data = Path(config['data_mount'])
    media = Path(config['media_mount'])
    uid = config.get('media', {}).get('uid', 1000)
    gid = config.get('media', {}).get('gid', 1000)

    def inspect(name, path, owner, groups, writable, bind_root=False):
        try:
            good = access(path, owner, groups, 7 if writable else 5, bind_root)
        except (OSError, RuntimeError, TypeError):
            good = False
        rows.append({
            'id': 'permission:' + name,
            'name': name + ' filesystem access',
            'status': 'pass' if good else 'needs_setup',
            'detail': 'Bound-root access for appdata, or full host traversal for shares/libraries, and requested Unix mode-bit access checked; existing ownership and permissions are preserved. Verify actual container/ACL/client access on the server.',
        })

    if 'media' in modules:
        for app in config['media']['services']:
            # Every app, Seerr included, runs as media.uid/gid (since v0.21.1).
            inspect(app + ':appdata', data / 'appdata' / app, uid, {gid}, True, bind_root=True)
        for category in ('movies', 'tv', 'music', 'downloads'):
            inspect('media:' + category, (data if category == 'music' else media) / 'library' / category, uid, {gid}, True)
        inspect('media:music-downloads', data / 'library/downloads/music', uid, {gid}, True)
    if 'samba' in modules:
        from modules.samba import samba_account
        user = samba_account(config.get('networking', {}))
        try:
            record = pwd.getpwnam(user)
            groups = set(os.getgrouplist(user, record.pw_gid))
        except (KeyError, OSError):
            rows.append({'id':'permission:samba:user','name':'Samba account access','status':'needs_setup','detail':'Configure the intended non-root Samba account and verify authenticated client access.'})
        else:
            settings = config['networking']
            from modules.samba import wanted_shares
            for name, relative in wanted_shares(config).items():
                inspect('samba:' + name, media / relative, record.pw_uid, groups,
                        name == 'Media' and settings.get('samba_media_write', False))
    return rows
