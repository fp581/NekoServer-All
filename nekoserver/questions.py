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

The golden file tests/golden/questions.txt lists every question read from the source, so
a change to one shows in review.
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
#   example values for the {names} in the words, for tests that show the question
#   above   the question is about what setup listed just before it: the window shows that list too
#   screen  where in setup it comes
Q = {
    # ---- before setup starts
    'keep_fewer': {
        'screen': 'Starting setup',
        'title': 'Keep fewer old backups from now on?',
        'hint': 'This server keeps the newest {old} {what}. New servers keep {new}, which saves drive space.',
        'kind': 'yesno',
        'yes': 'Keep the newest {new}',
        'no': 'Keep {old} as now',
        'example': {'old': 30, 'new': 7, 'what': 'app backups'},
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
    },
    # ---- a NAS
    'nas_address': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'What is the address of your NAS?',
        'hint': 'It is what you open in a browser to reach it, such as 192.168.1.20.',
        'kind': 'text',
        'send': 'Look there',
    },
    'nas_kind': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'How should NekoServer use your NAS?',
        'hint': 'It offers both ways. If you are not sure, take the first.',
        'kind': 'choice',
        'choices': [
            ('1', 'As a disk of its own (only this PC can use it)'),
            ('2', 'As a shared folder (the files stay visible on the NAS)'),
        ],
        'how': 'Type 1 or 2, then press Enter. Enter alone takes the first.',
    },
    'nas_disks': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'Which of these disks are for NekoServer?',
        'hint': 'The NAS offers the disks listed above. Choose only the ones you made for this server.',
        'kind': 'text',
        'above': True,
        'send': 'Use these',
        'how': 'Type their numbers with commas between them (such as 1,2), then press Enter. Enter alone stops setup here.',
    },
    'nas_login_needed': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'Does your NAS ask for a user name and password for this disk?',
        'hint': 'Most do not. If you set one up on the NAS (it calls it CHAP), choose yes. The disk is {iqn}.',
        'kind': 'yesno',
        'yes': 'Yes, it has a password',
        'no': 'No password',
        'how': 'Type yes or no, then press Enter. Enter alone means no.',
        'example': {'iqn': 'iqn.2000-01.com.synology:nas.Target-1'},
    },
    'nas_login_user': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'What is the user name for that disk?',
        'kind': 'text',
        'send': 'Next',
    },
    'nas_folder': {
        'screen': 'A NAS (a storage device on your home network)',
        'title': 'Which shared folder is for {words}?',
        'hint': 'The folders the NAS shares are listed above.',
        'kind': 'text',
        'above': True,
        'send': 'Use this folder',
        'skip': '',
        'skip_label': 'Use a drive on this PC instead',
        'how': 'Type its number or its name, then press Enter. Enter alone uses a drive on this PC instead.',
        'example': {'words': 'your films, shows and backups'},
    },
    # ---- logins for the apps
    'logins_who': {
        'screen': 'Logins for your apps',
        'title': 'How would you like to set up your app accounts?',
        'hint': 'Each selected app has a separate account. NekoServer stores its required credentials privately so '
        'it can connect and check the apps.',
        'kind': 'choice',
        'choices': [('1', 'Create strong passwords for me (recommended)'), ('2', 'Choose my own logins')],
        'how': 'Type 1 or 2, then press Enter. Enter alone takes the first.',
    },
    'login_name': {
        'screen': 'Logins for your apps',
        'title': 'What user name do you want for {app}?',
        'kind': 'text',
        'send': 'Next',
        'example': {'app': 'Jellyfin'},
    },
    'login_email': {
        'screen': 'Logins for your apps',
        'title': 'What email address do you want to sign in to {app} with?',
        'hint': 'This identifies your account. Email delivery and password recovery depend on how {app} is configured.',
        'kind': 'text',
        'send': 'Next',
        'example': {'app': 'Immich'},
    },
    'login_retry': {
        'screen': 'Logins for your apps',
        'title': '{app} did not take the saved login. What now?',
        'hint': 'If you changed the password in {app} yourself, tell NekoServer the new one.',
        'kind': 'choice',
        'choices': [('', 'Try the saved login again'), ('c', 'I changed it: here is the new one')],
        'how': 'Press Enter alone to try again, or type c and press Enter to enter the new login.',
        'example': {'app': 'Jellyfin'},
    },
    'app_signed_in': {
        'screen': 'Logins for your apps',
        'title': 'Could you sign in to {app}?',
        'hint': '{app} opened in your browser. "Show login" gives you its user name and password.',
        'kind': 'yesno',
        'yes': 'Yes, I am in',
        'no': 'Not yet',
        'example': {'app': 'Jellyfin'},
    },
    # ---- Tailscale
    'tailscale_connected': {
        'screen': 'Reaching the server from your phone',
        'title': 'Is this server connected to Tailscale?',
        'hint': 'The Tailscale page in your browser says so once you have signed in and pressed Connect.',
        'kind': 'yesno',
        'yes': 'Yes, it is connected',
        'no': 'Not yet',
    },
    'tailscale_https': {
        'screen': 'Reaching the server from your phone',
        'title': 'Did you switch on both settings in Tailscale?',
        'hint': 'On the page that opened, "MagicDNS" and "HTTPS Certificates" must both be on. They give each app a safe address.',
        'kind': 'yesno',
        'yes': 'Yes, both are on',
        'no': 'Not yet',
    },
    'phone_ready': {
        'screen': 'Reaching the server from your phone',
        'title': 'Is your phone set up?',
        'hint': 'Yes when the apps open on the phone, also with Wi-Fi off.',
        'kind': 'yesno',
        'yes': 'Yes, it works',
        'no': 'Not yet',
    },
    # ---- connecting the media apps
    'played': {
        'screen': 'Connecting your apps',
        'title': 'Did a film or show play in Jellyfin?',
        'hint': 'Add something you are allowed to watch, wait for it to arrive, and press play. You can also do this later.',
        'kind': 'yesno',
        'yes': 'Yes, it played',
        'no': 'Not yet',
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
    },
    'source_account_ready': {
        'screen': 'Connecting your apps',
        'title': 'Do you have your account at {source} ready?',
        'hint': '{source} needs an account of its own. Have its user name and password, or its key, at hand.',
        'kind': 'yesno',
        'yes': 'Yes, I have it',
        'no': 'Not yet',
        'example': {'source': 'ExampleTracker'},
    },
    'source_saved': {
        'screen': 'Connecting your apps',
        'title': 'Is {source} saved in Prowlarr now?',
        'hint': 'Prowlarr opened in your browser. Fill in what it asks for {source}, press Test, then Save.',
        'kind': 'yesno',
        'yes': 'Yes, it is saved',
        'no': 'Not yet',
        'example': {'source': 'ExampleTracker'},
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
    },
    'qbit_password': {
        'screen': 'Connecting your apps',
        'title': 'What is the password of your download app (qBittorrent)?',
        'hint': 'It is used once to connect the apps and is not kept.',
        'kind': 'secret',
        'send': 'Connect',
    },
    # ---- alerts
    'alert_arrived': {
        'screen': 'Alerts on your phone',
        'title': 'Did the test message arrive on your phone?',
        'hint': '{said} Look at your phone now: a message called "NekoServer test" should be there.',
        'kind': 'yesno',
        'yes': 'Yes, it arrived',
        'no': 'Not yet',
        'example': {'said': 'The alert server accepted the test message.'},
    },
    'alerts_token': {
        'screen': 'Alerts on your phone',
        'title': 'Does your alerts topic have an access token? Paste it here',
        'hint': 'Only private topics have one. If yours has none, skip this.',
        'kind': 'secret',
        'send': 'Save',
        'skip': '',
        'skip_label': 'It has none',
    },
    'alerts_switch': {
        'screen': 'Alerts on your phone',
        'title': 'Send alerts to the new topic?',
        'hint': 'Your settings name another alerts topic than the one alerts go to now.',
        'kind': 'yesno',
        'yes': 'Use the new one',
        'no': 'Keep the old one',
    },
    # ---- the outage monitor
    # ---- the assistant
    'existing_media': {
        'screen': 'Media you already have',
        'title': 'Do you already have films, shows or music that this server should use?',
        'hint': 'On this PC, on another drive, or on a NAS (a storage device on your home network) whose folder is connected to this PC. '
        'Nothing in your folder is changed.',
        'kind': 'yesno',
        'yes': 'Yes, I have some',
        'no': 'No, start empty',
    },
    'existing_folder': {
        'screen': 'Media you already have',
        'title': 'Which folder holds your {what}?',
        'hint': 'The folder as this PC sees it, such as /mnt/tank/{what}. For a NAS that is where its '
        'shared folder is connected here, not its network address.',
        'kind': 'text',
        'send': 'Use this folder',
        'skip': '',
        'skip_label': 'I have none',
        'how': 'Type the folder, then press Enter. Enter alone when you have none.',
        'example': {'what': 'films'},
    },
    'existing_use': {
        'screen': 'Media you already have',
        'title': 'How should this server use your {what}?',
        'hint': 'Your folder is kept as it is either way.',
        'kind': 'choice',
        'choices': [
            ('1', 'Use the files where they are (nothing is copied)'),
            ('2', 'Copy them into the NekoServer library'),
        ],
        'how': 'Type 1 or 2, then press Enter. Enter alone takes the first.',
        'example': {'what': 'films'},
    },
    'existing_new_files': {
        'screen': 'Media you already have',
        'title': 'Save new {what} here too?',
        'hint': 'New downloads will be saved in this folder. Your existing files are not moved.',
        'kind': 'yesno',
        'yes': 'Yes, save new ones here',
        'no': 'No, in the NekoServer library',
        'example': {'what': 'films'},
    },
    'existing_new_place': {
        'screen': 'Media you already have',
        'title': 'Where should new {what} be saved?',
        'kind': 'choice',
        'choices': [('1', 'In the NekoServer library'), ('2', 'In another folder of mine')],
        'how': 'Type 1 or 2, then press Enter. Enter alone takes the first.',
        'example': {'what': 'films'},
    },
    'existing_new_folder': {
        'screen': 'Media you already have',
        'title': 'Which folder should new {what} be saved in?',
        'hint': 'The folder as this PC sees it. The apps must be able to write there.',
        'kind': 'text',
        'send': 'Use this folder',
        'skip': '',
        'skip_label': 'In the NekoServer library instead',
        'how': 'Type the folder, then press Enter. Enter alone saves them in the NekoServer library.',
        'example': {'what': 'films'},
    },
    'assistant_model': {
        'screen': 'Ask Neko, the AI assistant',
        'title': 'Which AI assistant do you want?',
        'hint': 'Bigger ones answer better and use more memory. The list above says what each needs.',
        'kind': 'choice',
        'how': 'Type its number, then press Enter. Enter alone keeps {default}.',
        'example': {'default': 'no assistant'},
    },
    # ---- the TV
    'tv_worked': {
        'screen': 'A TV connected to this PC',
        'title': 'Did everything work on the TV?',
        'hint': 'Steam, the controller, and the picture and sound.',
        'kind': 'yesno',
        'yes': 'Yes, all of it',
        'no': 'Not yet',
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
    },
    # ---- recovery
    # Section 2, "Review this restore": the one approval of a restore, asked under the
    # review of what was chosen (recovery_wizard.review). Nothing is written before it.
    'restore_approve': {
        'screen': 'Recovery',
        'title': 'Restore these items now?',
        'hint': 'Nothing has been changed so far. "Go back" restores nothing.',
        'kind': 'yesno',
        'yes': 'Restore selected items',
        'no': 'Go back',
        'how': 'Type yes to restore, or no to go back, then press Enter. Enter alone does nothing.',
        'above': True,
    },
    # Asked only when a monthly snapshot's app versions are not all on this PC (what the
    # snapshot holds and what Docker has was looked at; "Is this a new server?" is gone).
    'restore_app_versions': {
        'screen': 'Recovery',
        'title': 'Put the app versions saved in this snapshot back on this PC too?',
        'hint': '{found} The settings and databases in the snapshot were made with exactly these versions, so a '
        'rebuilt server needs them. They are added to the program that runs the apps (Docker) and take '
        '{size} there. No app is started or replaced.',
        'kind': 'yesno',
        'yes': 'Yes, put them back too',
        'no': 'No, only unpack the settings and databases',
        'example': {
            'found': '3 of the 9 app versions this snapshot was made with are not on this PC.',
            'size': '6.2 GB',
        },
    },
    # Section 3, "Check or repair a drive". The check and the repair are two actions.
    'check_drive': {
        'screen': 'Recovery',
        'title': 'Stop every app and check this drive now?',
        'hint': 'The check only looks: it changes nothing on the drive. The apps are off until it has finished.',
        'kind': 'yesno',
        'yes': 'Check it now',
        'no': 'Not now',
        'how': 'Type yes to check, or no to leave everything as it is, then press Enter. Enter alone does nothing.',
        'above': True,
    },
    'repair_drive': {
        'screen': 'Recovery',
        'title': 'Stop every app and repair this drive now?',
        'hint': 'A repair changes the drive and cannot be undone. You approve it with the drive\'s name: {name}',
        'kind': 'text',
        'send': 'Repair this drive',
        'how': 'Type {name} and press Enter to repair. Anything else, or Enter alone, leaves the drive as it is.',
        'above': True,
        'example': {'name': 'sdb1'},
    },
    'fix_copies': {
        'screen': 'Recovery',
        'title': 'Fix these backup copies now?',
        'hint': 'Only the backup drives are changed. A damaged copy is moved aside on its drive, never deleted.',
        'kind': 'yesno',
        'yes': 'Fix them',
        'no': 'Not now',
        'how': 'Type yes to fix them, or no to leave them as they are, then press Enter. Enter alone does nothing.',
        'above': True,
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
