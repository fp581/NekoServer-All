"""Authenticated local qBittorrent setup; cookie and password stay in memory."""

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

from neko_common import app_port, local_opener


QBITTORRENT_PORT = app_port('qbittorrent')
# The most one request may take, its answer read whole (an answer dripping a byte at a
# time kept every read within the socket's timeout, and setup waiting for good).
REQUEST_TIME_LIMIT = 60
# The qBittorrent download-client field that holds the category, per app: each app
# names it after its own media (there is no field called "category"). Shared by the
# connector (media-connect.py); the media check (runtime media.rs) judges by the same fields.
CATEGORY_FIELDS = {'sonarr': 'tvCategory', 'radarr': 'movieCategory', 'lidarr': 'musicCategory'}


REQUEST_FAILED = 'qBittorrent API request failed; inspect local WebUI'
# qBittorrent's answers to a login it refuses: 401 (or "Fails.") for a wrong username or
# password, 403 once it has blocked this PC after too many wrong ones (for an hour by
# default, its "ban duration").
WRONG_LOGIN = (
    'qBittorrent did not accept the saved username and password. Run  sudo bash install.sh  again: '
    'its accounts step checks the login and says what to change'
)
BLOCKED = (
    'qBittorrent refuses logins from this PC for now, after too many wrong passwords. Wait an hour '
    '(or restart qBittorrent: sudo docker restart neko-qbittorrent-qbittorrent-1), then run setup again'
)
LOGIN_REFUSED = {401: WRONG_LOGIN, 403: BLOCKED}
ODD_ANSWER = 'qBittorrent gave an answer setup does not understand ({}); check that qBittorrent runs and is up to date'


class RequestFailed(RuntimeError):
    """A request qBittorrent did not answer, or answered with an HTTP error (`code`)."""

    def __init__(self, code=None):
        super().__init__(REQUEST_FAILED)
        self.code = code


class Client:
    def __init__(self, port=QBITTORRENT_PORT):
        self.base = f'http://127.0.0.1:{port}'
        self.jar = http.cookiejar.CookieJar()
        self.opener = local_opener(urllib.request.HTTPCookieProcessor(self.jar), seconds=REQUEST_TIME_LIMIT)

    def call(self, endpoint, form=None, json_result=False):
        if not endpoint.startswith('/') or '://' in endpoint:
            raise ValueError('Invalid local API endpoint')
        body = None if form is None else urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            self.base + '/api/v2' + endpoint,
            data=body,
            headers={'Referer': self.base, 'Content-Type': 'application/x-www-form-urlencoded'},
        )
        try:
            with self.opener.open(req, timeout=10) as response:
                payload = response.read(3_000_000).decode()
        except (OSError, urllib.error.URLError) as exc:
            code = None
            if isinstance(exc, urllib.error.HTTPError):
                code = exc.code
                exc.close()
            raise RequestFailed(code) from None
        if not json_result:
            return payload.strip()
        try:
            return json.loads(payload)
        except ValueError:
            raise RuntimeError(ODD_ANSWER.format(endpoint)) from None

    def login(self, username, password):
        try:
            reply = self.call('/auth/login', {'username': username, 'password': password})
        except RequestFailed as exc:
            if exc.code in LOGIN_REFUSED:
                raise RuntimeError(LOGIN_REFUSED[exc.code]) from None
            raise
        # qBittorrent 4 answers a good login with "Ok." and a cookie named SID,
        # qBittorrent 5 with an empty reply and QBT_SID_<port>; the cookie is the proof.
        if reply.strip() not in ('Ok.', '') or not any(
            c.name == 'SID' or c.name.startswith('QBT_SID') for c in self.jar
        ):
            raise RuntimeError(WRONG_LOGIN)

    def close(self):
        try:
            self.call('/auth/logout', {})
        except RuntimeError:
            pass
        self.jar.clear()


def answer_object(client, endpoint, of_objects=False):
    """The JSON object qBittorrent answers `endpoint` with (each value an object too, when
    `of_objects`: the categories). Anything else is said plainly, never as a Python error."""
    value = client.call(endpoint, json_result=True)
    if not isinstance(value, dict) or of_objects and not all(isinstance(v, dict) for v in value.values()):
        raise RuntimeError(ODD_ANSWER.format(endpoint))
    return value


def category_ok(category, path, desired='/data/downloads'):
    """An empty path means "default folder + category name". Music must end up in
    /data/downloads/music exactly: that folder is on the SSD, where Lidarr imports from.
    TV and movies may also use the shared /data/downloads on the HDD."""
    if category == 'music':
        return path in ('', desired + '/music')
    return path in ('', desired, desired + '/' + category)


