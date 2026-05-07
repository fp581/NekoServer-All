#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=/dev/null
source /mnt/apps/scripts/config.env

LOG="/mnt/apps/scripts/photo-backup-usb.log"
echo "[photo-backup] Starting $(date)" >> "$LOG"

[ "${ENABLE_USB_BACKUP:-0}" != "1" ] && { echo "[photo-backup] Disabled in config.env, exiting." >> "$LOG"; exit 0; }
[ -z "${USB_UUID:-}" ] && { echo "[photo-backup] No USB_UUID set, exiting." >> "$LOG"; exit 0; }

MOUNT="/mnt/usb-photo-backup"
mkdir -p "$MOUNT"

if ! mount UUID="$USB_UUID" "$MOUNT" 2>>"$LOG"; then
    echo "[photo-backup] USB drive not present (UUID $USB_UUID). Exiting cleanly." >> "$LOG"
    exit 0
fi

if ! mountpoint -q "$MOUNT"; then
    echo "[photo-backup] USB drive did not mount. Stopping." >> "$LOG"
    exit 1
fi

trap 'umount "$MOUNT" 2>/dev/null || true' EXIT

rsync -a --ignore-existing --no-perms /mnt/tank/photos/library/ "$MOUNT/photos/" >> "$LOG" 2>&1
sync

echo "[photo-backup] Finished $(date) OK" >> "$LOG"
