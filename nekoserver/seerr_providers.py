"""Create missing standard Seerr request providers, preserving existing choices."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from media_libraries import NOT_NEEDED, REQUEST_PROVIDERS, Client, SetupRequired, open_seerr, row
from neko_common import app_port, safe_path

SPECS = {'sonarr': (app_port('sonarr'), '/data/tv'), 'radarr': (app_port('radarr'), '/data/movies')}


# What each app's requests are for, as existing_media names the kinds.
KINDS = {'sonarr': 'tv', 'radarr': 'movies'}
OWN_FOLDER = re.compile(r'/data/existing/[a-z]+(-new)?')


def request_folders(config):
    """{app: the folder a request's files are saved in, as the apps see it}: the person's
    own folder where one was chosen for new films or shows ("save new ones here too",
    existing_media.new_files_folder), else the library's own."""
    import existing_media

    return {app: existing_media.new_files_folder(config, kind) for app, kind in KINDS.items()}


def wanted_folder(app, folder, standard, verify_only):
    """The folder the provider must save into. Told by the caller (`folder`), it is
    that one. A check that was not told (the readiness check) takes the folder the
    provider has when it is the library's own or a person's own folder."""
    library = SPECS[app][1]
    if folder:
        return folder
    has = str(standard[0].get('activeDirectory', '')).rstrip('/') if verify_only and standard else ''
    return has if OWN_FOLDER.fullmatch(has) else library


def app_key(root, app):
    path = safe_path(Path(root) / 'appdata' / app / 'config.xml')
    try:
        key = ET.parse(path).findtext('ApiKey')
    except (OSError, ET.ParseError):
        raise SetupRequired('Start the media manager and complete its initial authentication setup') from None
    if not key:
        raise SetupRequired('Media manager API key is unavailable')
    return key


def records(value):
    if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
        raise RuntimeError('Unsupported provider response')
    return value


def profiles(value):
    values = records(value)
    if not values or any(
        type(x.get('id')) is not int or x['id'] <= 0 or not isinstance(x.get('name'), str) or not x['name'].strip()
        for x in values
    ):
        raise SetupRequired('No usable live profile is available')
    if len({x['id'] for x in values}) != len(values):
        raise RuntimeError('Duplicate profile IDs')
    return values


# The request profiles setup picks for Seerr without asking, in this order (the user:
# nothing is asked after the one approval that does not need the running server); the
# first profile the app lists when it has none of them. They can be changed in Seerr.
STANDARD_PROFILES = ('HD-1080p', 'HD - 720p/1080p', 'Any', 'English')


def standard_profile(values, label):
    """The profile setup picks for Seerr's requests, said in the log."""
    names = {str(item['name']).casefold(): item for item in values}
    picked = next((names[n.casefold()] for n in STANDARD_PROFILES if n.casefold() in names), values[0])
    print(f"{label}: {picked['name']} (change it in Seerr when you want another)")
    return picked


def current_standard(values):
    values = records(values)
    if any(type(x.get('is4k')) is not bool for x in values):
        raise RuntimeError('Provider type missing')
    return [x for x in values if not x['is4k']]


def settings_of(app):
    """Seerr's settings page for one media manager (its list of providers for that app)."""
    return '/settings/' + app


def connection_matches(value, connection):
    return all(value.get(k, '' if k == 'baseUrl' else None) == v for k, v in connection.items())


# The check name of an app's request provider in Seerr.
PROVIDER = 'seerr:provider:'


def manager_release(manager):
    """(the major release, the base URL) of Sonarr or Radarr, as it reports them. Any
    release from 3 on (Sonarr 5 too): only Sonarr 3 has the separate language profiles;
    later releases keep the same provider settings."""
    status = manager.call('/system/status')
    if not isinstance(status, dict) or not isinstance(status.get('version'), str):
        raise RuntimeError('Media manager version missing')
    major = status['version'].split('.')[0]
    if not major.isdecimal() or int(major) < 3:
        raise SetupRequired('Unsupported media manager version; configure its provider manually')
    base = status.get('urlBase', '')
    plain = isinstance(base, str) and (
        not base
        or (
            base.startswith('/')
            and not base.startswith('//')
            and not any(c in base for c in ('?', '#', ':', '\\'))
            and '..' not in base.split('/')
        )
    )
    if not plain:
        raise SetupRequired('Unsupported media manager base URL; custom configuration preserved')
    return int(major), base


def same_folder(found, directory):
    return str(found).rstrip('/') == directory


def tested_through_seerr(seerr, app, standard, connection, verify_only):
    """What Seerr's own container sees of the app: its profiles and root folders. A check
    reads the saved provider through Seerr; setup tests the connection, which saves no
    provider and requests no content."""
    if verify_only:
        if type(standard[0].get('id')) is not int or standard[0]['id'] < 0:
            raise RuntimeError('Provider ID missing')
        tested = seerr.call('/service/' + app + '/' + str(standard[0]['id']))
    else:
        tested = seerr.call(settings_of(app) + '/test', 'POST', connection)
    if not isinstance(tested, dict):
        raise RuntimeError('Unsupported connection test response')
    return tested


def agreed_profiles(local_profiles, tested, base, directory):
    """The quality profiles the app and Seerr both report, once Seerr sees the app's
    folder for requests and its base URL."""
    remote_profiles = profiles(tested.get('profiles'))
    remote_roots = records(tested.get('rootFolders'))
    if tested.get('urlBase', base) != base or not any(same_folder(x.get('path', ''), directory) for x in remote_roots):
        raise SetupRequired('Seerr cannot see the expected Arr root folder/base URL')
    available = [
        x for x in local_profiles if any(p['id'] == x['id'] and p['name'] == x['name'] for p in remote_profiles)
    ]
    if len(available) != len(local_profiles) or len(remote_profiles) != len(local_profiles):
        raise SetupRequired('Seerr and the local manager report different profiles')
    return available


def existing_verified(app, existing, folders, available, languages):
    """The provider that is there already, checked and kept as it is. `folders`: (the
    library's own folder, the folder requests are to be saved in)."""
    library, directory = folders
    if same_folder(existing.get('activeDirectory', ''), library) and library != directory:
        # Chosen after the provider was made: its folder is the person's to change.
        raise SetupRequired(
            f'Requests are still saved in {library}. To save them in your own folder, open Seerr, Settings, '
            f'Services, {app.capitalize()}, and choose the root folder {directory}'
        )
    if not same_folder(existing.get('activeDirectory', ''), directory) or not any(
        p['id'] == existing.get('activeProfileId') for p in available
    ):
        raise SetupRequired('Existing provider root/quality profile is not valid; settings preserved')
    if languages and not any(p['id'] == existing.get('activeLanguageProfileId') for p in languages):
        raise SetupRequired('Existing Sonarr language profile is invalid; settings preserved')
    if existing.get('preventSearch') is not False:
        raise SetupRequired('Existing provider prevents searches or has an unknown search policy; review it in Seerr')
    return row(PROVIDER + app, 'pass', 'Existing default provider verified through Seerr; settings preserved')


def new_provider(app, connection, selected, directory, languages, prompt):
    """The provider setup makes for the app, as Seerr's settings take it."""
    payload = dict(
        connection,
        name='NekoServer ' + app.capitalize(),
        activeProfileId=selected['id'],
        activeProfileName=selected['name'],
        activeDirectory=directory,
        is4k=False,
        isDefault=True,
        syncEnabled=False,
        preventSearch=False,
    )
    if app != 'sonarr':
        payload['minimumAvailability'] = 'released'
        return payload
    payload['enableSeasonFolders'] = True
    if languages:
        payload['activeLanguageProfileId'] = standard_profile(languages, 'Sonarr request language profile')['id']
    return payload


def saved_provider(seerr, app, initial, payload, connection):
    """The provider saved, read back, and tested once more through Seerr with the
    connection it kept (Docker's names and the API reached from Seerr's container)."""
    endpoint = settings_of(app)
    if records(seerr.call(endpoint)) != initial:
        raise SetupRequired('Provider settings changed during setup; rerun before saving')
    seerr.call(endpoint, 'POST', payload)
    saved = current_standard(seerr.call(endpoint))
    if len(saved) != 1 or any(saved[0].get(k) != v for k, v in payload.items()):
        raise RuntimeError('Provider settings were not retained as expected')
    retained = seerr.call(endpoint + '/test', 'POST', {k: saved[0][k] for k in connection})
    if (
        not isinstance(retained, dict)
        or not any(p.get('id') == payload['activeProfileId'] for p in records(retained.get('profiles')))
        or not any(
            same_folder(p.get('path', ''), payload['activeDirectory']) for p in records(retained.get('rootFolders'))
        )
    ):
        raise RuntimeError('Retained provider connection could not be verified')
    return row(PROVIDER + app, 'pass', 'Default provider created, read back and tested through Seerr')


def provider(seerr, manager, root, app, apply, prompt, verify_only=False, folder=None):
    """Seerr's request provider for Sonarr or Radarr: the one that is there, verified
    and kept, or a new one made, saved and read back. `folder`: where requests are
    saved (request_folders); the library's own folder without it."""
    port, library = SPECS[app]
    initial = records(seerr.call(settings_of(app)))
    standard = current_standard(initial)
    directory = wanted_folder(app, folder, standard, verify_only)
    if verify_only and not standard:
        raise SetupRequired('No standard request provider is configured; rerun --request-providers')
    key = app_key(root, app)
    manager.headers['X-Api-Key'] = key
    major, base = manager_release(manager)
    connection = {'hostname': app, 'port': port, 'apiKey': key, 'useSsl': False, 'baseUrl': base}
    if standard and (
        len(standard) != 1
        or standard[0].get('isDefault') is not True
        or not connection_matches(standard[0], connection)
    ):
        raise SetupRequired('Existing standard providers/default routing differ; review them in Seerr')
    expected = [x for x in records(manager.call('/rootfolder')) if same_folder(x.get('path', ''), directory)]
    if len(expected) != 1 or expected[0].get('accessible') is not True:
        raise SetupRequired('Expected Arr root folder is missing or inaccessible; run --configure first')
    local_profiles = profiles(manager.call('/qualityprofile'))
    tested = tested_through_seerr(seerr, app, standard, connection, verify_only)
    available = agreed_profiles(local_profiles, tested, base, directory)
    languages = profiles(tested.get('languageProfiles')) if app == 'sonarr' and major == 3 else None
    if standard:
        return existing_verified(app, standard[0], (library, directory), available, languages)
    selected = standard_profile(available, app.capitalize() + ' request quality profile')
    payload = new_provider(app, connection, selected, directory, languages, prompt)
    if not apply:
        return row(PROVIDER + app, 'needs_setup', 'Preview: connection tested; default provider would be created')
    return saved_provider(seerr, app, initial, payload, connection)


def configure(
    root, apply=False, apps=('seerr', 'sonarr', 'radarr'), factory=Client, prompt=input, verify_only=False, folders=None
):
    """`folders`: where each app's requests are saved (request_folders); the library's own without it."""
    if verify_only and apply:
        raise ValueError('Read-only verification cannot apply changes')
    selected = [app for app in SPECS if app in apps]
    if 'seerr' not in apps:
        return [row(REQUEST_PROVIDERS, 'needs_setup', 'Select Seerr and at least one of Sonarr/Radarr')]
    if not selected:
        # Seerr kept without Sonarr and Radarr (both removed, say): nothing to connect.
        return [row(REQUEST_PROVIDERS, 'pass', NOT_NEEDED)]
    client = factory('seerr')
    try:
        try:
            open_seerr(client, root, 'Complete Seerr login/server selection in its first-run wizard')
        except SetupRequired as exc:
            return [row(REQUEST_PROVIDERS, 'needs_setup', str(exc))]
        except (RuntimeError, ValueError, TypeError, KeyError):
            return [row(REQUEST_PROVIDERS, 'fail', 'Seerr settings/authentication check failed')]
        results = []
        for app in selected:
            manager = factory(app)
            try:
                try:
                    folder = (folders or {}).get(app)
                    results.append(provider(client, manager, root, app, apply, prompt, verify_only, folder))
                except SetupRequired as exc:
                    results.append(row(PROVIDER + app, 'needs_setup', str(exc)))
                except (RuntimeError, ValueError, TypeError, KeyError, EOFError):
                    results.append(
                        row(
                            PROVIDER + app,
                            'fail',
                            'Provider API verification failed; review the service and rerun',
                        )
                    )
            finally:
                manager.close()
        return results
    finally:
        client.close()


def made_here(value, app):
    """True for the request provider `provider` above makes (its name, and pointing at
    the app's own container): a provider someone renamed or pointed elsewhere is theirs."""
    port = SPECS[app][0]
    return (
        value.get('name') == 'NekoServer ' + app.capitalize()
        and value.get('hostname') == app
        and value.get('port') == port
        and type(value.get('id')) is int
        and value['id'] >= 0
    )


def remove_stale(root, apps, apply=False, factory=Client):
    """Take away the request providers NekoServer made for Sonarr or Radarr once that app
    is out of media.services (its container is stopped, so Seerr would show errors and
    requests would fail). Only the providers `provider` made are removed; any other is
    kept and named. Rows for the report; [] when there is nothing to look at."""
    gone = [app for app in SPECS if app not in apps]
    if 'seerr' not in apps or not gone:
        return []
    client = factory('seerr')
    try:
        try:
            open_seerr(client, root, 'Seerr is not set up yet')
        except SetupRequired:
            return []  # never set up, so it has no providers of ours
        return [stale_provider(client, app, apply) for app in gone]
    except (RuntimeError, ValueError, TypeError, KeyError):
        return [
            row(
                'seerr:removed_apps',
                'fail',
                'Could not read Seerr\'s request providers; check them in Seerr (Settings, Services)',
            )
        ]
    finally:
        client.close()


def stale_provider(client, app, apply):
    name = app.capitalize()
    providers = records(client.call(settings_of(app)))
    ours = [p for p in providers if made_here(p, app)]
    others = len(providers) - len(ours)
    for value in ours:
        if apply:
            client.call(settings_of(app) + '/' + str(value['id']), 'DELETE')
    kept = (
        (
            f'; {others} other {name} provider(s) not made by NekoServer kept, remove them in Seerr '
            '(Settings, Services) if you no longer use them'
        )
        if others
        else ''
    )
    if not ours:
        return row(
            'seerr:removed:' + app,
            'pass',
            f'{name} is not set up; Seerr has no request provider of NekoServer\'s for it' + kept,
        )
    done = 'taken away' if apply else 'would be taken away (preview)'
    return row(
        'seerr:removed:' + app, 'pass', f'{name} is not set up; NekoServer\'s request provider for it was {done}' + kept
    )
