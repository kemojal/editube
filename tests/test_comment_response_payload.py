from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api.models.comments import CommentResponse
from app.api.routes.comments import _comment_response


def test_comment_response_serializes_transcript_anchor_and_user_avatar():
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    user = SimpleNamespace(
        id=7,
        name="Editor",
        email="editor@example.com",
        avatar_url="https://cdn.example.com/avatar.png",
    )
    comment = SimpleNamespace(
        id=91,
        video_id=32,
        parent_id=None,
        text="Needs a tighter line.",
        timecode=14,
        end_timecode=16,
        drawing_data=None,
        transcript_segment_index=4,
        word_start_index=22,
        word_end_index=28,
        anchor_text="Quickly from their words",
        is_resolved=False,
        is_private=False,
        visibility="public",
        due_at=None,
        kind="comment",
        status="open",
        assignee_user_id=None,
        assignee=None,
        user_id=7,
        user=user,
        guest_name=None,
        guest_email=None,
        guest_avatar_url=None,
        review_link_id=None,
        client_mutation_id="comment-test",
        revision=1,
        likes=[],
        replies=[],
        attachments=[],
        created_at=now,
        updated_at=now,
    )

    with patch("app.api.routes.comments._anchor_state", return_value=(True, None, None)):
        response = CommentResponse.model_validate(_comment_response(comment, current_user_id=7, db=SimpleNamespace()))

    assert response.transcript_segment_index == 4
    assert response.word_start_index == 22
    assert response.word_end_index == 28
    assert response.anchor_text == "Quickly from their words"
    assert response.user is not None
    assert response.user.avatar_url == "https://cdn.example.com/avatar.png"
