"""Validate in-app return paths (e.g. Stripe Connect onboarding)."""


def safe_internal_path(path: str | None, default: str = "/projects", max_len: int = 512) -> str:
    p = (path or default).strip() or default
    if not p.startswith("/") or p.startswith("//"):
        return default
    return p[:max_len]
