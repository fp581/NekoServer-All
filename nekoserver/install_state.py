"""What is already on this PC, for setup's "NekoServer was found on this PC" page (P00).

`look` reads what is really there (the settings file, the schedules, the record of the
setup steps, the releases kept on the app drive, a job that was cut off). `judge` turns
that into one answer: which of five things this PC is, and the choices that apply to it.
`found` does both.

    nothing        setup never ran here (a downloaded copy of NekoServer alone is this)
    settings_only  a settings file, and nothing installed from it
    unfinished     a setup, an upgrade or a drive move that was cut off, and where
    installed      a working server, with the release it runs
    older_folder   only an earlier release's folder with its settings

Nothing here asks a question or changes anything. What is returned holds no setting's
value: only what kind of thing was found, where, which release, and which steps are left.
"""

import json
import os
import re
from pathlib import Path

from neko_common import MY_SETTINGS, RECOVERY, SETTINGS_TEMPLATE, SYSTEMD_UNITS, VERSION, mount_path

ROOT = Path(__file__).resolve().parent

NOTHING, SETTINGS_ONLY, UNFINISHED, INSTALLED, OLDER_FOLDER = (
    'nothing',
    'settings_only',
    'unfinished',
    'installed',
    'older_folder',
)
# The choices of the page, by what they do. Only the ones that apply are returned.
CONTINUE, CHANGE, UPDATE, RESTORE = 'continue', 'change', 'update', 'restore'
# A job that was cut off: setup's own steps, an upgrade, a drive move.
SETUP, UPGRADE, DRIVE_MOVE = 'setup', 'upgrade', 'drive_move'

MIGRATE_JOURNAL = Path('/etc/nekoserver-backups/migrate.json')  # migrate.py JOURNAL
BY_UUID = Path('/dev/disk/by-uuid')  # each connected filesystem by its UUID (storage.BY_UUID)
DRIVE_ROLES = ('data_ssd', 'media_hdd')  # the two drives of the settings (storage.DRIVES)
JOURNALS = ((UPGRADE, RECOVERY['upgrade'][0]), (DRIVE_MOVE, MIGRATE_JOURNAL))
RELEASE = re.compile(r'v(\d{1,9})\.(\d{1,9})\.(\d{1,9})')
RELEASE_IN_UNIT = re.compile(r'/v(\d{1,9})\.(\d{1,9})\.(\d{1,9})/')  # as upgrade.RELEASE_IN_PATH
BUNDLE_MANIFEST = 'release-manifest.json'  # beside each release kept on the app drive
# How a step ended that needs nothing more (wizard.settled), and one that waits for the
# person, which a server can live with (wizard.sorted_steps "waiting").
SETTLED = ('success', 'unavailable')
WAITING = 'needs_setup'
LONGEST_FILE = 2_000_000


def numbers(text):
    """(0, 50, 0) from "0.50.0" or "v0.50.0"; None for anything else."""
    match = RELEASE.fullmatch('v' + str(text).lstrip('v'))
    return tuple(int(n) for n in match.groups()) if match else None


def words(version):
    return '.'.join(str(n) for n in version) if version else None


def read_object(path):
    """A JSON object from a file, or {} when it is missing, too big, or not one. Only a
    .json file is read, by its real path (a settings file named on the command line
    included): never another kind of file, and never through a link."""
    try:
        if Path(path).is_symlink():
            return {}
        real = os.path.realpath(path)
        if not (real.startswith(os.sep) and real.endswith('.json')) or '\x00' in real:
            return {}
        if os.stat(real).st_size > LONGEST_FILE:
            return {}
        with open(real, encoding='utf-8') as handle:
            value = json.loads(handle.read())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def app_drive(config):
    """The folder of the app drive the settings name, every link followed, or None when
    they name none that can be used."""
    try:
        return Path(os.path.realpath(mount_path(config.get('data_mount') or '/srv/nekoserver')))
    except (RuntimeError, ValueError, TypeError, OSError):
        return None


def below(folder, *parts):
    """`parts` under `folder`, only when that still lies inside it with every link
    followed (the folder's name comes from the settings file); else None."""
    if folder is None:
        return None
    inside = os.path.realpath(os.path.join(folder, *parts))
    return Path(inside) if inside.startswith(str(folder) + os.sep) else None


