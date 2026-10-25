#!/usr/bin/env python3
"""NekoServer media onboarding. Local API only; no secret persistence.

Loads provider schema from running apps. Does not download media or modify libraries.
"""
import argparse
import copy
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from neko_common import app_ports, safe_path, local_opener
import qbit_setup
import account_store

OPENER=local_opener()

PORTS = app_ports(('prowlarr', 'sonarr', 'radarr', 'lidarr'))
API_VERSIONS = {'prowlarr': 'v1', 'sonarr': 'v3', 'radarr': 'v3', 'lidarr': 'v1'}


def app_key(app, root):
    config = safe_path(Path(root) / 'appdata' / app / 'config.xml')
    if not config.is_file() or config.is_symlink():
        raise RuntimeError(f'{app}: missing configuration at {config}; start its container first')
    try:
        key = ET.parse(config).findtext('ApiKey')
    except (ET.ParseError,OSError):
        raise RuntimeError(f'{app}: configuration XML unreadable') from None
    if not key:
        raise RuntimeError(f'{app}: no API key found')
    return key


RESPONSE_LIMIT = 64_000_000
# The qBittorrent download-client field that holds the category, per app.
CATEGORY_FIELDS = {'sonarr': 'tvCategory', 'radarr': 'movieCategory', 'lidarr': 'musicCategory'}


def request(app, key, endpoint, method='GET', payload=None, timeout=12):
    if app not in PORTS or not endpoint.startswith('/'):
        raise ValueError('Unknown application or unsafe endpoint')
    url = f"http://127.0.0.1:{PORTS[app]}/api/{API_VERSIONS[app]}{endpoint}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        'X-Api-Key': key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with OPENER.open(req, timeout=timeout) as response:
            # Prowlarr's indexer catalogue (/indexer/schema) is several MB and grows;
            # a cut-off answer is not JSON ("Expecting value: line 108710 ...").
            content = response.read(RESPONSE_LIMIT + 1)
            if len(content) > RESPONSE_LIMIT:
                raise RuntimeError(f'{app} {endpoint}: the answer is larger than {RESPONSE_LIMIT // 1_000_000} MB')
            return json.loads(content) if content.strip() else {}
    except urllib.error.HTTPError as exc:
        # Do not print response bodies: providers might echo secrets.
        code=exc.code;exc.close()
        raise RuntimeError(f'{app} {endpoint}: HTTP {code}') from None
    except urllib.error.URLError:
        raise RuntimeError(f'{app} {endpoint}: unavailable on localhost') from None


def field_set(fields, name, value):
    for field in fields:
        if field.get('name', '').lower() == name.lower():
            field['value'] = value
            return True
    return False


def choose(items, prompt, ask=input, attempts=3):
    """The item picked by its number; None when skipped (blank or Ctrl+D). A number not
    in the list is asked again rather than failing the step."""
    for i, item in enumerate(items, 1):
        print(f" {i:3d}. {item.get('name') or item.get('implementationName') or item.get('implementation')}")
    for _ in range(attempts):
        try:
            answer = ask(f'{prompt} (number, blank to skip): ').strip()
        except EOFError:
            print()
            return None
        if not answer:
            return None
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer)-1]
        print(f'Type a number from 1 to {len(items)}, or press Enter to skip.')
    return None


def prepared(selected, profiles):
    """A new indexer from Prowlarr's catalogue entry, ready to save: switched on, and in
    an app profile (the catalogue leaves the profile at 0, which Prowlarr refuses). The
    profile Prowlarr made itself ("Standard") is preferred, else the first one."""
    ids = [p for p in profiles if isinstance(p, dict) and isinstance(p.get('id'), int) and p['id'] > 0]
    if not ids:
        raise RuntimeError('Prowlarr has no app profile to put the indexer in; open Prowlarr '
                           '(Settings, Apps, Sync Profiles), add one, and run setup again')
    profile = next((p for p in ids if p.get('name') == 'Standard'), ids[0])
    entry = copy.deepcopy(selected)
    entry['appProfileId'] = profile['id']
    entry['enable'] = True
    return entry


