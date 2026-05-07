# TrueNAS SCALE Home Media Server Alpha

Script-first setup files for a TrueNAS Community Edition home media server.

The original full walkthrough is still available in [guide.md](./guide.md). The runnable Phase 1 install flow is now:

```bash
sudo bash install.sh
```

Phase 1 assumes TrueNAS is installed and the required pools/datasets already exist. It creates folders, permissions, `/mnt/apps/scripts/config.env`, `/mnt/apps/scripts/docker-compose.yml`, and the maintenance scripts.

Optional services are explicit:

```bash
sudo bash install.sh --with flaresolverr
sudo bash install.sh --with profilarr
sudo bash install.sh --with clamav
```

After installation, follow [docs/manual-steps.md](./docs/manual-steps.md) for TrueNAS UI tasks such as Apps deployment, snapshot tasks, and cron jobs.

Real-Debrid, Zurg, and rclone are installed as a separate Phase 2 after local playback and imports work:

```bash
sudo bash install.sh --phase realdebrid --realdebrid-token-file /root/realdebrid-token
```

If you omit `--realdebrid-token-file`, edit `/mnt/apps/appdata/zurg/config.yml` before starting Zurg. See [docs/manual-steps.md](./docs/manual-steps.md) for the remaining TrueNAS UI steps.

## Project Highlights
* **Hardware:** Intel iGPU-focused TrueNAS media server.
* **Stack:** Jellyfin, Immich, Navidrome, qBittorrent, and the "Arr" Suite.
* **Network:** Secure remote access via Tailscale (No port forwarding).
