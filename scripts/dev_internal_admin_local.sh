#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
env_file="$repo_dir/.env.internal.local"

if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

cd "$repo_dir"
exec .venv/bin/uvicorn app.internal_admin:app \
  --host 127.0.0.1 \
  --port "${EDITUBE_INTERNAL_ADMIN_PORT:-8001}"
