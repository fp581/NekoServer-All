#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/00-common.sh"

require_root

log "Creating folders"
mkdir -p /mnt/apps/appdata/{bazarr,clamav,immich-db,immich-ml,jellyfin,lidarr,navidrome,profilarr,prowlarr,qbittorrent,radarr,rclone,seerr,sonarr,tailscale,zurg}
mkdir -p /mnt/apps/{backups,scripts,transcode/jellyfin,downloads-incomplete}
mkdir -p /mnt/tank/data/media/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/complete/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/quarantine
mkdir -p /mnt/tank/photos/library
mkdir -p /mnt/tank/backups/configs
mkdir -p /mnt/tank/realdebrid

log "Applying permissions"
chown -R 568:568 /mnt/apps/appdata /mnt/apps/transcode /mnt/apps/downloads-incomplete
chown -R 568:568 /mnt/tank/data /mnt/tank/photos /mnt/tank/realdebrid
chmod -R 775 /mnt/apps/appdata /mnt/apps/transcode /mnt/apps/downloads-incomplete
chmod -R 775 /mnt/tank/data /mnt/tank/photos /mnt/tank/realdebrid

script_owner="${SUDO_USER:-$(id -un)}"
if id "$script_owner" >/dev/null 2>&1; then
    chown -R "$script_owner":568 /mnt/apps/scripts
else
    chown -R root:568 /mnt/apps/scripts
fi
chmod -R 775 /mnt/apps/scripts

chown -R 999:999 /mnt/apps/appdata/immich-db

log "Folder setup finished"
