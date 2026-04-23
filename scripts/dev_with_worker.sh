#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".venv/bin/python" ]]; then
  echo "Missing virtualenv at .venv. Create it first:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "Missing .env in $ROOT_DIR"
  exit 1
fi

set -a
source .env
set +a

if [[ -z "${REDIS_URL:-}" ]]; then
  echo "REDIS_URL is empty. Set REDIS_URL in .env to run worker jobs."
  exit 1
fi

export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"

cleanup() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    kill "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting RQ worker on queue 'default'..."
echo "Note: plain 'rq worker' fails unless the venv is active — this script uses .venv/bin/rq."
echo "To run only the worker later: ./scripts/rq_worker.sh"
export PYTHONUNBUFFERED=1
.venv/bin/rq worker --verbose -u "$REDIS_URL" default &
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
exec .venv/bin/python -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" --reload
