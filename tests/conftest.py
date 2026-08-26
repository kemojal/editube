"""Shared test fixtures.

Two harnesses live here.

`db_session` — an in-memory SQLite session with the whole public schema
created. Tests used to hand-roll this per file (see the original
`tests/test_video_versions.py`), each repeating the JSONB compile shim and an
explicit table subset; the subset existed only because nobody had shimmed
`ARRAY`, which meant any table touching `review_links` or
`workspace_auth_policies` blew up at CREATE. Both are shimmed below, so the
full public schema builds and tests can just ask for a session.

`api_client` — a `TestClient` with the database and authentication
dependencies overridden. There was no route-level testing in this codebase
before; endpoint behaviour (status transitions, permission failures, approval
blockers) was only reachable by calling handler functions directly with
`SimpleNamespace` fakes.

Three tables live in non-default Postgres schemas (`community`, `repurpose`,
`aiugc`). SQLite has no schema concept, so they are skipped — tests for those
subsystems need Postgres.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterator

import pytest
from sqlalchemy import ARRAY as SA_ARRAY
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# --- SQLite type shims -------------------------------------------------------
# Registered at import time, before any engine is built. Postgres-only column
# types degrade to JSON, which SQLAlchemy round-trips as Python lists/dicts —
# close enough for behavioural tests, and the alternative (a live Postgres for
# every unit test) is not.


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


@compiles(SA_ARRAY, "sqlite")
def _compile_array_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


@compiles(PG_ARRAY, "sqlite")
def _compile_pg_array_for_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
    return "JSON"


def _teach_array_to_speak_sqlite() -> None:
    """Give ARRAY columns JSON bind/result processors under SQLite.

    The `@compiles` hooks above only rewrite DDL — they say how to *create* the
    column, not how to bind a Python list to it, so inserts still died with
    "type 'list' is not supported". The dialect-level `colspecs` hook is not
    consulted for these columns either. Patching the processors on the type
    classes, guarded on dialect name, is the narrow fix: Postgres runs are
    completely unaffected because the guard falls through to the original.
    """
    for array_type in (SA_ARRAY, PG_ARRAY):
        original_bind = array_type.bind_processor
        original_result = array_type.result_processor

        def _make(bind_impl, result_impl):  # noqa: ANN001, ANN202
            def bind_processor(self, dialect):  # noqa: ANN001, ANN202
                if dialect.name == "sqlite":
                    return lambda value: None if value is None else json.dumps(list(value))
                return bind_impl(self, dialect)

            def result_processor(self, dialect, coltype):  # noqa: ANN001, ANN202
                if dialect.name == "sqlite":
                    return lambda value: None if value is None else json.loads(value)
                return result_impl(self, dialect, coltype)

            return bind_processor, result_processor

        array_type.bind_processor, array_type.result_processor = _make(
            original_bind, original_result
        )


_teach_array_to_speak_sqlite()


def _public_tables() -> list:
    """Every table in the default schema, in dependency order."""
    from app.db.database import Base
    import app.db.models  # noqa: F401  — registers the mappers

    return [t for t in Base.metadata.sorted_tables if t.schema is None]


def all_public_tables() -> list:
    """The whole public schema, for tests that hand-roll their own engine.

    Several older test modules declare an explicit `tables = [...]` subset and
    build their own SQLite engine. That breaks the moment production code
    starts reading a table the subset never listed — which has already
    happened twice as the review spine grew (`video_approvals` when the video
    payload began reporting decisions, `activity_feed` when a new version
    started logging its status reset).

    The failure is loud rather than silent, so it is safe to leave those
    modules alone until they break. When one does, swap its list for this:

        from tests.conftest import all_public_tables
        tables = all_public_tables()

    Creating extra empty tables cannot change what a test asserts, so the
    swap is strictly additive. New tests should prefer the `db_session`
    fixture and skip the question entirely.
    """
    return _public_tables()


@pytest.fixture(autouse=True)
def never_touch_real_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the suite from reaching Redis, email, or push. Always.

    This is not belt-and-braces; it is repairing real damage. Route modules
    import their job helpers at module scope::

        from app.jobs.queue import enqueue_comment_notification_email_job

    which binds the *function object* into the route's namespace at import
    time. Patching `app.jobs.queue.enqueue_...` therefore does nothing to the
    reference the route actually calls — the test passes, and the job is
    enqueued for real. With `REDIS_URL` set in `.env`, a test run put 35 live
    email jobs on the production queue and a worker began delivering them to a
    real inbox, about a video that only ever existed in a fixture.

    So the block happens one layer lower, at the RQ boundary in
    `app.jobs.queue`, where every enqueue helper converges. Individual tests
    can still patch a specific helper to assert it was called; this only
    guarantees that nothing escapes if they forget.
    """
    def _refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError(
            "A test tried to reach real infrastructure. Patch the helper in "
            "the module that CALLS it (e.g. "
            "`app.api.routes.review_links.enqueue_comment_notification_email_job`), "
            "not in `app.jobs.queue` — routes bind these at import time, so "
            "patching the source module has no effect."
        )

    # The guard. Every enqueue_* helper starts with
    # `os.environ.get("REDIS_URL", "").strip()` and returns early when it is
    # empty, logging "not enqueued". Removing the variable disarms all of them
    # at once, through a branch the code already has — which is exactly what
    # would have stopped those 35 emails.
    #
    # Deliberately not also stubbing `redis.Redis.from_url`: tests that mean to
    # exercise queue behaviour re-add REDIS_URL and supply their own Redis
    # mock (see `tests/test_effect_reconcile.py`), and a blanket stub steals
    # the connection out from under them.
    monkeypatch.delenv("REDIS_URL", raising=False)

    # Direct sends never touch the queue, so they need their own block.
    try:
        import app.utils.email as email_module

        for sender in dir(email_module):
            if sender.startswith("send_"):
                monkeypatch.setattr(email_module, sender, _refuse, raising=False)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A committed-per-test in-memory database.

    Uses `StaticPool` with a single shared connection so the session handed to
    the test and the session handed to a request handler see the same data —
    without it, each connection gets its own empty `:memory:` database.
    """
    from app.db.database import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_public_tables())
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class ApiClient:
    """A `TestClient` plus the identity it authenticates as.

    `login(user)` swaps the user returned by the overridden
    `get_current_user`; `logout()` makes protected routes return 401, so
    anonymous access can be asserted too.
    """

    def __init__(self, client, state: dict[str, Any]) -> None:
        self._client = client
        self._state = state

    def login(self, user) -> "ApiClient":  # noqa: ANN001
        self._state["user"] = user
        return self

    def logout(self) -> "ApiClient":
        self._state["user"] = None
        return self

    def __getattr__(self, name: str) -> Any:
        # get/post/put/patch/delete and friends pass straight through.
        return getattr(self._client, name)


@pytest.fixture
def api_client(db_session: Session) -> Iterator[ApiClient]:
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from app.db.database import get_db
    from app.main import app
    from app.utils.security import get_current_user

    state: dict[str, Any] = {"user": None}

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    def _override_get_current_user():
        user = state["user"]
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user
    try:
        with TestClient(app) as client:
            yield ApiClient(client, state)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


# --- Object factories --------------------------------------------------------
# Positional-argument-free builders for the rows nearly every review test
# needs. Foreign keys are not enforced by SQLite unless explicitly enabled, so
# `workspace_id` can stay a bare integer rather than requiring a real
# workspace row — matching what the existing tests already relied on.


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., Any]:
    from app.db.models import User

    counter = {"n": 0}

    def _make(**overrides: Any):
        counter["n"] += 1
        n = counter["n"]
        user = User(
            email=overrides.pop("email", f"user{n}@example.test"),
            name=overrides.pop("name", f"User {n}"),
            role=overrides.pop("role", "creator"),
            **overrides,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make


@pytest.fixture
def make_project(db_session: Session, make_user) -> Callable[..., Any]:
    from app.db.models import Project

    def _make(creator=None, **overrides: Any):
        creator = creator or make_user()
        project = Project(
            name=overrides.pop("name", "Launch campaign"),
            creator_id=creator.id,
            workspace_id=overrides.pop("workspace_id", 1),
            **overrides,
        )
        db_session.add(project)
        db_session.flush()
        return project

    return _make


@pytest.fixture
def make_video(db_session: Session, make_project) -> Callable[..., Any]:
    from app.db.models import Video

    counter = {"n": 0}

    def _make(project=None, **overrides: Any):
        counter["n"] += 1
        n = counter["n"]
        project = project or make_project()
        video = Video(
            project_id=project.id,
            name=overrides.pop("name", f"Cut {n}"),
            version=overrides.pop("version", 1),
            version_group_id=overrides.pop("version_group_id", f"grp-{n}"),
            file_path=overrides.pop("file_path", f"https://cdn.example.test/{n}.mp4"),
            uploader_id=overrides.pop("uploader_id", project.creator_id),
            status=overrides.pop("status", "in_progress"),
            **overrides,
        )
        db_session.add(video)
        db_session.flush()
        return video

    return _make


@pytest.fixture
def make_comment(db_session: Session) -> Callable[..., Any]:
    from app.db.models import Comment

    def _make(video, **overrides: Any):
        comment = Comment(
            video_id=video.id,
            user_id=overrides.pop("user_id", video.uploader_id),
            text=overrides.pop("text", "Tighten this cut."),
            timecode=overrides.pop("timecode", 5),
            kind=overrides.pop("kind", "comment"),
            status=overrides.pop("status", "open"),
            visibility=overrides.pop("visibility", "public"),
            is_resolved=overrides.pop("is_resolved", False),
            is_private=overrides.pop("is_private", False),
            **overrides,
        )
        db_session.add(comment)
        db_session.flush()
        return comment

    return _make
