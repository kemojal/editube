#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${EDITUBE_PYTHON:-python3.12}"
VENV_DIR="${AUDIO_ENHANCE_VENV:-.venv-audio-enhance}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Enhance Audio needs Python 3.12. Set EDITUBE_PYTHON to a compatible interpreter."
  exit 1
fi

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --no-cache-dir --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install --no-cache-dir -r requirements-audio-enhance.txt

echo "Speech repair runtime ready at $VENV_DIR"
echo "The worker auto-detects it. For a custom path set AUDIO_DEEPFILTER_PYTHON."
