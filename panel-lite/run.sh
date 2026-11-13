#!/usr/bin/env bash
# run.sh — start the lite panel. Single operator, no accounts of its own.
#
#   ./run.sh            # 127.0.0.1:8788, tunnel if NGROK_AUTHTOKEN is set
#   ./run.sh 9000       # different port
#   ./stop.sh           # stop it
#
# Python: SPEEXX_LITE_PYTHON, else the sibling panel-cloud venv (it already
# has every dependency and the browser), else this folder's own .venv.
#
# Access: a PRIVATE Codespaces port needs no token. A public tunnel does —
# PANEL_LITE_TOKEN if you set one, otherwise run.sh generates one and prints
# it ONCE. The browser asks for it on first load and remembers it.
set -uo pipefail
cd "$(dirname "$0")"

PORT=8788
if [ "$#" -gt 0 ] && printf '%s' "$1" | grep -qE '^[0-9]+$'; then
  PORT="$1"
  shift
fi

PY="${SPEEXX_LITE_PYTHON:-}"

if [ -z "$PY" ]; then
  if [ -x "../panel-cloud/.venv/bin/python" ] \
     && "../panel-cloud/.venv/bin/python" -c "import fastapi, uvicorn, playwright, cryptography" >/dev/null 2>&1; then
    PY="$(cd ../panel-cloud && pwd)/.venv/bin/python"
    echo "[lite] using the panel-cloud venv: $PY"
  else
    echo "[lite] no shared venv; building this folder's own"
    PY="./.venv/bin/python"
    if [ ! -x "$PY" ]; then
      rm -rf .venv
      if ! python3 -m venv .venv 2>/dev/null; then
        echo "[lite] venv+ensurepip failed; retrying without bundled pip"
        rm -rf .venv
        python3 -m venv --without-pip .venv
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        .venv/bin/python /tmp/get-pip.py -q
      fi
      "$PY" -m pip install -q -r requirements-lite.txt
      "$PY" -m playwright install chromium
      if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" = "0" ]; then
        "$PY" -m playwright install-deps chromium || true
      elif [ "$(uname -s)" = "Linux" ] && command -v sudo >/dev/null 2>&1; then
        sudo "$PY" -m playwright install-deps chromium || true
      fi
    fi
  fi
fi

# The runner needs a browser; the shared venv already installed it.
if ! "$PY" -c "import playwright" >/dev/null 2>&1; then
  echo "[lite] ERROR: chosen python lacks playwright"; exit 1
fi

# Host allowlist: loopback + anything the environment asks for.
EXTRA="${SPEEXX_LITE_ALLOWED_HOSTS:-}"
WITH_TUNNEL=0
if [ -n "${NGROK_AUTHTOKEN:-}" ]; then
  WITH_TUNNEL=1
  case ",$EXTRA," in *ngrok-free*) : ;;
    *) EXTRA="${EXTRA:+$EXTRA,}*.ngrok-free.dev,*.ngrok-free.app" ;;
  esac
fi
if [ "${CODESPACES:-}" != "" ]; then
  case ",$EXTRA," in *app.github.dev*) : ;;
    *) EXTRA="${EXTRA:+$EXTRA,}*.app.github.dev" ;;
  esac
fi
export SPEEXX_LITE_ALLOWED_HOSTS="$EXTRA"

# Token: required whenever a public tunnel is up and none was provided.
if [ "$WITH_TUNNEL" = "1" ] && [ -z "${PANEL_LITE_TOKEN:-}" ]; then
  PANEL_LITE_TOKEN="$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")"
  export PANEL_LITE_TOKEN
  echo "[lite] generated access token (shown ONCE, do not lose it):"
  echo "[lite]   $PANEL_LITE_TOKEN"
elif [ -n "${PANEL_LITE_TOKEN:-}" ]; then
  echo "[lite] access token: set from the environment"
else
  echo "[lite] WARNING: no token and no tunnel — this is fine only behind a"
  echo "[lite]          PRIVATE Codespaces port. Public exposure without a"
  echo "[lite]          token lets anyone with the URL use the panel."
fi

if [ "$WITH_TUNNEL" = "1" ]; then
  rm -f lite_data/tunnel-url.txt
  nohup env PORT="$PORT" NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" \
    "$PY" tunnel_lite.py > /tmp/speexx-lite-tunnel.log 2>&1 &
  echo "[lite] tunnel starting (log: /tmp/speexx-lite-tunnel.log)"
fi

echo "[lite] panel on http://127.0.0.1:$PORT"
exec "$PY" -m uvicorn server_lite:app \
  --host 127.0.0.1 --port "$PORT" \
  --no-server-header \
  --proxy-headers \
  --forwarded-allow-ips '127.0.0.1'
