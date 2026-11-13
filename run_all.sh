#!/usr/bin/env bash
# run_all.sh — start BOTH projects in one command: the single-operator panel
# (panel-lite, with a public URL when possible) and the AutoCycle bot for
# every account in Accounts.md.
#
# Usage:
#   bash run_all.sh              # bot: 3.5 h window; panel-lite: 8788 + tunnel
#   bash run_all.sh 7200         # bot: 2 h window
#   bash run_all.sh stop         # stop both projects
#
# Accounts come from the SPEEXX_ACCOUNTS secret (one "email password" per line)
# or from Speexx-sonsole-cloud/Accounts.md (gitignored — never commit it).
# The tunnel uses NGROK_AUTHTOKEN when set; in Codespaces a PRIVATE forwarded
# port on 8788 needs no token at all.
set -uo pipefail
cd "$(dirname "$0")"

SONSOLE="Speexx-sonsole-cloud"
PANEL="panel-lite"
PORT=8788

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
# panel-lite builds its own venv inside run.sh (first run only); nothing to
# do here — but its first build + Chromium download can take a few minutes,
# which is why the URL wait below is generous.

# ---- panel-lite ---------------------------------------------------------------
URL_FILE="$PANEL/lite_data/tunnel-url.txt"
rm -f "$URL_FILE"
(cd "$PANEL" && nohup ./run.sh "$PORT" > /tmp/speexx-panel.log 2>&1 &)
for _ in $(seq 1 150); do
  [ -s "$URL_FILE" ] && break
  # first-run venv build prints progress into the log; a hard failure ends it
  grep -q "ERROR" /tmp/speexx-panel.log 2>/dev/null && break
  sleep 2
done

echo ""
echo "[run-all] panel running on http://127.0.0.1:$PORT"
if [ -s "$URL_FILE" ]; then
  echo "[run-all] public URL: $(cat "$URL_FILE" | tr -d '\n')"
  echo "[run-all]   ngrok shows a 'Visit Site' warning page once per browser"
  TOKEN="$(grep -A1 'shown ONCE' /tmp/speexx-panel.log 2>/dev/null | tail -1 | awk '{print $NF}')"
  if [ -n "$TOKEN" ]; then
    echo "[run-all] panel access token (paste once on the page): $TOKEN"
  fi
elif grep -q "ERROR" /tmp/speexx-panel.log 2>/dev/null; then
  echo "[run-all] panel failed to start — last log lines:"
  tail -n 8 /tmp/speexx-panel.log
elif [ -n "${CODESPACES:-}" ]; then
  echo "[run-all] open the Ports panel -> port $PORT (keep it Private — that IS the access control)"
  echo "[run-all] it is still building its venv; give it a minute, then check /tmp/speexx-panel.log"
else
  echo "[run-all] no tunnel URL — set NGROK_AUTHTOKEN for a public address"
  echo "[run-all] panel log: tail -f /tmp/speexx-panel.log"
fi

# ---- bot --------------------------------------------------------------------
(cd "$SONSOLE" && bash ./start.sh "$RUNTIME") > /tmp/speexx-bot.log 2>&1
tail -n 6 /tmp/speexx-bot.log 2>/dev/null

echo ""
echo "[run-all] both running. Watch:"
echo "[run-all]   bot sessions:   tail -f ~/speexx_autocycle_multi/session1_*.log.console"
echo "[run-all]   panel log:      tail -f /tmp/speexx-panel.log"
echo "[run-all] stop both with:  bash run_all.sh stop"
