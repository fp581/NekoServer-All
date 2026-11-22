#!/bin/bash
# Backup cards on real cards (runtime/src/cards/tests_loop.rs): two small ext4 filesystems
# in loop devices, mounted as a desktop mounts a card (rw,nosuid,nodev), one of them with a
# file already on it. The page lists them, enrols the empty one, and the job fills it to
# its reserve. Needs root, losetup, mkfs.ext4 and about 1 GB of free space (sparse files).
# Run it from the runtime folder, in a mount namespace of its own if you like:
#   sudo unshare -m bash ../tests/cards-loop-tests.sh
# BASE (optional) is where the cards and the work folder go.
set -u
cd "$(dirname "$0")/../runtime" || exit 2
[[ "$(id -u)" = 0 ]] || {
  echo 'Run it as root: it makes loop devices and mounts them.' >&2
  exit 2
}
BIN=$(cargo test --no-run 2>&1 | sed -n 's/^ *Executable unittests src\/main.rs (\(.*\))$/\1/p' | tail -1)
[[ -x "$BIN" ]] || {
  echo 'The test binary was not built.' >&2
  exit 2
}
BASE=${BASE:-$(mktemp -d /tmp/neko-cards.XXXXXX)}
mkdir -p "$BASE"
MOUNTS=()
DEVICES=()

cleanup() {
  for m in "${MOUNTS[@]}"; do umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null; done
  for d in "${DEVICES[@]}"; do losetup -d "$d" 2>/dev/null; done
  rm -rf "$BASE"
}
trap cleanup EXIT

# card <name> <MiB>: a fresh ext4 card mounted at $BASE/<name>.
card() {
  local name="$1" size_mib="$2"
  local img="$BASE/$name.img" mnt="$BASE/$name" dev
  truncate -s "${size_mib}M" "$img"
  mkfs.ext4 -q -m 0 -F "$img" >/dev/null
  dev=$(losetup -f --show "$img") || return 1
  DEVICES+=("$dev")
  mkdir -p "$mnt"
  mount -o rw,nosuid,nodev "$dev" "$mnt" || return 1
  MOUNTS+=("$mnt")
}

card empty 400 || exit 2
card used 64 || exit 2
echo 'a photo of someone else' >"$BASE/used/holiday.jpg"
mkdir -p "$BASE/work"
NEKO_CARD="$BASE/empty" NEKO_USED_CARD="$BASE/used" NEKO_WORK="$BASE/work" \
  "$BIN" --ignored --exact cards::tests_loop::a_real_card_is_enrolled_from_the_page_and_filled_by_the_job \
  --nocapture --test-threads 1 2>&1 | tail -20
code=${PIPESTATUS[0]}
if [[ "$code" = 0 ]]; then echo 'PASS  a real card enrolled from the page and filled by the job'; else echo 'FAIL  a real card enrolled from the page and filled by the job'; fi
exit "$code"
