#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/00-common.sh"

require_root

compose_dest="$APP_SCRIPTS_DIR/docker-compose.yml"
config_env="$APP_SCRIPTS_DIR/config.env"
zurg_config="$APPDATA_DIR/zurg/config.yml"
zurg_example="$APPDATA_DIR/zurg/config.yml.example"
rclone_conf="$APPDATA_DIR/rclone/rclone.conf"
jellyfin_wait_dir="$APPDATA_DIR/jellyfin/custom-cont-init.d"
services_env="$APP_SCRIPTS_DIR/enabled-services.env"

require_path "$compose_dest"
require_path "$config_env"

mkdir -p "$APPDATA_DIR/zurg" "$APPDATA_DIR/rclone" "$jellyfin_wait_dir" /mnt/tank/realdebrid

install_file "$REPO_ROOT/templates/phase2/zurg-config.yml.example" "$zurg_example" 0640

if [ -n "${REALDEBRID_TOKEN_FILE:-}" ]; then
    [ -r "$REALDEBRID_TOKEN_FILE" ] || die "Real-Debrid token file is not readable: $REALDEBRID_TOKEN_FILE"
    token="$(tr -d '[:space:]' < "$REALDEBRID_TOKEN_FILE")"
    [ -n "$token" ] || die "Real-Debrid token file is empty: $REALDEBRID_TOKEN_FILE"
    if [ -e "$zurg_config" ] && ! grep -q 'YOUR_REAL_DEBRID_API_TOKEN' "$zurg_config"; then
        log "Keeping existing $zurg_config"
    else
        backup_existing "$zurg_config"
        sed -e "s|YOUR_REAL_DEBRID_API_TOKEN|$token|g" "$REPO_ROOT/templates/phase2/zurg-config.yml.example" > "$zurg_config"
        log "Created $zurg_config from token file"
    fi
    chmod 0640 "$zurg_config"
elif [ -e "$zurg_config" ]; then
    log "Keeping existing $zurg_config"
else
    cp "$REPO_ROOT/templates/phase2/zurg-config.yml.example" "$zurg_config"
    chmod 0640 "$zurg_config"
    log "Created $zurg_config with placeholder token; edit it before starting Zurg"
fi

install_file "$REPO_ROOT/templates/phase2/rclone.conf" "$rclone_conf" 0644
install_file "$REPO_ROOT/templates/phase2/enable-fuse-allow-other.sh" "$APP_SCRIPTS_DIR/enable-fuse-allow-other.sh" 0755
install_file "$REPO_ROOT/templates/phase2/rclone-mount.sh" "$APP_SCRIPTS_DIR/rclone-mount.sh" 0755
install_file "$REPO_ROOT/templates/phase2/verify-realdebrid.sh" "$APP_SCRIPTS_DIR/verify-realdebrid.sh" 0755
install_file "$REPO_ROOT/templates/phase2/enable-realdebrid-wait.sh" "$APP_SCRIPTS_DIR/enable-realdebrid-wait.sh" 0755
install_file "$REPO_ROOT/templates/phase2/wait-for-rd.sh" "$jellyfin_wait_dir/wait-for-rd.sh" 0755

backup_existing "$compose_dest"

if ! grep -q '/mnt/tank/realdebrid:/media/realdebrid:ro' "$compose_dest"; then
    sed -i '/\/mnt\/tank\/data\/media\/tv:\/media\/tv:ro/a\      - /mnt/tank/realdebrid:/media/realdebrid:ro\n      - /mnt/apps/appdata/rclone:/realdebrid-status:ro' "$compose_dest"
    log "Added Jellyfin Real-Debrid mounts to $compose_dest"
fi

if ! grep -q '/mnt/apps/appdata/rclone:/realdebrid-status:ro' "$compose_dest"; then
    sed -i '/\/mnt\/tank\/realdebrid:\/media\/realdebrid:ro/a\      - /mnt/apps/appdata/rclone:/realdebrid-status:ro' "$compose_dest"
    log "Added Jellyfin Real-Debrid status mount to $compose_dest"
fi

if ! grep -q '^  zurg:' "$compose_dest"; then
    {
        printf '\n'
        cat "$REPO_ROOT/templates/phase2/zurg.yml"
    } >> "$compose_dest"
    log "Added Zurg service to $compose_dest"
else
    log "Zurg service already present in $compose_dest"
fi

if [ -r "$services_env" ]; then
    if ! grep -q 'zurg' "$services_env"; then
        backup_existing "$services_env"
        current="$(sed -n 's/^HEALTHCHECK_APPS="\(.*\)"$/\1/p' "$services_env")"
        printf 'HEALTHCHECK_APPS="%s zurg"\n' "$current" > "$services_env"
        log "Added zurg to $services_env"
    fi
fi

if grep -q 'YOUR_REAL_DEBRID_API_TOKEN' "$zurg_config"; then
    log "WARNING: $zurg_config still contains the placeholder token."
fi

log "Phase 2 Real-Debrid file installation finished"
