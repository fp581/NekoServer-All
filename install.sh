#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PHASE="base"
INSTALL_WITH=""
REALDEBRID_TOKEN_FILE=""

usage() {
    cat <<'USAGE'
Usage:
  sudo bash install.sh [--phase base] [--with flaresolverr] [--with profilarr] [--with clamav]
  sudo bash install.sh --phase realdebrid [--realdebrid-token-file /path/to/token]

Examples:
  sudo bash install.sh
  sudo bash install.sh --with flaresolverr --with profilarr
  sudo bash install.sh --phase realdebrid --realdebrid-token-file /root/realdebrid-token

Notes:
  Phase 1 installs the local media stack files.
  Phase 2 installs Real-Debrid/Zurg/rclone files after Phase 1 works.
  Use --realdebrid-token-file to avoid putting secrets in shell history.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --phase)
            [ "$#" -ge 2 ] || { echo "Missing value for --phase" >&2; exit 2; }
            PHASE="$2"
            shift 2
            ;;
        --with)
            [ "$#" -ge 2 ] || { echo "Missing value for --with" >&2; exit 2; }
            case "$2" in
                flaresolverr|profilarr|clamav)
                    INSTALL_WITH="${INSTALL_WITH}${INSTALL_WITH:+ }$2"
                    ;;
                *)
                    echo "Unknown optional service: $2" >&2
                    exit 2
                    ;;
            esac
            shift 2
            ;;
        --realdebrid-token-file)
            [ "$#" -ge 2 ] || { echo "Missing value for --realdebrid-token-file" >&2; exit 2; }
            REALDEBRID_TOKEN_FILE="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

export REPO_ROOT="$SCRIPT_DIR"
export INSTALL_WITH
export REALDEBRID_TOKEN_FILE

case "$PHASE" in
    base)
        bash "$SCRIPT_DIR/scripts/01-preflight.sh"
        bash "$SCRIPT_DIR/scripts/02-create-folders.sh"
        bash "$SCRIPT_DIR/scripts/03-install-stack-files.sh"
        bash "$SCRIPT_DIR/scripts/04-install-maintenance.sh"

        cat <<'DONE'

Phase 1 files installed.

Next manual steps:
  1. Review /mnt/apps/scripts/config.env and fill in secrets.
  2. Deploy /mnt/apps/scripts/docker-compose.yml through TrueNAS Apps > Install via YAML.
  3. Register cron jobs from docs/manual-steps.md.
  4. Run sudo bash install.sh --phase realdebrid only after local playback/imports work.
DONE
        ;;
    realdebrid)
        bash "$SCRIPT_DIR/scripts/01-preflight.sh"
        bash "$SCRIPT_DIR/scripts/02-create-folders.sh"
        bash "$SCRIPT_DIR/scripts/05-install-realdebrid-files.sh"

        cat <<'DONE'

Phase 2 Real-Debrid files installed.

Next manual steps:
  1. If no token file was supplied, edit /mnt/apps/appdata/zurg/config.yml.
  2. Install rclone on the TrueNAS host if /mnt/apps/scripts/verify-realdebrid.sh reports it missing.
  3. Register /mnt/apps/scripts/enable-fuse-allow-other.sh and /mnt/apps/scripts/rclone-mount.sh as TrueNAS Post Init scripts.
  4. Redeploy /mnt/apps/scripts/docker-compose.yml through TrueNAS Apps.
  5. Run bash /mnt/apps/scripts/rclone-mount.sh, then bash /mnt/apps/scripts/verify-realdebrid.sh.
  6. After verification passes, run bash /mnt/apps/scripts/enable-realdebrid-wait.sh and redeploy Jellyfin.
DONE
        ;;
    *)
        echo "Unknown phase: $PHASE" >&2
        usage
        exit 2
        ;;
esac
