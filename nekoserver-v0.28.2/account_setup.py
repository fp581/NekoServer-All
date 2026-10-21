"""One-time account provisioning over fixed loopback APIs; no runtime worker."""
import base64
import http.cookiejar
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import account_store as store
from neko_common import app_port, local_opener, safe_path

ARR = {app: (version, app_port(app)) for app, version in
       (('sonarr', 'v3'), ('radarr', 'v3'), ('lidarr', 'v1'), ('prowlarr', 'v1'))}
HEALTH = {'jellyfin': '/System/Info/Public', 'navidrome': '/ping',
          'qbittorrent': '/api/v2/app/version', 'immich': '/api/server/ping',
          'seerr': '/api/v1/status', 'nextcloud': '/status.php'}


class HTTPFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Local account API request failed (HTTP {code}); credentials were not logged')


class Client:
    def __init__(self, app, port=None):
        self.base = f'http://127.0.0.1:{port or store.APPS[app][1]}'
        self.headers = {'Accept': 'application/json', 'Referer': self.base}
        if app == 'jellyfin':
            self.headers['Authorization'] = 'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="neko-account-setup", Version="1"'
        self.jar = http.cookiejar.CookieJar()
        self.opener = local_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, endpoint, method='GET', payload=None, form=None, raw=False, headers=None):
        if not endpoint.startswith('/') or endpoint.startswith('//') or '\\' in endpoint:
            raise ValueError('Only fixed local API paths are permitted')
        request_headers = {**self.headers, **(headers or {})}
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif payload is not None:
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            request_headers.setdefault('Content-Type', 'application/json')
        else:
            body = None
        request = urllib.request.Request(self.base + endpoint, data=body, headers=request_headers, method=method)
        try:
            with self.opener.open(request, timeout=12) as response:
                data = response.read(1_000_001)
                if len(data) > 1_000_000:
                    raise RuntimeError('Local account API response exceeds expected size')
                if raw:
                    return data
                return json.loads(data) if data.strip() else {}
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise HTTPFailure(code) from None
        except (OSError, ValueError):
            raise HTTPFailure(0) from None

    def close(self):
        self.jar.clear()
        self.headers.clear()


def docker(args, input_text=None):
    result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                            input=input_text, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError('Local account setup command failed; private output was suppressed')
    return result.stdout + (result.stderr if args and args[0] == 'logs' else '')


def allowed(entry):
    if not entry['allow_create']:
        raise RuntimeError('Existing account not authenticated; no password reset or account replacement attempted')


def attempt(action):
    try:
        return action()
    except HTTPFailure as exc:
        if exc.code not in (400, 401, 403):
            raise
        return None


def check(condition):
    if not condition:
        raise RuntimeError('Authenticated account identity/administrator check did not match')


def wait(app, client, seconds=60):
    endpoint = HEALTH.get(app, '/api/' + ARR.get(app, ('v1', 0))[0] + '/system/status')
    deadline = time.monotonic() + seconds
    while True:
        try:
            client.call(endpoint, raw=True)
            return
        except HTTPFailure as exc:
            if exc.code in (401, 403):
                return
            if exc.code not in (0, 502, 503, 500) or time.monotonic() >= deadline:
                raise
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def jellyfin(client, entry):
    def login():
        result = client.call('/Users/AuthenticateByName', 'POST', {'Username': entry['username'], 'Pw': entry['password']})
        check(result.get('User', {}).get('Name') == entry['username'] and
              result.get('User', {}).get('Policy', {}).get('IsAdministrator') is True and result.get('AccessToken'))
        client.headers['X-Emby-Token'] = result['AccessToken']
        client.headers['Authorization'] = client.headers['Authorization'].split(', Token=')[0] + ', Token="' + result['AccessToken'] + '"'
        return result
    info = client.call('/System/Info/Public')
    result = attempt(login)
    if not result:
        allowed(entry)
        check(info.get('StartupWizardCompleted') is False)
        client.call('/Startup/User')  # Initialize the first user before updating it.
        client.call('/Startup/User', 'POST', {'Name': entry['username'], 'Password': entry['password']})
    if entry['allow_create'] and info.get('StartupWizardCompleted') is False:
        client.call('/Startup/RemoteAccess', 'POST', {'EnableRemoteAccess': False, 'EnableAutomaticPortMapping': False})
        client.call('/Startup/Complete', 'POST')
    login()
    client.call('/Sessions/Logout', 'POST')


