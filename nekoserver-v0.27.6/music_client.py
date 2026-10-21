"""Ad hoc authenticated Navidrome setup; periodic monitoring is native Rust."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import urllib.parse
import urllib.request

from neko_common import app_port, safe_path, atomic_json, local_opener

FORMAT = 'neko-music-client-v1'


def credential_path(config):
    return safe_path(Path(config['data_mount'])/'appdata/navidrome/nekoserver-client.json')


def credential(user, password):
    if not isinstance(user, str) or not user.strip() or len(user) > 128 or any(ord(c) < 32 for c in user):
        raise RuntimeError('Invalid music listener username')
    if not password:
        raise RuntimeError('A listener password is required')
    salt = secrets.token_hex(16)
    # Token+salt MD5 is required by the Subsonic protocol, not a password-storage scheme.
    token = hashlib.md5((password+salt).encode(), usedforsecurity=False).hexdigest()
    return {'format': FORMAT, 'user': user, 'salt': salt, 'token': token}


def validate(value):
    if not isinstance(value, dict) or value.get('format') != FORMAT:
        raise RuntimeError('Unknown music credential format')
    user = value.get('user')
    if not isinstance(user, str) or not user.strip() or len(user) > 128 or any(ord(c) < 32 for c in user):
        raise RuntimeError('Invalid listener identity')
    if any(not isinstance(value.get(k), str) or not re.fullmatch(r'[a-f0-9]{32}', value[k]) for k in ('salt', 'token')):
        raise RuntimeError('Invalid private music token')


NAVIDROME = f"http://127.0.0.1:{app_port('navidrome')}"


def inspect(value, base=NAVIDROME):
    validate(value)
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', base):
        raise RuntimeError('Music setup only contacts the local server')
    opener = local_opener()
    def call(endpoint, **extras):
        params = {'u': value['user'], 't': value['token'], 's': value['salt'], 'v': '1.16.1', 'c': 'NekoServer', 'f': 'json', **extras}
        url = base+'/rest/'+endpoint+'.view?'+urllib.parse.urlencode(params)
        try:
            with opener.open(url, timeout=8) as response:
                data = response.read(64001)
            if len(data) > 64000:
                raise RuntimeError('Music response exceeds the expected size')
            result = json.loads(data).get('subsonic-response', {})
            if result.get('status') != 'ok':
                raise RuntimeError('Music authentication or API check failed')
            return result
        except (OSError, ValueError, AttributeError):
            raise RuntimeError('Local music API unavailable; check Navidrome and the listener account') from None
    call('ping')
    def mapping(value):
        if not isinstance(value,dict):raise RuntimeError('Unexpected music response structure')
        return value
    user = mapping(call('getUser', username=value['user']).get('user', {}))
    if user.get('username') != value['user'] or user.get('adminRole') is not False:
        raise RuntimeError('Create a non-administrator listener account for clients and checks')
    folders = mapping(call('getMusicFolders').get('musicFolders', {})).get('musicFolder', [])
    if not isinstance(folders, list) or not folders:
        raise RuntimeError('Navidrome has no configured music library')
    albums = mapping(call('getAlbumList2', type='newest', size=1).get('albumList2', {})).get('album', [])
    if not isinstance(albums, list):
        raise RuntimeError('Unexpected music catalogue response')
    return {'status': 'pass', 'authenticated': True, 'non_admin_listener': True, 'library_configured': True, 'library_empty': not albums, 'playback_verified': False}


def check(config):
    path = credential_path(config)
    if not path.exists():
        return {'status': 'needs_setup', 'reason': 'Create a non-admin Navidrome listener and run music-setup.py --configure'}
    try:
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o077 or path.stat().st_size>64000:
            raise RuntimeError('Music monitor credential must be private and root-owned')
        result = inspect(json.loads(path.read_text()))
    except (OSError, ValueError, RuntimeError):
        return {'status': 'fail', 'reason': 'Authenticated music catalogue check failed; inspect the local service/account'}
    return result


def save(config, value):
    result = inspect(value)
    atomic_json(credential_path(config), value)
    return result
