#!/usr/bin/env bash
# Stop the panel and every bot it started.
#
# Scoped on purpose: this kills `server:app` and `runner.py`, which are the
# panel's own two processes. It does NOT use the broad `pkill -f` patterns from
# the sibling projects' stop.sh — those match the other bots too.
set -uo pipefail
cd "$(dirname "$0")"

echo "[stop] stopping panel and runners..."
pkill -TERM -f 'uvicorn server:app' 2>/dev/null || true
# Both the cloud copy's path (panel-cloud/runner.py) and the original's
# (panel/runner.py) — the pattern is a regex, the directory name differs.
pkill -TERM -f 'panel-cloud/runner.py|panel/runner.py' 2>/dev/null || true
sleep 3
pkill -9 -f 'uvicorn server:app' 2>/dev/null || true
pkill -9 -f 'panel-cloud/runner.py|panel/runner.py' 2>/dev/null || true
# Chromium instances the runners launched. Matched on the profile-less
# Playwright cache path so a sibling bot's browser is not caught.
pkill -9 -f 'ms-playwright/chromium' 2>/dev/null || true

echo "[stop] done"
