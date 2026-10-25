"""Authenticated library setup. No account creation, resets or secret persistence."""
import getpass
import account_store
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from neko_common import VERSION, app_ports, atomic_write, local_opener, mount_path, safe_path

PORTS = app_ports(('jellyfin', 'seerr', 'sonarr', 'radarr'))
LIBRARIES = (('movies', 'Movies', 'movies'), ('tv', 'TV Shows', 'tvshows'))


class SetupRequired(RuntimeError):
    pass


def jellyfin_headers(app, headers):
    """Jellyfin 12 no longer takes a key or session token from the X-Emby-Token
    header alone (401): it wants it in the MediaBrowser Authorization header, as
    Token="...". Other apps' headers are returned as they are."""
    if app != 'jellyfin' or not headers.get('X-Emby-Token'):
        return headers
    sent = dict(headers)
    authorization = sent.get('Authorization', 'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="nekoserver-installer"')
    if 'Token=' not in authorization:
        sent['Authorization'] = authorization + ', Token="' + sent['X-Emby-Token'] + '"'
    return sent


class Client:
    def __init__(self, app):
        self.app = app
        self.base = f'http://127.0.0.1:{PORTS[app]}'
        self.headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if app == 'jellyfin':
            self.headers['Authorization'] = f'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="nekoserver-installer", Version="{VERSION}"'
        # Local credentials must never follow redirects or environment proxies.
        self.opener = local_opener()

    def call(self, endpoint, method='GET', payload=None):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise ValueError('Invalid local endpoint')
        prefix = {'seerr':'/api/v1','sonarr':'/api/v3','radarr':'/api/v3'}.get(self.app,'')
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + prefix + endpoint, data=body, method=method, headers=jellyfin_headers(self.app, self.headers))
        try:
            with self.opener.open(req, timeout=12) as res:
                data = res.read(3_000_001)
                if len(data) > 3_000_000:
                    raise RuntimeError('API response too large')
                return json.loads(data) if data.strip() else {}
        except urllib.error.HTTPError as exc:
            code=exc.code;exc.close()
            raise RuntimeError(f'{self.app}: HTTP {code}') from None
        except (OSError, ValueError):
            raise RuntimeError(f'{self.app}: unavailable or incompatible API response') from None

    def close(self):
        if self.app == 'jellyfin' and self.headers.get('X-Emby-Token'):
            try:
                self.call('/Sessions/Logout', 'POST')
            except RuntimeError:
                pass
        self.headers.clear()


def row(check, status, reason=None):
    result = {'check': check, 'status': status}
    if reason:
        result['reason'] = reason
    return result


def folders(client):
    values = client.call('/Library/VirtualFolders')
    if not isinstance(values, list) or any(not isinstance(x, dict) or not isinstance(x.get('Locations'), list) for x in values):
        raise RuntimeError('Unsupported Jellyfin library response')
    return values


def matching(values, category, name, kind):
    path = '/data/' + category
    matches = [x for x in values if path in [str(p).rstrip('/') for p in x['Locations']]]
    if len(matches) > 1 or any(x.get('CollectionType') != kind for x in matches):
        raise SetupRequired('Existing libraries conflict with the expected path/type; review them in Jellyfin')
    if matches:
        if not matches[0].get('ItemId'):
            raise RuntimeError('Jellyfin library ID missing')
        return matches[0]
    if any(str(x.get('Name', '')).casefold() == name.casefold() for x in values):
        raise SetupRequired('Existing library name uses a different path; custom settings preserved')
    return None


def sign_in(client, user, secret):
    """Sign the client in as a Jellyfin administrator (the password is not kept)."""
    if not secret:
        raise SetupRequired('A nonempty administrator password is required')
    try:
        auth = client.call('/Users/AuthenticateByName', 'POST', {'Username': user, 'Pw': secret})
    except RuntimeError as exc:
        if str(exc) != 'jellyfin: HTTP 401':
            raise
        # A typing mistake: something to do again, not a broken Jellyfin.
        raise SetupRequired('Jellyfin did not accept that administrator username and password; '
                            'run  sudo bash install.sh  again and type them as you set them in Jellyfin') from None
    if not isinstance(auth, dict) or not isinstance(auth.get('AccessToken'), str) or not auth['AccessToken']:
        raise SetupRequired('Jellyfin administrator authentication was not verified')
    client.headers['X-Emby-Token'] = auth['AccessToken']
    if 'Authorization' in client.headers:
        client.headers['Authorization'] += ', Token="' + auth['AccessToken'] + '"'
    user_info = auth.get('User')
    policy = user_info.get('Policy') if isinstance(user_info, dict) else None
    if not isinstance(policy, dict) or policy.get('IsAdministrator') is not True:
        raise SetupRequired('Jellyfin administrator authentication was not verified')


