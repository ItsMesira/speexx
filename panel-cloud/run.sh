#!/usr/bin/env bash
# Start the panel. Binds to loopback unless you explicitly ask otherwise.
#
#   ./run.sh                  # 127.0.0.1:8787
#   ./run.sh 9000             # different port
#   ./run.sh 8787 --allow-remote   # bind 0.0.0.0 (see the warning below)
#   ./run.sh --with-tunnel    # also start the ngrok tunnel and print its URL
#   ./run.sh --reset-admin    # drop admin accounts; the next sign-up owns the panel
#   ./run.sh --reset          # wipe everything: accounts, saved logins, runs, audit
#   ./run.sh --open-signup    # let anyone register without admin approval
#
# --with-tunnel implies SPEEXX_PANEL_ALLOWED_HOSTS gains "*.ngrok-free.dev",
# otherwise the tunnel reaches the panel and the panel refuses it with 400.
#
# Admin password, two ways:
#   SPEEXX_PANEL_ADMIN_EMAIL='you@example.com' SPEEXX_PANEL_ADMIN_PASSWORD='one you choose' \
#       ./run.sh                                             # first admin, never touches disk
#   ./run.sh --reset-admin                                   # generates one, prints once
# Leaving it unset and never resetting is how you end up locked out: only the
# hash is stored, so the original cannot be recovered.
set -euo pipefail
cd "$(dirname "$0")"

# The port is optional and may appear anywhere; flags must not be mistaken for it.
PORT=8787
if [ "$#" -gt 0 ] && printf '%s' "$1" | grep -qE '^[0-9]+$'; then
  PORT="$1"
  shift
fi
HOST="127.0.0.1"
RESET_ADMIN=0
RESET_ALL=0
WITH_TUNNEL=0
OPEN_SIGNUP=0
for arg in "$@"; do
  case "$arg" in
    --allow-remote) HOST="0.0.0.0" ;;
    --reset-admin)  RESET_ADMIN=1 ;;
    --reset|--reset-all) RESET_ALL=1 ;;
    --open-signup)  OPEN_SIGNUP=1 ;;
    --with-tunnel)  WITH_TUNNEL=1 ;;
    --*) echo "[run] ignoring unknown option: $arg";;
  esac
done

# Asking for the tunnel implies you want to reach it through it. ngrok's free
# tier assigns domains from both *.ngrok-free.dev and *.ngrok-free.app depending
# on the account/region, so allow both — the *.suffix matcher covers either.
if [ "$WITH_TUNNEL" = "1" ]; then
  case "${SPEEXX_PANEL_ALLOWED_HOSTS:-}" in
    *ngrok-free*) : ;;
    *)
      if [ -n "${SPEEXX_PANEL_ALLOWED_HOSTS:-}" ]; then
        SPEEXX_PANEL_ALLOWED_HOSTS="$SPEEXX_PANEL_ALLOWED_HOSTS,*.ngrok-free.dev,*.ngrok-free.app"
      else
        SPEEXX_PANEL_ALLOWED_HOSTS="127.0.0.1,localhost,*.ngrok-free.dev,*.ngrok-free.app"
      fi
      export SPEEXX_PANEL_ALLOWED_HOSTS
      ;;
  esac
fi

# Codespaces forwards ports at https://<codespace>-8787.app.github.dev. That
# host must be in the allowlist or the panel answers 400 {"error":"unknown
# host"} — the tunnel is built in, so this is on whenever CODESPACES is set.
if [ "${CODESPACES:-}" != "" ]; then
  case "${SPEEXX_PANEL_ALLOWED_HOSTS:-}" in
    *app.github.dev*) : ;;
    *)
      SPEEXX_PANEL_ALLOWED_HOSTS="${SPEEXX_PANEL_ALLOWED_HOSTS:+$SPEEXX_PANEL_ALLOWED_HOSTS,}*.app.github.dev"
      export SPEEXX_PANEL_ALLOWED_HOSTS
      ;;
  esac
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[run] creating virtualenv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.14 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
  fi
fi

if ! .venv/bin/python -c "import playwright" >/dev/null 2>&1; then
  echo "[run] installing browser engine"
  .venv/bin/python -m playwright install chromium
  # Linux (Colab / Kaggle) needs the system libraries Chromium links against.
  if [ "$(uname -s)" = "Linux" ]; then
    if [ "$(id -u)" = "0" ]; then
      .venv/bin/python -m playwright install-deps chromium || true
    elif command -v sudo >/dev/null 2>&1; then
      sudo .venv/bin/python -m playwright install-deps chromium || true
    fi
  fi
