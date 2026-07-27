import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.google_drive_files import (
    DriveFileError,
    DriveFileMeta,
    fetch_file_metadata,
)


def _service_returning(*payloads):
    """Fake Drive service whose files().get().execute() yields payloads in order."""
    service = MagicMock()
    execute = MagicMock(side_effect=list(payloads))
    service.files.return_value.get.return_value.execute = execute
    return service


VIDEO_OK = {
    "id": "file123",
    "name": "interview.mp4",
    "mimeType": "video/mp4",
    "size": "104857600",  # 100 MB
    "thumbnailLink": "https://drive.example/thumb.jpg",
    "videoMediaMetadata": {"durationMillis": "125000", "width": 1920, "height": 1080},
    "capabilities": {"canDownload": True},
    "owners": [{"displayName": "Kemo", "emailAddress": "kemo@example.com"}],
}


class FetchFileMetadataTests(unittest.TestCase):
    def test_parses_a_valid_video(self) -> None:
        meta = fetch_file_metadata(_service_returning(VIDEO_OK), "file123")
        self.assertIsInstance(meta, DriveFileMeta)
        self.assertEqual(meta.file_id, "file123")
        self.assertEqual(meta.name, "interview.mp4")
        self.assertEqual(meta.size_bytes, 104857600)
        # durationMillis -> whole seconds, which is what the wizard's trim step needs
        self.assertEqual(meta.duration_seconds, 125)
        self.assertEqual(meta.width, 1920)
        self.assertEqual(meta.owner_email, "kemo@example.com")
        self.assertEqual(meta.warnings, [])

    def test_passes_supports_all_drives_so_shared_drives_work(self) -> None:
        service = _service_returning(VIDEO_OK)
        fetch_file_metadata(service, "file123")
        _, kwargs = service.files.return_value.get.call_args
        self.assertTrue(kwargs.get("supportsAllDrives"))

    def test_rejects_google_native_docs(self) -> None:
        payload = {**VIDEO_OK, "mimeType": "application/vnd.google-apps.document"}
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(ctx.exception.code, "google_native")

    def test_rejects_non_media_files(self) -> None:
        payload = {**VIDEO_OK, "mimeType": "application/pdf"}
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(ctx.exception.code, "not_media")

    def test_accepts_audio(self) -> None:
        payload = {**VIDEO_OK, "mimeType": "audio/mpeg", "name": "voiceover.mp3"}
        meta = fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(meta.mime_type, "audio/mpeg")
        # Audio has no video duration and must NOT be flagged as a problem.
        self.assertEqual(meta.warnings, [])

    def test_rejects_when_owner_disabled_download(self) -> None:
        payload = {**VIDEO_OK, "capabilities": {"canDownload": False}}
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(ctx.exception.code, "download_disabled")

    def test_rejects_trashed_files(self) -> None:
        payload = {**VIDEO_OK, "trashed": True}
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(ctx.exception.code, "trashed")

    def test_rejects_files_over_the_size_ceiling(self) -> None:
        payload = {**VIDEO_OK, "size": str(20 * 1024 * 1024 * 1024)}  # 20 GB
        with patch("app.services.google_drive_files.MAX_FILE_SIZE_MB", 10240):
            with self.assertRaises(DriveFileError) as ctx:
                fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(ctx.exception.code, "too_large")

    def test_follows_a_shortcut_to_its_target(self) -> None:
        shortcut = {
            "id": "shortcut1",
            "name": "link to interview",
            "mimeType": "application/vnd.google-apps.shortcut",
            "shortcutDetails": {"targetId": "file123", "targetMimeType": "video/mp4"},
        }
        meta = fetch_file_metadata(_service_returning(shortcut, VIDEO_OK), "shortcut1")
        # Resolves to the *target's* identity, not the shortcut's.
        self.assertEqual(meta.file_id, "file123")
        self.assertEqual(meta.name, "interview.mp4")

    def test_rejects_a_dangling_shortcut(self) -> None:
        shortcut = {
            "id": "shortcut1",
            "name": "broken link",
            "mimeType": "application/vnd.google-apps.shortcut",
            "shortcutDetails": {},
        }
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(_service_returning(shortcut), "shortcut1")
        self.assertEqual(ctx.exception.code, "shortcut_unresolved")

    def test_flags_unknown_duration_as_a_warning_not_a_failure(self) -> None:
        payload = {k: v for k, v in VIDEO_OK.items() if k != "videoMediaMetadata"}
        meta = fetch_file_metadata(_service_returning(payload), "file123")
        # The import job ffprobes as a fallback, so this must not block the user.
        self.assertEqual(meta.duration_seconds, 0)
        self.assertIn("duration_unknown", meta.warnings)

    def test_missing_size_does_not_trip_the_ceiling(self) -> None:
        payload = {k: v for k, v in VIDEO_OK.items() if k != "size"}
        meta = fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(meta.size_bytes, 0)

    def test_long_names_are_truncated_to_the_column_width(self) -> None:
        payload = {**VIDEO_OK, "name": "x" * 400}
        meta = fetch_file_metadata(_service_returning(payload), "file123")
        self.assertEqual(len(meta.name), 255)


