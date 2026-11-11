"""Plain words for the setup window: what each step does, what each package is for, and
the optional parts a person can switch on or off before setup starts.

The step titles themselves live in wizard.STEPS (the terminal shows them too); this
module adds the one-line explanation under each."""

# What each step does, in one line a person who never ran a server can follow.
STEP_EXPLANATIONS = {
    'system': 'Checks this PC: processor, memory, Python, Docker, firewall and the Linux it runs.',
    'network_storage': 'Connects the drives on your network storage box (NAS), over iSCSI or NFS, when you switched that on.',
    'storage': 'Asks which drive is the SSD (apps), the HDD (your media) and the USB drive (backups), and mounts them.',
    'capacity': 'Makes sure each drive has room for what will go on it.',
    'recovery': 'Keeps a copy of NekoServer and your settings on the SSD, so a reinstall can find them.',
    'account_credentials': 'Prepares a username and password for every app (random, or ones you choose).',
    'server_os': 'Stops the PC from going to sleep and keeps its logs from growing without end.',
    'docker': 'Installs Docker, which runs each app in its own container, with its data on the SSD.',
    'recovery_checkpoint': 'Records how Docker is set up, so the apps can be reattached after a reinstall.',
    'media': 'Installs Jellyfin (movies and shows), Navidrome (music), Seerr, Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent.',
    'photos': 'Installs Immich, your own photo backup and gallery, with an archive copy of every original.',
    'photo_preservation': 'Schedules a weekly copy of the photo originals and the photo database to the HDD.',
    'photo_backup': 'Schedules a weekly copy of photos and files to the USB drive (nothing is ever deleted from it).',
    'nextcloud': 'Installs Nextcloud, your private cloud drive for files.',
    'accounts': 'Creates your account in each app and finishes their first-run setup for you.',
    'hardware_video': "Lets Jellyfin and Immich use the graphics chip, so video plays smoothly.",
    'onboarding': 'Connects the apps to each other: libraries, download paths, sources and requests.',
    'music': 'Checks music playback and puts Navidrome on the desktop.',
    'assistant': 'Installs the AI assistant you chose for the dashboard\'s Ask Neko page, with its model running on this PC.',
    'gateway_lan': 'Gives your apps one address at home, with HTTPS, through the Caddy gateway.',
    'tailscale': 'Installs Tailscale: a private network between your own devices, so the apps work from anywhere.',
    'gateway_tailnet': 'Gives each app a private Tailscale address and walks you through setting up your phone.',
    'samba': 'Shares your media folders on the home network, for Windows, Mac and the TV.',
    'media_monitor': 'Schedules an hourly check that the apps are still connected to each other.',
    'dashboard': 'Installs the NekoServer dashboard: apps, backups, drives, temperatures and fans in one page.',
    'tv': 'Installs Steam Big Picture and a YouTube shortcut for a TV connected to this PC.',
    'desktop': 'Puts shortcuts for the dashboard, the browser and each app on the desktop.',
    'app_backup': 'Schedules a nightly backup of every app and its settings.',
    'notifications': 'Sends a message to your phone (through ntfy) when something fails.',
    'heartbeat': 'Lets an outside service notice when the server goes offline and tell you.',
    'host_backup': 'Schedules a nightly backup of the system settings NekoServer wrote.',
    'maintenance': 'Schedules the monthly update: a verified backup, then newer app versions, then a check.',
}

