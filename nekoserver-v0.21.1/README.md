# NekoServer v0.21.1

## v0.21 guided setup

Interactive desktop/browser/phone steps now open with clear instructions and yes/no confirmation. No reopens and repeats; yes runs available checks. See GUIDED-SETUP.md. Remote access remains the separate remote_access profile after the base full installation.


An Arch/Nyarch home-server installer for an existing x86_64 systemd PC. It does not install the OS or format drives. Use the illustrated installation guide supplied with this release; this README and STORAGE.md describe current defaults. Earlier AUDIT-v*.md files are historical.

## Start

Extract this release as your normal desktop user, open its directory, and keep a private working configuration:

```bash
cp config.json my-config.json
chmod 600 my-config.json
```

Verify `media.uid` / `media.gid` against `id -u` / `id -g`, choose your existing `tv.user`, and set `timezone` in your config (default `Asia/Jerusalem`; it drives container clocks and backup/maintenance schedules) and match the host to it (for the default: `sudo timedatectl set-timezone Asia/Jerusalem`). Have supported formatted filesystems on separate application SSD/media HDD/external backup drives ready. Keep the OS disk separate. Unmount desktop automounts of the app SSD/HDD before choosing them. Never format a disk without independently identifying its contents.

If Python 3.11+ is present, preview without writes:

```bash
bash install.sh --profile full --check --config "$PWD/my-config.json"
bash install.sh --profile full --dry-run --config "$PWD/my-config.json"
```

Then, on the intended supported server:

```bash
sudo bash install.sh --profile full --apply --config "$PWD/my-config.json"
```

The shell launcher checks base prerequisites before Python starts, checks repository availability, installs missing packages using pacman, and verifies commands afterward. It includes Python, mount/block/locking tools, core utilities, tar, curl/CA certificates, OpenSSL, IP/GPU inspection. It does not refresh package databases, upgrade the OS or enable repositories. Start from a maintained host with working repositories/network; if package availability fails, repair/update the host through its normal supported process, then retry. Check/dry-run never install packages; if Python is absent, they explain how to use the apply bootstrap. Do not combine these read-only modes with --apply. Use the shell entrypoint for automatic prerequisites; direct `python3 installer.py` bypasses bootstrap.

Selected modules install their own packages (Docker/Compose, Caddy, Samba, hdparm, SMART tools, optional Tailscale/TV packages). Steam requires a configured multilib repository. The server uses the bundled verified Rust runtime; no Rust compiler is needed.

## Accounts and connected apps

[ACCOUNT-SETUP.md](ACCOUNT-SETUP.md) describes the new console flow. Generated credentials use cryptographically secure randomness. Your private inventory is `/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt`; it is created on the server and excluded from the release ZIP.


The installer asks for unassigned drives and exact UUID confirmation. It persists choices in your config and the SSD release copy. Choose generated or custom per-app credentials in Konsole. The installer provisions supported local accounts, verifies logins, saves a private per-app inventory and passes verified credentials to the media connectors. See ACCOUNT-SETUP.md for the exact apps, paths and rerun behavior. Choose content providers, LAN/certificate trust and any TV/notification/remote setup afterward. `needs_setup` means a real unfinished step; unrelated modules can continue.

```bash
# The full installer runs account setup and the guided connectors.
# To resume pending setup with the same confirmed choices:
sudo bash install.sh --retry-failed --apply --config "$PWD/my-config.json"
sudo python3 acceptance.py --profile full --save --config "$PWD/my-config.json"
```

Use the same confirmed configuration on reruns. The persistent copy is `/srv/nekoserver/installer/releases/v0.21.1/config.json`. After a normal reboot, rerun acceptance from that release with its config. Only mark witnessed checks after actually performing them. Test a request/download/import/play, music playback, phone photo upload, Nextcloud client transfer, intended access and independent restoration.

## Storage and operation

| Content | Live location | Protection |
| --- | --- | --- |
| Immich uploads and database | App SSD | Weekly HDD originals+DB, weekly USB originals; monthly complete three-drive recovery snapshot |
| Music and music downloads | App SSD | No automatic audio-file backup; app settings/playlists still protected |
| Movies/shows/downloads | HDD | No media-file backup |
| Nextcloud user files | HDD | Monthly full backup to SSD and external drive; app/database/config SSD |

PC sleep is blocked; a verified SATA HDD policy requests standby after 60 minutes idle. Ordinary apps, seeding, scans and backups can still wake it. Linux filesystem cache is normal and reclaimable; bounded journals and actual service memory should be observed on hardware, rather than scheduling cache-clearing commands.

Weekly photo jobs: Sunday 02:15 and 03:00 Asia/Jerusalem. Monthly maintenance: first day 02:30 Asia/Jerusalem. Nightly app/host-state backups remain. Routine media API audit and raw music backup are disabled. See STORAGE.md and MONTHLY-MAINTENANCE.md for exact scope. External disk absence/fullness blocks monthly updates; no automatic deletion/retention policy exists. Recovery must restore matching data and old images, not just change a tag. Major app migrations and OS updates remain separate.

## Evidence and limits

AUDIT.md and audit-evidence contain current results and identify historical files. Filesystem/HTTP/native/Compose checks are executable tests; package manager, mounts, Docker services and hardware in integration fixtures are simulated. This is ready for controlled deployment testing, not a claim that a real server has passed acceptance. No full live VM or intended hardware installation is established by this release. Bazzite migration is separate; VM/KVM is not required.