def immich(client, entry):
    def login():
        value = client.call('/api/auth/login', 'POST', {'email': entry['username'], 'password': entry['password']})
        check(value.get('isAdmin') is True and value.get('userEmail', '').casefold() == entry['username'].casefold())
        return value
    result = attempt(login)
    if not result:
        allowed(entry)
        check(client.call('/api/server/config').get('isInitialized') is False)
        client.call('/api/auth/admin-sign-up', 'POST', {'email': entry['username'], 'password': entry['password'], 'name': entry['username'].split('@')[0]})
        result = login()
    client.headers['Authorization'] = 'Bearer ' + result['accessToken']
    client.call('/api/auth/logout', 'POST')


def navidrome(client, entry):
    body = {'username': entry['username'], 'password': entry['password']}
    result = attempt(lambda: client.call('/auth/login', 'POST', body))
    if not result:
        allowed(entry)
        client.call('/auth/createAdmin', 'POST', body)  # Upstream refuses if any user exists.
        result = client.call('/auth/login', 'POST', body)
    check(result.get('username') == entry['username'] and result.get('isAdmin') is True and result.get('token'))
    client.headers['X-ND-Authorization'] = 'Bearer ' + result['token']


def listener(client, entry, administrator, admin_entry):
    navidrome(administrator, admin_entry)
    body = {'username': entry['username'], 'password': entry['password']}
    value = attempt(lambda: client.call('/auth/login', 'POST', body))
    if not value:
        allowed(entry)
        users = administrator.call('/api/user/')
        check(isinstance(users, list))
        check(not any(user.get('userName') == entry['username'] for user in users))
        libraries = administrator.call('/api/library/')
        check(isinstance(libraries, list) and libraries)
        administrator.call('/api/user/', 'POST', {'userName': entry['username'], 'name': entry['username'],
                            'password': entry['password'], 'isAdmin': False})
        value = client.call('/auth/login', 'POST', body)
    check(value.get('username') == entry['username'] and value.get('isAdmin') is False and value.get('token') and value.get('id'))
    if entry['allow_create'] and entry['state'] != 'verified':
        libraries = administrator.call('/api/library/')
        check(isinstance(libraries, list) and bool(libraries))
        identity = urllib.parse.quote(str(value['id']), safe='')
        administrator.call('/api/user/' + identity + '/library/', 'PUT', {'libraryIds': [lib['id'] for lib in libraries]})
    return value


def arr(client, entry, config, app):
    key_file = safe_path(Path(config['data_mount']) / 'appdata' / app / 'config.xml')
    try:
        key = ET.parse(key_file).findtext('ApiKey')
    except (OSError, ET.ParseError):
        raise RuntimeError('Media manager API key is not ready') from None
    check(isinstance(key, str) and bool(key.strip()))
    client.headers['X-Api-Key'] = key
    endpoint = '/api/' + ARR[app][0] + '/config/host'
    current = client.call(endpoint)
    check(isinstance(current, dict) and 'authenticationMethod' in current and 'username' in current)
    if not current.get('username') and not current.get('password'):
        allowed(entry)
        current.update(authenticationMethod='forms', authenticationRequired='enabled',
                       username=entry['username'], password=entry['password'], passwordConfirmation=entry['password'])
        client.call(endpoint, 'PUT', current)
    else:
        check(current.get('username') == entry['username'])
    verified = client.call(endpoint)
    check(verified.get('username') == entry['username'] and
          str(verified.get('authenticationMethod', '')).lower() == 'forms' and
          str(verified.get('authenticationRequired', '')).lower() == 'enabled')
    # API-key success is not proof of the selected login password. Use a fresh
    # cookie session without the API key and verify an authenticated API call.
    client.headers.pop('X-Api-Key', None)
    client.jar.clear()
    try:
        client.call('/login', 'POST', form={'username': entry['username'], 'password': entry['password'], 'rememberMe': 'false'}, raw=True)
    except HTTPFailure as exc:
        if exc.code != 302 or not client.jar:
            raise
    client.call('/api/' + ARR[app][0] + '/system/status')
    check(any(cookie.name.endswith('Auth') or 'Auth' in cookie.name for cookie in client.jar))


