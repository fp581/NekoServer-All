"""Selected-app HTTPS gateways; never publish Funnel or change Docker bindings."""

import guided_setup
import native_runtime
import permission_checks
import hashlib, html, ipaddress, json, os, pwd, re, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.parse import urlsplit
import status_checks as health
import recovery_tools
from network_access import lan, verify_lan, choose_lan, save_settings
import host
from neko_common import (
    VERSION,
    safe_path,
    atomic_write,
    atomic_json,
    ensure_directory,
    local_opener,
    app_port,
    SYSTEMD_UNITS,
    dashboard_port,
)

LOOPBACK = 'http://127.0.0.1:'
# (name on the phone page, local port, private Tailscale port)
APPS = {
    app: (name, app_port(app), public)
    for app, name, public in (
        ('jellyfin', 'Jellyfin', 8443),
        ('seerr', 'Seerr', 8444),
        ('immich', 'Immich', 8445),
        ('navidrome', 'Music', 8446),
        ('nextcloud', 'Nextcloud', 8447),
    )
}
ROOT = Path(__file__).resolve().parent
# The home addresses' usual ports (the app menu, then the apps as in APPS). When another
# program on the PC has one, setup takes the next free one of the spares instead and
# keeps the choice in the settings ("lan_ports" under "gateway").
LAN_DEFAULTS = {'portal': 8442, **{app: APPS[app][2] for app in APPS}}
SPARE_PORTS = range(8450, 8500)


def lan_ports(config):
    """{app: port} for the home addresses of the app menu ('portal') and the selected
    apps: the usual ports, or the ones setup chose instead."""
    chosen = config.get('gateway', {}).get('lan_ports', {})
    out = {}
    if not isinstance(chosen, dict):
        chosen = None
    if chosen is None:
        raise RuntimeError('"lan_ports" under "gateway" must be an object')
    for app in ['portal', *selected(config)]:
        port = chosen.get(app, LAN_DEFAULTS[app])
        if type(port) is not int or not 1024 <= port <= 65535:
            raise RuntimeError(f'"lan_ports" under "gateway": {app} must be a port number from 1024 to 65535')
        out[app] = port
    if len(set(out.values())) != len(out):
        raise RuntimeError('"lan_ports" under "gateway": two apps have the same port')
    return out


def owned_listeners(config):
    """ip:port pairs the running NekoServer home gateway itself listens on (its own ports
    are not "taken")."""
    path = LAN_CONFIG
    ledger = managed(Path(config['data_mount']) / 'gateway/lan-managed.json')
    old = health.read_json(path)
    rc, _ = health.run(['systemctl', 'is-active', '--quiet', 'nekoserver-gateway-lan.service'])
    owned = set()
    if (
        rc == 0
        and path.is_file()
        and ledger.get('sha256', {}).get(str(path)) == hashlib.sha256(path.read_bytes()).hexdigest()
    ):
        for server in old.get('apps', {}).get('http', {}).get('servers', {}).values():
            owned.update(server.get('listen', []))
    return owned


