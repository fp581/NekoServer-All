#!/usr/bin/env python3
"""Favourite shows and search in Jellyfin, tried against the real apps.

The unit tests (tests/test_favourite_shows.py, tests/test_jellyfin_requests.py) answer
as the apps' source code says they do. This runs the same code against the apps
themselves: Jellyfin, Sonarr and Seerr started from NekoServer's own Compose files
(.github/workflows/real-apps.yml starts them; it needs the internet, for Sonarr's show
list, Seerr's search and the Jellyfin Enhanced plugin).

  python3 lab/real_apps.py ROOT

ROOT is the data folder the apps were started with (NEKO_DATA and NEKO_MEDIA). It is
set up as a person would set it up: one show in the library, with two of its episodes
on disk, marked as a favourite. Then:

  1. Favourite shows (favourite_shows.run_once, as the hourly job runs it): Sonarr
     follows the show in its own folder, and the episodes that aired and are not on
     disk are watched and searched for. The two on disk are not.
  2. Search and request in Jellyfin (media_libraries.search_and_request_rows, as setup
     runs it): Jellyfin Enhanced is installed and connected to Seerr; Jellyfin's search
     then finds a show the server does not have, through Seerr.

Each check prints PASS or FAIL with what it saw. A FAIL stops that feature's checks; the
other feature is still tried (exit 1 when either failed)."""

import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import favourite_shows  # noqa: E402
import jellyfin_requests  # noqa: E402
import media_libraries  # noqa: E402
import seerr_providers  # noqa: E402
from media_libraries import Client  # noqa: E402

# The show used: its TVDB ID is how Jellyfin, Sonarr and NekoServer agree which show it is.
SHOW = {'title': 'Severance', 'year': 2022, 'tvdb': '371980'}
ON_DISK = {(1, 1), (1, 2)}  # (season, episode) of the files made for it
# The administrator made in Jellyfin's first-run steps; a throwaway server, a throwaway password.
ADMIN = {'Username': 'neko', 'Pw': 'real-apps-test-only'}
# A show that is not on the server, to be found by Jellyfin's search through Seerr.
NOT_ON_THE_SERVER = 'Breaking Bad'


class Failed(Exception):
    pass


def section(text):
    print(f'\n== {text}', flush=True)


def check(condition, text, saw=None):
    if not condition:
        raise Failed(text + (f'\n   saw: {saw}' if saw is not None else ''))
    print(f'PASS: {text}', flush=True)


def wait_for(what, look, seconds=300):
    """look() until it gives something, for up to `seconds`. The apps take a while to
    start, scan and download; an app not answering yet is waited for too."""
    deadline = time.monotonic() + seconds
    last = None
    while True:
        try:
            found = look()
        except RuntimeError as exc:
            found, last = None, exc
        if found:
            return found
        if time.monotonic() > deadline:
            raise Failed(f'{what}: not within {seconds} seconds' + (f' (last answer: {last})' if last else ''))
        time.sleep(5)


class Seerr:
    """Seerr's API with a session, as its first-run sign-in needs (Client has no cookies)."""

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, endpoint, method='GET', payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            'http://127.0.0.1:5055/api/v1' + endpoint,
            data=body,
            method=method,
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        )
        try:
            with self.opener.open(request, timeout=60) as answer:
                data = answer.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'seerr: HTTP {exc.code}: {exc.read()[:300]!r}') from None
        except OSError as exc:
            raise RuntimeError(f'seerr: {exc}') from None
        return json.loads(data) if data.strip() else {}