class HttpErrorMappingTests(unittest.TestCase):
    def _http_error(self, status: int) -> Exception:
        from googleapiclient.errors import HttpError

        resp = MagicMock()
        resp.status = status
        resp.reason = "err"
        return HttpError(resp, b"{}")

    def _raising_service(self, exc: Exception):
        service = MagicMock()
        service.files.return_value.get.return_value.execute = MagicMock(side_effect=exc)
        return service

    def test_401_maps_to_reauth_required(self) -> None:
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(self._raising_service(self._http_error(401)), "f")
        self.assertEqual(ctx.exception.code, "reauth_required")

    def test_403_maps_to_reauth_required(self) -> None:
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(self._raising_service(self._http_error(403)), "f")
        self.assertEqual(ctx.exception.code, "reauth_required")

    def test_404_maps_to_not_found(self) -> None:
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(self._raising_service(self._http_error(404)), "f")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_500_maps_to_generic_drive_error(self) -> None:
        with self.assertRaises(DriveFileError) as ctx:
            fetch_file_metadata(self._raising_service(self._http_error(500)), "f")
        self.assertEqual(ctx.exception.code, "drive_error")


class OAuthStateTests(unittest.TestCase):
    """The Drive state JWT must not be interchangeable with the YouTube one."""

    def test_drive_state_round_trips(self) -> None:
        from app.api.routes.google_drive import _decode_state, _encode_state

        self.assertEqual(_decode_state(_encode_state(42)), 42)

    def test_youtube_state_is_rejected_by_drive(self) -> None:
        from fastapi import HTTPException

        from app.api.routes.google_drive import _decode_state as drive_decode
        from app.api.routes.youtube_oauth import _encode_state as youtube_encode

        with self.assertRaises(HTTPException):
            drive_decode(youtube_encode(42))

    def test_drive_state_is_rejected_by_youtube(self) -> None:
        from fastapi import HTTPException

        from app.api.routes.google_drive import _encode_state as drive_encode
        from app.api.routes.youtube_oauth import _decode_state as youtube_decode

        with self.assertRaises(HTTPException):
            youtube_decode(drive_encode(42))

    def test_garbage_state_is_rejected(self) -> None:
        from fastapi import HTTPException

        from app.api.routes.google_drive import _decode_state

        with self.assertRaises(HTTPException):
            _decode_state("not-a-jwt")


class ScopeTests(unittest.TestCase):
    def test_only_requests_the_narrow_drive_file_scope(self) -> None:
        """drive.readonly would be a *restricted* scope requiring an annual CASA
        security assessment — see docs/google-drive-import-plan.md §1."""
        from app.services.google_drive_credentials import DRIVE_SCOPES

        self.assertIn("https://www.googleapis.com/auth/drive.file", DRIVE_SCOPES)
        for scope in DRIVE_SCOPES:
            self.assertNotIn("drive.readonly", scope)
            self.assertNotEqual(scope, "https://www.googleapis.com/auth/drive")


class PopupScriptEscapingTests(unittest.TestCase):
    """The callback embeds JSON in an inline <script>; `error` comes straight off
    the unauthenticated query string, so it must not be able to break out."""

    def test_script_tag_in_error_cannot_break_out(self) -> None:
        from app.api.routes.google_drive import _popup_response

        hostile = '</script><img src=x onerror=alert(1)>'
        body = _popup_response({"ok": False, "error": hostile}).body.decode()
        # Exactly one script element — the legitimate one.
        self.assertEqual(body.count("</script>"), 1)
        self.assertNotIn("<img", body)
        self.assertIn("\\u003c", body)

    def test_ampersands_and_gt_are_escaped(self) -> None:
        from app.api.routes.google_drive import _js

        self.assertNotIn("<", _js({"a": "<b>&c"}))
        self.assertNotIn(">", _js({"a": "<b>&c"}))
        self.assertNotIn("&", _js({"a": "<b>&c"}))

    def test_ok_payload_still_round_trips_as_json(self) -> None:
        from app.api.routes.google_drive import _js

        decoded = json.loads(
            _js({"ok": True, "connection": {"email": "a@b.c"}})
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
            .replace("\\u0026", "&")
        )
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["connection"]["email"], "a@b.c")


class AuthorizeUrlTests(unittest.TestCase):
    def test_requests_offline_access_and_an_account_chooser(self) -> None:
        from app.api.routes.google_drive import drive_authorize_url

        req = MagicMock()
        req.url_for.return_value = "https://api.example.com/users/google/drive/callback"
        with patch.dict(
            "os.environ", {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec"}, clear=False
        ):
            url = drive_authorize_url(req, current_user=SimpleNamespace(id=7))["authorization_url"]

        # offline => we get a refresh token; select_account => "Add another
        # account" can actually reach a second Google account.
        self.assertIn("access_type=offline", url)
        self.assertIn("select_account", url)
        self.assertIn("drive.file", url)
        self.assertNotIn("drive.readonly", url)


class EnsureFreshAccessTokenTests(unittest.TestCase):
    def test_never_returns_a_null_token(self) -> None:
        """Credentials.expired is False when expiry is None, so a row with no
        stored token would otherwise yield access_token: null to the Picker."""
        from app.services import google_drive_credentials as mod

        row = SimpleNamespace(id=1, access_token=None, access_expires_at=None)
        creds = SimpleNamespace(token=None, expired=False, refresh_token=None, expiry=None)
        with patch.object(mod, "build_credentials_for_connection", return_value=creds), patch.object(
            mod, "mark_revoked"
        ):
            with self.assertRaises(mod.DriveReauthRequired):
                mod.ensure_fresh_access_token(MagicMock(), row)


if __name__ == "__main__":
    unittest.main()