def unit_versions(units):
    """The releases NekoServer's installed schedules and services run from: each names
    its release's folder (/v0.49.0/). On the system disk, so they are found with the app
    drive unplugged too. Returns (whether any is installed, the releases named)."""
    found, any_unit = set(), False
    try:
        listed = sorted(Path(units).glob('nekoserver-*'))
    except OSError:
        return False, []
    for unit in listed:
        any_unit = True
        try:
            text = unit.read_text(errors='replace') if unit.is_file() else ''
        except OSError:
            continue
        found.update(tuple(int(n) for n in match) for match in RELEASE_IN_UNIT.findall(text))
    return any_unit, sorted(found)


def kept_versions(data):
    """The releases setup kept a copy of on the app drive (installer/releases/v...), each
    with the list of its files beside it: setup ran from them."""
    releases = below(data, 'installer', 'releases')
    found = []
    try:
        folders = sorted(releases.iterdir()) if releases is not None and releases.is_dir() else []
    except OSError:
        return []
    for folder in folders:
        version = numbers(folder.name) if folder.name.startswith('v') else None
        if version and (folder / BUNDLE_MANIFEST).is_file():
            found.append(version)
    return sorted(found)


def step_states(data, default_state):
    """How each setup step ended, from setup's record: the one on the app drive, else the
    one a setup keeps until that drive is chosen. Only each step's state is taken, never
    its reason (a reason is text a program wrote)."""
    record = below(data, 'installer', 'state', 'state.json')
    states = read_object(record) if record is not None else {}
    if not states and default_state is not None:
        states = read_object(Path(default_state) / 'state.json')
    return {
        str(name): str(state.get('status'))
        for name, state in states.items()
        if isinstance(state, dict) and isinstance(state.get('status'), str)
    }


def cut_off_jobs(journals):
    """The upgrade or drive move that left its record behind: it was cut off part-way."""
    return [kind for kind, journal in journals if read_object(journal)]


def older_folders(root):
    """Earlier releases' folders beside this one that still hold the person's settings
    (before 0.38.0 each release had a folder named for it): [(release, settings file)],
    newest first. As wizard.earlier_candidates finds them."""
    root = Path(root)
    found = []
    try:
        beside = sorted(root.parent.iterdir()) if root.parent.is_dir() else []
    except OSError:
        return []
    for folder in beside:
        name = folder.name
        version = numbers(name[len('nekoserver-') :]) if name.startswith('nekoserver-v') else None
        if version and folder != root and (folder / MY_SETTINGS).is_file():
            found.append((version, folder / MY_SETTINGS))
    return sorted(found, reverse=True)


def answered(config, template):
    """The settings hold an answer of the person's: they differ from the template they
    were made from in more than whose account the apps run as (setup writes that itself
    when it makes the file, before anything was asked)."""

    def without_account(settings):
        media = settings.get('media') if isinstance(settings.get('media'), dict) else {}
        return {**settings, 'media': {key: value for key, value in media.items() if key not in ('uid', 'gid')}}

    return bool(config) and without_account(config) != without_account(template)


def drives_named(config):
    """The settings name both drives a restore needs (the app drive and the one with the backups)."""
    storage = config.get('storage') if isinstance(config.get('storage'), dict) else {}
    return all(
        isinstance(storage.get(key), dict) and bool(storage[key].get('uuid')) for key in ('data_ssd', 'media_hdd')
    )


def own_disk(record):
    """The UUID of the disk a drive's saved record names, or '' for none: no drive chosen,
    a folder of the system disk, or a folder a NAS shares (NFS), which has no disk here."""
    if not isinstance(record, dict) or record.get('folder') is not None:
        return ''
    network = record.get('network') if isinstance(record.get('network'), dict) else {}
    if network.get('export'):
        return ''
    uuid = record.get('uuid')
    return uuid.strip() if isinstance(uuid, str) and '/' not in uuid else ''