def make_the_show(root):
    """The show's folder as a person's library has it: tvshow.nfo naming the show (as
    Jellyfin's own NFO files do), and two short episodes."""
    folder = root / 'library/tv' / f"{SHOW['title']} ({SHOW['year']})"
    season = folder / 'Season 01'
    season.mkdir(parents=True, exist_ok=True)
    (folder / 'tvshow.nfo').write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<tvshow>\n'
        f"  <title>{SHOW['title']}</title>\n  <year>{SHOW['year']}</year>\n"
        f"  <tvdbid>{SHOW['tvdb']}</tvdbid>\n"
        f'  <uniqueid type="tvdb" default="true">{SHOW["tvdb"]}</uniqueid>\n</tvshow>\n'
    )
    for season_number, episode in sorted(ON_DISK):
        video = season / f"{SHOW['title']} S{season_number:02d}E{episode:02d}.mkv"
        subprocess.run(
            ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=5']
            + ['-c:v', 'libx264', str(video)],
            check=True,
        )
    print(f'made {folder} with episodes {sorted(ON_DISK)}')


def jellyfin_first_run():
    """Jellyfin's first-run steps (its startup wizard's API), then signed in as the
    administrator. Returns (the signed-in client, the administrator's user ID)."""
    jellyfin = Client('jellyfin')
    wait_for('Jellyfin answers', lambda: jellyfin.call('/System/Info/Public').get('Version'))
    print('Jellyfin', jellyfin.call('/System/Info/Public')['Version'])
    wait_for('Jellyfin has its first user', lambda: jellyfin.call('/Startup/User').get('Name'))
    jellyfin.call(
        '/Startup/Configuration',
        'POST',
        {'UICulture': 'en-US', 'MetadataCountryCode': 'US', 'PreferredMetadataLanguage': 'en'},
    )
    jellyfin.call('/Startup/User', 'POST', {'Name': ADMIN['Username'], 'Password': ADMIN['Pw']})
    jellyfin.call('/Startup/Complete', 'POST')
    signed_in = jellyfin.call('/Users/AuthenticateByName', 'POST', ADMIN)
    jellyfin.headers['X-Emby-Token'] = signed_in['AccessToken']
    return jellyfin, signed_in['User']['Id']


def the_show_in_jellyfin(jellyfin, user):
    """The show as Jellyfin lists it once it scanned the library, with its TVDB ID."""
    query = urllib.parse.urlencode(
        {'userId': user, 'includeItemTypes': 'Series', 'recursive': 'true', 'fields': 'ProviderIds,Path'}
    )
    items = jellyfin.call('/Items?' + query).get('Items', [])
    return next((i for i in items if (i.get('ProviderIds') or {}).get('Tvdb') == SHOW['tvdb']), None)


def set_up_jellyfin(root):
    section('Jellyfin: first run, a TV library, the show marked as a favourite')
    jellyfin, user = jellyfin_first_run()
    check(True, 'Jellyfin first-run steps done; signed in as its administrator')
    media_libraries.ensure_readiness_key(jellyfin, root)
    check(media_libraries.readiness_key_path(root).read_text().strip() != '', "NekoServer's Jellyfin key saved")
    query = urllib.parse.urlencode(
        {'name': 'TV Shows', 'collectionType': 'tvshows', 'paths': '/data/tv', 'refreshLibrary': 'true'}
    )
    jellyfin.call('/Library/VirtualFolders?' + query, 'POST', media_libraries.new_library_options())
    libraries = media_libraries.folders(jellyfin)
    check(
        bool(media_libraries.matching(libraries, 'tv', 'TV Shows', 'tvshows')),
        'Jellyfin keeps a TV library at /data/tv (as setup checks it)',
        [(f.get('Name'), f.get('Locations')) for f in libraries],
    )
    try:
        show = wait_for('Jellyfin finds the show with its TVDB ID', lambda: the_show_in_jellyfin(jellyfin, user))
    except Failed as exc:
        everything = jellyfin.call(
            '/Items?' + urllib.parse.urlencode({'userId': user, 'recursive': 'true', 'fields': 'ProviderIds,Path'})
        ).get('Items', [])
        raise Failed(
            f'{exc}\n   Jellyfin lists: '
            + json.dumps([(i.get('Type'), i.get('Name'), i.get('Path'), i.get('ProviderIds')) for i in everything])
        ) from None
    check(True, f"Jellyfin lists {show['Name']} (TVDB {SHOW['tvdb']}) at {show.get('Path')}")
    has = wait_for(
        'Jellyfin lists both episodes',
        lambda: favourite_shows.episodes_jellyfin_has(jellyfin, show['Id'], user) == ON_DISK,
    )
    check(has, f'Jellyfin has episodes {sorted(ON_DISK)} as files')
    jellyfin.call(f"/UserFavoriteItems/{show['Id']}?userId={user}", 'POST')
    check(
        [s['tvdb'] for s in favourite_shows.jellyfin_favourites(jellyfin)] == [SHOW['tvdb']],
        'the show is a favourite',
    )
    return jellyfin, show