def automatic_mode(preferences, categories=('music',)):
    """The preference change that makes new torrents start in automatic mode, where each
    category's own folder is used ({} when they already do, or when nothing needs it).
    Only Lidarr's music needs it: in manual mode TV and films still land in
    /data/downloads, where Sonarr and Radarr find them, but an album would not reach the
    SSD's /data/downloads/music, the only place Lidarr sees. Without Lidarr, a choice of
    manual mode made in qBittorrent is left alone."""
    if 'music' not in categories or preferences.get('auto_tmm_enabled') is True:
        return {}
    return {'auto_tmm_enabled': True}


def retained(verified, actual, changes, categories, desired):
    """qBittorrent kept what setup asked for: the download folders, automatic mode (when
    Lidarr needs it) and each category's folder."""
    path = lambda value: str(value).rstrip('/')
    return (
        path(verified.get('save_path', '')) == desired
        and ('temp_path' not in changes or path(verified.get('temp_path', '')) == changes['temp_path'])
        and ('music' not in categories or verified.get('auto_tmm_enabled') is True)
        and all(c in actual and category_ok(c, path(actual[c].get('savePath', '')), desired) for c in categories)
    )


def save_preferences(client, changes, output):
    """Save the preference changes (if any). Switching automatic mode on is said, once:
    only the run that switches it says so (later runs find it on). It is also said when
    it switches it back on after someone chose manual mode in qBittorrent."""
    if changes.get('auto_tmm_enabled'):
        output(
            'qBittorrent: new torrents now start in automatic mode (again, if you had chosen manual mode). '
            "Lidarr needs it: each torrent is then saved in its category's folder, and music on the SSD, "
            'where Lidarr finds it. Torrents you already have keep their mode. Without Lidarr in '
            'media.services, setup leaves this setting as you choose'
        )
    if changes:
        client.call('/app/setPreferences', {'json': json.dumps(changes)})


def configure(username, password, apply=False, categories=('tv', 'movies', 'music'), client=None, output=print):
    client = client or Client()
    try:
        client.login(username, password)
        preferences = answer_object(client, '/app/preferences')
        existing = answer_object(client, '/torrents/categories', of_objects=True)
        desired = '/data/downloads'
        current = str(preferences.get('save_path', '')).rstrip('/')
        changes = {}
        if current != desired:
            if current not in ('', '/downloads', '/downloads/complete'):
                raise RuntimeError('qBittorrent has a custom save path; preserve it and review manually')
            if client.call('/torrents/info', json_result=True):
                raise RuntimeError('Existing torrents prevent changing the default save path automatically')
            changes['save_path'] = desired
        if preferences.get('temp_path_enabled'):
            temp = str(preferences.get('temp_path', '')).rstrip('/')
            if temp != '/data/downloads/incomplete':
                if not temp.startswith('/downloads/'):
                    raise RuntimeError('qBittorrent has a custom incomplete path; review manually')
                if client.call('/torrents/info', json_result=True):
                    raise RuntimeError('Existing torrents prevent changing the incomplete path')
                changes['temp_path'] = '/data/downloads/incomplete'
        # Each category's own folder is used only in Automatic Torrent Management mode,
        # and qBittorrent starts new torrents in Manual mode unless told otherwise: the
        # Arr apps send only the category, so in Manual mode an album would land in the
        # HDD's /data/downloads, where Lidarr (which sees only the SSD) cannot find it.
        # So it is switched on only with Lidarr (the music category), and said each time.
        # This changes only how new torrents start; existing torrents keep their mode.
        changes.update(automatic_mode(preferences, categories))
        fix = {}
        for category in categories:
            if category not in existing:
                continue
            path = str(existing[category].get('savePath', '')).rstrip('/')
            if category_ok(category, path, desired):
                continue
            # Only music has a single right place (/data/downloads/music, on the SSD that
            # Lidarr sees). A wrong but empty music category is corrected; anything else
            # with torrents in it, or another category's custom path, needs a person.
            if (
                category == 'music'
                and path in (desired, '/downloads', '/downloads/complete')
                and not client.call('/torrents/info?category=music', json_result=True)
            ):
                fix[category] = desired + '/' + category
                continue
            raise RuntimeError(
                f'qBittorrent category {category} saves to {path or "the default folder"}, not {desired}/{category}; '
                'change it in qBittorrent (right-click the category, Edit) and run setup again'
            )
        output('qBittorrent: login verified; download paths/categories ' + ('APPLY' if apply else 'PREVIEW'))
        if apply:
            save_preferences(client, changes, output)
            for category in categories:
                if category not in existing:
                    client.call(
                        '/torrents/createCategory', {'category': category, 'savePath': desired + '/' + category}
                    )
            for category, path in fix.items():
                client.call('/torrents/editCategory', {'category': category, 'savePath': path})
            verified = answer_object(client, '/app/preferences')
            actual = answer_object(client, '/torrents/categories', of_objects=True)
            if not retained(verified, actual, changes, categories, desired):
                raise RuntimeError('qBittorrent did not retain expected download configuration')
    finally:
        client.close()
