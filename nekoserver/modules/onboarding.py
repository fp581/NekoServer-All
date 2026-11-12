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
    VERSION,
    app_port,
    app_ports,
    atomic_json,
    apps_to_connect,
    media_services,
    remember_answer,
    safe_path,
)

DEPENDENCIES = ['media', 'recovery']
DESCRIPTION = 'Wait for media startup, collect sources and run guided configuration/readiness reporting'


DOCKER_DOWN = (
    'Docker is not running, so none of the apps can be reached and nothing could be connected. '
    'Start it (sudo systemctl start docker), then retry this step'
)


def docker_running(run=subprocess.run):
    """Docker answers: the apps this step connects run in it. Asked first, so a Docker
    that is down is said as that, with its fix, instead of each app as "unavailable"
    (seen on the 0.50.0 gate with Docker stopped)."""
    try:
        done = run(
            ['docker', 'info', '--format', '{{.ServerVersion}}'],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or done.returncode != 0:
        raise RuntimeError(DOCKER_DOWN)


def run(ctx):
    settings = ctx.config.get('onboarding', {})
    if not ctx.apply or not settings.get('enabled', True):
        ctx.log('Guided media onboarding deferred')
        return
    if not apps_to_connect(ctx.config):
        # Jellyfin with its libraries, and the download apps, are what this step connects
        # (and asks about: the download sources, the indexers, the playback test).
        ctx.log(
            'Connecting the apps: Movies and shows and the downloads are switched off, so there is nothing to connect'
        )
        return
    docker_running()
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
        services = media_services(ctx.config)
        if settings.get('configure_libraries', True) and 'jellyfin' in services:
            cmd.append('--libraries')
        if (
            settings.get('configure_request_providers', True)
            and 'seerr' in services
            and set(services) & {'sonarr', 'radarr'}
        ):
            cmd.append('--request-providers')
        # Media a person already has is asked on setup's storage page (existing_page.py),
        # used where it is or copied: the connecting step no longer asks for "sources".
        optional = []
        if settings.get('configure_indexers', True) and 'prowlarr' in services:
            optional.append('indexers')
        if not playback_record(ctx.config).exists():
            optional.append('playback')
        ctx.optional_kept = guided_setup.optional_kept(ctx, optional)
        cmd += ['--' + question for question in optional if question not in ctx.optional_kept + ['playback']]
        # With the lock backups and monthly updates take: a nightly backup stopping the apps
        # while they are being connected would fail them half-way.
        from recovery_tools import lock

        # Inside setup's own process (setup_progress.run_program): its questions about
        # download sources and sign-ins are asked in the setup window too.
        with lock(ctx.config):
            result = setup_progress.run_program(cmd[1], cmd[2:])
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
                'Sign into the relevant opened applications using their saved logins ("Show login" in the setup '
                'window, or the question below this list in the terminal, shows them). '
                'Finish selected provider/indexer credentials or any provider verification in its own page.',
                'Run  sudo bash install.sh  again afterwards: it checks the configuration again.',
            ],
            [
                ('url', 'http://127.0.0.1:' + str(ports[a]) + '/')
                for a in ctx.config.get('media', {}).get('services', [])
                if a in ports
            ],
            logins=[a for a in media_services(ctx.config) if a in ports],
            ask='apps_finished',
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
    services = media_services(ctx.config)
    if 'jellyfin' not in services:
        # No Jellyfin on this server (a music-only one): nothing to play a film in.
        return
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
            ask='played',
        )
    except KeyboardInterrupt:
        done = False
    if done:
        atomic_json(record, {'confirmed_at': time.time(), 'evidence': 'user_reported'})
        ctx.log('Playback test confirmed')
    else:
        remember_answer(ctx.config['data_mount'], 'playback', 'skipped')
        ctx.log('Playback verification pending (optional): test a real playback when convenient')
