#!/bin/bash
# The tests that need a real browser, in the pinned Chromium of package-lock.json
# (Playwright 1.48.2, Chromium 130, build 1140). CI's "Browser tests" job and the rig
# (C:\neko-vhd\runs\browser-ci.sh) both run this script, so they run the same thing.
#
# Run it from nekoserver/, with the runtime built (runtime/build.py), as a user other than
# root (the runtime's web server refuses root), with
#   NODE_PATH                 the node_modules folder `npm ci` made from this folder
#   PLAYWRIGHT_BROWSERS_PATH  where `playwright install chromium` put the browser
# It fails when a test fails, and also when a browser test was skipped: a skip here
# means the browser was not found, which is the thing this job is for.
set -o pipefail
if [[ "$(id -u)" = 0 ]]; then
  echo "run.sh: run this as a user other than root" >&2
  exit 2
fi
NEKO_BROWSER=$(node -e "process.stdout.write(require('playwright').chromium.executablePath())") || exit 2
export NEKO_BROWSER
"$NEKO_BROWSER" --version || exit 2
log=$(mktemp)
cd tests || exit 2
# timed.py: unittest with a time limit on each test (a hang fails in minutes, with its stack).
python3 -B ../../.github/browser-tests/timed.py test_dashboard_browser test_backup_cards test_setup_window_look 2>&1 < /dev/null | tee "$log"
status=$?
if grep -E "skipped '(no browser here|no headless Chrome)" "$log"; then
  echo "::error title=Browser tests::a browser test was skipped: Playwright or its Chromium was not found"
  status=1
fi
rm -f "$log"
exit $status
