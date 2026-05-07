#!/usr/bin/env bash
set -Eeuo pipefail

# shellcheck source=/dev/null
source /mnt/apps/scripts/config.env

COMPLETE="/mnt/tank/data/downloads/complete"
INCOMPLETE="/mnt/apps/downloads-incomplete"
LOG="/mnt/apps/scripts/cleanup-downloads.log"
INCOMPLETE_DAYS="${INCOMPLETE_DAYS:-14}"

# shellcheck disable=SC2129
echo "[cleanup] Starting $(date)" >> "$LOG"

find "$COMPLETE" -not -path '*/.*' -type f \( \
    -iname "*.nfo" -o -iname "*.sfv" -o -iname "*.url" \
\) -print -delete >> "$LOG" 2>&1

find "$INCOMPLETE" -not -path '*/.*' -type f -mtime +"$INCOMPLETE_DAYS" -print -delete >> "$LOG" 2>&1

find "$COMPLETE" "$INCOMPLETE" -mindepth 1 -not -path '*/.*' -type d -empty -print -delete >> "$LOG" 2>&1

echo "[cleanup] Finished $(date)" >> "$LOG"
