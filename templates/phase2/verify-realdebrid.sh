#!/usr/bin/env bash
set -Eeuo pipefail

MOUNTPOINT="/mnt/tank/realdebrid"
READY_MARKER="/mnt/apps/appdata/rclone/realdebrid-mounted"

ok() {
    printf '[ok] %s\n' "$*"
}

fail() {
    printf '[fail] %s\n' "$*" >&2
    exit 1
}

[ -r /mnt/apps/appdata/zurg/config.yml ] || fail "missing /mnt/apps/appdata/zurg/config.yml"
if grep -q 'YOUR_REAL_DEBRID_API_TOKEN' /mnt/apps/appdata/zurg/config.yml; then
    fail "Zurg config still contains the placeholder Real-Debrid token"
fi
ok "Zurg config exists"

command -v rclone >/dev/null 2>&1 || fail "rclone is not installed on the TrueNAS host"
ok "rclone is installed"

curl -fsS http://localhost:9999 >/dev/null || fail "Zurg is not responding on http://localhost:9999"
ok "Zurg responds on localhost:9999"

mountpoint -q "$MOUNTPOINT" || fail "$MOUNTPOINT is not a mountpoint"
ok "$MOUNTPOINT is mounted"

ls "$MOUNTPOINT" >/dev/null || fail "$MOUNTPOINT cannot be listed"
ok "$MOUNTPOINT can be listed"

[ -s "$READY_MARKER" ] || fail "missing readiness marker: $READY_MARKER"
ok "readiness marker exists"

printf 'Real-Debrid verification passed.\n'
