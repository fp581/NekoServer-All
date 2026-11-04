"""Drives on a network storage box (a NAS) over iSCSI, for the media HDD and the backup
drive. Off unless switched on ("network_storage": {"wanted": true}).

iSCSI lets the NAS hand this PC a virtual disk (a LUN) over the network: it shows up here
as a disk of its own, which this PC alone formats and mounts. So:
- Each LUN belongs to this PC only. Another computer mounting the same LUN at the same
  time corrupts it; other computers reach the files through this PC's apps and shared
  folders. The NAS's own access rule (this PC's initiator name, shown below) is the lock.
- NekoServer never formats a LUN: an empty one gets the exact command to run.
- The application SSD stays on this PC (by USB or inside it): it is what makes the apps
  fast, and the place setup keeps its own records.
- The NAS watches its disks (RAID, SMART); this PC sees the connection, the filesystem and
  its free space. Keep the NAS's own alerts on.

What this step does: installs the iSCSI tools, shows this PC's initiator name, asks for
the NAS's address, finds the disks it offers, asks which ones NekoServer uses (with a CHAP
login if the NAS asks for one), logs in to them for good (again at every start), and lists
them with their identity, size and filesystem for the drive questions that follow."""

import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from neko_common import atomic_write

DEPENDENCIES = ['system']
DESCRIPTION = 'Connect the drives on a network storage box (NAS) over iSCSI, when switched on'

SECTION = 'network_storage'
PORT = 3260  # iSCSI's port
INITIATOR = Path('/etc/iscsi/initiatorname.iscsi')
# Where open-iscsi keeps each target's settings: Debian, Ubuntu and Arch under /etc,
# Fedora, RHEL and openSUSE under /var/lib.
NODE_ROOTS = (Path('/etc/iscsi/nodes'), Path('/var/lib/iscsi/nodes'))
BY_PATH = Path('/dev/disk/by-path')
# iSCSI names (RFC 3720): iqn.<year-month>.<reversed domain>[:name], or eui./naa. forms.
IQN = re.compile(
    r'(iqn\.\d{4}-\d{2}\.[a-z0-9][a-z0-9.-]*(:[A-Za-z0-9.:_-]+)?|eui\.[0-9A-Fa-f]{16}|naa\.[0-9A-Fa-f]{16,32})'
)
HOST = re.compile(r'([0-9]{1,3}(\.[0-9]{1,3}){3}|[A-Za-z0-9]([A-Za-z0-9-]{0,62}\.)*[A-Za-z0-9-]{1,63})')
# How long a disk takes to show up here after the login.
APPEAR_WAIT = 30
# "already logged in" (ISCSI_ERR_SESS_EXISTS) is no failure.
SESSION_EXISTS = 15
# How long this PC waits for a NAS that stopped answering before its writes fail (open-
# iscsi's replacement timeout, 120 s by default). Tried in the lab: a break of up to two
# minutes only paused the writes, but a longer one failed them and ext4 aborted its
# journal, so the drive stayed read-only until it was checked. A NAS restarting after a
# firmware update takes longer than two minutes; for ten minutes the apps wait instead.
REPLACEMENT_TIMEOUT = 600
SESSIONS = Path('/sys/class/iscsi_session')
# The disk driver gives up on one command after its timeout times six tries (30 s each:
# 180 s, also seen in the lab, after which ext4 aborted too). 120 s each covers the ten
# minutes above. Only for disks on an iSCSI session (a NAS), at every start (udev).
COMMAND_TIMEOUT = 120
UDEV_RULE = Path('/etc/udev/rules.d/90-nekoserver-nas-disks.rules')
RULE = (
    '# NekoServer: disks on a network storage box (iSCSI) wait for it to come back instead\n'
    '# of failing writes after 3 minutes (network_storage.py).\n'
    'ACTION=="add|change", SUBSYSTEM=="scsi", KERNELS=="session[0-9]*", ATTR{type}=="0", '
    f'ATTR{{timeout}}="{COMMAND_TIMEOUT}"\n'
)


def wanted(config):
    return config.get(SECTION, {}).get('wanted') is True


