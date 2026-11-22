#!/usr/bin/env python3
"""Favourite shows keep coming (README.md, "Favourite shows keep coming").

A show someone marks as a favourite in Jellyfin gets its missing episodes and its new
ones: every hour this looks at each Jellyfin user's favourite shows, and one Sonarr does
not follow yet is given to Sonarr to watch for episodes that have not aired yet. Once
Sonarr has the show's list of episodes (a little after it was added: the next run), the
episodes that aired and that Jellyfin does not have are asked for too. What Jellyfin has
is what counts: a show in a folder of the person's that Sonarr cannot see is not
downloaded again. Nothing is ever taken away: a show that stops being a favourite stays
followed (Sonarr is where to stop it).

A show requested through Seerr is followed already; this is for the shows that came from
a person's own folders (the Media folders page), which Sonarr did not know.

The show is added with Seerr's quality profile, in the show's own folder when Sonarr may
save there (NekoServer's library, or a folder of the person's chosen for new downloads):
the new episodes then land beside the old ones, as one show. A show in a folder of the
person's that the apps may only read gets its new episodes in the folder requests are
saved in, under a folder of the same name; Jellyfin shows them there (in TV Shows), and
the account says so.

    sudo python3 favourite_shows.py run --config <settings>   (the hourly timer runs this)
"""

import argparse
import calendar
import json
import sys
import time
import urllib.parse
from pathlib import Path, PurePosixPath

import media_readiness
import seerr_providers
from media_libraries import Client, SetupRequired, open_seerr
from neko_common import atomic_json, media_services, read_settings, safe_path

# Where the last run's account is kept, for the dashboard and for a person to read.
REPORT = Path('reports') / 'media' / 'favourite-shows.json'
# The shows given to Sonarr whose missing episodes are still to be asked for, kept from
# one run to the next: {tvdb: {title, show (Jellyfin's ID), user (one who likes it)}}.
TO_FILL = Path('installer') / 'state' / 'favourite-shows-to-fill.json'
FORMAT = 'neko-favourite-shows-v1'

# Sonarr: watch only episodes that have not aired yet, and every new season; search for
# nothing now (what the person has stays as it is, nothing old is fetched).
# Sonarr's list of shows (GET) and where a show is added (POST).
SERIES = '/series'
FUTURE_ONLY = {'monitor': 'future', 'searchForMissingEpisodes': False, 'searchForCutoffUnmetEpisodes': False}


def favourites_of(jellyfin, user_id):
    """The shows one Jellyfin user marked as a favourite, as Jellyfin lists them."""
    query = urllib.parse.urlencode(
        {
            'userId': user_id,
            'includeItemTypes': 'Series',
            'recursive': 'true',
            'filters': 'IsFavorite',
            'fields': 'ProviderIds,Path',
        }
    )
    found = jellyfin.call('/Items?' + query)
    items = found.get('Items') if isinstance(found, dict) else None
    if not isinstance(items, list):
        raise RuntimeError('jellyfin: unsupported list of favourites')
    return [item for item in items if isinstance(item, dict)]


def show_of(item, user_id):
    """A show as this job keeps it: {tvdb, title, path, id, user, by}."""
    ids = item.get('ProviderIds') if isinstance(item.get('ProviderIds'), dict) else {}
    return {
        'tvdb': str(ids.get('Tvdb') or '').strip(),
        'title': str(item.get('Name') or '').strip() or 'A show',
        'path': str(item.get('Path') or '').rstrip('/'),
        'id': str(item.get('Id') or ''),
        'user': str(user_id),
        'by': [],
    }


def jellyfin_favourites(jellyfin):
    """[{tvdb, title, path, id, user, by}] for every show some Jellyfin user marked as a
    favourite, once per show (`by`: the users who did). `path` is the show's own folder
    as the apps see it, or '' when Jellyfin does not say."""
    users = jellyfin.call('/Users')
    if not isinstance(users, list):
        raise RuntimeError('jellyfin: unsupported list of users')
    shows = {}
    for user in users:
        if not isinstance(user, dict) or not user.get('Id'):
            continue
        for item in favourites_of(jellyfin, user['Id']):
            show = show_of(item, user['Id'])
            key = show['tvdb'] or 'name:' + show['title'].casefold()
            shows.setdefault(key, show)['by'].append(str(user.get('Name') or 'someone'))
    return list(shows.values())