def port_free(ip, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Connections the gateway closed a moment ago (TIME_WAIT) do not make its own
            # port look taken; a program listening on it still does.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((ip, port))
    except OSError:
        return False
    return True


def choose_lan_ports(ctx):
    """The home addresses' ports: the usual ones, except where another program on this PC
    already has one, which gets the next free spare instead (saved for later runs, so an
    address never changes once given out)."""
    config = ctx.config
    ip, _ = lan(config)
    ports = lan_ports(config)
    owned = owned_listeners(config)
    # Ports given out before stay with their app, also while it is not selected: its old
    # address never opens another app, and it gets the same one back.
    saved = {k: v for k, v in config.get('gateway', {}).get('lan_ports', {}).items() if type(v) is int}
    taken = set(ports.values()) | set(saved.values())
    changed = []
    for app, port in ports.items():
        if ip + ':' + str(port) in owned or port_free(ip, port):
            continue
        spare = next((p for p in SPARE_PORTS if p not in taken and port_free(ip, p)), None)
        if spare is None:
            raise RuntimeError(
                f'Port {port} on {ip} is used by another program, and none of the spare ports {SPARE_PORTS.start}-{SPARE_PORTS.stop - 1} is free either'
            )
        taken.add(spare)
        ports[app] = spare
        changed.append((app, port, spare))
    for app, port, spare in changed:
        ctx.log(
            f'Home address: port {port} is used by another program on this PC, so {APPS[app][0] if app in APPS else "the app menu"} uses port {spare} instead'
        )
    if changed:
        config.setdefault('gateway', {})['lan_ports'] = {**saved, **ports}
        save_settings(ctx)
    return ports


LAN_CONFIG = Path('/etc/nekoserver-gateway/lan.json')  # the home gateway's Caddy settings
# The record of the files the gateway step wrote (their checksums, caddy's account) lives on
# the SSD beside the certificates; a copy stays on the system disk beside the files
# themselves, so a replaced SSD (RECOVERY.md) does not make setup refuse them as someone else's.
LAN_RECORD = Path('/etc/nekoserver-gateway/lan-managed.json')


def managed(ledger, copy=LAN_RECORD):
    """What the gateway step wrote last time: the SSD's record, or its copy on the system
    disk when the SSD is new (replaced after it broke; the files it describes are still here)."""
    saved = health.read_json(ledger)
    return saved if saved.get('sha256') else health.read_json(copy)


HTTPS = 'https://'
OCC_GET, OCC_SET, OCC_DELETE = 'config:system:get', 'config:system:set', 'config:system:delete'


def command(args, timeout=30, env=None):
    what = ' '.join(args[:3])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except OSError:
        raise RuntimeError(f'Gateway command could not run ({what})') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Gateway command took over {timeout} seconds ({what})') from None
    if p.returncode:
        # The command's own last error line says what to fix (no values are in it).
        last = next((line.strip() for line in reversed(p.stderr.splitlines()) if line.strip()), 'no reason given')
        raise RuntimeError(f'Gateway command failed ({what}): {last[-300:]}')
    return p.stdout


# The gateway list as config.json ships it; a list that still reads like this follows
# the apps you set up (Immich off means no Immich address), while a list you changed
# yourself is taken as it is, and an app in it that isn't set up stops setup.
DEFAULT_SERVICES = ('jellyfin', 'seerr', 'immich', 'navidrome', 'nextcloud')


def installed(config, app):
    if app == 'nextcloud':
        return bool(config.get('nextcloud', {}).get('enabled'))
    if app == 'immich':
        return bool(config.get('photos', {}).get('deploy_immich'))
    return app in config.get('media', {}).get('services', [])


def selected(config):
    apps = config.get('gateway', {}).get('services', list(APPS))
    if not isinstance(apps, list) or not apps or len(apps) != len(set(apps)) or not set(apps) <= set(APPS):
        raise RuntimeError('Unknown gateway application')
    if list(apps) == list(DEFAULT_SERVICES):
        apps = [a for a in apps if installed(config, a)]
        if not apps:
            raise RuntimeError('No app for the gateway: none of the default ones is set up')
        return apps
    for app in apps:
        if not installed(config, app):
            raise RuntimeError(
                f'"{app}" is in the gateway list ("services" under "gateway") but is not set up on this server; '
                'take it out of the list or set the app up'
            )
    return apps


def logins_verified(config, apps):
    """Every one of these apps has a saved login the accounts step verified (and, for
    Seerr and Jellyfin, their first-run setup done with it)."""
    import account_store

    try:
        if not account_store.enabled(config):
            return False
        return all(account_store.get(config, app, verified=True) for app in apps)
    except (OSError, RuntimeError, KeyError, ValueError):
        return False


def configure(ctx, kind):
    cfg = ctx.config
    apps = selected(cfg)
    opts = cfg.setdefault('gateway', {})
    changed = False
    if 'services' not in opts:
        opts['services'] = list(apps)
        changed = True
    # The same choice as LAN folder sharing (Samba), asked once by whichever runs first.
    if kind == 'lan' and not choose_lan(ctx):
        return False
    if opts.get('confirmed_services') != apps:
        if logins_verified(cfg, apps):
            # The accounts step signed into every one of these apps with its saved login
            # a moment ago (their first-run setup done, no public sign-up): nothing is
            # left to check by hand.
            ctx.log('App logins: verified by the accounts step for ' + ', '.join(apps) + '; no manual check needed')
        elif not sys.stdin.isatty():
            return False
        elif getattr(ctx, 'apply', False):
            if not guided_setup.app_security(ctx, apps, {a: APPS[a][1] for a in apps}):
                return False
        else:
            print(
                'Create administrator accounts, disable unwanted public signup and verify login/access permissions for: '
                + ', '.join(apps)
            )
            if input('After verifying those settings, type SECURED to enable access: ').strip() != 'SECURED':
                return False
        opts['confirmed_services'] = list(apps)
        changed = True
    if changed:
        save_settings(ctx)
    return True


# The apps whose first-run setup must be finished before they get a home address:
# (app, the address that says so, the field that is true once it is).
# Each app's own word that its first account exists, checked before it is published: an
# app reset or restored empty would otherwise let whoever opens it first become its admin.
ONBOARDING = (
    ('jellyfin', '/System/Info/Public', 'StartupWizardCompleted'),
    ('seerr', '/api/v1/settings/public', 'initialized'),
    ('immich', '/api/server/config', 'isInitialized'),
    ('nextcloud', '/status.php', 'installed'),
)
# Navidrome says it in its page: firstTime is true until its first account is made.
NAVIDROME_FIRST_TIME = re.compile(r'firstTime\\?"\s*:\s*true')


def onboarded(opener, app, endpoint, key):
    """The app answers on this PC and says its first-run setup is finished."""
    try:
        with opener.open(LOOPBACK + str(APPS[app][1]) + endpoint, timeout=5) as r:
            value = json.loads(r.read(64000))
    except (OSError, ValueError) as exc:
        # Usually the app is not running (its own step names why): say which one.
        raise RuntimeError(
            f'{APPS[app][0]} did not answer on this PC ({LOOPBACK}{APPS[app][1]}: {getattr(exc, "reason", exc)}); '
            'the home addresses are set up once it runs: see the apps step above, then run setup again'
        ) from None
    if not isinstance(value, dict) or value.get(key) is not True:
        raise RuntimeError(incomplete(app))


def incomplete(app):
    return (
        f'{APPS[app][0]} has no account yet (its first-run setup is not finished), so it is not put on the home '
        'or Tailscale address, where whoever opened it first would become its administrator. Finish its setup '
        f'on this PC ({LOOPBACK}{APPS[app][1]}/), then run setup again'
    )


def navidrome_onboarded(opener):
    """Navidrome has its first account (its page no longer says firstTime)."""
    try:
        with opener.open(LOOPBACK + str(APPS['navidrome'][1]) + '/app/', timeout=5) as r:
            page = r.read(200000).decode('utf-8', 'replace')
    except OSError as exc:
        raise RuntimeError(
            f'{APPS["navidrome"][0]} did not answer on this PC ({getattr(exc, "reason", exc)}); see the music step above, then run setup again'
        ) from None
    if NAVIDROME_FIRST_TIME.search(page):
        raise RuntimeError(incomplete('navidrome'))


def unverified_containers(config, apps):
    """The check rows (acceptance.item) of the selected apps' containers whose mounts,
    networks or ports are not the ones setup made, or that are still starting."""
    from acceptance import containers

    scoped = dict(config)
    scoped['media'] = {**config.get('media', {}), 'services': [a for a in apps if a not in ('immich', 'nextcloud')]}
    checked = containers(scoped) if scoped['media']['services'] else []
    if 'immich' in apps:
        checked += containers(config, photos=True)
    if 'nextcloud' in apps:
        from cloud_checks import check

        checked += check(config)
    return [r for r in checked if r['status'] != 'pass']


# How long the gateway step waits for a container whose health check is still running.
STARTING_WAIT = 120


def readiness(config, sleep=time.sleep, clock=time.monotonic):
    from acceptance import STARTING

    apps = selected(config)
    if config.get('gateway', {}).get('confirmed_services') != apps:
        raise RuntimeError('Explicit secured-account confirmation required for selected applications')
    opener = local_opener()
    for app, endpoint, key in ONBOARDING:
        if app in apps:
            onboarded(opener, app, endpoint, key)
    if 'navidrome' in apps:
        navidrome_onboarded(opener)
    # A snap's Docker takes over Docker's socket: every container would look missing.
    if host.snap_docker():
        raise RuntimeError(host.SNAP_DOCKER)
    # Docker started the apps a moment ago (a drive came back, a step before this one):
    # a container's health check says "starting" for a minute or two, and the step
    # failed on it at once. Such containers are waited for; anything else wrong is said.
    deadline = clock() + STARTING_WAIT
    while True:
        failed = unverified_containers(config, apps)
        if not failed or clock() >= deadline or any(r.get('detail') != STARTING for r in failed):
            break
        sleep(10)
    if failed:
        raise RuntimeError(
            'Selected containers must use the verified persistent mounts, networks and localhost ports; '
            'not verified: '
            + ', '.join(r.get('name') or r.get('id', '?') for r in failed)
            + ' (sudo docker ps -a shows their state)'
        )
    states = health.applications(config)
    if any(
        not any(
            r['id'] == app and r['state'] == 'running' and r['health'] not in ('unhealthy', 'starting') for r in states
        )
        for app in apps
    ):
        raise RuntimeError('Selected application containers must be running and healthy before exposure')


def portal(config, host):
    apps = selected(config)
    ports = lan_ports(config)
    cards = ''.join(
        '<a href="https://' + host + ':' + str(ports[a]) + '/">' + html.escape(APPS[a][0]) + ' <span>↗</span></a>'
        for a in apps
    )
    logo = (ROOT / 'dashboard/logo.svg').read_text()
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NekoServer · Your apps</title><style>body{margin:0;background:#11101b;color:#f3efff;font:18px system-ui}main{max-width:850px;margin:8vh auto;padding:30px}svg{width:96px;height:96px}h1{font-size:42px;margin-bottom:8px}p{color:#b4aabd;line-height:1.6}nav{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px;margin:40px 0}a{padding:35px;border:1px solid #43354f;border-radius:20px;background:#211a30;color:#e5c6fb;text-decoration:none;font-weight:600}a:hover,a:focus-visible{outline:2px solid #d1a1f2;background:#30223f}a span{float:right;color:#e8aac9}small{color:#b4aabd}</style><main>'
        + logo
        + '<h1>Your world, under one roof.</h1><p>Welcome home. Pick an app and sign in with its account.</p><nav aria-label="Your applications">'
        + cards
        + '</nav><small>NekoServer · selected apps only · management panels stay local</small></main></html>'
    )


def lan_model(config, portal_root):
    ip, net = lan(config)
    servers = {}
    ports = lan_ports(config)
    for app in ['portal', *selected(config)]:
        port = ports[app]
        handler = (
            {'handler': 'file_server', 'root': str(portal_root), 'hide': ['.*']}
            if app == 'portal'
            else {'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:' + str(APPS[app][1])}]}
        )
        match = {'host': [ip], 'remote_ip': {'ranges': [net]}}
        if app == 'portal':
            match['path'] = ['/', '/index.html']
        servers[app] = {
            'listen': [ip + ':' + str(port)],
            'protocols': ['h1', 'h2'],
            'automatic_https': {'disable_redirects': True},
            'routes': [
                {'match': [match], 'handle': [handler], 'terminal': True},
                {'handle': [{'handler': 'static_response', 'status_code': 403}], 'terminal': True},
            ],
        }
    return {
        'admin': {'disabled': True},
        'apps': {
            'pki': {'certificate_authorities': {'local': {'install_trust': False}}},
            'tls': {
                'automation': {'policies': [{'subjects': [ip], 'issuers': [{'module': 'internal', 'ca': 'local'}]}]}
            },
            'http': {'servers': servers},
        },
    }


