"""Deploy private Nextcloud, PostgreSQL, Redis and its upstream cron worker."""

import json
import account_store
import neko_common
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from neko_common import (
    atomic_json,
    atomic_write,
    checked_run,
    compose_up,
    ensure_directory,
    fetch,
    safe_path,
    verify_storage,
    compose_image_override,
)
from recovery_tools import lock

# account_credentials: Nextcloud's administrator password is chosen there before it
# is installed (the other apps get their accounts afterwards, in the accounts step).
DEPENDENCIES = ['docker', 'recovery', 'account_credentials']
DESCRIPTION = 'Personal cloud with SSD application state and HDD files'
HTML, POSTGRES = 'appdata/nextcloud/html', 'appdata/nextcloud/postgres'  # under the data SSD
ROOT_UID = 0  # setup files must belong to root (tests run as a normal user)


def options(config):
    opts = config.get('nextcloud', {})
    if not isinstance(opts, dict) or type(opts.get('enabled', True)) is not bool:
        raise RuntimeError('Invalid Nextcloud options')
    image = opts.get('image', 'auto')
    if image != 'auto' and (
        not isinstance(image, str) or not re.fullmatch(r'nextcloud:[1-9]\d(?:\.\d+){0,2}-apache', image)
    ):
        raise RuntimeError('Use auto or a versioned nextcloud Apache image')
    hosts = opts.get('trusted_hosts', ['localhost', '127.0.0.1'])
    if (
        not isinstance(hosts, list)
        or not hosts
        or any(not isinstance(h, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::\d{1,5})?', h) for h in hosts)
    ):
        raise RuntimeError('Explicit Nextcloud trusted hosts required; no wildcard')
    url = opts.get('public_url', '')
    if url and (not isinstance(url, str) or not re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::\d{1,5})?', url)):
        raise RuntimeError('Nextcloud public_url must be an HTTPS origin')
    if not isinstance(opts.get('admin_user', 'admin'), str) or not re.fullmatch(
        r'[A-Za-z][A-Za-z0-9_-]{0,31}', opts.get('admin_user', 'admin')
    ):
        raise RuntimeError('Invalid Nextcloud administrator name')
    return opts


def resolve_image(value):
    if value != 'auto':
        return value
    release = json.loads(
        fetch(
            'https://api.github.com/repos/nextcloud/server/releases/latest',
            'Finding the current Nextcloud release',
            200000,
        )
    )
    return image_for_release(release)


def image_for_release(release):
    """Docker image for a fresh install. A brand-new major (x.0.0 or x.0.1) installs the
    previous major instead: Nextcloud advises waiting for early fixes, and the official
    Docker image can lag the GitHub release by days, which would fail the install."""
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', release.get('tag_name', ''))
    if not match or release.get('prerelease') or release.get('draft'):
        raise RuntimeError('Stable Nextcloud release could not be resolved')
    major, minor, patch = (int(x) for x in match.groups())
    if minor == 0 and patch < 2:
        major -= 1
    return f'nextcloud:{major}-apache'


def settings(config, image):
    """What my-config.json contributes to the Compose definition: the things a person may
    have changed since the install. A changed definition with the same settings comes
    from a new release and is applied."""
    opts = options(config)
    return {
        'image': image,
        'admin_user': opts.get('admin_user', 'admin'),
        'trusted_hosts': list(opts.get('trusted_hosts', ['localhost', '127.0.0.1'])),
        'public_url': opts.get('public_url', ''),
        'data_mount': str(config['data_mount']),
        'media_mount': str(config['media_mount']),
    }


# The administrator names setup makes up itself (account_store.generate): never a person's choice.
GENERATED_NAME = re.compile(r'neko_[0-9a-f]{12}')


def adopt_admin(opts, owned, chosen, persist, log):
    """Settings that do not name the installed administrator while the account store on the
    SSD does: made anew (Linux reinstalled, the SSD kept: they say "admin"), or made for an
    earlier server (another name setup made up). The installed name is taken back into
    my-config.json, so the install is recognised as this one; a name a person chose is left
    to the settings check. True when the name was taken."""
    named = opts.get('admin_user', 'admin')
    if not chosen or not (named == 'admin' or GENERATED_NAME.fullmatch(named)):
        return False
    installed = settings_of(owned).get('admin_user')
    if installed in (None, 'admin', named) or installed != chosen['username']:
        return False
    opts['admin_user'] = installed
    persist()
    log(
        'Nextcloud: the administrator\'s name in my-config.json was taken from the installed Nextcloud '
        '(the settings were made anew or for an earlier server; the SSD was kept)'
    )
    return True


# The files Nextcloud checks for at the top of its files folder before it serves anything
# (.ncdata since Nextcloud 31, .ocdata before).
DATA_MARKERS = ('.ncdata', '.ocdata')
WWW_DATA = 33
# Nextcloud's files folder, under the HDD's mount.
FILES_FOLDER = 'cloud/files'


def files_folder_given_back(config, log):
    """A Nextcloud already set up whose files folder on the HDD is missing or empty (a new
    HDD after the old one broke, say): the folder is made, given to Nextcloud (www-data)
    and marked as its files folder, so Nextcloud starts again, with no files; the way to
    get them back is said. True when that was done."""
    files = safe_path(Path(config['media_mount']) / FILES_FOLDER)
    if files.is_dir() and any(files.iterdir()):
        return False
    ensure_directory(safe_path(files.parent), 0o755)
    ensure_directory(files, 0o750)
    for marker in DATA_MARKERS:
        (files / marker).write_text('# Nextcloud data directory\n')
        os.chown(files / marker, WWW_DATA, WWW_DATA)
    os.chown(files, WWW_DATA, WWW_DATA)
    log(
        'Nextcloud: its files folder on the HDD was empty (a new HDD?): made Nextcloud\'s own again, so it '
        'starts with no files. Get them back with  sudo bash install.sh --recover  (Nextcloud files, from '
        'the USB drive), then upload them again in Nextcloud.'
    )
    return True


def settings_changed(recorded, wanted):
    """What differs between the settings Nextcloud was installed with and the ones in
    my-config.json now, named for the refusal; '' when nothing does."""
    return ', '.join(
        f'{key}: {recorded.get(key)!r} is now {wanted.get(key)!r}'
        for key in wanted
        if recorded.get(key) != wanted.get(key)
    )


def settings_of(owned):
    """The settings an installed definition was made from: recorded since 0.29.0, read
    back from the definition itself for earlier installs."""
    if isinstance(owned.get('settings'), dict):
        return owned['settings']
    app = owned.get('model', {}).get('services', {}).get('nextcloud', {})
    env = app.get('environment', {})
    volumes = app.get('volumes', [])
    data = volumes[0].split(':', 1)[0].rsplit('/appdata/nextcloud/html', 1)[0] if volumes else ''
    media = volumes[1].split(':', 1)[0].rsplit(f'/{FILES_FOLDER}', 1)[0] if len(volumes) > 1 else ''
    return {
        'image': owned.get('image'),
        'admin_user': env.get('NEXTCLOUD_ADMIN_USER', 'admin'),
        'trusted_hosts': env.get('NEXTCLOUD_TRUSTED_DOMAINS', '').split(),
        'public_url': env.get('OVERWRITECLIURL', ''),
        'data_mount': data,
        'media_mount': media,
    }


def model(config, image):
    opts = options(config)
    root = Path(config['data_mount']) / 'appdata/nextcloud'
    files = Path(config['media_mount']) / FILES_FOLDER
    mounts = [str(root / 'html') + ':/var/www/html', str(files) + ':/var/www/html/data']
    env = {
        'POSTGRES_HOST': 'database',
        'POSTGRES_DB': 'nextcloud',
        'POSTGRES_USER': 'nextcloud',
        'POSTGRES_PASSWORD': '${DB_PASSWORD}',
        'REDIS_HOST': 'redis',
        'NEXTCLOUD_ADMIN_USER': opts.get('admin_user', 'admin'),
        'NEXTCLOUD_ADMIN_PASSWORD': '${ADMIN_PASSWORD}',
        'NEXTCLOUD_TRUSTED_DOMAINS': ' '.join(opts.get('trusted_hosts', ['localhost', '127.0.0.1'])),
        'NEXTCLOUD_UPDATE': '1',
    }
    # External HTTPS origins are set explicitly; forwarded headers are not trusted globally.
    if opts.get('public_url'):
        env.update(
            OVERWRITEPROTOCOL='https',
            OVERWRITEHOST=opts['public_url'].split('://', 1)[1],
            OVERWRITECLIURL=opts['public_url'],
        )
    services = {
        # A minute to stop (Docker's default is 10 s): Postgres writes out what it holds in
        # memory first, so it starts again without having to recover (backups, updates and
        # shutting the PC down all stop it).
        'database': {
            'image': 'postgres:17-bookworm',
            'restart': 'unless-stopped',
            'stop_grace_period': '60s',
            'environment': {
                'POSTGRES_DB': 'nextcloud',
                'POSTGRES_USER': 'nextcloud',
                'POSTGRES_PASSWORD': '${DB_PASSWORD}',
            },
            'volumes': [str(root / 'postgres') + ':/var/lib/postgresql/data'],
            'healthcheck': {
                'test': ['CMD-SHELL', 'pg_isready -U nextcloud -d nextcloud'],
                'interval': '10s',
                'timeout': '5s',
                'retries': 12,
            },
        },
        'redis': {
            'image': 'redis:7-bookworm',
            'restart': 'unless-stopped',
            'command': ['redis-server', '--save', '', '--appendonly', 'no'],
        },
        'nextcloud': {
            'image': image,
            'restart': 'unless-stopped',
            'ports': ['127.0.0.1:8088:80'],
            'volumes': mounts,
            'environment': env,
            'depends_on': {'database': {'condition': 'service_healthy'}, 'redis': {'condition': 'service_started'}},
        },
        # The Apache image declares SIGWINCH as its stop signal (Apache's graceful stop),
        # which crond ignores: stopping the apps for a backup killed it after 60 s (exit
        # 137) and the monthly update refused to go on. crond gets SIGTERM, through an
        # init process so that it is not PID 1 (which would ignore it too).
        'cron': {
            'image': image,
            'restart': 'unless-stopped',
            'entrypoint': '/cron.sh',
            'volumes': mounts,
            'init': True,
            'stop_signal': 'SIGTERM',
            'depends_on': ['nextcloud'],
        },
    }
    return {'services': services}


def quote_env(value):
    # Compose dotenv double-quoted strings decode JSON-style escapes. $$ keeps
    # literal dollars from being interpreted as environment variable references.
    return json.dumps(value, ensure_ascii=False).replace('$', '$$')


def command(args):
    checked_run(args, 1800, 'Nextcloud (docker compose)')


# What the image itself runs on its first start (entrypoint.sh), with the settings it is
# given in its environment, so no password is on a command line.
INSTALL = (
    'php occ maintenance:install -n --admin-user "$NEXTCLOUD_ADMIN_USER" --admin-pass "$NEXTCLOUD_ADMIN_PASSWORD" '
    '--database pgsql --database-name "$POSTGRES_DB" --database-user "$POSTGRES_USER" '
    '--database-pass "$POSTGRES_PASSWORD" --database-host "$POSTGRES_HOST" && i=0 && '
    'for domain in $NEXTCLOUD_TRUSTED_DOMAINS; do '
    'php occ config:system:set trusted_domains "$i" --value="$domain" || exit 1; i=$((i+1)); done'
)


def status(base, run=subprocess.run):
    """Nextcloud's own `occ status`, or None while it can't answer (still starting)."""
    result = run(
        base + ['exec', '-T', '-u', 'www-data', 'nextcloud', 'php', 'occ', 'status', '--output=json'],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    text = result.stdout or ''
    try:
        return json.loads(text[text.index('{') :]) if result.returncode == 0 else None
    except ValueError:
        return None


def finish_install(base, log, run=subprocess.run, wait=900, sleep=time.sleep, now=time.monotonic):
    """The image installs Nextcloud on its first start only, retrying for about two
    minutes; when the database was out of reach that long, every later start skips the
    install and Nextcloud stays "not installed" for good (the accounts step then sees a
    login that can't match). Once the image's own attempt is over, it is finished here."""
    deadline = now() + wait
    while True:
        found = status(base, run)
        if found and found.get('installed'):
            return False
        logs = (
            run(
                base + ['logs', '--no-color', '--tail', '300', 'nextcloud'],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout
            or ''
        )
        # Apache serving without an install means the image's attempt is over (given up,
        # then restarted); "failed!" is its own last word when it gives up.
        if found is not None and ('resuming normal operations' in logs or 'Installing of nextcloud failed' in logs):
            break
        if now() > deadline:
            raise RuntimeError(
                'Nextcloud did not finish installing within ' + str(wait // 60) + ' minutes; its log is  '
                'sudo docker logs neko-nextcloud-nextcloud-1'
            )
        sleep(10)
    log('Nextcloud: its first-start install did not finish (its database was out of reach then); finishing it now')
    checked_run(
        base + ['exec', '-T', '-u', 'www-data', 'nextcloud', 'sh', '-c', INSTALL],
        1800,
        'Nextcloud (finishing its install)',
    )
    found = status(base, run)
    if not (found and found.get('installed')):
        raise RuntimeError(
            'Nextcloud still reports "not installed" after finishing its install; its log is  '
            'sudo docker logs neko-nextcloud-nextcloud-1'
        )
    return True


# Written by monthly maintenance while an update is in progress; a resumed update or
# rollback starts the apps it covers again.
MAINTENANCE_JOURNAL = neko_common.MAINTENANCE_JOURNAL


def stop_installed(ctx, run_command=None, hold=None, journal=None):
    """Nextcloud turned off: never installed means nothing to do; installed means stop
    its containers (app, database, Redis, cron) and keep every file and the database.
    Stopped containers stay stopped after a reboot; turning it on again starts them.
    Runs under the lock monthly maintenance and the backups hold, so it never stops
    Nextcloud in the middle of an update, its health checks or a rollback."""
    folder = Path(ctx.config['data_mount']) / 'compose/nextcloud'
    manifest, env = folder / 'compose.yaml', folder / '.env'
    if not manifest.is_file():
        ctx.log('Nextcloud: turned off and not installed; nothing to do')
        return
    if not ctx.apply:
        ctx.log('Nextcloud: turned off; --apply stops its containers and keeps its data')
        return
    journal = Path(journal or MAINTENANCE_JOURNAL)  # looked up now (tests point it elsewhere)
    with (hold or lock)(ctx.config):
        if journal.exists():
            raise RuntimeError(
                'A monthly update did not finish; it is completed (or rolled back) the next '
                'time maintenance runs, and that starts Nextcloud again. Run  sudo bash '
                'install.sh  again after it has finished to turn Nextcloud off.'
            )
        base = [
            'docker',
            '--host',
            'unix:///var/run/docker.sock',
            'compose',
            '-p',
            'neko-nextcloud',
            '-f',
            str(manifest),
            *(['--env-file', str(env)] if env.is_file() else []),
            *compose_image_override(manifest),
        ]
        (run_command or command)(base + ['stop'])
    ctx.log(
        'Nextcloud: turned off. Its containers are stopped; your files and database are kept. '
        'Set "enabled": true under "nextcloud" and run  sudo bash install.sh  to start it again.'
    )


def run(ctx):
    opts = options(ctx.config)
    if not opts.get('enabled', True):
        return stop_installed(ctx)
    if not ctx.apply:
        ctx.log('Nextcloud: private localhost:8088, SSD database/application, HDD cloud files')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    config = ctx.config
    verify_storage(config)
    with lock(config):
        data = Path(config['data_mount'])
        folder = safe_path(data / 'compose/nextcloud')
        manifest, env = folder / 'compose.yaml', folder / '.env'
        managed = folder / 'managed.json'
        partial = [f for f in (env, managed, manifest) if f.exists()]
        if partial and not manifest.exists() or manifest.exists() and len(partial) < 3:
            # Setup stopped while writing its settings (compose.yaml is written last, so
            # it marks a finished step). Containers start only after that, so with no app
            # data yet nothing depends on these files: write them again from the start.
            for existing in (data / HTML, data / POSTGRES):
                if existing.exists() and any(safe_path(existing).iterdir()):
                    raise RuntimeError('Incomplete Nextcloud installation with existing data; preserved for inspection')
            for leftover in partial:
                safe_path(leftover).unlink()
            ctx.log('Nextcloud: finishing a setup that was interrupted')
        image = opts.get('image', 'auto')
        chosen = account_store.get(config, 'nextcloud') if account_store.enabled(config) else None
        if account_store.enabled(config) and chosen is None:
            raise RuntimeError('Complete the account_credentials prompts before Nextcloud setup')
        if not manifest.exists():
            if chosen:
                opts['admin_user'] = chosen['username']
                ctx.persist_config()
            for existing in (data / HTML, data / POSTGRES, Path(config['media_mount']) / FILES_FOLDER):
                safe_path(existing)
                if existing.exists() and any(existing.iterdir()):
                    raise RuntimeError('Existing Nextcloud data preserved; explicit recovery required')
            image = resolve_image(image)
            payload = json.dumps(model(config, image), indent=2) + '\n'
            for target, mode in (
                (data / 'appdata', 0o755),
                (data / 'appdata/nextcloud', 0o700),
                (data / HTML, 0o755),
                (data / POSTGRES, 0o700),
                (Path(config['media_mount']) / 'cloud', 0o755),
                (Path(config['media_mount']) / FILES_FOLDER, 0o750),
                (folder, 0o700),
            ):
                ensure_directory(safe_path(target), mode)
            # Nextcloud/Apache writes as www-data; only newly empty application data is initialized.
            files = safe_path(Path(config['media_mount']) / FILES_FOLDER)
            if not any(files.iterdir()):
                os.chown(files, WWW_DATA, WWW_DATA)
            password = chosen['password'] if chosen else secrets.token_hex(24)
            atomic_write(
                env,
                'DB_PASSWORD='
                + secrets.token_hex(32)
                + '\nADMIN_PASSWORD='
                + (quote_env(password) if chosen else password)
                + '\n',
                0o600,
            )
            atomic_json(
                managed,
                {
                    'format': 'neko-nextcloud-v1',
                    'image': image,
                    'model': model(config, image),
                    'settings': settings(config, image),
                },
            )
            # Written last: a compose.yaml means every setting file of this step is in place.
            atomic_write(manifest, payload, 0o600)
        else:
            files_folder_given_back(config, ctx.log)
        safe_path(env)
        if env.stat().st_uid != ROOT_UID or env.stat().st_mode & 0o077:
            raise RuntimeError('Nextcloud credentials must be private and root-owned')
        owned = json.loads(safe_path(managed).read_text())
        image = owned['image']
        adopt_admin(opts, owned, chosen, ctx.persist_config, ctx.log)
        changed = settings_changed(settings_of(owned), settings(config, image))
        if changed:
            raise RuntimeError(
                f'Nextcloud\'s settings in my-config.json changed since it was installed ({changed}), and a '
                'running Nextcloud is never reconfigured on a guess. Put the earlier values back, or change them '
                'inside Nextcloud (occ config:system:set); the gateway steps set trusted hosts and the address '
                'themselves, and the administrator\'s name cannot change after the install'
            )
        if json.loads(manifest.read_text()) != model(config, image) or owned['model'] != model(config, image):
            # Same settings, another definition: this release changed how Nextcloud is run
            # (a container option, say). compose up applies it to the running containers.
            atomic_json(managed, {**owned, 'model': model(config, image), 'settings': settings(config, image)})
            atomic_write(manifest, json.dumps(model(config, image), indent=2) + '\n', 0o600)
            ctx.log('Nextcloud: its container definition was brought up to date by this release (settings unchanged)')
        verify_storage(config)
        base = [
            'docker',
            '--host',
            'unix:///var/run/docker.sock',
            'compose',
            '-p',
            'neko-nextcloud',
            '-f',
            str(manifest),
            '--env-file',
            str(env),
        ]
        base += compose_image_override(manifest)
        command(base + ['config', '-q'])
        compose_up(base, 'Nextcloud', ctx.log, 1800)
        finish_install(base, ctx.log)
        if opts.get('image', 'auto') == 'auto':
            config['nextcloud']['image'] = image
            ctx.persist_config()
        ctx.log(
            'Nextcloud started at localhost:8088. Administrator credentials are in the private account inventory when account setup is enabled. Configure trusted hosts/public_url for the chosen gateway before remote access.'
        )
