"""Run onboarding from the persistent installer after media deployment."""

import guided_setup
import json
import time
import account_store
import setup_progress
import subprocess
import sys
from pathlib import Path
from neko_common import (
    MEDIA_APPS,
    OPTIONAL_QUESTIONS,
    VERSION,
    app_port,
    app_ports,
    atomic_json,
    optional_answers,
    remember_answer,
    safe_path,
)

DEPENDENCIES = ['media', 'recovery']
DESCRIPTION = 'Wait for media startup, collect sources and run guided configuration/readiness reporting'


def run(ctx):
    settings = ctx.config.get('onboarding', {})
    if not ctx.apply or not settings.get('enabled', True):
        ctx.log('Guided media onboarding deferred')
        return
    if account_store.enabled(ctx.config) and 'accounts' in getattr(ctx, 'plan', ()):
        required = account_store.selected(ctx.config, ['media'])
        if any(not account_store.get(ctx.config, app, verified=True) for app in required):
            return {
                'status': 'needs_setup',
                'reason': 'Complete local media account setup before connecting applications',
            }
    release = safe_path(Path(ctx.config['data_mount']) / 'installer' / 'releases' / ('v' + VERSION))
    script = release / 'onboard.py'
    if not script.is_file():
        raise RuntimeError('Persistent onboarding script missing')
    config = release / 'config.json'
    wait = int(settings.get('wait_seconds', 180))
    if not 0 <= wait <= 600:
        raise RuntimeError('Onboarding wait must be 0..600 seconds')
    cmd = [sys.executable, str(script), '--config', str(config), '--wait', str(wait)]
    if sys.stdin.isatty():
        cmd += ['--configure', '--apply']
        if settings.get('configure_libraries', True) and 'jellyfin' in ctx.config.get('media', {}).get('services', []):
            cmd.append('--libraries')
        services = ctx.config.get('media', {}).get('services', [])
        if (
            settings.get('configure_request_providers', True)
            and 'seerr' in services
            and set(services) & {'sonarr', 'radarr'}
        ):
            cmd.append('--request-providers')
        optional = []
        if settings.get('collect_sources', True):
            optional.append('sources')
        if settings.get('configure_indexers', True) and 'prowlarr' in services:
            optional.append('indexers')
        if not playback_record(ctx.config).exists():
            optional.append('playback')
        ctx.optional_kept = optional_kept(ctx, optional)
        cmd += ['--' + question for question in optional if question not in ctx.optional_kept + ['playback']]
        # With the lock backups and monthly updates take: a nightly backup stopping the apps
        # while they are being connected would fail them half-way.
        from recovery_tools import lock

        with (
            lock(ctx.config),
            setup_progress.in_terminal('Connecting the media apps: your download sources and sign-ins'),
        ):
            result = subprocess.run(cmd, check=False).returncode
    else:
        cmd.append('--report-only')
        result = subprocess.run(cmd, check=False).returncode
        if not result and not connected_before(ctx):
            # The check without a terminal sees the apps answer, not the connections
            # between them, and can't make them: an unfinished step stays unfinished
            # (seen on a VM upgraded from 0.47.6: this step passed, with Lidarr's music
            # folder still missing, and the monthly check failed on it every night).
            remove_stale(ctx, release, config)
            return {
                'status': 'needs_setup',
                'reason': 'Connecting the media apps is not finished, and it asks you questions: run  '
                'sudo bash install.sh  in a terminal',
            }
    remove_stale(ctx, release, config)
    if result:
        # Something to fix, said with its reason (on the finishing screen too), not a
        # failed step: what is left is finished in the apps, then setup runs again.
        why = first_problem(ctx.config)
        ports = app_ports(MEDIA_APPS)
        guided_setup.checkpoint(
            ctx,
            'Finish media provider setup',
            [
                'Read the onboarding results above to identify the remaining configuration.',
                'Sign into the relevant opened applications using their saved app accounts. '
                'Finish selected provider/indexer credentials or any provider verification in its own page.',
                'Run  sudo bash install.sh  again afterwards: it checks the configuration again.',
            ],
            [
                ('url', 'http://127.0.0.1:' + str(ports[a]) + '/')
                for a in ctx.config.get('media', {}).get('services', [])
                if a in ports
            ],
        )
        ctx.log('Media deployed; connecting the apps is not finished: ' + why)
        return {
            'status': 'needs_setup',
            'reason': 'Connecting the media apps is not finished: '
            + why
            + '. Fix that, then run  sudo bash install.sh  again',
        }
    ctx.log('Onboarding configuration checks complete')
    witness_playback(ctx)


def optional_kept(ctx, asked):
    """Setup run again: shows the last answer to each optional question it would ask now
    (the download sources, the indexers, the playback test) and asks whether to ask them
    again. The questions not to ask: none when no answer was kept yet (the first run, or
    0.47.8 and before) or on yes; the ones shown on no (Ctrl+D counts as no). A question
    with no kept answer is asked either way."""
    kept = optional_answers(ctx.config['data_mount'])
    shown = [question for question in asked if question in kept]
    if not shown:
        return []
    print()
    print('Last time, the optional questions were answered this way:')
    for question in shown:
        print(f'  {OPTIONAL_QUESTIONS[question]}: {kept[question]}')
    print('Answer no to keep these answers; yes asks the questions again.')
    again = guided_setup.yes_no('Ask these questions again?')
    print()
    ctx.log('Optional questions: ' + ('asked again' if again else 'kept as answered last time'))
    return [] if again else shown


