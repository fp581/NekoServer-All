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

ARR = {
    app: (version, app_port(app))
    for app, version in (('sonarr', 'v3'), ('radarr', 'v3'), ('lidarr', 'v1'), ('prowlarr', 'v1'))
}
HEALTH = {
    'jellyfin': '/System/Info/Public',
    'navidrome': '/ping',
    'qbittorrent': '/api/v2/app/version',
    'immich': '/api/server/ping',
    'seerr': '/api/v1/status',
    'nextcloud': '/status.php',
}


class HTTPFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Local account API request failed (HTTP {code}); credentials were not logged')


class Client:
    def __init__(self, app, port=None):
        self.base = f'http://127.0.0.1:{port or store.APPS[app][1]}'
        self.headers = {'Accept': 'application/json', 'Referer': self.base}
        if app == 'jellyfin':
            self.headers['Authorization'] = (
                'MediaBrowser Client="NekoServer", Device="Installer", DeviceId="neko-account-setup", Version="1"'
            )
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


# What may be passed to docker: a word (a name, a label, a path, an account name with
# Nextcloud's allowed characters) or an option, never a shell construct or a control character.
ARGUMENT = r"[A-Za-z0-9/][A-Za-z0-9_.@:/=+,'-]*|--?[A-Za-z][A-Za-z0-9-]*(?:=[A-Za-z0-9_./:,-]*)?"


