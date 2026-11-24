"""Current GET-only Jellyfin/Seerr readiness; no prompts or credentials in reports."""

import json
from media_libraries import (
    Client,
    SetupRequired,
    SEERR_OTHER_JELLYFIN,
    seerr_server_id,
    folders,
    matching,
    LIBRARIES,
    open_seerr,
    row,
    readiness_key_path,
)
from neko_common import media_services, safe_path
import seerr_providers

# Checks only a person can do (a real playback or transfer). They are listed so the
# report is honest, but they never block setup: automatic checks decide that.
WITNESSED = frozenset(
    {
        'end_to_end_media',
        'navidrome:authenticated_playback_or_transfer',
        'qbittorrent:authenticated_playback_or_transfer',
    }
)
# Checks of extras, reported but never holding setup up either: searching and asking for
# films in Jellyfin (jellyfin_requests.py) waits on a plugin made outside NekoServer, while
# Seerr works without it.
REPORTED_ONLY = WITNESSED | {'jellyfin:search_and_request'}


class ReadClient(Client):
    def call(self, endpoint, method='GET', payload=None):
        if method != 'GET' or payload is not None:
            raise RuntimeError('Readiness client only allows GET requests')
        return super().call(endpoint)

    def close(self):
        # Stored application keys are not newly created login sessions.
        self.headers.clear()


# Jellyfin's answer to a key it does not (or no longer) accept.
REJECTED = ('jellyfin: HTTP 401', 'jellyfin: HTTP 403')


def jellyfin_key(root, settings, server_id):
    """Jellyfin's own NekoServer key; Seerr's link to this server only when Seerr is
    installed and linked. Jellyfin alone never needs Seerr."""
    try:
        own = readiness_key_path(root).read_text().strip()
    except OSError:
        own = ''
    if own:
        return own
    saved = settings.get('jellyfin', {}) if isinstance(settings, dict) else {}
    if (
        isinstance(saved, dict)
        and saved.get('serverId', saved.get('serverID')) == server_id
        and isinstance(saved.get('apiKey'), str)
        and saved['apiKey']
    ):
        return saved['apiKey']
    raise SetupRequired(
        'No Jellyfin key for NekoServer yet: run setup again (it signs in to Jellyfin once to create it)'
    )


def chosen_folders(config):
    """Where the settings say requests are saved (seerr_providers.request_folders), for
    the check of Seerr's providers: one that still saves into a folder chosen earlier
    passed on its path alone, though that folder no longer takes new files. None when
    the settings cannot say (a damaged "placement"): the check then takes the folder
    the provider has, as before."""
    try:
        return seerr_providers.request_folders(config)
    except (RuntimeError, KeyError, TypeError, ValueError):
        return None


JELLYFIN_LIBRARIES = 'jellyfin:current_libraries'
SEERR_LIBRARIES = 'seerr:current_libraries'


def seerr_settings(root):
    """Seerr's own settings file, or {} when it is not there or cannot be read."""
    try:
        path = safe_path(root / 'appdata/seerr/settings.json')
        if path.is_file():
            return json.loads(path.read_text())
    except (OSError, ValueError, RuntimeError):
        pass
    return {}


def jellyfin_folders(client):
    """Jellyfin's libraries, asked with NekoServer's key; a key it no longer takes is
    something to set up again, not a broken Jellyfin."""
    try:
        return folders(client)
    except RuntimeError as exc:
        if str(exc) in REJECTED:
            raise SetupRequired(
                'Jellyfin no longer accepts NekoServer\'s key (it was revoked or replaced): run  sudo bash install.sh  again to renew it'
            ) from None
        raise


def jellyfin_row(client, root, settings, found):
    """The row of Jellyfin's films and shows libraries. `found` is given Jellyfin's server
    id and the libraries' ids as they are seen (Seerr's check below needs them)."""
    try:
        info = client.call('/System/Info/Public')
        if not isinstance(info, dict) or not info.get('Id'):
            raise RuntimeError('Invalid public system response')
        found['server_id'] = info['Id']
        if info.get('StartupWizardCompleted') is not True:
            raise SetupRequired('Jellyfin first administrator wizard is incomplete')
        client.headers['X-Emby-Token'] = jellyfin_key(root, settings, found['server_id'])
        values = jellyfin_folders(client)
        for category, name, kind in LIBRARIES:
            value = matching(values, category, name, kind)
            if not value:
                raise SetupRequired('Expected movie/TV libraries are missing')
            found['ids'].append(str(value['ItemId']))
        return row(JELLYFIN_LIBRARIES, 'pass')
    except SetupRequired as exc:
        return row(JELLYFIN_LIBRARIES, 'needs_setup', str(exc))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return row(JELLYFIN_LIBRARIES, 'fail', 'Jellyfin authenticated library check failed')


def seerr_row(client, root, server_id, ids):
    """The row of Seerr's link to this Jellyfin and its libraries switched on there."""
    try:
        open_seerr(client, root, 'Seerr first-run setup is incomplete')
        selected = client.call('/settings/jellyfin')
        linked = seerr_server_id(selected)
        if not server_id or not linked:
            raise SetupRequired('Seerr/Jellyfin server identity is not verified')
        if linked != server_id:
            raise SetupRequired(SEERR_OTHER_JELLYFIN)
        values = client.call('/settings/jellyfin/library')
        if (
            len(ids) != 2
            or not isinstance(values, list)
            or not all(
                any(isinstance(x, dict) and str(x.get('id')) == i and x.get('enabled') is True for x in values)
                for i in ids
            )
        ):
            raise SetupRequired('Expected Jellyfin libraries are not currently enabled in Seerr')
        return row(SEERR_LIBRARIES, 'pass')
    except SetupRequired as exc:
        return row(SEERR_LIBRARIES, 'needs_setup', str(exc))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return row(SEERR_LIBRARIES, 'fail', 'Seerr library/link API check failed')


def check(config, factory=ReadClient):
    root = safe_path(config['data_mount'])
    apps = media_services(config)
    results = []
    found = {'server_id': None, 'ids': []}
    settings = seerr_settings(root) if 'seerr' in apps else {}
    if 'jellyfin' in apps:
        client = factory('jellyfin')
        try:
            results.append(jellyfin_row(client, root, settings, found))
        finally:
            client.close()
    if 'seerr' in apps:
        client = factory('seerr')
        try:
            results.append(seerr_row(client, root, found['server_id'], found['ids']))
        finally:
            client.close()
        if set(apps) & set(seerr_providers.SPECS):
            results += seerr_providers.configure(
                root,
                apps=apps,
                factory=factory,
                verify_only=True,
                folders=chosen_folders(config),
            )
    if 'navidrome' in apps:
        import music_client

        music = music_client.check(config)
        results.append(
            row(
                'navidrome:catalogue',
                music['status'],
                'Authenticated non-admin listener/library check; real audio playback remains witnessed',
            )
        )
    for app in ('qbittorrent', 'navidrome'):
        if app in apps:
            results.append(
                row(
                    app + ':authenticated_playback_or_transfer',
                    'needs_setup',
                    'Current authenticated transfer/playback needs a server acceptance test; no passwords are requested by monitoring',
                )
            )
    if apps:
        results.append(
            row(
                'end_to_end_media',
                'needs_setup',
                'Complete a real request, acquisition, import and playback test on the deployed server',
            )
        )
    return results
