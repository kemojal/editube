"""The fixtures in `conftest.py` are load-bearing for the review-spine tests,
so they get their own coverage: a broken harness produces false green
everywhere else.
"""

from __future__ import annotations


class DbSessionTests:
    def test_creates_tables_that_need_postgres_only_types(self, db_session) -> None:
        # review_links has ARRAY columns and comments has JSONB; both used to
        # be impossible to create under SQLite.
        from app.db.models import Comment, ReviewLink, Video

        assert db_session.query(ReviewLink).count() == 0
        assert db_session.query(Comment).count() == 0
        assert db_session.query(Video).count() == 0

    def test_jsonb_columns_round_trip(self, db_session, make_video, make_comment) -> None:
        video = make_video()
        comment = make_comment(video, drawing_data=[{"type": "rect", "left": 12}])
        db_session.commit()

        stored = db_session.query(type(comment)).filter_by(id=comment.id).one()
        assert stored.drawing_data == [{"type": "rect", "left": 12}]

    def test_array_columns_round_trip(self, db_session, make_video) -> None:
        from app.db.models import ReviewLink

        video = make_video()
        link = ReviewLink(
            video_id=video.id,
            token="tok-array-test",
            geo_allow_countries=["GB", "US"],
        )
        db_session.add(link)
        db_session.commit()

        stored = db_session.query(ReviewLink).filter_by(token="tok-array-test").one()
        assert stored.geo_allow_countries == ["GB", "US"]

    def test_each_test_gets_a_fresh_database(self, db_session, make_user) -> None:
        # Depends on the previous tests having committed users; if teardown
        # leaked, this count would drift.
        make_user()
        assert db_session.query(type(make_user())).count() == 2


class FactoryTests:
    def test_make_video_defaults_to_in_progress(self, make_video) -> None:
        assert make_video().status == "in_progress"

    def test_make_video_reuses_the_projects_creator_as_uploader(
        self, make_project, make_video
    ) -> None:
        project = make_project()
        assert make_video(project).uploader_id == project.creator_id

    def test_overrides_win(self, make_video) -> None:
        video = make_video(name="Hero cut", version=4, status="in_review")
        assert (video.name, video.version, video.status) == ("Hero cut", 4, "in_review")


class ApiClientTests:
    def test_unauthenticated_requests_are_rejected(self, api_client) -> None:
        assert api_client.get("/videos/1").status_code == 401

    def test_authenticated_request_reaches_the_handler(
        self, api_client, db_session, make_video
    ) -> None:
        video = make_video()
        db_session.commit()
        owner = db_session.get(type(video), video.id).uploader_id

        from app.db.models import User

        api_client.login(db_session.get(User, owner))
        response = api_client.get(f"/videos/{video.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == video.id
        assert body["status"] == "in_progress"

    def test_missing_row_is_a_404_not_a_crash(
        self, api_client, db_session, make_user
    ) -> None:
        api_client.login(make_user())
        db_session.commit()
        assert api_client.get("/videos/99999").status_code == 404
