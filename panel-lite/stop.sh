#!/usr/bin/env bash
# stop.sh — stop the lite panel (and its tunnel and runs).
#
# Order matters: TERM uvicorn first so its shutdown hook can TERM each run's
# process group cleanly (runner + browser share one group). Only then do we
# hard-kill anything left over. Patterns are scoped to panel-lite filenames —
# never broad (a bare `pkill python` would stop every speexx bot at once).
set -uo pipefail

term() { pkill -TERM -f "$1" 2>/dev/null; }

# 1) graceful: uvicorn's lifespan shutdown TERMs each job's process group
term "uvicorn server_lite:app"
sleep 2

# 2) leftovers: runners, tunnel
term "panel-lite/runner.py"
term "tunnel_lite.py"
sleep 2

# 3) stubborn ones get -9 (still scoped to panel-lite paths / exact module)
pkill -9 -f "uvicorn server_lite:app" 2>/dev/null
pkill -9 -f "panel-lite/runner.py" 2>/dev/null
pkill -9 -f "tunnel_lite.py" 2>/dev/null

# 4) last resort: a hard-killed runner can leave its Chromium behind. This
#    pattern also matches panel-cloud's browsers — only use it if nothing
#    else of the speexx stack is running you care about.
if [ "${LITE_FORCE_BROWSERS:-0}" = "1" ]; then
  pkill -9 -f "ms-playwright/chromium" 2>/dev/null
fi

echo "[lite] stopped."
