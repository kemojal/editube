#!/bin/sh
set -e
if [ "${RUN_MIGRATIONS_ON_START:-0}" = "1" ]; then
  echo "Running alembic upgrade head..."
  alembic upgrade head
fi
exec "$@"