def jellyfin(client, media, apply, username, password):
    info = client.call('/System/Info/Public')
    if not isinstance(info, dict) or not info.get('Id'):
        raise RuntimeError('Unsupported Jellyfin system response')
    if info.get('StartupWizardCompleted') is not True:
        raise SetupRequired('Complete the first administrator wizard at http://127.0.0.1:8096, then rerun --libraries')
    user = username('Jellyfin administrator username (blank to skip): ').strip()
    if not user:
        raise SetupRequired('Administrator authentication skipped')
    sign_in(client, user, password('Jellyfin administrator password: '))
    results = [row('jellyfin:administrator', 'pass')]
    ids = []
    for category, name, kind in LIBRARIES:
        try:
            host = safe_path(Path(media) / 'library' / category)
            if not host.is_dir():
                raise SetupRequired('Expected HDD library folder is missing')
            existing = matching(folders(client), category, name, kind)
            if not existing and not apply:
                results.append(row('jellyfin:' + category, 'needs_setup', 'Preview: library would be created'))
                continue
            if not existing:
                query = urllib.parse.urlencode({'name': name, 'collectionType': kind, 'paths': '/data/' + category, 'refreshLibrary': 'true'})
                client.call('/Library/VirtualFolders?' + query, 'POST', {})
                existing = matching(folders(client), category, name, kind)
                if not existing:
                    raise RuntimeError('Library creation was not retained')
            ids.append(str(existing['ItemId']))
            results.append(row('jellyfin:' + category, 'pass'))
        except SetupRequired as exc:
            results.append(row('jellyfin:' + category, 'needs_setup', str(exc)))
        except (RuntimeError, ValueError, TypeError, KeyError):
            results.append(row('jellyfin:' + category, 'fail', 'Library API/path check failed; existing settings preserved'))
    return results, info['Id'], ids


def seerr_key(root):
    # Read the app's key, never write its settings file or duplicate it in logs.
    path = safe_path(Path(root) / 'appdata' / 'seerr' / 'settings.json')
    if not path.is_file():
        raise SetupRequired('Complete Seerr login/server selection, then rerun --libraries')
    try:
        key = json.loads(path.read_text()).get('main', {}).get('apiKey')
    except (ValueError, OSError, AttributeError):
        raise SetupRequired('Seerr settings could not be read; complete setup in its web interface') from None
    if not isinstance(key, str) or not key:
        raise SetupRequired('Seerr API key is unavailable; complete first-run setup')
    return key


def open_seerr(client, root, unfinished):
    """Sign the client in to Seerr; SetupRequired(unfinished) until its first-run
    wizard is done."""
    client.headers['X-Api-Key'] = seerr_key(root)
    public = client.call('/settings/public')
    if not isinstance(public, dict) or public.get('initialized') is not True:
        raise SetupRequired(unfinished)


