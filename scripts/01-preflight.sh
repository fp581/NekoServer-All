#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/00-common.sh"

require_root

log "Running preflight checks"
require_path /mnt/apps
require_path /mnt/tank
require_path /mnt/apps/appdata
require_path /mnt/apps/scripts
require_path /mnt/apps/transcode
require_path /mnt/apps/downloads-incomplete
require_path /mnt/tank/data
require_path /mnt/tank/photos
require_path /mnt/tank/backups

for cmd in bash cp chmod chown date find getent mkdir sed awk; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd"
done

if ! command -v docker >/dev/null 2>&1; then
    log "WARNING: docker was not found. TrueNAS Apps deployment may still provide it later, but shell checks will be limited."
fi

if [ ! -e /dev/dri/renderD128 ]; then
    log "WARNING: /dev/dri/renderD128 is missing. Hardware transcoding will not work until the Intel iGPU is visible."
fi

if ! getent group render >/dev/null 2>&1; then
    log "WARNING: render group not found. Falling back to RENDER_GID=107 in generated files."
fi

log "Preflight checks finished"
