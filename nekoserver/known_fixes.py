"""The failures setup knows a fix for. A step that did not finish says why (its own
reason); when the reason is one of the kinds below, setup shows the fix under it, in the
terminal and in the setup window, with "Retry this step".

Each entry is recognised by words the failing program itself says (apt, pacman, dnf,
zypper, Docker, curl) or by NekoServer's own reason for it, and its fix is a few steps a
person can do, in the order to try them. Nothing here runs a command by itself: the
only action offered is trying the step again. The dashboard shows the same fix for a
step that still waits (its Needs attention), with the command to run setup again; it
never runs setup from a web page.

Adding an entry: its words are checked against real reasons in tests/test_known_fixes.py.
The first entry that matches is the one shown, so the order below matters where a
reason has two kinds of words in it (a site not reached because the connection timed
out is "could not reach a site")."""

import re

RETRY = 'Then press Retry this step (or answer yes to "Retry this step now?" in the terminal).'

# How a person finds the drive in setup's list (drive_table.py): by its name and size on
# its row, and, for two that look alike, by the ID behind its Details (drive_table.details:
# "UUID <id>"), the one the message above names.
DRIVE_LIST_LOOK = (
    'Run setup again and look for the drive in its list by its name and size. If two look alike, open '
    'Details under each and compare the UUID there with the one the message above names.'
)


FIXES = (
    {
        'id': 'no_internet',
        'matches': (
            r'Temporary failure in name resolution',
            r'Could not resolve host',
            r'Name or service not known',
            r'Network is unreachable',
            r'No route to host',
            r'Could not reach \S+ \(',
        ),
        'title': 'This PC could not reach a site it downloads from',
        'steps': (
            'Open any web page in a browser on this PC. If none opens, the PC is offline: check its network cable '
            'or Wi-Fi, and that the router is on.',
            'If other pages open but the site named above does not, something on your network blocks it: a '
            'router or parental filter, an ad-blocker such as Pi-hole, a proxy or a VPN. Allow that site there, or '
            'switch the VPN off for now.',
            RETRY,
        ),
    },
    {
        'id': 'package_lock',
        'matches': (
            r'Could not get lock',
            r'is another process using it',
            r'unable to lock database',
            r'System management is locked',
            r'is still locked',
            r'holding the (yum|dnf) lock',
            r'Waiting for process with pid \d+ to finish',
        ),
        'title': 'Another program is installing or updating packages right now',
        'steps': (
            'Linux installs packages for one program at a time. Most often it is the desktop\'s own updater '
            '(Software, Discover, automatic updates) working in the background.',
            'Close the software app if it is open, and give it a few minutes to finish. Do not delete lock files '
            'by hand: that can break a half-installed package.',
            RETRY,
        ),
    },
    {
        'id': 'docker_down',
        'matches': (
            r'Cannot connect to the Docker daemon',
            r'Is the docker daemon running',
            r'Docker is not running',
            r'error during connect',
        ),
        'title': 'Docker, which runs the apps, is not running',
        'steps': (
            'In a second terminal (Ctrl+Alt+T), start it:  sudo systemctl start docker',
            'If that fails, see why:  sudo journalctl -u docker -n 30 --no-pager  . Most often one of NekoServer\'s '
            'drives is not connected: Docker waits for the SSD and the media drive, so nothing is ever written '
            'to the wrong place. Connect the drive and start Docker again.',
            RETRY,
        ),
    },
    {
        'id': 'disk_full',
        'matches': (r'No space left on device', r'Disk quota exceeded', r'\bENOSPC\b'),
        'title': 'A drive is full',
        'steps': (
            'See which one, in a second terminal (Ctrl+Alt+T):  df -h  (the line at or near 100%).',
            'Make room on it. On the disk Linux runs from:  sudo journalctl --vacuum-size=200M  frees old logs, '
            'and the Downloads folder is the usual place for big files. On the SSD or the media drive: move or '
            'delete what you no longer need.',
            RETRY,
        ),
    },
    {
        'id': 'drive_missing',
        'matches': (
            r'\(UUID [^)]+\) is not connected',
            r'Drive UUID \S+ not found',
            r'Drive check failed: .* nothing is mounted there',
            r'External drive missing',
        ),
        'title': 'A drive NekoServer uses is not connected',
        'steps': (
            'Check the drive named above: its cable at both ends, and its power when it has its own (a USB case, '
            'a dock). A drive that just woke up can take half a minute to appear.',
            DRIVE_LIST_LOOK,
            'If you replaced that drive on purpose, do what the message above says to choose the new one.',
            RETRY,
        ),
    },
    {
        'id': 'download_timeout',
        'matches': (
            r'did not finish within \d+ (seconds|minutes)',
            r'timed out',
            r'TLS handshake timeout',
            r'context deadline exceeded',
            r'Connection reset by peer',
        ),
        'title': 'A download took too long or was cut off',
        'steps': (
            'This is nearly always a slow or busy connection, or a download site having a bad moment. What was '
            'already downloaded is kept.',
            'Wait a minute, pause other big downloads on your network if there are any, and try again. If it '
            'keeps happening, try at another time of day.',
            RETRY,
        ),
    },
    {
        'id': 'tailscale_certificate',
        'matches': (r'Tailscale has not got the HTTPS certificate',),
        'title': "Tailscale is still getting this server's HTTPS certificate",
        'steps': (
            'Nothing is wrong on this PC: the certificate comes from Let\'s Encrypt through Tailscale, and the '
            'first one can take a few minutes.',
            'If the message above names a time ("it allows the next one after ..."), no retry works before then: '
            'Let\'s Encrypt limits how many certificates one machine name gets in a week. Run setup again after '
            'that time, or give this server another name in Tailscale.',
            'Otherwise wait a few minutes. ' + RETRY,
        ),
    },
)


def fix_for(reason):
    """The known fix for a step's reason: {'id', 'title', 'steps'}, or None when the
    reason is not one setup knows a fix for."""
    text = str(reason or '')
    for fix in FIXES:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in fix['matches']):
            return {'id': fix['id'], 'title': fix['title'], 'steps': list(fix['steps'])}
    return None


def lines(fix):
    """A fix as the terminal shows it."""
    return ['  What usually fixes it: ' + fix['title'] + '.'] + [
        f'    {number}. {step}' for number, step in enumerate(fix['steps'], 1)
    ]