def followed(sonarr):
    """The TVDB IDs of the shows Sonarr has already (followed or not: a show Sonarr has
    is the person's to manage there, and is never changed here)."""
    rows = sonarr.call(SERIES)
    if not isinstance(rows, list):
        raise RuntimeError('sonarr: unsupported list of shows')
    return {str(r.get('tvdbId')) for r in rows if isinstance(r, dict) and r.get('tvdbId')}


def seerr_profile(seerr):
    """The quality profile Seerr's own requests for shows use, or None."""
    standard = seerr_providers.current_standard(seerr.call(seerr_providers.settings_of('sonarr')))
    default = next((p for p in standard if p.get('isDefault')), standard[0] if standard else None)
    profile = default.get('activeProfileId') if isinstance(default, dict) else None
    return profile if type(profile) is int and profile > 0 else None


def choices(config, sonarr, seerr):
    """{folder: where requests are saved, as Sonarr sees it; roots: Sonarr's root folders
    (where it may save); profile: the quality profile's ID}: what a Seerr request for a
    show would use. Without Seerr, the profile setup picks for Seerr from Sonarr's own
    list (seerr_providers.STANDARD_PROFILES)."""
    folder = seerr_providers.request_folders(config)['sonarr'].rstrip('/')
    roots = {str(r.get('path') or '').rstrip('/') for r in sonarr.call('/rootfolder') or [] if isinstance(r, dict)}
    profile = seerr_profile(seerr) if seerr is not None else None
    if profile is None:
        profile = standard_profile(seerr_providers.profiles(sonarr.call('/qualityprofile')))
    return {'folder': folder, 'roots': roots, 'profile': profile}


def standard_profile(listed):
    """The ID of the profile setup would pick for Seerr: the first of
    seerr_providers.STANDARD_PROFILES that Sonarr has, else Sonarr's first."""
    by_name = {str(p['name']).casefold(): p for p in listed}
    for name in seerr_providers.STANDARD_PROFILES:
        if name.casefold() in by_name:
            return by_name[name.casefold()]['id']
    return listed[0]['id']


def place(show, match, chosen):
    """(Sonarr's root folder, the show's folder) for its new episodes: its own folder when
    that is in one of Sonarr's root folders, else a folder of the same name where requests
    are saved."""
    own = PurePosixPath(show['path']) if show['path'] else None
    if own is not None and str(own.parent) in chosen['roots']:
        return str(own.parent), str(own)
    name = own.name if own is not None else str(match.get('folder') or match.get('title') or show['title'])
    return chosen['folder'], chosen['folder'] + '/' + name.replace('/', ' ')


def follow(sonarr, show, chosen):
    """The show given to Sonarr, watching only episodes still to air. Returns the
    folder its new episodes are saved in."""
    found = sonarr.call('/series/lookup?' + urllib.parse.urlencode({'term': 'tvdb:' + show['tvdb']}))
    match = next((s for s in found or [] if isinstance(s, dict) and str(s.get('tvdbId')) == show['tvdb']), None)
    if match is None:
        raise SetupRequired('Sonarr could not find it in its show list (TVDB ' + show['tvdb'] + ')')
    root, folder = place(show, match, chosen)
    payload = dict(
        match,
        qualityProfileId=chosen['profile'],
        rootFolderPath=root,
        path=folder,
        monitored=True,
        seasonFolder=True,
        monitorNewItems='all',
        addOptions=dict(FUTURE_ONLY),
    )
    sonarr.call(SERIES, 'POST', payload)
    return folder


def episodes_jellyfin_has(jellyfin, show_id, user):
    """{(season, episode)} of the episodes of a show Jellyfin has as files (a file of two
    episodes counts for both); not the ones it only knows of."""
    query = urllib.parse.urlencode({'userId': user, 'isMissing': 'false', 'fields': 'Path'})
    found = jellyfin.call(f'/Shows/{urllib.parse.quote(show_id)}/Episodes?' + query)
    items = found.get('Items') if isinstance(found, dict) else None
    if not isinstance(items, list):
        raise RuntimeError('jellyfin: unsupported list of episodes')
    has = set()
    for item in items:
        season, first = item.get('ParentIndexNumber'), item.get('IndexNumber')
        if type(season) is not int or type(first) is not int:
            continue
        last = item.get('IndexNumberEnd') if type(item.get('IndexNumberEnd')) is int else first
        has.update((season, n) for n in range(first, max(first, last) + 1))
    return has


