"""Search and request in Jellyfin (README.md, "Search and request in Jellyfin").

Jellyfin's own search also finds films and shows the server does not have: they come from
Seerr, with a button to ask for them, and Seerr has them downloaded. This is the Jellyfin
Enhanced plugin (github.com/n00bcodr/Jellyfin-Enhanced), set up here with Jellyfin's API:
its repository added, the plugin installed and Jellyfin restarted for it, then its Seerr
connection made (Seerr on the apps' own network, with Seerr's key, each Jellyfin user
added to Seerr the first time they search). It works in Jellyfin in a browser and in the
official phone apps, not in TV apps (the plugin's own FAQ).

A step that only checks (setup without --apply) says what it would do and changes
nothing. Switched off (onboarding.search_and_request_in_jellyfin: false), the plugin is
neither installed nor changed; one installed before stays as it is, for the person to
remove in Jellyfin (Dashboard, Plugins).
"""

import time
import urllib.parse

from media_libraries import SetupRequired, row, seerr_key

NAME = 'Jellyfin Enhanced'
# The plugin's own ID (its manifest): it is installed and found by this, never by a name
# another plugin could also have.
GUID = 'f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b'
# The plugin's repository: one list for Jellyfin 12 and 10.11, from which Jellyfin takes
# the build made for itself (the plugin's installation guide).
REPOSITORY = 'https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/manifest.json'
# Seerr as Jellyfin's container reaches it: by its name on the apps' network
# (compose/*.yaml: nekoserver-media), as Seerr reaches Jellyfin.
SEERR = 'http://seerr:5055'
CHECK = 'jellyfin:search_and_request'
# How long Jellyfin may take to install the plugin, and to start again after.
WAIT = 180


def same_id(a, b):
    return str(a or '').replace('-', '').lower() == b.replace('-', '').lower()


def installed(client):
    """The plugin as Jellyfin lists it ({Id, Version, Status, ...}), or None."""
    plugins = client.call('/Plugins')
    if not isinstance(plugins, list):
        raise RuntimeError('jellyfin: unsupported list of plugins')
    return next((p for p in plugins if isinstance(p, dict) and same_id(p.get('Id'), GUID)), None)


def wait_for(what, check, clock, sleep):
    """check() until it gives something, for up to WAIT seconds."""
    deadline = clock() + WAIT
    while True:
        try:
            found = check()
        except RuntimeError:  # Jellyfin starting again does not answer for a while
            found = None
        if found:
            return found
        if clock() >= deadline:
            raise SetupRequired(f'Jellyfin did not {what} within {WAIT // 60} minutes; run setup again')
        sleep(5)


def install(client, log, clock, sleep):
    """The plugin's repository added (the person's own repositories are kept), the
    plugin installed, and Jellyfin restarted for it. Returns the plugin as listed."""
    repositories = client.call('/Repositories')
    if not isinstance(repositories, list):
        raise RuntimeError('jellyfin: unsupported list of plugin repositories')
    if not any(isinstance(r, dict) and r.get('Url') == REPOSITORY for r in repositories):
        client.call('/Repositories', 'POST', [*repositories, {'Name': NAME, 'Url': REPOSITORY, 'Enabled': True}])
    query = urllib.parse.urlencode({'assemblyGuid': GUID, 'repositoryUrl': REPOSITORY})
    try:
        client.call(f'/Packages/Installed/{urllib.parse.quote(NAME)}?{query}', 'POST')
    except RuntimeError as exc:
        if str(exc) != 'jellyfin: HTTP 404':
            raise
        # Jellyfin finds no build of it for this Jellyfin (a new Jellyfin, a day before
        # the plugin's build for it), or could not read the plugin's repository.
        raise SetupRequired(
            'Jellyfin found no Jellyfin Enhanced for this Jellyfin version in the plugin repository (or could not '
            'reach it): searching and asking for films in Jellyfin waits; Seerr itself works as before. Setup tries '
            'again when it runs next'
        ) from None
    wait_for('install Jellyfin Enhanced', lambda: installed(client), clock, sleep)
    log('jellyfin: Jellyfin Enhanced installed; Jellyfin starts again for it (a minute)')
    client.call('/System/Restart', 'POST')
    sleep(10)  # it stops answering first

    def loaded():
        """The plugin once Jellyfin has started again with it (no longer waiting for it)."""
        plugin = installed(client)
        return plugin if plugin and plugin.get('Status') != 'Restart' else None

    return wait_for('start again with Jellyfin Enhanced', loaded, clock, sleep)


def wanted_settings(root):
    """The plugin's settings for Seerr, as its configuration names them."""
    return {
        'JellyseerrEnabled': True,
        'JellyseerrShowSearchResults': True,
        'JellyseerrUrls': SEERR,
        'JellyseerrApiKey': seerr_key(root),
        'JellyseerrAutoImportUsers': True,
    }


def connect(client, root, apply, wanted=True, log=print, clock=None, sleep=None):
    """The rows for setup's report. `client`: Jellyfin, signed in as its administrator.
    `clock`, `sleep`: time.monotonic and time.sleep unless a test gives its own."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    if not wanted:
        return [row(CHECK, 'pass', 'Switched off (onboarding.search_and_request_in_jellyfin)')]
    plugin = installed(client)
    if plugin is None:
        if not apply:
            return [row(CHECK, 'needs_setup', 'Preview: Jellyfin Enhanced would be installed and connected to Seerr')]
        plugin = install(client, log, clock, sleep)
    if plugin.get('Status') != 'Active':
        # NotSupported: no build for this Jellyfin yet; Malfunctioned, Disabled: said as they are.
        raise SetupRequired(
            f'Jellyfin Enhanced is {plugin.get("Status") or "not active"} in Jellyfin (Dashboard, Plugins): '
            'searching and asking for films there waits until it is active; Seerr itself works as before'
        )
    endpoint = f'/Plugins/{plugin["Id"]}/Configuration'
    settings = client.call(endpoint)
    if not isinstance(settings, dict):
        raise RuntimeError('jellyfin: unsupported plugin settings')
    wanted_values = wanted_settings(root)
    changes = {k: v for k, v in wanted_values.items() if settings.get(k) != v}
    if changes and not apply:
        return [row(CHECK, 'needs_setup', 'Preview: Jellyfin Enhanced would be connected to Seerr')]
    if changes:
        client.call(endpoint, 'POST', {**settings, **changes})
        kept = client.call(endpoint)
        if not isinstance(kept, dict) or any(kept.get(k) != v for k, v in wanted_values.items()):
            raise RuntimeError('Jellyfin Enhanced did not keep its Seerr settings')
        log('jellyfin: search and request in Jellyfin is on (Jellyfin Enhanced, connected to Seerr)')
    return [row(CHECK, 'pass')]
