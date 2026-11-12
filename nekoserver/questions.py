"""Every question NekoServer asks, in one place, in the words a person is asked it.

Each question has what the setup window shows (a short question as its title, one plain
line under it when that helps, and buttons or choices that say what they do) and what the
terminal asks: the same question in the same words, then one short line on how to answer.

The window never says "type", "Enter", "number" or "yes/no": a choice is a button, a
password is a password box, and something that can wait has a "Skip for now" button. The
terminal's last line says how to answer there.

A question is asked through `ask` (or `yes_no`, `confirm`): the code keeps reading the
answer as it always did ("yes", "2", an address), so nothing about what an answer does is
decided here. Only the words are.

The review page (plans/0.50.0-questions-review.html) and the golden file
tests/golden/questions.txt are made from this table, so they cannot drift from it.
"""

import contextlib

import setup_progress

# How to answer in the terminal, said under the question.
HOW = {
    'yesno': 'Type yes or no, then press Enter.',
    'choice': 'Type the number of your choice, then press Enter.',
    'text': 'Type it, then press Enter.',
    'secret': 'Type it, then press Enter. It is not shown while you type.',
}
OPTIONAL = ' Press Enter alone to skip this for now.'
SKIP = 'Skip for now'

# The questions. For each:
#   title   the question itself
#   hint    one plain line under it (optional)
#   kind    yesno, choice, text or secret
#   yes/no  what the two buttons say (yesno)
#   choices [(the answer the code reads, what the button says)] for a fixed choice
#   send    what the button that sends a typed answer says (text, secret)
#   skip    the answer "Skip for now" gives, when the question can wait ('' is Enter alone)
#   how     the terminal's line on how to answer, when the usual one does not fit
#   example values for the {names} in the words, for the review page
#   old     the question as 0.49.0 asked it, where it existed (for the review page)
#   above   the question is about what setup listed just before it: the window shows that list too
#   screen  where in setup it comes (the review page's order)
Q = {
    # ---- before setup starts
    'earlier_settings': {
        'screen': 'Starting setup',
        'title': 'This PC already has a NekoServer server. Keep its settings?',
        'hint': 'Choose "Keep them" when you are updating. "Start fresh" asks for every drive and password again.',
        'kind': 'yesno',
        'yes': 'Keep them',
        'no': 'Start fresh',
        'old': 'Use them for this release (yes when updating)? [yes/no]:',
    },
    'keep_fewer': {
        'screen': 'Starting setup',
        'title': 'Keep fewer old backups from now on?',
        'hint': 'This server keeps the newest {old} {what}. New servers keep {new}, which saves drive space.',
        'kind': 'yesno',
        'yes': 'Keep the newest {new}',
        'no': 'Keep {old} as now',
        'example': {'old': 30, 'new': 7, 'what': 'app backups'},
        'old': 'Your settings keep the newest 30 app backups, the default before v0.30.0 (now 7). '
        'Keep fewer from now on, the newest 7? [yes/no]:',
    },
    # ---- the optional questions of a rerun
    'ask_again': {
        'screen': 'Setup run again',
        'title': 'Change any of these answers?',
        'hint': 'Your answers from last time are listed above. "Keep them" goes straight on.',
        'kind': 'yesno',
        'above': True,
        'yes': 'Ask me again',
        'no': 'Keep them',
        'old': 'Ask these questions again? [yes/no]:',
    },
    # ---- the media apps
    'install_group': {
        'screen': 'What to install',
        'title': 'Install {group}?',
        'hint': '{about}',
        'kind': 'yesno',
        'yes': 'Install',
        'no': 'Leave it out',
        'example': {
            'group': 'Photos (Immich)',
            'about': 'Immich is your own photo backup and gallery. You can add it later.',
        },
        'old': 'Install the media apps too? [yes/no]:',
    },
    # ---- drives
    'drive_space': {
        'screen': 'Your drives',
        'title': 'One of your drives is small. Go on anyway?',
        'hint': 'The list above says which drive and how much room it has. It will fill up sooner.',
        'kind': 'yesno',
        'above': True,
        'yes': 'Go on',
        'no': 'Stop, I will change the drive',
        'old': 'Continue with these drives anyway? [yes/no]:',
    },
    # ---- a network storage box
    'nas_address': {
        'screen': 'A network storage box (NAS)',
        'title': 'What is the address of your storage box?',
        'hint': 'It is what you open in a browser to reach it, such as 192.168.1.20.',
        'kind': 'text',
        'send': 'Look there',
        'old': "The NAS's address on your network (such as 192.168.1.20):",
    },
    'nas_kind': {
        'screen': 'A network storage box (NAS)',
        'title': 'How should NekoServer use your storage box?',
        'hint': 'It offers both ways. If you are not sure, take the first.',
        'kind': 'choice',
        'choices': [
            ('1', 'As a disk of its own (only this PC can use it)'),
            ('2', 'As a shared folder (the files stay visible on the storage box)'),
        ],
        'how': 'Type 1 or 2, then press Enter. Enter alone takes the first.',
        'old': 'Which one does NekoServer use? [1/2] (Enter alone: 1):',
    },
    'nas_disks': {
        'screen': 'A network storage box (NAS)',
        'title': 'Which of these disks are for NekoServer?',
        'hint': 'The storage box offers the disks listed above. Choose only the ones you made for this server.',
        'kind': 'text',
        'above': True,
        'send': 'Use these',
        'how': 'Type their numbers with commas between them (such as 1,2), then press Enter. Enter alone stops setup here.',
        'old': 'Numbers of the ones NekoServer uses, separated by commas (Enter alone stops here):',
    },
    'nas_login_needed': {
        'screen': 'A network storage box (NAS)',
        'title': 'Does your storage box ask for a user name and password for this disk?',
        'hint': 'Most do not. If you set one up on the storage box (it calls it CHAP), choose yes. The disk is {iqn}.',
        'kind': 'yesno',
        'yes': 'Yes, it has a password',
        'no': 'No password',
        'how': 'Type yes or no, then press Enter. Enter alone means no.',
        'example': {'iqn': 'iqn.2000-01.com.synology:nas.Target-1'},
        'old': 'Does the NAS ask for a CHAP user name and password for iqn.2000-01.com.synology:nas.Target-1? [y/N]:',
    },
    'nas_login_user': {
        'screen': 'A network storage box (NAS)',
        'title': 'What is the user name for that disk?',
        'kind': 'text',
        'send': 'Next',
        'old': 'CHAP user name:',
    },
    'nas_folder': {
        'screen': 'A network storage box (NAS)',
        'title': 'Which shared folder is for {words}?',
        'hint': 'The folders the storage box shares are listed above.',
        'kind': 'text',
        'above': True,
        'send': 'Use this folder',
        'skip': '',
        'skip_label': 'Use a drive on this PC instead',
        'how': 'Type its number or its name, then press Enter. Enter alone uses a drive on this PC instead.',
        'example': {'words': 'your films, shows and backups'},
        'old': 'The shared folder for the media drive: its number (1 to 3) or its name, or Enter alone for a '
        'drive on this PC:',
    },
    # ---- logins for the apps
    'logins_who': {
        'screen': 'Logins for your apps',
        'title': 'How would you like to set up your app accounts?',
        'hint': 'Each selected app has a separate account. NekoServer stores its required credentials privately so '
        'it can connect and check the apps.',
        'kind': 'choice',
        'choices': [('1', 'Create strong passwords for me (recommended)'), ('2', 'Choose my own logins')],
        'how': 'Type 1 or 2, then press Enter.',
        'old': 'Choose 1 or 2:',
    },
    'login_name': {
        'screen': 'Logins for your apps',
        'title': 'What user name do you want for {app}?',
        'kind': 'text',
        'send': 'Next',
        'example': {'app': 'Jellyfin'},
        'old': 'Username:',
    },
    'login_email': {
        'screen': 'Logins for your apps',
        'title': 'What email address do you want to sign in to {app} with?',
        'hint': 'This identifies your account. Email delivery and password recovery depend on how {app} is configured.',
        'kind': 'text',
        'send': 'Next',
        'example': {'app': 'Immich'},
        'old': 'Email login:',
    },
    'login_retry': {
        'screen': 'Logins for your apps',
        'title': '{app} did not take the saved login. What now?',
        'hint': 'If you changed the password in {app} yourself, tell NekoServer the new one.',
        'kind': 'choice',
        'choices': [('', 'Try the saved login again'), ('c', 'I changed it: here is the new one')],
        'how': 'Press Enter alone to try again, or type c and press Enter to enter the new login.',
        'example': {'app': 'Jellyfin'},
        'old': 'ENTER to retry saved login, or c to enter the current login:',
    },
    'app_signed_in': {
        'screen': 'Logins for your apps',
        'title': 'Could you sign in to {app}?',
        'hint': '{app} opened in your browser. "Show login" gives you its user name and password.',
        'kind': 'yesno',
        'yes': 'Yes, I am in',
        'no': 'Not yet',
        'example': {'app': 'Jellyfin'},
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    # ---- Tailscale
    'tailscale_connected': {
        'screen': 'Reaching the server from your phone',
        'title': 'Is this server connected to Tailscale?',
        'hint': 'The Tailscale page in your browser says so once you have signed in and pressed Connect.',
        'kind': 'yesno',
        'yes': 'Yes, it is connected',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'tailscale_https': {
        'screen': 'Reaching the server from your phone',
        'title': 'Did you switch on both settings in Tailscale?',
        'hint': 'On the page that opened, "MagicDNS" and "HTTPS Certificates" must both be on. They give each app a safe address.',
        'kind': 'yesno',
        'yes': 'Yes, both are on',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'phone_ready': {
        'screen': 'Reaching the server from your phone',
        'title': 'Is your phone set up?',
        'hint': 'Yes when the apps open on the phone, also with Wi-Fi off.',
        'kind': 'yesno',
        'yes': 'Yes, it works',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]: (asked after each of up to eight steps)',
    },
    # ---- connecting the media apps
    'apps_finished': {
        'screen': 'Connecting your apps',
        'title': 'Did you finish what was left in the apps?',
        'hint': 'The list above says what each app still needs.',
        'kind': 'yesno',
        'above': True,
        'yes': 'Yes, done',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'played': {
        'screen': 'Connecting your apps',
        'title': 'Did a film or show play in Jellyfin?',
        'hint': 'Add something you are allowed to watch, wait for it to arrive, and press play. You can also do this later.',
        'kind': 'yesno',
        'yes': 'Yes, it played',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'source_filter': {
        'screen': 'Connecting your apps',
        'title': 'Looking for a download source by name?',
        'hint': 'A few letters of its name shorten the list.',
        'kind': 'text',
        'send': 'Show the list',
        'skip': '',
        'skip_label': 'Show them all',
        'how': 'Type part of a name and press Enter. Enter alone shows them all.',
        'old': 'Filter catalog by name (blank for all):',
    },
    'source_account_ready': {
        'screen': 'Connecting your apps',
        'title': 'Do you have your account at {source} ready?',
        'hint': '{source} needs an account of its own. Have its user name and password, or its key, at hand.',
        'kind': 'yesno',
        'yes': 'Yes, I have it',
        'no': 'Not yet',
        'example': {'source': 'ExampleTracker'},
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'source_saved': {
        'screen': 'Connecting your apps',
        'title': 'Is {source} saved in Prowlarr now?',
        'hint': 'Prowlarr opened in your browser. Fill in what it asks for {source}, press Test, then Save.',
        'kind': 'yesno',
        'yes': 'Yes, it is saved',
        'no': 'Not yet',
        'example': {'source': 'ExampleTracker'},
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'qbit_user': {
        'screen': 'Connecting your apps',
        'title': 'What is the user name of your download app (qBittorrent)?',
        'hint': 'It is "admin" unless you changed it.',
        'kind': 'text',
        'send': 'Next',
        'skip': '',
        'skip_label': 'It is admin',
        'how': 'Type it and press Enter. Enter alone means admin.',
        'old': 'qBittorrent username [admin]:',
    },
    'qbit_password': {
        'screen': 'Connecting your apps',
        'title': 'What is the password of your download app (qBittorrent)?',
        'hint': 'It is used once to connect the apps and is not kept.',
        'kind': 'secret',
        'send': 'Connect',
        'old': 'qBittorrent WebUI password (not saved by this connector):',
    },
    # ---- alerts
    'alerts_phone_ready': {
        'screen': 'Alerts on your phone',
        'title': 'Is the alerts app set up on your phone?',
        'hint': 'Install "ntfy" on the phone and follow a topic there. Keep its address at hand for the next question.',
        'kind': 'yesno',
        'yes': 'Yes, it is',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'alerts_address': {
        'screen': 'Alerts on your phone',
        'title': 'Paste the address of your alerts topic',
        'hint': 'It starts with https:// and ends with the name you chose in the ntfy app. You can add it later.',
        'kind': 'secret',
        'send': 'Save',
        'skip': '',
        'old': 'Private ntfy topic URL (blank defers):',
    },
    'alerts_token': {
        'screen': 'Alerts on your phone',
        'title': 'Does your alerts topic have an access token? Paste it here',
        'hint': 'Only private topics have one. If yours has none, skip this.',
        'kind': 'secret',
        'send': 'Save',
        'skip': '',
        'skip_label': 'It has none',
        'old': 'ntfy access token (blank for no token):',
    },
    'alerts_switch': {
        'screen': 'Alerts on your phone',
        'title': 'Send alerts to the new topic?',
        'hint': 'Your settings name another alerts topic than the one alerts go to now.',
        'kind': 'yesno',
        'yes': 'Use the new one',
        'no': 'Keep the old one',
        'old': 'Alerts go to another ntfy topic now. Send them to the topic in my-config.json '
        '(notifications.endpoint) instead? [yes/no]:',
    },
    # ---- the outage monitor
    'monitor_ready': {
        'screen': 'A warning when the server is off',
        'title': 'Did you make a check at your monitoring service?',
        'hint': 'A service outside your home (such as Healthchecks.io) warns you when this server stops answering. '
        'Make a check there and copy the address it gives you.',
        'kind': 'yesno',
        'yes': 'Yes, I copied the address',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    'monitor_address': {
        'screen': 'A warning when the server is off',
        'title': 'Paste the address your monitor gave you',
        'hint': 'It starts with https://. You can add it later from setup.',
        'kind': 'secret',
        'send': 'Save',
        'skip': '',
        'old': 'Private monitor ping URL (blank defers):',
    },
    # ---- the assistant
    'assistant_model': {
        'screen': 'Ask Neko, the AI assistant',
        'title': 'Which AI assistant do you want?',
        'hint': 'Bigger ones answer better and use more memory. The list above says what each needs.',
        'kind': 'choice',
        'how': 'Type its number, then press Enter. Enter alone keeps {default}.',
        'example': {'default': 'no assistant'},
        'old': 'Which one? (0-2, Enter for no assistant):',
    },
    # ---- the TV
    'tv_worked': {
        'screen': 'A TV connected to this PC',
        'title': 'Did everything work on the TV?',
        'hint': 'Steam, the controller, and the picture and sound.',
        'kind': 'yesno',
        'yes': 'Yes, all of it',
        'no': 'Not yet',
        'old': 'Have you completed this step successfully? [yes/no]: (asked after each of three steps)',
    },
    # ---- the first checkup's tests
    'test_passed': {
        'screen': 'Checking the finished server',
        'title': 'Did this test work: {name}?',
        'hint': 'Say yes only if you saw it work yourself.',
        'kind': 'yesno',
        'yes': 'Yes, I saw it work',
        'no': 'Not yet',
        'example': {'name': 'A photo taken on the phone appears in Immich'},
        'old': 'Have you completed this step successfully? [yes/no]:',
    },
    # ---- recovery
    'restore_rest': {
        'screen': 'Recovery',
        'title': 'Restore everything else from this backup now?',
        'hint': '"Stop" leaves the server as it is: nothing is restored.',
        'kind': 'yesno',
        'yes': 'Restore it',
        'no': 'Stop',
        'old': 'Restore everything else from this backup now? ("no" stops, with nothing restored) [yes/no]:',
    },
    'restore_newest': {
        'screen': 'Recovery',
        'title': 'Bring back only the newest version of each file?',
        'hint': 'That is what most people want. The other choice also copies every older version, which takes longer and more room.',
        'kind': 'yesno',
        'yes': 'Only the newest (recommended)',
        'no': 'Every version',
        'old': 'Restore only the NEWEST version of each file? (recommended; "no" copies every version) [yes/no]:',
    },
    'restore_app_versions': {
        'screen': 'Recovery',
        'title': 'Is this a new server?',
        'hint': 'On a new server the apps themselves are put back from the backup too, so they are the versions your data was made with.',
        'kind': 'yesno',
        'yes': 'Yes, put the apps back too',
        'no': 'No, the apps are here',
        'old': 'Also load the saved app versions into Docker (needed on a new server)? [yes/no]:',
    },
    'restore_folder_ok': {
        'screen': 'Recovery',
        'title': 'Put the restored files in this folder?',
        'kind': 'yesno',
        'yes': 'Use this folder',
        'no': 'Choose another',
        'old': 'Use this folder? [yes/no]:',
    },
    'check_drive': {
        'screen': 'Recovery',
        'title': 'Stop the apps and check the drive now?',
        'hint': 'The apps are off while the drive is checked, and start again afterwards.',
        'kind': 'yesno',
        'yes': 'Check it now',
        'no': 'Not now',
        'old': 'Stop the apps and check the drive now? [yes/no]:',
    },
    'repair_drive': {
        'screen': 'Recovery',
        'title': 'Repair the drive now?',
        'hint': 'The check found damage. A repair rewrites parts of the filesystem on the drive, and a file that was '
        'damaged can be lost. Copy what you still can first.',
        'kind': 'yesno',
        'yes': 'Repair it',
        'no': 'Leave it',
        'old': 'Check and repair its filesystem now (e2fsck: it changes only what is broken)? [yes/no]:',
    },
}


def words(key, **values):
    """A question's own words with its values put in: {'title', 'hint', ...}."""
    entry = Q[key]

    def filled(text):
        return text.format(**values) if values and isinstance(text, str) else text

    found = {name: filled(entry.get(name, '')) for name in ('title', 'hint', 'yes', 'no', 'send', 'skip_label')}
    found['kind'] = entry['kind']
    found['choices'] = [(answer, filled(label)) for answer, label in entry.get('choices', ())]
    found['how'] = filled(entry.get('how')) or HOW[entry['kind']] + (OPTIONAL if 'skip' in entry else '')
    found['skip'] = entry.get('skip')
    return found


def terminal(key, choices=None, **values):
    """What the terminal asks: the question, its hint, its choices by number, and how to answer."""
    asked = words(key, **values)
    lines = ['', asked['title']]
    if asked['hint']:
        lines.append(asked['hint'])
    for answer, label in choices if choices is not None else asked['choices']:
        lines.append(f'  {answer or "Enter"}) {label}')
    return '\n'.join(lines) + '\n' + asked['how'] + ' '


def window(key, choices=None, **values):
    """What the setup window shows for the question: its page."""
    asked = words(key, **values)
    choices = choices if choices is not None else asked['choices']
    page = {
        'title': asked['title'],
        'hint': asked['hint'],
        'kind': asked['kind'],
        'choices': [{'answer': answer, 'label': label} for answer, label in choices],
        'send': asked['send'] or 'Save',
        'above': bool(Q[key].get('above')),
        'skip': asked['skip'],
        'skip_label': asked['skip_label'] or SKIP,
    }
    if asked['kind'] == 'yesno':
        page['choices'] = [{'answer': 'yes', 'label': asked['yes']}, {'answer': 'no', 'label': asked['no']}]
    return page


@contextlib.contextmanager
def shown(key, choices=None, leave=False, **values):
    """While this is active the setup window shows the question in its own words, whatever
    the terminal is asked with. `leave`: the question may be left for later (a step the
    person confirms): the window has "Skip for now", which does what Ctrl+D does."""
    with setup_progress.page('ask', {'answer': ''}, leave=leave, **window(key, choices, **values)):
        setup_progress.page_asks('answer')
        yield


def ask(key, prompt=None, choices=None, **values):
    """Ask the question in both places and return what was answered, as typed: the window's
    buttons give the same answers the terminal takes. `choices`: [(answer, label)] for a
    list made at run time (the connections of this PC, the drives). EOFError as ever when
    the question was left (Ctrl+D, or the window's Skip on a step that can be left)."""
    prompt = prompt or input
    with shown(key, choices, **values):
        return prompt(terminal(key, choices, **values))


def yes_no(key, prompt=None, output=None, leave=False, **values):
    """A yes/no question: True, False, or None when it was left (Ctrl+D)."""
    import guided_setup

    asked = words(key, **values)
    text = '\n' + asked['title'] + ('\n' + asked['hint'] if asked['hint'] else '')
    with shown(key, leave=leave, **values):
        return guided_setup.yes_no(text, prompt, output, how='\n' + asked['how'] + ' ')


def screens():
    """The questions in the order setup shows them, by screen: [(screen, [key, ...])]."""
    found = {}
    for key, entry in Q.items():
        found.setdefault(entry['screen'], []).append(key)
    return list(found.items())
