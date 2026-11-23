"""Authenticated library setup. No account creation, resets or secret persistence."""

import getpass
import account_store
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from neko_common import (
    NETWORK_FILESYSTEMS,
    VERSION,
    app_ports,
    atomic_write,
    findmnt,
    local_opener,
    mount_path,
    safe_path,
)

PORTS = app_ports(('jellyfin', 'seerr', 'sonarr', 'radarr'))
LIBRARIES = (('movies', 'Movies', 'movies'), ('tv', 'TV Shows', 'tvshows'))


class SetupRequired(RuntimeError):
    pass


# Seerr knows one Jellyfin by its ID; setup never changes that choice. A Jellyfin whose
# settings came back from a backup has the old server's ID, a Seerr made since a new one
# (T1-R7, after a rebuild): the two match again once their settings are from the same night.
SEERR_OTHER_JELLYFIN = (
    'Seerr is linked to another Jellyfin server than this one, which setup leaves as it is. '
    "After putting app settings back from a backup, put Seerr's, Sonarr's and Radarr's back too, from the same "
    "night as Jellyfin's "
    '(APP-BACKUPS.md, step 2), then run setup again'
)


def jellyfin_headers(app, headers):
    """Jellyfin 12 no longer takes a key or session token from the X-Emby-Token
    header alone (401): it wants it in the MediaBrowser Authorization header, as
    Token="...". Other apps' headers are returned as they are."""
    if app != 'jellyfin' or not headers.get('X-Emby-Token'):
        return headers
    sent = dict(headers)
    authorization = sent.get(
        'Authorization', 'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="nekoserver-installer"'
    )
    if 'Token=' not in authorization:
        sent['Authorization'] = authorization + ', Token="' + sent['X-Emby-Token'] + '"'
    return sent


class Client:
    def __init__(self, app):
        self.app = app
        self.base = f'http://127.0.0.1:{PORTS[app]}'
        self.headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if app == 'jellyfin':
            self.headers['Authorization'] = (
                f'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="nekoserver-installer", Version="{VERSION}"'
            )
        # Local credentials must never follow redirects or environment proxies.
        self.opener = local_opener()

    def call(self, endpoint, method='GET', payload=None):
        if not endpoint.startswith('/') or endpoint.startswith('//'):
            raise ValueError('Invalid local endpoint')
        prefix = {'seerr': '/api/v1', 'sonarr': '/api/v3', 'radarr': '/api/v3'}.get(self.app, '')
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + prefix + endpoint, data=body, method=method, headers=jellyfin_headers(self.app, self.headers)
        )
        try:
            with self.opener.open(req, timeout=12) as res:
                data = res.read(3_000_001)
                if len(data) > 3_000_000:
                    raise RuntimeError('API response too large')
                return json.loads(data) if data.strip() else {}
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
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
    if not isinstance(values, list) or any(
        not isinstance(x, dict) or not isinstance(x.get('Locations'), list) for x in values
    ):
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


# Jellyfin sees a new file at once only with its library's "real time monitoring" on. A
# library made through Jellyfin's API has it off (LibraryOptions' own default, unlike
# Jellyfin's Add Library page), and its library scan runs every 12 hours: a film Radarr
# had put in the library stayed out of Jellyfin, and "requested" in Seerr, for up to half
# a day (deep check 691). Libraries setup makes get it on; one made by an earlier setup
# is switched on (said in the log).
WATCHED = {'EnableRealtimeMonitor': True}

# Real time monitoring waits for the kernel's notices of new files, which a network share
# (NETWORK_FILESYSTEMS) does not give for files another computer puts on the NAS
# (Jellyfin's own troubleshooting page says so); review of 0.51.3: setup promised "as soon
# as they arrive" for a library on an NFS share all the same.
# How often Jellyfin's "Scan Media Library" runs when a library is on a share (Jellyfin's
# own is every 12 hours), in its ticks of 100 ns.
HOURLY_TICKS = 60 * 60 * 10_000_000


def on_a_share(path):
    """Whether the folder at `path` (on this PC) is on a network share."""
    return (findmnt('FSTYPE', str(path), '--target') or '') in NETWORK_FILESYSTEMS