def docker(args, input_text=None):
    """Docker's own command line (never a shell). Every argument must be a plain word (a
    name, a path, a label) or an option; anything else is refused before it is passed on."""
    checked = []
    for argument in args:
        if not isinstance(argument, str) or not re.fullmatch(ARGUMENT, argument):
            raise RuntimeError('Local account setup command refused: an argument is not a plain word or option')
        checked.append(argument)
    if not checked:
        raise RuntimeError('Local account setup command refused: nothing to run')
    result = subprocess.run(
        ['docker', '--host', 'unix:///var/run/docker.sock', *checked],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise RuntimeError('Local account setup command failed; private output was suppressed')
    return result.stdout + (result.stderr if args and args[0] == 'logs' else '')


def allowed(entry):
    if not entry['allow_create']:
        raise RuntimeError(
            'the app did not accept the saved username and password, and it already has an account, '
            'so none is made or reset. Run  sudo python3 account-setup.py  and choose c to enter the '
            'login the app really has'
        )


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
        result = client.call(
            '/Users/AuthenticateByName', 'POST', {'Username': entry['username'], 'Pw': entry['password']}
        )
        check(
            result.get('User', {}).get('Name') == entry['username']
            and result.get('User', {}).get('Policy', {}).get('IsAdministrator') is True
            and result.get('AccessToken')
        )
        client.headers['X-Emby-Token'] = result['AccessToken']
        client.headers['Authorization'] = (
            client.headers['Authorization'].split(', Token=')[0] + ', Token="' + result['AccessToken'] + '"'
        )
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
        client.call(
            '/api/auth/admin-sign-up',
            'POST',
            {'email': entry['username'], 'password': entry['password'], 'name': entry['username'].split('@')[0]},
        )
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
        administrator.call(
            '/api/user/',
            'POST',
            {'userName': entry['username'], 'name': entry['username'], 'password': entry['password'], 'isAdmin': False},
        )
        value = client.call('/auth/login', 'POST', body)
    check(
        value.get('username') == entry['username']
        and value.get('isAdmin') is False
        and value.get('token')
        and value.get('id')
    )
    if entry['allow_create'] and entry['state'] != 'verified':
        libraries = administrator.call('/api/library/')
        check(isinstance(libraries, list) and bool(libraries))
        identity = urllib.parse.quote(str(value['id']), safe='')
        administrator.call(
            '/api/user/' + identity + '/library/', 'PUT', {'libraryIds': [lib['id'] for lib in libraries]}
        )
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
        current.update(
            authenticationMethod='forms',
            authenticationRequired='enabled',
            username=entry['username'],
            password=entry['password'],
            passwordConfirmation=entry['password'],
        )
        client.call(endpoint, 'PUT', current)
        # The app saves the new sign-in settings at once but applies them only after a
        # restart (its settings page says "restart required"): until then there is no
        # sign-in page, and the password check below found no session. Restart it and
        # wait for it to answer again.
        client.call('/api/' + ARR[app][0] + '/system/restart', 'POST')
        time.sleep(5)
        wait(app, client, seconds=180)
    else:
        check(current.get('username') == entry['username'])
    verified = client.call(endpoint)
    check(
        verified.get('username') == entry['username']
        and str(verified.get('authenticationMethod', '')).lower() == 'forms'
        and str(verified.get('authenticationRequired', '')).lower() == 'enabled'
    )
    # API-key success is not proof of the selected login password. Use a fresh
    # cookie session without the API key and verify an authenticated page.
    client.headers.pop('X-Api-Key', None)
    client.jar.clear()
    try:
        client.call(
            '/login',
            'POST',
            form={'username': entry['username'], 'password': entry['password'], 'rememberMe': 'false'},
            raw=True,
        )
    except HTTPFailure as exc:
        if exc.code != 302 or not client.jar:
            raise
    check(any(cookie.name.endswith('Auth') or 'Auth' in cookie.name for cookie in client.jar))
    # The API itself wants the key even with a session cookie (401 without it, since
    # Sonarr v4), so the cookie is proved on the page the web app loads first: it
    # carries the API key only for a signed-in session, and sends out to the sign-in
    # page otherwise.
    page = client.call('/initialize.json', raw=True)
    try:
        loaded = json.loads(page)
    except ValueError:
        loaded = None
    check(isinstance(loaded, dict) and loaded.get('apiKey') == key)


def qbittorrent(client, entry):
    def login(username, password):
        reply = client.call('/api/v2/auth/login', 'POST', form={'username': username, 'password': password}, raw=True)
        # qBittorrent 4 answers a good login with "Ok." and a cookie named SID;
        # qBittorrent 5 with an empty 204 and QBT_SID_<port>. A wrong login is
        # "Fails." (or 401). The session cookie is the proof.
        if reply.strip() not in (b'Ok.', b'') or not any(
            cookie.name == 'SID' or cookie.name.startswith('QBT_SID') for cookie in client.jar
        ):
            return None
        return True

    if not attempt(lambda: login(entry['username'], entry['password'])):
        allowed(entry)
        ids = docker(
            [
                'ps',
                '-q',
                '--filter',
                'label=com.docker.compose.project=neko-qbittorrent',
                '--filter',
                'label=com.docker.compose.service=qbittorrent',
            ]
        ).split()
        check(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]))
        logs = docker(['logs', '--tail', '200', ids[0]])
        matches = re.findall(r'temporary password is provided for this session:\s*(\S+)', logs)
        check(bool(matches))
        check(login('admin', matches[-1]))
        preferences = client.call('/api/v2/app/preferences')
        check(preferences.get('web_ui_username', 'admin') == 'admin')
        client.call(
            '/api/v2/app/setPreferences',
            'POST',
            form={
                'json': json.dumps(
                    {
                        'web_ui_username': entry['username'],
                        'web_ui_password': entry['password'],
                        'bypass_local_auth': False,
                        'bypass_auth_subnet_whitelist_enabled': False,
                    }
                )
            },
            raw=True,
        )
        client.call('/api/v2/auth/logout', 'POST', form={}, raw=True)
        client.jar.clear()
        check(login(entry['username'], entry['password']))
    preferences = client.call('/api/v2/app/preferences')
    check(
        preferences.get('web_ui_username') == entry['username']
        and preferences.get('bypass_local_auth') is False
        and preferences.get('bypass_auth_subnet_whitelist_enabled') is False
    )
    client.call('/api/v2/auth/logout', 'POST', form={}, raw=True)


def seerr_signed_in_as(client, result, entry):
    """The signed-in session must be Seerr's first user with the saved email. Seerr 3.5
    answers a sign-in with little more than the id; the signed-in user's own page
    (/auth/me) still carries the email, so that is what is checked."""
    check(result.get('id') == 1)
    me = client.call('/api/v1/auth/me')
    check(me.get('id') == 1 and me.get('email', '').casefold() == entry['username'].casefold())


