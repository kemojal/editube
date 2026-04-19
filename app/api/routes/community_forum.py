from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional

from app.db.database import get_db
from app.db.models import ForumCategory, ForumPost, ForumComment, ForumVote, User
from app.api.models.forum import (
    ForumCategoryResponse,
    ForumPostCreate,
    ForumPostUpdate,
    ForumPostResponse,
    ForumPostDetailResponse,
    ForumCommentCreate,
    ForumCommentResponse,
)
from app.utils.security import authenticate_access_token, get_current_user

router = APIRouter(prefix="/community/forum", tags=["Community Forum"])

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

@router.get("/categories", response_model=List[ForumCategoryResponse])
def get_categories(db: Session = Depends(get_db)):
    categories = db.query(ForumCategory).order_by(ForumCategory.name.asc()).all()
    return categories

@router.get("/posts", response_model=List[ForumPostResponse])
def get_posts(
    category_id: Optional[int] = None,
    status_: Optional[str] = Query(None, alias="status"),
    sort: str = "latest",  # latest, popular
    skip: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    current_user = _get_user_from_auth_header(db, authorization)
    query = db.query(ForumPost)
    
    if category_id:
        query = query.filter(ForumPost.category_id == category_id)
    if status_:
        query = query.filter(ForumPost.status == status_)
        
    if sort == "popular":
        query = query.outerjoin(ForumVote).group_by(ForumPost.id).order_by(func.count(ForumVote.id).desc(), ForumPost.created_at.desc())
    else:
        query = query.order_by(ForumPost.created_at.desc())
        
    posts = query.offset(skip).limit(limit).all()
    
    results = []
    for post in posts:
        upvotes_count = db.query(func.count(ForumVote.id)).filter(ForumVote.post_id == post.id).scalar() or 0
        comments_count = db.query(func.count(ForumComment.id)).filter(ForumComment.post_id == post.id).scalar() or 0
        has_upvoted = False
        if current_user:
            vote = db.query(ForumVote).filter(ForumVote.post_id == post.id, ForumVote.user_id == current_user.id).first()
            if vote:
                has_upvoted = True
                
        post_dict = {
            "id": post.id,
            "title": post.title,
            "content": post.content,
            "category_id": post.category_id,
            "user_id": post.user_id,
            "status": post.status,
            "view_count": post.view_count,
            "created_at": post.created_at,
            "updated_at": post.updated_at,
            "upvotes_count": upvotes_count,
            "comments_count": comments_count,
            "has_upvoted": has_upvoted,
            "user": post.user,
            "category": post.category
        }
        results.append(post_dict)
        
    return results

@router.post("/posts", response_model=ForumPostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    post_in: ForumPostCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    category = db.query(ForumCategory).filter(ForumCategory.id == post_in.category_id).first()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
        
    new_post = ForumPost(
        title=post_in.title,
        content=post_in.content,
        category_id=post_in.category_id,
        user_id=current_user.id
    )
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    
    vote = ForumVote(post_id=new_post.id, user_id=current_user.id)
    db.add(vote)
    db.commit()
    
    post_dict = {
        "id": new_post.id,
        "title": new_post.title,
        "content": new_post.content,
        "category_id": new_post.category_id,
        "user_id": new_post.user_id,
        "status": new_post.status,
        "view_count": new_post.view_count,
        "created_at": new_post.created_at,
        "updated_at": new_post.updated_at,
        "upvotes_count": 1,
        "comments_count": 0,
        "has_upvoted": True,
        "user": current_user,
        "category": category
    }
    return post_dict

@router.get("/posts/{post_id}", response_model=ForumPostDetailResponse)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    current_user = _get_user_from_auth_header(db, authorization)
    post = db.query(ForumPost).filter(ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    post.view_count += 1
    db.commit()
    
    upvotes_count = db.query(func.count(ForumVote.id)).filter(ForumVote.post_id == post.id).scalar() or 0
    comments = db.query(ForumComment).filter(ForumComment.post_id == post.id).order_by(ForumComment.created_at.asc()).all()
    comments_count = len(comments)
    
    has_upvoted = False
    if current_user:
        vote = db.query(ForumVote).filter(ForumVote.post_id == post.id, ForumVote.user_id == current_user.id).first()
        if vote:
            has_upvoted = True
            
    post_dict = {
        "id": post.id,
        "title": post.title,
        "content": post.content,
        "category_id": post.category_id,
        "user_id": post.user_id,
        "status": post.status,
        "view_count": post.view_count,
        "created_at": post.created_at,
        "updated_at": post.updated_at,
        "upvotes_count": upvotes_count,
        "comments_count": comments_count,
        "has_upvoted": has_upvoted,
        "user": post.user,
        "category": post.category,
        "comments": comments
    }
    return post_dict

@router.post("/posts/{post_id}/comments", response_model=ForumCommentResponse, status_code=status.HTTP_201_CREATED)
def create_comment(
    post_id: int,
    comment_in: ForumCommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    post = db.query(ForumPost).filter(ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    if comment_in.parent_id:
        parent = db.query(ForumComment).filter(ForumComment.id == comment_in.parent_id, ForumComment.post_id == post_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent comment not found")
            
    new_comment = ForumComment(
        content=comment_in.content,
        post_id=post_id,
        user_id=current_user.id,
        parent_id=comment_in.parent_id
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    return new_comment

@router.post("/posts/{post_id}/vote")
def toggle_vote(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    post = db.query(ForumPost).filter(ForumPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
        
    existing_vote = db.query(ForumVote).filter(ForumVote.post_id == post_id, ForumVote.user_id == current_user.id).first()
    
    if existing_vote:
        db.delete(existing_vote)
        db.commit()
        return {"status": "unvoted"}
    else:
        new_vote = ForumVote(post_id=post_id, user_id=current_user.id)
        db.add(new_vote)
        db.commit()
        return {"status": "voted"}
