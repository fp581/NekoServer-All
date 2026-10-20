#!/usr/bin/env bash
# One-time Arch/Nyarch bootstrap. Scheduled work uses the bundled Rust runtime.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Where the system update downloads packages; its disk is checked before updating.
system_path=/var/cache/pacman
[[ -d "$system_path" ]] || system_path=/
# No options: the guided setup wizard. It installs, so it needs the same checks as --apply.
if (( $# == 0 )); then
    set -- --wizard
fi
apply=false
readonly_mode=false
recover=false
full=false
previous=''
for arg in "$@"; do
    case "$arg" in
        --apply) apply=true ;;
        --wizard) apply=true; full=true ;;
        --recover) apply=true; recover=true ;;
        --check|--dry-run|--help|-h) readonly_mode=true ;;
    esac
    [[ "$previous" == --profile && "$arg" == full ]] && full=true
    [[ "$arg" == --profile=full ]] && full=true
    previous="$arg"
done
if "$apply" && "$readonly_mode"; then
    echo 'Use --apply separately from --check, --dry-run, or --help.' >&2
    exit 2
fi

if "$apply"; then
    if (( EUID != 0 )); then
        echo 'Run it with sudo:  sudo bash install.sh' >&2
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
    if ! "$recover" && [[ -e /etc/nekoserver-backups/maintenance-resume.json ]]; then
        # An unfinished monthly update can only be finished by the version that started
        # it; stop before the system update or any NekoServer file is replaced.
        if ! command -v python3 >/dev/null; then
            echo 'A monthly update did not finish. Run  sudo systemctl start nekoserver-maintenance-resume.service  first, then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
        if ! NEKO_ROOT="$ROOT" python3 -I -c 'import os, sys; sys.path.insert(0, os.environ["NEKO_ROOT"]); import neko_common; m = neko_common.pending_maintenance(); m and sys.exit(m)'; then
            exit 1
        fi
    fi
    if ! "$recover"; then
        # The update and packages go on the system disk; the apps and data go on the SSD
        # and HDD, which the wizard checks once you have picked them. Stop before changing
        # anything when the system disk is too full for a safe update.
        # GNU df: --output cannot be combined with -P. A failed measurement is reported
        # plainly and does not stop the install (pacman checks space again itself).
        system_free=''
        if df_output=$(df -B1G --output=avail "$system_path" 2>&1); then
            system_free=$(printf '%s\n' "$df_output" | tail -n 1 | tr -dc '0-9')
        fi
        if [[ -z "$system_free" ]]; then
            echo "Could not measure the free space on the system disk ($system_path); continuing." >&2
            echo 'Make sure at least 5 GB is free there before the update.' >&2
        elif (( system_free < 5 )); then
            echo "The system disk has only ${system_free} GB free. The system update and the packages" >&2
            echo 'need at least 5 GB there (your apps and files go on the SSD and HDD instead).' >&2
            echo 'Free some space (for example: sudo pacman -Sc), then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
        # Arch supports only full upgrades: refresh and upgrade everything first, so the
        # apps and their dependencies install at their current versions and match each
        # other. Recovery skips this so restoring never depends on the network.
        echo 'Updating the system first (pacman -Syu). This can take a while.'
        if pacman -Q archlinux-keyring >/dev/null 2>&1; then
            # Arch's recommended order: a current keyring before the upgrade it signs.
            pacman -Sy --needed --noconfirm archlinux-keyring
        fi
        pacman -Syu --noconfirm
        if [[ ! -d "/usr/lib/modules/$(uname -r)" ]]; then
            echo
            echo 'A new Linux kernel was installed by the update.'
            echo 'Restart the PC now (sudo reboot), then run  sudo bash install.sh  again.'
            echo 'Nothing else was changed; it starts from here next time.'
            exit 3
        fi
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
    if "$full"; then
        # Every system package the apps need that is not inside their containers,
        # installed up front so a missing one stops the setup before it starts.
        # Installing does not start Docker; its setup step does that on the SSD.
        app_packages=(docker docker-compose tailscale caddy samba hdparm smartmontools qrencode xdg-utils)
        echo "Installing the packages the apps need: ${app_packages[*]}"
        pacman -S --needed --noconfirm -- "${app_packages[@]}"
    fi
fi
if ! command -v python3 >/dev/null; then
    echo 'Python 3.11+ is missing. Running  sudo bash install.sh  installs it' >&2
    echo 'Read-only check/dry-run modes never install packages.' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo 'Python 3.11+ is required; bring the host up to date before retrying.' >&2
    exit 1
fi
exec python3 "$ROOT/installer.py" "$@"
