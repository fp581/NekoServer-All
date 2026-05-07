#!/usr/bin/with-contenv bash
# shellcheck shell=bash

if [ "${WAIT_FOR_RD:-0}" != "1" ]; then
    exit 0
fi

for _ in $(seq 1 60); do
    if [ -s /realdebrid-status/realdebrid-mounted ] && cd /media/realdebrid && ls >/dev/null 2>&1; then
        exit 0
    fi
    sleep 5
done

echo "Real-Debrid readiness marker or mount not found. Jellyfin will not start."
exit 1
