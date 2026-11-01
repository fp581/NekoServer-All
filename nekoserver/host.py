"""The Linux family this server runs, and how to install packages and name services on it.

Shared code never calls a package manager itself: it asks this module. Each family
(Arch/Nyarch and Debian/Ubuntu) knows its package manager, the names its repositories
use for the packages NekoServer needs, what its services are called and which of its
settings files belong in host backups. See PORTING.md for how a family is added.

Run as  python3 host.py bootstrap  (install.sh does) to install the system packages
the apps need, and  python3 host.py repair  to undo repository settings an older
NekoServer left that stop the package manager working (install.sh runs it first).
"""

import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import MappingProxyType

import setup_progress
from neko_common import atomic_write

OS_RELEASE = '/etc/os-release'
# Every system package the apps need outside their containers, by NekoServer's names.
APP_PACKAGES = (
    'docker',
    'docker-compose',
    'tailscale',
    'caddy',
    'samba',
    'hdparm',
    'smartmontools',
    'qrencode',
    'xdg-utils',
)
# Tailscale's service has the same name on every family.
TAILSCALE_UNIT = 'tailscaled.service'
SAMBA_UNIT = 'smb.service'


class Unsupported(RuntimeError):
    """This Linux family has no NekoServer adapter yet."""


def os_release(path=OS_RELEASE):
    """The KEY=value pairs of /etc/os-release, unquoted ({} if unreadable)."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _words(release, key):
    return release.get(key, '').lower().split()


# A package being set up together with this one: it meets any version asked of it.
ANY_VERSION = object()


def _alternative(text):
    """`name:arch (>= 1.2)` -> ('name', '>=', '1.2'); no version: ('name', None, None)."""
    name, _, version = text.partition('(')
    name = name.strip().split(':')[0]
    if not name:
        return None, None, None
    match = re.fullmatch(r'\s*(<<|<=|=|>=|>>)\s*(\S+?)\s*\)\s*', version)
    return (name, match[1], match[2]) if match else (name, None, None)


def _still_needed(group, met, pending, satisfies):
    """The unfinished package one dependency (`A (>= 1) | B`) still needs: none when one
    of its choices is already met, otherwise the first unfinished choice, as apt picks.
    `met`: {name: installed version, None for a name only provided without a version, or
    ANY_VERSION}; a versioned choice is met only by a version that satisfies it, and
    never by an unversioned "Provides" (Debian policy). `satisfies(version, op, wanted)`
    compares versions as dpkg does."""
    options = [option for option in (_alternative(a) for a in group.split('|')) if option[0]]
    for name, op, wanted in options:
        if name not in met:
            continue
        have = met[name]
        if op is None or have is ANY_VERSION or (have is not None and satisfies(have, op, wanted)):
            return None
    return next((name for name, _, _ in options if name in pending), None)


# What a clock set wrong makes package managers and downloads say: signatures and
# certificates look not valid yet, or expired, and keys look made in the future.
CLOCK = re.compile(
    r'not yet valid|not valid yet|certificate has expired|seconds? in the future|time warp'
    r'|clock problem',
    re.IGNORECASE,
)
CLOCK_HINT = (
    " (this PC's clock looks wrong: check the date and time with  timedatectl, and let the network "
    'set them:  sudo timedatectl set-ntp true)'
)


def clock_hint(text):
    """CLOCK_HINT when a message is what a wrong clock causes, otherwise ''."""
    return CLOCK_HINT if CLOCK.search(text or '') else ''


def _output(result):
    return (result.stdout or '') + '\n' + (result.stderr or '')


def _up_to(seconds):
    """The end of a "waiting for it to finish" line: how long it waits at most."""
    return '(up to ' + str(seconds // 60) + ' minutes)...'


def _reason(result):
    """The last line a package manager printed, for error messages (with a word on the
    clock when that is what went wrong)."""
    last = ((result.stderr or result.stdout or '').strip().splitlines()[-1:] or ['no details'])[0]
    return last + clock_hint(_output(result))


# A package source that did not answer: a mirror hiccup, a download cut off, the network
# still coming up. The same command is tried again a little later, a few times; any other
# failure (a missing package, a conflict, a broken file) is reported at once.
TRANSIENT = re.compile(
    r'failed to fetch|failed retrieving file|failed to download|cannot download|download(ing)? (\(curl\) )?(error|failed)'
    r'|curl error|could not resolve|temporary failure|name resolution|connection (failed|timed out|refused|reset)'
    r'|timeout (was reached|exceeded)|timed out|network is unreachable|all mirrors were tried|hash sum mismatch'
    r'|operation too slow|not found on medium|http (response|error)[: ]+(404|5\d\d)|service unavailable|bad gateway',
    re.IGNORECASE,
)
RETRIES = 3  # attempts in all
RETRY_PAUSE = 20  # seconds between them


# Another program holds the package manager's lock (automatic updates, PackageKit, a
# software centre, another pacman): waited out, up to LOCK_WAIT seconds, instead of
# counting as a failure. (apt-get and zypper wait for some of these locks themselves.)
BUSY = re.compile(
    r'could not get lock|is held by process|failed to obtain rpm transaction lock'
    r'|unable to lock database|system management is locked',
    re.IGNORECASE,
)
LOCK_WAIT = 600


def transient(result):
    """The package manager failed because a source did not answer."""
    return bool(result.returncode) and bool(TRANSIENT.search(_output(result)))


def busy(result):
    """The package manager failed because another program was using it."""
    return bool(result.returncode) and bool(BUSY.search(_output(result)))


# apt-get update says these and still succeeds when a source did not answer: it goes on
# with that source's lists from before, so a new repository may have none at all.
STALE_LISTS = re.compile(r'^W: (Failed to fetch|Some index files failed to download)', re.MULTILINE)


def stale_lists(result):
    return bool(STALE_LISTS.search(_output(result)))


def environment():
    """For package managers and systemctl: English messages (their output is read, and
    "Candidate:" is "Candidat :" in French), no questions on the terminal, and Ubuntu's
    needrestart left out: after an install it can restart running services on its own,
    a Docker someone already runs included."""
    return {
        **os.environ,
        'LC_ALL': 'C',
        'LANGUAGE': 'C',
        'DEBIAN_FRONTEND': 'noninteractive',
        'NEEDRESTART_SUSPEND': '1',
        'NEEDRESTART_MODE': 'l',
    }


# apt waits this long (seconds) for another package job, such as Ubuntu's automatic
# security updates, instead of failing at once because dpkg is busy.
APT_WAIT = ['-o', 'DPkg::Lock::Timeout=600']


# The package managers: never killed when they run out of time (run_command).
PACKAGE_TOOLS = frozenset({'pacman', 'apt-get', 'dpkg', 'dnf', 'zypper'})
# How long a package manager asked to stop (as Ctrl+C asks) gets before it is asked
# again with SIGTERM; after that it is waited for however long it takes.
STOP_GRACE = 300


def run_command(argv, **kwargs):
    """subprocess.run, except for a package manager (see run_patiently)."""
    if Path(argv[0]).name in PACKAGE_TOOLS:
        return run_patiently(argv, **kwargs)
    check = kwargs.pop('check', False)
    return subprocess.run(argv, check=check, **kwargs)


def run_patiently(argv, *, capture_output=False, timeout=None, check=False, **kwargs):
    """subprocess.run for a package manager. When it runs out of time, or setup itself is
    stopped, it is asked to stop and waited for instead of being killed (as
    subprocess.run does): killed, pacman leaves its lock behind (and every later run
    then waits for it), and dnf or zypper can stop half-way through changing the system."""
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    given = kwargs.pop('input', None)
    if given is not None:
        kwargs['stdin'] = subprocess.PIPE
    with subprocess.Popen(argv, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(given, timeout=timeout)
        except subprocess.TimeoutExpired:
            stdout, stderr = _stop_patiently(process)
            raise subprocess.TimeoutExpired(argv, timeout, stdout, stderr) from None
        except BaseException:
            _stop_patiently(process)
            raise
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def _stop_patiently(process, grace=None):
    """Ask a package manager to stop as Ctrl+C does (pacman, dnf and zypper then finish
    or undo the step they are on) and wait for it; one still running after `grace`
    seconds gets SIGTERM and is waited for. (Only the package manager itself is asked:
    a dpkg it started goes on to the end, as it must.)"""
    if process.poll() is not None:
        return process.communicate()
    with contextlib.suppress(OSError):  # the terminal may be gone (an SSH connection that dropped)
        print('Asking ' + Path(str(process.args[0])).name + ' to stop; waiting until it has stopped safely...')
    process.send_signal(signal.SIGINT)
    try:
        return process.communicate(timeout=STOP_GRACE if grace is None else grace)
    except subprocess.TimeoutExpired:
        process.terminate()
        return process.communicate()


def _fetch(url, timeout=60):
    """A repository signing key (at most 64 KB) from its maker's https address."""
    if not url.startswith('https://'):
        raise RuntimeError('Repository keys are only downloaded over https')
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read(65536)


def _site(url):
    """The site (host name) of an address: what a person checks in a browser or a filter."""
    return urllib.parse.urlsplit(url).hostname or url


def _why(exc):
    """The words of a failed download (urllib keeps them in `reason`)."""
    return str(getattr(exc, 'reason', exc))


# What a proxy or firewall that refused the connection makes a download say (a filter
# that answers in its place says 403 through the proxy's tunnel).
REFUSED = re.compile(r'tunnel connection failed|proxy|403|forbidden', re.IGNORECASE)


