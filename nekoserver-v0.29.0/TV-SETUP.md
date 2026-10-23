# NekoServer TV setup

## Guided setup

Interactive setup now opens Steam, selected Bluetooth settings, display settings and browser playback, with yes/no retries. Actual Remote Play/controller/HDMI results are user-witnessed; they do not become automatic hardware probes. See GUIDED-SETUP.md.


## Before applying

This module supports native x86_64 Nyarch/Arch installation; on Debian and Ubuntu it is skipped for now. Bazzite requires a future backend. Sign into the existing desktop user, connect the TV, and make sure the desktop works. Configure normal Arch repositories, including multilib for Steam/32-bit packages, and update the system normally. The installer does not edit repositories or refresh package databases separately.

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

Packages are Steam, steam-devices, Intel Vulkan drivers for both 64-bit and 32-bit clients, Firefox, and optionally BlueZ/tools. Bluetooth is enabled when selected. Steam device rules prepare permissions; pairing or model-specific controller drivers are separate steps.

The TV step adds two shortcuts of its own, to the app menu (`.local/share/applications`) and, with `desktop_shortcuts`, to the desktop: **Steam Big Picture** (Steam with `-gamepadui`) and **YouTube** in Firefox. The shortcuts for the dashboard, the web browser, Steam in a normal window and each app come from the desktop step, on every Linux (see README.md, "Desktop shortcuts"). GNOME shows desktop icons only with a desktop-icons extension; the app menu always has them.

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
sudo python3 /srv/nekoserver/installer/releases/v0.29.0/media-health.py --config /srv/nekoserver/installer/releases/v0.29.0/config.json --json-out /srv/nekoserver/reports/media/manual.json
```

For hourly monitoring, reapply the `media_monitor` profile from this release. Current checks use existing local application keys for GET requests; missing keys/server linkage stay incomplete. They check configured libraries and existing request-provider connectivity, while authenticated qBittorrent transfers, music playback and a complete request/download/import/playback exercise still need real-server validation. Keep original photos until a full independent Immich restore succeeds.
