#!/usr/bin/env bash
# One-time bootstrap for Arch/Nyarch, Debian/Ubuntu, Fedora, RHEL / Rocky / AlmaLinux and openSUSE. Scheduled work uses the bundled Rust runtime.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# apt's lock on its package lists is held (a POSIX lock, which lslocks lists by path).
apt_lists_busy() {
    local held
    held=$'\n'"$(lslocks -n -o PATH 2>/dev/null || true)"$'\n'
    [[ "$held" == *$'\n/var/lib/apt/lists/lock\n'* ]]
}
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
    # One setup at a time, held until setup ends (the installer inherits it). A second
    # one stops here, before it touches packages, their records or any service.
    exec 9>>/run/nekoserver-setup.lock
    if ! flock -n 9; then
        echo 'Another NekoServer setup is running. Let it finish, then run  sudo bash install.sh  again.' >&2
        exit 1
    fi
    # Never use a package manager on an unsupported distribution, and never a partial
    # upgrade on Arch.
    if [[ ! -r /etc/os-release ]]; then
        echo 'Cannot identify the host distribution.' >&2
        exit 1
    fi
    . /etc/os-release
    distro=" ${ID:-} ${ID_LIKE:-} "
    if [[ "$distro" == *' arch '* || "$distro" == *' nyarch '* ]] && command -v pacman >/dev/null; then
        family=arch
        clean_hint='sudo pacman -Sc'
    elif [[ "$distro" == *' debian '* || "$distro" == *' ubuntu '* ]] && command -v apt-get >/dev/null; then
        family=debian
        clean_hint='sudo apt-get clean'
        system_path=/var/cache/apt
        [[ -d "$system_path" ]] || system_path=/
        export DEBIAN_FRONTEND=noninteractive
        # Ubuntu's needrestart could restart running services (a Docker someone already
        # runs) after an install; apt waits for automatic security updates to finish;
        # a settings file you edited is kept rather than asked about on the terminal.
        export NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l
        apt_wait=(-o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold)
        # Python 3.11 comes with Debian 12 and Ubuntu 24.04; an older release can't get it
        # from its own packages, so say so before anything is installed.
        # (Debian testing has no VERSION_ID: not judged here; the Python check below still runs.)
        release="${VERSION_ID:-}"
        if [[ "$release" =~ ^[0-9]+ ]] && { [[ "${ID:-}" == debian && "${release%%.*}" -lt 12 ]] || [[ "${ID:-}" == ubuntu && "${release%%.*}" -lt 24 ]]; }; then
            echo "NekoServer needs Debian 12 or newer, or Ubuntu 24.04 or newer; this is ${PRETTY_NAME:-$ID $release}." >&2
            echo 'Upgrade the system first (or install a newer release), then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
    elif [[ "${ID:-}" == fedora ]] && command -v dnf >/dev/null; then
        # Fedora itself (Workstation, Server, spins). RHEL and its rebuilds name fedora in
        # ID_LIKE but have other repositories: the next branch.
        family=fedora
        clean_hint='sudo dnf clean all'
        system_path=/var/cache/libdnf5
        [[ -d "$system_path" ]] || system_path=/
        # Docker and Tailscale package the Fedora releases still supported (two at a time).
        release="${VERSION_ID:-}"
        if [[ "$release" =~ ^[0-9]+$ ]] && (( release < 42 )); then
            echo "NekoServer needs Fedora 42 or newer; this is ${PRETTY_NAME:-Fedora $release}." >&2
            echo 'Upgrade the system first (sudo dnf system-upgrade), then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
    elif [[ "$distro" == *' rhel '* ]] && command -v dnf >/dev/null; then
        # RHEL and its rebuilds (Rocky Linux, AlmaLinux, CentOS Stream), which all name rhel
        # in ID or ID_LIKE: Docker's and Tailscale's packages for them, Caddy from EPEL.
        family=rhel
        clean_hint='sudo dnf clean all'
        system_path=/var/cache/dnf
        [[ -d "$system_path" ]] || system_path=/
        # Version 10 brings Python 3.12; version 9's Python 3.9 is older than NekoServer needs.
        release="${VERSION_ID%%.*}"
        if [[ "$release" =~ ^[0-9]+$ ]] && (( release < 10 )); then
            echo "NekoServer needs version 10 or newer of ${NAME:-this system}; this is ${PRETTY_NAME:-$ID ${VERSION_ID:-}}." >&2
            echo 'Version 9 comes with Python 3.9, older than NekoServer needs: install version 10, then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
    elif [[ "$distro" == *' opensuse'*' '* ]] && command -v zypper >/dev/null; then
        # openSUSE Leap, Tumbleweed and Slowroll (and systems naming opensuse in ID_LIKE).
        # SLES names only suse, and has other repositories and a subscription: not covered.
        family=suse
        clean_hint='sudo zypper clean'
        system_path=/var/cache/zypp
        [[ -d "$system_path" ]] || system_path=/
        # Leap 15 comes with Python 3.6; Leap 16 brings 3.13. (Tumbleweed's VERSION_ID is a date.)
        release="${VERSION_ID%%.*}"
        if [[ "${ID:-}" == opensuse-leap && "$release" =~ ^[0-9]+$ ]] && (( release < 16 )); then
            echo "NekoServer needs openSUSE Leap 16 or newer; this is ${PRETTY_NAME:-Leap ${VERSION_ID:-}}." >&2
            echo 'Leap 15 comes with Python 3.6, older than NekoServer needs: upgrade to Leap 16, then run  sudo bash install.sh  again.' >&2
            exit 1
        fi
    else
        echo 'Automatic setup supports Arch Linux / Nyarch (with pacman), Debian / Ubuntu (with apt), Fedora and RHEL / Rocky / AlmaLinux (with dnf), and openSUSE (with zypper).' >&2
        exit 1
    fi
    if [[ "$(uname -m)" != x86_64 ]] || ! command -v systemctl >/dev/null; then
        echo 'An x86_64 systemd host is required.' >&2
        exit 1
    fi
    # Image-based systems (Fedora Silverblue and Kinoite, SteamOS) name their family but
    # keep the system files read-only: the package manager would fail halfway through.
    usr_options=$(findmnt -n -o OPTIONS --target /usr 2>/dev/null || true)
    if [[ -e /run/ostree-booted || ",$usr_options," == *,ro,* ]]; then
        echo "${PRETTY_NAME:-This system} keeps its system files read-only (an image-based system), so" >&2
        echo 'NekoServer cannot install the packages it needs. Install it on Arch Linux, Debian, Ubuntu,' >&2
        echo 'Fedora Workstation or Server, or openSUSE Leap or Tumbleweed instead.' >&2
        exit 1
    fi
    if [[ "$family" == debian && -e /run/reboot-required ]]; then
        # An earlier update is only finished after a restart; change no service before it.
        echo 'An earlier system update needs a restart first.' >&2
        echo 'Restart the PC now (sudo reboot), then run  sudo bash install.sh  again.' >&2
        exit 3
    fi
    if [[ "$family" == debian ]] && ! "$recover" && command -v python3 >/dev/null; then
        # An older NekoServer could leave a second definition of Docker's or Tailscale's
        # repository, which stops apt from working at all: removed before apt first runs.
        # Recovery skips it: it must open from local backups, also without a network.
        python3 "$ROOT/host.py" repair
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
        # plainly and does not stop the install (the package manager checks space again itself).
        # Measured in MB: df rounds GB up, so 4.1 GB would have read as 5.
        system_free=''
        if df_output=$(df -BM --output=avail "$system_path" 2>&1); then
            system_free=$(printf '%s\n' "$df_output" | tail -n 1 | tr -dc '0-9')
        fi
        if [[ -z "$system_free" ]]; then
            echo "Could not measure the free space on the system disk ($system_path); continuing." >&2
            echo 'Make sure at least 5 GB is free there before the update.' >&2
        elif (( system_free < 5120 )); then
            echo "The system disk has only $(( system_free / 1024 )) GB free. The system update and the packages" >&2
            echo 'need at least 5 GB there (your apps and files go on the SSD and HDD instead).' >&2
            echo "Free some space (for example: $clean_hint), then run  sudo bash install.sh  again." >&2
            exit 1
        fi
        # Recovery skips this so restoring never depends on the network.
        if [[ "$family" == debian ]]; then
            # Debian and Ubuntu: refresh the package lists only and install just what is
            # needed. Upgrading the rest of the system stays your own routine.
            # They also refresh the lists themselves now and then (apt-daily), holding the
            # lists' lock a minute or two: apt-get waits for dpkg's lock, not for that one.
            waited=0
            while apt_lists_busy && (( waited < 600 )); do
                (( waited )) || echo 'The system is refreshing its package lists itself; waiting for it to finish (up to 10 minutes)...'
                sleep 5
                waited=$(( waited + 5 ))
            done
            echo 'Refreshing the package lists (apt-get update).'
            apt-get "${apt_wait[@]}" update
        elif [[ "$family" == fedora || "$family" == rhel ]]; then
            # Fedora and RHEL: refresh the package lists only, as on Debian; upgrading the
            # rest of the system stays your own routine.
            echo 'Refreshing the package lists (dnf makecache).'
            dnf -q makecache
        elif [[ "$family" == suse ]]; then
            # openSUSE: the same. ZYPP_LOCK_TIMEOUT makes zypper wait for another zypper
            # or YaST (up to 10 minutes) instead of failing at once.
            echo 'Refreshing the package lists (zypper refresh).'
            ZYPP_LOCK_TIMEOUT=600 zypper --non-interactive --quiet refresh
        else
            # Arch supports only full upgrades: refresh and upgrade everything first, so the
            # apps and their dependencies install at their current versions and match each
            # other.
            # pacman's lock is there while another pacman (an updater, say) works: wait for
            # it. One older than this boot was left by an update cut off before the last
            # restart, which nothing can still be running: removed, and -Syu finishes it.
            pacman_lock=/var/lib/pacman/db.lck
            boot_time=''
            while read -r key value _; do
                if [[ "$key" == btime ]]; then boot_time=$value; fi
            done < /proc/stat
            if [[ -e "$pacman_lock" && -n "$boot_time" ]] && (( $(stat -c %Y "$pacman_lock") < boot_time )); then
                rm -f -- "$pacman_lock"
                echo "pacman's lock was left by an update cut off before the last restart: removed. The update below finishes that one."
            fi
            waited=0
            while [[ -e "$pacman_lock" ]] && (( waited < 600 )); do
                (( waited )) || echo 'Another package installation is running; waiting for it to finish (up to 10 minutes)...'
                sleep 5
                waited=$(( waited + 5 ))
            done
            if [[ -e "$pacman_lock" ]]; then
                echo "pacman is still locked ($pacman_lock) after 10 minutes: another installation is running." >&2
                echo 'Let it finish, then run  sudo bash install.sh  again. If nothing is installing, the lock is left' >&2
                echo "from one that was cut off: remove it (sudo rm $pacman_lock), run  sudo pacman -Syu, then setup again." >&2
                exit 1
            fi
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
                echo 'The system packages were updated; nothing of NekoServer was set up yet. It starts from here next time.'
                exit 3
            fi
        fi
    fi
    packages=()
    # Check commands as well as packages: an installed but incomplete package
    # must not be mistaken for a usable prerequisite.
    # package:commands it provides, in each family's package names.
    if [[ "$family" == debian ]]; then
        prerequisites=( \
            'python3:python3' \
            'util-linux:lsblk findmnt blkid flock runuser' 'mount:mount umount' \
            'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
            'tar:tar' 'curl:curl' 'openssl:openssl' 'iproute2:ip' 'pciutils:lspci' )
        certificates=(ca-certificates)
        certificate_bundle=/etc/ssl/certs/ca-certificates.crt
    elif [[ "$family" == fedora || "$family" == rhel ]]; then
        prerequisites=( \
            'python3:python3' \
            'util-linux:lsblk findmnt blkid mount umount flock runuser' \
            'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
            'tar:tar' 'curl:curl' 'openssl:openssl' 'iproute:ip' 'pciutils:lspci' )
        certificates=(ca-certificates)
        certificate_bundle=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
    elif [[ "$family" == suse ]]; then
        # openSUSE keeps lsblk and findmnt in a package of their own.
        prerequisites=( \
            'python3:python3' \
            'util-linux-systemd:lsblk findmnt' 'util-linux:blkid mount umount flock runuser' \
            'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
            'tar:tar' 'curl:curl' 'openssl:openssl' 'iproute2:ip' 'pciutils:lspci' )
        certificates=(ca-certificates ca-certificates-mozilla)
        certificate_bundle=/etc/ssl/ca-bundle.pem
    else
        prerequisites=( \
            'python:python3' \
            'util-linux:lsblk findmnt blkid mount umount flock runuser' \
            'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
            'tar:tar' 'curl:curl' 'openssl:openssl' 'iproute2:ip' 'pciutils:lspci' )
        certificates=(ca-certificates ca-certificates-utils)
        certificate_bundle=/etc/ssl/certs/ca-certificates.crt
    fi
    for entry in "${prerequisites[@]}"; do
        package="${entry%%:*}"
        for command_name in ${entry#*:}; do
            if ! command -v "$command_name" >/dev/null; then
                packages+=("$package")
                break
            fi
        done
    done
    if [[ ! -s "$certificate_bundle" ]]; then
        packages+=("${certificates[@]}")
    fi
    if ((${#packages[@]})); then
        printf 'Checking repository availability for missing prerequisites: %s\n' "${packages[*]}"
        # Check the complete set before changing anything.
        if [[ "$family" == debian ]]; then
            apt-get "${apt_wait[@]}" install -y --no-install-recommends -- "${packages[@]}"
        elif [[ "$family" == fedora || "$family" == rhel ]]; then
            # (dnf5 takes no "--" before the names; these are fixed package names.)
            dnf install -y --setopt=install_weak_deps=False "${packages[@]}"
        elif [[ "$family" == suse ]]; then
            ZYPP_LOCK_TIMEOUT=600 zypper --non-interactive install --no-recommends "${packages[@]}"
        else
            pacman -Si -- "${packages[@]}" >/dev/null
            pacman -S --needed --noconfirm -- "${packages[@]}"
        fi
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
    echo 'Python 3.11+ is missing. Running  sudo bash install.sh  installs it' >&2
    echo 'Read-only check/dry-run modes never install packages.' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "NekoServer needs Python 3.11 or newer; this system has $(python3 -c 'import platform; print(platform.python_version())')." >&2
    echo 'It comes with Arch, Debian 12, Ubuntu 24.04, Fedora 42, RHEL 10 and openSUSE Leap 16 or newer (and Tumbleweed). A system based on an older release' >&2
    echo '(such as Linux Mint 21, based on Ubuntu 22.04) does not get it from updates: upgrade to the next release first.' >&2
    exit 1
fi
if "$apply" && "$full"; then
    # Every system package the apps need that is not inside their containers (host.py
    # APP_PACKAGES), installed up front so a missing one stops the setup before it
    # starts. Installing does not start Docker; its setup step does that on the SSD.
    python3 "$ROOT/host.py" bootstrap
fi
exec python3 "$ROOT/installer.py" "$@"