def options(config):
    """The saved choices, checked before setup changes anything: a switch, the NAS's
    address (host:port) and the targets' iSCSI names."""
    values = config.get(SECTION, {})
    if not isinstance(values, dict):
        raise RuntimeError('network_storage must be an object')
    if type(values.get('wanted', False)) is not bool:
        raise RuntimeError('network_storage.wanted must be true or false')
    portal = values.get('portal', '')
    if portal and (not isinstance(portal, str) or portal_of(portal) is None):
        raise RuntimeError("network_storage.portal must be the NAS's address, such as 192.168.1.20:3260")
    targets = values.get('targets', [])
    if not isinstance(targets, list) or not all(isinstance(t, str) and IQN.fullmatch(t) for t in targets):
        raise RuntimeError('network_storage.targets must be iSCSI names (iqn.2013-03.com.example:target)')
    return values


def portal_of(text):
    """'192.168.1.20' or 'nas.home:3260' as host:port, or None when it is neither."""
    host, _, port = str(text).strip().partition(':')
    port = port or str(PORT)
    if not HOST.fullmatch(host) or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    if re.fullmatch(r'[0-9.]+', host) and not all(0 <= int(p) <= 255 for p in host.split('.')):
        return None
    return f'{host}:{int(port)}'


def answers(portal, timeout=5):
    """The NAS answers on its iSCSI port (a connection opened and closed again)."""
    host, _, port = portal.rpartition(':')
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def run_tool(argv, timeout=60, run=subprocess.run):
    """iscsiadm (or another tool) run directly, never through a shell; its result."""
    return run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def initiator_name(path=INITIATOR, run=subprocess.run):
    """This PC's iSCSI name (the one to allow on the NAS). Made once when the package left
    none (Arch makes it only at the first start of iscsid): `iscsi-iname` picks one."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith('InitiatorName='):
                name = line.split('=', 1)[1].strip()
                if IQN.fullmatch(name):
                    return name
    except OSError:
        pass
    made = run_tool(['iscsi-iname'], 30, run).stdout.strip()
    if not IQN.fullmatch(made):
        raise RuntimeError('Could not make an iSCSI name for this PC (iscsi-iname gave none)')
    atomic_write(Path(path), f'InitiatorName={made}\n', 0o644)
    return made


def discovered(portal, run=subprocess.run):
    """The targets the NAS offers at `portal`: [(target IQN, portal as the NAS gave it)]."""
    done = run_tool(['iscsiadm', '-m', 'discovery', '-t', 'sendtargets', '-p', portal], 60, run)
    if done.returncode:
        said = (done.stderr or done.stdout or '').strip().splitlines()
        raise RuntimeError(
            f'The NAS at {portal} answered, but listed no iSCSI disks'
            + (f' ({said[-1][:200]})' if said else '')
            + '. On the NAS, make an iSCSI target with a LUN on each pool, allow this PC on it, then run setup again.'
        )
    found = []
    for line in done.stdout.splitlines():
        # "192.168.1.20:3260,1 iqn.2013-03.com.wdc:ex4100:media"
        parts = line.split()
        if len(parts) == 2 and IQN.fullmatch(parts[1]):
            found.append((parts[1], parts[0].split(',')[0]))
    return found


def node_record(iqn, portal, roots=NODE_ROOTS):
    """The file open-iscsi keeps a target's settings in (one per portal), or None.
    Newer releases: <root>/<iqn>/<host>,<port>,<tpgt>/default; older ones: that last part
    is the file itself."""
    host, _, port = portal.rpartition(':')
    for root in roots:
        folder = Path(root) / iqn
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            if entry.name.startswith(f'{host},{port},') and not entry.is_symlink():
                record = entry / 'default' if entry.is_dir() else entry
                if record.is_file() and not record.is_symlink():
                    return record
    return None


def with_chap(text, user, password):
    """A target's settings with a CHAP login for its sessions (the settings open-iscsi
    reads at each login). Written in its file, never on a command line, so other
    programs on this PC never see the password."""
    keys = {
        'node.session.auth.authmethod': 'CHAP',
        'node.session.auth.username': user,
        'node.session.auth.password': password,
    }
    lines = [line for line in text.splitlines() if line.split('=', 1)[0].strip() not in keys]
    return '\n'.join([*lines, *(f'{k} = {v}' for k, v in keys.items())]) + '\n'


def chap_ok(user, password):
    """CHAP values the settings file can hold: one line each, no spaces at either end, no
    = or #; the password 12 characters or more (most NAS boxes ask for 12 to 16)."""
    plain = re.compile(r'[^\s=#]([^\n\r=#]*[^\s=#])?')
    return bool(plain.fullmatch(user or '') and plain.fullmatch(password or '')) and len(password) >= 12