def missing_drives(config, by_uuid=BY_UUID):
    """The drive roles ('data_ssd', 'media_hdd') whose saved disk is not connected now:
    unplugged, broken, or replaced by another one."""
    storage = config.get('storage') if isinstance(config.get('storage'), dict) else {}
    found = [(key, own_disk(storage.get(key))) for key in DRIVE_ROLES]
    return [key for key, uuid in found if uuid and not (Path(by_uuid) / uuid).exists()]


def missing_group_drives(config, by_uuid=BY_UUID):
    """{drive: what it holds, in words} for the drives of a group's own (a group's files or
    apps placed on them, 0.50.0) that are not connected now. A folder a NAS shares is not
    one: its NAS may only be off."""
    try:
        import placement
        from neko_common import drives_in_use, extra_drives

        found = {}
        for name in drives_in_use(config):
            record = extra_drives(config)[name]
            uuid = str(record.get('uuid', ''))
            network = record.get('network') if isinstance(record.get('network'), dict) else {}
            if not uuid or network.get('transport') == 'nfs' or (Path(by_uuid) / uuid).exists():
                continue
            rows = sorted(
                {
                    row
                    for section in ('locations', 'install')
                    if isinstance(config.get(section), dict)
                    for row, drive in config[section].items()
                    if drive == name
                }
            )
            found[name] = ' and '.join(placement.name_of(row) for row in rows)
        return found
    except (RuntimeError, KeyError, TypeError, ValueError, StopIteration):
        return {}


def planned_steps(config, states, root):
    """The steps this server's setup is meant to run, in their order (config_checks), or
    the recorded ones when the settings cannot say."""
    try:
        from config_checks import expected_steps

        wanted = expected_steps(config, read_object(Path(root) / SETTINGS_TEMPLATE))
    except (KeyError, TypeError, ValueError, AttributeError):
        wanted = []
    return [step for step in wanted if isinstance(step, str)] or sorted(states)


def look(root=ROOT, settings=None, units=SYSTEMD_UNITS, default_state=None, journals=JOURNALS, by_uuid=BY_UUID):
    """What is on this PC, read and not judged. `settings`: the settings file this run
    uses (my-config.json beside setup when not given). `default_state`: where setup keeps
    its record before the app drive is chosen (installer.DEFAULT_STATE)."""
    root = Path(root)
    own = Path(settings) if settings else root / MY_SETTINGS
    config = read_object(own)
    if not answered(config, read_object(root / SETTINGS_TEMPLATE)):
        config = {}  # made by setup a moment ago and never answered: not a saved configuration
    older = older_folders(root)
    # The settings of an earlier folder say where the app drive is, when this folder has none.
    known = config or (read_object(older[0][1]) if older else {})
    data = app_drive(known)
    any_unit, running = unit_versions(units)
    engine = below(data, 'docker-engine')
    states = step_states(data, default_state)
    return {
        'this_version': numbers(VERSION),
        'settings': str(own) if config else None,
        'older': [(version, str(path)) for version, path in older],
        'location': str(data) if data is not None else None,
        'units': any_unit,
        'unit_versions': running,
        'kept_versions': kept_versions(data),
        'engine': bool(engine is not None and engine.is_dir()),
        'states': states,
        'plan': planned_steps(known, states, root),
        'optional': sorted(optional_steps()),
        'jobs': cut_off_jobs(journals),
        'drives_named': drives_named(known),
        'missing': missing_drives(known, by_uuid),
        'missing_groups': missing_group_drives(known, by_uuid),
    }


def optional_steps():
    """The steps a finished server does not wait for (wizard.OPTIONAL)."""
    try:
        from wizard import OPTIONAL

        return set(OPTIONAL)
    except ImportError:
        return {'assistant'}


def steps_left(seen):
    """(the steps that did not finish, the ones that wait for the person), in the plan's
    order. An optional step is in neither: a server is finished without it."""
    states, optional = seen['states'], set(seen['optional'])
    broken, waiting = [], []
    for step in seen['plan']:
        state = states.get(step)
        if step in optional or state in SETTLED:
            continue
        (waiting if state == WAITING else broken).append(step)
    return broken, waiting


def server_there(seen):
    """Something of a server is installed: a schedule, Docker's folder on the app drive,
    or a setup step recorded as done. A settings file, or NekoServer's own folder, is not."""
    return seen['units'] or seen['engine'] or 'success' in seen['states'].values()


