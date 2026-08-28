#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${EDITUBE_VENV:-}"
if [[ -z "$VENV_DIR" ]]; then
  if [[ -f ".venv312/bin/python" ]]; then
    VENV_DIR=".venv312"
  else
    VENV_DIR=".venv"
  fi
fi

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
  echo "Missing virtualenv at $VENV_DIR. Create the ML-ready environment first:"
  echo "  ./scripts/setup_ml_env.sh"
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "Missing .env in $ROOT_DIR"
  exit 1
fi

set -a
source .env
set +a

# The local public API and worker must never inherit the privileged request-log
# read connection. Keep the fail-closed check in app.main for every other
# deployment, while making this public development launcher safe by default.
if [[ -n "${LOG_READ_DATABASE_URL:-}" ]]; then
  echo "Ignoring LOG_READ_DATABASE_URL for the public API and worker."
  echo "Run app.internal_admin separately with an internal-admin-only environment."
fi
export LOG_READ_DATABASE_URL=""

if [[ -z "${REDIS_URL:-}" ]]; then
  echo "REDIS_URL is empty. Set REDIS_URL in .env to run worker jobs."
  exit 1
fi

export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
# This script owns the worker. Prevent FastAPI's local-worker watchdog from
# launching a second process against the same queue.
export AUTO_START_RQ_WORKER=false
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"

cleanup() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    kill "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting RQ worker on queue 'default'..."
echo "Using Python environment: $VENV_DIR"
echo "Note: plain 'rq worker' fails unless the venv is active — this script uses $VENV_DIR/bin/rq."
echo "To run only the worker later: ./scripts/rq_worker.sh"
export PYTHONUNBUFFERED=1
"$VENV_DIR/bin/python" -m app.rq_worker default &
WORKER_PID=$!

if lsof -iTCP:"$API_PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "Port $API_PORT is already in use. Assuming API is already running."
  echo "Keeping only the RQ worker attached. Press Ctrl+C to stop this worker."
  echo "Tip: If new routes (e.g. GET /health/queue) return 404, restart the API process on this port or use --reload."
  echo "Tip: Transcription can sit on 'ffmpeg' or 'Whisper' for several minutes with no new lines — that is normal on CPU."
  wait "$WORKER_PID"
  exit $?
fi

echo "Starting FastAPI server on http://${API_HOST}:${API_PORT} (with --reload) ..."
exec "$VENV_DIR/bin/python" -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" --reload