def unreachable(maker, url, what, reason, tries):
    """The words for a site that could not be reached: which site, the likely causes and
    what to do. Never "check your internet" alone: on a PC that has just downloaded its
    packages, the internet works, and a filter or proxy blocking that one site is likelier."""
    site = _site(url)
    tried = f', after {tries} tries' if tries > 1 else ''
    if REFUSED.search(reason):
        causes = (
            ' A proxy, firewall or filter on your network refused it (a router or parental filter, an ad-blocker '
            'such as Pi-hole, a VPN): allow that site there, or ask whoever runs your network.'
        )
    else:
        causes = (
            ' Likely causes: something on your network blocks that site (a proxy, a router or parental filter, '
            'an ad-blocker such as Pi-hole, a VPN), the site is down for a while, or this PC is offline right now.'
        )
    return (
        f'Could not reach {site} ({maker}\'s site) to download {what}{tried} ({reason}).{causes} Check that '
        f'https://{site} opens in a browser on this PC, then run  sudo bash install.sh  again.' + clock_hint(reason)
    )


def answered_badly(maker, url, code, tries):
    """The words for a site that answered with an error of its own (it is up but failing)."""
    tried = f' {tries} times' if tries > 1 else ''
    return (
        f'{_site(url)} ({maker}\'s site) answered with an error{tried} (HTTP {code} for {url}): the site has a '
        'problem for now. Try again later with  sudo bash install.sh .'
    )


