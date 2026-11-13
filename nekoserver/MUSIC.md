# Your personal music service

The server player is **Navidrome**, already included with the media containers. This release finishes guided listener setup and authenticated monitoring. Lidarr is the music acquisition/import manager; it is not the player. Prowlarr and qBittorrent remain connected to Lidarr through the existing guided source/indexer/category setup. With Lidarr in your list, setup sets qBittorrent to start new torrents in automatic mode, so music downloads land on the music drive, where Lidarr finds them (see TV-SETUP.md, "Taking an app out, or changing the qBittorrent password").

Use Navidrome's browser player on the TV/Linux PC, **Amperfy on iPhone**, and optionally **Feishin on Windows/Linux/macOS** for a polished desktop player. This provides an Apple Music-style personal-library experience (album artwork, artists, playlists, favourites and phone offline listening), not Apple's subscription catalogue or its proprietary recommendations. The native Apple Music app is not the Navidrome client. Install Amperfy from its App Store page and Feishin from its official project/Flathub; these client devices have not been remotely modified by this release.

## Storage and connections

- The drive setup asked you to pick for music (the SSD by default, or the media drive: STORAGE.md, "Where each app keeps your files"): library/music, shared by Lidarr as /data/music and Navidrome as read-only /music. Music downloads (library/downloads/music) use the same drive.
- SSD: appdata/navidrome, containing its database/accounts/playlists/history/art cache; appdata/lidarr and their independent Compose files.
- Browser setup: http://127.0.0.1:4533 on the server itself.
- Phone/remote clients: the configured LAN HTTPS music URL, https://YOUR-SERVER-IP:8446, or your configured private Tailscale Serve music URL on port 8446. localhost on your phone refers to the phone, not the server. Use the same login on every client to share its playlists and favourites ("Your logins" below says which one is yours).
- Container UIs stay bound to loopback. Remote access uses the existing secured-account confirmation and HTTPS gateway; nothing opens public router ports.
- Navidrome watches file changes with a short settling delay and performs an hourly fallback scan using its current Scanner settings. No artificial read loop is added to keep the HDD active. Embedded tags and cover art improve browsing; imported files need proper artist/album/title tags. An empty library is reported honestly.

Fresh deployments use the current scanner keys and disable public share links by default. Edited manifests/settings and differing existing backups are preserved for explicit review. Existing monthly immutable image pins continue to apply.

## Setup on the real server

The full profile includes music setup. A music profile is also provided; it respects media.services in your existing confirmed configuration and does not silently disable the other services you selected.

```bash
sudo bash install.sh --profile music --apply --config /absolute/path/to/confirmed-config.json
```

**Your logins.** Setup creates two Navidrome accounts: an **administrator** and a separate **listener** that is not an administrator.

- **A server set up with 0.50.0 or later:** your Navidrome login is the **administrator**, on your phone and computer too, as in every other app. To see it on the screen (with a QR code for your phone): `sudo python3 /srv/nekoserver/installer/releases/v0.51.1/account-setup.py show navidrome` (see ACCOUNT-SETUP.md). The listener is NekoServer's own login for checking the music library: a login with the fewest rights is the safer one to keep a token of.
- **A server set up before 0.50.0:** nothing was switched. Your phones are signed in with the **listener** (`show navidrome_listener`) and keep working. The administrator (`show navidrome`) is the one that manages Navidrome: users, settings, libraries. Navidrome keeps playlists and favourites per login, so a phone you move to the administrator starts without the listener's; stay with the listener on a phone whose playlists you want to keep, or make them again after moving.

If you change one of these passwords in Navidrome, tell setup: in the NekoServer folder (where `install.sh` is) run `sudo python3 account-setup.py --apply --config my-config.json`; it finds that the saved login no longer works. Run it once more: it asks about that account; press **c** and type the new password.

Only with account setup switched off (`accounts.enabled: false`) do you make them yourself: in Navidrome's first web page create the administrator, then in its Users settings create a separate non-administrator listener.

The interactive installer invokes music-setup.py. You can also run it afterward from this release's preserved SSD directory:

```bash
sudo python3 music-setup.py --config /absolute/path/to/confirmed-config.json --configure
```

The setup checks authenticated Subsonic ping, the listener's non-admin role, music folders and the album catalogue. It saves a root-only token/salt pair in appdata/navidrome/nekoserver-client.json only after success. No plaintext password is saved by this helper or placed in process arguments. The token/salt pair still authorizes that listener and must be treated as a secret. Reconfigure it if the listener password changes. MD5 token+salt is the Subsonic protocol requirement, not a claim of strong password hashing.

Noninteractive setup stays pending until the listener is configured. Missing credentials stay pending; private-file violations, invalid responses, bad login or a missing configured library fail authentication checks. An empty catalogue can pass service setup while remaining unverified for real playback. The existing TV music shortcut is reused; when Firefox/the desktop identity is not yet ready, desktop launch setup remains pending rather than creating a broken shortcut.

## Phone and desktop

1. Install Amperfy: https://apps.apple.com/app/amperfy-music/id1530145038 . Choose its Subsonic/Navidrome-compatible connection, enter the HTTPS music base URL and your Navidrome login ("Your logins" above). Amperfy provides offline caching, CarPlay, Siri integration and gapless playback for supported files; test those features on the actual phone.
2. Optional desktop player: https://github.com/jeffvli/feishin . Select Navidrome as the server type and use the same HTTPS URL/account. Linux Flatpak listing: https://flathub.org/apps/org.jeffvli.feishin . This is an optional user-facing upstream client, not another server daemon installed by the NekoServer release.
3. Test a tagged album: play/pause, next track, seeking, album artwork, favourites and a playlist. Cache an album on the iPhone and test offline playback. Verify away-from-home access through Tailscale if selected. LAN-only HTTPS requires the server's private CA to be trusted by the phone and client.

Navidrome playlists/accounts/history remain covered by application snapshots. Music files have no automatic backup (STORAGE.md); the opt-in music backup can be re-enabled with `music.backup_enabled`. It writes to the media drive: with the music itself on the media drive, it keeps older versions of your files but does not protect them from losing that drive (or the NAS it is on). See STORAGE.md for schedules, version retention and music-recovery.py instructions.

## Automated checks and monthly updates

Music setup uses Python only as an ad hoc credential/setup tool. The periodic media-health worker uses Rust to make read-only authenticated local API calls; it does not run a Python daemon or print/store credentials in public reports. It keeps human listening tests pending until witnessed.

This review also corrects monthly update gating: the old media audit always included pending human playback/end-to-end checks, which made an otherwise healthy selected media stack permanently ineligible for monthly updates. Maintenance now accepts the audit's pending exit code only when every automated service/integration/catalogue check passes. Only three exact human-only pending rows are exempted. Actual failures, missing app routing, invalid music credentials, warnings and other pending setup still block updates. The dashboard and deployment acceptance continue to show the unverified human tests; they are never marked passed by the update worker.

References: https://www.navidrome.org/docs/ ; https://github.com/BLeeEZ/amperfy ; https://github.com/jeffvli/feishin
