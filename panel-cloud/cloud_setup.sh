#!/usr/bin/env bash
# cloud_setup.sh — one-shot environment install for Google Colab / Kaggle.
# Builds the same stack the local run.sh builds: venv, Python deps, Chromium,
# Linux system libraries, plus pyngrok for the public tunnel.
# Run from this project's directory:
#     bash cloud_setup.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -x ".venv/bin/python" ]; then
  echo "[cloud] creating virtualenv"
  rm -rf .venv
  if ! "$PY" -m venv .venv 2>/dev/null; then
    # Colab's Debian Python has no working ensurepip — build the venv without
    # pip and bootstrap it with get-pip.py instead.
    echo "[cloud] venv+ensurepip failed; retrying without bundled pip"
    rm -rf .venv
    "$PY" -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python /tmp/get-pip.py -q
  fi
fi
if [ ! -x ".venv/bin/pip" ]; then
  echo "[cloud] ERROR: no .venv/bin/pip after bootstrap; aborting"
  exit 1
fi

echo "[cloud] installing python dependencies"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q pyngrok

echo "[cloud] installing Chromium for Playwright"
.venv/bin/python -m playwright install chromium

if [ "$(uname -s)" = "Linux" ]; then
  echo "[cloud] installing system libraries for Chromium"
  if [ "$(id -u)" = "0" ]; then
    if ! .venv/bin/python -m playwright install-deps chromium; then
      apt-get update -qq
      apt-get install -y -qq libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libasound2 || true
    fi
  elif command -v sudo >/dev/null 2>&1; then
    sudo .venv/bin/python -m playwright install-deps chromium || true
  else
    echo "[cloud] WARNING: no root and no sudo; Chromium may fail to launch"
  fi
fi

echo "[cloud] done. Next:"
echo "[cloud]   export NGROK_AUTHTOKEN=<token>   # for the public URL"
echo "[cloud]   export SPEEXX_PANEL_ADMIN_PASSWORD='choose one'  # optional first admin"
echo "[cloud]   ./run.sh --with-tunnel            # panel + public URL"