# What each system package is for (NekoServer's names; a family may call them differently).
PACKAGES = {
    'docker': 'Runs each app in its own container, apart from the rest of the PC',
    'docker-compose': "Starts each app's containers together from one description",
    'tailscale': 'A private network between your own devices: your apps from anywhere, no open ports',
    'caddy': 'The home gateway: one address with HTTPS for all your apps',
    'samba': 'Shared folders on your home network',
    'hdparm': 'Lets the HDD spin down when nothing uses it',
    'smartmontools': "Reads the drives' own health reports (SMART)",
    'qrencode': 'Draws the QR codes you scan with your phone',
    'xdg-utils': 'Opens pages in your browser during setup',
    'openrgb': 'Controls the RGB lighting from the dashboard',
    'intel-media-driver': 'Video decoding on Intel graphics',
    'libva-mesa-driver': 'Video decoding on AMD graphics',
    'libva-utils': 'Checks that video decoding on the graphics chip works',
    'steam': 'Steam, for Big Picture on the TV and Remote Play from your gaming PC',
    'steam-devices': 'Lets Steam use game controllers',
    'vulkan-intel': 'Graphics driver for games on Intel chips',
    'lib32-vulkan-intel': 'The same driver for 32-bit games (Intel)',
    'vulkan-radeon': 'Graphics driver for games on AMD chips',
    'lib32-vulkan-radeon': 'The same driver for 32-bit games (AMD)',
    'firefox': 'A web browser for the TV shortcuts',
    'bluez': 'Bluetooth, for controllers and headphones',
    'bluez-utils': 'The Bluetooth command-line tools',
    'python3': 'The language setup is written in',
    'open-iscsi': "Connects this PC to the disks on your network storage box (iSCSI)",
    'nfs-utils': 'Opens the folders your network storage box shares (NFS)',
}

