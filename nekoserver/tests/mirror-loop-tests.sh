#!/bin/bash
# Storage from the dashboard (1.0.1), job D on loop devices: a mirror's lost drive replaced
# (tests/loop_mirror_add.py), a Btrfs pool and an mdadm mirror. Loop devices are the only
# disks the tool sees and may write to while NEKOSERVER_STORAGE_TEST=1. Needs root,
# losetup, sfdisk, wipefs, mkfs.ext4, mdadm and btrfs-progs (a test whose tools are missing
# is skipped), the btrfs and raid1 kernel modules, and about 1 GB of free space (sparse files):
#   sudo bash tests/mirror-loop-tests.sh
# Never in WSL: its shared kernel panics in md and restarts every distro (the mdadm tests
# skip there; this script refuses). Run it on the VM.
set -u
cd "$(dirname "$0")/.." || exit 2
case "$(uname -r)" in *[Mm]icrosoft*)
  echo 'Not in WSL: its kernel panics in md and takes every distro down. Run it on the VM.' >&2
  exit 2 ;;
esac
[[ "$(id -u)" = 0 ]] || {
  echo 'Run it as root: it makes loop devices, a Btrfs pool and an mdadm mirror.' >&2
  exit 2
}
modprobe btrfs 2>/dev/null
modprobe raid1 2>/dev/null
NEKOSERVER_STORAGE_TEST=1 exec python3 -m unittest -v tests.loop_mirror_add
