"""
RQ job: upload a VideoPublication to YouTube (resumable upload, chapters, thumbnail).

Run worker from editube/:
  rq worker -u "$REDIS_URL" default
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import ThumbnailVariant, UserYoutubeConnection, Video, VideoChapter, VideoPublication
from app.services.youtube_chapters import chapter_lines_from_rows, merge_description_with_chapters
from app.services.youtube_credentials import persist_google_credentials, refresh_credentials_if_needed
from app.utils.token_crypto import decrypt_secret
from app.services.product_analytics import emit, emit_once

logger = logging.getLogger(__name__)


def _category_id(pub: VideoPublication) -> str:
    raw = (pub.category or "").strip()
    if raw.isdigit():
        return raw
    return "22"


def _tags(pub: VideoPublication) -> list[str]:
    t = (pub.tags or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", t) if p.strip()]
    return parts[:450]


def _fail(db: Session, pub: VideoPublication, message: str) -> None:
    try:
        pub.status = "failed"
        pub.error_message = message[:4000]
        db.add(pub)
        emit_once(
            db,
            "publication_failed",
            event_id=f"publication:{pub.id}:failed",
            user_id=pub.created_by,
            properties={
                "platform": "youtube",
                "feature_key": "youtube_publish",
                "publication_id": pub.id,
                "error_code": "publish_failed",
                "result": "failure",
            },
            source="worker",
        )
        emit_once(
            db,
            "feature_failed",
            event_id=f"feature:youtube-publish:publication:{pub.id}:failed",
            user_id=pub.created_by,
            properties={
                "feature_key": "youtube_publish",
                "error_code": "publish_failed",
                "failure_class": "processing",
                "result": "failure",
            },
            source="worker",
        )
        db.commit()
    except Exception:
        logger.exception("Could not persist publication failure for %s", pub.id)


def youtube_publish_job(publication_id: int) -> None:
    db: Session = SessionLocal()
    try:
        pub = db.query(VideoPublication).filter(VideoPublication.id == publication_id).first()
        if not pub:
            logger.error("youtube_publish_job: publication %s not found", publication_id)
            raise RuntimeError(f"Publication {publication_id} not found")
        if (pub.platform or "").lower() != "youtube":
            logger.warning("youtube_publish_job: publication %s is not youtube", publication_id)
            raise RuntimeError(f"Publication {publication_id} is not a YouTube publication")

        if pub.status == "published" and pub.external_id:
            logger.info("youtube_publish_job: publication %s already published", publication_id)
            return

        video = db.query(Video).filter(Video.id == pub.video_id).first()
        if not video or not video.file_path:
            _fail(db, pub, "Video or file_path missing.")
            raise RuntimeError("Video or file_path missing")

        user_id = pub.created_by
        if not user_id:
            _fail(db, pub, "Publication has no created_by user.")
            raise RuntimeError("Publication has no created_by user")

        conn = db.query(UserYoutubeConnection).filter(UserYoutubeConnection.user_id == user_id).first()
        if not conn:
            _fail(db, pub, "YouTube is not connected for this user. Connect in Creator studio.")
            raise RuntimeError("YouTube is not connected for this user")

        pub.status = "processing"
        pub.error_message = None
        db.add(pub)
        db.commit()

        try:
            decrypt_secret(conn.refresh_token_encrypted)
        except ValueError as e:
            _fail(db, pub, str(e))
            raise

        creds = refresh_credentials_if_needed(db, conn)

        chapters = (
            db.query(VideoChapter)
            .filter(VideoChapter.video_id == pub.video_id)
            .order_by(VideoChapter.start_time.asc(), VideoChapter.order_index.asc())
            .all()
        )
        lines = chapter_lines_from_rows(chapters)
        full_description = merge_description_with_chapters(pub.description, lines)

        status_body: dict = {
            "privacyStatus": (pub.privacy or "private").lower(),
            "selfDeclaredMadeForKids": False,
        }
        if pub.scheduled_at:
            status_body["privacyStatus"] = "private"
            ts = pub.scheduled_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            status_body["publishAt"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

        body = {
            "snippet": {
                "title": (pub.title or video.name or "Untitled")[:100],
                "description": full_description[:5000],
                "categoryId": _category_id(pub),
            },
            "status": status_body,
        }
        tags = _tags(pub)
        if tags:
            body["snippet"]["tags"] = tags

        dry = os.environ.get("YOUTUBE_PUBLISH_DRY_RUN", "").strip() in ("1", "true", "yes")
        if dry:
            pub.status = "published"
            pub.published_at = datetime.utcnow()
            pub.external_id = "dry-run"
            pub.external_url = "https://www.youtube.com/watch?v=dry-run"
            extra = dict(pub.extra or {})
            extra["post_publish_checklist"] = [
                {"label": "Dry run only", "detail": "YOUTUBE_PUBLISH_DRY_RUN was set"},
            ]
            pub.extra = extra
            db.add(pub)
            emit(
                db,
                "publication_completed",
                user_id=user_id,
                properties={
                    "platform": "youtube",
                    "feature_key": "youtube_publish",
                    "publication_id": pub.id,
                    "completion_type": "dry_run",
                    "result": "success",
                },
                source="worker",
            )
            emit(
                db,
                "feature_completed",
                user_id=user_id,
                properties={
                    "feature_key": "youtube_publish",
                    "completion_type": "dry_run",
                    "result": "success",
                },
                source="worker",
            )
            db.commit()
            return

        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            media_path = tmp_path / "upload.mp4"
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=60.0)) as client:
                r = client.get(video.file_path, follow_redirects=True)
                r.raise_for_status()
                media_path.write_bytes(r.content)

            media = MediaFileUpload(
                str(media_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=8 * 1024 * 1024,
            )

            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            try:
                while response is None:
                    _, response = request.next_chunk()
            except HttpError as e:
                _fail(db, pub, f"YouTube upload failed: {e}")
                raise RuntimeError("YouTube upload failed") from e

            if not response or "id" not in response:
                _fail(db, pub, "YouTube returned no video id.")
                raise RuntimeError("YouTube returned no video id")

            vid = response["id"]
            pub.external_id = vid
            pub.external_url = f"https://www.youtube.com/watch?v={vid}"

            thumb_path: Path | None = None
            if pub.thumbnail_variant_id:
                tv = (
                    db.query(ThumbnailVariant)
                    .filter(ThumbnailVariant.id == pub.thumbnail_variant_id)
                    .first()
                )
                if tv and tv.image_url:
                    try:
                        thumb_path = tmp_path / "thumb.jpg"
                        with httpx.Client(timeout=60.0) as tclient:
                            tr = tclient.get(tv.image_url, follow_redirects=True)
                            tr.raise_for_status()
                            thumb_path.write_bytes(tr.content)
                        youtube.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb_path))).execute()
                    except Exception as e:
                        logger.warning("Thumbnail upload failed for publication %s: %s", publication_id, e)

            extra = dict(pub.extra or {})
            extra["post_publish_checklist"] = [
                {
                    "label": "End screens & cards",
                    "url": f"https://studio.youtube.com/video/{vid}/edits",
                },
                {
                    "label": "Pinned comment",
                    "url": f"https://studio.youtube.com/video/{vid}/comments",
                },
            ]
            pub.extra = extra
            pub.status = "published"
            pub.published_at = datetime.utcnow()
            db.add(pub)
            emit(
                db,
                "publication_completed",
                user_id=user_id,
                properties={
                    "platform": "youtube",
                    "feature_key": "youtube_publish",
                    "publication_id": pub.id,
                    "completion_type": "published",
                    "result": "success",
                },
                source="worker",
            )
            emit(
                db,
                "feature_completed",
                user_id=user_id,
                properties={
                    "feature_key": "youtube_publish",
                    "completion_type": "published",
                    "result": "success",
                },
                source="worker",
            )
            if pub.thumbnail_variant_id:
                emit_once(
                    db,
                    "feature_result_used",
                    event_id=f"feature:thumbnail:publication:{pub.id}:published",
                    user_id=user_id,
                    properties={
                        "feature_key": "thumbnail",
                        "publication_id": pub.id,
                        "thumbnail_variant_id": pub.thumbnail_variant_id,
                        "result_action": "published_with_thumbnail",
                        "result": "success",
                    },
                    source="worker",
                )
            db.commit()

        conn2 = db.query(UserYoutubeConnection).filter(UserYoutubeConnection.user_id == user_id).first()
        if conn2:
            try:
                persist_google_credentials(db, conn2, creds)
            except Exception:
                logger.warning("Could not persist refreshed YouTube access token", exc_info=True)

        logger.info("YouTube publish completed for publication %s -> %s", publication_id, pub.external_id)

    except Exception as e:
        logger.exception("youtube_publish_job failed for %s", publication_id)
        try:
            pub = db.query(VideoPublication).filter(VideoPublication.id == publication_id).first()
            if pub:
                _fail(db, pub, str(e)[:4000])
        except Exception:
            pass
        raise
    finally:
        db.close()
