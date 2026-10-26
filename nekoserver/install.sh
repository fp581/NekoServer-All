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
# A package source that did not answer (a mirror hiccup, a download cut off, the network
# still coming up): the same command is run again a little later, three times in all.
# Any other failure is reported at once. (host.py does the same for the apps' packages.)
transient_pattern='failed to fetch|failed retrieving file|failed to download|cannot download|download(ing)? (\(curl\) )?(error|failed)|curl error|could not resolve|temporary failure|name resolution|connection (failed|timed out|refused|reset)|timeout (was reached|exceeded)|timed out|network is unreachable|all mirrors were tried|hash sum mismatch|operation too slow|not found on medium|http (response|error)[: ]+(404|5[0-9][0-9])|service unavailable|bad gateway'
# Another program holds the package manager's lock (automatic updates, PackageKit, a
# software centre): waited out, up to 10 minutes, without counting as an attempt.
busy_pattern='could not get lock|is held by process|failed to obtain rpm transaction lock|unable to lock database|system management is locked'
# apt-get update says these and still succeeds when a source did not answer.
stale_pattern='^W: (Failed to fetch|Some index files failed to download)'
# What a clock set wrong makes package managers and downloads say.
clock_pattern='not yet valid|not valid yet|certificate has expired|seconds? in the future|time warp|clock problem'
# The command failed (or apt only warned) because a source did not answer: worth another try.
worth_retrying() {
    local code=$1 log=$2
    if (( code != 0 )); then
        grep -qiE "$transient_pattern" "$log"
    else
        grep -qE "$stale_pattern" "$log"
    fi
}
retrying() {
    local attempt=1 waited=0 code output
    output=$(mktemp)
    while :; do
        # (In a || list, so a failure here is judged below instead of ending the script.)
        { "$@" 2>&1 | tee "$output"; code=${PIPESTATUS[0]}; } || true
        if (( code != 0 && waited < 600 )) && grep -qiE "$busy_pattern" "$output"; then
            (( waited )) || echo 'Another program is installing or updating packages; waiting for it to finish (up to 10 minutes)...'
            sleep 20
            waited=$(( waited + 20 ))
            continue
        fi
        if (( attempt == 3 )) || ! worth_retrying "$code" "$output"; then
            break
        fi
        echo "The package source did not answer; trying again in 20 seconds ($(( attempt + 1 )) of 3)..."
        sleep 20
        attempt=$(( attempt + 1 ))
    done
    if (( code == 0 )) && grep -qE "$stale_pattern" "$output"; then
        echo 'Some package sources could not be reached (the lines above say which); going on with their lists from before.'
    fi
    if grep -qiE "$clock_pattern" "$output"; then
        echo "This PC's clock looks wrong (check the date and time with  timedatectl): let the network set it (sudo timedatectl set-ntp true), wait a minute, then run  sudo bash install.sh  again." >&2
    fi
    rm -f -- "$output"
    return "$code"
}
# zypper, as retrying runs it, waiting for another zypper or YaST (up to 10 minutes). Its
# documented "done, but" exit codes are not failures: 106 (ZYPPER_EXIT_INF_REPOS_SKIPPED)
# when a repository that did not answer was skipped for this run, and 107
# (ZYPPER_EXIT_INF_RPM_SCRIPT_FAILED) when a package's install script reported an error
# though the package is installed. A refresh where only some repositories failed says so
# ("Some of the repositories have not been refreshed"). Setup names the repository and
# goes on with the others, as it does for apt's stale lists; every other failure (one
# where no repository could be refreshed, too) stays one.
zypper_run() {
    local code output skipped
    output=$(mktemp)
    { retrying env ZYPP_LOCK_TIMEOUT=600 zypper --non-interactive "$@" 2>&1 | tee "$output"; code=${PIPESTATUS[0]}; } || true
    if (( code == 106 )) || { (( code != 0 )) && grep -q 'Some of the repositories have not been refreshed' "$output"; }; then
        skipped=$(grep -oE "(Skipping repository|Problem retrieving files from) '[^']+'" "$output" | grep -oE "'[^']+'" | sort -u | paste -sd ' ' -) || true
        echo "The repository ${skipped:-named above} could not be reached and was skipped; going on with the other repositories. If it stays away, turn it off with  sudo zypper modifyrepo --disable <its name>  and turn it back on later." >&2
        code=0
    elif (( code == 107 )); then
        echo 'The packages were installed, but an install script of one of them reported an error (the lines above say which); going on.' >&2
        code=0
    fi
    rm -f -- "$output"
    return "$code"
}
# Nothing has pacman's lock open: libalpm, which pacman and the graphical updaters use,
# keeps it open while it works, so a lock no program has open was left by a pacman that
# was cut off (killed, or the PC lost power) and nothing would ever remove it.
pacman_lock_abandoned() {
    command -v find >/dev/null && [[ -z "$(find /proc/[0-9]*/fd -lname "$pacman_lock" -print -quit 2>/dev/null)" ]]
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
finish=false
full=false
previous=''
for arg in "$@"; do
    case "$arg" in
        --apply) apply=true ;;
        --wizard) apply=true; full=true ;;
        --recover) apply=true; recover=true ;;
        --finish-update) apply=true; finish=true ;;
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
        if command -v sudo >/dev/null; then
            echo 'Run it with sudo:  sudo bash install.sh' >&2
        else
            # Debian set up with a root password has no sudo at first.
            echo 'Setup needs administrator rights, and this PC has no sudo yet. Become root with  su -  (it asks for the root password), then either:' >&2
            echo "  install sudo:  apt install sudo  and  usermod -aG sudo $(id -un)  then log out, log in again and run  sudo bash install.sh  here, or" >&2
            echo "  run it as root with your account named:  cd $ROOT && SUDO_USER=$(id -un) bash install.sh" >&2
        fi
        exit 1
    fi
    if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == root ]] && [[ ! -e "$ROOT/my-config.json" ]]; then
        # Started as root, not with sudo from someone's account: setup cannot tell whose
        # account the server is for (the apps' files, the shared folder, the build).
        echo 'Started as root, not with sudo from your own account. To set the server up for your account, stop now (Ctrl+C) and run  SUDO_USER=<your account name> bash install.sh  instead (or  sudo bash install.sh  from your account).'
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
    if "$finish"; then
        # Only an unfinished monthly update, finished by this release: its runtime is built
        # (when needed) and its resume run; no system update, package or other step.
        if [[ ! -e /etc/nekoserver-backups/maintenance-resume.json ]]; then
            echo 'No monthly update is waiting to be finished. Run  sudo bash install.sh  as usual.'
            exit 0
        fi
        if python3 "$ROOT/native_runtime.py" needs-build; then
            echo "Building this release's background program to finish the monthly update (needs the internet)."
            python3 "$ROOT/host.py" toolchain
            NEKOSERVER_BUILD_AS_ROOT=1 python3 "$ROOT/runtime/build.py"
        fi
        # (set -e ends the script with its failure; the one setup start stays at the end.)
        python3 "$ROOT/installer.py" --finish-update
        exit 0
    fi
    if ! "$recover" && [[ -e /etc/nekoserver-backups/maintenance-resume.json ]]; then
        # An unfinished monthly update is finished first (by the release that started it,
        # or by this one with --finish-update); stop before the system update or any
        # NekoServer file is replaced.
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
        if ! "$recover" && command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            # What setup cannot run without (SELinux enforcing, an older systemd, a pending
            # restart), said before the system update when Python is already here; with
            # no Python yet, it is said as soon as it is installed (below).
            python3 "$ROOT/capabilities.py" preflight || exit 1
        fi
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
            retrying apt-get "${apt_wait[@]}" update
        elif [[ "$family" == fedora || "$family" == rhel ]]; then
            # Fedora and RHEL: refresh the package lists only, as on Debian; upgrading the
            # rest of the system stays your own routine.
            echo 'Refreshing the package lists (dnf makecache).'
            retrying dnf -q makecache
        elif [[ "$family" == suse ]]; then
            # openSUSE: the same (zypper_run waits for another zypper or YaST, up to 10
            # minutes, instead of failing at once).
            echo 'Refreshing the package lists (zypper refresh).'
            zypper_run --quiet refresh
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
                if pacman_lock_abandoned; then
                    rm -f -- "$pacman_lock"
                    echo "pacman's lock was left by a pacman that was cut off (no program has it open): removed. The update below finishes that one."
                    break
                fi
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
                retrying pacman -Sy --needed --noconfirm --noprogressbar archlinux-keyring
            fi
            retrying pacman -Syu --noconfirm --noprogressbar
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
    # package:commands it provides, in each family's package names. These are named the
    # same everywhere; the others differ by family.
    common_tools=( \
        'coreutils:sha256sum du id install stat cp chmod chown df timeout' \
        'tar:tar' 'curl:curl' 'openssl:openssl' 'pciutils:lspci' )
    if [[ "$family" == debian ]]; then
        prerequisites=( \
            'python3:python3' \
            'util-linux:lsblk findmnt blkid flock runuser' 'mount:mount umount' \
            "${common_tools[@]}" 'iproute2:ip' )
        certificates=(ca-certificates)
        certificate_bundle=/etc/ssl/certs/ca-certificates.crt
    elif [[ "$family" == fedora || "$family" == rhel ]]; then
        prerequisites=( \
            'python3:python3' \
            'util-linux:lsblk findmnt blkid mount umount flock runuser' \
            "${common_tools[@]}" 'iproute:ip' )
        certificates=(ca-certificates)
        certificate_bundle=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
    elif [[ "$family" == suse ]]; then
        # openSUSE keeps lsblk and findmnt in a package of their own.
        prerequisites=( \
            'python3:python3' \
            'util-linux-systemd:lsblk findmnt' 'util-linux:blkid mount umount flock runuser' \
            "${common_tools[@]}" 'iproute2:ip' )
        certificates=(ca-certificates ca-certificates-mozilla)
        certificate_bundle=/etc/ssl/ca-bundle.pem
    else
        prerequisites=( \
            'python:python3' \
            'util-linux:lsblk findmnt blkid mount umount flock runuser' \
            "${common_tools[@]}" 'iproute2:ip' )
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
            retrying apt-get "${apt_wait[@]}" install -y --no-install-recommends -- "${packages[@]}"
        elif [[ "$family" == fedora || "$family" == rhel ]]; then
            # (dnf5 takes no "--" before the names; these are fixed package names.)
            retrying dnf install -y --setopt=install_weak_deps=False "${packages[@]}"
        elif [[ "$family" == suse ]]; then
            zypper_run install --no-recommends "${packages[@]}"
        else
            pacman -Si -- "${packages[@]}" >/dev/null
            retrying pacman -S --needed --noconfirm --noprogressbar -- "${packages[@]}"
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
if "$apply" && [[ "$family" == suse ]] && ! python3 -c 'import ssl, sqlite3, pyexpat, xml.etree.ElementTree, zlib, lzma, bz2, ctypes, hashlib' >/dev/null 2>&1; then
    # openSUSE's Python packages name only the libraries' names, not their versions: on
    # a system not updated for a while, a freshly installed Python can meet libraries
    # too old for it (pyexpat, which the XML parser loads only when it first parses: "undefined symbol" in an old libexpat) and parts of it do not
    # load. Bringing the installed packages up to date within the release fixes that
    # (Tumbleweed and Slowroll are rolling and are brought up to date as a whole, as
    # Arch is).
    echo 'Python is installed, but parts of it do not load with the libraries this system has (it needs updating).'
    if [[ "${ID:-}" == opensuse-leap ]]; then
        echo 'Bringing the installed packages up to date (zypper update). This can take a while.'
        zypper_run update --no-recommends
    else
        echo 'Bringing the system up to date (zypper dist-upgrade). This can take a while.'
        zypper_run dist-upgrade --no-recommends
    fi
    if ! python3 -c 'import ssl, sqlite3, pyexpat, xml.etree.ElementTree, zlib, lzma, bz2, ctypes, hashlib'; then
        echo 'Parts of Python still do not load after the update; the lines above say which. Fix that, then run  sudo bash install.sh  again.' >&2
        exit 1
    fi
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    echo "NekoServer needs Python 3.11 or newer; this system has $(python3 -c 'import platform; print(platform.python_version())')." >&2
    echo 'It comes with Arch, Debian 12, Ubuntu 24.04, Fedora 42, RHEL 10 and openSUSE Leap 16 or newer (and Tumbleweed). A system based on an older release' >&2
    echo '(such as Linux Mint 21, based on Ubuntu 22.04) does not get it from updates: upgrade to the next release first.' >&2
    exit 1
