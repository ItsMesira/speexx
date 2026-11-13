#!/usr/bin/env bash
# Speexx AutoCycle - macOS run script.
# Usage: ./run.sh <username> <password>
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -ne 2 ]; then
  echo "usage: ./run.sh <username> <password>"
  exit 1
fi

PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "[run] creating virtualenv..."
  "$PY" -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c "import playwright" >/dev/null 2>&1; then
  echo "[run] installing dependencies..."
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt
  echo "[run] installing browser engine..."
  "$VENV/bin/python" -m playwright install chromium
  # Linux (Colab / Kaggle) needs the system libraries Chromium links against.
  if [ "$(uname -s)" = "Linux" ]; then
    if [ "$(id -u)" = "0" ]; then
      "$VENV/bin/python" -m playwright install-deps chromium || apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 || true
    elif command -v sudo >/dev/null 2>&1; then
      sudo "$VENV/bin/python" -m playwright install-deps chromium || true
    fi
  fi
fi

echo "[run] starting Speexx AutoCycle (Ctrl+C to stop)..."
exec "$VENV/bin/python" speexx_autocycle.py "$1" "$2"
