from __future__ import annotations

import threading
from collections.abc import Iterator

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import connect_args_for

from .config import RequestLogSettings


# read_session_factory() creates its engine while holding this lock.  The
# engine factory uses the same guard, so it must be re-entrant; a plain Lock
# deadlocks the first internal-admin request before a reader session exists.
_lock = threading.RLock()
_engines: dict[tuple[str, str], Engine] = {}
_sessions: dict[tuple[str, str], sessionmaker] = {}


def _normalise_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _engine(kind: str, url: str) -> Engine:
    key = (kind, url)
    with _lock:
        engine = _engines.get(key)
        if engine is None:
            engine = create_engine(
                _normalise_url(url),
                pool_pre_ping=True,
                pool_recycle=300,
                pool_size=2,
                max_overflow=2,
                connect_args=connect_args_for(url),
            )
            _engines[key] = engine
        return engine


def write_engine(settings: RequestLogSettings) -> Engine:
    if not settings.write_database_url:
        raise RuntimeError("LOG_WRITE_DATABASE_URL is not configured")
    return _engine("write", settings.write_database_url)


def read_session_factory(settings: RequestLogSettings) -> sessionmaker:
    settings.validate_for_read()
    assert settings.read_database_url
    key = ("read", settings.read_database_url)
    with _lock:
        factory = _sessions.get(key)
        if factory is None:
            factory = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=_engine("read", settings.read_database_url),
            )
            _sessions[key] = factory
        return factory


def get_log_read_db() -> Iterator[Session]:
    try:
        settings = RequestLogSettings.from_env()
        factory = read_session_factory(settings)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db = factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def dispose_log_engines() -> None:
    with _lock:
        engines = list(_engines.values())
        _engines.clear()
        _sessions.clear()
    for engine in engines:
        engine.dispose()
