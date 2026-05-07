#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/00-common.sh"

require_root

log "Installing maintenance scripts"
install_file "$REPO_ROOT/templates/maintenance/backup-app-config.sh" "$APP_SCRIPTS_DIR/backup-app-config.sh" 0755
install_file "$REPO_ROOT/templates/maintenance/photo-backup-usb.sh" "$APP_SCRIPTS_DIR/photo-backup-usb.sh" 0755
install_file "$REPO_ROOT/templates/maintenance/cleanup-downloads.sh" "$APP_SCRIPTS_DIR/cleanup-downloads.sh" 0755
install_file "$REPO_ROOT/templates/maintenance/health-check.sh" "$APP_SCRIPTS_DIR/health-check.sh" 0755

if optional_enabled clamav; then
    install_file "$REPO_ROOT/templates/maintenance/scan-downloads.sh" "$APP_SCRIPTS_DIR/scan-downloads.sh" 0755
fi

apps="jellyfin navidrome immich-server immich-db immich-redis immich-machine-learning qbittorrent prowlarr sonarr radarr lidarr bazarr seerr tailscale"
for optional in ${INSTALL_WITH:-}; do
    apps="$apps $optional"
done

services_env="$APP_SCRIPTS_DIR/enabled-services.env"
backup_existing "$services_env"
printf 'HEALTHCHECK_APPS="%s"\n' "$apps" > "$services_env"
chmod 0644 "$services_env"
log "Installed $services_env"
