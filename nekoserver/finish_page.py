"""The finishing page of the setup window (0.50.0, the wizard: page P09).

The terminal ends with a summary in lines. The window shows the same facts as a page: a
heading that says the real outcome, a card for each app (its address, how it stands,
"Open" and "Show login", and the test a person can try in it, which can also be left for
later), the backups, what still waits by name, and the dashboard's address. No command
is on the page: where the terminal types one, the window has a button.

The facts are finish_states.summary's; the words are the terminal's (wizard.py), so the
two never say different things.
"""

import phone_guide
import wizard


def state_shown(label, found):
    """One state as the page shows it: its label, the state itself (for its mark) and
    the same words the terminal says."""
    return {'label': label, 'state': found.get('state', ''), 'words': wizard.state_said(found)}


def test_shown(test):
    """A test a person can try, with the answers the page offers for it."""
    title = wizard.TEST_TITLES.get(test['id'], str(test['id']).replace('_', ' ').capitalize())
    return {'id': test['id'], 'title': title, 'state': test.get('state', ''), 'words': wizard.state_said(test)}


def app_shown(config, app, tests):
    """An app's card: where it opens, how it stands, the login its button shows and the
    test a person can try in it (None when it has none)."""
    states = [
        state_shown(label, app['states'][key])
        for key, label in wizard.APP_STATES
        if key in ('installed', 'login', 'address')
    ]
    return {
        'id': app['id'],
        'name': app['name'],
        'url': app.get('url') or app.get('local_url') or '',
        'states': states,
        'login': phone_guide.login_of(config, app['id']) if app['id'] in phone_guide.LOGIN else '',
        'test': tests.get(app.get('test')),
    }


def item_said(item, names):
    """Something that still waits or needs fixing, by name, in the terminal's words."""
    kind, _, what = str(item.get('id', '')).partition(':')
    if kind == 'step':
        name = wizard.title(what)
    elif kind == 'backup':
        name = 'Backup: ' + what.replace('_', ' ')
    else:
        name = names.get(what, what.capitalize())
    return f'{name}: {wizard.state_said(item)}'


def dashboard_shown(config, pin_set):
    """The dashboard's address on this PC and what its PIN is for, in plain words."""
    port = wizard.dashboard_port(config)
    if not port:
        return None
    pin = (
        'A dashboard PIN is set: with it you can restart apps and change fans and lights from the dashboard.'
        if pin_set
        else 'No dashboard PIN is set: phones can only look. Run setup again to choose one.'
    )
    return {'url': f'http://127.0.0.1:{port}', 'pin': pin}


def page(config, summary, pin_set=False):
    """The finishing page from the judged facts (finish_states.summary); None without them."""
    if not summary:
        return None
    tests = {test['id']: test_shown(test) for test in summary.get('tests', [])}
    apps = [app_shown(config, app, tests) for app in summary.get('apps', [])]
    in_apps = {app['test']['id'] for app in apps if app['test']}
    names = {app['id']: app['name'] for app in summary.get('apps', [])}
    server = summary.get('server', {})
    return {
        'heading': wizard.OUTCOMES.get(summary.get('outcome'), wizard.OUTCOMES['pending']),
        'outcome': summary.get('outcome', 'pending'),
        'apps': apps,
        'backups': [
            state_shown('Backup made', server.get('backup', {})),
            state_shown('Restore tried', server.get('restore', {})),
        ],
        # The tests that belong to no app's card (the TV, a restore).
        'tests': [test for name, test in tests.items() if name not in in_apps],
        'attention': [item_said(item, names) for item in summary.get('attention', [])],
        # And each group set up again with empty settings on the drive that replaced its lost one.
        'waiting': [item_said(item, names) for item in summary.get('pending', [])] + wizard.started_empty_lines(),
        'dashboard': dashboard_shown(config, pin_set),
        # The server's own secrets chosen on the accounts page, each with Show login.
        'secrets': secrets_shown(config),
    }


def secrets_shown(config):
    """The dashboard PIN and the shared folders' password the account store keeps."""
    import account_store

    return [
        {'name': name, 'login': key} for key, name in account_store.OTHERS.items() if account_store.other(config, key)
    ]


def offered(shown):
    """(the logins, the tests) this page has a button for; nothing else is answered."""
    if not isinstance(shown, dict):
        return set(), set()
    apps = shown.get('apps', [])
    tests = [app['test']['id'] for app in apps if app.get('test')] + [test['id'] for test in shown.get('tests', [])]
    logins = {app['login'] for app in apps if app.get('login')} | {s['login'] for s in shown.get('secrets', [])}
    return logins, set(tests)