def set_up_sonarr(root):
    section("Sonarr: its key, and NekoServer's library as its root folder")
    key = wait_for('Sonarr writes its key', lambda: seerr_providers.app_key(root, 'sonarr'))
    sonarr = Client('sonarr')
    sonarr.headers['X-Api-Key'] = key
    status = wait_for('Sonarr answers', lambda: sonarr.call('/system/status'))
    print('Sonarr', status.get('version'))
    sonarr.call('/rootfolder', 'POST', {'path': '/data/tv'})
    check([r['path'].rstrip('/') for r in sonarr.call('/rootfolder')] == ['/data/tv'], 'Sonarr saves into /data/tv')
    return sonarr


def set_up_seerr(root):
    """Seerr's first run as setup does it (account_setup.py): signed in with Jellyfin's
    administrator, which links Seerr to Jellyfin, then initialised."""
    section('Seerr: first run, linked to Jellyfin')
    seerr = Seerr()
    wait_for('Seerr answers', lambda: seerr.call('/settings/public'))
    seerr.call(
        '/auth/jellyfin',
        'POST',
        {
            'username': ADMIN['Username'],
            'password': ADMIN['Pw'],
            'email': 'neko@example.org',
            'hostname': 'jellyfin',
            'port': 8096,
            'useSsl': False,
            'urlBase': '',
            'serverType': 2,
        },
    )
    seerr.call('/settings/initialize', 'POST')
    check(seerr.call('/settings/public').get('initialized') is True, 'Seerr is set up and linked to Jellyfin')
    check(bool(media_libraries.seerr_key(root)), "Seerr's key is in its settings")


def favourite_shows_test(root, sonarr):
    section('Favourite shows keep coming (favourite_shows.run_once, as the hourly job runs it)')
    config = {'data_mount': str(root), 'media': {'services': ['jellyfin', 'sonarr', 'seerr']}}
    first = favourite_shows.run_once(config)
    print(json.dumps(first, indent=1))
    followed = first['followed']
    check(
        [f['title'] for f in followed] == [SHOW['title']] and not first['not_followed'],
        'the favourite is given to Sonarr',
        first,
    )
    folder = f"/data/tv/{SHOW['title']} ({SHOW['year']})"
    check(followed[0]['saved_in'] == folder and not followed[0].get('apart'), f'in its own folder, {folder}', followed)
    series = next(s for s in sonarr.call('/series') if str(s.get('tvdbId')) == SHOW['tvdb'])
    check(series['path'].rstrip('/') == folder and series['monitorNewItems'] == 'all', 'Sonarr follows new seasons')

    filled = first['filled']
    started = time.monotonic()
    while not filled and time.monotonic() - started < 300:
        time.sleep(10)  # Sonarr lists the show's episodes after it read its show list
        again = favourite_shows.run_once(config)
        check(again['already'] == [SHOW['title']] and not again['followed'], 'a second run gives it to Sonarr once')
        filled = again['filled']
    check(bool(filled), 'the missing episodes were asked for, once Sonarr listed them', filled)

    episodes = sonarr.call(f"/episode?seriesId={series['id']}")
    now = time.time()
    missing = {
        e['id']
        for e in episodes
        if e['seasonNumber'] > 0
        and favourite_shows.aired(e, now)
        and (e['seasonNumber'], e['episodeNumber']) not in ON_DISK
    }
    print(f'Sonarr lists {len(episodes)} episodes; {len(missing)} aired and are not on disk')
    check(len(missing) > 0 and filled[0]['episodes'] == len(missing), 'every aired episode not on disk', filled)
    watched = {e['id'] for e in episodes if e['monitored']}
    check(missing <= watched, 'each one is watched in Sonarr')
    on_disk = [e for e in episodes if (e['seasonNumber'], e['episodeNumber']) in ON_DISK]
    check(
        len(on_disk) == len(ON_DISK) and not any(e['monitored'] for e in on_disk),
        'the two on disk are not',
        [(e['seasonNumber'], e['episodeNumber'], e['monitored'], e['hasFile'], e.get('airDateUtc')) for e in on_disk],
    )
    searches = [c for c in sonarr.call('/command') if c.get('name') == 'EpisodeSearch']
    asked = {i for c in searches for i in (c.get('body') or {}).get('episodeIds', [])}
    check(asked == missing, 'and Sonarr was told to search for exactly those', sorted(asked ^ missing))


