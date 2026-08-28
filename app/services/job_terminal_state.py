"""Resolve safe ownership and persisted terminal state for background jobs.

RQ only knows a function name and arguments. Product analytics needs the user,
workspace, project, video, and authoritative row status without copying any
user-authored fields into an event. This module is the single mapping between
job resources and that privacy-safe context.
"""

from __future__ import annotations

from typing import Any

from app.db.database import SessionLocal


JOB_FEATURE_BY_FRAGMENT: tuple[tuple[str, str], ...] = (
    ("rough_cut_export", "export"),
    ("rough_cut_effect", "rough_cut"),
    ("rough_cut", "rough_cut"),
    ("transcri", "transcript_edit"),
    ("clip_render", "clip_render"),
    ("ugc_product", "ugc_product_import"),
    ("ugc_brief", "ugc_brief"),
    ("ugc_variation", "ugc_variations"),
    ("ugc_render", "ugc_render"),
    ("ai_review", "ai_review"),
    ("director", "ai_director"),
    ("generated_media", "broll_generation"),
    ("generate_media", "broll_generation"),
    ("ai_media", "broll_generation"),
    ("mask_track", "mask_tracking"),
    ("aspect_export", "multi_aspect_export"),
    ("multi_format_export", "multi_aspect_export"),
    ("delivery_package", "delivery"),
    ("chapter", "chapters"),
    ("watch_folder", "watch_folder"),
    ("drive_import", "google_drive"),
    ("youtube_publish", "youtube_publish"),
)


def feature_key_for_job(job_type: str, context: dict[str, Any] | None = None) -> str | None:
    override = (context or {}).get("feature_key")
    if isinstance(override, str) and override:
        return override
    lowered = job_type.lower()
    return next(
        (feature for fragment, feature in JOB_FEATURE_BY_FRAGMENT if fragment in lowered),
        None,
    )