def seerr(client, root, server_id, library_ids, apply):
    client.headers['X-Api-Key'] = seerr_key(root)
    settings = client.call('/settings/jellyfin')
    if not isinstance(settings, dict) or settings.get('serverId', settings.get('serverID')) != server_id:
        raise SetupRequired('Seerr is not linked to this Jellyfin server; custom server selection preserved')
    if len(library_ids) != len(LIBRARIES):
        raise SetupRequired('Both Jellyfin movie/TV libraries must be verified before enabling them in Seerr')
    results = [row('seerr:jellyfin_link', 'pass')]
    if apply:
        client.call('/settings/jellyfin/library/sync', 'POST')
    values = client.call('/settings/jellyfin/library')
    if not isinstance(values, list):
        raise RuntimeError('Unsupported Seerr library response')
    for lib_id in library_ids:
        matches = [x for x in values if isinstance(x, dict) and str(x.get('id')) == lib_id]
        if len(matches) != 1:
            results.append(row('seerr:library:' + lib_id, 'needs_setup', 'Expected Jellyfin library not discovered'))
            continue
        if matches[0].get('enabled') is not True and apply:
            client.call('/settings/jellyfin/library/' + urllib.parse.quote(lib_id, safe=''), 'PUT', {'enabled': True})
            saved = client.call('/settings/jellyfin/library')
            if not isinstance(saved, list) or not any(isinstance(x, dict) and str(x.get('id')) == lib_id and x.get('enabled') is True for x in saved):
                raise RuntimeError('Seerr library setting was not retained')
            results.append(row('seerr:library:' + lib_id, 'pass'))
        else:
            results.append(row('seerr:library:' + lib_id, 'pass' if matches[0].get('enabled') is True else 'needs_setup'))
    results.append(row('seerr:request_providers', 'needs_setup', 'Run --request-providers and verify a complete request/import/playback on the server'))
    return results


READINESS_APP = 'NekoServer readiness'


def readiness_key_path(root):
    """Private (root-only) file holding Jellyfin's API key for NekoServer's own checks."""
    return safe_path(mount_path(str(root)) / 'secrets/jellyfin-readiness.key')


def ensure_readiness_key(client, root, log=print):
    """While signed in as administrator: make sure the key kept for NekoServer's read-only
    library check is one Jellyfin still accepts, so the check does not depend on Seerr
    (which may not be installed). A kept key that was revoked or replaced (it is no longer
    among Jellyfin's active NekoServer keys) is renewed; one still active is kept as is.
    Only setup does this: scheduled checks report a rejected key and never change it."""
    path = readiness_key_path(root)
    try:
        kept = path.read_text().strip()
    except OSError:
        kept = ''

    def active():
        keys = client.call('/Auth/Keys')
        items = keys.get('Items', []) if isinstance(keys, dict) else []
        return [k['AccessToken'] for k in items if isinstance(k, dict)
                and k.get('AppName') == READINESS_APP and isinstance(k.get('AccessToken'), str) and k['AccessToken']]
    tokens = active()
    if kept and kept in tokens:
        return
    if not tokens:
        client.call('/Auth/Keys?' + urllib.parse.urlencode({'app': READINESS_APP}), 'POST', {})
        tokens = active()
    if not tokens:
        raise RuntimeError('Jellyfin did not create the readiness key')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(path, tokens[0] + '\n', 0o600)
    if kept:
        log('Jellyfin: NekoServer\'s key was revoked or replaced in Jellyfin; saved its current key')


def configure(root, media, apply=False, apps=('jellyfin', 'seerr'), factory=Client, username=input, password=getpass.getpass):
    if 'jellyfin' not in apps:
        return [row('media_libraries', 'needs_setup', 'Jellyfin is required for this library workflow')]
    stored = account_store.get({'data_mount': str(root)}, 'jellyfin', verified=True)
    if stored:
        username = lambda _: stored['username']
        password = lambda _: stored['password']
    jf = factory('jellyfin')
    try:
        try:
            results, server_id, ids = jellyfin(jf, media, apply, username, password)
            if apply:
                ensure_readiness_key(jf, root)
        except SetupRequired as exc:
            return [row('jellyfin:setup', 'needs_setup', str(exc))]
        except (RuntimeError, ValueError, TypeError, KeyError, EOFError):
            return [row('jellyfin:setup', 'fail', 'Authenticated Jellyfin setup failed; rerun after checking the service')]
        if 'seerr' in apps:
            sr = factory('seerr')
            try:
                try:
                    results += seerr(sr, root, server_id, ids, apply)
                except SetupRequired as exc:
                    results.append(row('seerr:setup', 'needs_setup', str(exc)))
                except (RuntimeError, ValueError, TypeError, KeyError):
                    results.append(row('seerr:setup', 'fail', 'Seerr authenticated link/library API checks failed'))
            finally:
                sr.close()
        return results
    finally:
        jf.close()