def scan_every_hour(client, log=print):
    """On a server with a library on a share: Jellyfin's library scan every hour, unless it
    already runs at least as often. Files put on the NAS from another computer then appear
    within the hour instead of within 12. Jellyfin's other triggers for that task (a daily
    time, say) are kept."""
    tasks = client.call('/ScheduledTasks')
    task = next((x for x in tasks or [] if isinstance(x, dict) and x.get('Key') == 'RefreshLibrary'), None)
    if not task or not task.get('Id'):
        return
    triggers = [x for x in task.get('Triggers') or [] if isinstance(x, dict)]
    intervals = [x.get('IntervalTicks') for x in triggers if x.get('Type') == 'IntervalTrigger']
    if any(isinstance(ticks, int) and 0 < ticks <= HOURLY_TICKS for ticks in intervals):
        return
    kept = [x for x in triggers if x.get('Type') != 'IntervalTrigger']
    client.call(
        f"/ScheduledTasks/{task['Id']}/Triggers",
        'POST',
        kept + [{'Type': 'IntervalTrigger', 'IntervalTicks': HOURLY_TICKS}],
    )
    log(
        'jellyfin: the library scan now runs every hour (it ran every 12 hours), because a library is on a network '
        'share: a share does not tell Jellyfin of a file put on the NAS from another computer'
    )


def new_library_options():
    """The body of Jellyfin's "add a library" call: real time monitoring on."""
    return {'LibraryOptions': dict(WATCHED)}


# How long Jellyfin may take to add a library while it is busy (below).
ADDING_WAIT = 120


def added(client, category, name, kind):
    """The library once Jellyfin has added it, else None. Listed while still being added,
    it has no ID yet ("Jellyfin library ID missing", seen with Jellyfin 12.4)."""
    try:
        return matching(folders(client), category, name, kind)
    except RuntimeError:
        return None


def add_library(client, category, name, kind):
    """Add the library and return it as Jellyfin lists it. While Jellyfin scans the
    library made just before (many films), adding the next one can take longer than a
    call is waited for (12 seconds; seen with Jellyfin 12.4 and 40 films, lab/real_apps.py).
    Jellyfin goes on adding it, so its list of libraries is looked at until it has it."""
    query = urllib.parse.urlencode(
        {'name': name, 'collectionType': kind, 'paths': '/data/' + category, 'refreshLibrary': 'true'}
    )
    try:
        client.call('/Library/VirtualFolders?' + query, 'POST', new_library_options())
    except RuntimeError as exc:
        if str(exc) != 'jellyfin: unavailable or incompatible API response':
            raise
        deadline = time.monotonic() + ADDING_WAIT
        while not added(client, category, name, kind):
            if time.monotonic() >= deadline:
                raise RuntimeError('Library creation was not retained') from None
            time.sleep(5)
    existing = matching(folders(client), category, name, kind)
    if not existing:
        raise RuntimeError('Library creation was not retained')
    return existing


def scan_new_library(client, library, log=print):
    """Jellyfin looks through a library setup has just made, so the files already in it
    are listed now. Adding a library starts a library scan only when none is running
    (Jellyfin 12.4, LibraryManager.StartScanInBackground: "an active scan already
    handles library structure changes"); but a scan already running, of the library
    made just before or Jellyfin's own, read the list of libraries before this one was
    added and passes it over until the next scheduled scan, 12 hours later. A refresh of
    the library itself (POST /Items/{id}/Refresh) goes in Jellyfin's refresh queue, apart
    from the scan, and looks through its folder (ProviderManager.RefreshCollectionFolderChildren)."""
    query = urllib.parse.urlencode(
        {
            'metadataRefreshMode': 'Default',
            'imageRefreshMode': 'Default',
            'replaceAllMetadata': 'false',
            'replaceAllImages': 'false',
        }
    )
    try:
        client.call(f'/Items/{urllib.parse.quote(str(library["ItemId"]))}/Refresh?' + query, 'POST')
    except RuntimeError:
        log(
            f'jellyfin: the new library "{library.get("Name", "")}" is listed at Jellyfin\'s next library scan; '
            'to see its files now, scan it in Jellyfin (Dashboard, Libraries)'
        )


def watch_library(client, library, log=print, on_share=False):
    """Switch real time monitoring on in a library setup made, when Jellyfin says it is
    off. A library whose options Jellyfin does not give is left as it is. `on_share`: the
    library's folder is on a network share, where it can't be counted on (scan_every_hour)."""
    options = library.get('LibraryOptions')
    if not isinstance(options, dict) or options.get('EnableRealtimeMonitor') is not False:
        return
    payload = {'Id': library['ItemId'], 'LibraryOptions': {**options, **WATCHED}}
    client.call('/Library/VirtualFolders/LibraryOptions', 'POST', payload)
    said = (
        'it is on a network share, so a new film or episode may appear only at the next library scan (every hour)'
        if on_share
        else 'new films and episodes now appear as soon as they arrive, not at the next scan (every 12 hours)'
    )
    log(f'jellyfin: real time monitoring switched on for the library "{library.get("Name", "")}": {said}')


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
        raise SetupRequired(
            'Jellyfin did not accept that administrator username and password; '
            'run  sudo bash install.sh  again and type them as you set them in Jellyfin'
        ) from None
    if not isinstance(auth, dict) or not isinstance(auth.get('AccessToken'), str) or not auth['AccessToken']:
        raise SetupRequired('Jellyfin administrator authentication was not verified')
    client.headers['X-Emby-Token'] = auth['AccessToken']
    if 'Authorization' in client.headers:
        client.headers['Authorization'] += ', Token="' + auth['AccessToken'] + '"'
    user_info = auth.get('User')
    policy = user_info.get('Policy') if isinstance(user_info, dict) else None
    if not isinstance(policy, dict) or policy.get('IsAdministrator') is not True:
        raise SetupRequired('Jellyfin administrator authentication was not verified')


