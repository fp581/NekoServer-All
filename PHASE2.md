# Phase 2: Real-Debrid, Zurg, and rclone

Phase 2 is not installed by the Phase 1 scripts. Do this only after Phase 1 proves that local media playback works, qBittorrent downloads import correctly, and the app config backup succeeds.

## Not Included In Phase 1

- Real-Debrid account setup and API token handling.
- Zurg container enablement.
- Zurg `/mnt/apps/appdata/zurg/config.yml`.
- rclone host installation.
- rclone WebDAV config.
- FUSE `user_allow_other` setup.
- TrueNAS Init/Shutdown script registration.
- Real-Debrid mount lifecycle management.
- Jellyfin startup wait for the Real-Debrid mount.
- Jellyfin Real-Debrid libraries.
- Prowlarr Real-Debrid indexer setup.
- `WAIT_FOR_RD=1` activation.
- Recovery steps for failed WebDAV, failed FUSE mounts, and stale mount points.

## Required Inputs

- Real-Debrid account.
- Real-Debrid API token from `https://real-debrid.com/apitoken`.
- Working Phase 1 stack.
- Working local Jellyfin playback.
- Working Sonarr/Radarr import from qBittorrent.
- A successful `/mnt/apps/scripts/backup-app-config.sh` run.

## Target Files For Phase 2

These files should be added or generated in the final Phase 2 implementation:

- `/mnt/apps/appdata/zurg/config.yml`
- `/mnt/apps/appdata/rclone/rclone.conf`
- `/mnt/apps/scripts/enable-fuse-allow-other.sh`
- `/mnt/apps/scripts/rclone-mount.sh`
- `/mnt/apps/appdata/jellyfin/custom-cont-init.d/wait-for-rd.sh`
- A Phase 2 compose fragment or regenerated compose file that adds `zurg`.

## Important Design Fix

Do not use a `.mount-test` marker inside `/mnt/tank/realdebrid` as the only readiness signal. That path is the FUSE mount target and can exist even when the mount is broken.

The safer final Phase 2 design should:

- Check `mountpoint -q /mnt/tank/realdebrid`.
- Verify Zurg responds on `http://localhost:9999`.
- Write a host-side readiness marker under `/mnt/apps/appdata/rclone`, for example `/mnt/apps/appdata/rclone/realdebrid-mounted`.
- Mount that marker into Jellyfin read-only if Jellyfin needs startup gating.

## Manual Phase 2 Outline

1. Create `/mnt/apps/appdata/zurg/config.yml` with the Real-Debrid token.
2. Create `/mnt/apps/appdata/rclone/rclone.conf`:

   ```ini
   [zurg]
   type = webdav
   url = http://localhost:9999/dav
   vendor = other
   ```

3. Install `rclone` on the TrueNAS host if it is missing.
4. Enable FUSE `user_allow_other`.
5. Add and start the `zurg` service.
6. Register the rclone mount script as a TrueNAS Post Init script.
7. Verify:

   ```bash
   curl http://localhost:9999
   mountpoint -q /mnt/tank/realdebrid
   ls /mnt/tank/realdebrid
   ```

8. Set `WAIT_FOR_RD="1"` in `/mnt/apps/scripts/config.env`.
9. Add Jellyfin libraries:
   - Movies: `/media/realdebrid/movies`
   - Shows: `/media/realdebrid/shows`
10. Add Real-Debrid as a priority 1 Prowlarr indexer.

## Troubleshooting To Include In Final Phase 2

- `rclone` missing from host.
- `/etc/fuse.conf` does not contain `user_allow_other`.
- Zurg starts but returns no Real-Debrid content.
- rclone uses `http://zurg:9999/dav` instead of `http://localhost:9999/dav`.
- `/mnt/tank/realdebrid` exists but is not a mount.
- FUSE mount is stale and needs unmount/remount.
- Jellyfin starts before the Real-Debrid mount is ready.
