#!/usr/bin/env bash
# Run the RQ worker with the same venv + .env as the API (rq is NOT on PATH without .venv).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".venv/bin/rq" ]]; then
  echo "Missing .venv/bin/rq. From editube/:"
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
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
echo "Starting RQ worker (queue=$QUEUE) with .venv/bin/rq …"
exec .venv/bin/rq worker --verbose -u "$REDIS_URL" "$QUEUE"
