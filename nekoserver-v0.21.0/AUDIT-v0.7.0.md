# NekoServer v0.7.0 — TV and media readiness audit

October 8, 2026. **258 automated tests pass**, including 42 new tests. All earlier fixes remain included; historical audits are retained in the package.

## Connected behavior

The installer now has a `tv` profile, and `full` runs TV setup before onboarding. TV depends on system checks and the persistent recovery bundle, which depends on storage. It can run independently of media onboarding failures. Existing configurations do not silently enable this new module for `--all`; explicit profiles select it.

Native TV installation supports x86_64 Nyarch/Arch only. It selects an existing non-root desktop user, validates home/directory ownership and shortcut paths before package changes, checks configured repositories, installs missing packages and verifies retention. Steam/Firefox launchers are created for that user; the installer never launches those GUI applications as root. Different existing shortcuts, symlinks, incompatible ownership and nonexecutable matching files require review. Reruns reuse matching launchers and installed packages.

Steam autostart is off by default. An existing autostart is preserved and flagged when disabled in configuration. Bluetooth service setup does not pair devices. Optional GNOME power setup verifies that automatic AC suspend was disabled; unavailable or locked settings are reported for manual setup. HDMI/audio, login, Remote Play and controller behavior are explicitly incomplete until tested. Details are in TV-SETUP.md.

Current media readiness is shared between onboarding reports and scheduled monitoring. It reads Jellyfin's public identity/wizard state, authenticates library reads with Seerr's stored Jellyfin application key, verifies expected library paths/types/IDs, checks Seerr's selected server and enabled library IDs, and verifies existing Sonarr/Radarr providers against current roots and quality profiles. Provider connectivity uses GET `/service/sonarr/:id` or `/service/radarr/:id`. Missing providers never prompt or trigger configuration from monitoring. A client guard rejects any non-GET call.

Monitoring no longer relies on generic library/account placeholders or old successful setup reports. Both health summary counts and CLI output include current readiness. The scheduled module copies all imported helper dependencies beside the health script. Reapply `media_monitor` to update an earlier deployed script. Setup commands retain their authorized POST/PUT configuration workflow; scheduled checks remain read-only.

Local authenticated clients ignore environment proxies and reject redirects. Keys remain in memory or applications' own protected configuration; reports omit credentials and remote error bodies. HTTP errors are closed. qBittorrent/Navidrome authenticated transfer/playback and real media acquisition/import/playback remain explicit acceptance tasks.

## Validation and limits

All 258 tests pass. New coverage includes desktop user/ownership, reruns, custom shortcuts, symlinks, package availability/failure/retention, optional autostart/Bluetooth/power behavior, dependency ordering, current library IDs/paths/server identity, missing or failing providers, credential redaction, GET-only enforcement, health summaries and preserved monitoring imports. Local HTTP fixtures exercise real JSON/authentication headers and independent provider failures without allowing writes. They simulate services rather than running actual Docker images.

Compilation, shell syntax, all seven profile plans in check/dry-run mode, eight rendered media Compose templates, generated systemd fixture syntax and desktop entry parsing pass. Package installation, user ownership and power/service operations are simulated in tests; no host packages or live production units are installed. Archive integrity and all packaged checksum entries are verified before delivery.

Actual Nyarch installation, package availability on that machine, live Compose startup/image compatibility, Intel media acceleration, Steam display/audio/controller streaming, reboot/missing-disk behavior, downloads/imports and an isolated full Immich restore remain unverified. Image version/digest pinning, Bazzite recovery and a remote browser gateway remain outstanding. Preserve original photos until restoration is proven. First administrators, Seerr login/server selection, application authentication, remote mounts and Tailscale/Samba credentials require the user's input.

## Usage

```bash
python3 -m unittest discover -s tests -v
bash install.sh --profile full --check
bash install.sh --profile full --dry-run
sudo bash install.sh --profile tv --apply
sudo bash install.sh --profile media_monitor --apply
```

After web wizards, run `sudo python3 onboard.py --configure --libraries --request-providers --apply` from this bundle. For read-only current checks, run `sudo python3 media-health.py --config config.json --json-out /srv/nekoserver/reports/media/manual.json`. TV and media reports remain incomplete for real-machine acceptance even when individual software/configuration checks pass. A needs_setup result does not undo successful independent installation steps.

Primary references consulted:

- Steam package/dependencies: https://archlinux.org/packages/multilib/x86_64/steam/
- Arch Steam setup: https://wiki.archlinux.org/title/Steam
- Big Picture and troubleshooting: https://wiki.archlinux.org/title/Steam/Troubleshooting
- Controller behavior: https://wiki.archlinux.org/title/Gamepad
- Valve Remote Play guide: https://help.steampowered.com/ms/faqs/view/0689-74B8-92AC-10F2
- Seerr GET service connectivity: https://docs.seerr.dev/api/get-sonarr-server-quality-profiles-and-root-folders/
- Seerr OpenAPI: https://raw.githubusercontent.com/seerr-team/seerr/refs/heads/develop/seerr-api.yml
