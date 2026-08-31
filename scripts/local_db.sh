#!/usr/bin/env bash
# A local Postgres for development — the biggest remaining dev-speed lever.
#
# Every query against Neon in us-east-2 costs ~250ms from a laptop, and each
# endpoint runs several. The same query against a local Postgres costs ~0.2ms,
# which is the difference between an editor that opens in seconds and one that
# opens instantly.
#
#   ./scripts/local_db.sh up      create the database and build its schema
#   ./scripts/local_db.sh migrate apply new migrations to it
#   ./scripts/local_db.sh sync    copy the remote data into it (read-only on the remote)
#   ./scripts/local_db.sh url     print the DATABASE_URL line for .env
#   ./scripts/local_db.sh reset   drop and rebuild it, empty
#
# Opting in is a single .env edit — this script never touches .env:
#   DATABASE_URL=<output of `url`>
# Keep the Neon line commented next to it so switching back is one edit, then
# restart the API and the RQ worker.
#
# Override the target with PGHOST / PGPORT / PGUSER / LOCAL_DB_NAME.
set -euo pipefail
cd "$(dirname "$0")/.."

PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-$(whoami)}"
DB_NAME="${LOCAL_DB_NAME:-editube_dev}"
LOCAL_URL="postgresql://${PGUSER}@${PGHOST}:${PGPORT}/${DB_NAME}"
export PGHOST PGPORT PGUSER

