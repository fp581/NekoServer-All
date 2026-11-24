#!/bin/bash
# Storage from the dashboard (1.0.1) on loop devices: the one erase entry point
# (tests/loop_storage_jobs.py). Loop devices are the only disks the tool sees and may write
# to while NEKOSERVER_STORAGE_TEST=1. Needs root, losetup, sfdisk, wipefs, mkfs.ext4 and
# about 300 MB of free space (sparse files):
#   sudo bash tests/storage-loop-tests.sh
set -u
cd "$(dirname "$0")/.." || exit 2
[[ "$(id -u)" = 0 ]] || {
  echo 'Run it as root: it makes loop devices and erases them.' >&2
  exit 2
}
NEKOSERVER_STORAGE_TEST=1 exec python3 -m unittest -v tests.loop_storage_jobs
