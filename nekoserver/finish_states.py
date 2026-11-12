"""How the finished server stands, for setup's last page (P09, "ready / remaining tasks").

Four things are kept apart for every app that was chosen, and two for the server, because
they are different kinds of knowledge:

    installed   its setup step finished and its container runs        (a program checked)
    login       NekoServer signed in to it with the saved login       (a program checked)
    address     the app answers at its address on this PC             (a program checked)
    tried       a person used it and said it worked                   (a person said so)
    backup      each backup that is set up has run and passed         (a program checked)
    restore     a person restored from a backup and said it worked    (a person said so)

Each is one of: done, failed, not_tested, waiting. Nothing a person has not tried is ever
"done": an empty library is not a failed playback test and not a passed one, and a backup
that exists is not a restore that was tried. There is no single "all passed".

`summary` judges facts and reads nothing. `collect` reads the facts from the server.
`record_test` keeps what a person said about a test ("it worked", "it did not", "later").
"""

import json
import os
import time
from pathlib import Path

from neko_common import APPS, app_port, atomic_json, immich_wanted, media_services, mount_path

DONE, FAILED, NOT_TESTED, WAITING = 'done', 'failed', 'not_tested', 'waiting'
READY, PENDING, ATTENTION = 'ready', 'pending', 'attention'
# What a person can say about a test, and what is kept (record_test).
SEEN, NOT_WORKING, LATER = 'seen', 'not_working', 'later'

# The setup step that installs each app (every media app comes with the "media" step).
STEP_OF = {'immich': 'photos', 'nextcloud': 'nextcloud'}
# A step a finished server does not wait for (wizard.OPTIONAL and acceptance.OPTIONAL).
OPTIONAL_STEPS = frozenset(('assistant', 'tv', 'notifications', 'heartbeat'))
# The one thing a person tries in each app: (test, the app it belongs to, whether an
# empty library means there is nothing to try yet).
APP_TESTS = (
    ('play_video', 'jellyfin', True),
    ('play_song', 'navidrome', True),
    ('phone_photo', 'immich', False),
    ('cloud_file', 'nextcloud', False),
)
# A TV connected to this PC: each seen on its own, never "everything worked".
TV_TESTS = ('tv_picture', 'tv_sound', 'tv_controller', 'tv_steam')
# A witnessed test of the acceptance checklist that shows the same thing (acceptance.manual_tests).
SAME_AS = {'play_video': 'media_workflow', 'play_song': 'music_playback', 'cloud_file': 'cloud_client'}
# Each backup's step, and its report in reports/jobs (first_checkup.JOBS).
BACKUPS = (
    ('app_backup', 'applications'),
    ('host_backup', 'host'),
    ('music_backup', 'music'),
    ('photo_preservation', 'database'),
    ('photo_backup', 'external'),
)
# The restores a person can try, each with the backup step it reads from (acceptance.restore_tests).
RESTORES = (
    ('app_restore', 'app_backup'),
    ('host_restore', 'host_backup'),
    ('music_restore', 'music_backup'),
    ('independent_restore', 'photo_preservation'),
    ('cloud_restore', 'photo_backup'),
)
TESTS_FILE = ('reports', 'setup', 'person-tests.json')
RUNNING_STATES = ('running',)
BROKEN_STEP = ('failed', 'blocked', 'running', 'missing')
LONGEST_FILE = 2_000_000


def row(state, reason=None, **action):
    """One state: what it is, why (a word, never free text), and the named next action."""
    return {'state': state, 'reason': reason, 'action': action or None}


def step_of(app):
    return STEP_OF.get(app, 'media')


def step_state(facts, step):
    return facts['steps'].get(step, 'missing')