# Postgres.app and Homebrew keep their client tools off the default PATH, and
# several versions are often installed side by side. Always pick the newest:
# pg_dump refuses to dump a server newer than itself, so an older set of tools
# earlier in PATH would break `sync` with a confusing error.
newest_bin=""
newest_major=0
for candidate in \
  /opt/homebrew/opt/postgresql@*/bin \
  /usr/local/opt/postgresql@*/bin \
  /Applications/Postgres.app/Contents/Versions/*/bin
do
  [ -x "$candidate/pg_dump" ] || continue
  major="$("$candidate/pg_dump" --version 2>/dev/null | grep -oE '[0-9]+' | head -1)"
  [ -n "$major" ] || continue
  if [ "$major" -gt "$newest_major" ]; then
    newest_major="$major"
    newest_bin="$candidate"
  fi
done
[ -n "$newest_bin" ] && PATH="$newest_bin:$PATH"
export PATH

python_bin() {
  if [ -x ".venv312/bin/python" ]; then echo ".venv312/bin/python"
  elif [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"
  else echo "python3"; fi
}
alembic_bin() {
  if [ -x ".venv312/bin/alembic" ]; then echo ".venv312/bin/alembic"
  elif [ -x ".venv/bin/alembic" ]; then echo ".venv/bin/alembic"
  else echo "alembic"; fi
}

require_server() {
  if ! psql -d postgres -tAc "select 1" >/dev/null 2>&1; then
    cat >&2 <<MSG
Cannot reach Postgres at ${PGHOST}:${PGPORT} as ${PGUSER}.

Start one, then re-run:
  Postgres.app   open it from /Applications
  Homebrew       brew services start postgresql@15
MSG
    exit 1
  fi
}

db_exists() {
  psql -d postgres -tAc "select 1 from pg_database where datname='${DB_NAME}'" | grep -q 1
}

# The base tables predate Alembic: the initial revision only renames a column
# (its create_table calls live in downgrade), so `alembic upgrade head` cannot
# build an empty database. models.py is the real source of truth — create the
# schema from it, then stamp Alembic so later migrations apply normally.
build_schema() {
  DATABASE_URL="$LOCAL_URL" "$(python_bin)" - <<'PY'
import warnings
warnings.filterwarnings("ignore")
from sqlalchemy import text
from app.db.database import Base, engine
import app.db.models  # noqa: F401  — importing registers every table

with engine.begin() as conn:
    for schema in ("repurpose", "community", "aiugc"):
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
Base.metadata.create_all(engine)

with engine.connect() as conn:
    count = conn.execute(
        text(
            "select count(*) from information_schema.tables "
            "where table_schema in ('public','repurpose','community','aiugc')"
        )
    ).scalar()
print(f"schema built: {count} tables")
PY
  DATABASE_URL="$LOCAL_URL" "$(alembic_bin)" stamp head >/dev/null
  echo "alembic stamped at head"
}

# The database to copy from. Once .env has been pointed at the local copy its
# active DATABASE_URL is local, so every postgres URL in the file is
# considered — including commented-out ones, which is where the remote line
# normally ends up — and local targets are filtered out.
# One awk pass rather than a grep pipeline: under `set -o pipefail` a `head -1`
# that closes the pipe early makes the whole pipeline non-zero, and `set -e`
# then kills the script with no output at all.
remote_url() {
  awk '
    match($0, /postgresql:\/\/[^"'"'"' ]+/) {
      url = substr($0, RSTART, RLENGTH)
      if (url !~ /@(localhost|127\.0\.0\.1)/) { print url; exit }
    }
  ' .env .env.local 2>/dev/null || true
}

case "${1:-up}" in
  up)
    require_server
    if db_exists; then
      echo "Database ${DB_NAME} already exists — leaving its data alone."
      echo "Use 'reset' to rebuild it empty, or 'migrate' to apply new migrations."
    else
      createdb "$DB_NAME"
      build_schema
    fi
    echo
    echo "Point the API at it by setting in .env:"
    echo "  DATABASE_URL=$LOCAL_URL"
    echo "Then restart the API and the RQ worker. 'sync' copies your remote data in."
    ;;

  migrate)
    require_server
    db_exists || { echo "Database ${DB_NAME} does not exist — run 'up' first." >&2; exit 1; }
    DATABASE_URL="$LOCAL_URL" "$(alembic_bin)" upgrade head
    ;;

  sync)
    require_server
    db_exists || { echo "Database ${DB_NAME} does not exist — run 'up' first." >&2; exit 1; }
    # An explicit source wins; otherwise the remote is found in .env.
    REMOTE_URL="${2:-$(remote_url)}"
    if [ -z "$REMOTE_URL" ]; then
      cat >&2 <<MSG
No remote postgres URL found in .env to copy from.

Pass one explicitly:
  ./scripts/local_db.sh sync 'postgresql://user:pass@host/dbname?sslmode=require'
MSG
      exit 1
    fi

    # libpq fills anything a connection string omits from PGHOST/PGPORT/PGUSER,
    # and remote URLs rarely carry a port — so without clearing those the
    # "remote" connection dials the remote host on the *local* port.
    remote() { env -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE "$@"; }

    server_major="$(remote psql "$REMOTE_URL" -tAc 'show server_version' 2>/dev/null | cut -d. -f1 || true)"
    if [ -z "$server_major" ]; then
      echo "Could not connect to the remote database to read its version." >&2
      echo "Check the URL in .env (or pass one: $0 sync '<url>')." >&2
      exit 1
    fi
    # pg_dump refuses to dump a server newer than itself.
    dump_major="$(pg_dump --version | grep -oE '[0-9]+' | head -1)"
    if [ -n "$server_major" ] && [ "$dump_major" -lt "$server_major" ]; then
      cat >&2 <<MSG
pg_dump is ${dump_major} but the remote server is ${server_major}; pg_dump cannot
dump a newer server. Install matching client tools and re-run:

  brew install postgresql@${server_major}
  PATH="/opt/homebrew/opt/postgresql@${server_major}/bin:\$PATH" ./scripts/local_db.sh sync

The schema built by 'up' does not need this — only copying data does.
MSG
      exit 1
    fi

    # The dump carries the whole schema, so restore into a freshly created
    # database: layering it over an existing schema produces a wall of
    # "already exists" noise that hides real failures. Alembic's version row
    # rides along in the dump, so no stamping is needed afterwards.
    echo "Copying data from the remote database (read-only on the remote)..."
    dropdb --if-exists "$DB_NAME"
    createdb "$DB_NAME"
    remote pg_dump "$REMOTE_URL" --no-owner --no-privileges \
      | psql -d "$DB_NAME" -v ON_ERROR_STOP=0 -q
    echo "Sync complete."
    ;;

  url)
    echo "DATABASE_URL=$LOCAL_URL"
    ;;

  reset)
    require_server
    dropdb --if-exists "$DB_NAME"
    createdb "$DB_NAME"
    build_schema
    echo "Rebuilt ${DB_NAME}, empty."
    ;;

  *)
    echo "Usage: $0 {up|migrate|sync|url|reset}" >&2
    exit 1
    ;;
esac
