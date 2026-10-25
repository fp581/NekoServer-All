"""Pure checks for selected modules, before any installer module can mutate OS state."""
import re
from neko_common import DASHBOARD_DEFAULT_PORT, host_timezone
from modules import app_backup, dashboard, heartbeat, host_backup, notifications, server_os, nextcloud, maintenance, music


def nextcloud_default(config, plan=()):
    """"enabled" missing under "nextcloud" means Nextcloud is set up, as its own step and
    the account step read it. Written in once, here, so the dashboard, the gateways, the
    backups and the monthly update (which read a missing one as false) see the same
    answer. Without a "nextcloud" section at all it is added only when this run sets
    Nextcloud up: otherwise nothing installed it, and false is right."""
    opts = config.get('nextcloud')
    if opts is None and 'nextcloud' in plan:
        opts = config['nextcloud'] = {}
    if isinstance(opts, dict):
        opts.setdefault('enabled', True)


def validate(config, plan):
    nextcloud_default(config, plan)
    validators = {
        'music': music.options, 'nextcloud': nextcloud.options, 'maintenance': maintenance.options, 'server_os': server_os.settings, 'dashboard': dashboard.options,
        'notifications': notifications.options, 'heartbeat': heartbeat.interval,
        'host_backup': host_backup.settings,
        'app_backup': lambda c: app_backup.definitions(c, '/runtime', '/config'),
    }
    for name in plan:
        if name in validators:
            validators[name](config)
    for group in ('tv', 'onboarding', 'docker', 'photos', 'networking', 'guided_setup'):
        values = config.get(group, {})
        if not isinstance(values, dict):
            raise RuntimeError(group+' must be an object')
    if type(config.get('guided_setup', {}).get('enabled', True)) is not bool:
        raise RuntimeError('guided_setup.enabled must be a boolean')
    switches = {
        'tv': ('desktop_shortcuts','bluetooth','keep_awake'),
        'onboarding': ('enabled','collect_sources','configure_indexers','configure_libraries','configure_request_providers'),
        'docker': ('install_engine','enable_service'), 'photos': ('deploy_immich',),
        'networking': ('tailnet_only','samba_media_write'),
    }
    for name in plan:
        group = 'networking' if name == 'samba' else name
        for key in switches.get(group, ()):
            values = config.get(group, {})
            if key in values and type(values[key]) is not bool:
                raise RuntimeError(group+'.'+key+' must be a boolean')
    if 'media' in plan:
        values = config.get('media', {})
        for key in ('uid','gid'):
            value = values.get(key,1000)
            if type(value) is not int or not 1 <= value <= 2**32-2:
                raise RuntimeError('Media uid/gid must be positive numeric account IDs')
        from modules import media as media_module
        peer = media_module.torrent_port(values)
        if peer is not None and peer == config.get('dashboard', {}).get('port', DASHBOARD_DEFAULT_PORT):
            raise RuntimeError(f'media.torrent_port {peer} is the dashboard port; choose another port for one of them')
        if values.get('bind_ip','127.0.0.1') != '127.0.0.1':
            raise RuntimeError('Media web interfaces must bind to localhost')
    if set(plan) & {'media', 'photos', 'photo_backup', 'photo_preservation', 'music_backup', 'maintenance'}:
        host_timezone(config)
    if 'media_monitor' in plan:
        value=config.get('monitoring',{}).get('interval_minutes',60)
        if type(value) is not int or not 15<=value<=1440:raise RuntimeError('Invalid media monitoring interval')
    if 'onboarding' in plan:
        value=config.get('onboarding',{}).get('wait_seconds',180)
        if type(value) is not int or not 0<=value<=600:raise RuntimeError('Invalid startup wait')
    if set(plan) & {'account_credentials', 'accounts'}:
        import account_store
        account_store.enabled(config)
        value = config.get('accounts', {}).get('wait_seconds', 180)
        if type(value) is not int or not 0 <= value <= 600:
            raise RuntimeError('Account startup wait must be 0..600 seconds')
    opts=config.get('photos',{})
    if 'photos' in plan:
        value=opts.get('immich_version','auto')
        if value != 'auto' and (not isinstance(value,str) or not re.fullmatch(r'v\d+\.\d+\.\d+',value)):
            raise RuntimeError('Invalid Immich release version')
    if 'photo_preservation' in plan:
        for key,default,minimum in (('archive_interval_minutes',60,15),('archive_min_age_seconds',600,60)):
            value=opts.get(key,default)
            maximum=10080 if key=='archive_interval_minutes' else 31536000
            if type(value) is not int or not minimum<=value<=maximum:raise RuntimeError('Invalid photo archive interval/age')
        _time(opts.get('database_backup_time','02:15'))
    if 'photo_backup' in plan:
        _time(opts.get('backup_time','03:00'))
        if opts.get('backup_weekday','Sun') not in ('Mon','Tue','Wed','Thu','Fri','Sat','Sun'):
            raise RuntimeError('Invalid external backup weekday')
        if opts.get('external_backup_fstype','ext4') not in ('ext4','xfs','btrfs','exfat'):
            raise RuntimeError('Invalid external backup filesystem')