def installed(facts, app):
    """The app's setup step finished and its container runs."""
    step = step_of(app)
    state = step_state(facts, step)
    if state == 'needs_setup':
        return row(WAITING, 'step_waits_for_you', do='finish_step', step=step)
    if state != 'success':
        return row(FAILED, 'step_did_not_finish', do='retry_step', step=step)
    if facts['containers'] is None:
        return row(NOT_TESTED, 'docker_did_not_answer')
    container, health = facts['containers'].get(app, ('missing', 'unknown'))
    if container in RUNNING_STATES and health == 'starting':
        return row(WAITING, 'still_starting')
    if container in RUNNING_STATES and health != 'unhealthy':
        return row(DONE)
    if container == 'missing':
        return row(FAILED, 'container_missing', do='retry_step', step=step)
    return row(FAILED, 'container_not_running', do='restart_app', app=app)


def login(facts, app, is_installed):
    """NekoServer signed in to the app with the login it keeps for it."""
    if not is_installed:
        return row(WAITING, 'not_installed_yet')
    state = facts['logins'].get(app)
    if state == 'verified':
        return row(DONE)
    if state == 'needs_setup':
        return row(FAILED, 'saved_login_refused', do='enter_login', app=app)
    if state in ('planned', 'started'):
        return row(WAITING, 'account_step_not_finished', do='finish_step', step='accounts')
    return row(NOT_TESTED, 'no_login_kept')


def address(facts, app, is_installed):
    """The app answers at its address on this PC."""
    if not is_installed:
        return row(WAITING, 'not_installed_yet')
    answer = facts['answers'].get(app)
    if answer is True:
        return row(DONE)
    if answer is False:
        return row(FAILED, 'no_answer', do='restart_app', app=app)
    return row(NOT_TESTED, 'not_asked')


def said(facts, test):
    """What a person said about a test (seen, not_working, later), or None: their own
    word first, else the same test passed or failed in the acceptance checklist."""
    own = facts['person'].get(test)
    if own in (SEEN, NOT_WORKING, LATER):
        return own
    return {'pass': SEEN, 'fail': NOT_WORKING}.get(facts['witnessed'].get(SAME_AS.get(test)))


def tried(facts, test, app=None, needs_library=False, usable=True):
    """A person used it and said it worked. Only their word makes it done or failed."""
    word = said(facts, test)
    if word == SEEN:
        return row(DONE)
    if word == NOT_WORKING:
        action = {'do': 'open_app', 'app': app} if app else {}
        return row(FAILED, 'you_said_it_did_not_work', **action)
    if not usable:
        return row(WAITING, 'not_installed_yet')
    if needs_library and facts['library'].get(app) == 0:
        # Nothing to play yet: not a failure of the installation, and not a passed test.
        return row(
            NOT_TESTED, 'empty_library', do='add_media', app=app, also=['open_app', 'choose_test_file', 'test_later']
        )
    if word == LATER:
        return row(WAITING, 'you_chose_later', do='try', test=test)
    return row(NOT_TESTED, 'nobody_tried_yet', do='try', test=test)


def app_rows(facts):
    """Every chosen app with its four states, and the test a person can try in it."""
    tests = {app: (test, needs_library) for test, app, needs_library in APP_TESTS}
    found = []
    for app in facts['apps']:
        is_installed = installed(facts, app['id'])
        ready = is_installed['state'] == DONE
        states = {
            'installed': is_installed,
            'login': login(facts, app['id'], ready),
            'address': address(facts, app['id'], ready),
        }
        if app['id'] in tests:
            test, needs_library = tests[app['id']]
            states['tried'] = tried(facts, test, app['id'], needs_library, ready)
        found.append({**app, 'test': tests.get(app['id'], (None,))[0], 'states': states})
    return found


def backup_rows(facts):
    """Each backup that this server's setup has, with whether it ran and passed."""
    found = []
    for step, report in BACKUPS:
        state = facts['steps'].get(step)
        if state in (None, 'unavailable'):
            continue  # not part of this server
        if state == 'needs_setup':
            made = row(WAITING, 'step_waits_for_you', do='finish_step', step=step)
        elif state != 'success':
            made = row(FAILED, 'step_did_not_finish', do='retry_step', step=step)
        else:
            made = backup_run(facts['jobs'].get(report), report)
        found.append({'id': report, 'step': step, **made})
    return found