# Setup's first page, "What would you like this server to do?": every part a person can
# leave out, chosen once, here. No step asks about one of them again.
# `section`/`key` is where the choice is saved in my-config.json. `part` is the heading it
# is listed under (PARTS). `on_default` is what a server set up before has when nothing
# was saved; `new_default` what a new server is offered (an extra is an opt-in there).
PAGE_TITLE = 'What would you like this server to do?'
PAGE_HINT = (
    'Backups, the dashboard and private access from your own devices are always set up. '
    'Switching something off later stops it and keeps its data.'
)
PARTS = (
    ('apps', 'Apps'),
    ('extras', 'Optional extras'),
    ('storage', 'Storage'),
)
# The small switch inside the Movies and shows card and the Music card (`under`): off
# leaves Jellyfin, or Navidrome, alone. Offered while its card is on (`needs`).
FINDING_SWITCH = {
    group: {
        'id': group + '_downloads',
        'section': 'media_apps',
        'key': group + '_downloads',
        'group': True,
        'part': 'apps',
        'under': 'media_' + group,
        'needs': ('media_' + group,),
        'on_default': True,
        'step': 'media',
        'title': 'Also find and download new ones',
        'explanation': 'Finds and downloads ' + what + ' you are entitled to, and adds them to your library. '
        'You choose where it searches later.',
        'installs': (),
    }
    for group, what in (('movies', 'films and shows'), ('music', 'music'))
}
EXTRAS = (
    # The media apps (neko_common.MEDIA_GROUPS): each a switch of its own, saved under
    # "media_apps". `group` marks them: one with no answer follows the one yes/no of
    # before ("wanted"), which extra_state reads through neko_common.media_group.
    {
        'id': 'media_photos',
        'section': 'media_apps',
        'key': 'photos',
        'group': True,
        'part': 'apps',
        'on_default': True,
        'step': 'photos',
        'title': 'Photos and phone backup - Immich',
        'explanation': 'Store and browse your photos and back them up from your phone.',
        'installs': (),
    },
    {
        'id': 'media_movies',
        'section': 'media_apps',
        'key': 'movies',
        'group': True,
        'part': 'apps',
        'on_default': True,
        'step': 'media',
        'title': 'Movies and shows - Jellyfin',
        'explanation': 'Watch your films and shows on any device, and ask for new ones to be found and added.',
        'small': 'Jellyfin. With finding and downloading: Seerr, Sonarr, Radarr, Prowlarr and qBittorrent.',
        'installs': (),
    },
    {
        'id': 'media_music',
        'section': 'media_apps',
        'key': 'music',
        'group': True,
        'part': 'apps',
        'on_default': True,
        'step': 'music',
        'title': 'Music - Navidrome',
        'explanation': 'Play your music collection from your phone or browser, and have new music found and added.',
        'small': 'Navidrome. With finding and downloading: Lidarr, Prowlarr and qBittorrent.',
        'installs': (),
    },
    {
        'id': 'nextcloud',
        'section': 'nextcloud',
        'key': 'enabled',
        'part': 'apps',
        'on_default': True,
        'step': 'nextcloud',
        'title': 'Personal files - Nextcloud',
        'explanation': 'Access your files from your devices.',
        'installs': (),
    },
    # Drawn inside their cards (`under`); listed here after the cards themselves.
    FINDING_SWITCH['movies'],
    FINDING_SWITCH['music'],
    {
        'id': 'tv',
        'section': 'tv',
        'key': 'wanted',
        'part': 'extras',
        'on_default': True,
        'new_default': False,
        'step': 'tv',
        'title': 'A TV connected to this PC',
        'explanation': 'Play games from your gaming PC and watch YouTube on the TV this PC is plugged into: Steam Big '
        'Picture with Remote Play, a YouTube shortcut and Bluetooth for a controller. Leave it off if this PC has no TV.',
        'installs': ('steam', 'steam-devices', 'vulkan-intel', 'lib32-vulkan-intel', 'firefox', 'bluez'),
    },
    {
        'id': 'notifications',
        'section': 'notifications',
        'key': 'wanted',
        'part': 'extras',
        'on_default': True,
        'new_default': False,
        'step': 'notifications',
        'title': 'Send alerts to my phone',
        'explanation': 'A message on your phone, through the free ntfy app, when a backup or an app fails.',
        'installs': (),
    },
    {
        'id': 'heartbeat',
        'section': 'heartbeat',
        'key': 'wanted',
        'part': 'extras',
        'on_default': True,
        'new_default': False,
        'step': 'heartbeat',
        'title': 'Warn me if the server stops checking in',
        'explanation': 'This server checks in with an outside service you choose (Healthchecks.io, say). When the '
        'check-ins stop, after a power cut for example, that service warns you.',
        'installs': (),
    },
    {
        'id': 'assistant',
        'section': 'assistant',
        'key': 'wanted',
        'part': 'extras',
        'on_default': False,
        'step': 'assistant',
        'title': 'Add a local AI assistant',
        'explanation': 'Ask Neko, in the dashboard: it explains what the dashboard shows and what to do about a '
        'problem, and can suggest a fix that runs only when you approve it. Its model runs on this PC; setup '
        'shows how much memory each model needs before you choose one.',
        'installs': (),
    },
    {
        'id': 'network_storage',
        'section': 'network_storage',
        'key': 'wanted',
        'part': 'storage',
        'on_default': False,
        'step': 'network_storage',
        'title': 'My media and backup drives are in a network storage box (NAS)',
        'explanation': 'For a NAS that offers iSCSI disks (a WD My Cloud EX, Synology, QNAP...) or shares folders '
        'over NFS. The SSD for the apps stays on this PC. Setup asks for the NAS\'s address, sees which of '
        'the two it offers, and asks which disks or folders to use; it never formats one, and tries a shared '
        'folder out before using it. '
        'Experimental: tried in a test lab, not yet on many real NAS boxes (SUPPORT.md).',
        # One of the two, by what the NAS offers.
        'installs': ('open-iscsi', 'nfs-utils'),
    },
)


def shown():
    """The extras in the order a list shows them: each card followed by the switches inside it."""
    inside = [extra for extra in EXTRAS if extra.get('under')]
    order = []
    for extra in EXTRAS:
        if not extra.get('under'):
            order.append(extra)
            order += [sub for sub in inside if sub['under'] == extra['id']]
    return order


def extra_chosen(config, extra):
    """Whether this extra has a saved answer (from the first page, or by hand in the settings)."""
    section = config.get(extra['section'])
    own = isinstance(section, dict) and isinstance(section.get(extra['key']), bool)
    if extra.get('group') and not extra.get('needs'):
        from neko_common import media_group_chosen

        return media_group_chosen(config, extra['key'])
    return own


def extra_state(config, extra):
    """Whether this extra is on: its saved answer; with none, what a server set up
    before has (`on_default`), or what a new server is offered (`new_default`)."""
    import neko_common

    chosen = extra_chosen(config, extra)
    if not chosen and not neko_common.server_exists(config) and not extra.get('new_default', extra['on_default']):
        return False
    if extra.get('group'):
        return neko_common.media_group(config, extra['key'])
    return config[extra['section']][extra['key']] if chosen else extra['on_default']


