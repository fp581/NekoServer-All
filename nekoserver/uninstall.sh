#!/bin/bash
# Remove NekoServer from this PC: what you choose, after two confirmations.
#   sudo bash uninstall.sh             choose what goes, see the plan, confirm twice
#   sudo bash uninstall.sh --dry-run   see what each choice would remove; nothing changes
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dry=false
for arg in "$@"; do [[ "$arg" == --dry-run ]] && dry=true; done
if ! $dry && [[ $EUID -ne 0 ]]; then
  echo 'Run with sudo:  sudo bash uninstall.sh   (or  bash uninstall.sh --dry-run  to only look)' >&2
  exit 1
fi
if ! command -v python3 >/dev/null; then
  echo 'python3 is needed (it came with the installation; install it with your package manager)' >&2
  exit 1
fi
exec python3 "$ROOT/uninstaller.py" "$@"
