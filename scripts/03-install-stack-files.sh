#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/00-common.sh"

require_root

mkdir -p "$APP_SCRIPTS_DIR"

tz="$(detect_timezone)"
render_gid="$(detect_render_gid)"
config_dest="$APP_SCRIPTS_DIR/config.env"
compose_dest="$APP_SCRIPTS_DIR/docker-compose.yml"
phase2_active="0"

if [ ! -e "$config_dest" ]; then
    sed \
        -e "s|__TZ__|$tz|g" \
        -e "s|__RENDER_GID__|$render_gid|g" \
        "$REPO_ROOT/templates/config.env.example" > "$config_dest"
    chmod 0640 "$config_dest"
    log "Created $config_dest"
else
    log "Keeping existing $config_dest"
fi

backup_existing "$compose_dest"
sed -e "s|__RENDER_GID__|$render_gid|g" "$REPO_ROOT/templates/docker-compose.yml" > "$compose_dest"

if [ -r "$APPDATA_DIR/zurg/config.yml" ] && [ -r "$APPDATA_DIR/rclone/rclone.conf" ]; then
    phase2_active="1"
    sed -i '/\/mnt\/tank\/data\/media\/tv:\/media\/tv:ro/a\      - /mnt/tank/realdebrid:/media/realdebrid:ro\n      - /mnt/apps/appdata/rclone:/realdebrid-status:ro' "$compose_dest"
    {
        printf '\n'
        cat "$REPO_ROOT/templates/phase2/zurg.yml"
    } >> "$compose_dest"
    log "Preserved Phase 2 Real-Debrid compose entries"
fi

for optional in ${INSTALL_WITH:-}; do
    fragment="$REPO_ROOT/templates/optional/$optional.yml"
    [ -r "$fragment" ] || die "Optional compose fragment missing: $fragment"
    {
        printf '\n'
        cat "$fragment"
    } >> "$compose_dest"
    log "Enabled optional service: $optional"
done

chmod 0644 "$compose_dest"
log "Installed $compose_dest"

if [ "$phase2_active" = "1" ]; then
    log "Phase 2 is active; rerunning base kept Zurg and Jellyfin Real-Debrid mounts."
fi