def unit(config, code, state):
    data = config['data_mount']
    media = config['media_mount']
    guard = str(code / 'nekoserver-runtime') + ' gateway-guard'
    return (
        '[Unit]\nDescription=NekoServer LAN HTTPS gateway\nWants=network-online.target\nAfter=network-online.target\nRequiresMountsFor='
        + data
        + ' '
        + media
        + '\n[Service]\nUser=caddy\nGroup=caddy\nEnvironment=XDG_DATA_HOME='
        + str(state)
        + '\nEnvironment=XDG_CONFIG_HOME='
        + str(state)
        + '\nExecStartPre=+'
        + guard
        + ' --config /etc/nekoserver-gateway/storage.json\nExecStart=/usr/bin/caddy run --config /etc/nekoserver-gateway/lan.json\nRestart=on-failure\nRestartSec=10s\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nPrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\nReadWritePaths='
        + str(state)
        + '\nInaccessiblePaths=-/run/docker.sock -/var/run/docker.sock '
        + data
        + '/appdata '
        + data
        + '/docker-engine '
        + media
        + '\nRestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK\n[Install]\nWantedBy=multi-user.target\n'
    )


def hashes(payloads):
    return {str(p): hashlib.sha256(v if isinstance(v, bytes) else v.encode()).hexdigest() for p, v in payloads.items()}


