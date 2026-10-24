"""Plain words for the setup window: what each step does, what each package is for, and
the optional parts a person can switch on or off before setup starts.

The step titles themselves live in wizard.STEPS (the terminal shows them too); this
module adds the one-line explanation under each."""

# What each step does, in one line a person who never ran a server can follow.
STEP_EXPLANATIONS = {
    'system': 'Checks this PC: processor, memory, Python, Docker, firewall and the Linux it runs.',
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
}

# The parts a person can leave out. `section`/`key` is where the choice is saved in
# my-config.json; `on_default` is what setup assumes when nothing was chosen.
EXTRAS = (
    {'id': 'tv', 'section': 'tv', 'key': 'wanted', 'on_default': True, 'step': 'tv',
     'title': 'A TV connected to this PC',
     'explanation': 'Steam Big Picture for Remote Play from your gaming PC, a YouTube shortcut and Bluetooth for a '
                    'controller. Skip it if this PC has no TV; your apps work on your other devices either way.',
     'installs': ('steam', 'steam-devices', 'vulkan-intel', 'lib32-vulkan-intel', 'firefox', 'bluez')},
    {'id': 'notifications', 'section': 'notifications', 'key': 'wanted', 'on_default': True, 'step': 'notifications',
     'title': 'Phone alerts when something fails',
     'explanation': 'A message on your phone through the free ntfy app when a backup or an app fails. '
                    'You set up the ntfy app on your phone during setup.',
     'installs': ()},
    {'id': 'heartbeat', 'section': 'heartbeat', 'key': 'wanted', 'on_default': True, 'step': 'heartbeat',
     'title': 'Outage alerts if the server goes offline',
     'explanation': 'The server checks in with an outside service you choose (Healthchecks.io, say); when it stops '
                    'checking in, that service tells you.',
     'installs': ()},
    {'id': 'nextcloud', 'section': 'nextcloud', 'key': 'enabled', 'on_default': True, 'step': 'nextcloud',
     'title': 'Nextcloud, your private cloud drive',
     'explanation': 'Your files on your own server, from the phone and the browser, with sharing. '
                    'Photos go to Immich either way.',
     'installs': ()},
)


def extra_state(config, extra):
    """Whether this extra is on in the settings (its default when nothing was chosen)."""
    value = config.get(extra['section'], {}).get(extra['key'])
    return extra['on_default'] if not isinstance(value, bool) else value


def set_extra(config, extra, on):
    config.setdefault(extra['section'], {})[extra['key']] = bool(on)
