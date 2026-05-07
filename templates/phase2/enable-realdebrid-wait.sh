#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_ENV="/mnt/apps/scripts/config.env"

bash /mnt/apps/scripts/verify-realdebrid.sh

[ -r "$CONFIG_ENV" ] || { echo "Missing $CONFIG_ENV" >&2; exit 1; }

if grep -q '^WAIT_FOR_RD=' "$CONFIG_ENV"; then
    sed -i 's/^WAIT_FOR_RD=.*/WAIT_FOR_RD="1"/' "$CONFIG_ENV"
else
    printf '\nWAIT_FOR_RD="1"\n' >> "$CONFIG_ENV"
fi

printf 'WAIT_FOR_RD="1" is enabled. Redeploy/restart Jellyfin for the wait script to take effect.\n'
