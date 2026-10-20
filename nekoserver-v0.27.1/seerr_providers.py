"""Create missing standard Seerr request providers, preserving existing choices."""
import xml.etree.ElementTree as ET
from pathlib import Path
from media_libraries import Client, SetupRequired, row, seerr_key
from neko_common import safe_path

SPECS = {'sonarr': (8989, '/data/tv'), 'radarr': (7878, '/data/movies')}


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
    if not values or any(type(x.get('id')) is not int or x['id'] <= 0 or not isinstance(x.get('name'), str) or not x['name'].strip() for x in values):
        raise SetupRequired('No usable live profile is available')
    if len({x['id'] for x in values}) != len(values):
        raise RuntimeError('Duplicate profile IDs')
    return values


def choose(values, label, prompt):
    if len(values) == 1:
        return values[0]
    print(label + ':')
    for index, item in enumerate(values, 1):
        print(f"  {index}. {item['name']}")
    answer = prompt('Choose a number (blank to skip): ').strip()
    if not answer:
        raise SetupRequired('Profile selection skipped')
    if not answer.isdecimal() or not 1 <= int(answer) <= len(values):
        raise SetupRequired('Invalid profile selection; provider unchanged')
    return values[int(answer) - 1]


def current_standard(values):
    values = records(values)
    if any(type(x.get('is4k')) is not bool for x in values):
        raise RuntimeError('Provider type missing')
    return [x for x in values if not x['is4k']]


def connection_matches(value, connection):
    return all(value.get(k, '' if k == 'baseUrl' else None) == v for k, v in connection.items())