def own_by_name(path):
    """A file NekoServer's by its name: its own systemd unit, or anything inside a folder
    named nekoserver-* (its settings, its web page, its runtime). Nobody else puts files
    under NekoServer's name."""
    return any(part.startswith('nekoserver-') for part in Path(path).parts)


def preflight(payloads, ledger, copy=LAN_RECORD):
    """Every gateway file already on this PC is as setup would write it, or one setup wrote
    before (its record says so). A file that differs with no record is an earlier install's
    (Linux reinstalled, the SSD replaced by a release before 0.41.0) or changed by hand:
    one under NekoServer's own name is set aside and rewritten (the list returned, for
    the log); a file of someone else's is never overwritten."""
    previous = managed(ledger, copy).get('sha256', {})
    strangers, aside = [], []
    for path, content in payloads.items():
        p = safe_path(path)
        desired = content if isinstance(content, bytes) else content.encode()
        if (
            p.exists()
            and p.read_bytes() != desired
            and hashlib.sha256(p.read_bytes()).hexdigest() != previous.get(str(p))
        ):
            (aside if own_by_name(p) else strangers).append(str(p))
    if strangers:
        names = ' '.join(strangers)
        raise RuntimeError(
            f'Gateway files on this PC are not as setup would write them, and setup has no record of writing them: {names}. '
            f'If NekoServer ran on this PC before (Linux reinstalled, or the SSD replaced by a release before 0.41.0) and nobody '
            f'changed them by hand, remove them and run setup again:  sudo rm {names} . Otherwise look at them first; '
            f'setup never overwrites a file it did not write'
        )
    return aside


def set_aside(paths, stamp=None):
    """A copy of each file beside it (name.before-<time>) before setup rewrites it: what an
    earlier install, or a hand, put there is kept to look at. The log lines."""
    stamp = stamp or time.strftime('%Y%m%dT%H%M%S')
    notes = []
    for path in paths:
        p = safe_path(path)
        kept = p.with_name(p.name + '.before-' + stamp)
        shutil.copy2(p, kept)
        notes.append(
            f'Gateway: {p} was not as this release writes it and no record says setup wrote it (an earlier install\'s, or '
            f'changed by hand); a copy is kept at {kept} and the file is written anew'
        )
    return notes


def port_preflight(config):
    """Every home-address port is free or already this gateway's own (choose_lan_ports
    picked them a moment ago; a program started since is the only way this fails)."""
    ip, _ = lan(config)
    owned = owned_listeners(config)
    for port in lan_ports(config).values():
        if ip + ':' + str(port) in owned or port_free(ip, port):
            continue
        raise RuntimeError(
            f'Port {port} on {ip} was taken by another program just now; run setup again (it then picks a free port)'
        )