def connected_before(ctx):
    """Whether an earlier run finished connecting the apps (True when that is not known)."""
    previous = getattr(ctx, 'previous', None)
    if not isinstance(previous, dict):
        return True
    return previous.get('onboarding', {}).get('status') == 'success'


# The apps that other apps connect to. Taken out of media.services, their containers
# are stopped (modules/media.py), and the connections setup made to them are taken away.
CONNECTED = ('sonarr', 'radarr', 'lidarr', 'qbittorrent')
# Connections to these are not taken away by setup (they were not made by NekoServer
# alone, or can't be told apart from yours), so they are named for you to remove:
# {app: (the apps that keep them, what to do)}.
BY_HAND = {
    'prowlarr': (
        ('sonarr', 'radarr', 'lidarr'),
        (
            'Prowlarr is no longer in media.services: the indexers it added to Sonarr, Radarr and Lidarr '
            'still point at it and will show errors. Remove them in each app (Settings, Indexers), or add your indexers there'
        ),
    ),
    'jellyfin': (
        ('seerr',),
        (
            'Jellyfin is no longer in media.services: Seerr is still linked to it and cannot check its '
            'server. Link Seerr to your media server again (Seerr, Settings), or take Seerr out of media.services too'
        ),
    ),
}


def remove_stale(ctx, release, config):
    """Take away the connections setup made to an app taken out of media.services (one
    whose Compose file is still on the SSD): Prowlarr's link to it, the qBittorrent
    client in Sonarr, Radarr and Lidarr, Seerr's request provider. Left in place, the apps
    would show errors about it for good. Only connections setup made are removed (by
    their name and where they point); never a problem for this step, only said."""
    services = ctx.config.get('media', {}).get('services', [])
    compose = Path(ctx.config['data_mount']) / 'compose'
    removed = [a for a in CONNECTED if a not in services and (compose / a / 'compose.yaml').is_file()]
    for app, (users, what) in BY_HAND.items():
        if app not in services and (compose / app / 'compose.yaml').is_file() and set(users) & set(services):
            ctx.log(what)
    if not removed:
        return
    ctx.log('Taking away the connections setup made to apps no longer in media.services: ' + ', '.join(removed))
    cmd = [sys.executable, str(release / 'media-connect.py'), '--config', str(config), '--remove-stale', '--apply']
    if subprocess.run(cmd, check=False).returncode:
        ctx.log(
            'Some connections to the removed apps could not be checked (the messages above say which). '
            'They only cause warnings in the other apps; remove them there, or run  sudo bash install.sh  again'
        )


# Said for the connector's row, which has no reason of its own: its messages are above.
CONNECTOR = (
    'the app connections (Prowlarr, the download client, the library folders, or a quality profile '
    'that Enter skipped; the messages above say which)'
)


def first_problem(config):
    """The first check onboarding's report (reports/onboarding.json on the SSD) did not
    pass, with its reason; a failed one before one still to set up. Tests only a person
    can do (a real playback) are left out: they never hold setup up."""
    from media_readiness import WITNESSED

    try:
        rows = json.loads((Path(config['data_mount']) / 'reports' / 'onboarding.json').read_text()).get('checks', [])
    except (OSError, ValueError, AttributeError):
        rows = []
    rows = [r for r in rows if isinstance(r, dict) and r.get('status') != 'pass' and r.get('check') not in WITNESSED]
    for wanted in ('fail', 'needs_setup'):
        row = next((r for r in rows if r.get('status') == wanted), None)
        if row is not None:
            return problem_line(row)
    return 'see the messages above'


def problem_line(row):
    """One report row said as "check: reason"."""
    if row.get('check') == 'connector':
        return CONNECTOR
    return str(row.get('check')) + (': ' + str(row['reason']) if row.get('reason') else '')


def playback_record(config):
    return Path(config['data_mount']) / 'reports/onboarding/playback.json'


def witness_playback(ctx, checkpoint=None):
    """Ask once for a real playback test. It is evidence only a person can give, so it
    never blocks setup: a yes is recorded, otherwise the finishing screen says it is
    still to do."""
    record = playback_record(ctx.config)
    if record.exists():
        return
    kept = getattr(ctx, 'optional_kept', None)
    if isinstance(kept, list) and 'playback' in kept:  # a rerun that said no to asking again
        ctx.log('Playback test: skipped last time and not asked again (optional)')
        return
    services = ctx.config.get('media', {}).get('services', [])
    try:
        done = (checkpoint or guided_setup.checkpoint)(
            ctx,
            'Test playback (optional)',
            [
                'Using media you are allowed to use, request or add something, let it download and import, '
                'and play it in Jellyfin' + (' (and a song in Navidrome)' if 'navidrome' in services else '') + '.',
                'Answer yes only after it actually played. Press Ctrl+C to skip; setup still finishes '
                'and reminds you that playback is untested.',
            ],
            [('url', f"http://127.0.0.1:{app_port('jellyfin')}/")],
            keys=guided_setup.SKIPPABLE,
        )
    except KeyboardInterrupt:
        done = False
    if done:
        atomic_json(record, {'confirmed_at': time.time(), 'evidence': 'user_reported'})
        ctx.log('Playback test confirmed')
    else:
        remember_answer(ctx.config['data_mount'], 'playback', 'skipped')
        ctx.log('Playback verification pending (optional): test a real playback when convenient')
