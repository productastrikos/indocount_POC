"""Predictive maintenance analytics and S!AP advisories (illustrative model on simulated telemetry)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, require
from ..db import WRITE_LOCK, get_db
from ..services import predictive as pm
from ..services.common import iso, log_event
from .core import commit_change
from .production import _wo_row
from .serializers import Ctx, get_or_404, lookup

router = APIRouter(prefix="/api/predictive", tags=["predictive"])


@router.get("/machines")
def machines(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    """Per-machine failure predictions + fleet analytics + related predictions for a factory scope."""
    c = Ctx(db, factory)
    return {"factory": c.factory} | pm.fleet_view(db, c.fids, c.now)


@router.get("/machines/{machine_id}")
def machine(machine_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    """One machine: prediction, 14-day probability / hazard curve, telemetry trend vs threshold, options."""
    mc = get_or_404(db, m.Machine, machine_id)
    c = Ctx(db, None)
    return pm.machine_detail(db, mc, c.now)


@router.get("/advisories")
def advisories(factory: str = "ALL", limit: int | None = None, category: str | None = None,
               db: Session = Depends(get_db), user=Depends(current_user)):
    """S!AP advisories ranked by severity then impact. `category` filters (comma-separated, case-insensitive)."""
    c = Ctx(db, factory)
    items = pm.advisories(db, c.fids, c.now)
    if category:
        wanted = {x.strip().lower() for x in category.split(",") if x.strip()}
        items = [a for a in items if a["category"].lower() in wanted]
    counts = {s: sum(1 for a in items if a["severity"] == s) for s in pm.SEVERITY_RANK}
    return {"sim_time": iso(c.now), "factory": c.factory, "total": len(items), "counts": counts,
            "advisories": items[:limit] if limit and limit > 0 else items, "disclaimer": pm.DISCLAIMER}


class ScheduleIn(BaseModel):
    start: datetime | None = None
    end: datetime | None = None


@router.post("/advisories/{advisory_id}/schedule")
def schedule(advisory_id: str, body: ScheduleIn | None = None, db: Session = Depends(get_db),
             user: m.User = Depends(require("maintenance.write"))):
    """Create a Predictive work order for the advisory's recommended (or the given) window."""
    body = body or ScheduleIn()
    with WRITE_LOCK:
        now = Ctx(db, None).now
        try:
            wo = pm.schedule_work_order(db, advisory_id, now, user.username, body.start, body.end)
        except pm.ScheduleError as e:
            raise HTTPException(e.status, str(e))
        mc = wo.machine or db.get(m.Machine, wo.machine_id)
        log_event(db, now, wo.factory_id, "production", "work_order",
                  f"{wo.number} scheduled from S!AP advisory for {mc.code} by {user.username}, "
                  f"window {wo.scheduled_start:%a %H:%M}–{wo.scheduled_end:%H:%M}", "machine", mc.id, user.username)
        commit_change(db, "maintenance")
        return _wo_row(wo, lookup(db)) | {"advisory_id": advisory_id}