def aired(episode, now):
    """Whether Sonarr says the episode has aired already."""
    when = str(episode.get('airDateUtc') or '')
    try:
        return bool(when) and calendar.timegm(time.strptime(when[:19], '%Y-%m-%dT%H:%M:%S')) <= now
    except ValueError:
        return False


def fill(sonarr, jellyfin, series_id, wanted, now):
    """The episodes of a show Sonarr has that aired and that Jellyfin does not have,
    watched and searched for in Sonarr (specials left out). Returns how many, or None
    while Sonarr has no list of episodes yet."""
    episodes = sonarr.call(f'/episode?seriesId={series_id}')
    if not isinstance(episodes, list) or not episodes:
        return None
    has = episodes_jellyfin_has(jellyfin, wanted['show'], wanted['user'])
    missing = [
        e['id']
        for e in episodes
        if isinstance(e, dict)
        and type(e.get('id')) is int
        and e.get('seasonNumber', 0) > 0
        and not e.get('hasFile')
        and aired(e, now)
        and (e.get('seasonNumber'), e.get('episodeNumber')) not in has
    ]
    if missing:
        sonarr.call('/episode/monitor', 'PUT', {'episodeIds': missing, 'monitored': True})
        sonarr.call('/command', 'POST', {'name': 'EpisodeSearch', 'episodeIds': missing})
    return len(missing)


def fill_missing(root, sonarr, jellyfin, account, now):
    """Each show given to Sonarr whose missing episodes are still to be asked for: asked
    for once Sonarr has finished adding it. A show taken out of Sonarr since is let go."""
    state = safe_path(root / TO_FILL)
    try:
        to_fill = json.loads(state.read_text())
    except (OSError, ValueError):
        to_fill = {}
    if not isinstance(to_fill, dict) or not to_fill:
        return
    series = {str(r.get('tvdbId')): r for r in sonarr.call(SERIES) or [] if isinstance(r, dict)}
    still = {}  # the shows to look at again next run
    for tvdb, wanted in to_fill.items():
        if tvdb not in series or not isinstance(wanted, dict):
            continue  # taken out of Sonarr since: let go
        if series[tvdb].get('addOptions') is not None:
            # Sonarr is still adding it: once it has scanned the show's folder, it sets
            # which episodes it watches from the add options (aired ones off) and only
            # then drops them (Sonarr's SeriesScannedHandler). Episodes watched before
            # that would be switched off again: the next run.
            still[tvdb] = wanted
            continue
        try:
            asked = fill(sonarr, jellyfin, series[tvdb].get('id'), wanted, now)
        except (RuntimeError, OSError) as exc:  # tried again next hour
            account['not_filled'].append({'title': wanted.get('title', ''), 'why': str(exc)})
            still[tvdb] = wanted
            continue
        if asked is None:
            still[tvdb] = wanted  # Sonarr has not listed its episodes yet: the next run
            continue
        account['filled'].append({'title': wanted.get('title', ''), 'episodes': asked})
    atomic_json(state, still)


def remember_to_fill(root, show):
    """A show just given to Sonarr: its missing episodes are asked for once Sonarr has its
    list of episodes."""
    state = safe_path(root / TO_FILL)
    try:
        to_fill = json.loads(state.read_text())
    except (OSError, ValueError):
        to_fill = {}
    if not isinstance(to_fill, dict):
        to_fill = {}
    to_fill[show['tvdb']] = {'title': show['title'], 'show': show['id'], 'user': show['user']}
    atomic_json(state, to_fill)


