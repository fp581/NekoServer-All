#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=/dev/null
source /mnt/apps/scripts/config.env

if [ -r /mnt/apps/scripts/enabled-services.env ]; then
    # shellcheck source=/dev/null
    source /mnt/apps/scripts/enabled-services.env
fi

LOG="/mnt/apps/scripts/health-check.log"
APPS="${HEALTHCHECK_APPS:-jellyfin navidrome immich-server immich-db immich-redis immich-machine-learning qbittorrent prowlarr sonarr radarr lidarr bazarr seerr tailscale}"

send_alert() {
    message="$1"
    [ -n "${WEBHOOK_URL:-}" ] || return 0
    command -v curl >/dev/null 2>&1 || return 0

    case "$WEBHOOK_URL" in
        https://ntfy.sh/*|http://ntfy.sh/*)
            curl -sf -d "$message" "$WEBHOOK_URL" >/dev/null || true
            ;;
        *discord.com/api/webhooks*|*discordapp.com/api/webhooks*)
            payload="$(printf '{"content":"%s"}' "$message")"
            curl -sf -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$payload" >/dev/null || true
            ;;
        *)
            payload="$(printf '{"text":"%s"}' "$message")"
            curl -sf -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$payload" >/dev/null || true
            ;;
    esac
}

echo "[health] Check $(date)" >> "$LOG"

for app in $APPS; do
    if docker ps --format '{{.Names}}' | grep -qx "$app"; then
        echo "[health] OK: $app" >> "$LOG"
    else
        echo "[health] DOWN: $app" >> "$LOG"
        send_alert "NAS: $app is down"
    fi
done
