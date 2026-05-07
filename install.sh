#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PHASE="base"
INSTALL_WITH=""

usage() {
    cat <<'USAGE'
Usage:
  sudo bash install.sh [--phase base] [--with flaresolverr] [--with profilarr] [--with clamav]

Examples:
  sudo bash install.sh
  sudo bash install.sh --with flaresolverr --with profilarr

Notes:
  Phase 1 installs the local media stack files only.
  Real-Debrid/Zurg/rclone are intentionally documented in PHASE2.md.
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

if [ "$PHASE" != "base" ]; then
    echo "Only Phase 1/base is implemented as runnable scripts in this pass." >&2
    echo "See PHASE2.md for the deferred Real-Debrid/Zurg/rclone work." >&2
    exit 2
fi

export REPO_ROOT="$SCRIPT_DIR"
export INSTALL_WITH

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
  4. Follow PHASE2.md only after local playback/imports work.
DONE
