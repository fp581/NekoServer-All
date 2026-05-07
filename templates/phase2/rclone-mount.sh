#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=/dev/null
source /mnt/apps/scripts/config.env

LOG="/mnt/apps/scripts/rclone-mount.log"
MOUNTPOINT="/mnt/tank/realdebrid"
CONFIG="/mnt/apps/appdata/rclone/rclone.conf"
READY_MARKER="/mnt/apps/appdata/rclone/realdebrid-mounted"

log() {
    printf '[realdebrid] %s\n' "$*" | tee -a "$LOG"
}

fail() {
    log "ERROR: $*"
    exit 1
}

mkdir -p "$MOUNTPOINT" "$(dirname "$READY_MARKER")"
rm -f "$READY_MARKER"

command -v rclone >/dev/null 2>&1 || fail "rclone is not installed on the TrueNAS host"
[ -r "$CONFIG" ] || fail "missing rclone config: $CONFIG"

if ! grep -q '^user_allow_other$' /etc/fuse.conf 2>/dev/null; then
    fail "/etc/fuse.conf does not enable user_allow_other; run /mnt/apps/scripts/enable-fuse-allow-other.sh"
fi

log "Waiting for Zurg on http://localhost:9999"
for _ in $(seq 1 60); do
    if curl -fsS http://localhost:9999 >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

curl -fsS http://localhost:9999 >/dev/null 2>&1 || fail "Zurg is not responding on http://localhost:9999"

if mountpoint -q "$MOUNTPOINT"; then
    if ls "$MOUNTPOINT" >/dev/null 2>&1; then
        log "$MOUNTPOINT is already mounted"
    else
        log "$MOUNTPOINT is mounted but not readable; attempting stale mount cleanup"
        if command -v fusermount >/dev/null 2>&1; then
            fusermount -uz "$MOUNTPOINT" >> "$LOG" 2>&1 || true
        elif command -v fusermount3 >/dev/null 2>&1; then
            fusermount3 -uz "$MOUNTPOINT" >> "$LOG" 2>&1 || true
        else
            umount -l "$MOUNTPOINT" >> "$LOG" 2>&1 || true
        fi
    fi
fi

if mountpoint -q "$MOUNTPOINT"; then
    log "$MOUNTPOINT is ready"
else
    log "Starting rclone mount"
    rclone mount zurg: "$MOUNTPOINT" \
        --config "$CONFIG" \
        --allow-other \
        --uid "${PUID:-568}" \
        --gid "${PGID:-568}" \
        --attr-timeout 10s \
        --dir-cache-time 24h \
        --daemon >> "$LOG" 2>&1
fi

for _ in $(seq 1 30); do
    if mountpoint -q "$MOUNTPOINT"; then
        if ls "$MOUNTPOINT" >/dev/null 2>&1; then
            date -Is > "$READY_MARKER"
            chmod 0644 "$READY_MARKER"
            log "Real-Debrid mounted successfully"
            exit 0
        fi
    fi
    sleep 2
done

fail "Real-Debrid mount did not become ready in time"