def configure_indexers(root, apply, config=None):
    failures=[]
    key = app_key('prowlarr', root)
    schemas = request('prowlarr', key, '/indexer/schema')
    existing = request('prowlarr', key, '/indexer')
    profiles = request('prowlarr', key, '/appprofile')
    print(f'Prowlarr catalog: {len(schemas)} definitions; {len(existing)} configured indexers')
    query = input('Filter catalog by name (blank for all): ').strip().casefold()
    matches = [s for s in schemas if query in str(s.get('name', '')).casefold()]
    if not matches:
        print('No matching definitions'); return
    # No builtin hard-coded indexer catalog; everything comes from Prowlarr.
    while True:
        selected = choose(matches, 'Select an indexer')
        if selected is None:
            if failures: raise RuntimeError('One or more indexers were rejected; setup incomplete')
            return
        if any(e.get('implementation') == selected.get('implementation') and e.get('name') == selected.get('name') for e in existing):
            print('Already configured; skipping duplicate'); continue
        entry = prepared(selected, profiles)
        if apply and config is not None:
            import guided_setup
            from types import SimpleNamespace
            auth_fields = {'apikey', 'username', 'password', 'passkey', 'cookie'}
            if any(str(f.get('name', '')).lower() in auth_fields for f in entry.get('fields', [])):
                done = guided_setup.checkpoint(SimpleNamespace(config=config, apply=True),
                    'Prepare the selected provider account: ' + str(entry.get('name', 'indexer')), [
                        'In Prowlarr, read the selected provider definition and its own account requirements.',
                        'If required, open that provider from its documented link, sign in/register on its own site, '
                        'and finish any email verification or challenge there.',
                        'Keep the required provider API key/credentials ready. After confirming here, '
                        'enter the requested values into the terminal (what you type is not shown).',
                        'Use only providers and content you are authorized to access.'
                    ], [('url', 'http://127.0.0.1:9696/')])
                if not done and guided_setup.interactive(SimpleNamespace(config=config, apply=True)):
                    raise RuntimeError('Provider account preparation pending')
        for f in entry.get('fields', []):
            name = f.get('name', '')
            if not name or f.get('hidden') or f.get('type') in ('tag',):
                continue
            required = f.get('required', False)
            if not required and name.lower() not in ('baseurl', 'apikey', 'username', 'password', 'passkey', 'cookie'):
                continue
            visible = f.get('label') or name
            value = getpass.getpass(f'{visible} (leave blank for default): ') if any(x in name.lower() for x in ('password', 'key', 'cookie', 'pass')) else input(f'{visible} (leave blank for default): ')
            if value:
                field_set(entry['fields'], name, value)
        print('Selected:', entry.get('name'), '| action:', 'create' if apply else 'PREVIEW ONLY')
        if apply:
            try:
                result = request('prowlarr', key, '/indexer', 'POST', entry)
                existing.append(result)
                print('Created indexer:', result.get('name', '(unnamed)'))
            except RuntimeError as exc:
                failures.append('indexer')
                print('Rejected by Prowlarr:', exc)
                print('Open Prowlarr UI if this indexer requires additional fields or challenge handling.')
                if config is not None:
                    import guided_setup
                    from types import SimpleNamespace
                    def present(entry=entry):
                        return any(row.get('name') == entry.get('name') and
                                   row.get('implementation') == entry.get('implementation')
                                   for row in request('prowlarr', key, '/indexer'))
                    done = guided_setup.checkpoint(SimpleNamespace(config=config, apply=True),
                        'Complete rejected provider setup in Prowlarr', [
                            'Sign into Prowlarr, finish any provider fields/verification or challenge, '
                            'test the provider connection and save this selected indexer.',
                            'Return and confirm yes. The installer checks that the selected provider was saved. '
                            'A real acquisition/import/playback still needs its separate test.'
                        ], [('url', 'http://127.0.0.1:9696/')], verify=present)
                    if done:
                        failures.pop()
                        existing = request('prowlarr', key, '/indexer')


def fields_match(entry, expected):
    actual={f.get('name','').lower():f.get('value') for f in entry.get('fields',[])}
    return all(str(actual.get(k.lower(),'')).rstrip('/')==str(v).rstrip('/') for k,v in expected.items())