def qbittorrent(client, entry):
    def login(username, password):
        reply = client.call('/api/v2/auth/login', 'POST', form={'username': username, 'password': password}, raw=True)
        if reply.strip() != b'Ok.' or not any(cookie.name == 'SID' for cookie in client.jar):
            return None
        return True
    if not attempt(lambda: login(entry['username'], entry['password'])):
        allowed(entry)
        ids = docker(['ps', '-q', '--filter', 'label=com.docker.compose.project=neko-qbittorrent',
                      '--filter', 'label=com.docker.compose.service=qbittorrent']).split()
        check(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]))
        logs = docker(['logs', '--tail', '200', ids[0]])
        matches = re.findall(r'temporary password is provided for this session:\s*(\S+)', logs)
        check(bool(matches))
        check(login('admin', matches[-1]))
        preferences = client.call('/api/v2/app/preferences')
        check(preferences.get('web_ui_username', 'admin') == 'admin')
        client.call('/api/v2/app/setPreferences', 'POST', form={'json': json.dumps({
            'web_ui_username': entry['username'], 'web_ui_password': entry['password'],
            'bypass_local_auth': False, 'bypass_auth_subnet_whitelist_enabled': False})}, raw=True)
        client.call('/api/v2/auth/logout', 'POST', form={}, raw=True)
        client.jar.clear()
        check(login(entry['username'], entry['password']))
    preferences = client.call('/api/v2/app/preferences')
    check(preferences.get('web_ui_username') == entry['username'] and
          preferences.get('bypass_local_auth') is False and
          preferences.get('bypass_auth_subnet_whitelist_enabled') is False)
    client.call('/api/v2/auth/logout', 'POST', form={}, raw=True)


def seerr(client, entry, jellyfin_entry):
    login_body = {'email': entry['username'], 'password': entry['password']}
    result = attempt(lambda: client.call('/api/v1/auth/local', 'POST', login_body))
    if not result:
        allowed(entry)
        check(client.call('/api/v1/settings/public').get('initialized') is False)
        check(jellyfin_entry and jellyfin_entry['state'] == 'verified')
        result = client.call('/api/v1/auth/jellyfin', 'POST', {
            'username': jellyfin_entry['username'], 'password': jellyfin_entry['password'],
            'hostname': 'jellyfin', 'port': app_port('jellyfin'), 'useSsl': False, 'urlBase': '',
            'serverType': 2, 'email': entry['username']})
        check(result.get('id') == 1 and result.get('email', '').casefold() == entry['username'].casefold())
        # A partially completed managed setup may already have set the password.
        # Never overwrite a password that does not authenticate with saved choices.
        check(client.call('/api/v1/user/1/settings/password').get('hasPassword') is False)
        client.call('/api/v1/user/1/settings/password', 'POST', {'newPassword': entry['password']})
        client.call('/api/v1/auth/logout', 'POST')
        client.jar.clear()
        result = client.call('/api/v1/auth/local', 'POST', login_body)
    check(result.get('id') == 1 and result.get('email', '').casefold() == entry['username'].casefold())
    if entry['allow_create'] and client.call('/api/v1/settings/public').get('initialized') is False:
        client.call('/api/v1/settings/initialize', 'POST')
    check(client.call('/api/v1/settings/public').get('initialized') is True)
    client.call('/api/v1/auth/logout', 'POST')


