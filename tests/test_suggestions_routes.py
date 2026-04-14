import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.routes.suggestions import _get_user_from_auth_header


class SuggestionsRouteAuthTests(unittest.TestCase):
    def test_get_user_from_auth_header_returns_none_without_header(self) -> None:
        self.assertIsNone(_get_user_from_auth_header(db=object(), authorization=None))

    def test_get_user_from_auth_header_returns_none_for_non_bearer_header(self) -> None:
        self.assertIsNone(
            _get_user_from_auth_header(db=object(), authorization="Basic abc123")
        )

    def test_get_user_from_auth_header_resolves_bearer_token(self) -> None:
        fake_user = object()
        with patch(
            "app.api.routes.suggestions.authenticate_access_token",
            return_value=fake_user,
        ) as auth_mock:
            result = _get_user_from_auth_header(db=object(), authorization="Bearer token-123")
        auth_mock.assert_called_once()
        self.assertIs(result, fake_user)

    def test_get_user_from_auth_header_swallows_auth_error(self) -> None:
        with patch(
            "app.api.routes.suggestions.authenticate_access_token",
            side_effect=HTTPException(status_code=401, detail="bad token"),
        ):
            result = _get_user_from_auth_header(db=object(), authorization="Bearer bad-token")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
