from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.api.models.suggestions import (
    SuggestionCommentCreate,
    SuggestionCommentResponse,
    SuggestionCreate,
    SuggestionResponse,
)
from app.db.database import get_db
from app.db.models import Suggestion, SuggestionComment, SuggestionVote, User
from app.utils.security import authenticate_access_token, get_current_user

router = APIRouter(prefix="/community/suggestions", tags=["Suggestions"])


def _get_user_from_auth_header(db: Session, authorization: str | None) -> User | None:
    if not authorization:
        return None
    prefix = "bearer "
    if not authorization.lower().startswith(prefix):
        return None
    token = authorization[len(prefix) :].strip()
    if not token:
        return None
    try:
        return authenticate_access_token(db, token, touch_session=False)
    except HTTPException:
        return None


def _serialize_suggestion(suggestion: Suggestion, voted_by_me: bool = False) -> dict:
    return {
        "id": suggestion.id,
        "title": suggestion.title,
        "body": suggestion.body,
        "category": suggestion.category,
        "status": suggestion.status,
        "upvotes_count": suggestion.upvotes_count,
        "comments_count": len(suggestion.comments or []),
        "voted_by_me": voted_by_me,
        "user": suggestion.user,
        "created_at": suggestion.created_at,
        "updated_at": suggestion.updated_at,
    }


@router.get("/", response_model=list[SuggestionResponse])
def list_suggestions(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    current_user = _get_user_from_auth_header(db, authorization)
    suggestions = (
        db.query(Suggestion)
        .order_by(Suggestion.upvotes_count.desc(), Suggestion.created_at.desc())
        .all()
    )
    user_votes: set[int] = set()
    if current_user:
        user_votes = {
            row.suggestion_id
            for row in db.query(SuggestionVote)
            .filter(SuggestionVote.user_id == current_user.id)
            .all()
        }
    return [_serialize_suggestion(s, voted_by_me=s.id in user_votes) for s in suggestions]


@router.get("/{suggestion_id}", response_model=SuggestionResponse)
def get_suggestion(
    suggestion_id: int,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    suggestion = db.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    current_user = _get_user_from_auth_header(db, authorization)
    voted_by_me = False
    if current_user:
        voted_by_me = (
            db.query(SuggestionVote)
            .filter(
                SuggestionVote.suggestion_id == suggestion_id,
                SuggestionVote.user_id == current_user.id,
            )
            .first()
            is not None
        )
    return _serialize_suggestion(suggestion, voted_by_me=voted_by_me)


@router.get("/{suggestion_id}/comments", response_model=list[SuggestionCommentResponse])
def list_suggestion_comments(suggestion_id: int, db: Session = Depends(get_db)):
    suggestion = db.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    comments = (
        db.query(SuggestionComment)
        .filter(SuggestionComment.suggestion_id == suggestion_id)
        .order_by(SuggestionComment.created_at.asc())
        .all()
    )
    return comments


@router.post("/", response_model=SuggestionResponse)
def create_suggestion(
    payload: SuggestionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    suggestion = Suggestion(
        user_id=current_user.id,
        title=payload.title.strip(),
        body=payload.body.strip(),
        category=payload.category.strip() if payload.category else None,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return _serialize_suggestion(suggestion, voted_by_me=False)


@router.post("/{suggestion_id}/comments", response_model=SuggestionCommentResponse)
def create_suggestion_comment(
    suggestion_id: int,
    payload: SuggestionCommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    suggestion = db.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    comment = SuggestionComment(
        suggestion_id=suggestion_id,
        user_id=current_user.id,
        body=payload.body.strip(),
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


@router.post("/{suggestion_id}/vote", response_model=SuggestionResponse)
def toggle_suggestion_vote(
    suggestion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    suggestion = db.query(Suggestion).filter(Suggestion.id == suggestion_id).first()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    existing = (
        db.query(SuggestionVote)
        .filter(
            SuggestionVote.suggestion_id == suggestion_id,
            SuggestionVote.user_id == current_user.id,
        )
        .first()
    )
    if existing:
        db.delete(existing)
        suggestion.upvotes_count = max(0, int(suggestion.upvotes_count or 0) - 1)
        voted_by_me = False
    else:
        db.add(SuggestionVote(suggestion_id=suggestion_id, user_id=current_user.id))
        suggestion.upvotes_count = int(suggestion.upvotes_count or 0) + 1
        voted_by_me = True

    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return _serialize_suggestion(suggestion, voted_by_me=voted_by_me)
