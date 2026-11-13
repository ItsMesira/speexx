#!/usr/bin/env bash
# run_all.sh — start BOTH projects in one command: the panel (with a public URL
# when possible) and the AutoCycle bot for every account in Accounts.md.
#
# Usage:
#   bash run_all.sh              # bot: 3.5 h window; panel: 8787 + tunnel
#   bash run_all.sh 7200         # bot: 2 h window
#   bash run_all.sh stop         # stop both projects
#
# Accounts come from the SPEEXX_ACCOUNTS secret (one "email password" per line)
# or from Speexx-sonsole-cloud/Accounts.md (gitignored — never commit it).
# The tunnel uses NGROK_AUTHTOKEN when set; in Codespaces the built-in port
# forward on 8787 works without it.
set -uo pipefail
cd "$(dirname "$0")"

SONSOLE="Speexx-sonsole-cloud"
PANEL="panel-cloud"

if [ "${1:-}" = "stop" ]; then
  (cd "$SONSOLE" && ./stop.sh)
  (cd "$PANEL" && ./stop.sh)
  echo "[run-all] both projects stopped"
  exit 0
fi

RUNTIME="12600"
case "${1:-}" in
  ''|*[!0-9]*) : ;;
  *) RUNTIME="$1" ;;
esac

# ---- credentials ------------------------------------------------------------
ACCT="$SONSOLE/Accounts.md"
if [ ! -s "$ACCT" ] && [ -n "${SPEEXX_ACCOUNTS:-}" ]; then
  printf '%s\n' "$SPEEXX_ACCOUNTS" > "$ACCT"
  echo "[run-all] wrote Accounts.md from the SPEEXX_ACCOUNTS secret"
fi
if [ ! -s "$ACCT" ]; then
  echo "[run-all] ERROR: no accounts — set the SPEEXX_ACCOUNTS secret or edit $ACCT"
  exit 1
fi

# ---- environment (skipped when already built) --------------------------------
if [ ! -x "$SONSOLE/.venv/bin/python" ]; then
  bash "$SONSOLE/cloud_setup.sh" || exit 1
fi
if [ ! -x "$PANEL/.venv/bin/python" ]; then
  bash "$PANEL/cloud_setup.sh" || exit 1
fi

# ---- panel ------------------------------------------------------------------
URL_FILE="$PANEL/data/tunnel-url.txt"
rm -f "$URL_FILE"
(cd "$PANEL" && nohup ./run.sh 8787 --with-tunnel > /tmp/speexx-panel.log 2>&1 &)
for _ in $(seq 1 60); do
  [ -s "$URL_FILE" ] && break
  sleep 2
done

echo ""
echo "[run-all] panel running on http://127.0.0.1:8787"
if [ -s "$URL_FILE" ]; then
  echo "[run-all] public URL: $(cat "$URL_FILE" | tr -d '\n')"
  echo "[run-all]   ngrok shows a 'Visit Site' warning page once per browser"
elif [ -n "${CODESPACES:-}" ]; then
  echo "[run-all] open the Ports panel -> port 8787 (set it to Public for phones)"
else
  echo "[run-all] no tunnel URL — set NGROK_AUTHTOKEN for a public address"
fi

# ---- bot --------------------------------------------------------------------
(cd "$SONSOLE" && bash ./start.sh "$RUNTIME") > /tmp/speexx-bot.log 2>&1
tail -n 6 /tmp/speexx-bot.log 2>/dev/null

echo ""
echo "[run-all] both running. Watch:"
echo "[run-all]   bot sessions:   tail -f ~/speexx_autocycle_multi/session1_*.log.console"
echo "[run-all]   panel log:      tail -f /tmp/speexx-panel.log"
echo "[run-all] stop both with:  bash run_all.sh stop"