def configure_applications(root, apply, apps=('sonarr','radarr','lidarr')):
    failures=[]
    pro_key = app_key('prowlarr', root)
    schemas = request('prowlarr', pro_key, '/applications/schema')
    existing = request('prowlarr', pro_key, '/applications')
    for app in apps:
        try:
            app_api_key = app_key(app, root)
            request(app, app_api_key, '/system/status')
        except RuntimeError as exc:
            print(f'{app}: not ready ({exc})'); failures.append(app); continue
        expected={'prowlarrUrl':'http://prowlarr:9696','baseUrl':f'http://{app}:{PORTS[app]}','apiKey':app_api_key}
        found=next((e for e in existing if str(e.get('name','')).lower()==f'neko-{app}'),None)
        if found:
            # Prowlarr masks the stored key ("********") in its answers, so only the
            # routing fields can be compared; the key was checked when the link was made.
            routing={k:v for k,v in expected.items() if k!='apiKey'}
            if fields_match(found,routing) and found.get('syncLevel') in ('addOnly','fullSync'): print(f'{app}: existing connection fields verified')
            else:
                print(f'{app}: existing connection differs; review in Prowlarr'); failures.append(app)
            continue
        match = next((s for s in schemas if app in str(s.get('implementation', '')).lower()), None)
        if match is None:
            print(f'{app}: no matching Prowlarr application definition; manual setup needed'); failures.append(app); continue
        entry = copy.deepcopy(match)
        entry['name'] = f'neko-{app}'
        entry['syncLevel']='addOnly'  # additive sync; never remove existing indexers
        # Docker service DNS must be used for container-to-container access.
        fields = entry.get('fields', [])
        required = {'prowlarrUrl': 'http://prowlarr:9696', 'baseUrl': f'http://{app}:{PORTS[app]}', 'apiKey': app_api_key}
        for name, value in required.items():
            if not field_set(fields, name, value):
                failures.append(app)
                print(f'{app}: missing provider field {name}; manual setup needed')
                break
        else:
            print(f'{app}:', 'will link' if not apply else 'linking')
            if apply:
                try:
                    result = request('prowlarr', pro_key, '/applications', 'POST', entry)
                    existing.append(result)
                    print(f'{app}: linked')
                except RuntimeError as exc:
                    failures.append(app)
                    print(f'{app}: could not link ({exc}); check Prowlarr UI')

    if failures: raise RuntimeError('Prowlarr application connections incomplete: '+', '.join(failures))


def configure_download_clients(root, apply, apps=('sonarr','radarr','lidarr'),verify_qbit=True):
    failures=[]
    stored = account_store.get({'data_mount': str(root)}, 'qbittorrent', verified=True)
    if stored:
        user, password = stored['username'], stored['password']
    else:
        password = getpass.getpass('qBittorrent WebUI password (not saved by this connector): ')
        if not password:
            raise RuntimeError('No password supplied; download-client setup incomplete')
        user = input('qBittorrent username [admin]: ').strip() or 'admin'
    if verify_qbit:
        categories=tuple({'sonarr':'tv','radarr':'movies','lidarr':'music'}[app] for app in apps)
        qbit_setup.configure(user,password,apply,categories)
    for app, category in [('sonarr','tv'), ('radarr','movies'), ('lidarr','music')]:
        if app not in apps: continue
        # Each app names its qBittorrent category field after its own media.
        category_field = CATEGORY_FIELDS[app]
        try:
            key = app_key(app, root)
            existing = request(app, key, '/downloadclient')
            expected={'host':'qbittorrent','port':8080,'username':user,'password':password,category_field:category}
            found=next((x for x in existing if x.get('name')=='Neko-qBittorrent'),None)
            if found:
                # Servers may mask passwords; compare routing fields, never log secrets.
                routing={k:v for k,v in expected.items() if k!='password'}
                if found.get('enable') and fields_match(found,routing): print(f'{app}: existing client routing verified; confirm stored credentials in UI')
                else:
                    print(f'{app}: existing client differs or is disabled; review in UI'); failures.append(app)
                continue
            candidates = request(app, key, '/downloadclient/schema')
            schema = next((s for s in candidates if str(s.get('implementation','')).lower() == 'qbittorrent'), None)
            if not schema:
                print(f'{app}: no qBittorrent provider schema'); failures.append(app); continue
            entry = copy.deepcopy(schema)
            entry['name'] = 'Neko-qBittorrent'
            entry['enable'] = True
            fields = entry.get('fields', [])
            needed = {'host':'qbittorrent','port':8080,'username':user,'password':password,category_field:category}
            for name, value in needed.items():
                if not field_set(fields, name, value):
                    failures.append(app)
                    print(f'{app}: unsupported schema field {name}; needs manual configuration')
                    break
            else:
                print(f'{app}:', 'will configure' if not apply else 'configuring', 'qBittorrent')
                if apply:
                    try:
                        request(app, key, '/downloadclient', 'POST', entry)
                        print(f'{app}: configured')
                    except RuntimeError as exc:
                        failures.append(app)
                        print(f'{app}: rejected ({exc}); check credentials and WebUI settings')
        except RuntimeError as exc:
            failures.append(app)
            print(f'{app}: skipped ({exc})')

    if failures: raise RuntimeError('Download-client setup incomplete: '+', '.join(failures))