def adopt_state(state, user, ledger):
    """The gateway's TLS state on the SSD (its certificate authority, which phones trust)
    owned by another account than this PC's caddy. Handed to caddy only when NekoServer
    itself gave it that owner, every file of it (its ledger says so): Linux was reinstalled,
    or Caddy's package was, and caddy got another number (the old number may belong to
    some other account now; the files are still the ones NekoServer gave to caddy).
    Anything else is left as it is, for a person to look at. Returns a line for the log."""
    owner = state.stat()
    try:
        recorded = json.loads(ledger.read_text()) if ledger.is_file() else {}
    except (OSError, ValueError):
        recorded = {}

    def account(uid):
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None

    held = account(owner.st_uid)
    found = {(owner.st_uid, owner.st_gid)}
    for root, folders, files in os.walk(state):
        for name in folders + files:
            item = os.lstat(os.path.join(root, name))
            found.add((item.st_uid, item.st_gid))
    if found != {(recorded.get('uid'), recorded.get('gid'))}:
        who = f'the account "{held}"' if held else f'user number {owner.st_uid}'
        raise RuntimeError(
            f'The gateway\'s certificate files ({state}) belong to {who}, not to caddy, and NekoServer did not '
            f'leave them that way; check them, then give them to caddy (sudo chown -R caddy:caddy {state}) and run setup again'
        )
    for root, folders, files in os.walk(state):
        for name in folders + files:
            os.chown(os.path.join(root, name), user.pw_uid, user.pw_gid, follow_symlinks=False)
    os.chown(state, user.pw_uid, user.pw_gid, follow_symlinks=False)
    return (
        f'Gateway certificates kept: they belonged to the caddy account of an earlier install (number {owner.st_uid}), '
        f'now to this one ({user.pw_uid}); phones that trusted them keep doing so'
    )


def _apply_lan(config):
    recovery_tools.require_root()
    recovery_tools.mounts(config)
    verify_lan(config)
    readiness(config)
    data = Path(config['data_mount'])
    state = safe_path(data / 'gateway/caddy-state')
    code = safe_path(Path('/usr/local/lib/nekoserver-gateway') / ('v' + VERSION))
    web = Path('/usr/local/share/nekoserver-gateway/lan')
    ledger = data / 'gateway/lan-managed.json'
    caddy_settings = json.dumps(lan_model(config, web), indent=2) + '\n'
    payloads = {
        LAN_CONFIG: caddy_settings,
        SYSTEMD_UNITS / 'nekoserver-gateway-lan.service': unit(config, code, state),
        web / 'index.html': portal(config, lan(config)[0]),
    }
    payloads[code / native_runtime.NAME] = native_runtime.payload(ROOT, config)
    aside = preflight(payloads, ledger)
    port_preflight(config)
    if not shutil.which('caddy'):
        if not recovery_tools.host_supported():
            raise RuntimeError('Install official Caddy for this OS; automatic installation does not support it yet')
        family = host.current()
        if not family.available('caddy'):
            raise RuntimeError('Caddy is not offered by this system\'s package repositories')
        family.install(['caddy'])
    user = pwd.getpwnam('caddy')
    if user.pw_uid == 0 or user.pw_gid == 0:
        raise RuntimeError('Caddy needs an unprivileged package service account')
    adopted = None
    if state.exists() and (state.stat().st_uid != user.pw_uid or state.stat().st_gid != user.pw_gid):
        adopted = adopt_state(state, user, ledger)
    # Package Caddy runs without root credentials. Fresh public ancestors must
    # survive a private installer umask; existing restrictive paths are preserved.
    public = (data / 'gateway', code.parent, code, web.parent, web, Path('/etc/nekoserver-gateway'))
    for folder in public:
        safe_path(folder)
        existing = folder
        while not existing.exists():
            existing = existing.parent
        if not permission_checks.access(existing, user.pw_uid, {user.pw_gid}, 5 if existing == folder else 1):
            raise RuntimeError('Existing gateway directory blocks service traversal; review its permissions')
    with tempfile.TemporaryDirectory(prefix='neko-caddy-validate-') as folder:
        # A copy in a fresh temporary folder (its own name, nothing from the settings in the path).
        test = Path(folder, 'lan.json')
        test.write_text(caddy_settings)
        command(
            ['caddy', 'validate', '--config', str(test)],
            env={**os.environ, 'XDG_DATA_HOME': folder, 'XDG_CONFIG_HOME': folder},
        )
    # Configuration validity is checked before replacing managed files or restarting our service.
    recovery_tools.mounts(config)
    verify_lan(config)
    for folder in public:
        ensure_directory(folder, 0o755)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(state, user.pw_uid, user.pw_gid)
    notes = set_aside(aside)
    for p, content in payloads.items():
        atomic_write(p, content, 0o750 if p.name == native_runtime.NAME else 0o644)
    clean = {k: config[k] for k in ('data_mount', 'media_mount', 'storage')}
    clean['gateway'] = {k: config['gateway'][k] for k in ('lan_ip', 'lan_cidr')}
    atomic_json(Path('/etc/nekoserver-gateway/storage.json'), clean)
    record = {'sha256': hashes(payloads), 'uid': user.pw_uid, 'gid': user.pw_gid}
    atomic_json(ledger, record)
    atomic_json(LAN_RECORD, record)
    configure_cloud_origin(config, lan(config)[0], lan_ports(config).get('nextcloud', 8447))
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'enable', '--now', 'nekoserver-gateway-lan.service'])
    try:
        command(['systemctl', 'restart', 'nekoserver-gateway-lan.service'])
    except RuntimeError:
        command(['systemctl', 'stop', 'nekoserver-gateway-lan.service'])
        raise
    report = {
        'format': 'neko-gateway-v1',
        'mode': 'lan',
        'portal': HTTPS + lan(config)[0] + ':' + str(lan_ports(config)['portal']) + '/',
        'services': selected(config),
        'status': 'configured',
        'client_acceptance': 'needs_setup',
        'reason': 'Trust the private LAN CA on each client, then verify login, uploads, streaming and forbidden-source access.',
    }
    atomic_json(data / 'reports/gateway-lan.json', report)
    if adopted:
        notes.insert(0, adopted)
    if notes:
        report['notes'] = notes
    return report


