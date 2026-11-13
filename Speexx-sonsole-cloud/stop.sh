#!/usr/bin/env bash
# stop.sh — stop everything (all sessions, browsers)
set -uo pipefail

echo "stopping sessions..."
pkill -TERM -f 'speexx_autocycle.py' 2>/dev/null || true
pkill -TERM -f 'multi_run.sh' 2>/dev/null || true
sleep 5
pkill -9 -f 'speexx_autocycle.py' 2>/dev/null || true
pkill -9 -f 'multi_run.sh' 2>/dev/null || true
pkill -9 -f 'chrome-mac-arm64' 2>/dev/null || true
pkill -9 -f 'chrome-linux/' 2>/dev/null || true

echo "stopped"
