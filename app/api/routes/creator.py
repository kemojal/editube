"""Creator-native routes: YouTube publish, aspect exports, chapters,
end-screens, brand deals, thumbnail A/B variants.

Operational: YouTube upload, aspect exports, and chapter synthesis jobs need
``REDIS_URL`` and ``rq worker``. YouTube publish uses the real Data API when
OAuth is connected (see ``app.jobs.youtube_publish``). Other platforms use
``StubPublisher`` until dedicated publishers exist.

Aspect exports v1 use center / smart crop only — AI subject-tracking reframe
is not implemented yet (see ``docs/future_plan.md`` §4). Auto-chapters need a
video transcript plus AI config (``GEMINI_API_KEY`` / worker).
"""

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.models.creator import (
    AspectExportCreate,
    AspectExportResponse,
    DeliveryExportCreate,
    DeliveryExportResponse,
    BrandDealCreate,
    BrandDealResponse,
    BrandDealUpdate,
    ChapterCreate,
    ChapterResponse,
    ChapterUpdate,
    EndScreenBody,
    EndScreenResponse,
    PublicationCreate,
    PublicationResponse,
    PublicationUpdate,
    ThumbnailVariantCreate,
    ThumbnailVariantResponse,
    ThumbnailVariantUpdate,
    YoutubeChapterBlockResponse,
)
from app.db.database import get_db
from app.db.models import (
    BrandDeal,
    DeliveryExport,
    Project,
    ThumbnailVariant,
    User,
    UserYoutubeConnection,
    Video,
    VideoAspectExport,
    VideoChapter,
    VideoEndScreen,
    VideoPublication,
)
from app.jobs.queue import (
    enqueue_aspect_export_job,
    enqueue_chapter_synthesis_job,
    enqueue_multi_format_export_job,
)
from app.publishers import get_publisher
from app.services.youtube_chapters import youtube_description_block
from app.utils.security import get_current_user
from app.services.project_access import can_access_project
from app.services.product_analytics import emit, emit_once


router = APIRouter(prefix="/creator", tags=["Creator"])


