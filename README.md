# TrueNAS SCALE Home Media Server

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

Real-Debrid, Zurg, and rclone are not included in Phase 1. See [PHASE2.md](./PHASE2.md) for the deferred Phase 2 scope and implementation notes.

## Project Highlights
* **Hardware:** Intel iGPU-focused TrueNAS media server.
* **Stack:** Jellyfin, Immich, Navidrome, qBittorrent, and the "Arr" Suite.
* **Network:** Secure remote access via Tailscale (No port forwarding).