def canonical(value):
    if not isinstance(value, dict):
        raise RuntimeError('Cannot inspect Tailscale Serve configuration')
    funnel = value.get('AllowFunnel') or {}
    if not isinstance(funnel, dict) or any(type(v) is not bool for v in funnel.values()):
        raise RuntimeError('Malformed Funnel state; no Tailscale route changed')
    if (
        any(k not in ('TCP', 'Web', 'AllowFunnel', 'Foreground', 'Services', 'ETag') for k in value)
        or value.get('Foreground')
        or value.get('Services')
        or any(funnel.values())
    ):
        raise RuntimeError('Unsupported/Funnel/foreground Tailscale routes preserved; manual review required')
    return {k: value.get(k) or {} for k in ('TCP', 'Web')}


DASHBOARD_PORT = 8448  # private dashboard over Tailscale; changes need its PIN (see serve.rs PUBLIC_PORT)

# Installed by the dashboard step; the phone is only sent to a dashboard that exists.
DASHBOARD_UNIT = SYSTEMD_UNITS / 'nekoserver-dashboard.service'


def dashboard_target(config):
    """The local dashboard for the Tailscale route, or None when turned off or not
    installed (a "remote access" setup without the dashboard step)."""
    opts = config.get('dashboard', {})
    if opts.get('remote', True) is not True or not DASHBOARD_UNIT.is_file():
        return None
    port = dashboard_port(config)
    return LOOPBACK + str(port) if port else None


def desired(host, apps, dashboard=None):
    ports = {str(APPS[a][2]): LOOPBACK + str(APPS[a][1]) for a in apps}
    if dashboard:
        ports[str(DASHBOARD_PORT)] = dashboard
    return {
        'TCP': {p: {'HTTPS': True} for p in ports},
        'Web': {host + ':' + p: {'Handlers': {'/': {'Proxy': target}}} for p, target in ports.items()},
    }


def serve_state():
    return canonical(json.loads(command(['tailscale', 'serve', 'status', '--json'])))


