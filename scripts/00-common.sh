#!/usr/bin/env bash

APP_SCRIPTS_DIR="${APP_SCRIPTS_DIR:-/mnt/apps/scripts}"
APPDATA_DIR="${APPDATA_DIR:-/mnt/apps/appdata}"
TANK_DIR="${TANK_DIR:-/mnt/tank}"
REPO_ROOT="${REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"

log() {
    printf '[install] %s\n' "$*"
}

die() {
    printf '[install] ERROR: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "Run this installer as root, e.g. sudo bash install.sh"
}

require_path() {
    [ -e "$1" ] || die "Required path is missing: $1"
}

backup_existing() {
    target="$1"
    [ -e "$target" ] || return 0
    stamp="$(date +%Y%m%d-%H%M%S)"
    cp -a "$target" "$target.bak-$stamp"
    log "Backed up $target to $target.bak-$stamp"
}

install_file() {
    src="$1"
    dest="$2"
    mode="${3:-0644}"
    mkdir -p "$(dirname -- "$dest")"
    backup_existing "$dest"
    cp "$src" "$dest"
    chmod "$mode" "$dest"
    log "Installed $dest"
}

detect_timezone() {
    if [ -r /etc/timezone ]; then
        tz="$(tr -d '\n' </etc/timezone)"
        [ -n "$tz" ] && { printf '%s\n' "$tz"; return; }
    fi

    if command -v timedatectl >/dev/null 2>&1; then
        tz="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
        [ -n "$tz" ] && { printf '%s\n' "$tz"; return; }
    fi

    if [ -L /etc/localtime ]; then
        tz="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
        [ -n "$tz" ] && [ "$tz" != "/etc/localtime" ] && { printf '%s\n' "$tz"; return; }
    fi

    printf 'Europe/Mariehamn\n'
}

detect_render_gid() {
    if getent group render >/dev/null 2>&1; then
        getent group render | awk -F: '{print $3}'
        return
    fi

    printf '107\n'
}

optional_enabled() {
    needle="$1"
    for item in ${INSTALL_WITH:-}; do
        [ "$item" = "$needle" ] && return 0
    done
    return 1
}
