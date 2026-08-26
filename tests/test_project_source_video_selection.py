from datetime import datetime, timedelta

from app.api.routes.projects import _latest_project_source_video


def test_latest_project_video_ignores_newer_rough_cut_asset(
    db_session, make_project, make_video
):
    project = make_project()
    now = datetime.utcnow()
    source = make_video(
        project=project,
        description="Rough cut source",
        updated_at=now,
    )
    make_video(
        project=project,
        description="  ROUGH CUT ASSET ",
        updated_at=now + timedelta(minutes=1),
    )
    db_session.commit()

    selected = _latest_project_source_video(db_session, project.id)

    assert selected is not None
    assert selected.id == source.id


def test_latest_project_video_is_none_when_project_only_has_assets(
    db_session, make_project, make_video
):
    project = make_project()
    make_video(project=project, description="Rough cut asset")
    db_session.commit()

    assert _latest_project_source_video(db_session, project.id) is None