def set_extra(config, extra, on):
    config.setdefault(extra['section'], {})[extra['key']] = bool(on)


def states(config):
    """Every extra with what it is now: {id: on or off}."""
    return {extra['id']: extra_state(config, extra) for extra in EXTRAS}


def settle(config):
    """Every extra saved as it stands (the first page was gone through): from here on
    each one has an answer, so no step asks about it."""
    now = states(config)
    for extra in EXTRAS:
        # A switch inside a card keeps its own answer while its card is off: it counts
        # again when the card is switched back on.
        needed = not extra.get('under') and extra.get('needs')
        set_extra(config, extra, now[extra['id']] and (not needed or any(now[other] for other in extra['needs'])))


def offered(config, extra):
    """The extra can be switched on: what it needs (one of them) is on."""
    now = states(config)
    return not extra.get('needs') or any(now[other] for other in extra['needs'])


# ---- the review before anything is installed ("Ready to install NekoServer")
REVIEW_TITLE = 'Ready to install NekoServer'
REVIEW_TITLE_AGAIN = 'Review your changes'
INSTALL = 'Install selected apps'
APPLY = 'Apply these changes'
ALWAYS = (
    'The dashboard, with a PIN you choose',
    'Backups of your apps and settings, and a monthly checkup',
    'Tailscale: private access from your own devices, not from the internet',
    'Shared folders on your home network',
)
# Until each of these has its own page before the review, setup asks about it when it
# gets there (the pages of 0.50.0 are built one by one; this list shrinks with them).
ASKED_DURING = {
    'drives': 'which drive is for the apps, which for your files and which for backups',
    'logins': 'whether to make a password for each app or use ones you choose',
    'backup_times': 'when the backups run',
    'notifications': 'the address of your alerts topic',
    'heartbeat': 'the check-in address of your outage service',
    'assistant': 'which AI model to use',
    'phone': 'signing in to Tailscale, and setting up your phone',
}


def review(config):
    """What pressing Install will do, as headed lists, from the settings as they stand:
    [(heading, [line])]. The same in the window and in the terminal."""
    import neko_common

    now = states(config)
    apps = []
    if now['media_photos'] and config.get('photos', {}).get('deploy_immich', True) is True:
        apps.append('Immich: your photos, with a backup from your phone')
    wanted = {**config, 'media_apps': {**_section(config, 'media_apps')}}
    for extra in EXTRAS:
        if extra.get('group'):
            wanted['media_apps'][extra['key']] = now[extra['id']] and offered(config, extra)
    for app in neko_common.media_services(wanted):
        name, role, _port = neko_common.APPS[app]
        apps.append(f'{name}: {role[0].lower()}{role[1:]}')
    if now['nextcloud']:
        apps.append('Nextcloud: your files on your own devices')
    extras = [extra['title'] for extra in EXTRAS if extra['part'] in ('extras', 'storage') and now[extra['id']]]
    left_out = [
        extra['title']
        for extra in EXTRAS
        if extra['part'] == 'apps' and not extra.get('under') and not now[extra['id']]
    ]
    asked = [words for key, words in ASKED_DURING.items() if key not in now or now[key]]
    sections = [('Apps to install', apps or ['None: only what is always set up'])]
    if extras:
        sections.append(('Extras', extras))
    if left_out:
        sections.append(
            ('Left out', [title + ' (stopped if this server has it; its data is kept)' for title in left_out])
        )
    sections.append(('Always set up', list(ALWAYS)))
    sections.append(
        (
            'What happens next',
            [
                'Setup takes about an hour. It installs the apps, makes an account in each one and connects them.',
                'It never formats a drive and never deletes your files.',
            ],
        )
    )
    sections.append(('Setup stops to ask you', [words[0].upper() + words[1:] for words in asked]))
    return sections


def _section(config, name):
    value = config.get(name)
    return value if isinstance(value, dict) else {}