def set_chap(iqn, portal, user, password, roots=NODE_ROOTS):
    record = node_record(iqn, portal, roots)
    if record is None:
        raise RuntimeError(f'The settings of {iqn} were not found after it was discovered; run setup again')
    atomic_write(record, with_chap(record.read_text(), user, password), 0o600)


def known_at(iqn, portal, run=subprocess.run):
    """The target's settings at the address the person gave. A NAS may list its targets
    at another address of its own (a second network port, an address for inside it):
    discovery saves them there, so they are saved again at the given one, which this PC
    reaches. Only that one logs in at every start."""
    node = ['iscsiadm', '-m', 'node', '-T', iqn, '-p', portal]
    if run_tool(node, 30, run).returncode:
        made = run_tool([*node, '-o', 'new'], 30, run)
        if made.returncode:
            said = (made.stderr or made.stdout or '').strip().splitlines()
            raise RuntimeError(f'Could not save {iqn} at {portal}' + (f' ({said[-1][:200]})' if said else ''))


def log_in(iqn, portal, run=subprocess.run):
    """Log in to a target, and again at every start of this PC (node.startup automatic)."""
    node = ['iscsiadm', '-m', 'node', '-T', iqn, '-p', portal]
    for name, value in (
        ('node.startup', 'automatic'),
        ('node.conn[0].startup', 'automatic'),
        ('node.session.timeo.replacement_timeout', str(REPLACEMENT_TIMEOUT)),
    ):
        run_tool([*node, '-o', 'update', '-n', name, '-v', value], 30, run)
    done = run_tool([*node, '--login'], 90, run)
    if done.returncode not in (0, SESSION_EXISTS):
        said = (done.stderr or done.stdout or '').strip().splitlines()
        reason = said[-1][:200] if said else f'exit {done.returncode}'
        hint = (
            'the NAS refused the login: check that this PC is allowed on that target (its initiator name, above) '
            'and the CHAP user name and password'
            if 'auth' in reason.lower()
            else 'check the NAS'
        )
        raise RuntimeError(f'Could not log in to {iqn} ({reason}); {hint}, then run setup again')
    patient(iqn)


def patient(iqn, sessions=SESSIONS, block=Path('/sys/block'), by_path=BY_PATH):
    """The waits for a session already logged in: the iSCSI one (the saved setting is read
    only at a login) and its disks' command timeout (the udev rule applies at the next
    start, or when a disk is added)."""
    for session in sorted(Path(sessions).glob('session*')):
        try:
            if (session / 'targetname').read_text().strip() == iqn:
                (session / 'recovery_tmo').write_text(str(REPLACEMENT_TIMEOUT))
        except OSError:
            pass
    for disk in disks_of(iqn, by_path):
        try:
            (Path(block) / Path(disk['device']).name / 'device' / 'timeout').write_text(str(COMMAND_TIMEOUT))
        except OSError:
            pass


def install_rule(path=UDEV_RULE):
    if not path.exists() or path.read_text() != RULE:
        atomic_write(path, RULE, 0o644)
        run_tool(['udevadm', 'control', '--reload'], 30)


def disks_of(iqn, by_path=BY_PATH):
    """The disks a logged-in target gave this PC: [{'lun', 'device', 'path'}], by their
    stable names (/dev/disk/by-path/ip-<portal>-iscsi-<iqn>-lun-<n>), never by sdX."""
    found = []
    folder = Path(by_path)
    if not folder.is_dir():
        return found
    pattern = re.compile(r'ip-(.+)-iscsi-(.+)-lun-(\d+)')
    for link in sorted(folder.iterdir()):
        match = pattern.fullmatch(link.name)
        if match and match.group(2) == iqn:
            found.append({'lun': int(match.group(3)), 'device': str(link.resolve()), 'path': str(link)})
    return found


def where(disk, by_path=BY_PATH):
    """{'portal', 'iqn', 'lun'} of the iSCSI disk named `disk` (sdc), or None when it is
    not one: what the settings keep beside its filesystem's UUID."""
    folder = Path(by_path)
    if not folder.is_dir():
        return None
    pattern = re.compile(r'ip-(.+)-iscsi-(.+)-lun-(\d+)')
    for link in sorted(folder.iterdir()):
        match = pattern.fullmatch(link.name)
        if match and link.resolve().name == disk:
            return {'portal': match.group(1), 'iqn': match.group(2), 'lun': int(match.group(3))}
    return None


