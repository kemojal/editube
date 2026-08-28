import re
import sys
import time
from logging.config import fileConfig
from sqlalchemy import create_engine
from sqlalchemy import pool
from sqlalchemy.exc import OperationalError
from alembic import context

# Import the Base and URL from app.db.database so migrations use the same DB as the app
from app.db.database import Base, SQLALCHEMY_DATABASE_URL, connect_args_for
# Import all models so Alembic can detect them for autogenerate
import app.db.models  # noqa: F401
import app.db.log_models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)

# Add your model's MetaData object here
target_metadata = Base.metadata

CONNECT_ATTEMPTS = 3


def _redacted_target() -> str:
    """host/dbname of the migration target, with credentials stripped."""
    return re.sub(r"://[^@/]*@", "://", SQLALCHEMY_DATABASE_URL).split("?")[0]


def _connect_with_retry(connectable):
    """Connect, retrying transient network failures.

    Neon sits behind DNS and a pooler that both blip: a laptop waking up, a VPN
    reconnecting, or a cold serverless endpoint all surface as OperationalError
    on the first attempt and succeed on the next. Retrying beats making someone
    re-read a sixty-frame traceback to learn their wifi dropped.
    """
    delay = 2
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return connectable.connect()
        except OperationalError as exc:
            detail = str(exc.orig).strip() if exc.orig is not None else str(exc)
            if attempt == CONNECT_ATTEMPTS:
                sys.stderr.write(
                    f"\nalembic: cannot reach the database after {CONNECT_ATTEMPTS} "
                    f"attempts.\n"
                    f"  target: {_redacted_target()}\n"
                    f"  cause:  {detail}\n"
                )
                if "translate host name" in detail or "Name or service not known" in detail:
                    sys.stderr.write(
                        "  DNS could not resolve the host. Check your network/VPN, "
                        "then retry.\n"
                    )
                sys.stderr.write(
                    "  No migrations ran; the database is unchanged.\n\n"
                )
                raise SystemExit(1) from None
            sys.stderr.write(
                f"alembic: database unreachable ({detail.splitlines()[0]}); "
                f"retrying in {delay}s "
                f"[attempt {attempt + 1}/{CONNECT_ATTEMPTS}]\n"
            )
            time.sleep(delay)
            delay *= 2


def run_migrations_offline():
    """Run migrations in 'offline' mode.
    This configures the context with just a URL
    and not an Engine, though an Engine is also acceptable
    here.  By skipping the Engine creation we don't even need a
    DBAPI to be available.
    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = SQLALCHEMY_DATABASE_URL
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, include_schemas=True
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """Run migrations in 'online' mode.
    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = create_engine(
        SQLALCHEMY_DATABASE_URL,
        poolclass=pool.NullPool,
        connect_args=connect_args_for(SQLALCHEMY_DATABASE_URL),
    )

    with _connect_with_retry(connectable) as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_schemas=True
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
