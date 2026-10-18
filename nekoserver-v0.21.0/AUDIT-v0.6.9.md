# NekoServer v0.6.9 — Seerr request-provider setup

October 8, 2026. **216 automated tests pass**, including 38 new tests for this release. All previous storage, preservation, recovery, source-import, media-connection and library fixes are included. Historical audits remain in the package.

## Connected onboarding steps

Interactive media/full installation now runs these steps from the preserved SSD installer:

1. Wait for deployed media services and report availability.
2. Collect media sources and copy only explicitly selected folders, add-only with checksums.
3. Authenticate an existing Jellyfin administrator and configure/verify its libraries; verify Seerr's selected Jellyfin server and library flags.
4. Configure Arr root folders, Prowlarr connections and qBittorrent paths/categories.
5. Configure and verify missing Seerr Sonarr/Radarr request providers using those Arr roots.
6. Save the combined private readiness report, retaining independent failures and remaining real-server acceptance tasks.

The new provider step is controlled by `onboarding.configure_request_providers` (default true) and requires Seerr plus a selected Sonarr/Radarr service. Headless installation remains report-only without credential/profile prompts or API configuration.

## Provider behavior

API keys come from the applications' existing settings files and remain in memory or the target application's own configuration. Requests are local, ignore environment proxies and do not follow redirects. Reports and errors exclude keys and API response bodies.

Seerr must already have completed its first-run login/server-selection wizard. The helper checks the media manager version, base URL, accessible expected root and live quality profiles. It calls Seerr's connection-test API using Docker hostnames `sonarr:8989` and `radarr:7878`. Seerr must return matching profiles, roots and base URL before a provider can be saved. This tests from Seerr's container rather than assuming host-loopback access proves Docker connectivity.

A sole live quality profile is selected automatically. Multiple profiles prompt for a choice; skipping leaves that provider incomplete while the other continues. Sonarr 3 additionally selects a live language profile; Sonarr 4 omits that obsolete field. Other Sonarr major versions are conservatively left for manual setup.

Only an absent standard provider is created, with the expected root, `isDefault=true`, `is4k=false`, `preventSearch=false` and `syncEnabled=false`. New Sonarr providers enable season folders; new Radarr providers use minimum availability `released`. No request, title search, torrent or download is submitted by this helper. Future user requests can use the configured provider.

Existing standard providers are reused only when the local connection/key, unique default routing, expected root and live selected profiles validate. Other hosts/keys, custom roots, duplicate standard records, nondefault routing or a disabled/unknown search policy are preserved and reported for review. Existing 4K providers keep their settings. The helper rechecks the provider list before saving, reads all saved fields back and retests the retained connection. It does not roll back a provider after a verification failure; rerunning verifies any retained record and prevents duplicate creation.

## Validation and limits

All 216 tests pass. The 38 new tests cover default creation, readback, reruns, previews, ambiguous/skipped profiles, Sonarr 3/4 behavior, unsupported versions, custom routing/roots/keys, 4K preservation, duplicate records, inaccessible roots, Seerr connection failures, failed retention, concurrent settings changes, independent provider failures, onboarding ordering/report retention, headless behavior and preserved helper imports.

Isolated local HTTP tests exercise the complete helper through Seerr and Arr API routes with real JSON/header handling, including successful provisioning/reruns and an independently failing provider. They simulate application responses and do not run actual Docker images.

Compilation, shell syntax, six profile plans in check/dry-run mode, eight rendered media templates and generated systemd unit syntax also pass (23 release checks). Archive integrity and packaged checksums are checked before delivery.

Real Nyarch deployment, actual Compose validation/startup and image compatibility, GPU acceleration, reboot/missing-disk tests, acquisition/import/playback and a full isolated Immich restore remain unverified. Image version/digest pinning is still outstanding. Preserve original photos until restoration is proven. Initial administrators, Seerr login/server selection, any Arr/qBittorrent first-run authentication, authenticated remote mounts and Tailscale/Samba credentials still need user input. TV/Steam/controller setup and Bazzite recovery remain later phases; the remote browser gateway remains deferred.

## Usage

```bash
python3 -m unittest discover -s tests -v
bash install.sh --profile media --check
bash install.sh --profile media --dry-run
sudo bash install.sh --profile media --apply
```

After completing required web wizards, rerun the complete connection/library/provider setup:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.9/onboard.py --configure --libraries --request-providers --apply
```

To preview provider creation after the Arr roots already exist:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.6.9/onboard.py --request-providers
```

Preview performs read-only connection-test POSTs, not settings writes. Add `--apply` to save absent providers. Use `--sources --indexers` alongside `--configure` to repeat those guided workflows. Reports live under the SSD's `reports/` directory. Exit 1 still means setup or real end-to-end verification remains; exit 2 means a check failed. Successful provider configuration is not proof of completed requests or playback.

Primary API references consulted:

- Seerr OpenAPI: https://raw.githubusercontent.com/seerr-team/seerr/refs/heads/develop/seerr-api.yml
- Seerr Radarr implementation: https://github.com/seerr-team/seerr/blob/develop/server/routes/settings/radarr.ts
- Seerr Sonarr implementation: https://github.com/seerr-team/seerr/blob/develop/server/routes/settings/sonarr.ts