JELLYFIN_ROW = 'jellyfin:'  # a Jellyfin library's row: jellyfin:movies, jellyfin:tv
SEERR_LIBRARY_ROW = 'seerr:library:'  # a library's row in Seerr, by its Jellyfin id


def jellyfin_library(client, media, apply, category, name, kind):
    """One of Jellyfin's libraries (films or shows), found or made on its folder of the
    media drive: (its row, its id once it is there, whether it is on a network share)."""
    try:
        host = safe_path(Path(media) / 'library' / category)
        if '\x00' in str(host):  # names no folder (realpath would raise on it)
            raise SetupRequired('Expected HDD library folder is missing')
        # The settings name the media drive: the folder is looked at only by its real path,
        # and only when that is the path asked for (safe_path refused links on the way).
        real = os.path.realpath(host)
        if not real.startswith(os.sep) or real != os.path.abspath(host) or not os.path.isdir(real):
            raise SetupRequired('Expected HDD library folder is missing')
        on_share = on_a_share(Path(real))
    except SetupRequired as exc:
        return row(JELLYFIN_ROW + category, 'needs_setup', str(exc)), None, False
    except (RuntimeError, ValueError, TypeError, KeyError):
        return row(JELLYFIN_ROW + category, 'fail', LIBRARY_CHECK_FAILED), None, False
    try:
        existing = matching(folders(client), category, name, kind)
        if not existing and not apply:
            return row(JELLYFIN_ROW + category, 'needs_setup', 'Preview: library would be created'), None, on_share
        if not existing:
            existing = add_library(client, category, name, kind)
            scan_new_library(client, existing)
        if apply:
            watch_library(client, existing, on_share=on_share)
        return row(JELLYFIN_ROW + category, 'pass'), str(existing['ItemId']), on_share
    except SetupRequired as exc:
        return row(JELLYFIN_ROW + category, 'needs_setup', str(exc)), None, on_share
    except (RuntimeError, ValueError, TypeError, KeyError):
        return row(JELLYFIN_ROW + category, 'fail', LIBRARY_CHECK_FAILED), None, on_share


LIBRARY_CHECK_FAILED = 'Library API/path check failed; existing settings preserved'


def jellyfin(client, media, apply, username, password):
    info = client.call('/System/Info/Public')
    if not isinstance(info, dict) or not info.get('Id'):
        raise RuntimeError('Unsupported Jellyfin system response')
    if info.get('StartupWizardCompleted') is not True:
        raise SetupRequired('Complete the first administrator wizard at http://127.0.0.1:8096, then rerun --libraries')
    user = username('Jellyfin administrator username (Enter alone skips): ').strip()
    if not user:
        raise SetupRequired('Administrator authentication skipped')
    sign_in(client, user, password('Jellyfin administrator password: '))
    results = [row('jellyfin:administrator', 'pass')]
    ids = []
    shared = False
    for category, name, kind in LIBRARIES:
        result, library_id, on_share = jellyfin_library(client, media, apply, category, name, kind)
        results.append(result)
        if library_id is not None:
            ids.append(library_id)
        shared = shared or on_share
    if apply and shared:
        try:
            scan_every_hour(client)
        except (RuntimeError, ValueError, TypeError, KeyError):
            results.append(row('jellyfin:scan', 'needs_setup', SCAN_NOT_SET))
    return results, info['Id'], ids


SCAN_NOT_SET = (
    'A library is on a network share, where Jellyfin is not told of new files, and its library scan could not be '
    'set to every hour. In Jellyfin open Dashboard, Scheduled tasks, Scan media library and add a trigger every hour'
)


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