def seerr(client, entry, jellyfin_entry):
    login_body = {'email': entry['username'], 'password': entry['password']}
    result = attempt(lambda: client.call('/api/v1/auth/local', 'POST', login_body))
    if not result:
        allowed(entry)
        check(client.call('/api/v1/settings/public').get('initialized') is False)
        check(jellyfin_entry and jellyfin_entry['state'] == 'verified')
        sign_in = {
            'username': jellyfin_entry['username'],
            'password': jellyfin_entry['password'],
            'email': entry['username'],
        }
        where = {'hostname': 'jellyfin', 'port': app_port('jellyfin'), 'useSsl': False, 'urlBase': '', 'serverType': 2}
        try:
            result = client.call('/api/v1/auth/jellyfin', 'POST', {**sign_in, **where})
        except HTTPFailure as exc:
            # Once Seerr is linked to Jellyfin (an earlier run got this far), it refuses
            # the address again ("Jellyfin hostname already configured", HTTP 500); the
            # Jellyfin login alone signs that user in.
            if exc.code != 500:
                raise
            result = client.call('/api/v1/auth/jellyfin', 'POST', sign_in)
        seerr_signed_in_as(client, result, entry)
        # A partially completed managed setup may already have set the password.
        # Never overwrite a password that does not authenticate with saved choices.
        check(client.call('/api/v1/user/1/settings/password').get('hasPassword') is False)
        client.call('/api/v1/user/1/settings/password', 'POST', {'newPassword': entry['password']})
        client.call('/api/v1/auth/logout', 'POST')
        client.jar.clear()
        result = client.call('/api/v1/auth/local', 'POST', login_body)
    seerr_signed_in_as(client, result, entry)
    if entry['allow_create'] and client.call('/api/v1/settings/public').get('initialized') is False:
        client.call('/api/v1/settings/initialize', 'POST')
    check(client.call('/api/v1/settings/public').get('initialized') is True)
    client.call('/api/v1/auth/logout', 'POST')


def nextcloud_occ(config, *args):
    """Nextcloud's own command line (occ) inside its container, as its web user."""
    manifest = safe_path(Path(config['data_mount']) / 'compose/nextcloud/compose.yaml')
    env = safe_path(manifest.parent / '.env')
    return docker(
        [
            'compose',
            '-p',
            'neko-nextcloud',
            '-f',
            str(manifest),
            '--env-file',
            str(env),
            'exec',
            '-T',
            '--user',
            'www-data',
            'nextcloud',
            'php',
            'occ',
            *args,
        ]
    )


def nextcloud_two_factor(config, username, groups=()):
    """Whether Nextcloud asks this user for a second sign-in step: turned on for the
    user, or required of them by the administrator. Required "for everyone" counts; one
    required only for some groups, or for all but some, counts only when this user is
    covered (`groups`: the user's groups, from user:info)."""
    state = nextcloud_occ(config, 'twofactorauth:state', username)
    if 'is enabled for user' in state:
        return True
    enforced = nextcloud_occ(config, 'twofactorauth:enforce')
    if 'is enforced' not in enforced or 'not enforced' in enforced:
        return False
    if 'is enforced for all users' in enforced and 'except members of' not in enforced:
        return True
    try:
        listed = nextcloud_occ(config, 'config:list', 'system', '--output=json')
    except RuntimeError:  # can't be told: the refused password counts as wrong
        return False
    return two_factor_required_of(listed, groups)