def _time(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value):
        raise RuntimeError('Invalid job time; use HH:MM')


# ----- Steps added to the template's profiles after a settings file was made

# Where a settings file records the template's profiles as last merged into it: a step
# listed there but missing from your own profile was taken out on purpose and stays out.
KNOWN_STEPS = 'profile_steps_known'
# Settings files made before that record existed (before 0.39.5): the release each step
# joined the template's profiles, so a step the file's release already had (and you took
# out) stays out. Steps added from now on are found through the record instead.
STEP_ADDED = {'account_credentials': (0, 21, 0), 'accounts': (0, 21, 0), 'capacity': (0, 22, 0),
              'tailscale': (0, 22, 0), 'gateway_tailnet': (0, 22, 0), 'hardware_video': (0, 24, 1),
              'desktop': (0, 28, 7)}
RELEASE = re.compile(r'v(\d{1,9})\.(\d{1,9})\.(\d{1,9})')


def made_with(config):
    """The release whose template the settings file was made from ("project_name"), or None."""
    match = RELEASE.search(str(config.get('project_name', '')))
    return tuple(int(n) for n in match.groups()) if match else None


def new_step(step, known, release):
    """The step is new to this settings file (not one you took out): the file's record says
    it was not offered yet, or without a record, the file's release came before the step."""
    if known is not None:
        return step not in known
    # Without a record (a file made before 0.39.5): only the steps in STEP_ADDED came later
    # than the oldest supported template; a step added after this release is in the record.
    # A file that names no release (written by hand) is taken as it is.
    added = STEP_ADDED.get(step)
    return added is not None and release is not None and release < added


def insert_after_neighbour(mine, template, step):
    """`step` put into `mine` after the nearest step before it in the template's order."""
    before = template[:template.index(step)]
    place = max((mine.index(s) + 1 for s in before if s in mine), default=0)
    mine.insert(place, step)


def merge_profile(name, own, steps, known, release):
    """One profile: the template's new steps added to your list, in their place."""
    lines = []
    for step in steps:
        if step not in own and new_step(step, known, release):
            insert_after_neighbour(own, steps, step)
            lines.append(f'Settings: the step "{step}" (new since your settings were made) is added to the {name} profile')
    return lines


def merge_profiles(config, template):
    """Steps a newer release added to the template's profiles are added to the settings'
    profiles of the same name (a settings file keeps the profiles it was made with, so an
    upgraded server would never run them). Your own additions stay, and so do steps you
    took out. Returns (one line per step added, whether the settings changed); the record
    of the template's steps is brought up to date."""
    theirs = template.get('profiles') if isinstance(template.get('profiles'), dict) else {}
    mine = config.setdefault('profiles', {})
    if not isinstance(mine, dict):
        return [], False
    record = config.get(KNOWN_STEPS) if isinstance(config.get(KNOWN_STEPS), dict) else {}
    release, lines = made_with(config), []
    for name, steps in theirs.items():
        if isinstance(steps, list) and isinstance(mine.get(name, []), list):
            known = record.get(name) if isinstance(record.get(name), list) else None
            lines += merge_profile(name, mine.setdefault(name, []), steps, known, release)
    updated = {name: list(steps) for name, steps in theirs.items() if isinstance(steps, list)}
    changed = bool(lines) or updated != record
    config[KNOWN_STEPS] = updated
    return lines, changed


def expected_steps(config, template):
    """The steps a full installation is meant to run: the template's full profile without
    the ones you took out, plus your own additions, in the template's order."""
    mine = config.get('profiles', {}).get('full', []) if isinstance(config.get('profiles'), dict) else []
    record = config.get(KNOWN_STEPS) if isinstance(config.get(KNOWN_STEPS), dict) else {}
    known = record.get('full') if isinstance(record.get('full'), list) else None
    full = template.get('profiles', {}).get('full', []) if isinstance(template.get('profiles'), dict) else []
    wanted = [s for s in full if s in mine or new_step(s, known, made_with(config))]
    return wanted + [s for s in mine if s not in wanted]