def nextcloud(client, entry, config):
    status = client.call('/status.php')
    check(status.get('installed') is True and status.get('maintenance') is False and not status.get('needsDbUpgrade'))
    basic = base64.b64encode((entry['username'] + ':' + entry['password']).encode()).decode()
    path = '/remote.php/dav/files/' + urllib.parse.quote(entry['username'], safe='') + '/'
    client.call(path, 'PROPFIND', b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
                raw=True, headers={'Authorization': 'Basic ' + basic, 'Content-Type': 'application/xml', 'Depth': '0'})
    manifest = safe_path(Path(config['data_mount']) / 'compose/nextcloud/compose.yaml')
    env = safe_path(manifest.parent / '.env')
    info = json.loads(docker(['compose', '-p', 'neko-nextcloud', '-f', str(manifest), '--env-file', str(env),
                            'exec', '-T', '--user', 'www-data', 'nextcloud', 'php', 'occ',
                            'user:info', '--output=json', entry['username']]))
    check(info.get('enabled') is True and 'admin' in info.get('groups', []))


def verify_project(config, app):
    """Prove the local port belongs to this managed app before sending a secret."""
    project = 'navidrome' if app == 'navidrome_listener' else app
    service = 'immich-server' if project == 'immich' else project
    root = Path(config['data_mount'])
    manifest = safe_path(root / 'compose' / project / 'compose.yaml')
    check(manifest.is_file())
    ids = docker(['ps', '-q', '--filter', 'label=com.docker.compose.project=neko-' + project,
                  '--filter', 'label=com.docker.compose.service=' + service]).split()
    check(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]))
    values = json.loads(docker(['inspect', ids[0]]))
    check(isinstance(values, list) and len(values) == 1)
    value = values[0]
    labels = value.get('Config', {}).get('Labels', {})
    check(value.get('State', {}).get('Running') is True and
          labels.get('com.docker.compose.project') == 'neko-' + project and
          labels.get('com.docker.compose.service') == service)
    expected = root / ('photos/immich-library' if project == 'immich' else
                       'appdata/nextcloud/html' if project == 'nextcloud' else 'appdata/' + project)
    check(any(m.get('Type') == 'bind' and m.get('Source') == str(expected) for m in value.get('Mounts', [])))
    bindings = value.get('HostConfig', {}).get('PortBindings', {})
    port = 80 if project == 'nextcloud' else store.APPS[project][1]
    ports = bindings.get(str(port) + '/tcp')
    check(isinstance(ports, list) and len(ports) == 1 and
          ports[0].get('HostIp') == '127.0.0.1' and ports[0].get('HostPort') == str(store.APPS[project][1]))


def provision(config, apps, output=print, client_factory=Client):
    pending = []
    for app in apps:
        entry = store.get(config, app)
        if not entry:
            pending.append(app)
            continue
        output(store.APPS[app][0] + ': checking account setup')
        client = client_factory('navidrome' if app == 'navidrome_listener' else app)
        extra = None
        try:
            verify_project(config, app)
            wait('navidrome' if app == 'navidrome_listener' else app, client,
                 config.get('accounts', {}).get('wait_seconds', 60))
            store.update(config, app, state='started')
            if app in ARR:
                arr(client, entry, config, app)
            elif app == 'navidrome_listener':
                admin = store.get(config, 'navidrome', verified=True)
                check(admin is not None)
                extra = client_factory('navidrome')
                listener(client, entry, extra, admin)
                import music_client
                music_client.save(config, music_client.credential(entry['username'], entry['password']))
            elif app == 'seerr':
                seerr(client, entry, store.get(config, 'jellyfin', verified=True))
            elif app == 'nextcloud':
                nextcloud(client, entry, config)
            else:
                {'jellyfin': jellyfin, 'immich': immich, 'navidrome': navidrome,
                 'qbittorrent': qbittorrent}[app](client, entry)
            store.update(config, app, state='verified', allow_create=False)
            output(store.APPS[app][0] + ': verified')
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            store.update(config, app, state='needs_setup')
            pending.append(app)
            output(store.APPS[app][0] + ': pending; saved credentials preserved, no success claimed')
        finally:
            client.close()
            if extra:
                extra.close()
    return pending
