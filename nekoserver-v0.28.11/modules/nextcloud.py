"""Deploy private Nextcloud, PostgreSQL, Redis and its upstream cron worker."""
import json
import account_store
import neko_common
import os
from pathlib import Path
import re
import secrets

from neko_common import atomic_json, atomic_write, checked_run, compose_up, ensure_directory, fetch, safe_path, verify_storage, compose_image_override
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
    if image != 'auto' and (not isinstance(image, str) or not re.fullmatch(r'nextcloud:[1-9][0-9](?:\.\d+){0,2}-apache', image)):
        raise RuntimeError('Use auto or a versioned nextcloud Apache image')
    hosts = opts.get('trusted_hosts', ['localhost', '127.0.0.1'])
    if not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?', h) for h in hosts):
        raise RuntimeError('Explicit Nextcloud trusted hosts required; no wildcard')
    url = opts.get('public_url', '')
    if url and (not isinstance(url, str) or not re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?', url)):
        raise RuntimeError('Nextcloud public_url must be an HTTPS origin')
    if not isinstance(opts.get('admin_user', 'admin'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', opts.get('admin_user', 'admin')):
        raise RuntimeError('Invalid Nextcloud administrator name')
    return opts


def resolve_image(value):
    if value != 'auto':
        return value
    release = json.loads(fetch('https://api.github.com/repos/nextcloud/server/releases/latest',
                               'Finding the current Nextcloud release', 200000))
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


def model(config, image):
    opts = options(config)
    root = Path(config['data_mount'])/'appdata/nextcloud'
    files = Path(config['media_mount'])/'cloud/files'
    mounts = [str(root/'html')+':/var/www/html', str(files)+':/var/www/html/data']
    env = {'POSTGRES_HOST': 'database', 'POSTGRES_DB': 'nextcloud', 'POSTGRES_USER': 'nextcloud',
           'POSTGRES_PASSWORD': '${DB_PASSWORD}', 'REDIS_HOST': 'redis',
           'NEXTCLOUD_ADMIN_USER': opts.get('admin_user', 'admin'), 'NEXTCLOUD_ADMIN_PASSWORD': '${ADMIN_PASSWORD}',
           'NEXTCLOUD_TRUSTED_DOMAINS': ' '.join(opts.get('trusted_hosts', ['localhost', '127.0.0.1'])),
           'NEXTCLOUD_UPDATE': '1'}
    # External HTTPS origins are set explicitly; forwarded headers are not trusted globally.
    if opts.get('public_url'):
        env.update(OVERWRITEPROTOCOL='https', OVERWRITEHOST=opts['public_url'].split('://', 1)[1], OVERWRITECLIURL=opts['public_url'])
    services = {
        'database': {'image': 'postgres:17-bookworm', 'restart': 'unless-stopped',
                     'environment': {'POSTGRES_DB': 'nextcloud', 'POSTGRES_USER': 'nextcloud', 'POSTGRES_PASSWORD': '${DB_PASSWORD}'},
                     'volumes': [str(root/'postgres')+':/var/lib/postgresql/data'],
                     'healthcheck': {'test': ['CMD-SHELL', 'pg_isready -U nextcloud -d nextcloud'], 'interval': '10s', 'timeout': '5s', 'retries': 12}},
        'redis': {'image': 'redis:7-bookworm', 'restart': 'unless-stopped', 'command': ['redis-server', '--save', '', '--appendonly', 'no']},
        'nextcloud': {'image': image, 'restart': 'unless-stopped', 'ports': ['127.0.0.1:8088:80'],
                      'volumes': mounts, 'environment': env,
                      'depends_on': {'database': {'condition': 'service_healthy'}, 'redis': {'condition': 'service_started'}}},
        'cron': {'image': image, 'restart': 'unless-stopped', 'entrypoint': '/cron.sh', 'volumes': mounts,
                 'depends_on': ['nextcloud']},
    }
    return {'services': services}


def quote_env(value):
    # Compose dotenv double-quoted strings decode JSON-style escapes. $$ keeps
    # literal dollars from being interpreted as environment variable references.
    return json.dumps(value, ensure_ascii=False).replace('$', '$$')


def command(args):
    checked_run(args, 1800, 'Nextcloud (docker compose)')


# Written by monthly maintenance while an update is in progress; a resumed update or
# rollback starts the apps it covers again.
MAINTENANCE_JOURNAL = neko_common.MAINTENANCE_JOURNAL


def stop_installed(ctx, run_command=None, hold=None, journal=MAINTENANCE_JOURNAL):
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
    with (hold or lock)(ctx.config):
        if journal.exists():
            raise RuntimeError('A monthly update did not finish; it is completed (or rolled back) the next '
                               'time maintenance runs, and that starts Nextcloud again. Run  sudo bash '
                               'install.sh  again after it has finished to turn Nextcloud off.')
        base = ['docker', '--host', 'unix:///var/run/docker.sock', 'compose', '-p', 'neko-nextcloud',
                '-f', str(manifest), *(['--env-file', str(env)] if env.is_file() else []),
                *compose_image_override(manifest)]
        (run_command or command)(base + ['stop'])
    ctx.log('Nextcloud: turned off. Its containers are stopped; your files and database are kept. '
            'Set "enabled": true under "nextcloud" and run  sudo bash install.sh  to start it again.')


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
        folder = safe_path(data/'compose/nextcloud')
        manifest, env = folder/'compose.yaml', folder/'.env'
        managed = folder/'managed.json'
        partial = [f for f in (env, managed, manifest) if f.exists()]
        if partial and not manifest.exists() or manifest.exists() and len(partial) < 3:
            # Setup stopped while writing its settings (compose.yaml is written last, so
            # it marks a finished step). Containers start only after that, so with no app
            # data yet nothing depends on these files: write them again from the start.
            for existing in (data/HTML, data/POSTGRES):
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
            for existing in (data/HTML, data/POSTGRES, Path(config['media_mount'])/'cloud/files'):
                safe_path(existing)
                if existing.exists() and any(existing.iterdir()):
                    raise RuntimeError('Existing Nextcloud data preserved; explicit recovery required')
            image = resolve_image(image)
            payload = json.dumps(model(config, image), indent=2)+'\n'
            for target, mode in ((data/'appdata', 0o755), (data/'appdata/nextcloud', 0o700), (data/HTML, 0o755), (data/POSTGRES, 0o700), (Path(config['media_mount'])/'cloud', 0o755), (Path(config['media_mount'])/'cloud/files', 0o750), (folder, 0o700)):
                ensure_directory(safe_path(target), mode)
            # Nextcloud/Apache writes as www-data; only newly empty application data is initialized.
            files = safe_path(Path(config['media_mount'])/'cloud/files')
            if not any(files.iterdir()):
                os.chown(files, 33, 33)
            password = chosen['password'] if chosen else secrets.token_hex(24)
            atomic_write(env, 'DB_PASSWORD='+secrets.token_hex(32)+'\nADMIN_PASSWORD='+(quote_env(password) if chosen else password)+'\n', 0o600)
            atomic_json(managed, {'format': 'neko-nextcloud-v1', 'image': image, 'model': model(config, image)})
            # Written last: a compose.yaml means every setting file of this step is in place.
            atomic_write(manifest, payload, 0o600)
        safe_path(env)
        if env.stat().st_uid != ROOT_UID or env.stat().st_mode & 0o077:
            raise RuntimeError('Nextcloud credentials must be private and root-owned')
        owned = json.loads(safe_path(managed).read_text())
        image = owned['image']
        if json.loads(manifest.read_text()) != model(config, image) or owned['model'] != model(config, image):
            raise RuntimeError('Nextcloud\'s settings in my-config.json changed since it was installed (its image, '
                               'trusted hosts or public address), and a running Nextcloud is never reconfigured on a '
                               'guess. Put the earlier values back, or change them inside Nextcloud (occ config:system:set); '
                               'the gateway steps set trusted hosts and the address themselves')
        verify_storage(config)
        base = ['docker', '--host', 'unix:///var/run/docker.sock', 'compose', '-p', 'neko-nextcloud', '-f', str(manifest), '--env-file', str(env)]
        base += compose_image_override(manifest)
        command(base+['config', '-q'])
        compose_up(base, 'Nextcloud', ctx.log, 1800)
        if opts.get('image', 'auto') == 'auto':
            config['nextcloud']['image'] = image
            ctx.persist_config()
        ctx.log('Nextcloud started at localhost:8088. Administrator credentials are in the private account inventory when account setup is enabled. Configure trusted hosts/public_url for the chosen gateway before remote access.')
