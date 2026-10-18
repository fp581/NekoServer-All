# NekoServer v0.6.8 — authenticated library handoff

October 8, 2026. **178 automated tests pass**, including 28 added for this release. This release includes all v0.6.6 and v0.6.7 fixes. Their audit documents are retained as history.

## What changed

Interactive media/full onboarding now offers authenticated Jellyfin library setup through `--libraries`. It checks that the first-run wizard is complete, prompts for an existing administrator and verifies administrator privileges before using its session token. Missing Movies and TV Shows libraries are created against `/data/movies` and `/data/tv`, then read back. An existing correctly typed library at either path is reused even if renamed. Conflicting types, duplicate paths or conflicting names are preserved and reported for review. Each category is independent.

When Seerr is deployed, the helper reads its existing API key from its own settings file. It verifies that Seerr's Jellyfin server ID matches the local Jellyfin server before any writes. Applied runs refresh Seerr's library list with POST, enable only the two verified Jellyfin library IDs with PUT, and read enabled flags back. Other libraries retain their flags. Missing first-run settings or a different selected server are reported as incomplete; no account or server selection is overwritten.

The helper is saved with the persistent installer on the SSD. Its results survive the subsequent Arr connector step in the final readiness report. Library-only deployment configurations no longer require an Arr connector. Headless installation still uses report-only mode and never prompts for accounts.

Credentials are entered through a hidden password prompt and remain in memory. API errors and reports omit credentials, tokens and response bodies. New authenticated clients refuse redirects and ignore environment proxies; Jellyfin sessions are logged out on exit where the service allows it. Preview may authenticate a session, but does not create libraries, sync Seerr or change enabled flags.

## What remains manual / unverified

- Create the first Jellyfin administrator in its own wizard at `http://127.0.0.1:8096`. The installer does not call startup-user APIs that could reset an existing account. Complete Seerr's login and select this Jellyfin server using its web wizard. Within Docker, Jellyfin is reachable as `jellyfin:8096`.
- Configure/verify Seerr's Sonarr and Radarr request providers. This release does not provision those providers or mark a successful library sync as a successful media request.
- Jellyfin library creation does not start an automatic scan (`refreshLibrary=false`). Scan from Jellyfin and test real content, imports and playback. Empty libraries can be configured successfully without proving playback.
- Complete other first-run accounts where required, including Navidrome and Immich. Arr/qBittorrent may require their initial authentication choices before the existing connectors can run.
- No real Docker daemon, Nyarch machine or application instance was available. API fixtures validate request handling and reruns, rather than deployed image compatibility. Images still use moving tags; version/digest pinning and real Compose/container validation remain outstanding.
- Hardware/GPU, reboot/missing-disk behavior, remote share permissions and authenticated mounts, complete Immich restore and deletion safety, external backup reconnects and OS replacement still require acceptance tests. Keep original photos until a full restore is proven.
- The TV/Steam/controller experience, Bazzite recovery backend and authenticated remote browser gateway remain future work.

## Validation

All 178 automated tests pass. New cases cover fresh-wizard refusal, blank/skipped credentials, non-administrator rejection, library creation/readback, previews, reruns, conflicts and symlink paths, wrong Seerr server selection, missing/symlink settings, failed sync/retention, independent service selection, preserved helpers and integrated reports. Isolated local HTTP tests check methods, JSON, authentication headers, session logout, redirect refusal and error redaction.

Release evidence also records compilation, shell syntax, six profile plans in check/dry-run mode, eight rendered media templates, generated systemd unit syntax and ZIP integrity. These are software checks, not production acceptance.

## Usage

```bash
python3 -m unittest discover -s tests -v
bash install.sh --profile media --check
bash install.sh --profile media --dry-run
sudo bash install.sh --profile media --apply
```

After completing the first-run web wizards, rerun from the preserved SSD installer:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.8/onboard.py --configure --libraries --apply
```

To preview only authenticated library setup, omit `--apply`:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.8/onboard.py --libraries
```

Use `--sources --indexers` with `--configure` to repeat the existing source/catalog workflow. Previously selected source copies remain add-only and checksum-verified. Reports are private files under the SSD's `reports/` directory. Exit 1 means setup or end-to-end validation remains; exit 2 means a check failed. Incomplete installation modules remain retryable.

Primary API references consulted:

- Jellyfin SDK library API: https://typescript-sdk.jellyfin.org/classes/generated-client.LibraryStructureApi.html
- Jellyfin startup API: https://typescript-sdk.jellyfin.org/classes/generated-client.StartupApi.html
- Seerr OpenAPI: https://raw.githubusercontent.com/seerr-team/seerr/refs/heads/develop/seerr-api.yml
- Seerr authentication implementation: https://github.com/seerr-team/seerr/blob/develop/server/routes/auth.ts