def provider(seerr, manager, root, app, apply, prompt, verify_only=False):
    port, directory = SPECS[app]
    endpoint = '/settings/' + app
    initial = records(seerr.call(endpoint))
    standard = current_standard(initial)
    if verify_only and not standard:
        raise SetupRequired('No standard request provider is configured; rerun --request-providers')
    key = app_key(root, app)
    manager.headers['X-Api-Key'] = key
    status = manager.call('/system/status')
    if not isinstance(status, dict) or not isinstance(status.get('version'), str):
        raise RuntimeError('Media manager version missing')
    major = status['version'].split('.')[0]
    if not major.isdecimal() or (app == 'sonarr' and int(major) not in (3, 4)) or int(major) < 3:
        raise SetupRequired('Unsupported media manager version; configure its provider manually')
    base = status.get('urlBase', '')
    if not isinstance(base, str) or (base and (not base.startswith('/') or base.startswith('//') or any(c in base for c in ('?', '#', ':', '\\')) or '..' in base.split('/'))):
        raise SetupRequired('Unsupported media manager base URL; custom configuration preserved')
    connection = {'hostname': app, 'port': port, 'apiKey': key, 'useSsl': False, 'baseUrl': base}
    if standard and (len(standard) != 1 or standard[0].get('isDefault') is not True or not connection_matches(standard[0], connection)):
        raise SetupRequired('Existing standard providers/default routing differ; review them in Seerr')
    roots = records(manager.call('/rootfolder'))
    expected = [x for x in roots if str(x.get('path', '')).rstrip('/') == directory]
    if len(expected) != 1 or expected[0].get('accessible') is not True:
        raise SetupRequired('Expected Arr root folder is missing or inaccessible; run --configure first')
    local_profiles = profiles(manager.call('/qualityprofile'))
    # This POST tests from Seerr's container; it does not save a provider or request content.
    if verify_only:
        if type(standard[0].get('id')) is not int or standard[0]['id']<0:
            raise RuntimeError('Provider ID missing')
        tested=seerr.call('/service/'+app+'/'+str(standard[0]['id']))
    else:tested = seerr.call(endpoint + '/test', 'POST', connection)
    if not isinstance(tested, dict):
        raise RuntimeError('Unsupported connection test response')
    remote_profiles = profiles(tested.get('profiles'))
    remote_roots = records(tested.get('rootFolders'))
    if tested.get('urlBase', base) != base or not any(str(x.get('path', '')).rstrip('/') == directory for x in remote_roots):
        raise SetupRequired('Seerr cannot see the expected Arr root folder/base URL')
    available = [x for x in local_profiles if any(p['id'] == x['id'] and p['name'] == x['name'] for p in remote_profiles)]
    if len(available) != len(local_profiles) or len(remote_profiles) != len(local_profiles):
        raise SetupRequired('Seerr and the local manager report different profiles')
    languages = None
    if app == 'sonarr' and int(major) == 3:
        languages = profiles(tested.get('languageProfiles'))
    if standard:
        existing = standard[0]
        if str(existing.get('activeDirectory', '')).rstrip('/') != directory or not any(p['id'] == existing.get('activeProfileId') for p in available):
            raise SetupRequired('Existing provider root/quality profile is not valid; settings preserved')
        if languages and not any(p['id'] == existing.get('activeLanguageProfileId') for p in languages):
            raise SetupRequired('Existing Sonarr language profile is invalid; settings preserved')
        if existing.get('preventSearch') is not False:
            raise SetupRequired('Existing provider prevents searches or has an unknown search policy; review it in Seerr')
        return row('seerr:provider:' + app, 'pass', 'Existing default provider verified through Seerr; settings preserved')
    selected = choose(available, app.capitalize() + ' request quality profile', prompt)
    payload = dict(connection, name='NekoServer ' + app.capitalize(), activeProfileId=selected['id'], activeProfileName=selected['name'], activeDirectory=directory,
                   is4k=False, isDefault=True, syncEnabled=False, preventSearch=False)
    if app == 'sonarr':
        payload['enableSeasonFolders'] = True
        if languages:
            payload['activeLanguageProfileId'] = choose(languages, 'Sonarr request language profile', prompt)['id']
    else:
        payload['minimumAvailability'] = 'released'
    if not apply:
        return row('seerr:provider:' + app, 'needs_setup', 'Preview: connection tested; default provider would be created')
    if records(seerr.call(endpoint)) != initial:
        raise SetupRequired('Provider settings changed during setup; rerun before saving')
    seerr.call(endpoint, 'POST', payload)
    saved = current_standard(seerr.call(endpoint))
    if len(saved) != 1 or any(saved[0].get(k) != v for k, v in payload.items()):
        raise RuntimeError('Provider settings were not retained as expected')
    # Recheck Docker DNS/API access using the actual retained connection.
    retained = seerr.call(endpoint + '/test', 'POST', {k: saved[0][k] for k in connection})
    if not isinstance(retained, dict) or not any(p.get('id') == selected['id'] for p in records(retained.get('profiles'))) or not any(str(p.get('path', '')).rstrip('/') == directory for p in records(retained.get('rootFolders'))):
        raise RuntimeError('Retained provider connection could not be verified')
    return row('seerr:provider:' + app, 'pass', 'Default provider created, read back and tested through Seerr')


def configure(root, apply=False, apps=('seerr', 'sonarr', 'radarr'), factory=Client, prompt=input, verify_only=False):
    if verify_only and apply:raise ValueError('Read-only verification cannot apply changes')
    selected = [app for app in SPECS if app in apps]
    if 'seerr' not in apps or not selected:
        return [row('seerr:request_providers', 'needs_setup', 'Select Seerr and at least one of Sonarr/Radarr')]
    client = factory('seerr')
    try:
        try:
            client.headers['X-Api-Key'] = seerr_key(root)
            public = client.call('/settings/public')
            if not isinstance(public, dict) or public.get('initialized') is not True:
                raise SetupRequired('Complete Seerr login/server selection in its first-run wizard')
        except SetupRequired as exc:
            return [row('seerr:request_providers', 'needs_setup', str(exc))]
        except (RuntimeError, ValueError, TypeError, KeyError):
            return [row('seerr:request_providers', 'fail', 'Seerr settings/authentication check failed')]
        results = []
        for app in selected:
            manager = factory(app)
            try:
                try:
                    results.append(provider(client, manager, root, app, apply, prompt,verify_only))
                except SetupRequired as exc:
                    results.append(row('seerr:provider:' + app, 'needs_setup', str(exc)))
                except (RuntimeError, ValueError, TypeError, KeyError, EOFError):
                    results.append(row('seerr:provider:' + app, 'fail', 'Provider API verification failed; review the service and rerun'))
            finally:
                manager.close()
        return results
    finally:
        client.close()