fi
if "$apply" && ! "$recover"; then
    # What setup cannot run without (SELinux enforcing, an older systemd, a restart an
    # update is waiting for): said now, before minutes of installing and building.
    python3 "$ROOT/capabilities.py" preflight || exit 1
fi
if "$apply" && "$full"; then
    # Every system package the apps need that is not inside their containers (host.py
    # APP_PACKAGES), installed up front so a missing one stops the setup before it
    # starts. Installing does not start Docker; its setup step does that on the SSD.
    python3 "$ROOT/host.py" bootstrap
fi
# Recovery needs no runtime: its menu and every restore in it are Python, so it opens
# without the internet or Rust (on a reinstalled PC, say). Setup run afterwards builds it.
if "$apply" && ! "$recover" && python3 "$ROOT/native_runtime.py" needs-build; then
    # NekoServer's own background program (the Rust runtime) is built here, on this PC,
    # the first time and after each update: git keeps its sources, not the binary.
    echo "Building NekoServer's background program for this PC (Rust is installed from your Linux's packages,"
    echo 'then the program is compiled). This takes a few minutes and needs the internet.'
    python3 "$ROOT/host.py" toolchain
    builder=''
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]] && [[ "$(stat -c %U "$ROOT")" == "$SUDO_USER" ]]; then
        # The release folder is the person's own: built as them, with their cargo cache.
        builder=$SUDO_USER
    fi
    if [[ -n "$builder" ]]; then
        # What an earlier build left as root (a build.py run with sudo, say) would stop
        # the person's own build: handed to them first.
        for left in "$ROOT/runtime/target" "$ROOT/runtime/bin" "$ROOT/runtime/build.json"; do
            [[ -e "$left" && "$(stat -c %U "$left")" != "$builder" ]] && chown -R "$builder" "$left"
        done
        runuser -u "$builder" -- env HOME="$(getent passwd "$builder" | cut -d: -f6)" python3 "$ROOT/runtime/build.py"
    else
        NEKOSERVER_BUILD_AS_ROOT=1 python3 "$ROOT/runtime/build.py"
    fi
fi
if "$apply" && ! "$recover"; then
    # Once more after the packages and the build, before "Ready to start?": an
    # automatic update (Ubuntu's unattended upgrades) may have installed a new kernel
    # in those minutes and be waiting for a restart.
    python3 "$ROOT/capabilities.py" preflight || exit 1
fi
exec python3 "$ROOT/installer.py" "$@"