def wait_for_disks(iqn, by_path=BY_PATH, wait=APPEAR_WAIT, sleep=time.sleep):
    for _ in range(max(1, wait)):
        found = disks_of(iqn, by_path)
        if found:
            return found
        sleep(1)
    return disks_of(iqn, by_path)


def lsblk_row(device, run=subprocess.run):
    """What lsblk says of one disk and what it holds (size, filesystem, UUID, WWN)."""
    shown = run_tool(['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,FSTYPE,UUID,WWN,SERIAL,MOUNTPOINT', device], 30, run)
    try:
        rows = json.loads(shown.stdout).get('blockdevices', [])
    except ValueError:
        rows = []
    return rows[0] if rows else {}


def described(disk, row):
    """One LUN as setup lists it: where it comes from, its size, what is on it."""
    filesystems = [r for r in [row, *row.get('children', [])] if r.get('fstype')]
    held = (
        ', '.join(f"{r.get('fstype')} (UUID {r.get('uuid')})" for r in filesystems)
        if filesystems
        else 'empty: no filesystem yet'
    )
    identity = row.get('wwn') or row.get('serial') or 'no WWN reported'
    return f"LUN {disk['lun']}: /dev/{row.get('name', '?')}, {row.get('size', '?')}, {held}; identity {identity}"


def format_command(disk, iqn=''):
    """The command that makes an ext4 filesystem on an empty LUN, by its stable name,
    labelled after its target (iqn...:media gives "neko-media"; at most 16 characters)."""
    tail = re.sub(r'[^A-Za-z0-9]', '', iqn.rsplit(':', 1)[-1]) if ':' in iqn else ''
    label = f'neko-{tail[:6]}{disk["lun"]}' if tail else f"neko-nas-lun{disk['lun']}"
    return f"sudo mkfs.ext4 -L {label} {disk['path']}"


def install_tools(log):
    """open-iscsi (iscsi-initiator-utils on Fedora and RHEL), and its services: the
    daemon, and the one that logs in to the saved targets at every start."""
    import host

    family = host.current()
    family.install(['open-iscsi'])
    units = [family.service('iscsid'), family.service('iscsi-login')]
    for unit in units:
        done = run_tool(['systemctl', 'enable', '--now', unit], 120)
        if done.returncode:
            raise RuntimeError(f'Could not start {unit}: {(done.stderr or "").strip()[:200]}')
    log('iSCSI tools ready (' + ', '.join(units) + ')')
    return family


def wait_online_unit(run=subprocess.run):
    """The service that holds the boot until the network is up, for the network manager
    this PC runs (NetworkManager on desktops, systemd-networkd on servers), or None."""
    for manager, waiter in (
        ('NetworkManager.service', 'NetworkManager-wait-online.service'),
        ('systemd-networkd.service', 'systemd-networkd-wait-online.service'),
    ):
        if run_tool(['systemctl', 'is-enabled', '--quiet', manager], 30, run).returncode == 0:
            return waiter
    return None


def ask(prompt, input_fn):
    try:
        return input_fn(prompt).strip()
    except EOFError:
        return ''


def chosen_targets(found, saved, input_fn, out_fn):
    """The targets NekoServer uses: those saved before (still offered), or those picked now."""
    names = [iqn for iqn, _ in found]
    kept = [iqn for iqn in saved if iqn in names]
    if kept:
        return kept
    out_fn('The NAS offers these iSCSI targets:')
    for n, iqn in enumerate(names, 1):
        out_fn(f'  {n}) {iqn}')
    for _ in range(3):
        answer = ask('Numbers of the ones NekoServer uses, separated by commas (Enter alone stops here): ', input_fn)
        if not answer:
            break
        picks = [p.strip() for p in answer.split(',') if p.strip()]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(names) for p in picks):
            return list(dict.fromkeys(names[int(p) - 1] for p in picks))
        out_fn(f'Type numbers from 1 to {len(names)}, such as 1,2.')
    raise RuntimeError('No iSCSI target was chosen; run setup again to choose them. Nothing was changed.')


def asked_portal(saved, input_fn, out_fn):
    if saved and portal_of(saved):
        return portal_of(saved)
    for _ in range(3):
        portal = portal_of(ask("The NAS's address on your network (such as 192.168.1.20): ", input_fn))
        if portal:
            return portal
        out_fn('Type its address as numbers (192.168.1.20) or its name on your network (nas.home).')
    raise RuntimeError("No NAS address was given; run setup again to give it. Nothing was changed.")


