#!/usr/bin/env bash
# One-time Arch/Nyarch bootstrap. Scheduled work uses the bundled Rust runtime.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
apply=false
readonly_mode=false
for arg in "$@"; do
    case "$arg" in
        --apply) apply=true ;;
        --check|--dry-run|--help|-h) readonly_mode=true ;;
    esac
done
if "$apply" && "$readonly_mode"; then
    echo 'Use --apply separately from --check, --dry-run, or --help.' >&2
    exit 2
fi

if "$apply"; then
    if (( EUID != 0 )); then
        echo 'Installation requires root: run sudo bash install.sh ... --apply.' >&2
        exit 1
    fi
    # Never use pacman on an unsupported distribution or perform a partial upgrade.
    if [[ ! -r /etc/os-release ]]; then
        echo 'Cannot identify the host distribution.' >&2
        exit 1
    fi
    . /etc/os-release
    if [[ " ${ID:-} ${ID_LIKE:-} " != *' arch '* ]] || ! command -v pacman >/dev/null; then
        echo 'Automatic setup supports an existing Arch/Nyarch host with pacman.' >&2
        exit 1
    fi
    if [[ "$(uname -m)" != x86_64 ]] || ! command -v systemctl >/dev/null; then
        echo 'An x86_64 systemd host is required.' >&2
        exit 1
    fi
    packages=()
    # Check commands as well as packages: an installed but incomplete package
    # must not be mistaken for a usable prerequisite.
    prerequisites=( \
        'python:python3' \
        'util-linux:lsblk findmnt blkid mount umount flock runuser' \
        'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
        'tar:tar' 'curl:curl' 'openssl:openssl' 'iproute2:ip' 'pciutils:lspci' )
    for entry in "${prerequisites[@]}"; do
        package="${entry%%:*}"
        for command_name in ${entry#*:}; do
            if ! command -v "$command_name" >/dev/null; then
                packages+=("$package")
                break
            fi
        done
    done
    if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
        packages+=(ca-certificates ca-certificates-utils)
    fi
    if ((${#packages[@]})); then
        printf 'Checking repository availability for missing prerequisites: %s\n' "${packages[*]}"
        # Check the complete set before changing anything. Repository refresh
        # and full host upgrades remain an explicit OS administration step.
        pacman -Si -- "${packages[@]}" >/dev/null
        pacman -S --needed --noconfirm -- "${packages[@]}"
    fi
    for entry in "${prerequisites[@]}"; do
        for command_name in ${entry#*:}; do
            if ! command -v "$command_name" >/dev/null; then
                echo "Prerequisite still missing: $command_name. Repair package ${entry%%:*} before retrying." >&2
                exit 1
            fi
        done
    done
fi
if ! command -v python3 >/dev/null; then
    echo 'Python 3.11+ is missing. An apply run can install it: sudo bash install.sh --profile full --apply' >&2
    echo 'Read-only check/dry-run modes never install packages.' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo 'Python 3.11+ is required; bring the host up to date before retrying.' >&2
    exit 1
fi
exec python3 "$ROOT/installer.py" "$@"
