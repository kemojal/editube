"""Request-scoped correlation context shared by logs, analytics, and Sentry."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class RequestContext:
    request_id: str | None = None
    trace_id: str | None = None
    analytics_session_id: str | None = None
    route_template: str | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    plan: str | None = None
    subscription_status: str | None = None
    user_role: str | None = None


_context: ContextVar[RequestContext | None] = ContextVar(
    "editube_request_context", default=None
)


def _current() -> RequestContext:
    context = _context.get()
    if context is None:
        # Never use a mutable object as a ContextVar default: the default is a
        # process-wide singleton and a route bound in one completed request
        # would bleed into unrelated analytics events and tests.
        context = RequestContext()
        _context.set(context)
    return context


def _sentry_update(*, user_id: int | None = None, **tags) -> None:  # noqa: ANN003
    try:
        import sentry_sdk

        if user_id is not None:
            sentry_sdk.set_user({"id": str(user_id)})
        for key, value in tags.items():
            if value is not None:
                sentry_sdk.set_tag(key, str(value))
    except ImportError:
        return


def begin_request(
    *, request_id: str, trace_id: str | None, analytics_session_id: str | None
) -> Token:
    token = _context.set(
        RequestContext(
            request_id=request_id,
            trace_id=trace_id,
            analytics_session_id=analytics_session_id,
        )
    )
    _sentry_update(
        request_id=request_id,
        trace_id=trace_id,
        analytics_session_id=analytics_session_id,
    )
    return token


def bind_route(route_template: str | None) -> None:
    # Context is intentionally mutated in place. FastAPI executes synchronous
    # dependencies in a worker thread: ContextVar values are copied into that
    # thread, but assigning a replacement there does not flow back to the ASGI
    # task. A per-request mutable value preserves user/workspace bindings for
    # the request logger without sharing state between requests.
    _current().route_template = route_template
    _sentry_update(route_template=route_template)


def bind_user(
    *,
    user_id: int,
    plan: str | None,
    subscription_status: str | None,
    user_role: str | None,
) -> None:
    context = _current()
    context.user_id = user_id
    context.plan = plan
    context.subscription_status = subscription_status
    context.user_role = user_role
    _sentry_update(
        user_id=user_id,
        plan=plan,
        subscription_status=subscription_status,
        user_role=user_role,
    )


def bind_workspace(workspace_id: int | None) -> None:
    _current().workspace_id = workspace_id
    _sentry_update(workspace_id=workspace_id)


def current_request_context() -> RequestContext:
    return _current()


def end_request(token: Token) -> None:
    _context.reset(token)