def backup_run(status, report):
    if status == 'pass':
        return row(DONE)
    if status == 'running':
        return row(WAITING, 'running_now')
    if status == 'partial':
        return row(FAILED, 'left_something_out', do='open_backups', backup=report)
    if status == 'fail':
        return row(FAILED, 'did_not_finish', do='open_backups', backup=report)
    return row(NOT_TESTED, 'has_not_run_yet', do='run_backup', backup=report)


def together(rows):
    """Several rows as one state: failed if any failed; not tested if any was never
    run; done only when at least one is done and none is failed or untested; else waiting."""
    states = [r['state'] for r in rows]
    for state in (FAILED, NOT_TESTED):
        if state in states:
            return state
    if DONE in states:
        return DONE
    return WAITING if states else NOT_TESTED


def restore_rows(facts):
    """Each restore a person can try on this server. A backup that exists says nothing here."""
    found = []
    for test, step in RESTORES:
        if facts['steps'].get(step) != 'success':
            continue
        word = {'pass': SEEN, 'fail': NOT_WORKING}.get(facts['witnessed'].get(test)) or facts['person'].get(test)
        if word == SEEN:
            tried_it = row(DONE)
        elif word == NOT_WORKING:
            tried_it = row(FAILED, 'you_said_it_did_not_work', do='open_recovery')
        elif word == LATER:
            tried_it = row(WAITING, 'you_chose_later', do='try_restore', test=test)
        else:
            tried_it = row(NOT_TESTED, 'nobody_tried_yet', do='try_restore', test=test)
        found.append({'id': test, **tried_it})
    return found


def tv_rows(facts):
    """Picture, sound, controller and Steam on the TV, each seen on its own."""
    if facts['steps'].get('tv') != 'success':
        return []
    return [{'id': test, **tried(facts, test)} for test in TV_TESTS]


def step_items(facts):
    """(what makes the server need attention, what is only still pending), from the
    setup steps: a step that did not finish is the first unless it is optional; one
    that waits for the person is always the second."""
    attention, pending = [], []
    for step in facts['plan']:
        state = step_state(facts, step)
        if state == 'needs_setup':
            pending.append({'id': 'step:' + step, **row(WAITING, 'step_waits_for_you', do='finish_step', step=step)})
        elif state in BROKEN_STEP:
            item = {'id': 'step:' + step, **row(FAILED, 'step_did_not_finish', do='retry_step', step=step)}
            (pending if step in OPTIONAL_STEPS else attention).append(item)
    return attention, pending


def app_items(apps):
    """(attention, pending) from the apps: one that is not installed or does not answer
    needs attention; a login NekoServer could not use, or an app still starting, is pending."""
    attention, pending = [], []
    for app in apps:
        for name in ('installed', 'address', 'login'):
            state = app['states'][name]
            item = {'id': f"{name}:{app['id']}", **state}
            if state['state'] == FAILED and name != 'login':
                attention.append(item)
            elif state['state'] == FAILED or (state['state'] == WAITING and state['reason'] != 'not_installed_yet'):
                pending.append(item)
    return attention, pending


def backup_items(backups):
    attention = [{**b, 'id': 'backup:' + b['id']} for b in backups if b['state'] == FAILED]
    pending = [{**b, 'id': 'backup:' + b['id']} for b in backups if b['state'] in (WAITING, NOT_TESTED)]
    return attention, pending


def outcome(attention, pending):
    """The page's heading: something needed failed; else something is still to do; else ready."""
    if attention:
        return ATTENTION
    return PENDING if pending else READY


