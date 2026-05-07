#!/usr/bin/env bash
set -Eeuo pipefail

FUSE_CONF="/etc/fuse.conf"

if [ ! -e "$FUSE_CONF" ]; then
    touch "$FUSE_CONF"
fi

if grep -q '^user_allow_other$' "$FUSE_CONF"; then
    exit 0
fi

if grep -q '^#user_allow_other$' "$FUSE_CONF"; then
    sed -i 's/^#user_allow_other$/user_allow_other/' "$FUSE_CONF"
else
    printf '\nuser_allow_other\n' >> "$FUSE_CONF"
fi
