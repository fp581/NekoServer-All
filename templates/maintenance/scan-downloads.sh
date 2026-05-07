#!/usr/bin/env bash
set -Eeuo pipefail

LOG="/mnt/apps/scripts/clamav-scan.log"
echo "[clamav] Starting $(date)" >> "$LOG"

docker exec clamav clamscan --recursive \
    --exclude-dir='(^|/)\.zfs' \
    --move=/quarantine \
    --quiet \
    /scandir >> "$LOG" 2>&1 || true

echo "[clamav] Finished $(date)" >> "$LOG"