def _get_owned_video(db: Session, video_id: int, user: User) -> Video:
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not can_access_project(db, user.id, video.project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return video


def _get_owned_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not can_access_project(db, user.id, project):
        raise HTTPException(status_code=403, detail="Not authorized")
    return project


# =====================================================================
# Publications (YouTube / social)
# =====================================================================

@router.get("/videos/{video_id}/publications", response_model=List[PublicationResponse])
def list_publications(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    return (
        db.query(VideoPublication)
        .filter(VideoPublication.video_id == video_id)
        .order_by(VideoPublication.created_at.desc())
        .all()
    )


@router.post("/videos/{video_id}/publications", response_model=PublicationResponse)
def create_publication(
    video_id: int,
    body: PublicationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_owned_video(db, video_id, current_user)
    pub = VideoPublication(
        video_id=video_id,
        created_by=current_user.id,
        platform=body.platform,
        title=body.title,
        description=body.description,
        tags=body.tags,
        category=body.category,
        privacy=body.privacy or "private",
        scheduled_at=body.scheduled_at,
        thumbnail_variant_id=body.thumbnail_variant_id,
        extra=body.extra,
        status="scheduled" if body.scheduled_at else "draft",
    )
    db.add(pub)
    db.commit()
    db.refresh(pub)
    return pub


@router.patch("/publications/{pub_id}", response_model=PublicationResponse)
def update_publication(
    pub_id: int,
    body: PublicationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pub = db.query(VideoPublication).filter(VideoPublication.id == pub_id).first()
    if not pub:
        raise HTTPException(status_code=404, detail="Publication not found")
    _get_owned_video(db, pub.video_id, current_user)
    data = body.dict(exclude_unset=True)
    for k, v in data.items():
        setattr(pub, k, v)
    db.commit()
    db.refresh(pub)
    return pub


@router.post("/publications/{pub_id}/publish", response_model=PublicationResponse)
def publish_publication(
    pub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pub = db.query(VideoPublication).filter(VideoPublication.id == pub_id).first()
    if not pub:
        raise HTTPException(status_code=404, detail="Publication not found")
    _get_owned_video(db, pub.video_id, current_user)
    if (pub.platform or "").lower() == "youtube":
        author_id = pub.created_by
        if not author_id:
            raise HTTPException(status_code=400, detail="Publication has no author")
        conn = (
            db.query(UserYoutubeConnection)
            .filter(UserYoutubeConnection.user_id == author_id)
            .first()
        )
        if not conn:
            raise HTTPException(
                status_code=400,
                detail="The user who created this draft must connect YouTube (Studio → Connect YouTube).",
            )
    get_publisher(pub.platform).start_publish(db, pub)
    feature_key = "youtube_publish" if (pub.platform or "").lower() == "youtube" else None
    if pub.status == "failed":
        emit(
            db,
            "publication_failed",
            user=current_user,
            properties={
                "platform": (pub.platform or "unknown").lower(),
                "feature_key": feature_key,
                "publication_id": pub.id,
                "error_code": "queue_unavailable",
                "result": "failure",
            },
        )
        if feature_key:
            emit(
                db,
                "feature_failed",
                user=current_user,
                properties={
                    "feature_key": feature_key,
                    "error_code": "queue_unavailable",
                    "failure_class": "queue",
                    "result": "failure",
                },
            )
    else:
        emit(
            db,
            "publication_queued",
            user=current_user,
            properties={
                "platform": (pub.platform or "unknown").lower(),
                "feature_key": feature_key,
                "publication_id": pub.id,
            },
        )
        if feature_key:
            emit(
                db,
                "feature_started",
                user=current_user,
                properties={"feature_key": feature_key, "entry_point": "creator_publish"},
            )
    db.commit()
    db.refresh(pub)
    return pub


@router.delete("/publications/{pub_id}")
def delete_publication(pub_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pub = db.query(VideoPublication).filter(VideoPublication.id == pub_id).first()
    if not pub:
        raise HTTPException(status_code=404, detail="Publication not found")
    _get_owned_video(db, pub.video_id, current_user)
    db.delete(pub)
    db.commit()
    return {"ok": True}


# =====================================================================
# Aspect exports (9:16, 1:1, etc.)
# =====================================================================

@router.get("/videos/{video_id}/aspect-exports", response_model=List[AspectExportResponse])
def list_aspect_exports(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    return (
        db.query(VideoAspectExport)
        .filter(VideoAspectExport.video_id == video_id)
        .order_by(VideoAspectExport.created_at.desc())
        .all()
    )


@router.post("/videos/{video_id}/aspect-exports", response_model=AspectExportResponse)
def create_aspect_export(
    video_id: int,
    body: AspectExportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _get_owned_video(db, video_id, current_user)
    project = video.project
    exp = VideoAspectExport(
        video_id=video_id,
        aspect_ratio=body.aspect_ratio,
        platform_preset=body.platform_preset,
        subject_tracking=body.subject_tracking,
        status="queued",
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    if enqueue_aspect_export_job(exp.id):
        common = {
            "user": current_user,
            "workspace_id": project.workspace_id if project else None,
            "properties": {
                "feature_key": "multi_aspect_export",
                "job_type": "aspect_export",
                "resource_id": exp.id,
                "project_id": video.project_id,
                "video_id": video.id,
                "aspect_ratio": exp.aspect_ratio,
                "result": "queued",
            },
        }
        emit_once(
            db,
            "feature_started",
            event_id=f"feature:multi-aspect-export:aspect:{exp.id}:started",
            **common,
        )
        db.commit()
    else:
        exp.status = "failed"
        exp.error_message = "Could not queue export (set REDIS_URL and run an RQ worker)."
        db.add(exp)
        common = {
            "user": current_user,
            "workspace_id": project.workspace_id if project else None,
            "properties": {
                "feature_key": "multi_aspect_export",
                "job_type": "aspect_export",
                "resource_id": exp.id,
                "project_id": video.project_id,
                "video_id": video.id,
                "failure_class": "queue",
                "error_code": "queue_unavailable",
                "result": "failure",
            },
        }
        emit_once(db, "job_failed", event_id=f"aspect-export:{exp.id}:queue-failed", **common)
        emit_once(
            db,
            "feature_failed",
            event_id=f"feature:multi-aspect-export:aspect:{exp.id}:queue-failed",
            **common,
        )
        db.commit()
        db.refresh(exp)
    return exp


@router.delete("/aspect-exports/{export_id}")
def delete_aspect_export(export_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    exp = db.query(VideoAspectExport).filter(VideoAspectExport.id == export_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Export not found")
    _get_owned_video(db, exp.video_id, current_user)
    db.delete(exp)
    db.commit()
    return {"ok": True}


@router.get("/videos/{video_id}/delivery-exports", response_model=List[DeliveryExportResponse])
def list_delivery_exports(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    video = _get_owned_video(db, video_id, current_user)
    project = video.project
    return (
        db.query(DeliveryExport)
        .filter(DeliveryExport.video_id == video_id)
        .order_by(DeliveryExport.created_at.desc())
        .all()
    )


@router.post("/videos/{video_id}/delivery-exports", response_model=List[DeliveryExportResponse])
def create_delivery_exports(
    video_id: int,
    body: DeliveryExportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_owned_video(db, video_id, current_user)
    keys = body.profile_keys or ["4k_master", "yt_1080p", "social_720p"]
    rows: list[DeliveryExport] = []
    for key in keys:
        row = DeliveryExport(
            video_id=video_id,
            profile_key=key,
            status="queued",
            created_by=current_user.id,
        )
        db.add(row)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
        if enqueue_multi_format_export_job(row.id):
            common = {
                "user": current_user,
                "workspace_id": project.workspace_id if project else None,
                "properties": {
                    "feature_key": "multi_aspect_export",
                    "job_type": "multi_format_export",
                    "resource_id": row.id,
                    "project_id": video.project_id,
                    "video_id": video.id,
                    "profile_key": row.profile_key,
                    "result": "queued",
                },
            }
            emit_once(
                db,
                "feature_started",
                event_id=f"feature:multi-aspect-export:delivery:{row.id}:started",
                **common,
            )
        else:
            row.status = "failed"
            row.error_message = "Could not queue export (set REDIS_URL and run an RQ worker)."
            db.add(row)
            common = {
                "user": current_user,
                "workspace_id": project.workspace_id if project else None,
                "properties": {
                    "feature_key": "multi_aspect_export",
                    "job_type": "multi_format_export",
                    "resource_id": row.id,
                    "project_id": video.project_id,
                    "video_id": video.id,
                    "profile_key": row.profile_key,
                    "failure_class": "queue",
                    "error_code": "queue_unavailable",
                    "result": "failure",
                },
            }
            emit_once(db, "job_failed", event_id=f"multi-format-export:{row.id}:queue-failed", **common)
            emit_once(
                db,
                "feature_failed",
                event_id=f"feature:multi-aspect-export:delivery:{row.id}:queue-failed",
                **common,
            )
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


@router.get("/delivery-exports/{export_id}", response_model=DeliveryExportResponse)
def get_delivery_export(export_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(DeliveryExport).filter(DeliveryExport.id == export_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Delivery export not found")
    _get_owned_video(db, row.video_id, current_user)
    return row


# =====================================================================
# Chapters
# =====================================================================

@router.get("/videos/{video_id}/chapters", response_model=List[ChapterResponse])
def list_chapters(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    return (
        db.query(VideoChapter)
        .filter(VideoChapter.video_id == video_id)
        .order_by(VideoChapter.start_time.asc())
        .all()
    )


@router.post("/videos/{video_id}/chapters", response_model=ChapterResponse)
def create_chapter(
    video_id: int,
    body: ChapterCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _get_owned_video(db, video_id, current_user)
    chap = VideoChapter(
        video_id=video_id,
        start_time=body.start_time,
        end_time=body.end_time,
        title=body.title,
        source=body.source or "manual",
        order_index=body.order_index or 0,
    )
    db.add(chap)
    db.flush()
    emit_once(
        db,
        "feature_completed",
        event_id=f"feature:chapters:chapter:{chap.id}:created",
        user=current_user,
        workspace_id=video.project.workspace_id if video.project else None,
        properties={
            "feature_key": "chapters",
            "project_id": video.project_id,
            "video_id": video.id,
            "chapter_id": chap.id,
            "completion_type": "chapter_persisted",
            "result": "success",
        },
    )
    db.commit()
    db.refresh(chap)
    return chap


@router.patch("/chapters/{chapter_id}", response_model=ChapterResponse)
def update_chapter(
    chapter_id: int,
    body: ChapterUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    chap = db.query(VideoChapter).filter(VideoChapter.id == chapter_id).first()
    if not chap:
        raise HTTPException(status_code=404, detail="Chapter not found")
    _get_owned_video(db, chap.video_id, current_user)
    for k, v in body.dict(exclude_unset=True).items():
        setattr(chap, k, v)
    db.commit()
    db.refresh(chap)
    return chap


@router.delete("/chapters/{chapter_id}")
def delete_chapter(chapter_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    chap = db.query(VideoChapter).filter(VideoChapter.id == chapter_id).first()
    if not chap:
        raise HTTPException(status_code=404, detail="Chapter not found")
    _get_owned_video(db, chap.video_id, current_user)
    db.delete(chap)
    db.commit()
    return {"ok": True}


@router.post("/videos/{video_id}/chapters/auto")
def auto_chapters(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    enqueued = enqueue_chapter_synthesis_job(video_id)
    return {"ok": True, "enqueued": enqueued}


@router.get(
    "/videos/{video_id}/chapters/youtube-description-block",
    response_model=YoutubeChapterBlockResponse,
)
def youtube_chapter_description_block(
    video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    video = _get_owned_video(db, video_id, current_user)
    chapters = (
        db.query(VideoChapter)
        .filter(VideoChapter.video_id == video_id)
        .order_by(VideoChapter.start_time.asc(), VideoChapter.order_index.asc())
        .all()
    )
    if chapters:
        emit_once(
            db,
            "feature_result_used",
            event_id=f"feature:chapters:video:{video.id}:description-block-opened",
            user=current_user,
            workspace_id=video.project.workspace_id if video.project else None,
            properties={
                "feature_key": "chapters",
                "project_id": video.project_id,
                "video_id": video.id,
                "chapter_count": len(chapters),
                "result_action": "youtube_description_block_opened",
                "result": "success",
            },
        )
        db.commit()
    return YoutubeChapterBlockResponse(block=youtube_description_block(chapters))


# =====================================================================
# End screens
# =====================================================================

@router.get("/videos/{video_id}/end-screen", response_model=EndScreenResponse)
def get_end_screen(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    es = db.query(VideoEndScreen).filter(VideoEndScreen.video_id == video_id).first()
    if not es:
        raise HTTPException(status_code=404, detail="No end screen yet")
    return es


@router.put("/videos/{video_id}/end-screen", response_model=EndScreenResponse)
def upsert_end_screen(
    video_id: int,
    body: EndScreenBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_owned_video(db, video_id, current_user)
    es = db.query(VideoEndScreen).filter(VideoEndScreen.video_id == video_id).first()
    if not es:
        es = VideoEndScreen(video_id=video_id)
        db.add(es)
    es.cards = body.cards
    es.pinned_comment = body.pinned_comment
    db.commit()
    db.refresh(es)
    return es


# =====================================================================
# Brand deals
# =====================================================================

@router.get("/projects/{project_id}/brand-deals", response_model=List[BrandDealResponse])
def list_brand_deals(project_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_project(db, project_id, current_user)
    return (
        db.query(BrandDeal)
        .filter(BrandDeal.project_id == project_id)
        .order_by(BrandDeal.created_at.desc())
        .all()
    )


@router.post("/projects/{project_id}/brand-deals", response_model=BrandDealResponse)
def create_brand_deal(
    project_id: int,
    body: BrandDealCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_owned_project(db, project_id, current_user)
    deal = BrandDeal(project_id=project_id, **body.dict())
    db.add(deal)
    db.flush()
    emit(
        db,
        "feature_completed",
        user=current_user,
        workspace_id=project.workspace_id,
        properties={
            "feature_key": "brand_deal",
            "project_id": project.id,
            "brand_deal_id": deal.id,
            "currency": deal.currency,
            "amount_cents": deal.amount_cents,
            "result": "success",
        },
    )
    db.commit()
    db.refresh(deal)
    return deal


@router.patch("/brand-deals/{deal_id}", response_model=BrandDealResponse)
def update_brand_deal(
    deal_id: int,
    body: BrandDealUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deal = db.query(BrandDeal).filter(BrandDeal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Brand deal not found")
    project = _get_owned_project(db, deal.project_id, current_user)
    previous_payout_status = deal.payout_status
    for k, v in body.dict(exclude_unset=True).items():
        setattr(deal, k, v)
    if body.payout_status == "paid" and not deal.paid_at:
        deal.paid_at = datetime.utcnow()
    if previous_payout_status != deal.payout_status:
        emit(
            db,
            "feature_result_used",
            user=current_user,
            workspace_id=project.workspace_id,
            properties={
                "feature_key": "brand_deal",
                "project_id": project.id,
                "brand_deal_id": deal.id,
                "result_type": "payout_status_changed",
                "payout_status": deal.payout_status,
                "result": "success",
            },
        )
    db.commit()
    db.refresh(deal)
    return deal


@router.delete("/brand-deals/{deal_id}")
def delete_brand_deal(deal_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    deal = db.query(BrandDeal).filter(BrandDeal.id == deal_id).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Brand deal not found")
    _get_owned_project(db, deal.project_id, current_user)
    db.delete(deal)
    db.commit()
    return {"ok": True}


# =====================================================================
# Thumbnail variants (A/B)
# =====================================================================

@router.get("/videos/{video_id}/thumbnails", response_model=List[ThumbnailVariantResponse])
def list_thumbnails(video_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _get_owned_video(db, video_id, current_user)
    return (
        db.query(ThumbnailVariant)
        .filter(ThumbnailVariant.video_id == video_id)
        .order_by(ThumbnailVariant.created_at.asc())
        .all()
    )


@router.post("/videos/{video_id}/thumbnails", response_model=ThumbnailVariantResponse)
def create_thumbnail(
    video_id: int,
    body: ThumbnailVariantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    video = _get_owned_video(db, video_id, current_user)
    t = ThumbnailVariant(video_id=video_id, label=body.label, image_url=body.image_url)
    db.add(t)
    db.flush()
    emit_once(
        db,
        "feature_completed",
        event_id=f"feature:thumbnail:variant:{t.id}:created",
        user=current_user,
        workspace_id=video.project.workspace_id if video.project else None,
        properties={
            "feature_key": "thumbnail",
            "project_id": video.project_id,
            "video_id": video.id,
            "thumbnail_variant_id": t.id,
            "completion_type": "thumbnail_variant_stored",
            "result": "success",
        },
    )
    db.commit()
    db.refresh(t)
    return t


@router.patch("/thumbnails/{thumb_id}", response_model=ThumbnailVariantResponse)
def update_thumbnail(
    thumb_id: int,
    body: ThumbnailVariantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    t = db.query(ThumbnailVariant).filter(ThumbnailVariant.id == thumb_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    video = _get_owned_video(db, t.video_id, current_user)
    was_winner = bool(t.is_winner)
    data = body.dict(exclude_unset=True)
    if data.get("is_winner"):
        db.query(ThumbnailVariant).filter(
            ThumbnailVariant.video_id == t.video_id,
            ThumbnailVariant.id != t.id,
        ).update({"is_winner": False})
    for k, v in data.items():
        setattr(t, k, v)
    if bool(t.is_winner) and not was_winner:
        emit_once(
            db,
            "feature_result_used",
            event_id=f"feature:thumbnail:variant:{t.id}:selected",
            user=current_user,
            workspace_id=video.project.workspace_id if video.project else None,
            properties={
                "feature_key": "thumbnail",
                "project_id": video.project_id,
                "video_id": video.id,
                "thumbnail_variant_id": t.id,
                "result_action": "thumbnail_selected",
                "result": "success",
            },
        )
    db.commit()
    db.refresh(t)
    return t


@router.delete("/thumbnails/{thumb_id}")
def delete_thumbnail(thumb_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    t = db.query(ThumbnailVariant).filter(ThumbnailVariant.id == thumb_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    _get_owned_video(db, t.video_id, current_user)
    db.delete(t)
    db.commit()
    return {"ok": True}