def seerr(client, root, server_id, library_ids, apply, apps=('sonarr', 'radarr')):
    client.headers['X-Api-Key'] = seerr_key(root)
    settings = client.call('/settings/jellyfin')
    if not isinstance(settings, dict) or settings.get('serverId', settings.get('serverID')) != server_id:
        raise SetupRequired(SEERR_OTHER_JELLYFIN)
    if len(library_ids) != len(LIBRARIES):
        raise SetupRequired('Both Jellyfin movie/TV libraries must be verified before enabling them in Seerr')
    results = [row('seerr:jellyfin_link', 'pass')]
    if apply:
        client.call('/settings/jellyfin/library/sync', 'POST')
    values = client.call('/settings/jellyfin/library')
    if not isinstance(values, list):
        raise RuntimeError('Unsupported Seerr library response')
    results += [seerr_library(client, values, lib_id, apply) for lib_id in library_ids]
    results.append(request_providers_row(apps))
    return results


def seerr_library(client, values, lib_id, apply):
    """The row for one Jellyfin library in Seerr (`values`: Seerr's list of them), switched
    on there with --apply."""
    matches = [x for x in values if isinstance(x, dict) and str(x.get('id')) == lib_id]
    if len(matches) != 1:
        return row(SEERR_LIBRARY_ROW + lib_id, 'needs_setup', 'Expected Jellyfin library not discovered')
    if matches[0].get('enabled') is True or not apply:
        return row(SEERR_LIBRARY_ROW + lib_id, 'pass' if matches[0].get('enabled') is True else 'needs_setup')
    client.call('/settings/jellyfin/library/' + urllib.parse.quote(lib_id, safe=''), 'PUT', {'enabled': True})
    saved = client.call('/settings/jellyfin/library')
    if not isinstance(saved, list) or not any(
        isinstance(x, dict) and str(x.get('id')) == lib_id and x.get('enabled') is True for x in saved
    ):
        raise RuntimeError('Seerr library setting was not retained')
    return row(SEERR_LIBRARY_ROW + lib_id, 'pass')


REQUEST_PROVIDERS = 'seerr:request_providers'
NOT_NEEDED = 'Not needed: without Sonarr or Radarr, Seerr has no app to send requests to'


def request_providers_row(apps):
    """Seerr sends requests to Sonarr and Radarr. Without either there is nothing to
    connect (both can be removed and Seerr kept), so the row is not a step left to do."""
    if not set(apps) & {'sonarr', 'radarr'}:
        return row(REQUEST_PROVIDERS, 'pass', NOT_NEEDED)
    return row(
        REQUEST_PROVIDERS,
        'needs_setup',
        'Run --request-providers and verify a complete request/import/playback on the server',
    )


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
        return [
            k['AccessToken']
            for k in items
            if isinstance(k, dict)
            and k.get('AppName') == READINESS_APP
            and isinstance(k.get('AccessToken'), str)
            and k['AccessToken']
        ]

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


def search_and_request_rows(jellyfin, root, apply, wanted):
    """Jellyfin Enhanced's rows (jellyfin_requests.py): a step left for later or one that
    failed is said with why; never one that stops the rest of setup."""
    import jellyfin_requests

    try:
        return jellyfin_requests.connect(jellyfin, root, apply, wanted)
    except SetupRequired as exc:
        return [row(jellyfin_requests.CHECK, 'needs_setup', str(exc))]
    except (RuntimeError, ValueError, TypeError, KeyError) as exc:
        return [row(jellyfin_requests.CHECK, 'fail', f'Jellyfin Enhanced could not be set up: {exc}')]


def configure(
    root,
    media,
    apply=False,
    apps=('jellyfin', 'seerr'),
    factory=Client,
    username=input,
    password=getpass.getpass,
    more=None,
    search_and_request=True,
):
    """`more`: called with Jellyfin's signed-in client for further libraries (the
    person's own folders, existing_page.libraries); its rows are added to the results.
    `search_and_request`: Jellyfin Enhanced set up and connected to Seerr once Seerr is
    linked (jellyfin_requests.py; onboarding.search_and_request_in_jellyfin)."""
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
            if more is not None:
                results += more(jf)
            if apply:
                ensure_readiness_key(jf, root)
        except SetupRequired as exc:
            return [row('jellyfin:setup', 'needs_setup', str(exc))]
        except (RuntimeError, ValueError, TypeError, KeyError, EOFError):
            return [
                row('jellyfin:setup', 'fail', 'Authenticated Jellyfin setup failed; rerun after checking the service')
            ]
        if 'seerr' in apps:
            sr = factory('seerr')
            try:
                try:
                    results += seerr(sr, root, server_id, ids, apply, apps)
                except SetupRequired as exc:
                    results.append(row('seerr:setup', 'needs_setup', str(exc)))
                except (RuntimeError, ValueError, TypeError, KeyError):
                    results.append(row('seerr:setup', 'fail', 'Seerr authenticated link/library API checks failed'))
            finally:
                sr.close()
            results += search_and_request_rows(jf, root, apply, search_and_request)
        return results
    finally:
        jf.close()