def two_factor_required_of(listed, groups):
    """Nextcloud's own rule for enforced two-factor sign-in (MandatoryTwoFactor::isEnforcedFor),
    from its settings (`occ config:list system --output=json`): with groups named, only
    their members; otherwise everyone but the members of the excluded groups. False when
    the settings can't be read: a refused password is then a wrong password."""
    try:
        system = json.loads(listed)['system']
        named, excluded = (
            system.get('twofactor_enforced_groups') or [],
            system.get('twofactor_enforced_excluded_groups') or [],
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    if not isinstance(named, list) or not isinstance(excluded, list) or str(system.get('twofactor_enforced')) != 'true':
        return False
    if named:
        return any(group in groups for group in named)
    return not any(group in groups for group in excluded)


NEXTCLOUD_TWO_FACTOR = (
    'verified: the administrator exists; two-factor sign-in is on, so Nextcloud does not accept '
    'a plain password from NekoServer and the saved password could not be tried (an app '
    'password from Nextcloud, Settings, Security, can be saved instead with account-setup.py c)'
)


def nextcloud(client, entry, config):
    """The administrator, from Nextcloud's own user list (occ), then the saved password
    over WebDAV. With two-factor sign-in on, Nextcloud refuses every plain password
    there (HTTP 401) and only takes an app password, so the occ check is the answer and
    the step is not stuck for ever. Returns a note for the setup log, or None."""
    status = client.call('/status.php')
    check(status.get('installed') is True and status.get('maintenance') is False and not status.get('needsDbUpgrade'))
    info = json.loads(nextcloud_occ(config, 'user:info', '--output=json', entry['username']))
    check(info.get('enabled') is True and 'admin' in info.get('groups', []))
    basic = base64.b64encode((entry['username'] + ':' + entry['password']).encode()).decode()
    path = '/remote.php/dav/files/' + urllib.parse.quote(entry['username'], safe='') + '/'
    try:
        client.call(
            path,
            'PROPFIND',
            b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
            raw=True,
            headers={'Authorization': 'Basic ' + basic, 'Content-Type': 'application/xml', 'Depth': '0'},
        )
    except HTTPFailure as exc:
        if exc.code != 401 or not nextcloud_two_factor(config, entry['username'], info.get('groups', [])):
            raise
        return NEXTCLOUD_TWO_FACTOR
    return None


def verify_project(config, app):
    """Prove the local port belongs to this managed app before sending a secret."""
    project = 'navidrome' if app == 'navidrome_listener' else app
    service = 'immich-server' if project == 'immich' else project
    root = Path(config['data_mount'])
    manifest = safe_path(root / 'compose' / project / 'compose.yaml')
    check(manifest.is_file())
    ids = docker(
        [
            'ps',
            '-q',
            '--filter',
            'label=com.docker.compose.project=neko-' + project,
            '--filter',
            'label=com.docker.compose.service=' + service,
        ]
    ).split()
    check(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]))
    values = json.loads(docker(['inspect', ids[0]]))
    check(isinstance(values, list) and len(values) == 1)
    value = values[0]
    labels = value.get('Config', {}).get('Labels', {})
    check(
        value.get('State', {}).get('Running') is True
        and labels.get('com.docker.compose.project') == 'neko-' + project
        and labels.get('com.docker.compose.service') == service
    )
    expected = root / {'immich': 'photos/immich-library', 'nextcloud': 'appdata/nextcloud/html'}.get(
        project, 'appdata/' + project
    )
    check(any(m.get('Type') == 'bind' and m.get('Source') == str(expected) for m in value.get('Mounts', [])))
    bindings = value.get('HostConfig', {}).get('PortBindings', {})
    port = 80 if project == 'nextcloud' else store.APPS[project][1]
    ports = bindings.get(str(port) + '/tcp')
    check(
        isinstance(ports, list)
        and len(ports) == 1
        and ports[0].get('HostIp') == '127.0.0.1'
        and ports[0].get('HostPort') == str(store.APPS[project][1])
    )


def provision(config, apps, output=print, client_factory=Client):
    pending = []
    for app in apps:
        entry = store.get(config, app)
        if not entry:
            pending.append(app)
            continue
        output(store.APPS[app][0] + ': checking account setup')
        client = client_factory('navidrome' if app == 'navidrome_listener' else app)
        extra = note = None
        try:
            verify_project(config, app)
            wait(
                'navidrome' if app == 'navidrome_listener' else app,
                client,
                config.get('accounts', {}).get('wait_seconds', 180),
            )
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
                note = nextcloud(client, entry, config)
            else:
                {'jellyfin': jellyfin, 'immich': immich, 'navidrome': navidrome, 'qbittorrent': qbittorrent}[app](
                    client, entry
                )
            store.update(config, app, state='verified', allow_create=False)
            output(store.APPS[app][0] + ': ' + (note or 'verified'))
        except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            store.update(config, app, state='needs_setup')
            pending.append(app)
            # The reason, never the password: the checks' own messages name no values.
            output(
                store.APPS[app][0]
                + ': not verified ('
                + (str(exc) or type(exc).__name__)
                + '); the saved login is kept'
            )
        finally:
            client.close()
            if extra:
                extra.close()
    return pending