def configure_root_folders(root,media,apply,apps=('sonarr','radarr','lidarr')):
    failures=[]
    for app in apps:
        category={'sonarr':'tv','radarr':'movies','lidarr':'music'}[app]
        host=safe_path(Path(root if category=='music' else media)/'library'/category)
        path='/data/'+category
        try:
            if not host.is_dir():raise RuntimeError('Host library directory missing')
            key=app_key(app,root)
            existing=request(app,key,'/rootfolder')
            found=next((r for r in existing if r.get('path','').rstrip('/')==path),None)
            if found:
                if found.get('accessible') is False:raise RuntimeError('Existing root folder is inaccessible')
                print(f'{app}: root folder already present');continue
            payload={'path':path}
            if app=='lidarr':
                payload.update(name='Neko Music',defaultTags=[])
                for endpoint,field in [('/qualityprofile','defaultQualityProfileId'),('/metadataprofile','defaultMetadataProfileId')]:
                    options=request(app,key,endpoint)
                    if not options:raise RuntimeError('Lidarr has no configured profiles')
                    profile=options[0] if len(options)==1 else choose(options,'Choose Lidarr '+endpoint[1:])
                    if profile is None:raise RuntimeError('Lidarr profile choice required')
                    payload[field]=profile['id']
            print(f'{app}: root folder {path}', 'APPLY' if apply else 'PREVIEW')
            if apply:
                request(app,key,'/rootfolder','POST',payload)
                if not any(r.get('path','').rstrip('/')==path and r.get('accessible') is not False for r in request(app,key,'/rootfolder')):
                    raise RuntimeError('Root folder was not retained or is inaccessible')
        except (RuntimeError,ValueError,OSError,KeyError) as exc:
            print(f'{app}: root folder incomplete ({exc})');failures.append(app)
    if failures:raise RuntimeError('Root folder setup incomplete: '+', '.join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description='NekoServer Prowlarr source and application connector')
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--data-root', default=None, help='SSD mount with appdata')
    parser.add_argument('--apply', action='store_true', help='Write configurations through application APIs')
    parser.add_argument('--indexers', action='store_true', help='Choose from Prowlarr live catalog')
    parser.add_argument('--applications', action='store_true', help='Link Sonarr/Radarr/Lidarr to Prowlarr')
    parser.add_argument('--root-folders',action='store_true')
    parser.add_argument('--media-root')
    parser.add_argument('--download-clients', action='store_true', help='Set qBittorrent in Sonarr/Radarr/Lidarr')
    args = parser.parse_args(argv)
    config=json.loads(Path(args.config).read_text())
    selected=config.get('media',{}).get('services',list(PORTS)+['qbittorrent'])
    apps=tuple(x for x in ('sonarr','radarr','lidarr') if x in selected)
    root = Path(args.data_root or config['data_mount'])
    if not root.is_dir() or root.is_symlink() or not os.path.ismount(root):
        parser.error('Data SSD must be mounted at --data-root')
    actions = args.indexers or args.applications or args.download_clients or args.root_folders
    if not actions:
        parser.error('Select one or more of --indexers, --applications, --download-clients')
    print('NekoServer media connector:', 'APPLY' if args.apply else 'PREVIEW (no writes)')
    failures=[]
    actions=[]
    if args.root_folders: actions.append(('root folders',lambda:configure_root_folders(root,args.media_root or config['media_mount'],args.apply,apps)))
    if args.indexers: actions.append(('indexers',lambda:configure_indexers(root,args.apply,config)))
    if args.applications: actions.append(('applications',lambda:configure_applications(root,args.apply,apps)))
    if args.download_clients: actions.append(('download clients',lambda:configure_download_clients(root,args.apply,apps)))
    for label,action in actions:
        try:
            if label in ('indexers','applications') and 'prowlarr' not in selected:
                raise RuntimeError('Prowlarr is not selected for deployment')
            if label=='download clients' and 'qbittorrent' not in selected:
                raise RuntimeError('qBittorrent is not selected for deployment')
            action()
        except (RuntimeError, ValueError, OSError, KeyboardInterrupt, EOFError) as exc:
            # Ctrl+C or Ctrl+D: nobody is answering any more, so stop asking.
            print(f'{label}: incomplete ({str(exc) or "stopped"})',file=sys.stderr)
            failures.append(label)
            if isinstance(exc,(KeyboardInterrupt,EOFError)): break
    return 1 if failures else 0

if __name__ == '__main__':
    sys.exit(main())
