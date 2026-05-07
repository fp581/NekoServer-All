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