def asked_chap(iqn, portal, input_fn, out_fn, secret_fn):
    answer = ask(f'Does the NAS ask for a CHAP user name and password for {iqn}? [y/N]: ', input_fn).lower()
    if answer not in ('y', 'yes'):
        return
    for _ in range(3):
        user = ask('CHAP user name: ', input_fn)
        try:
            password = secret_fn('CHAP password (not shown; at least 12 characters): ')
        except EOFError:
            password = ''
        if chap_ok(user, password):
            set_chap(iqn, portal, user, password)
            out_fn(f'CHAP login saved for {iqn} (in its iSCSI settings, readable by root only)')
            return
        out_fn('The user name and password must be one line each, without = or #; the password 12 characters or more.')
    raise RuntimeError('No usable CHAP login was given; run setup again. Nothing was changed.')


def run(context):
    config = context.config
    if not wanted(config):
        context.log('Drives on a network storage box: not used (all drives are connected to this PC)')
        return
    options = config.setdefault(SECTION, {})
    if not context.apply:
        context.log('Drives on a NAS over iSCSI: connected with --apply')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Connecting network drives requires root')
    import getpass
    import sys

    interactive = sys.stdin.isatty()
    install_tools(context.log)
    name = initiator_name()
    context.log(
        f"This PC's iSCSI name (initiator): {name}\n"
        "  On the NAS, allow only this name on each iSCSI target NekoServer uses (in WD's My Cloud page: "
        'Storage > iSCSI, the target, Access). Each disk then belongs to this PC alone: another computer '
        'mounting it at the same time would damage it.'
    )
    if not options.get('portal') and not interactive:
        raise RuntimeError("The NAS's address is not saved yet; run  sudo bash install.sh  in a terminal")
    portal = asked_portal(options.get('portal'), input, context.log)
    if not answers(portal):
        raise RuntimeError(
            f'Nothing answers on {portal}: check that the NAS is on, its address, that iSCSI is switched on in '
            'its settings, and that this PC is on the same network. Then run setup again.'
        )
    found = discovered(portal)
    if not found:
        raise RuntimeError(f'The NAS at {portal} offers no iSCSI targets yet. Make one with a LUN on each pool first.')
    targets = chosen_targets(found, options.get('targets', []), input, context.log)
    install_rule()
    first_time = set(targets) - set(options.get('targets', []))
    for iqn in targets:
        known_at(iqn, portal)
        if iqn in first_time and interactive:
            asked_chap(iqn, portal, input, context.log, getpass.getpass)
        log_in(iqn, portal)
    options.update(portal=portal, targets=targets)
    if hasattr(context, 'persist_config'):
        context.persist_config()
    empty = []
    for iqn in targets:
        disks = wait_for_disks(iqn)
        if not disks:
            raise RuntimeError(
                f'Logged in to {iqn}, but it gave this PC no disk. On the NAS, give that target a LUN '
                '(a virtual disk on one of its pools), then run setup again.'
            )
        context.log(f'{iqn} at {portal}:')
        for disk in disks:
            row = lsblk_row(disk['device'])
            context.log('  ' + described(disk, row))
            if not row.get('fstype') and not row.get('children'):
                empty.append((disk, iqn))
    waiter = wait_online_unit()
    if waiter:
        run_tool(['systemctl', 'enable', waiter], 60)
        context.log(f'At every start, the drives on the NAS are mounted once the network is up ({waiter})')
    else:
        context.log(
            'Neither NetworkManager nor systemd-networkd runs here, so the start-up cannot wait for the '
            'network: the NAS drives may show as missing after a restart until the network is up'
        )
    context.log(
        'Both NAS drives are in one box: a fault of the NAS (its power supply, a setting) can take the media '
        'and the backup at once. Keep a second copy of what matters elsewhere when you can.'
    )
    if empty:
        commands = '\n'.join('  ' + format_command(d, iqn) for d, iqn in empty)
        return {
            'status': 'needs_setup',
            'reason': (
                'These NAS disks have no filesystem yet, and NekoServer never formats a drive by itself. '
                'Check each is the one you mean (its size and LUN above), then make its filesystem (this '
                f'erases that LUN):\n{commands}\nThen run  sudo bash install.sh  again.'
            ),
        }