def counted(rows):
    """How many rows are in each state."""
    return {state: sum(r['state'] == state for r in rows) for state in (DONE, FAILED, NOT_TESTED, WAITING)}


def summary(facts):
    """The finished server, judged:

    {'outcome': ready | pending | attention,
     'ready': True unless something the server needs failed (pending tasks leave it True),
     'apps': [{'id', 'name', 'url', 'local_url', 'test',
               'states': {'installed', 'login', 'address', 'tried' (when the app has a test)}}],
     'server': {'backup': one state for all backups, 'restore': one for all restores},
     'backups': [{'id', 'step', state...}], 'restores': [{'id', state...}], 'tv': [{'id', state...}],
     'attention': what failed and is needed, 'pending': what waits or was put off,
     'tests': every test a person can try, with its state,
     'checked': counts of what programs checked, 'tried': counts of what people tried}

    A state is {'state': done | failed | not_tested | waiting, 'reason': a word or None,
    'action': None or {'do': ..., and what it is done to}}.
    """
    apps = app_rows(facts)
    backups, restores, tv = backup_rows(facts), restore_rows(facts), tv_rows(facts)
    attention, pending = [], []
    for found in (step_items(facts), app_items(apps), backup_items(backups)):
        attention += [item for item in found[0] if item['id'] not in {a['id'] for a in attention}]
        pending += found[1]
    tests = [{'id': app['test'], 'app': app['id'], **app['states']['tried']} for app in apps if app['test']]
    tests += tv + restores
    machine = [app['states'][name] for app in apps for name in ('installed', 'login', 'address')] + backups
    return {
        'outcome': outcome(attention, pending),
        'ready': not attention,
        'apps': apps,
        'server': {
            'backup': row(together(backups), None if backups else 'no_backup_set_up'),
            'restore': row(together(restores), None if restores else 'no_backup_to_restore_from'),
        },
        'backups': backups,
        'restores': restores,
        'tv': tv,
        'attention': attention,
        'pending': pending,
        'tests': tests,
        'checked': counted(machine),
        'tried': counted(tests),
    }


# ---- reading the facts from the server -------------------------------------------------


def app_drive(config):
    """The app drive's folder as the settings name it, every link followed."""
    return Path(os.path.realpath(mount_path(config['data_mount'])))


def below(data, parts):
    """`parts` under the app drive, or None when that leaves it (a link on the way)."""
    inside = os.path.realpath(os.path.join(data, *parts))
    return Path(inside) if inside.startswith(str(data) + os.sep) else None


def read_object(path):
    try:
        if path is None or path.stat().st_size > LONGEST_FILE:
            return {}
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def chosen_apps(config, links=None):
    """The apps this server is meant to have, each with its address on this PC and the
    address the phone uses when there is one."""
    links = links if isinstance(links, dict) else {}
    selected = list(media_services(config))
    if immich_wanted(config):
        selected.append('immich')
    if config.get('nextcloud', {}).get('enabled') is True:
        selected.append('nextcloud')
    found = []
    for app in (a for a in APPS if a in selected):
        local = f'http://127.0.0.1:{app_port(app)}/'
        url = links.get(app) if isinstance(links.get(app), str) else None
        found.append({'id': app, 'name': APPS[app][0], 'url': url or local, 'local_url': local})
    return found


def step_statuses(data):
    states = read_object(below(data, ('installer', 'state', 'state.json')))
    return {
        str(name): str(state['status'])
        for name, state in states.items()
        if isinstance(state, dict) and isinstance(state.get('status'), str)
    }


def job_statuses(data):
    """How each backup's last run ended, from its own report (reports/jobs)."""
    found = {}
    for _, report in BACKUPS:
        status = read_object(below(data, ('reports', 'jobs', report + '.json'))).get('status')
        found[report] = status if status in ('pass', 'partial', 'fail', 'running') else None
    return found


