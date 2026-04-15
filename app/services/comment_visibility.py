"""Comment visibility for team vs client review surfaces."""

COMMENT_VISIBILITY_PUBLIC = "public"
COMMENT_VISIBILITY_TEAM = "team"
COMMENT_VISIBILITY_AUTHOR_ONLY = "author_only"


def normalize_visibility(raw: str | None, is_private: bool) -> str:
    if raw in (COMMENT_VISIBILITY_PUBLIC, COMMENT_VISIBILITY_TEAM, COMMENT_VISIBILITY_AUTHOR_ONLY):
        return raw
    return COMMENT_VISIBILITY_AUTHOR_ONLY if is_private else COMMENT_VISIBILITY_PUBLIC


def is_client_visible(visibility: str | None, is_private: bool) -> bool:
    v = normalize_visibility(visibility, is_private)
    return v == COMMENT_VISIBILITY_PUBLIC
