#!/usr/bin/env bash
set -Eeuo pipefail

DATASET="apps/appdata"
SCRIPT_SRC="/mnt/apps/scripts"
DEST="/mnt/tank/backups/configs"
DATE="$(date +%F_%H-%M-%S)"
LOG="/mnt/apps/scripts/backup-app-config.log"

mkdir -p "$DEST"
echo "[backup] Starting $DATE" >> "$LOG"

LATEST_SNAP="$(zfs list -H -t snapshot -o name -s creation -d 1 "$DATASET" 2>/dev/null | tail -1 || true)"

if [ -n "$LATEST_SNAP" ]; then
    SNAP_NAME="${LATEST_SNAP##*@}"
    APPDATA_SRC="/mnt/apps/appdata/.zfs/snapshot/$SNAP_NAME"
    echo "[backup] Using snapshot: $SNAP_NAME" >> "$LOG"
else
    APPDATA_SRC="/mnt/apps/appdata"
    echo "[backup] WARNING: no snapshot found, falling back to live data." >> "$LOG"
fi

ARCHIVE="$DEST/app-config-$DATE.tar.gz"
tar -czf "$ARCHIVE" \
    --transform "s|^${APPDATA_SRC#/}|mnt/apps/appdata|" \
    "$APPDATA_SRC" "$SCRIPT_SRC" >> "$LOG" 2>&1

if ! tar -tzf "$ARCHIVE" >/dev/null 2>>"$LOG"; then
    echo "[backup] FAILED: archive is corrupt, removing." >> "$LOG"
    rm -f "$ARCHIVE"
    exit 1
fi

find "$DEST" -name "app-config-*.tar.gz" -type f -mtime +30 -print -delete >> "$LOG" 2>&1

archive_size="$(du -h "$ARCHIVE" | cut -f1)"
echo "[backup] Finished $DATE OK ($archive_size)" >> "$LOG"
