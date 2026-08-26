"""`POST .../comments/{id}/attachments/upload` — binary in, attachment out.

The schema and the JSON endpoint existed for months with no UI, partly because
the JSON endpoint demands a `file_url` the client had no way to produce. This
endpoint closes that loop.
"""

from __future__ import annotations

import io
import json
from unittest import mock

import pytest


@pytest.fixture
def ctx(db_session, make_user, make_project, make_video, make_comment):
    owner = make_user(name="Ollie Owner")
    project = make_project(creator=owner)
    video = make_video(project, uploader_id=owner.id)
    comment = make_comment(video, user_id=owner.id)
    db_session.commit()
    return {"owner": owner, "project": project, "video": video, "comment": comment}


def _post(api_client, ctx, *, filename="note.webm", content_type="audio/webm",
          attachment_type="voice_note", data=None, extra=None):
    url = (
        f"/projects/{ctx['project'].id}/videos/{ctx['video'].id}"
        f"/comments/{ctx['comment'].id}/attachments/upload"
    )
    files = {"file": (filename, io.BytesIO(data or b"RIFFfake"), content_type)}
    payload = {"attachment_type": attachment_type, **(extra or {})}
    with mock.patch(
        "app.api.routes.comments.upload_file_to_cloudinary_with_meta",
        return_value={"url": "https://cdn.example.test/stored.webm"},
    ):
        return api_client.post(url, files=files, data=payload)


class AttachmentUploadTests:
    def test_stores_and_returns_the_attachment(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])

        response = _post(api_client, ctx, extra={"duration_ms": "3200"})

        assert response.status_code == 200
        attachments = response.json()["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["attachment_type"] == "voice_note"
        assert attachments[0]["file_url"] == "https://cdn.example.test/stored.webm"
        assert attachments[0]["duration_ms"] == 3200

    def test_waveform_is_parsed_and_clamped(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])

        response = _post(
            api_client, ctx,
            extra={"waveform": json.dumps([0.5, 1.7, -0.2, 0.25])},
        )

        # Out-of-range peaks are clamped, not rejected — the waveform is
        # decoration and must never sink the upload.
        assert response.json()["attachments"][0]["waveform"] == [0.5, 1.0, 0.0, 0.25]

    def test_a_voice_note_must_be_audio(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])

        response = _post(
            api_client, ctx, filename="cat.png", content_type="image/png",
            attachment_type="voice_note",
        )

        assert response.status_code == 400

    def test_an_unknown_type_is_rejected(self, api_client, ctx) -> None:
        api_client.login(ctx["owner"])
        assert _post(api_client, ctx, attachment_type="hologram").status_code == 400

    def test_requires_authentication(self, api_client, ctx) -> None:
        assert _post(api_client, ctx).status_code == 401
