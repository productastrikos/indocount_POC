"""Digital twin: US network map and per-facility live twin."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user
from ..db import get_db
from ..services import twin
from ..services.common import factory_ids, sim_now
from .serializers import get_or_404

router = APIRouter(prefix="/api/twin", tags=["twin"])


@router.get("/network")
def network(db: Session = Depends(get_db), user=Depends(current_user)):
    return twin.network(db, sim_now(db))


@router.get("/facility/{code}")
def facility(code: str, db: Session = Depends(get_db), user=Depends(current_user)):
    f = db.scalar(select(m.Factory).where(m.Factory.code == code.upper()))
    if f is None:
        raise HTTPException(404, f"Unknown factory {code}")
    return twin.facility(db, f, sim_now(db))


@router.get("/workers/{employee_id}")
def worker(employee_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    e = get_or_404(db, m.Employee, employee_id)
    return twin.worker(db, e, sim_now(db))


@router.get("/advisories")
def advisories(factory: str = "ALL", limit: int | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    now = sim_now(db)
    return {"sim_time": now.isoformat(), "advisories": twin.workforce_advisories(db, factory_ids(db, factory), now, limit)}
