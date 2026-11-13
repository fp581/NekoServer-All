#!/bin/bash
# The card engine on real cards (tests_loop.rs): small ext4 filesystems in loop devices,
# mounted as setup mounts a card (rw,nosuid,nodev,noexec). For each case this script makes
# fresh cards, runs one ignored test, and kills the process or unmounts the card where the
# case says. Needs root, losetup, mkfs.ext4 and about 2 GB of free space for the images
# (sparse files). Run it from the runtime folder:
#   sudo bash ../tests/cardset-loop-tests.sh
# A process killed with SIGKILL is a simulated crash, not a power cut: what the process had
# handed to the kernel is still written (archive format 1, section 11).
set -u
cd "$(dirname "$0")/../runtime" || exit 2
[[ "$(id -u)" = 0 ]] || { echo 'Run it as root: it makes loop devices and mounts them.' >&2; exit 2; }
BIN=$(cargo test --no-run 2>&1 | sed -n 's/^ *Executable unittests src\/main.rs (\(.*\))$/\1/p' | tail -1)
[[ -x "$BIN" ]] || { echo 'The test binary was not built.' >&2; exit 2; }
BASE=$(mktemp -d /tmp/neko-loop.XXXXXX)
MOUNTS=()
DEVICES=()
FAILED=0

cleanup() {
  for m in "${MOUNTS[@]}"; do umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null; done
  for d in "${DEVICES[@]}"; do losetup -d "$d" 2>/dev/null; done
  rm -rf "$BASE"
  if findmnt -rn -o TARGET | grep -q "^$BASE/"; then echo "LEFT MOUNTED under $BASE" >&2; fi
}
trap cleanup EXIT

# card <name> <MiB>: a fresh ext4 card mounted at $BASE/<name>, which is left in CARD.
# Not called in a subshell, so that cleanup knows every mount and device.
card() {
  local name="$1" size_mib="$2"
  local img="$BASE/$name.img" mnt="$BASE/$name" dev
  truncate -s "${size_mib}M" "$img"
  mkfs.ext4 -q -m 0 -F "$img" >/dev/null
  dev=$(losetup -f --show "$img") || return 1
  DEVICES+=("$dev")
  mkdir -p "$mnt"
  mount -o rw,nosuid,nodev,noexec "$dev" "$mnt" || return 1
  MOUNTS+=("$mnt")
  CARD="$mnt"
}

device_of() {
  local folder="$1"
  findmnt -n -o SOURCE --target "$folder"
}

# run_test <name>: runs one ignored test of tests_loop.rs.
run_test() {
  local name="$1"
  "$BIN" --ignored --exact "cardset::tests_loop::$name" --nocapture --test-threads 1 2>&1 | tail -20
  return "${PIPESTATUS[0]}"
}

# result <what> <exit code>
result() {
  local what="$1" code="$2"
  if [[ "$code" = 0 ]]; then echo "PASS  $what"; else echo "FAIL  $what"; FAILED=1; fi
}

# wait_ready <work>: until the copy waits at its step (at most two minutes).
wait_ready() {
  local work="$1"
  for _ in $(seq 1200); do
    [[ -f "$work/signals/ready" ]] && return 0
    sleep 0.1
  done
  return 1
}

echo '== A01 on real cards: the first fills by its real free space, the second takes the rest'
export NEKO_LOOP_WORK="$BASE/work-a01"
card a01-1 320 && c1="$CARD" && card a01-2 400 && c2="$CARD"
NEKO_LOOP_CARDS="$c1,$c2" run_test loop_a_library_larger_than_one_card
result 'A01 a library larger than one card' $?

echo '== A32: the restore tool on each card, as an ordinary user, nothing but the cards'
# What a person does on another computer: the tool written onto the card, no catalogue,
# no NekoServer; one progress file across the two cards. The result must be the library.
RESTORER=$(getent passwd 1000 | cut -d: -f1)
RESTORER=${RESTORER:-root}
chmod 755 "$BASE" # the cards are mounted below it: an ordinary user may read them
HOME_OF="$BASE/as-$RESTORER"
mkdir -p "$HOME_OF" && chown "$RESTORER" "$HOME_OF"
OUT="$HOME_OF/restored"
runuser -u "$RESTORER" -- mkdir "$OUT"
code=0
for c in "$c1" "$c2"; do
  runuser -u "$RESTORER" -- python3 "$c/NekoBackup/tools/neko-restore.py" verify || code=1
  runuser -u "$RESTORER" -- python3 "$c/NekoBackup/tools/neko-restore.py" restore "$OUT" \
    --progress "$HOME_OF/progress.json" >/dev/null || code=1
done
diff -r "$NEKO_LOOP_WORK/a01" "$OUT" || code=1
[[ "$(find "$OUT" -name '.neko-restore-*' | wc -l)" = 0 ]] || code=1
result "A32 the tool on the cards restores the whole library as $RESTORER" "$code"

for point in Begun IntentWritten PayloadStarted PayloadWritten Checked VerifiedWritten Published \
  RecordTempWritten RecordPublished CatalogueCommitted; do
  echo "== killed with SIGKILL at $point (a simulated crash), then the card alone recovered"
  export NEKO_LOOP_WORK="$BASE/work-$point" NEKO_LOOP_KILL_AT="$point"
  card "kill-$point" 400
  export NEKO_LOOP_CARDS="$CARD"
  "$BIN" --ignored --exact cardset::tests_loop::loop_copy_until_killed --test-threads 1 >/dev/null 2>&1 &
  pid=$!
  if wait_ready "$NEKO_LOOP_WORK"; then
    kill -9 "$pid"
    wait "$pid" 2>/dev/null
    run_test loop_recover_after_kill
    result "killed at $point, recovered from the card" $?
  else
    kill -9 "$pid" 2>/dev/null
    result "killed at $point: the copy never reached the step" 1
  fi
done

echo '== the card unmounted while a file is written, then mounted again'
export NEKO_LOOP_WORK="$BASE/work-pulled"
card pulled 400
c="$CARD"
dev=$(device_of "$c")
export NEKO_LOOP_CARDS="$c"
run_test loop_card_pulled_mid_copy > "$BASE/pulled.log" 2>&1 &
pid=$!
if wait_ready "$NEKO_LOOP_WORK"; then
  umount -l "$c"
  touch "$NEKO_LOOP_WORK/signals/pulled"
  wait "$pid"
  code=$?
  cat "$BASE/pulled.log"
  result 'A11 card unmounted mid-copy: stopped, nothing in the bare folder' "$code"
  # The lazy unmount ends when the process closed the card's files: mount it again.
  for _ in $(seq 100); do findmnt -n "$dev" >/dev/null || break; sleep 0.1; done
  mount -o rw,nosuid,nodev,noexec "$dev" "$c"
  run_test loop_card_returns
  result 'A14 the card returns: the interrupted file copied again or finished from its verified intent' $?
else
  kill -9 "$pid" 2>/dev/null
  result 'A11 card unmounted mid-copy: the copy never reached the step' 1
fi

exit "$FAILED"
