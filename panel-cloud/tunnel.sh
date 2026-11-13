#!/usr/bin/env bash
# Public URL for the panel via ngrok.
#
# Two paths:
#  * Local (macOS): the ngrok binary with the authtoken already in ngrok's own
#    config (~/Library/Application Support/ngrok/ngrok.yml), so the assigned
#    *.ngrok-free.dev domain is STABLE across restarts — unlike a Cloudflare
#    quick tunnel, which hands out a random name every time.
#  * Cloud (Colab / Kaggle): pyngrok through the panel venv, driven by
#    NGROK_AUTHTOKEN from the environment (secret), via tunnel_ngrok.py.
#
# The URL is written to data/tunnel-url.txt so you never have to read the log.
#
# Start the panel with the matching host allowed, or it will answer
# 400 {"error":"unknown host"} — that is the allowlist working:
#
#   SPEEXX_PANEL_ALLOWED_HOSTS="127.0.0.1,localhost,*.ngrok-free.dev" ./run.sh
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
URL_FILE="data/tunnel-url.txt"
NGROK_API="http://127.0.0.1:4040/api/tunnels"

mkdir -p data && chmod 700 data

# ngrok's web UI listens on 4040; poll it for the public URL.
( for _ in $(seq 1 60); do
    url=$(curl -s --max-time 3 "$NGROK_API" 2>/dev/null \
          | python3 -c "
import sys, json
try:
    for t in json.load(sys.stdin).get('tunnels', []):
        if t.get('public_url', '').startswith('https://'):
            print(t['public_url']); break
except Exception:
    pass
" 2>/dev/null)
    if [ -n "${url:-}" ]; then printf '%s\n' "$url" > "$URL_FILE"; break; fi
    sleep 1
  done ) &

# Cloud first when pyngrok + a token are available; local binary otherwise.
if [ -n "${NGROK_AUTHTOKEN:-}" ] && [ -x .venv/bin/python ] \
   && .venv/bin/python -c "import pyngrok" >/dev/null 2>&1; then
  exec .venv/bin/python tunnel_ngrok.py
elif [ -x /opt/homebrew/bin/ngrok ]; then
  exec /opt/homebrew/bin/ngrok http "$PORT" --log=stdout --log-format=logfmt
elif command -v ngrok >/dev/null 2>&1; then
  exec ngrok http "$PORT" --log=stdout --log-format=logfmt
else
  echo "[tunnel] no ngrok available: set NGROK_AUTHTOKEN and 'pip install pyngrok'" >&2
  exit 1
fi
