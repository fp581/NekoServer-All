# NekoServer v0.6.7 — installation handoff fixes

October 8, 2026. Continues the v0.6.6 audit. **150 automated tests pass**, including 39 new tests for this release. The earlier audit remains in `AUDIT-v0.6.6.md` as history; current behavior below takes precedence.

## Completed handoffs

- **Storage → preserved installer:** the recovery module saves the installer, helper scripts, modules, templates, documentation and current configuration under `/srv/nekoserver/installer/releases/v0.6.7`. Code files are immutable on rerun. Configuration is updated atomically with owner-only permissions. Different existing code is preserved and blocks replacement.
- **Installer → persistent state:** after successful storage application, default logs and module state continue under `/srv/nekoserver/installer/state`. An already mounted SSD is used to find prior state, including on a replaced OS. An explicitly chosen `--state-dir` remains honored. Recovering an unmounted SSD still requires mounting it through the foundation profile first.
- **Media deployment → onboarding:** the media and full profiles now invoke onboarding from the preserved SSD release. Interactive runs collect sources, offer Prowlarr catalog selection, and configure root folders and media connections. Headless runs only produce readiness reports and do not prompt for passwords.
- **Startup → readiness:** onboarding can wait up to a configured bounded interval for application HTTP responses. Reachability is distinct from authenticated integration/playback.
- **Arr apps → root folders:** add `/data/tv`, `/data/movies`, and `/data/music` through the application APIs and read them back. Preserve existing roots. Lidarr uses live quality/metadata profiles, asking for a choice when multiple profiles exist. API incompatibility is reported as incomplete.
- **qBittorrent → verified downloader:** use cookie authentication, check default/incomplete download paths, create absent tv/movies/music categories, and verify saved settings before registering the client with Arr. Secrets remain in memory or the target application's own settings. Authentication controls are not disabled.
- **Source inventory → HDD libraries:** the source wizard records explicit per-source copy choices. During applied onboarding, those selected folders are checksum-verified and copied into a stable source namespace inside the corresponding library. Original files remain intact, existing destination content is never replaced, and interrupted temporary copies can be retried. Preview runs report new files/bytes without copying.
- **Remote source → mounted-folder verification:** an SMB/NFS reference can include its already mounted local folder. The importer verifies cifs/nfs/nfs4 before reading it. Merely saving a remote URL does not make it accessible.
- **Incomplete setup → retry:** incomplete onboarding is recorded as `needs_setup`, returns a non-success installation result and is included by `--retry-failed`. Failed prerequisites still block dependent modules; independent modules still continue.

## Important behavior

The downloader's expected default path is `/data/downloads`; categories may use `/data/downloads/tv`, `/data/downloads/movies` and `/data/downloads/music`. Existing custom paths/categories are preserved and require review. Existing torrents prevent automatic default/incomplete path changes. The helpers never submit torrent downloads, search for titles or change existing torrent locations.

Library imports copy whole selected folders into `library/<category>/source-<stable-id>`. This consumes HDD space and does not register movie/series/artist records in Arr databases. Jellyfin/Navidrome library setup and scans, and any Arr library import decisions, are still required. Source labels and private URLs are not placed in copy reports. Copies can be substantial; only choose folders you intend to preserve on the HDD.

Imported files/directories use the configured media UID/GID. Only newly created import directories receive ownership changes; original folders and existing library/import directory permissions are preserved. Conflicts, symlinks, unavailable mounts, insufficient space and changed files produce incomplete reports. Files named as temporary/partial transfers are skipped.

The installer waits for basic HTTP readiness, not account initialization. API-key files can exist while an application still needs its initial authentication wizard. Complete required application setup locally and rerun the SSD onboarding script.

## Verified here

- 150 unit/integration tests, with temporary filesystem fixtures and simulated external command responses where physical hardware was required.
- Preserved bundle can run the full dependency check without the original extracted package.
- Actual local HTTP fixtures exercise Arr request headers/payloads and qBittorrent cookie login, preferences, categories, logout and authentication failure.
- Root folder creation/readback, preview behavior, existing roots and Lidarr profile payloads.
- Installer state relocation, incomplete setup retries, startup wait logic, interactive/headless command selection and refreshed onboarding reports.
- Local copy preservation, reruns, conflicts, symlinks, free-space refusal and selected-source handoffs. Network mount type detection was simulated; no actual NAS was available.
- Profile planning, rendered Compose structure, compilation, shell syntax and generated systemd definitions are checked separately in release evidence.

No real Docker/Compose daemon, Nyarch machine, GPU, NAS, torrent client or Immich instance was available. Live API fixtures prove this code's request handling, not compatibility with every installed application version.

## What still needs the server / next phase

1. Initial accounts and authentication for Jellyfin, Navidrome, Seerr, Immich, qBittorrent and any Arr applications that demand first-run setup.
2. Jellyfin library creation, Seerr authentication/linking, and actual playback/import tests. This release does not automate their account APIs.
3. Authenticated remote share mounting. Existing mounts can be imported, but mounting and credentials are not provisioned automatically.
4. Image version/digest pinning, actual Compose validation and container startup, Intel GPU acceleration and reboot/missing-disk tests.
5. Photo preservation/deletion behavior, external backup scenarios and a complete isolated Immich restore before deleting original photos.
6. Tailscale sign-in and SMB password setup, plus live share permission/interface checks. Browser gateway remains deferred.
7. Reinstalling host packages/services and restoring systemd/network state after OS replacement. Preserving the installer makes recovery repeatable; it does not implement a Bazzite backend or prove a full restore.
8. Steam/controller/TV experience and a user-facing management dashboard.

## Usage

From the extracted release:

```bash
python3 -m unittest discover -s tests -v
bash install.sh --profile media --check
bash install.sh --profile media --dry-run
sudo bash install.sh --profile media --apply
```

The applied media profile includes interactive onboarding. Disk assignments are confirmed before mounts are configured. Existing unformatted drives are not formatted automatically. Tailscale/Samba/photos remain separate profiles or part of `full`, with the prerequisites described in the earlier audit.

After deployment, repeat onboarding from the SSD:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.7/onboard.py --configure --sources --indexers --apply
```

To preview/import previously saved source inventory without connection setup:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.7/source_import.py --selected-only
sudo python3 /srv/nekoserver/installer/releases/v0.6.7/source_import.py --selected-only --apply
```

`source_import.py` without `--selected-only` considers all local/mounted entries in the saved inventory; use that only when you intend to import them all. Onboarding always filters to selected entries unless `--import-sources` is explicitly supplied.

Reports live under the SSD's `reports/` directory. A nonzero readiness result can mean setup remains, even though installation and specific API connections succeeded.

Primary references consulted:

- qBittorrent WebUI API: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
- Radarr API: https://radarr.video/docs/api/
- Lidarr API: https://lidarr.audio/docs/api/

For the earlier storage/backup corrections and their sources, see `AUDIT-v0.6.6.md`.
