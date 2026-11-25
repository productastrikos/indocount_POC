"""CCTV feeds with computer-vision analytics (see services/cctv.py)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user
from ..db import get_db
from ..services import cctv
from ..services.common import sim_now

router = APIRouter(prefix="/api/cctv", tags=["cctv"])


@router.get("/feeds")
def feeds(factory: str = "ALL", kind: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    codes = None if factory.upper() == "ALL" else [factory.upper()]
    if codes and db.scalar(select(m.Factory.id).where(m.Factory.code == codes[0])) is None:
        raise HTTPException(404, f"Unknown factory {factory}")
    return {"sim_time": sim_now(db).isoformat(), "feeds": cctv.feeds(db, codes, kind)}


@router.get("/feeds/{feed_id}")
def feed(feed_id: str, db: Session = Depends(get_db), user=Depends(current_user)):
    out = cctv.feed_detail(db, feed_id, sim_now(db))
    if out is None:
        raise HTTPException(404, f"Unknown feed {feed_id}")
    return out
