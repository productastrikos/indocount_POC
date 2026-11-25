"""Executive Overview."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..services import alerts, metrics
from ..services.common import iso
from .serializers import Ctx, alert_dict, lookup

router = APIRouter(prefix="/api/overview")


@router.get("")
def overview(factory: str = "ALL", date: str | None = None, shift: int | None = None, customer: int | None = None,
             db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory, date, shift, customer)
    k = metrics.kpis(db, c.fids, c.day, c.shift, c.customer, c.now)
    pipe = metrics.pipeline(db, c.fids, c.day, c.now)
    L = lookup(db)
    return {
        "sim_time": iso(c.now), "date": c.day.isoformat(), "shift": c.shift, "factory": c.factory,
        "kpis": k,
        "comparison": metrics.factory_comparison(db, c.day, c.now),
        "hourly": metrics.hourly(db, c.fids, c.day, c.shift, c.now),
        "lifecycle": metrics.lifecycle_health(db, c.fids, c.day, c.now, pipe),
        "attention": [alert_dict(a, L) for a in alerts.open_alerts(db, c.fids, 8)],
    }
