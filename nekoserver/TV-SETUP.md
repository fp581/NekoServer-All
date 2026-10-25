# NekoServer TV setup

## Guided setup

Interactive setup now opens Steam, selected Bluetooth settings, display settings and browser playback, with yes/no retries. Actual Remote Play/controller/HDMI results are user-witnessed; they do not become automatic hardware probes. See GUIDED-SETUP.md.


## Before applying

This module installs Steam from your Linux's own repositories on x86_64 **Arch/Nyarch, Debian, Ubuntu, Fedora and openSUSE Tumbleweed/Slowroll**. Two systems have no Steam package, so the step reports itself unavailable there: **RHEL and its rebuilds** (Rocky, AlmaLinux, CentOS Stream) and **openSUSE Leap 16** (it dropped the 32-bit libraries Steam needs); Flatpak Steam is the way on those, which NekoServer does not set up yet. Bazzite requires a future backend. Sign into the existing desktop user, connect the TV, and make sure the desktop works.

Steam and its 32-bit libraries live in a part of the repositories that is off by default. NekoServer never switches repositories on itself: when the packages are not offered, the step stops with `needs_setup` and the log says what to run, then you run setup again. Per family:

| Family | Switch on first |
| --- | --- |
| Arch / Nyarch | `multilib` in `/etc/pacman.conf` (remove the `#` before `[multilib]` and its `Include` line), then `sudo pacman -Syu` |
| Debian | `sudo dpkg --add-architecture i386`, add `contrib non-free` to every `Components:` line in `/etc/apt/sources.list.d/*.sources` (`debian.sources` on an installed Debian; or to each `deb` line of `/etc/apt/sources.list`), then `sudo apt update` |
| Ubuntu | `sudo dpkg --add-architecture i386 && sudo add-apt-repository multiverse && sudo apt update` (a desktop install usually has both already) |
| Fedora | `sudo dnf install fedora-workstation-repositories && sudo dnf config-manager setopt rpmfusion-nonfree-steam.enabled=1` (RPM Fusion's Steam repository, Fedora's own third-party source for it) |
| openSUSE Tumbleweed / Slowroll | `sudo zypper addrepo -f https://download.opensuse.org/tumbleweed/repo/non-oss/ repo-non-oss` (Slowroll: `.../slowroll/repo/non-oss/`), then `sudo zypper refresh` |

The installer does not refresh package databases separately for this step.

Review `tv` in config.json:

```json
"tv": {
  "user": "auto",
  "desktop_shortcuts": true,
  "bluetooth": true,
  "keep_awake": true
}
```

`auto` chooses the invoking non-root SUDO_USER, otherwise the account with `media.uid` (default 1000). Set an explicit username when installing from a different administrative account. The selected user must own a real non-symlink home and shortcut directories. No users are created automatically.

```bash
bash install.sh --profile tv --check
bash install.sh --profile tv --dry-run
sudo bash install.sh --profile tv --apply
```

This profile includes storage checks and the persistent recovery bundle. Drive assignment still requires confirmation; nothing is formatted. The full profile also includes TV setup. Unavailable package repositories produce needs_setup so you can fix them and retry.

## What is installed

Packages are Steam, its device rules (`steam-devices`), the Vulkan driver for your Intel or AMD graphics for both 64-bit and 32-bit clients, a web browser only if the PC has none (Firefox, or Firefox ESR on Debian), and optionally BlueZ. Each family's own names are used (`steam-installer` and `mesa-vulkan-drivers:i386` on Debian and Ubuntu, `mesa-vulkan-drivers.i686` on Fedora, `libvulkan_intel-32bit` and `MozillaFirefox` on openSUSE). Bluetooth is enabled when selected. Steam device rules prepare permissions; pairing or model-specific controller drivers are separate steps. NVIDIA graphics need the maker's driver, installed your Linux's own way; Steam is installed without a Vulkan package then.

The TV step adds two shortcuts of its own, to the app menu (`.local/share/applications`) and, with `desktop_shortcuts`, to the desktop: **Steam Big Picture** (Steam with `-gamepadui`, from `/usr/games` on Debian and Ubuntu) and **YouTube** in the browser the PC has. The shortcuts for the dashboard, the web browser, Steam in a normal window and each app come from the desktop step, on every Linux (see README.md, "Desktop shortcuts"). GNOME shows desktop icons only with a desktop-icons extension; the app menu always has them.

