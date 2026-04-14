"""Review link routes — tokenised no-signup video review.

Two route groups live here:

* `/projects/{pid}/videos/{vid}/review-links` — authenticated, for the
  video owner to create / list / revoke / inspect analytics.
* `/review/{token}` — public, no auth. Used by clients who received a
  review link. Handles password gate, guest identity, watch events,
  comments, and approvals.
"""

from __future__ import annotations

import secrets
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.models.review_links import (
    PublicReviewApproveRequest,
    PublicReviewAuthRequest,
    PublicReviewAuthResponse,
    PublicReviewCommentCreate,
    PublicReviewCommentResponse,
    PublicReviewCommentUser,
    PublicReviewEventCreate,
    PublicReviewLinkInfo,
    PublicReviewVideo,
    ReviewAnalyticsResponse,
    ReviewHeatmapBucket,
    ReviewLinkCreate,
    ReviewLinkResponse,
    ReviewLinkUpdate,
    ReviewSessionSummary,
)
from app.db.database import get_db
from app.db.models import (
    Comment,
    Project,
    ReviewEvent,
    ReviewLink,
    ReviewSession,
    User,
    Video,
)
from app.utils.security import (
    get_current_user,
    get_password_hash,
    verify_password,
)


# =============================================================================
# Auth router — for the video owner
# =============================================================================

auth_router = APIRouter(
    prefix="/projects/{project_id}/videos/{video_id}/review-links",
    tags=["Review Links"],
)