fi

# The cloud tunnel path (tunnel.sh -> tunnel_ngrok.py) needs pyngrok in the venv.
# Installing it here keeps './run.sh --with-tunnel' self-sufficient on Colab/Kaggle.
if [ "$WITH_TUNNEL" = "1" ] && [ -n "${NGROK_AUTHTOKEN:-}" ]; then
  if ! .venv/bin/python -c "import pyngrok" >/dev/null 2>&1; then
    echo "[run] installing pyngrok for the cloud tunnel"
    .venv/bin/pip install -q pyngrok
  fi
fi

# Refuse to expose the panel by accident. This service takes Speexx passwords
# and can drive a browser on this machine; binding it to every interface is a
# decision, not a default.
if [ "$HOST" = "0.0.0.0" ]; then
  echo "[run] WARNING: binding 0.0.0.0 — the panel will accept connections from"
  echo "[run]          the network. Put Cloudflare Access or an equivalent in"
  echo "[run]          front of it, or use a tunnel to 127.0.0.1 instead."
fi

if [ "$RESET_ALL" = "1" ]; then
  # Everything. Accounts, encrypted saved logins, runs, sessions, audit, rate
  # limits and cooldowns -- so the vault key too, since it would be worthless
  # without them and leaving it behind just looks like saved passwords survived.
  # Job logs are kept; they are the only record of what a run did.
  echo "[run] wiping every account, saved login, run and audit row"
  .venv/bin/python - <<'PYEOF'
import os, sys, pathlib
sys.path.insert(0, ".")
os.environ.pop("SPEEXX_PANEL_VAULT_KEY", None)
import db, users
for table in ("sessions", "speexx_accounts", "jobs", "audit", "rate_limits",
              "cooldowns", "users"):
    db.execute("DELETE FROM %s" % table)
db.set_meta("reset_at", str(db.now()))
for name in ("vault.key",):
    key = db.DATA_DIR / name
    if key.exists():
        key.unlink()
        print("[run]   removed %s" % key.name)
print("[run] no admin yet; the next person to sign up owns the panel")
PYEOF
fi

if [ "$OPEN_SIGNUP" = "1" ]; then
  # Off by default: sign-up lands in the admin queue instead of being usable.
  export SPEEXX_PANEL_OPEN_SIGNUP=1
fi

if [ "$RESET_ADMIN" = "1" ]; then
  # There is no stored admin password any more -- admins are rows in `users`,
  # so "reset" means: remove every admin, and let bootstrap_admin() print a
  # fresh one on the next start. Sessions go too, since they outlive the role.
  echo "[run] removing admin accounts and sessions; the next sign-up owns the panel"
  .venv/bin/python - <<'PYEOF'
import os, sys
sys.path.insert(0, ".")
os.environ.setdefault("SPEEXX_PANEL_ADMIN_PASSWORD", "")
import db, users
db.execute("DELETE FROM sessions")
db.execute("DELETE FROM users WHERE role = 'admin'")
users.set_status  # noqa: B018 - keeps the import meaningful for readers
db.audit(None, "admin_reset", "run.sh --reset-admin")
PYEOF
fi

if [ "$WITH_TUNNEL" = "1" ]; then
  if [ ! -x ./tunnel.sh ]; then
    echo "[run] tunnel.sh is missing; continuing without a tunnel"
  else
    echo "[run] starting the ngrok tunnel..."
    rm -f data/tunnel-url.txt
    nohup ./tunnel.sh >/tmp/speexx-tunnel.log 2>&1 &
    for _ in $(seq 1 60); do
      [ -s data/tunnel-url.txt ] && break
      sleep 1
    done
    _url="$(cat data/tunnel-url.txt 2>/dev/null | tr -d '\n')"
    if [ -n "$_url" ]; then
      echo "[run] tunnel up: $_url"
      echo "[run]   first visit shows ngrok's warning page — click 'Visit Site' once."
    else
      echo "[run] WARNING: the tunnel did not report a URL; see /tmp/speexx-tunnel.log"
    fi
  fi
fi

# No password is printed any more: the first person to sign up is the admin, so
# there is nothing to read out of a log.
echo "[run] to set the admin yourself, set SPEEXX_PANEL_ADMIN_PASSWORD before starting"
echo "[run] panel on http://$HOST:$PORT"
exec .venv/bin/python -m uvicorn server:app \
  --host "$HOST" --port "$PORT" \
  --no-server-header \
  --proxy-headers \
  --forwarded-allow-ips '127.0.0.1'