def _integer(value: int | str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def persisted_job_context(
    job_type: str,
    resource_id: int | str | None,
) -> dict[str, Any]:
    """Return identifiers and status safe to attach to job analytics."""

    numeric_id = _integer(resource_id)
    if numeric_id is None:
        return {}

    from app.db.models import (
        AiResult,
        Clip,
        DeliveryExport,
        DeliveryPackage,
        DirectorPlan,
        DriveImport,
        GeneratedMedia,
        Project,
        UgcBrief,
        UgcCampaign,
        UgcProduct,
        UgcVariation,
        Video,
        VideoAspectExport,
        VideoProxy,
        VideoPublication,
        VideoTranscription,
        WatchFolderConfig,
        WorkspaceMember,
    )

    lowered = job_type.lower()
    db = SessionLocal()
    try:
        row = None
        user_id = None
        workspace_id = None
        project_id = None
        video_id = None
        feature_key = None

        if "generated_media" in lowered or "ai_media" in lowered:
            row = db.query(GeneratedMedia).filter(GeneratedMedia.id == numeric_id).first()
            if row:
                user_id, project_id, video_id = row.user_id, row.project_id, row.video_id
        elif "ai_review" in lowered:
            row = (
                db.query(AiResult)
                .filter(AiResult.video_id == numeric_id, AiResult.result_type == "review")
                .first()
            )
            video_id = numeric_id
        elif "director" in lowered:
            row = db.query(DirectorPlan).filter(DirectorPlan.id == numeric_id).first()
            if row:
                user_id, project_id, video_id = row.user_id, row.project_id, row.video_id
        elif "drive_import" in lowered:
            row = db.query(DriveImport).filter(DriveImport.id == numeric_id).first()
            if row:
                user_id = row.user_id
        elif (
            "mask_track" in lowered
            or "rough_cut_effect" in lowered
            or "rough_cut_export" in lowered
        ):
            row = db.query(AiResult).filter(AiResult.id == numeric_id).first()
            if row:
                video_id = row.video_id
                if "rough_cut_effect" in lowered:
                    effect_type = str((row.result_data or {}).get("effectType") or "")
                    feature_key = {
                        "remove_bg": "background_removal",
                        "retouch": "retouch",
                        "chroma_key": "chroma_key",
                        "adjust": "color_adjust",
                    }.get(effect_type, "rough_cut")
        elif "clip_render" in lowered:
            row = db.query(Clip).filter(Clip.id == numeric_id).first()
            if row:
                user_id, video_id = row.user_id, row.video_id
        elif "ugc_product" in lowered:
            row = db.query(UgcProduct).filter(UgcProduct.id == numeric_id).first()
            if row:
                user_id, workspace_id = row.user_id, row.workspace_id
        elif "ugc_brief" in lowered:
            row = (
                db.query(UgcBrief)
                .filter(UgcBrief.product_id == numeric_id)
                .order_by(UgcBrief.id.desc())
                .first()
            )
            product = db.query(UgcProduct).filter(UgcProduct.id == numeric_id).first()
            if product:
                user_id, workspace_id = product.user_id, product.workspace_id
        elif "ugc_variation" in lowered:
            row = db.query(UgcCampaign).filter(UgcCampaign.id == numeric_id).first()
            if row:
                user_id, workspace_id = row.user_id, row.workspace_id
        elif "ugc_render" in lowered:
            row = db.query(UgcVariation).filter(UgcVariation.id == numeric_id).first()
            campaign = (
                db.query(UgcCampaign).filter(UgcCampaign.id == row.campaign_id).first()
                if row
                else None
            )
            if campaign:
                user_id, workspace_id = campaign.user_id, campaign.workspace_id
        elif "aspect_export" in lowered:
            row = db.query(VideoAspectExport).filter(VideoAspectExport.id == numeric_id).first()
            if row:
                video_id = row.video_id
        elif "multi_format_export" in lowered:
            row = db.query(DeliveryExport).filter(DeliveryExport.id == numeric_id).first()
            if row:
                user_id, video_id = row.created_by, row.video_id
        elif "delivery_package" in lowered:
            row = db.query(DeliveryPackage).filter(DeliveryPackage.id == numeric_id).first()
            if row:
                user_id, project_id, video_id = (
                    row.requested_by_user_id,
                    row.project_id,
                    row.video_id,
                )
        elif "youtube_publish" in lowered:
            row = db.query(VideoPublication).filter(VideoPublication.id == numeric_id).first()
            if row:
                user_id, video_id = row.created_by, row.video_id
        elif "transcri" in lowered:
            row = (
                db.query(VideoTranscription)
                .filter(VideoTranscription.video_id == numeric_id)
                .first()
            )
            video_id = numeric_id
        elif "proxy_generation" in lowered:
            row = db.query(VideoProxy).filter(VideoProxy.id == numeric_id).first()
            if row:
                video_id = row.video_id
        elif "chapter" in lowered:
            row = db.query(Video).filter(Video.id == numeric_id).first()
            video_id = numeric_id
        elif "watch_folder" in lowered:
            row = (
                db.query(WatchFolderConfig)
                .filter(WatchFolderConfig.id == numeric_id)
                .first()
            )
            if row:
                user_id, project_id = row.user_id, row.project_id

        if video_id is not None:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                user_id = user_id or video.uploader_id
                project_id = project_id or video.project_id
        if project_id is not None:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project:
                workspace_id = workspace_id or project.workspace_id
                user_id = user_id or project.creator_id
        if workspace_id is None and user_id is not None:
            membership = (
                db.query(WorkspaceMember.workspace_id)
                .filter(WorkspaceMember.user_id == user_id)
                .order_by(WorkspaceMember.id.asc())
                .first()
            )
            workspace_id = int(membership[0]) if membership else None

        return {
            "status": str(getattr(row, "status", "") or "").lower() or None,
            "user_id": _integer(user_id),
            "workspace_id": _integer(workspace_id),
            "project_id": _integer(project_id),
            "video_id": _integer(video_id),
            "feature_key": feature_key_for_job(lowered, {"feature_key": feature_key}),
        }
    except Exception:
        # Analytics must never change job behavior. This also lets unit tests
        # that omit optional Postgres schemas exercise unrelated job types.
        return {}
    finally:
        db.close()


def persisted_job_status(job_type: str, resource_id: int | str | None) -> str | None:
    return persisted_job_context(job_type, resource_id).get("status")