def tailnode():
    v = json.loads(command(['tailscale', 'status', '--json']))
    name = v.get('Self', {}).get('DNSName', '').rstrip('.')
    if v.get('BackendState') != 'Running' or not re.fullmatch(r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\.ts\.net', name):
        raise RuntimeError('Authenticate Tailscale and enable tailnet HTTPS/MagicDNS first')
    return name


def https_ready():
    """True when MagicDNS and HTTPS certificates are already on for this tailnet."""
    try:
        v = json.loads(command(['tailscale', 'status', '--json']))
        host = v.get('Self', {}).get('DNSName', '').rstrip('.')
        return bool(
            v.get('BackendState') == 'Running'
            and host
            and (v.get('CurrentTailnet') or {}).get('MagicDNSEnabled') is True
            and host in (v.get('CertDomains') or [])
        )
    except (RuntimeError, ValueError, AttributeError, TypeError):
        return False


def tailnet_routes(config, host):
    """The private Tailscale routes this server should have: the selected apps plus the
    dashboard (changes on it need its PIN). Installation and the acceptance check both use this one."""
    return desired(host, selected(config), dashboard_target(config))


def tailnet_ports():
    """Every Tailscale port NekoServer may use: one per app and the dashboard's."""
    return {str(v[2]) for v in APPS.values()} | {str(DASHBOARD_PORT)}


def valid_owned(owned):
    """The routes in the ownership journal have the shape NekoServer writes: HTTPS on its
    own ports, each to a service on this PC. The host name and the local port are not
    compared: the server may have been renamed, the dashboard moved to another port."""
    if (
        not isinstance(owned, dict)
        or set(owned) != {'TCP', 'Web'}
        or not all(isinstance(owned[k], dict) for k in owned)
    ):
        return False
    if not set(owned['TCP']) <= tailnet_ports() or any(v != {'HTTPS': True} for v in owned['TCP'].values()):
        return False
    for key, value in owned['Web'].items():
        proxy = value.get('Handlers', {}).get('/', {}).get('Proxy') if isinstance(value, dict) else None
        if key.rpartition(':')[2] not in owned['TCP'] or not isinstance(proxy, str) or not proxy.startswith(LOOPBACK):
            return False
    return True


def handlers(web, port):
    """The handlers of every host name on this Serve port."""
    return [value for key, value in web.items() if key.rpartition(':')[2] == port]


def https(port):
    """tailscale serve's --https= argument for a port: a number from 1 to 65535 (it comes
    from Tailscale's own state and the journal, so it is checked before it is used)."""
    number = int(port)
    if not 0 < number < 65536:
        raise ValueError('Not a Tailscale port: ' + str(port))
    return '--https=' + str(number)


def ours(state, owned):
    """The Serve ports still exactly as NekoServer set them (its journal), whatever host
    name they were set under. Any other port, or one changed since, is someone else's."""
    mine = set()
    for port, value in state['TCP'].items():
        recorded = handlers(owned.get('Web', {}), port)
        found = handlers(state['Web'], port)
        if owned.get('TCP', {}).get(port) == value and recorded and found and all(h == recorded[0] for h in found):
            mine.add(port)
    return mine


def only(state, ports):
    """The part of a Serve state on these ports."""
    return {
        'TCP': {p: v for p, v in state['TCP'].items() if p in ports},
        'Web': {k: v for k, v in state['Web'].items() if k.rpartition(':')[2] in ports},
    }


def _apply_tailnet(config):
    recovery_tools.require_root()
    recovery_tools.mounts(config)
    readiness(config)
    host = tailnode()
    apps = selected(config)
    board = dashboard_target(config)
    target = tailnet_routes(config, host)
    state = serve_state()
    ledger = safe_path(Path(config['data_mount']) / 'gateway/tailnet-owned.json')
    saved = health.read_json(ledger)
    owned = saved.get('owned', {'TCP': {}, 'Web': {}})
    if ledger.exists() and (
        saved.get('format') != 'neko-tailnet-owned-v1'
        or saved.get('status') not in ('pending', 'configured')
        or not valid_owned(owned)
    ):
        raise RuntimeError('Malformed gateway ownership journal preserved; review it locally')
    # NekoServer's routes are recognised by port and handler, so a renamed server, a
    # dashboard on another port or a route of your own on another port changes nothing.
    mine = ours(state, owned)
    theirs = set(state['TCP']) - mine
    needed = sorted(theirs & set(target['TCP']))
    if needed:
        raise RuntimeError(
            'Tailscale port ' + ', '.join(needed) + ' already has a route of your own, and NekoServer needs it. '
            'Move your route to another port (sudo tailscale serve --https=' + needed[0] + ' off), then run setup again'
        )
    configure_cloud_origin(config, host, 8447)
    # Journal ownership before requests; interrupted/partly completed configuration remains identifiable.
    union = {k: {**only(owned, mine)[k], **target[k]} for k in ('TCP', 'Web')}
    atomic_json(ledger, {'format': 'neko-tailnet-owned-v1', 'host': host, 'owned': union, 'status': 'pending'})
    try:
        for port in sorted(mine):
            # Ours but no longer wanted, or set under the server's old name: off first.
            if port not in target['TCP'] or only(state, {port}) != only(target, {port}):
                command(['tailscale', 'serve', '--bg', https(port), 'off'])
        for port in target['TCP']:
            if only(state, {port}) == only(target, {port}):
                continue
            command(
                ['tailscale', 'serve', '--bg', https(port), target['Web'][host + ':' + port]['Handlers']['/']['Proxy']],
                45,
            )
        if only(serve_state(), set(target['TCP'])) != target:
            raise RuntimeError('Tailnet routes did not match the planned private configuration')
    except (RuntimeError, ValueError, TypeError) as exc:
        # Fail closed for our routes (only those) when they are still recognisable.
        for port in sorted(ours(serve_state(), union)):
            command(['tailscale', 'serve', '--bg', https(port), 'off'])
        raise RuntimeError(
            f'Tailscale addresses not set up ({exc}). The addresses were switched off again until the next run'
        ) from None
    atomic_json(ledger, {'format': 'neko-tailnet-owned-v1', 'host': host, 'owned': target, 'status': 'configured'})
    result = {
        'format': 'neko-gateway-v1',
        'mode': 'tailnet',
        'status': 'configured',
        'client_acceptance': 'needs_setup',
        'services': apps,
        'links': {
            **{a: HTTPS + host + ':' + str(APPS[a][2]) + '/' for a in apps},
            **({'dashboard': HTTPS + host + ':' + str(DASHBOARD_PORT) + '/'} if board else {}),
        },
        'reason': 'Restrict tailnet policy to intended users/devices and verify each application login and streaming/upload from a client.',
    }
    atomic_json(Path(config['data_mount']) / 'reports/gateway-tailnet.json', result)
    return result


def refresh_dashboard_route(config, log):
    """The dashboard moved to another port (or was switched off for remote use) after the
    Tailscale addresses were set up: only its route is brought up to date, when it is
    still NekoServer's. True when it changed."""
    ledger = safe_path(Path(config['data_mount']) / 'gateway/tailnet-owned.json')
    saved = health.read_json(ledger)
    owned = saved.get('owned')
    if saved.get('format') != 'neko-tailnet-owned-v1' or saved.get('status') != 'configured' or not valid_owned(owned):
        return False
    port = str(DASHBOARD_PORT)
    host = tailnode()
    state = serve_state()
    board = dashboard_target(config)
    if port not in owned['TCP'] or port not in ours(state, owned):
        return False
    if handlers(state['Web'], port) == [{'Handlers': {'/': {'Proxy': board}}}]:
        return False
    command(['tailscale', 'serve', '--bg', https(port), 'off'])
    if board:
        command(['tailscale', 'serve', '--bg', https(port), board], 45)
    kept = only(owned, set(owned['TCP']) - {port})
    mine = desired(host, [], board) if board else {'TCP': {}, 'Web': {}}
    atomic_json(ledger, {**saved, 'host': host, 'owned': {k: {**kept[k], **mine[k]} for k in ('TCP', 'Web')}})
    log('Dashboard: its Tailscale address now leads to ' + (board or 'nothing (remote use switched off)'))
    return True


def apply_lan(config):
    recovery_tools.require_root()
    recovery_tools.mounts(config)
    with recovery_tools.lock(config):
        return _apply_lan(config)


def apply_tailnet(config):
    recovery_tools.require_root()
    recovery_tools.mounts(config)
    with recovery_tools.lock(config):
        return _apply_tailnet(config)


def configure_cloud_origin(config, host, port):
    """Make Nextcloud answer on this gateway host without breaking any other gateway.

    Each gateway (LAN, tailnet) only adds its host to trusted_domains. Caddy passes
    the client's Host header through, and says it used https (X-Forwarded-Proto), so
    Nextcloud builds links for whichever address and protocol the client used. A single overwritehost would force every client onto
    one address, so it is never set here, and a value an earlier release set for a
    gateway is removed. The proxy hop is trusted so client addresses are preserved.
    """
    if 'nextcloud' not in selected(config):
        return
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise RuntimeError('Invalid Nextcloud gateway host')
    from neko_common import compose_image_override

    folder = Path(config['data_mount']) / 'compose/nextcloud'
    compose = [
        'docker',
        '--host',
        'unix:///var/run/docker.sock',
        'compose',
        '-p',
        'neko-nextcloud',
        '-f',
        str(folder / 'compose.yaml'),
        '--env-file',
        str(folder / '.env'),
        *compose_image_override(folder / 'compose.yaml'),
    ]
    base = compose + ['exec', '-T', '--user', 'www-data', 'nextcloud', 'php', 'occ']

    def get(key):
        try:
            return json.loads(command(base + [OCC_GET, key, '--output=json']))
        except (RuntimeError, ValueError):
            return None

    values = get('trusted_domains')
    if not isinstance(values, list):
        raise RuntimeError('Cannot inspect Nextcloud trusted hosts')
    if host not in values:
        command(base + [OCC_SET, 'trusted_domains', str(len(values)), '--value=' + host])
    # Both gateways tell Nextcloud the client used https (X-Forwarded-Proto, from a
    # trusted proxy), so it builds https links for them and plain ones on this PC itself
    # (http://127.0.0.1:8088). A forced protocol, set by releases before 0.39.4, sent
    # this PC's own browser to https on that port, which fails: it is removed.
    if get('overwriteprotocol') == 'https':
        command(base + [OCC_DELETE, 'overwriteprotocol'])
    previous = get('overwritehost')
    if isinstance(previous, str) and previous.endswith(':' + str(port)):
        command(base + [OCC_DELETE, 'overwritehost'])
    # The address Nextcloud's background jobs put in links: the first gateway's, kept
    # while it is still one of its gateway addresses (whatever that gateway's port).
    cli = get('overwrite.cli.url')
    known = urlsplit(cli).hostname if isinstance(cli, str) and cli.startswith(HTTPS) else None
    if known not in [*values, host] or known in ('localhost', '127.0.0.1'):
        command(base + [OCC_SET, 'overwrite.cli.url', '--value=https://' + host + ':' + str(port)])
    for index, proxy in enumerate(cloud_proxies(compose)):
        command(base + [OCC_SET, 'trusted_proxies', str(index), '--value=' + proxy])


# Docker's own default range for the networks it makes (172.17.0.0/16 to 172.31.0.0/16).
# Trusted as a whole, so a Nextcloud network made again later (with another number)
# still trusts the hop. 192.168.0.0/16, Docker's other default range, is not trusted as a
# whole: it is the usual home network range too.
DOCKER_RANGE = '172.16.0.0/12'


def cloud_proxies(compose):
    """The addresses that relay the gateway's connections to Nextcloud (Docker's network
    gateway): Docker's default range, plus a gateway outside it (192.168.x.1 once Docker has
    used up its 172 networks) by itself. With only the one gateway trusted, Nextcloud's
    network made again with another number (taken down and up) no longer trusted the hop
    and made http links behind the https gateways, which broke signing in."""
    ids = command(compose + ['ps', '-q', 'nextcloud']).split()
    if len(ids) != 1:
        raise RuntimeError('Nextcloud container not found')
    try:
        info = json.loads(command(['docker', '--host', 'unix:///var/run/docker.sock', 'inspect', ids[0]]))[0]
    except (ValueError, IndexError, KeyError):
        raise RuntimeError('Cannot inspect Nextcloud networks') from None
    out = []
    for network in (info.get('NetworkSettings', {}).get('Networks') or {}).values():
        try:
            gateway = ipaddress.ip_address(network.get('Gateway', ''))
        except ValueError:
            continue
        if gateway.is_private and str(gateway) not in out:
            out.append(str(gateway))
    if not out:
        raise RuntimeError('Nextcloud has no private Docker network gateway')
    docker = ipaddress.ip_network(DOCKER_RANGE)
    return [DOCKER_RANGE, *(g for g in out if ipaddress.ip_address(g) not in docker)]