def search_in_jellyfin_test(root, jellyfin):
    section('Search and request in Jellyfin (media_libraries.search_and_request_rows, as setup runs it)')
    rows = media_libraries.search_and_request_rows(jellyfin, root, True, True)
    check(rows == [{'check': jellyfin_requests.CHECK, 'status': 'pass'}], 'setup installs and connects it', rows)
    plugin = jellyfin_requests.installed(jellyfin)
    check(plugin and plugin['Status'] == 'Active', f"Jellyfin Enhanced {plugin and plugin['Version']} is active")
    settings = jellyfin.call(f"/Plugins/{plugin['Id']}/Configuration")
    wanted = jellyfin_requests.wanted_settings(root)
    check(all(settings.get(k) == v for k, v in wanted.items()), "its Seerr settings are setup's")
    rows = media_libraries.search_and_request_rows(jellyfin, root, True, True)
    check(rows[0]['status'] == 'pass', 'setup run again finds it as it left it', rows)

    status = jellyfin.call('/JellyfinEnhanced/jellyseerr/status')
    check(status.get('active') is True, 'the plugin reaches Seerr (http://seerr:5055, with its key)', status)
    query = urllib.parse.urlencode({'query': NOT_ON_THE_SERVER})
    found = wait_for(
        "Jellyfin's search answers through Seerr", lambda: jellyfin.call('/JellyfinEnhanced/jellyseerr/search?' + query)
    )
    results = found.get('results', []) if isinstance(found, dict) else []
    names = [r.get('name') or r.get('title') for r in results]
    print('found:', names[:8])
    check(
        any(r.get('mediaType') == 'tv' and r.get('name') == NOT_ON_THE_SERVER for r in results),
        f'searching "{NOT_ON_THE_SERVER}" in Jellyfin finds the show, with Seerr\'s request button',
        names,
    )


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = Path(argv[1])
    try:
        make_the_show(root)
        jellyfin, _show = set_up_jellyfin(root)
        sonarr = set_up_sonarr(root)
        set_up_seerr(root)
    except Failed as exc:
        print(f'FAIL: {exc}', flush=True)
        return 1
    # Each feature is tried even when the other failed: one run says how both went.
    failed = []
    for name, test in (
        ('Favourite shows keep coming', lambda: favourite_shows_test(root, sonarr)),
        ('Search and request in Jellyfin', lambda: search_in_jellyfin_test(root, jellyfin)),
    ):
        try:
            test()
        except Failed as exc:
            print(f'FAIL: {exc}', flush=True)
            failed.append(name)
    print('\nAll passed.' if not failed else '\nFailed: ' + ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
