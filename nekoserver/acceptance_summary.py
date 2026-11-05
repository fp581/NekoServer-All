"""The fingerprint of the settings an acceptance run covers: setup writes it into the run
and into the dashboard's settings, and the dashboard (collect.rs acceptance) compares the
two. How a run reads on the dashboard is the collector's to say; this kept a copy of
that until 0.47.2."""

import hashlib, json

from neko_common import LOCATIONS, location

# The photo and USB-copy settings whose change makes an earlier acceptance run outdated:
# a different USB drive or archive folder was never checked by it.
PHOTO_SCOPE = ('external_backup_uuid', 'external_backup_fstype', 'archive_dir', 'backup_weekday', 'backup_time')


def photo_scope(config):
    photos = config.get('photos')
    photos = photos if isinstance(photos, dict) else {}
    return {'deploy_immich': photos.get('deploy_immich') is True, **{k: photos.get(k) for k in PHOTO_SCOPE}}


def data_scope(config):
    """Where the data is beyond the drives themselves: an app's files moved to the other
    drive (`locations`: photos, music or Nextcloud's files on the HDD instead of the SSD,
    say), or the USB copy on another NAS disk. A restore that was witnessed before such a
    move did not restore from there (the review of 0.47.2: moving the photos kept an
    earlier restore's pass). Only what differs from the defaults is listed, so a server
    that never moved anything keeps its fingerprints, and the tests recorded under them."""
    out = {}
    moved = {}
    for what, (default, _) in LOCATIONS.items():
        try:
            chosen = location(config, what)
        except RuntimeError:
            chosen = str((config.get('locations') or {}).get(what))
        if chosen != default:
            moved[what] = chosen
    if moved:
        out['locations'] = moved
    network = (config.get('photos') or {}).get('external_backup_network')
    wwn = network.get('wwn') if isinstance(network, dict) else None
    if wwn:
        out['usb_disk'] = wwn
    return out


def with_data_scope(values, config):
    """`values` with data_scope added when there is one (see data_scope)."""
    extra = data_scope(config)
    return {**values, 'data_scope': extra} if extra else values


def scope_digest(config):
    supplied = config.get('acceptance_scope')
    if isinstance(supplied, str) and len(supplied) == 64 and all(c in '0123456789abcdef' for c in supplied):
        return supplied
    values = {k: config.get(k) for k in ('data_mount', 'media_mount', 'external_backup_mount', 'storage')}
    values['review_settings'] = {
        k: config.get(k)
        for k in (
            'server_os',
            'heartbeat',
            'host_backup',
            'tv',
            'networking',
            'dashboard',
            'monitoring',
            'app_backup',
            'nextcloud',
            'maintenance',
        )
    }
    values['runtime'] = {'backend': config.get('runtime', {}).get('backend')}
    values['gateway'] = {k: config.get('gateway', {}).get(k) for k in ('services', 'lan_ip', 'lan_cidr')}
    values['services'] = config.get('media', {}).get('services', [])
    values['photos'] = photo_scope(config)
    values = with_data_scope(values, config)
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
