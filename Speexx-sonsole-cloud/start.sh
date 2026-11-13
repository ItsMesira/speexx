#!/usr/bin/env bash
# start.sh — one command to run everything.
# Stops any old sessions, then launches all accounts in Accounts.md in
# parallel for the given runtime window.
#
# Usage:
#   ./start.sh                # 3.5h (12600s) with Accounts.md
#   ./start.sh 7200           # 2h
#   ./start.sh 12600 Accounts.md
set -uo pipefail

RUNTIME="${1:-12600}"
ACCOUNTS="${2:-Accounts.md}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== stopping any old sessions =="
pkill -TERM -f 'speexx_autocycle.py' 2>/dev/null || true
pkill -TERM -f 'multi_run.sh' 2>/dev/null || true
sleep 4
pkill -9 -f 'speexx_autocycle.py' 2>/dev/null || true
pkill -9 -f 'multi_run.sh' 2>/dev/null || true
pkill -9 -f 'chrome-mac-arm64' 2>/dev/null || true
pkill -9 -f 'chrome-linux/' 2>/dev/null || true

if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "ERROR: $SCRIPT_DIR/.venv is missing — run 'bash cloud_setup.sh' first (fresh Colab/Kaggle session) or './run.sh <email> <password>' locally"
    exit 1
fi

if [ ! -f "$ACCOUNTS" ]; then
    echo "ERROR: $ACCOUNTS not found"
    exit 1
fi

N=$(grep -c '@' "$ACCOUNTS" || true)
echo "== launching $N session(s) for ${RUNTIME}s ($ACCOUNTS) =="
nohup "$SCRIPT_DIR/multi_run.sh" "$RUNTIME" "$ACCOUNTS" > "$HOME/speexx_autocycle_start.log" 2>&1 &
echo "started (pid $!)"
echo ""
echo "Watch progress:  tail -f ~/speexx_autocycle_multi_run.log"
echo "Per-session:     tail -f ~/speexx_autocycle_multi/session1_*.log"
echo "Stop everything: $SCRIPT_DIR/stop.sh"