def installed_version(seen):
    """The release the server runs: the newest its schedules name, else the newest setup
    kept on the app drive. None when neither says."""
    for versions in (seen['unit_versions'], seen['kept_versions']):
        if versions:
            return versions[-1]
    return None


def cut_off(seen):
    """The job that was cut off and can go on, or None: {'kind', 'step', 'left'}. An
    upgrade or a drive move comes before setup's own steps (they hold the server
    half-changed); for setup, `step` is the first one that did not finish."""
    if seen['jobs']:
        return {'kind': seen['jobs'][0], 'step': None, 'left': []}
    broken, _ = steps_left(seen)
    if seen['states'] and broken:
        return {'kind': SETUP, 'step': broken[0], 'left': broken}
    return None


def kind_of(seen, job):
    if job is not None:
        return UNFINISHED
    if server_there(seen):
        return INSTALLED
    if seen['settings']:
        return SETTINGS_ONLY
    return OLDER_FOLDER if seen['older'] else NOTHING


def choices_for(kind, seen, version):
    """The choices that apply, the one offered first at the front. Never one that would
    do nothing here: no "continue" without a job that was cut off, no "review this
    update" unless the server runs an older release than this one, no "restore" before
    the settings name the drives the backups are on, no "change" without settings."""
    has_settings = bool(seen['settings'] or seen['older'])
    server = kind in (UNFINISHED, INSTALLED) and server_there(seen)
    offered = []
    if kind == UNFINISHED:
        offered.append(CONTINUE)
    if kind == INSTALLED and version is not None and version < seen['this_version']:
        offered.append(UPDATE)
    if has_settings and kind != NOTHING:
        offered.append(CHANGE)
    if server and has_settings and seen['drives_named']:
        offered.append(RESTORE)
    return offered


def judge(seen):
    """What `look` saw, as the page needs it:

    {'kind': nothing | settings_only | unfinished | installed | older_folder,
     'show': whether the page is shown at all (never on a PC with nothing),
     'version': the release the server runs ("0.49.0"), or None when not known,
     'this_version': the release of this copy of NekoServer,
     'location': the app drive's folder, or None,
     'settings': the settings file found (this folder's, else an earlier folder's), or None,
     'settings_from': the release of the earlier folder those settings are in, or None,
     'job': None, or {'kind': setup | upgrade | drive_move, 'step': the step setup goes
            on at (setup only), 'left': the steps that did not finish},
     'waiting': the steps that wait for the person (a server works without them),
     'missing': the drives the settings name that are not connected ('data_ssd', 'media_hdd'),
     'choices': the choices that apply, in the order to show them,
     'default': the one chosen when the person only presses Continue, or None}
    """
    job = cut_off(seen)
    kind = kind_of(seen, job)
    version = installed_version(seen) if kind in (UNFINISHED, INSTALLED) and server_there(seen) else None
    choices = choices_for(kind, seen, version)
    older = seen['older'][0] if seen['older'] and not seen['settings'] else None
    return {
        'kind': kind,
        'show': kind != NOTHING,
        'version': words(version),
        'this_version': words(seen['this_version']),
        'location': seen['location'] if kind in (UNFINISHED, INSTALLED) else None,
        'settings': seen['settings'] or (older[1] if older else None),
        'settings_from': words(older[0]) if older else None,
        'job': job,
        'waiting': steps_left(seen)[1] if kind in (UNFINISHED, INSTALLED) else [],
        'missing': list(seen.get('missing') or []) if kind != NOTHING else [],
        # The drives of a group's own that are not connected: {name: what they hold, in words}.
        'missing_groups': dict(seen.get('missing_groups') or {}) if kind != NOTHING else {},
        'choices': choices,
        'default': choices[0] if choices else None,
    }


def found(root=ROOT, settings=None, units=SYSTEMD_UNITS, default_state=None, journals=JOURNALS, by_uuid=BY_UUID):
    """What this PC has, and the choices that apply (`judge` of `look`)."""
    return judge(look(root, settings, units, default_state, journals, by_uuid))