class Family:
    """One Linux family. Subclasses fill in the class attributes and the package-manager
    calls; everything else is shared."""

    name = ''
    label = ''
    manager = ''  # the package-manager program
    ids = ()  # os-release ID values of this family
    like = ()  # os-release ID_LIKE words of derivatives
    packages = MappingProxyType({})  # NekoServer's name -> this family's package name(s)
    services = MappingProxyType({})  # NekoServer's name -> this family's systemd unit
    settings = ()  # this family's own host settings, kept in host backups
    journal = None  # where an unfinished installation is recorded, for families
    # whose packages need tidying afterwards (settle)
    root = Path('/')  # where system files are (a folder in tests)
    sleep = staticmethod(time.sleep)
    say = staticmethod(print)  # a word to the person running setup

    def __init__(self, run=run_command, release=None):
        self.run = run
        self.release = os_release() if release is None else release

    def retrying(self, call, failed=transient):
        """call() runs the package manager; when a source did not answer (`failed`), it is
        called again a little later, RETRIES times in all. While another program holds the
        package manager's lock it is called again until that one has finished (up to
        LOCK_WAIT seconds, also counting the time the package manager waited itself),
        without counting as an attempt. The last result is returned."""
        attempt, waited, start = 1, 0, time.monotonic()
        while True:
            result = call()
            if busy(result) and max(waited, time.monotonic() - start) < LOCK_WAIT:
                waited = self._wait_for_other(result, waited)
                continue
            if not failed(result) or attempt == RETRIES:
                return result
            self.say(
                'The package source did not answer ('
                + _reason(result)
                + '); trying again in '
                + str(RETRY_PAUSE)
                + ' seconds ('
                + str(attempt + 1)
                + ' of '
                + str(RETRIES)
                + ')...'
            )
            self.sleep(RETRY_PAUSE)
            attempt += 1

    # Attempts at a maker's site (a repository key, a release check): as many as for a
    # package source. repair() asks only once: it goes on without the key when the
    # repository still works, and must not hold up a PC that is offline.
    download_tries = RETRIES

    def download(self, url):
        """self.fetch(url). A site that did not answer (no connection, a timeout, a proxy
        that refused it, an error of the site's own) is asked again RETRY_PAUSE seconds
        later, download_tries times in all, as a package source is; one that said "not
        found" (404, a release it does not package) or that a wrong clock is refusing is
        not. The last error is raised."""
        for attempt in range(1, self.download_tries + 1):
            try:
                return self.fetch(url)
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or attempt == self.download_tries:
                    raise
                reason = 'HTTP ' + str(exc.code)
            except (OSError, http.client.HTTPException) as exc:
                reason = _why(exc)
                if attempt == self.download_tries or clock_hint(reason):
                    raise
            self.say(
                _site(url)
                + ' did not answer ('
                + reason
                + '); trying again in '
                + str(RETRY_PAUSE)
                + ' seconds ('
                + str(attempt + 1)
                + ' of '
                + str(self.download_tries)
                + ')...'
            )
            self.sleep(RETRY_PAUSE)
        raise RuntimeError('Nothing was downloaded from ' + url)  # (download_tries below 1)

    def _wait_for_other(self, result, waited):
        """One pause while another program uses the package manager (said once)."""
        if not waited:
            self.say(
                'Another program is installing or updating packages (' + _reason(result) + '); waiting for it '
                'to finish ' + _up_to(LOCK_WAIT)
            )
        self.sleep(RETRY_PAUSE)
        return waited + RETRY_PAUSE

    @classmethod
    def matches(cls, release):
        return bool(set(_words(release, 'ID')) & set(cls.ids) or set(_words(release, 'ID_LIKE')) & set(cls.like))

    @classmethod
    def service(cls, wanted):
        """The systemd unit (with .service) this family uses for a NekoServer service name."""
        return cls.services.get(wanted, wanted + '.service')

    def path(self, absolute):
        return self.root / absolute.lstrip('/')

    def ready(self):
        """The package manager can be called."""
        return shutil.which(self.manager) is not None

    def names(self, wanted):
        """This family's package names for one of NekoServer's names."""
        found = self.packages.get(wanted, wanted)
        return list(found) if isinstance(found, tuple) else [found]

    @classmethod
    def reboot_pending(cls, root='/'):
        """An earlier update needs a restart before services are changed. (Arch's
        new-kernel check runs in install.sh, right after its system update.)"""
        return False

    def _call(self, argv, timeout, **extra):
        extra.setdefault('env', environment())
        return self.run(argv, capture_output=True, text=True, timeout=timeout, check=False, **extra)

    def available(self, wanted):
        """The configured repositories offer this package."""
        raise NotImplementedError

    def has(self, package):
        """This one package (in the family's own name) is installed."""
        raise NotImplementedError

    def installed(self, wanted):
        return self.ready() and all(self.has(name) for name in self.names(wanted))

    def _install(self, names, timeout):
        raise NotImplementedError

    def prepare(self, missing):
        """Before installing: check for conflicts and add vendor repositories. May return
        what settle() must know about the system as it was before (kept in the journal)."""

    def protect(self, before):
        """Just before installing: keep the new services from starting until settle()."""

    def wait_until_idle(self):
        """After an installation was cut off: wait until the package tools have stopped."""

    def repair(self, log=print):
        """Bring NekoServer's own package-manager settings up to date (an older release
        may have left some that stop the package manager working)."""

    @contextlib.contextmanager
    def locked(self):
        """One NekoServer package job at a time, whichever process runs it."""
        yield

    def settle(self, packages, before):
        """After installing, also after a failed or interrupted one: undo anything the
        packages did that NekoServer manages itself."""

    def packaged_example(self, absolute):
        """This settings file is known to be its package's untouched example (families
        without a record of what their packages wrote never know: None)."""

    def set_aside_samba_example(self, packages, before):
        """Samba's packaged example smb.conf, never anyone's own: it must not have existed
        before, and the package system's record of what the package wrote (ucf on Debian,
        rpm on Fedora) must say it is unchanged. With no such record it stays, and the Samba
        step asks for it to be reviewed."""
        config = self.path(SAMBA_CONFIG)
        if 'samba' in packages and not before.get('samba_config_existed', True) and self.packaged_example(SAMBA_CONFIG):
            config.replace(config.with_name('smb.conf.packaged'))

    def reconcile(self):
        """Finish an installation that stopped part-way, now or in an earlier run: the
        packages it did install are tidied up (settle) before anything else happens."""
        with self.locked():
            self._reconcile()

    def _reconcile(self):
        if self.journal is None:
            return
        record = self.path(self.journal)
        if not record.exists():
            return
        try:
            saved = json.loads(record.read_text())
            packages, before = saved['packages'], saved['before']
            if (
                saved.get('format') != 'neko-package-install-v1'
                or not isinstance(before, dict)
                or not all(isinstance(name, str) for name in packages)
            ):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError('Unreadable record of an unfinished package installation: ' + str(record)) from None
        self.settle(packages, before)
        record.unlink()

    def install(self, wanted, timeout=900):
        """Install the packages that aren't installed yet; nothing else on the system is
        upgraded. Raises RuntimeError with the package manager's reason on failure."""
        if not self.ready():
            raise RuntimeError(self.label + ' package manager (' + self.manager + ') not found')
        with self.locked():
            self._reconcile()
            missing = [name for name in wanted if not self.installed(name)]
            if missing:
                self._install_missing(missing, timeout)
            return missing

    def _install_missing(self, missing, timeout):
        if {'docker', 'docker-compose'} & set(missing) and snap_docker(self.root):
            raise RuntimeError(SNAP_DOCKER)
        before = self.prepare(missing) or {}
        if self.journal is not None:
            atomic_write(
                self.path(self.journal),
                json.dumps({'format': 'neko-package-install-v1', 'packages': missing, 'before': before}) + '\n',
            )
        # Only the parts that are missing: a package already there is never reinstalled
        # or upgraded (that would restart a Docker someone already runs, for example).
        # (Two of NekoServer's names can be one package here: listed once.)
        names = list(dict.fromkeys(real for name in missing for real in self.names(name) if not self.has(real)))
        setup_progress.packages(missing, names)  # the setup window says what each is for
        try:
            self.protect(before)
            result = self.retrying(lambda: self._install(names, timeout))
        except subprocess.TimeoutExpired as exc:
            self._after_interruption()
            # The time actually waited (a family may allow more than asked).
            raise RuntimeError(
                'Installing '
                + ', '.join(missing)
                + ' did not finish within '
                + str(int(exc.timeout or timeout) // 60)
                + ' minutes; run setup again'
            ) from None
        except BaseException:
            self._after_interruption()
            raise
        # Packages can be installed even when the whole command failed: tidy them first.
        self._reconcile()
        if result.returncode:
            raise RuntimeError('Installing ' + ', '.join(missing) + ' failed: ' + _reason(result))

    def existing_units(self, units):
        """The services that are already there before installing (a Docker someone runs,
        say): those are never stopped or switched off, only ones the packages newly bring."""
        states = self.unit_states(units) if units else {}
        return [unit for unit in units if states.get(unit, {}).get('LoadState') not in (None, 'not-found')]

    def unit_states(self, units):
        result = self._call(['systemctl', 'show', *units, '--property=Id,LoadState,UnitFileState,ActiveState'], 60)
        if result.returncode:
            raise RuntimeError('systemd could not report on ' + ', '.join(units) + ': ' + _reason(result))
        states = {}
        for block in result.stdout.split('\n\n'):
            fields = {
                key.strip(): value.strip()
                for key, value in (line.split('=', 1) for line in block.splitlines() if '=' in line)
            }
            if fields.get('Id'):
                states[fields['Id']] = fields
        return states

    def switch_off(self, units):
        """Disable and stop these services, then check that they really are. Units that
        aren't installed (not found) or are masked can't start and are left alone."""
        if not units:
            return
        states = self.unit_states(units)
        present = [unit for unit in units if states.get(unit, {}).get('LoadState') == 'loaded']
        if not present:
            return
        result = self._call(['systemctl', 'disable', '--now', '--quiet', *present], 120)
        states = self.unit_states(present)
        still = [
            unit
            for unit in present
            if states.get(unit, {}).get('UnitFileState') not in ('disabled', 'masked')
            or states.get(unit, {}).get('ActiveState') not in ('inactive', 'failed')
        ]
        if result.returncode or still:
            raise RuntimeError(
                'Could not switch off '
                + ', '.join(still or present)
                + ' after installing'
                + (': ' + _reason(result) if result.returncode else '')
                + '. NekoServer starts these itself once their drive is ready; switch them '
                'off (sudo systemctl disable --now ' + ' '.join(still or present) + ') and run setup again.'
            )

    def unmask(self, units):
        """Lift masks NekoServer set itself before installing."""
        if units:
            result = self._call(['systemctl', 'unmask', '--quiet', *units], 60)
            if result.returncode:
                raise RuntimeError('Could not unmask ' + ', '.join(units) + ': ' + _reason(result))

    def _after_interruption(self):
        """Tidy up once the package tools have stopped. If that isn't possible now, the
        record and the start protection stay for the next setup run."""
        with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
            self.wait_until_idle()
            self._reconcile()


class Arch(Family):
    name = 'arch'
    label = 'Arch Linux / Nyarch'
    manager = 'pacman'
    ids = ('arch', 'nyarch')
    like = ('arch',)
    services = MappingProxyType({'samba': SAMBA_UNIT, 'tailscale': TAILSCALE_UNIT})
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')
    wait = 600  # seconds to wait for another pacman to finish
    sleep = staticmethod(time.sleep)

    def available(self, wanted):
        return self.ready() and all(
            self._call(['pacman', '-Si', name], 60).returncode == 0 for name in self.names(wanted)
        )

    def has(self, package):
        return self._call(['pacman', '-Q', package], 30).returncode == 0

    def wait_until_idle(self):
        """pacman's lock file is there while another pacman (an updater, say) works: wait
        for it. One that no program has open was left by a pacman that was cut off
        (killed, or the PC lost power): nothing would ever remove it, so it goes now."""
        lock = self.path(PACMAN_LOCK)
        waited = 0
        while lock.exists():
            if self.lock_holders() == []:
                lock.unlink(missing_ok=True)
                self.say(
                    "pacman's lock (" + PACMAN_LOCK + ') was left by a pacman that was cut off (no program has '
                    'it open): removed.'
                )
                return
            if waited >= self.wait:
                raise RuntimeError(
                    f'pacman is still locked ({PACMAN_LOCK}) after {self.wait // 60} minutes: '
                    'another installation is running. Let it finish, then run setup again. If '
                    'nothing is installing, the lock is left from one that was cut off: remove it '
                    f'(sudo rm {PACMAN_LOCK}), run  sudo pacman -Syu, then setup again.'
                )
            if not waited:
                self.say(
                    'Another package installation is running (pacman is locked); waiting for it to finish '
                    + _up_to(self.wait)
                )
            self.sleep(5)
            waited += 5

    def lock_holders(self):
        """The processes that have pacman's lock open: libalpm, which pacman and the
        graphical updaters use, keeps it open while it works. None when that cannot be
        seen (no /proc, or another account's processes that this one may not look at)."""
        try:
            found = {fds.parent.name: _open_files(fds) for fds in self.path('/proc').glob('[0-9]*/fd')}
        except PermissionError:
            return None
        seen = {pid: files for pid, files in found.items() if files is not None}
        return [pid for pid, files in seen.items() if PACMAN_LOCK in files] if seen else None

    def _install(self, names, timeout):
        self.wait_until_idle()
        return self._call(['pacman', '-S', '--needed', '--noconfirm', *names], timeout)


def _open_files(fds):
    """The files one process has open (its /proc/<pid>/fd), None when it ended meanwhile.
    PermissionError when this account may not look."""
    try:
        return [os.readlink(fd) for fd in fds.iterdir()]
    except PermissionError:
        raise
    except OSError:
        return None


class Repository:
    """A maker's apt repository: where it is, its signing key and its source line."""

    MARKER = '# Managed by NekoServer\n'

    def __init__(self, name, address, key_url, key_path, line):
        self.name, self.address, self.key_url, self.key_path, self.line = name, address, key_url, key_path, line

    def listing(self, key, system, codename):
        return self.line.format(key=key, address=self.address, os=system, codename=codename)


DOCKER = Repository(
    'docker',
    'https://download.docker.com/linux/',
    'https://download.docker.com/linux/{os}/gpg',
    '/etc/apt/keyrings/docker.asc',
    'deb [arch=amd64 signed-by={key}] {address}{os} {codename} stable\n',
)
TAILSCALE = Repository(
    'tailscale',
    'https://pkgs.tailscale.com/stable/',
    'https://pkgs.tailscale.com/stable/{os}/{codename}.noarmor.gpg',
    # Under /etc/apt/keyrings, so host backups keep it with the list.
    '/etc/apt/keyrings/tailscale-archive-keyring.gpg',
    'deb [signed-by={key}] {address}{os} {codename} main\n',
)
# Debian's samba package writes an example smb.conf; NekoServer writes its own.
SAMBA_CONFIG = '/etc/samba/smb.conf'
POLICY = '/usr/sbin/policy-rc.d'
# Held by NekoServer for each package job (install, tidy-up, repository repair).
PACKAGE_LOCK = '/run/nekoserver-packages.lock'
# There while a pacman works (created, held open and deleted by pacman itself).
PACMAN_LOCK = '/var/lib/pacman/db.lck'
# Where snapd mounts installed snaps: Ubuntu's folder, and other systems'.
SNAP_FOLDERS = ('/snap', '/var/lib/snapd/snap')
# Tried on Ubuntu: the snap's Docker takes over /run/docker.sock at once, and after it is
# removed there is no socket until Docker is restarted.
SNAP_DOCKER = (
    'Docker is installed here as a snap. Its Docker takes over the socket every docker command uses '
    '(/run/docker.sock), so NekoServer\'s backups, updates and setup would reach it instead of the '
    'Docker that runs your apps. NekoServer uses Docker\'s official packages and does not mix them: if '
    'nothing else uses the snap\'s containers, remove it (sudo snap remove docker), restart Docker if it '
    'is installed (sudo systemctl restart docker.socket docker), then run setup again.'
)


def snap_docker(root='/'):
    """Docker is installed as a snap (its daemon runs as snap.docker.dockerd)."""
    return any((Path(root) / folder.lstrip('/') / 'docker').is_dir() for folder in SNAP_FOLDERS)


# Held while they work: apt's (released if apt-get is stopped) and dpkg's own (held
# until dpkg itself has finished, also when the apt-get that started it was stopped).
DPKG_LOCKS = ('/var/lib/dpkg/lock-frontend', '/var/lib/dpkg/lock')
# Held while the package lists are refreshed (by apt-get update, or the system's apt-daily).
APT_LISTS_LOCK = '/var/lib/apt/lists/lock'
# deb-systemd-helper's record of units a package masked itself.
PACKAGE_MASKS = '/var/lib/systemd/deb-systemd-helper-masked'
OUR_POLICY = '#!/bin/sh\n# NekoServer: no service starts while packages install.\nexit 101\n'


# What apt says when it cannot check a repository with the key its definition names.
KEY_REJECTED = re.compile(
    r'NO_PUBKEY|EXPKEYSIG|is not signed|unsupported filetype'
    r"|signatures? (couldn't|could not) be verified"
)


def valid_key(data):
    """An OpenPGP public key as apt reads it: ASCII-armoured (whole, from its first line to
    its last), or binary, starting with a public-key packet (old or new packet format)."""
    text = data.strip()
    if text.startswith(b'-----BEGIN PGP PUBLIC KEY BLOCK-----'):
        return text.endswith(b'-----END PGP PUBLIC KEY BLOCK-----')
    return bool(data) and data[0] in (0x98, 0x99, 0x9A, 0xC6)


def _valid_key_file(path):
    try:
        return path.is_file() and valid_key(path.read_bytes())
    except OSError:
        return False


# Questions a package asks while it is set up, answered beforehand (debconf-set-selections):
# with no terminal to ask on, Steam's installer takes its licence as declined and fails.
# The TV step installs Steam because it was chosen, so its licence is accepted then.
ANSWERS = MappingProxyType(
    {'steam-installer': 'steam-installer steam/question select I AGREE\nsteam-installer steam/license note \n'}
)


def _deb822(text):
    """The stanzas of a deb822 file as {lower-case field name: value}."""
    for stanza in text.replace('\r\n', '\n').split('\n\n'):
        fields = _deb822_fields(stanza)
        if fields:
            yield fields


def _deb822_fields(stanza):
    """One stanza's fields. A value can go on over lines that start with a space or tab
    (a lone "." is an empty line); comment lines are skipped."""
    fields, name = {}, None
    for line in stanza.splitlines():
        if line.startswith('#'):
            continue
        if line[:1] in (' ', '\t') and name:
            fields[name] += '\n' + ('' if line.strip() == '.' else line.strip())
        elif ':' in line:
            name, value = line.split(':', 1)
            name = name.strip().lower()
            fields[name] = value.strip()
    return fields


def _stanzas(path, text):
    """The repository addresses of the enabled entries in a one-line .list file or a
    deb822 .sources file."""
    if path.suffix == '.sources':
        for fields in _deb822(text):
            if fields.get('enabled', 'yes').strip().lower() != 'no':
                yield from fields.get('uris', '').split()
    else:
        yield from (line for line in text.splitlines() if line.split()[:1] in (['deb'], ['deb-src']))


def _audit_packages(text):
    """The package names dpkg --audit lists (indented lines; an ":arch" suffix dropped)."""
    return {line.split()[0].split(':')[0] for line in text.splitlines() if line[:1] in (' ', '\t') and line.split()}


def _free(lock):
    """Nobody holds this lock file (a missing one is free too)."""
    if not lock.exists():
        return True
    try:
        with lock.open('a') as handle:
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(handle, fcntl.LOCK_UN)
            return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _md5(path):
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


class PackageLock:
    """One NekoServer package job at a time across processes (setup, a retry, a step
    installing a missing tool), held at PACKAGE_LOCK; nested jobs share it."""

    wait = 1800  # seconds to wait for another package job to finish
    sleep = staticmethod(time.sleep)
    holding = 0  # nested package jobs in this process

    @contextlib.contextmanager
    def locked(self):
        if self.holding:
            self.holding += 1
            try:
                yield
            finally:
                self.holding -= 1
            return
        path = self.path(PACKAGE_LOCK)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as handle:
            self._take(handle)
            self.holding = 1
            try:
                yield
            finally:
                self.holding = 0
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _take(self, handle):
        """The lock, once another setup's package job has finished (up to self.wait seconds)."""
        waited = 0
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if waited >= self.wait:
                    raise RuntimeError(
                        'Another NekoServer setup is installing packages; let it finish, then run setup again'
                    ) from None
                if not waited:
                    self.say(
                        'Another NekoServer setup is installing packages; waiting for it to finish ' + _up_to(self.wait)
                    )
                self.sleep(5)
                waited += 5


class Debian(PackageLock, Family):
    name = 'debian'
    label = 'Debian / Ubuntu'
    manager = 'apt-get'
    ids = ('debian', 'ubuntu')
    # Some Ubuntu-based systems (elementary OS, for one) name only Ubuntu.
    like = ('debian', 'ubuntu')
    packages = MappingProxyType(
        {
            # Docker's official packages, never Debian's own docker.io.
            'docker': ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'),
            'docker-compose': 'docker-compose-plugin',
            'intel-media-driver': 'intel-media-va-driver',
            'libva-mesa-driver': 'mesa-va-drivers',
            'libva-utils': 'vainfo',
            'firefox': 'firefox-esr',
            # The TV: Steam (contrib/multiverse, with 32-bit packages allowed), Mesa's Vulkan
            # drivers for every chip in one package, and bluetoothctl inside bluez.
            'steam': 'steam-installer',
            'vulkan-intel': 'mesa-vulkan-drivers',
            'lib32-vulkan-intel': 'mesa-vulkan-drivers:i386',
            'vulkan-radeon': 'mesa-vulkan-drivers',
            'lib32-vulkan-radeon': 'mesa-vulkan-drivers:i386',
            'bluez-utils': 'bluez',
            # Building the runtime: Rust comes as two packages here.
            'rust': ('rustc', 'cargo'),
        }
    )
    services = MappingProxyType({'samba': 'smbd.service', 'tailscale': TAILSCALE_UNIT})
    settings = (
        '/etc/apt/sources.list',
        '/etc/apt/sources.list.d',
        '/etc/apt/preferences.d',
        '/etc/apt/keyrings',
        '/etc/default/locale',
        '/etc/default/keyboard',
    )
    journal = '/var/lib/nekoserver-host/package-install.json'
    # Packages from their makers' own repositories (Debian doesn't carry them).
    repositories = MappingProxyType({'docker': DOCKER, 'docker-compose': DOCKER, 'tailscale': TAILSCALE})
    # Services these packages enable and start on their own. NekoServer starts them
    # itself once their storage, settings and guards are in place (Docker must never
    # start on the system disk), so they are kept from starting and left switched off.
    autostarted = MappingProxyType(
        {
            'docker': ('docker.service', 'docker.socket'),
            'caddy': ('caddy.service',),
            # (samba-ad-dc.service comes masked by the package itself and is left as it is.
            # winbind comes with samba here and is never used by a standalone server; the
            # settings backup refuses to run beside it.)
            'samba': ('smbd.service', 'nmbd.service', 'winbind.service'),
            'tailscale': (TAILSCALE_UNIT,),
        }
    )
    # Debian's own container packages that would conflict with Docker's.
    conflicts = ('docker.io', 'docker-compose', 'docker-doc', 'podman-docker', 'containerd', 'runc')

    def __init__(self, run=run_command, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch
        self.refreshed = False
        self.holding = 0  # nested package jobs in this process
        if self.ubuntu():
            self.packages = MappingProxyType({**type(self).packages, 'firefox': 'firefox'})

    def ubuntu(self):
        return 'ubuntu' in _words(self.release, 'ID') + _words(self.release, 'ID_LIKE')

    def foreign_architectures(self):
        """The extra package architectures dpkg allows (i386 is what Steam's 32-bit side
        needs; without it apt still lists steam-installer, then fails to install it)."""
        return set(self._call(['dpkg', '--print-foreign-architectures'], 30).stdout.split())

    def vendor_release(self):
        """(distribution, codename) as Docker's and Tailscale's repositories name them."""
        codename = self.release.get('VERSION_CODENAME', '')
        if self.ubuntu():
            # Derivatives such as Linux Mint name the Ubuntu release they are built on.
            return 'ubuntu', (self.release.get('UBUNTU_CODENAME') or codename).lower()
        # Derivatives such as LMDE name the Debian release they are built on.
        return 'debian', (self.release.get('DEBIAN_CODENAME') or codename).lower()

    @classmethod
    def reboot_pending(cls, root='/'):
        return (Path(root) / 'run/reboot-required').exists()

    def refresh(self):
        """Refresh the package lists (never an upgrade), once. The system refreshes them
        itself now and then (apt-daily): apt-get waits for dpkg's lock, not for the lists'
        lock, so that wait is here (apt-get names the holder if it outlasts it)."""
        if self.refreshed:
            return
        self._wait_for_lists()
        result = self._update()
        if self._renew_rejected_keys(result):
            result = self._update()
        if result.returncode:
            raise RuntimeError('Refreshing the package lists failed: ' + _reason(result))
        if stale_lists(result):
            line = next(line for line in _output(result).splitlines() if STALE_LISTS.match(line))
            self.say(
                'Some package sources could not be reached (' + line[3:].strip() + '); going on with their '
                'lists from before' + clock_hint(_output(result)) + '.'
            )
        self.refreshed = True

    def _wait_for_lists(self):
        waited = 0
        while waited < self.wait and not _free(self.path(APT_LISTS_LOCK)):
            if not waited:
                self.say(
                    'The system is refreshing its package lists itself; waiting for it to finish ' + _up_to(self.wait)
                )
            self.sleep(5)
            waited += 5

    def _update(self):
        """apt-get update, tried again when a source did not answer, also when apt only
        warned about it (it exits 0 then)."""
        return self.retrying(
            lambda: self._call(['apt-get', *APT_WAIT, 'update'], 1200),
            failed=lambda result: transient(result) or stale_lists(result),
        )

    def _renew_rejected_keys(self, result):
        """NekoServer's own repositories whose signing key apt could not use (missing,
        damaged, or replaced by its maker): their key is downloaded again. True when one was."""
        rejected = [line for line in _output(result).splitlines() if KEY_REJECTED.search(line)]
        renewed = False
        for repository in dict.fromkeys(self.repositories.values()):
            if not any(repository.address in line for line in rejected) or not self.own_listing(repository):
                continue
            self.say(
                'apt could not check the ' + repository.name + ' repository with its key; downloading the key again'
            )
            renewed = self.add_repository(repository, renew=True) or renewed
        return renewed

    def has(self, package):
        # "install ok installed", or "hold ok installed" for a package someone holds.
        result = self._call(['dpkg-query', '-W', '-f=${Status}', package], 30)
        return result.returncode == 0 and result.stdout.split()[1:] == ['ok', 'installed']

    def candidate(self, package):
        """(major, minor) of the version apt would install for this package, or None."""
        if not self.ready():
            return None
        self.refresh()
        policy = self._call(['apt-cache', 'policy', package], 60).stdout or ''
        match = re.search(r'Candidate:\s*(?:\d+:)?(\d+)\.(\d+)', policy)
        return (int(match[1]), int(match[2])) if match else None

    def available(self, wanted):
        if not self.ready():
            return False
        self.refresh()
        for name in self.names(wanted):
            policy = self._call(['apt-cache', 'policy', name], 60).stdout
            if 'Candidate:' not in policy or 'Candidate: (none)' in policy:
                return False
        return True

    def sources(self, address):
        """Every apt source file that already uses this repository address (one-line
        .list files and deb822 .sources files alike, whatever they are called)."""
        folder = self.path('/etc/apt/sources.list.d')
        files = [self.path('/etc/apt/sources.list'), *sorted(folder.glob('*.list')), *sorted(folder.glob('*.sources'))]
        found = []
        for path in files:
            try:
                text = path.read_text(errors='replace')
            except OSError:
                continue
            if any(address in entry for entry in _stanzas(path, text)):
                found.append(path)
        return found

    def _ours(self, listing):
        """The list is NekoServer's: it carries the marker setup writes first."""
        return listing.read_text(errors='replace').startswith(Repository.MARKER)

    def fetch_key(self, repository, system, codename):
        """The repository's signing key, or an error that says what went wrong: no
        internet, or a release the maker doesn't package for yet (their key and package
        addresses name the release, so a new Ubuntu gets "404" until they add it)."""
        url = repository.key_url.format(os=system, codename=codename)
        name = repository.name.capitalize()
        try:
            data = self.download(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    f'{name} does not offer packages for this release yet ({system} "{codename}": '
                    f'{url} was not found). Wait for {name} to add it, or install a release it '
                    f'supports, then run setup again'
                ) from None
            raise RuntimeError(answered_badly(name, url, exc.code, self.download_tries)) from None
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(
                unreachable(name, url, f'the key that checks {name}\'s packages', _why(exc), self.download_tries)
            ) from None
        if not data or len(data) >= 65536 or not valid_key(data):
            raise RuntimeError(
                f'The {name} repository key downloaded from {url} is not a key file (a Wi-Fi login '
                'page or a proxy may have answered instead); try again later'
            )
        return data

    def add_repository(self, repository, renew=False):
        """Make a maker's repository usable. Someone else's definition of it, in any
        file, is left alone (a second one makes apt refuse to work); NekoServer's own
        is brought up to date and its key restored when missing or damaged (or, with
        `renew`, downloaded again because apt could not use it)."""
        system, codename = self.vendor_release()
        if not codename:
            raise RuntimeError('Cannot tell this system\'s release codename for the ' + repository.name + ' repository')
        listing = self.path('/etc/apt/sources.list.d/' + repository.name + '.list')
        found = self.sources(repository.address)
        ours = listing in found and self._ours(listing)
        if ours and len(found) > 1:
            listing.unlink()  # someone else defines it too: theirs is used
            self.refreshed = False
            return False
        if found and not ours:
            return False
        if listing.exists() and not ours:
            raise RuntimeError(
                str(listing)
                + ' is someone else\'s and does not use '
                + repository.address
                + '; rename or remove it, then run setup again.'
            )
        desired = Repository.MARKER + repository.listing(repository.key_path, system, codename)
        key = self.path(repository.key_path)
        if not renew and ours and listing.read_text() == desired and _valid_key_file(key):
            return False
        data = self.fetch_key(repository, system, codename)
        key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        atomic_write(key, data, 0o644)
        atomic_write(listing, desired.encode(), 0o644)
        self.refreshed = False
        return True

    def wait_until_idle(self):
        """Wait until neither apt nor dpkg is working any more."""
        waited = 0
        while not self.dpkg_idle():
            if waited >= self.wait:
                raise RuntimeError(
                    'apt or dpkg is still working (another installation, or one that was cut '
                    'off). New services stay switched off; run setup again once it has finished.'
                )
            if not waited:
                self.say(
                    'apt or dpkg is still working (another installation, or one that was cut off); waiting for '
                    'it to finish ' + _up_to(self.wait)
                )
            self.sleep(5)
            waited += 5

    def dpkg_idle(self):
        return all(_free(self.path(lock)) for lock in DPKG_LOCKS)

    def answer_questions(self, names):
        """The questions these packages would ask while they are set up, answered first."""
        answers = ''.join(ANSWERS[name] for name in dict.fromkeys(names) if name in ANSWERS)
        if not answers:
            return
        if 'steam-installer' in names:
            self.say(
                "Steam's installer asks to accept Valve's Steam licence: setup answers \"I agree\" for you "
                '(the TV step, which you chose, installs Steam).'
            )
        result = self._call(['debconf-set-selections'], 60, input=answers)
        if result.returncode:
            raise RuntimeError(
                'Could not answer the setup questions of '
                + ', '.join(n for n in names if n in ANSWERS)
                + ' beforehand: '
                + _reason(result)
            )

    def finish_configuring(self, names):
        """Packages an interrupted installation left unpacked but not set up are set up
        now, while their services are still kept from starting: run later by hand
        (dpkg --configure -a), their setup would start those services unprotected."""
        audit = self._call(['dpkg', '--audit'], 60)
        if audit.returncode and not audit.stdout.strip():
            raise RuntimeError(
                'dpkg could not say which packages are unfinished: '
                + _reason(audit)
                + '. Their services stay switched off; run setup again.'
            )
        pending = _audit_packages(audit.stdout)
        ours = [name for name in names if name in pending]
        if not ours:
            return  # nothing of this installation (someone else's is theirs)
        # Only these and the unfinished packages they need, never someone else's.
        self.answer_questions(ours)
        chosen = self._with_dependencies(ours, pending)
        result = self._call(['dpkg', '--configure', *chosen], 1800)
        if result.returncode:
            # Setup cannot go on past it (apt refuses to install anything while a package
            # is half set up), so it says which, and both ways out.
            raise RuntimeError(
                'dpkg could not finish setting up ' + ', '.join(chosen) + ' (left unfinished by an '
                'earlier installation): ' + _reason(result) + '. Their services stay switched off. '
                'Fix the problem and run setup again; or, if you no longer need '
                + ', '.join(ours)
                + ', remove '
                + ('it' if len(ours) == 1 else 'them')
                + ' (sudo apt-get remove '
                + ' '.join(ours)
                + ') and run setup again.'
            )

    def _with_dependencies(self, names, pending):
        """These packages plus the unfinished packages they need, directly or not. For a
        choice (`A | B`) nothing is added when one of them is already set up (or provided
        by a package that is); otherwise only the first unfinished one, as apt chose."""
        ready = self._ready(pending)
        chosen, queue = set(names), list(names)
        while queue:
            result = self._call(['dpkg-query', '-W', '-f=${Pre-Depends}, ${Depends}', queue.pop()], 30)
            for group in (result.stdout if result.returncode == 0 else '').split(','):
                needed = _still_needed(group, {**ready, **dict.fromkeys(chosen, ANY_VERSION)}, pending, self.satisfies)
                if needed:
                    chosen.add(needed)
                    queue.append(needed)
        return sorted(chosen)

    def satisfies(self, version, op, wanted):
        """`version op wanted` as dpkg compares Debian versions."""
        return self._call(['dpkg', '--compare-versions', version, op, wanted], 30).returncode == 0

    def _ready(self, pending):
        """{package: version} installed and set up (not unfinished), also when held, plus
        the names they provide (with the provided version, or None when unversioned)."""
        result = self._call(['dpkg-query', '-W', '-f=${db:Status-Abbrev}|${Package}|${Version}|${Provides}\n'], 60)
        ready = {}
        for line in result.stdout.splitlines() if result.returncode == 0 else []:
            status, package, version, provides = (line.split('|', 3) + ['', '', ''])[:4]
            # Wanted install or hold (i/h), installed (i), no error flag: "ii " or "hi ".
            if status[:2] not in ('ii', 'hi') or status[2:3].strip() or package in pending:
                continue
            ready[package] = version
            for given in provides.split(','):
                name, op, provided = _alternative(given)
                if name:
                    ready.setdefault(name, provided if op == '=' else None)
        return ready

    def repair(self, log=print):
        """Bring NekoServer's own repository definitions up to date, whether or not any
        package needs installing (on an upgrade, everything usually is installed): a copy
        of a repository someone else also defines goes (two definitions stop apt from
        working at all), and a restored definition whose key is missing gets the key back."""
        with self.locked():
            for repository in dict.fromkeys(self.repositories.values()):
                listing = self.own_listing(repository)
                if not listing:
                    continue
                # A definition that still works is kept when its site does not answer: asked
                # once, so an offline PC is not held up. A missing key is needed: asked as often
                # as any download.
                usable = self._usable(listing)
                self.download_tries = 1 if usable else RETRIES
                try:
                    self.add_repository(repository)
                except (OSError, RuntimeError, http.client.HTTPException) as exc:
                    if not usable:
                        raise RuntimeError(
                            'The ' + repository.name + ' repository key is missing and could not '
                            'be downloaded: ' + str(exc)
                        ) from None
                    log('Keeping the ' + repository.name + ' repository as it is for now (' + str(exc) + ')')
                finally:
                    self.download_tries = RETRIES

    def own_listing(self, repository):
        """NekoServer's own definition of this repository, when apt uses it (else None)."""
        listing = self.path('/etc/apt/sources.list.d/' + repository.name + '.list')
        if listing in self.sources(repository.address) and self._ours(listing):
            return listing
        return None

    def _usable(self, listing):
        """The key the definition points to is there and is a key (apt can use it as it is)."""
        found = re.search(r'signed-by=([^\s\]]+)', listing.read_text(errors='replace'))
        return bool(found) and _valid_key_file(self.path(found.group(1)))

    def prepare(self, missing):
        self.refuse_conflicts(missing)
        self.answer_questions([real for name in missing for real in self.names(name)])
        for repository in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(repository)
        self.refresh()
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        existing = self.existing_units(units)
        new = [unit for unit in units if unit not in existing]
        self.check_policy(new)
        return {'samba_config_existed': self.path(SAMBA_CONFIG).exists(), 'units_before': existing, 'new_units': new}

    def protect(self, before):
        """The new services are masked (they can't start, not even after a restart) and
        policy-rc.d stops the packages from starting anything while they install. Both
        stay until settle() has switched the services off."""
        new = before.get('new_units', [])
        if new:
            result = self._call(['systemctl', 'mask', '--quiet', *new], 60)
            if result.returncode:
                raise RuntimeError('Could not keep ' + ', '.join(new) + ' from starting: ' + _reason(result))
        if not self.path(POLICY).exists():
            atomic_write(self.path(POLICY), OUR_POLICY.encode(), 0o755)

    def refuse_conflicts(self, missing):
        if not {'docker', 'docker-compose'} & set(missing):
            return
        found = [name for name in self.conflicts if self.has(name)]
        if found:
            raise RuntimeError(
                'This system has its own Docker packages installed ('
                + ', '.join(found)
                + '). NekoServer uses Docker\'s official packages and does not mix or '
                'replace them. Remove them yourself if nothing else uses them, then run '
                'setup again.'
            )

    def check_policy(self, units):
        """A policy-rc.d that someone else put there must already refuse to start these
        services while packages install; NekoServer won't replace it."""
        policy = self.path(POLICY)
        if not policy.exists() or policy.read_text(errors='replace') == OUR_POLICY:
            return
        allowed = [unit for unit in units if not self._refuses(policy, unit)]
        if allowed:
            raise RuntimeError(
                str(POLICY) + ' (someone else\'s) would let ' + ', '.join(allowed) + ' start while '
                'installing, before NekoServer has its drive and settings ready. Move it aside '
                'for the installation (sudo mv ' + POLICY + ' ' + POLICY + '.saved), run setup '
                'again, then move it back.'
            )

    def _refuses(self, policy, unit):
        """The policy answers "not allowed" (101) to starting this service. One that hangs
        or can't run refuses nothing."""
        try:
            return self._call([str(policy), unit, 'start'], 30).returncode == 101
        except (OSError, subprocess.SubprocessError):
            return False

    def _install(self, names, timeout):
        if not self.dpkg_idle():
            self.say(
                'Another program (automatic updates, say) is installing packages; apt waits for it to finish '
                'first (up to 10 minutes)...'
            )
        return self._call(
            ['apt-get', *APT_WAIT, 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', *names], timeout + 600
        )

    def _reconcile(self):
        policy = self.path(POLICY)
        ours = policy.is_file() and policy.read_text(errors='replace') == OUR_POLICY
        if ours or self.path(self.journal).exists():
            # A package process from a cut-off installation may still be working:
            # nothing is tidied and no protection lifted until apt and dpkg have stopped.
            self.wait_until_idle()
        super()._reconcile()
        # Start protection goes only once nothing is left to tidy up.
        if ours:
            policy.unlink()

    def settle(self, packages, before):
        self.finish_configuring([real for name in packages for real in self.names(name)])
        self.set_aside_samba_example(packages, before)
        existing = before.get('units_before', [])
        new = [unit for name in packages for unit in self.autostarted.get(name, ()) if unit not in existing]
        self.unmask([unit for unit in new if unit in before.get('new_units', [])])
        try:
            self.switch_off(new)
        except RuntimeError:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._call(['systemctl', 'mask', '--quiet', *new], 60)  # stay protected until next time
            raise

    def unmask(self, units):
        """Lift NekoServer's own masks; one a package set itself stays."""
        ours = [unit for unit in units if not self.path(PACKAGE_MASKS + '/' + unit).exists()]
        if ours:
            result = self._call(['systemctl', 'unmask', '--quiet', *ours], 60)
            if result.returncode:
                raise RuntimeError('Could not unmask ' + ', '.join(ours) + ': ' + _reason(result))

    def packaged_example(self, absolute):
        """This settings file is still exactly what its package wrote (ucf's checksum of it).
        Without that record nothing is assumed: it may be someone's settings."""
        path = self.path(absolute)
        try:
            records = self.path('/var/lib/ucf/hashfile').read_text(errors='replace').splitlines()
        except OSError:
            return False
        recorded = [line.split()[0] for line in records if line.split()[1:] == [absolute]]
        return bool(recorded) and path.is_file() and recorded[-1] == _md5(path)


class RpmRepository:
    """A maker's dnf repository, written from known text rather than a downloaded .repo
    file; dnf checks its packages with the maker's key from the https address it names."""

    MARKER = '# Managed by NekoServer\n'

    def __init__(self, name, maker, address, text, release_url=None):
        self.name, self.maker, self.address, self.text = name, maker, address, text
        self.release_url = release_url  # answers only for a Fedora release the maker packages


DOCKER_RPM = RpmRepository(
    'docker-ce',
    'Docker',
    'https://download.docker.com/linux/fedora/',
    '[docker-ce-stable]\nname=Docker CE Stable - $basearch\n'
    'baseurl=https://download.docker.com/linux/fedora/$releasever/$basearch/stable\n'
    'enabled=1\ngpgcheck=1\ngpgkey=https://download.docker.com/linux/fedora/gpg\n',
    release_url='https://download.docker.com/linux/fedora/{release}/x86_64/stable/repodata/repomd.xml',
)
TAILSCALE_RPM = RpmRepository(
    'tailscale',
    'Tailscale',
    'https://pkgs.tailscale.com/stable/fedora/',
    '[tailscale-stable]\nname=Tailscale stable\nbaseurl=https://pkgs.tailscale.com/stable/fedora/$basearch\n'
    'enabled=1\ntype=rpm\nrepo_gpgcheck=1\ngpgcheck=1\ngpgkey=https://pkgs.tailscale.com/stable/fedora/repo.gpg\n',
)


class Fedora(PackageLock, Family):
    name = 'fedora'
    label = 'Fedora'
    manager = 'dnf'
    ids = ('fedora',)
    # RHEL and its rebuilds name fedora in ID_LIKE, but their repositories (and Docker's
    # for them) differ: not covered yet.
    like = ()
    packages = MappingProxyType(
        {
            # Docker's official packages, never Fedora's own moby-engine.
            'docker': ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'),
            'docker-compose': 'docker-compose-plugin',
            'libva-mesa-driver': 'mesa-va-drivers',
            # The TV: Steam from RPM Fusion's repository for it (Fedora's third-party source),
            # Mesa's Vulkan drivers for every chip in one package, bluetoothctl inside bluez.
            'vulkan-intel': 'mesa-vulkan-drivers',
            'lib32-vulkan-intel': 'mesa-vulkan-drivers.i686',
            'vulkan-radeon': 'mesa-vulkan-drivers',
            'lib32-vulkan-radeon': 'mesa-vulkan-drivers.i686',
            'bluez-utils': 'bluez',
            # Building the runtime: Rust comes as two packages here.
            'rust': ('rust', 'cargo'),
        }
    )
    services = MappingProxyType({'samba': SAMBA_UNIT, 'tailscale': TAILSCALE_UNIT})
    settings = ('/etc/dnf/dnf.conf', '/etc/yum.repos.d')
    journal = '/var/lib/nekoserver-host/package-install.json'
    repositories = MappingProxyType({'docker': DOCKER_RPM, 'docker-compose': DOCKER_RPM, 'tailscale': TAILSCALE_RPM})
    # Services these packages may enable on their own (presets, scriptlets): kept from
    # starting while they install, then left switched off until NekoServer's own steps
    # start them on the right drive, as on Debian.
    autostarted = MappingProxyType(
        {
            'docker': ('docker.service', 'docker.socket'),
            'caddy': ('caddy.service',),
            'samba': (SAMBA_UNIT, 'nmb.service'),
            'tailscale': (TAILSCALE_UNIT,),
        }
    )
    # Fedora's own container packages that would conflict with Docker's.
    conflicts = ('moby-engine', 'podman-docker', 'containerd')

    def __init__(self, run=run_command, release=None, fetch=_fetch):
        super().__init__(run, release)
        self.fetch = fetch

    def has(self, package):
        return self._call(['rpm', '-q', '--quiet', package], 30).returncode == 0

    def available(self, wanted):
        if not self.ready():
            return False
        for name in self.names(wanted):
            found = self.retrying(
                lambda name=name: self._call(
                    ['dnf', '-q', 'repoquery', '--available', '--latest-limit', '1', '--qf', '%{name}\n', name], 600
                )
            )
            if found.returncode or name not in found.stdout.split():
                return False
        return True

    def _install(self, names, timeout):
        return self._call(['dnf', 'install', '-y', *names], timeout + 600)

    def repo_files(self, address):
        """Every dnf repository file that already uses this address."""
        found = []
        for path in sorted(self.path('/etc/yum.repos.d').glob('*.repo')):
            try:
                if address in path.read_text(errors='replace'):
                    found.append(path)
            except OSError:
                continue
        return found

    def release_number(self):
        """The release as the makers' repositories name it (Fedora: its version)."""
        return self.release.get('VERSION_ID', '')

    def check_release(self, repository):
        """The maker packages this release (a new one can take them a while)."""
        release = self.release_number()
        system = self.release.get('NAME') or self.label
        url = repository.release_url.format(release=release)
        try:
            self.download(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(
                    f'{repository.maker} does not offer packages for {system} {release} yet ({url} '
                    f'was not found). Wait for {repository.maker} to add it, or use a release '
                    'it supports, then run setup again'
                ) from None
            raise RuntimeError(answered_badly(repository.maker, url, exc.code, self.download_tries)) from None
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError(
                unreachable(
                    repository.maker, url, f'the list of {repository.maker}\'s packages', _why(exc), self.download_tries
                )
            ) from None

    def add_repository(self, repository):
        """Make a maker's repository usable. Someone else's definition of it is used as it
        is (two would make dnf warn about duplicates); NekoServer's own is kept current."""
        listing = self.path('/etc/yum.repos.d/nekoserver-' + repository.name + '.repo')
        found = self.repo_files(repository.address)
        ours = listing in found and listing.read_text(errors='replace').startswith(RpmRepository.MARKER)
        if ours and len(found) > 1:
            listing.unlink()  # someone else defines it too: theirs is used
            return False
        if found and not ours:
            return False
        desired = RpmRepository.MARKER + repository.text
        if ours and listing.read_text(errors='replace') == desired:
            return False
        if repository.release_url:
            self.check_release(repository)
        atomic_write(listing, desired.encode(), 0o644)
        return True

    def prepare(self, missing):
        if {'docker', 'docker-compose'} & set(missing):
            found = [name for name in self.conflicts if self.has(name)]
            if found:
                raise RuntimeError(
                    'This system has its own container packages installed ('
                    + ', '.join(found)
                    + '). NekoServer uses Docker\'s official packages and does not mix or replace '
                    'them. Remove them yourself if nothing else uses them (sudo dnf remove '
                    + ' '.join(found)
                    + '), then run setup again.'
                )
        for repository in dict.fromkeys(self.repositories[name] for name in missing if name in self.repositories):
            self.add_repository(repository)
        units = [unit for name in missing for unit in self.autostarted.get(name, ())]
        existing = self.existing_units(units)
        return {
            'samba_config_existed': self.path(SAMBA_CONFIG).exists(),
            'units_before': existing,
            'new_units': [unit for unit in units if unit not in existing],
        }

    def protect(self, before):
        """The new services are masked while their packages install (a preset or
        scriptlet can't enable or start them) and until settle() has switched them off."""
        new = before.get('new_units', [])
        if new:
            result = self._call(['systemctl', 'mask', '--quiet', *new], 60)
            if result.returncode:
                raise RuntimeError('Could not keep ' + ', '.join(new) + ' from starting: ' + _reason(result))

    def settle(self, packages, before):
        self.set_aside_samba_example(packages, before)
        existing = before.get('units_before', [])
        new = [unit for name in packages for unit in self.autostarted.get(name, ()) if unit not in existing]
        self.unmask([unit for unit in new if unit in before.get('new_units', [])])
        try:
            self.switch_off(new)
        except RuntimeError:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._call(['systemctl', 'mask', '--quiet', *new], 60)  # stay protected until next time
            raise

    def packaged_example(self, absolute):
        """This settings file is still exactly what its package wrote: rpm's record of the
        file (size, checksum, owner, time) shows no change. Without a record: not assumed."""
        if not self.path(absolute).is_file():
            return False
        owner = self._call(['rpm', '-qf', '--qf', '%{NAME}\n', absolute], 30)
        if owner.returncode or not owner.stdout.split():
            return False
        check = self._call(['rpm', '-V', owner.stdout.split()[0]], 120)
        return not any(line.split()[-1:] == [absolute] for line in check.stdout.splitlines())


class Rhel(Fedora):
    """RHEL and its rebuilds (Rocky Linux, AlmaLinux, CentOS Stream), version 10 and newer:
    Fedora's way of working with dnf, other makers' repositories for Docker and
    Tailscale, and EPEL (Fedora's extra packages for them) for Caddy and qrencode."""

    name = 'rhel'
    label = 'RHEL / Rocky Linux / AlmaLinux'
    ids = ('rhel', 'rocky', 'almalinux', 'centos')
    like = ('rhel',)
    # Docker's own instructions for RHEL remove runc (RHEL's or EPEL's) and podman-docker:
    # they clash with Docker's containerd.io and its docker command.
    conflicts = ('moby-engine', 'podman-docker', 'containerd', 'runc')
    epel = frozenset({'caddy', 'qrencode'})
    EPEL_ADDRESS = 'repo=epel-'  # in every EPEL repository definition (its metalink)

    def __init__(self, run=run_command, release=None, fetch=_fetch):
        super().__init__(run, release, fetch)
        # Docker packages RHEL itself under rhel/ and its rebuilds under centos/; Tailscale
        # packages them all under rhel/<major version>.
        docker = 'rhel' if self.release.get('ID') == 'rhel' else 'centos'
        major = self.release_number()
        docker_rpm = RpmRepository(
            'docker-ce',
            'Docker',
            f'https://download.docker.com/linux/{docker}/',
            '[docker-ce-stable]\nname=Docker CE Stable - $basearch\n'
            f'baseurl=https://download.docker.com/linux/{docker}/$releasever/$basearch/stable\n'
            f'enabled=1\ngpgcheck=1\ngpgkey=https://download.docker.com/linux/{docker}/gpg\n',
            release_url=f'https://download.docker.com/linux/{docker}/{{release}}/x86_64/stable/repodata/repomd.xml',
        )
        tailscale_rpm = RpmRepository(
            'tailscale',
            'Tailscale',
            f'https://pkgs.tailscale.com/stable/rhel/{major}/',
            f'[tailscale-stable]\nname=Tailscale stable\nbaseurl=https://pkgs.tailscale.com/stable/rhel/{major}/$basearch\n'
            f'enabled=1\ntype=rpm\nrepo_gpgcheck=1\ngpgcheck=1\ngpgkey=https://pkgs.tailscale.com/stable/rhel/{major}/repo.gpg\n',
            release_url='https://pkgs.tailscale.com/stable/rhel/{release}/x86_64/repodata/repomd.xml',
        )
        self.repositories = MappingProxyType(
            {'docker': docker_rpm, 'docker-compose': docker_rpm, 'tailscale': tailscale_rpm}
        )

    def release_number(self):
        # "10.2" is release 10 to Docker and Tailscale (dnf's $releasever is "10" too).
        return self.release.get('VERSION_ID', '').split('.')[0]

    def epel_ready(self):
        return self.has('epel-release') or bool(self.repo_files(self.EPEL_ADDRESS))

    def add_epel(self):
        """EPEL's release package: from the system's own repositories on Rocky, AlmaLinux
        and CentOS Stream, from EPEL's site on RHEL itself. dnf then checks EPEL's packages
        with EPEL's key, which that package brings."""
        if self.epel_ready():
            return False
        source = (
            ('https://dl.fedoraproject.org/pub/epel/epel-release-latest-' + self.release_number() + '.noarch.rpm')
            if self.release.get('ID') == 'rhel'
            else 'epel-release'
        )
        result = self._call(['dnf', 'install', '-y', source], 900)
        if result.returncode:
            raise RuntimeError('Adding EPEL, where Caddy and qrencode come from, failed: ' + _reason(result))
        return True

    def available(self, wanted):
        if self.ready() and set(self.names(wanted)) & self.epel:
            self.add_epel()
        return super().available(wanted)

    def prepare(self, missing):
        if self.epel & {real for name in missing for real in self.names(name)}:
            self.add_epel()
        return super().prepare(missing)


# zypper's exit codes for "done, but": a repository that did not answer was skipped for
# this run (ZYPPER_EXIT_INF_REPOS_SKIPPED), or a package's install script failed though the
# package is installed (ZYPPER_EXIT_INF_RPM_SCRIPT_FAILED). How zypper names the repository.
ZYPPER_REPOS_SKIPPED, ZYPPER_SCRIPT_FAILED = 106, 107
SKIPPED_REPOSITORY = re.compile(r"(?:Skipping repository|Problem retrieving files from) '([^']+)'")


class Suse(Fedora):
    """openSUSE Leap 16 and newer, Tumbleweed and Slowroll: rpm as on Fedora, zypper for
    the packages, and everything from openSUSE's own repositories (Docker, Compose, Caddy
    and Tailscale included), as on Arch. SLES names only suse, has other repositories and
    a subscription: not covered."""

    name = 'suse'
    label = 'openSUSE'
    manager = 'zypper'
    ids = ('opensuse-leap', 'opensuse-tumbleweed', 'opensuse-slowroll', 'opensuse')
    like = ('opensuse',)
    packages = MappingProxyType(
        {
            'docker': ('docker', 'docker-buildx'),
            'docker-compose': 'docker-compose',
            'libva-mesa-driver': 'Mesa-libva',
            # The TV (Tumbleweed and Slowroll, from the non-OSS repository; Leap 16 has no
            # Steam package).
            'vulkan-intel': 'libvulkan_intel',
            'lib32-vulkan-intel': 'libvulkan_intel-32bit',
            'vulkan-radeon': 'libvulkan_radeon',
            'lib32-vulkan-radeon': 'libvulkan_radeon-32bit',
            'firefox': 'MozillaFirefox',
            'bluez-utils': 'bluez',
            # Lighting: openSUSE spells the package with capitals (Tumbleweed has it).
            'openrgb': 'OpenRGB',
            # Building the runtime: Rust comes as two packages here.
            'rust': ('rust', 'cargo'),
        }
    )
    settings = ('/etc/zypp/zypp.conf', '/etc/zypp/repos.d')
    repositories = MappingProxyType({})
    conflicts = ()
    # zypper waits this long (seconds) for another zypper or YaST to finish, instead of
    # failing at once ("System management is locked").
    LOCK_WAIT = '600'

    def _call(self, argv, timeout, **extra):
        if argv[0] == 'zypper':
            extra.setdefault('env', {**environment(), 'ZYPP_LOCK_TIMEOUT': self.LOCK_WAIT})
        return super()._call(argv, timeout, **extra)

    def available(self, wanted):
        if not self.ready():
            return False
        # zypper answers 104 when no package matches. Its search ignores case unless told
        # otherwise, and the install does not: "openrgb" would be found and "OpenRGB" installed.
        return all(
            self._found(
                name,
                self.retrying(
                    lambda name=name: self._call(
                        [
                            'zypper',
                            '--non-interactive',
                            '--quiet',
                            'search',
                            '--match-exact',
                            '--case-sensitive',
                            '--type',
                            'package',
                            name,
                        ],
                        300,
                    )
                ),
            )
            for name in self.names(wanted)
        )

    @staticmethod
    def _found(name, result):
        """The search found this package; also when a repository that did not answer was
        skipped (106), as long as the others list it."""
        if result.returncode == ZYPPER_REPOS_SKIPPED:
            return re.search(r'\|\s*' + re.escape(name) + r'\s*\|', result.stdout or '') is not None
        return result.returncode == 0

    def _install(self, names, timeout):
        return self._done_but(
            self._call(['zypper', '--non-interactive', 'install', '--no-recommends', *names], timeout + 600), names
        )

    def _done_but(self, result, names):
        """zypper's "done, but" exit codes (its manual): 106 when it skipped a repository
        that did not answer and installed from the others, 107 when a package's install
        script reported an error though the package is installed. Both are a success once
        rpm confirms every package (said with the reason); any other code stays a failure."""
        if result.returncode not in (ZYPPER_REPOS_SKIPPED, ZYPPER_SCRIPT_FAILED) or not all(
            self.has(name) for name in names
        ):
            return result
        if result.returncode == ZYPPER_REPOS_SKIPPED:
            skipped = sorted(set(SKIPPED_REPOSITORY.findall(_output(result))))
            self.say(
                'zypper could not reach the repository '
                + (', '.join("'" + name + "'" for name in skipped) or '(zypper did not say which)')
                + ' and skipped it; the packages came from the other repositories. If it stays away, turn it off with  '
                'sudo zypper modifyrepo --disable <its name>  and turn it back on later.'
            )
        else:
            self.say(
                'The packages were installed, but an install script of one of them reported an error: '
                + _reason(result)
            )
        return subprocess.CompletedProcess(result.args, 0, result.stdout, result.stderr)


FAMILIES = (Arch, Debian, Fedora, Rhel, Suse)


def detect(path=OS_RELEASE):
    """The family class for this system, or None when NekoServer has no adapter for it."""
    release = os_release(path)
    return next((family for family in FAMILIES if family.matches(release)), None)


def current(run=run_command, path=OS_RELEASE):
    """This system's family, ready to use. Raises Unsupported on other systems."""
    family = detect(path)
    release = os_release(path)
    if family is None:
        named = release.get('PRETTY_NAME') or release.get('ID') or 'this system'
        raise Unsupported(
            'Automatic setup supports '
            + ' and '.join(f.label for f in FAMILIES)
            + '; '
            + named
            + ' is not supported yet'
        )
    return family(run, release)


def supported(path=OS_RELEASE):
    return detect(path) is not None


def family_or_default(path=OS_RELEASE):
    """For reading only (names of services and settings files): this system's family, or
    Arch when it has none, which was NekoServer's only family before 0.28.0."""
    return detect(path) or Arch


def service(wanted, path=OS_RELEASE):
    return family_or_default(path).service(wanted)


def bootstrap(family=None, log=print):
    """Install every system package the apps need, before setup starts."""
    family = family or current()
    family.repair(log)
    log('Installing the packages the apps need: ' + ', '.join(APP_PACKAGES))
    family.install(APP_PACKAGES)


# What building the runtime on this PC needs (runtime/build.py): Rust with cargo, and
# the C compiler cargo links with. Cargo.toml's rust-version is the oldest Rust that
# builds it; a family whose Rust is older offers a newer one: Ubuntu 24.04 beside the old
# one under a versioned name (rustc-1.85, cargo-1.85), Debian 12 in place of it as
# rustc-web and cargo-web (Debian's newer Rust, the same rustc and cargo commands).
DEBIAN_NEWER_RUST = ('rustc-web', 'cargo-web')
TOOLCHAIN_PACKAGES = ('rust', 'gcc')
RUST_VERSION = re.compile(r'^rust-version\s*=\s*"(\d+)\.(\d+)', re.MULTILINE)


def oldest_rust(cargo_toml=None):
    """(major, minor) of the oldest Rust that builds the runtime, from Cargo.toml."""
    text = Path(cargo_toml or Path(__file__).resolve().parent / 'runtime/Cargo.toml').read_text()
    match = RUST_VERSION.search(text)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def rustc_version(run=subprocess.run):
    """(major, minor) of the rustc on PATH, or None when there is none."""
    try:
        result = run(['rustc', '--version'], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'rustc (\d+)\.(\d+)', result.stdout or '')
    return (int(match[1]), int(match[2])) if match else None


def toolchain(family=None, log=print, oldest=None, version=rustc_version, which=shutil.which):
    """Install what building the runtime needs: Rust (cargo, rustc) and gcc, and on a
    family whose plain Rust is older than Cargo.toml allows, the versioned newer one
    (which build.py then finds by its versioned name)."""
    family = family or current()
    oldest = oldest or oldest_rust()
    label = '%d.%d' % oldest
    log('Installing what building the runtime needs: Rust and a C compiler')
    found = version()
    if found is not None and found >= oldest:
        # A new enough Rust is already there (Debian 12's rustc-web, rustup, one set up by
        # hand): it stays as it is, and only what is missing beside it is installed. Asking
        # for the plain package then would make Debian 12's apt replace rustc-web with its
        # older rustc, and every later run would fail at the build.
        family.install(['gcc', *missing_cargo(family, which)])
        return
    if debian_needs_newer_rust(family, oldest, version):
        return debian_newer_rust(family, oldest, label, log, version)
    family.install(TOOLCHAIN_PACKAGES)
    found = version()
    if found is not None and found >= oldest:
        return
    if family.name != 'debian':
        raise RuntimeError(
            'Rust '
            + label
            + ' or newer is needed to build the runtime; this system has '
            + ('%d.%d' % found if found else 'none')
            + '. Update it, or install a newer Rust and run setup again.'
        )
    # Ubuntu keeps the plain rustc at the release's version and newer ones beside it.
    log('This system\'s Rust is older than ' + label + ': installing rustc-' + label + ' and cargo-' + label)
    family.install(['rustc-' + label, 'cargo-' + label])


def missing_cargo(family, which=shutil.which):
    """What gives a new enough rustc its cargo, when there is no cargo on PATH yet: on
    Debian beside rustc-web its own cargo-web (the plain cargo conflicts with it), else the
    family's Rust package."""
    if which('cargo'):
        return []
    if family.name == 'debian' and not family.ubuntu() and family.has(DEBIAN_NEWER_RUST[0]):
        return [DEBIAN_NEWER_RUST[1]]
    return ['rust']


def debian_needs_newer_rust(family, oldest, version=rustc_version):
    """Debian itself (not Ubuntu), whose plain rustc is older than the runtime needs (Debian
    12 has 1.63): its newer Rust, rustc-web, is the way. Nothing to do when a new enough
    Rust is already there."""
    if family.name != 'debian' or family.ubuntu():
        return False
    found = version()
    if found is not None and found >= oldest:
        return False
    plain = family.candidate('rustc')
    return plain is None or plain < oldest


def debian_newer_rust(family, oldest, label, log, version=rustc_version):
    """rustc-web and cargo-web in place of the older rustc and cargo. They take over the same
    commands, so an older rustc already installed (perhaps for something else) is never
    replaced without you: setup says how instead."""
    newer = family.candidate(DEBIAN_NEWER_RUST[0])
    if newer is None or newer < oldest:
        raise RuntimeError(
            'Rust '
            + label
            + ' or newer is needed to build the runtime, and this Debian offers none (rustc-web '
            + (f'{newer[0]}.{newer[1]}' if newer else 'is not available')
            + '). Make sure the "main" and security '
            'repositories are enabled in /etc/apt/sources.list, or use Debian 13 or newer.'
        )
    found = version()
    if found is not None or family.has('rustc') or family.has('cargo'):
        raise RuntimeError(
            'This system\'s Rust ('
            + (f'{found[0]}.{found[1]}' if found else 'rustc')
            + ') is older than '
            + label
            + ', which building the runtime needs. Debian\'s newer Rust takes its place: run  sudo apt install '
            'rustc-web cargo-web  (apt replaces the older rustc and cargo with them), then  sudo bash install.sh  again.'
        )
    log(
        'Debian\'s own Rust is older than '
        + label
        + ': installing rustc-web and cargo-web (Debian\'s newer Rust, the same commands)'
    )
    family.install(['gcc', *DEBIAN_NEWER_RUST])
    found = version()
    if found is None or found < oldest:
        raise RuntimeError(
            'rustc-web was installed, but rustc still reports '
            + (f'{found[0]}.{found[1]}' if found else 'nothing')
            + ' (Rust '
            + label
            + ' is needed); check  rustc --version  and run setup again'
        )


def main(argv):
    commands = {'bootstrap': bootstrap, 'repair': lambda: current().repair(), 'toolchain': toolchain}
    if len(argv) != 1 or argv[0] not in commands:
        print('usage: python3 host.py bootstrap|repair|toolchain', file=sys.stderr)
        return 2
    before = _stop_on_hangup()
    try:
        commands[argv[0]]()
    except KeyboardInterrupt:
        with contextlib.suppress(OSError):  # the terminal may be gone
            print(
                '\nStopped. Packages already installed stay installed (switched off). Run  sudo bash install.sh  '
                'again when you are ready.',
                file=sys.stderr,
            )
        return 130
    except subprocess.TimeoutExpired as exc:
        stopped(
            str(exc.cmd[0])
            + ' did not finish within '
            + str(int(exc.timeout) // 60)
            + ' minutes; check the network and run  sudo bash install.sh  again'
        )
        return 1
    except (RuntimeError, OSError, http.client.HTTPException) as exc:
        stopped(str(exc))
        return 1
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)
    return 0


def stopped(message, note=None):
    """Why setup stopped, on the terminal and (for install.sh to show in the setup window)
    in the file NEKOSERVER_STOP_NOTE names: one install.sh made for it (mktemp), so it
    is only ever written, never created, and never through a link."""
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.flush()  # the retry lines before it, also when both go to one file
    print('ERROR: ' + message, file=sys.stderr)
    note = os.environ.get('NEKOSERVER_STOP_NOTE', '') if note is None else note
    if note.startswith('/') and '..' not in note.split('/'):
        try:
            handle = os.open(note, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return
        with os.fdopen(handle, 'w', encoding='utf-8') as stream:
            stream.write(message + '\n')


HANGUPS = (signal.SIGHUP, signal.SIGTERM)


def _stop_on_hangup():
    """A closed terminal or SSH connection (SIGHUP), or the system shutting down
    (SIGTERM), stops host.py as Ctrl+C does, so the cleanup in its finally blocks still
    runs: start protection (policy-rc.d, the masks) lifted once nothing is left to tidy.
    Only the first one: a second does not cut that cleanup short. Returns the handlers
    there were before."""
    stopping = []

    def stop(number, frame):
        if not stopping:
            stopping.append(number)
            raise KeyboardInterrupt

    return {number: signal.signal(number, stop) for number in HANGUPS}


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
