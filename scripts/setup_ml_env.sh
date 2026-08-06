#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${EDITUBE_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.12 python3.13 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Background removal needs Python 3.11-3.13 (3.12 recommended)."
  exit 1
fi

VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$VERSION" in
  3.11|3.12|3.13) ;;
  *)
    echo "Background removal does not support Python $VERSION. Use Python 3.11-3.13."
    exit 1
    ;;
esac

VENV_DIR="${EDITUBE_VENV:-.venv312}"
if [[ ! -f "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --no-cache-dir --upgrade pip setuptools wheel
"$VENV_DIR/bin/python" -m pip install --no-cache-dir -r requirements.txt
# SAM 2's isolated build environment downloads a second copy of Torch. Reuse
# the already installed runtime instead; this saves hundreds of MB and avoids a
# common "no space left" failure on development machines.
"$VENV_DIR/bin/python" -m pip install --no-cache-dir --no-build-isolation -r requirements-ml.txt

echo "ML environment ready at $VENV_DIR"
echo "Run the API and worker with: EDITUBE_VENV=$VENV_DIR ./scripts/dev_with_worker.sh"