def follow_one(root, sonarr, show, chosen, account):
    """One favourite Sonarr does not have, given to it; the account says how it went.
    One show's trouble never stops the others."""
    if not show['tvdb']:
        account['not_followed'].append(
            {'title': show['title'], 'why': 'Jellyfin does not know which show it is (no TVDB ID)'}
        )
        return
    try:
        saved_in = follow(sonarr, show, chosen)
    except (RuntimeError, OSError) as exc:
        account['not_followed'].append({'title': show['title'], 'why': str(exc)})
        return
    if show['id']:
        remember_to_fill(root, show)
    entry = {'title': show['title'], 'by': show['by'], 'saved_in': saved_in}
    if show['path'] and saved_in != show['path']:
        # In a folder of the person's the apps may only read: the new episodes are
        # elsewhere, and Jellyfin shows them in that library.
        entry['apart'] = True
    account['followed'].append(entry)


def follow_new_favourites(config, root, jellyfin, sonarr, seerr, account):
    """Every favourite Sonarr does not have yet, given to it."""
    shows = jellyfin_favourites(jellyfin)
    has = followed(sonarr)
    account['already'] = [s['title'] for s in shows if s['tvdb'] and s['tvdb'] in has]
    wanted = [s for s in shows if not s['tvdb'] or s['tvdb'] not in has]
    if not wanted:
        return
    chosen = choices(config, sonarr, seerr)
    for show in wanted:
        follow_one(root, sonarr, show, chosen, account)


def sign_in(root, jellyfin, sonarr, seerr):
    """Each app's key on its client: Jellyfin's NekoServer key (or Seerr's link to it),
    Sonarr's own; Seerr checked to be set up."""
    info = jellyfin.call('/System/Info/Public')
    settings = {}
    if seerr is not None:
        open_seerr(seerr, root, 'Seerr first-run setup is incomplete')
        path = root / 'appdata/seerr/settings.json'
        settings = json.loads(path.read_text()) if path.is_file() else {}
    jellyfin.headers['X-Emby-Token'] = media_readiness.jellyfin_key(root, settings, info.get('Id', ''))
    sonarr.headers['X-Api-Key'] = seerr_providers.app_key(root, 'sonarr')


def run_once(config, factory=Client, now=time.time):
    """One look at the favourites. Returns the account it saved: {followed, already,
    not_followed} with the shows in each (followed: given to Sonarr in this run)."""
    apps = media_services(config)
    account = {
        'format': FORMAT,
        'at': now(),
        'followed': [],
        'already': [],
        'not_followed': [],
        'filled': [],
        'not_filled': [],
    }
    if 'jellyfin' not in apps or 'sonarr' not in apps:
        account['said'] = 'Jellyfin and Sonarr are not both set up: nothing to follow'
        return account
    root = safe_path(config['data_mount'])
    jellyfin, sonarr = factory('jellyfin'), factory('sonarr')
    seerr = factory('seerr') if 'seerr' in apps else None
    try:
        sign_in(root, jellyfin, sonarr, seerr)
        follow_new_favourites(config, root, jellyfin, sonarr, seerr, account)
        fill_missing(root, sonarr, jellyfin, account, now())
    finally:
        for client in (jellyfin, sonarr, seerr):
            if client is not None:
                client.close()
    return account


def said(account):
    """The run in one line, for the journal."""
    if account.get('said'):
        return 'Favourite shows: ' + account['said']
    parts = [f'{len(account["followed"])} now followed', f'{len(account["already"])} followed already']
    if account['not_followed']:
        parts.append(f'{len(account["not_followed"])} could not be followed')
    asked = sum(f['episodes'] for f in account['filled'])
    if asked:
        parts.append(f'{asked} missing episodes asked for')
    return 'Favourite shows: ' + ', '.join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run',))
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    config = read_settings(args.config)
    report = safe_path(config['data_mount']) / REPORT
    try:
        account = run_once(config, factory=Client)
    except (RuntimeError, OSError, ValueError) as exc:
        # The apps not answering (stopped for a backup, say): the next hour tries again.
        # Kept in the account too, so a look that keeps failing (an app's API changed in
        # an update, say) is seen there, not only in the journal.
        atomic_json(report, {'format': FORMAT, 'at': time.time(), 'failed': str(exc)})
        print(f'Favourite shows: not looked at this time: {exc}')
        return 1
    atomic_json(report, account)
    print(said(account))
    for show in account['followed']:
        if show.get('apart'):
            print(f'  {show["title"]}: its new episodes are saved in {show["saved_in"]} (your folder is read-only)')
    for show in account['not_followed'] + account['not_filled']:
        print(f'  {show["title"]}: {show["why"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