def _check_video_owner(
    project_id: int, video_id: int, db: Session, current_user: User
) -> tuple[Video, Project]:
    video = (
        db.query(Video)
        .filter(Video.id == video_id, Video.project_id == project_id)
        .first()
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    allowed = [project.creator] + [c.user for c in project.collaborators]
    if current_user not in allowed:
        raise HTTPException(status_code=403, detail="Not authorized")
    return video, project


def _link_to_response(link: ReviewLink, db: Session) -> dict:
    session_rows = (
        db.query(
            func.count(ReviewSession.id),
            func.coalesce(func.sum(ReviewSession.view_count), 0),
            func.count(ReviewSession.approved_at),
        )
        .filter(ReviewSession.review_link_id == link.id)
        .first()
    )
    unique_viewers = int(session_rows[0] or 0)
    view_count = int(session_rows[1] or 0)
    approvals = int(session_rows[2] or 0)
    total_comments = (
        db.query(func.count(Comment.id))
        .filter(Comment.review_link_id == link.id)
        .scalar()
        or 0
    )
    return {
        "id": link.id,
        "video_id": link.video_id,
        "token": link.token,
        "label": link.label,
        "has_password": bool(link.password_hash),
        "expires_at": link.expires_at,
        "allow_download": link.allow_download,
        "allow_comments": link.allow_comments,
        "watermark_enabled": link.watermark_enabled,
        "require_email": link.require_email,
        "revoked_at": link.revoked_at,
        "created_at": link.created_at,
        "updated_at": link.updated_at,
        "view_count": view_count,
        "unique_viewers": unique_viewers,
        "total_comments": int(total_comments),
        "approvals": approvals,
    }


@auth_router.post("", response_model=ReviewLinkResponse)
def create_review_link(
    project_id: int,
    video_id: int,
    body: ReviewLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    token = secrets.token_urlsafe(24)
    link = ReviewLink(
        video_id=video_id,
        created_by=current_user.id,
        token=token,
        label=body.label,
        password_hash=(
            get_password_hash(body.password) if body.password else None
        ),
        expires_at=body.expires_at,
        allow_download=body.allow_download,
        allow_comments=body.allow_comments,
        watermark_enabled=body.watermark_enabled,
        require_email=body.require_email,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return _link_to_response(link, db)


@auth_router.get("", response_model=List[ReviewLinkResponse])
def list_review_links(
    project_id: int,
    video_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    links = (
        db.query(ReviewLink)
        .filter(ReviewLink.video_id == video_id)
        .order_by(ReviewLink.created_at.desc())
        .all()
    )
    return [_link_to_response(l, db) for l in links]


@auth_router.patch("/{link_id}", response_model=ReviewLinkResponse)
def update_review_link(
    project_id: int,
    video_id: int,
    link_id: int,
    body: ReviewLinkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")

    data = body.dict(exclude_unset=True)
    if "password" in data:
        pw = data.pop("password")
        link.password_hash = get_password_hash(pw) if pw else None
    if "revoked" in data:
        link.revoked_at = datetime.now(timezone.utc) if data.pop("revoked") else None
    for k, v in data.items():
        setattr(link, k, v)
    db.commit()
    db.refresh(link)
    return _link_to_response(link, db)


@auth_router.delete("/{link_id}")
def delete_review_link(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    db.delete(link)
    db.commit()
    return {"ok": True}


@auth_router.get("/{link_id}/analytics", response_model=ReviewAnalyticsResponse)
def link_analytics(
    project_id: int,
    video_id: int,
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _check_video_owner(project_id, video_id, db, current_user)
    link = (
        db.query(ReviewLink)
        .filter(ReviewLink.id == link_id, ReviewLink.video_id == video_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")

    sessions = (
        db.query(ReviewSession)
        .filter(ReviewSession.review_link_id == link_id)
        .order_by(ReviewSession.last_viewed_at.desc())
        .all()
    )
    # Heatmap: bucket progress events per second
    buckets: dict[int, int] = defaultdict(int)
    session_ids = [s.id for s in sessions]
    if session_ids:
        events = (
            db.query(ReviewEvent)
            .filter(
                ReviewEvent.session_id.in_(session_ids),
                ReviewEvent.event_type == "progress",
            )
            .all()
        )
        for e in events:
            start = int(e.position or 0)
            end = int(e.range_end or start + 1)
            for s in range(start, max(start + 1, end)):
                buckets[s] += 1

    heatmap = [
        ReviewHeatmapBucket(second=k, views=v) for k, v in sorted(buckets.items())
    ]
    return ReviewAnalyticsResponse(
        link=_link_to_response(link, db),
        sessions=sessions,
        heatmap=heatmap,
    )


# =============================================================================
# Public router — no auth, token-based
# =============================================================================

public_router = APIRouter(prefix="/review", tags=["Review (public)"])


def _get_link_or_404(token: str, db: Session) -> ReviewLink:
    link = db.query(ReviewLink).filter(ReviewLink.token == token).first()
    if not link:
        raise HTTPException(status_code=404, detail="Review link not found")
    return link


def _link_expired(link: ReviewLink) -> bool:
    if link.expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    exp = link.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < now


def _public_video(video: Video) -> PublicReviewVideo:
    return PublicReviewVideo(
        id=video.id,
        name=video.name,
        description=video.description,
        file_path=video.file_path,
        duration=video.duration,
        thumbnail_url=video.thumbnail_url,
    )


def _assert_link_usable(link: ReviewLink) -> None:
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Review link has been revoked")
    if _link_expired(link):
        raise HTTPException(status_code=410, detail="Review link has expired")


@public_router.get("/{token}", response_model=PublicReviewLinkInfo)
def get_public_link_info(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    expired = _link_expired(link)
    revoked = link.revoked_at is not None
    video_payload: Optional[PublicReviewVideo] = None
    if not link.password_hash and not expired and not revoked:
        video = db.query(Video).filter(Video.id == link.video_id).first()
        if video:
            video_payload = _public_video(video)
    return PublicReviewLinkInfo(
        token=link.token,
        label=link.label,
        has_password=bool(link.password_hash),
        requires_email=link.require_email,
        allow_download=link.allow_download,
        allow_comments=link.allow_comments,
        watermark_enabled=link.watermark_enabled,
        expired=expired,
        revoked=revoked,
        video=video_payload,
    )


@public_router.post("/{token}/session", response_model=PublicReviewAuthResponse)
def start_session(
    token: str,
    body: PublicReviewAuthRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)

    if link.password_hash:
        if not body.password or not verify_password(body.password, link.password_hash):
            return PublicReviewAuthResponse(ok=False, error="Incorrect password")

    if link.require_email and not body.guest_email:
        return PublicReviewAuthResponse(ok=False, error="Email required")

    session = (
        db.query(ReviewSession)
        .filter(
            ReviewSession.review_link_id == link.id,
            ReviewSession.fingerprint == body.fingerprint,
        )
        .first()
    )
    now = datetime.now(timezone.utc)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    if session:
        session.view_count = (session.view_count or 0) + 1
        session.last_viewed_at = now
        if body.guest_name:
            session.guest_name = body.guest_name
        if body.guest_email:
            session.guest_email = body.guest_email
    else:
        session = ReviewSession(
            review_link_id=link.id,
            fingerprint=body.fingerprint,
            guest_name=body.guest_name,
            guest_email=body.guest_email,
            ip_address=client_ip,
            user_agent=user_agent,
            view_count=1,
        )
        db.add(session)

    db.commit()
    db.refresh(session)

    video = db.query(Video).filter(Video.id == link.video_id).first()
    watermark = None
    if link.watermark_enabled:
        who = body.guest_name or body.guest_email or (client_ip or "guest")
        watermark = f"{who} • {now.strftime('%Y-%m-%d %H:%M UTC')}"

    return PublicReviewAuthResponse(
        ok=True,
        session_id=session.id,
        video=_public_video(video) if video else None,
        watermark_text=watermark,
    )


def _get_session_or_404(
    link: ReviewLink, session_id: int, db: Session
) -> ReviewSession:
    session = (
        db.query(ReviewSession)
        .filter(
            ReviewSession.id == session_id,
            ReviewSession.review_link_id == link.id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@public_router.post("/{token}/events")
def record_event(
    token: str,
    body: PublicReviewEventCreate,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)

    ev = ReviewEvent(
        session_id=session.id,
        event_type=body.event_type,
        position=max(0, int(body.position)),
        range_end=(
            max(int(body.position), int(body.range_end))
            if body.range_end is not None
            else None
        ),
    )
    db.add(ev)

    # Update aggregates cheaply
    session.last_viewed_at = datetime.now(timezone.utc)
    pos = int(body.position or 0)
    if pos > (session.max_position or 0):
        session.max_position = pos
    if body.event_type == "progress" and body.range_end is not None:
        delta = max(0, int(body.range_end) - pos)
        session.total_watch_seconds = (session.total_watch_seconds or 0) + delta
    if body.event_type == "ended":
        session.reached_end = True

    db.commit()
    return {"ok": True}


def _public_comment_user(c: Comment) -> PublicReviewCommentUser:
    if c.user_id and c.user:
        return PublicReviewCommentUser(
            id=c.user.id,
            name=c.user.name or (c.user.email or "User"),
            email=c.user.email,
            avatar_url=getattr(c.user, "avatar_url", None),
            is_guest=False,
        )
    return PublicReviewCommentUser(
        id=None,
        name=c.guest_name or "Guest",
        email=c.guest_email,
        avatar_url=None,
        is_guest=True,
    )


def _serialize_public_comment(
    c: Comment, replies: List[Comment] | None = None
) -> PublicReviewCommentResponse:
    return PublicReviewCommentResponse(
        id=c.id,
        video_id=c.video_id,
        parent_id=c.parent_id,
        text=c.text,
        timecode=c.timecode,
        end_timecode=c.end_timecode,
        drawing_data=c.drawing_data,
        is_resolved=c.is_resolved,
        user=_public_comment_user(c),
        likes_count=len(c.likes) if c.likes else 0,
        replies_count=len(replies) if replies is not None else (len(c.replies) if c.replies else 0),
        created_at=c.created_at,
        updated_at=c.updated_at,
        replies=[_serialize_public_comment(r) for r in (replies or [])],
    )


@public_router.get(
    "/{token}/comments", response_model=List[PublicReviewCommentResponse]
)
def list_public_comments(token: str, db: Session = Depends(get_db)):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    top = (
        db.query(Comment)
        .filter(
            Comment.video_id == link.video_id,
            Comment.parent_id.is_(None),
            Comment.is_private == False,  # noqa: E712
        )
        .order_by(Comment.timecode.asc(), Comment.created_at.asc())
        .all()
    )
    result: List[PublicReviewCommentResponse] = []
    for c in top:
        replies = sorted(
            [r for r in (c.replies or []) if not r.is_private],
            key=lambda r: r.created_at,
        )
        result.append(_serialize_public_comment(c, replies))
    return result


@public_router.post(
    "/{token}/comments", response_model=PublicReviewCommentResponse
)
def create_public_comment(
    token: str,
    body: PublicReviewCommentCreate,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    if not link.allow_comments:
        raise HTTPException(status_code=403, detail="Comments disabled on this link")
    session = _get_session_or_404(link, body.session_id, db)

    if body.parent_id is not None:
        parent = (
            db.query(Comment)
            .filter(
                Comment.id == body.parent_id,
                Comment.video_id == link.video_id,
            )
            .first()
        )
        if not parent or parent.is_private:
            raise HTTPException(status_code=404, detail="Parent comment not found")

    comment = Comment(
        video_id=link.video_id,
        user_id=None,
        parent_id=body.parent_id,
        text=body.text,
        timecode=body.timecode,
        end_timecode=body.end_timecode,
        drawing_data=body.drawing_data,
        guest_name=session.guest_name or "Guest",
        guest_email=session.guest_email,
        review_link_id=link.id,
    )
    db.add(comment)

    # Also log as an event for analytics
    db.add(
        ReviewEvent(
            session_id=session.id,
            event_type="comment",
            position=max(0, int(body.timecode or 0)),
        )
    )
    db.commit()
    db.refresh(comment)
    return _serialize_public_comment(comment)


@public_router.post("/{token}/approve")
def approve(
    token: str,
    body: PublicReviewApproveRequest,
    db: Session = Depends(get_db),
):
    link = _get_link_or_404(token, db)
    _assert_link_usable(link)
    session = _get_session_or_404(link, body.session_id, db)
    session.approved_at = (
        datetime.now(timezone.utc) if body.approved else None
    )
    db.commit()
    return {"ok": True, "approved_at": session.approved_at}
