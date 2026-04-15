"""Public workspace branding by custom domain (verified only)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.workspace_branding_resolve import branding_by_host

router = APIRouter(prefix="/public/workspace", tags=["Public workspace"])


@router.get("/branding")
def get_branding_by_host(
    host: str = Query("", description="Request Host header value, e.g. review.agency.com"),
    db: Session = Depends(get_db),
):
    data = branding_by_host(db, host)
    return data if data is not None else {}