Custom or incompatible files are preserved and block setup with a review message. Matching generated launchers are reused. Nothing starts by itself when you log in: since 0.28.7 the desktop step removes the Steam Big Picture autostart an older version could add (`.config/autostart/nekoserver-steam.desktop`; one you made yourself is kept). Automatic user login is not configured.

With `keep_awake=true`, the step checks that sleep is blocked system-wide (the server-OS step masks every sleep target, which no desktop setting can override); that counts on every desktop, KDE and XFCE included. On GNOME its own suspend-on-AC setting is switched off too, so the power panel agrees. Only when the system-wide block is missing does the step ask you to turn off automatic suspend in your desktop's power settings. Screen blanking is separate; do not mistake a dark screen for suspension.

The private report is `/srv/nekoserver/reports/tv-setup.json` with default mounts. Login, Remote Play, controller and HDMI acceptance remain needs_setup even when package/launcher installation succeeds. Settings/Steam client state live in the user's OS-side home; rerun this profile to recreate launchers after OS replacement. Persistent application databases remain on the application SSD.

## Complete the TV experience

1. Open NekoServer Steam Big Picture as the desktop user and sign in. Sign into the same Steam account on the main gaming PC.
2. Enable Remote Play on the main PC. Run the chosen game there once to confirm it works, then select Stream from the server's Steam library. Test a wired connection first when possible.
3. Connect the Xbox controller by USB first and check Steam's controller/Steam Input settings. For Bluetooth, pair your intended device through the desktop Bluetooth settings and retest. Some controller revisions and Xbox wireless dongles need additional model-specific drivers; the installer does not add AUR/DKMS drivers automatically.
4. Check HDMI resolution, TV sound output, controller navigation and keyboard/mouse access. Play a stream long enough to check lag and reconnect behavior. The module does not force display or sound settings or change firewall policy.
5. Test Jellyfin playback, music, Seerr requests and YouTube from the desktop shortcuts. Verify hardware acceleration separately; generated shortcuts do not prove media decoding, browser DRM or TV resolution compatibility.
6. Leave the server idle on AC and verify services remain reachable, then reboot and check Docker/media startup.

## Check current media connections

After completing first-run application web wizards, run the current onboarding command from README.md. To inspect live configuration without changing it:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.39.6/media-health.py --config /srv/nekoserver/installer/releases/v0.39.6/config.json --json-out /srv/nekoserver/reports/media/manual.json
```

For hourly monitoring, reapply the `media_monitor` profile from this release. Current checks use existing local application keys for GET requests; missing keys/server linkage stay incomplete. They check configured libraries and existing request-provider connectivity, while authenticated qBittorrent transfers, music playback and a complete request/download/import/playback exercise still need real-server validation. Keep original photos until a full independent Immich restore succeeds.

## Taking an app out, or changing the qBittorrent password

- **An app you take out of `services` under `media`** (in `my-config.json`, then `sudo bash install.sh`) is stopped. Its settings and data stay, so putting it back starts it as it was.
- **The connections other apps have to it are taken away** on that same setup run, so the other apps don't show warnings about it for good. These are only the ones setup made: Prowlarr's `neko-sonarr`, `neko-radarr` or `neko-lidarr` app, the `Neko-qBittorrent` download client in Sonarr, Radarr and Lidarr, and Seerr's `NekoServer Sonarr` and `NekoServer Radarr` servers. Setup knows them by their name and by where they point. A connection you made yourself, or one you changed to point somewhere else, stays; setup says so, and you can delete it in the app that has it (Prowlarr: Settings, Apps; Sonarr, Radarr, Lidarr: Settings, Download Clients; Seerr: Settings, Services). When you put the app back, the next setup run makes its connections again.
- **A new qBittorrent password:** tell setup, as for any app login. In the NekoServer folder run `sudo python3 account-setup.py --apply --config my-config.json` and choose **c** at qBittorrent. Setup then checks the new login and saves it in the `Neko-qBittorrent` download client in Sonarr, Radarr and Lidarr too (it says so for each app). Each later `sudo bash install.sh` checks again that these download clients can sign in to qBittorrent, and gives them the saved login if not. A download client you changed to point somewhere else is left alone.
- **qBittorrent's "automatic mode"** (Options, Downloads, Default Torrent Management Mode) is set to Automatic by setup only when Lidarr is in your list. Lidarr needs it: in Manual mode an album is saved on the HDD, where Lidarr can't see it. If you switch it back to Manual, the next setup run switches it to Automatic again and says so. Without Lidarr, setup leaves this setting as you choose.
