#!/usr/bin/env bash
# Run the RQ worker with the same venv + .env as the API (rq is NOT on PATH without .venv).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${EDITUBE_VENV:-}"
if [[ -z "$VENV_DIR" ]]; then
  if [[ -f ".venv312/bin/rq" ]]; then
    VENV_DIR=".venv312"
  else
    VENV_DIR=".venv"
  fi
fi

if [[ ! -f "$VENV_DIR/bin/rq" ]]; then
  echo "Missing $VENV_DIR/bin/rq. From editube/:"
  echo "  ./scripts/setup_ml_env.sh"
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "Missing .env in $ROOT_DIR (need REDIS_URL)."
  exit 1
fi

set -a
source .env
set +a

if [[ -z "${REDIS_URL:-}" ]]; then
  echo "REDIS_URL is empty in .env"
  exit 1
fi

export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export PYTHONUNBUFFERED=1

QUEUE="${1:-default}"
echo "Starting instrumented RQ worker (queue=$QUEUE) with $VENV_DIR/bin/python …"
# SAM 2 / Metal cannot safely run in RQ's default forked work horse on macOS.
# SimpleWorker executes inside this clean process and also avoids duplicating
# multi-gigabyte model memory on Linux workers.
exec "$VENV_DIR/bin/python" -m app.rq_worker "$QUEUE"
