import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Resolve .env from the editube package root so DATABASE_URL is found regardless of cwd
# (e.g. `alembic upgrade head` from repo root vs. editube/).
_project_root = Path(__file__).resolve().parents[2]
load_dotenv(_project_root / ".env")
load_dotenv(_project_root / ".env.local")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            f"Export it or add it to {_project_root / '.env'} (see .env.example)."
        )
    if "sslmode=" not in url and "neon.tech" in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    return url


SQLALCHEMY_DATABASE_URL = _database_url()

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