def witnessed_tests(data):
    """The acceptance checklist's tests a person recorded as passed or failed."""
    report = read_object(below(data, ('reports', 'acceptance', 'latest.json')))
    rows = report.get('manual') if isinstance(report.get('manual'), list) else []
    return {
        r['id']: r['status']
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get('id'), str)
        and r.get('status') in ('pass', 'fail')
        and r.get('evidence') == 'user_reported'
    }


def person_tests(data):
    """What a person said on this page about each test (record_test)."""
    saved = read_object(below(data, TESTS_FILE)).get('tests')
    saved = saved if isinstance(saved, dict) else {}
    return {
        str(test): entry['said']
        for test, entry in saved.items()
        if isinstance(entry, dict) and entry.get('said') in (SEEN, NOT_WORKING, LATER)
    }


def login_states(config):
    """Each app's saved login as the accounts step left it (verified, needs_setup, ...):
    the state only, never the login itself."""
    try:
        import account_store

        apps = account_store.load(config).get('apps', {})
    except (RuntimeError, OSError, ValueError, KeyError, ImportError):
        return {}
    return {app: entry.get('state') for app, entry in apps.items() if isinstance(entry, dict)}


def container_states(config):
    """{app: (state, health)} from Docker, or None when Docker did not answer."""
    try:
        import status_checks

        found = status_checks.applications(config)
    except (RuntimeError, OSError, ValueError, ImportError):
        return None
    if found and all(app['state'] == 'unknown' for app in found):
        return None
    return {app['id']: (app['state'], app['health']) for app in found}


def answering(apps, opener=None, timeout=5):
    """Whether each app answers at its address on this PC: any answer from the app
    itself counts, an error page too (it is there); no connection does not."""
    import urllib.error

    from neko_common import local_opener

    opener = opener or local_opener()
    found = {}
    for app in apps:
        try:
            with opener.open(app['local_url'], timeout=timeout):
                found[app['id']] = True
        except urllib.error.HTTPError as answer:
            answer.close()
            found[app['id']] = answer.code < 500
        except (OSError, ValueError):
            found[app['id']] = False
    return found


def collect(config, plan, read=None):
    """The facts `summary` judges, read from the server. `plan`: the steps this server's
    setup runs (wizard.planned). `read`: stand-ins for the parts that ask a program
    ('containers', 'logins', 'answers', 'library'), each a function of (config, apps)."""
    read = read or {}
    data = app_drive(config)
    apps = chosen_apps(config, read_object(below(data, ('reports', 'gateway-tailnet.json'))).get('links'))

    def asked(name, default):
        return read[name](config, apps) if name in read else default()

    return {
        'apps': apps,
        'plan': list(plan),
        'steps': step_statuses(data),
        'containers': asked('containers', lambda: container_states(config)),
        'logins': asked('logins', lambda: login_states(config)),
        'answers': asked('answers', lambda: answering(apps)),
        'library': asked('library', dict),
        'jobs': job_statuses(data),
        'witnessed': witnessed_tests(data),
        'person': person_tests(data),
    }


def known_tests():
    return {test for test, *_ in APP_TESTS} | set(TV_TESTS) | {test for test, _ in RESTORES}


def record_test(config, test, word, now=None):
    """Keep what a person said about a test: SEEN, NOT_WORKING or LATER. Only a test this
    page has, and only one of the three words; anything else is refused."""
    if test not in known_tests() or word not in (SEEN, NOT_WORKING, LATER):
        raise ValueError('Not a test of this page, or not an answer to one')
    data = app_drive(config)
    path = below(data, TESTS_FILE)
    if path is None or not data.is_dir():
        raise RuntimeError('The app drive is not where the settings say, so the answer was not kept')
    saved = read_object(path)
    tests = saved.get('tests') if isinstance(saved.get('tests'), dict) else {}
    tests[test] = {'said': word, 'at': time.time() if now is None else now}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(path, {'format': 'neko-person-tests-v1', 'tests': tests})
    return tests[test]
