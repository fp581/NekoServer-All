"""Apply saved account choices before media connectors and private gateways."""
import guided_setup
import account_setup
import account_store
from neko_common import verify_storage
from recovery_tools import lock

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Provision and authenticate app accounts from private saved choices'


def run(ctx):
    if not account_store.enabled(ctx.config):
        return
    if not ctx.apply:
        ctx.log('Accounts: verify/provision only during apply; existing accounts are not reset')
        return
    verify_storage(ctx.config)
    apps = account_store.selected(ctx.config, getattr(ctx, 'plan', ('media', 'photos', 'nextcloud')))
    if not apps:
        # Only this step is running (the accounts profile, or a retry): every app set up
        # in the settings, never an app switched off since (its saved login stays saved).
        apps = account_store.selected(ctx.config, ('media', 'photos', 'nextcloud'))
    with lock(ctx.config):
        pending = account_setup.provision(ctx.config, apps, output=ctx.log)
    pending = finish_pending(ctx, pending)
    if pending:
        return {'status': 'needs_setup', 'reason': 'Account setup pending for: ' + ', '.join(pending) + '; saved passwords preserved'}
    ctx.log('Selected app logins verified; private APP-CREDENTIALS.txt updated')


def finish_pending(ctx, pending):
    """Keep saved choices; open manual setup only for accounts the API could not finish."""
    remaining = []
    for app in pending:
        def verify(app=app):
            with lock(ctx.config):
                return not account_setup.provision(ctx.config, [app], output=ctx.log)
        title, port = account_store.APPS[app]
        done = guided_setup.checkpoint(ctx, 'Finish ' + title, [
            'Open the app and complete its remaining first-run or verification steps. '
            'If data already exists, use its current account; do not reset it.',
            'Use the saved username/email and password from the private '
            'secrets/accounts/APP-CREDENTIALS.txt under your data SSD mount.',
            'For an email verification explicitly requested by the app, open your email app '
            'and approve the message. Local email-shaped logins usually do not need this.',
            'If the saved credentials are wrong, leave this step pending with Ctrl+C, '
            'then rerun account-setup.py to enter the current login.',
            'Return here and answer yes. The installer retries the authenticated account check.'
        ], [('url', 'http://127.0.0.1:' + str(port) + '/')], verify=verify)
        if not done:
            remaining.append(app)
    return remaining
