"""Loss prevention: pilferage / unexplained-loss cases, their evidence and handling."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, require
from ..db import WRITE_LOCK, get_db
from ..services import loss
from .core import commit_change
from .serializers import Ctx, get_or_404

router = APIRouter(prefix="/api/loss", tags=["loss prevention"])


@router.get("/overview")
def overview(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    """KPIs, patterns (by kind, zone, day, outcome), detection controls and the playbook catalogue."""
    c = Ctx(db, factory)
    return {"factory": c.factory, **loss.overview(db, c.fids, c.now)}


@router.get("/incidents")
def incidents(factory: str = "ALL", status: str | None = None, stage: str | None = None, kind: str | None = None,
              camera: str | None = None, limit: int | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    """Cases, active first by severity. status: Open | Investigating | Resolved | active."""
    if kind and kind not in loss.KINDS:
        raise HTTPException(400, f"Unknown kind {kind}")
    c = Ctx(db, factory)
    rows = loss.incidents(db, c.fids, status=status, stage=stage, kind=kind, camera=camera, limit=limit)
    L = loss._lookups(db)
    return {"sim_time": c.now.isoformat(), "factory": c.factory, "total": len(rows),
            "incidents": [loss.incident_dict(db, r, L) for r in rows]}


@router.get("/incidents/{incident_id}")
def incident(incident_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    inc = get_or_404(db, m.LossIncident, incident_id)
    return loss.incident_dict(db, inc, detail=True)


class ActionIn(BaseModel):
    action: str                       # assign | step | note | resolve | reopen
    step: str | None = None
    outcome: str | None = None
    note: str | None = None
    recovered_qty: float | None = None
    assignee: str | None = None


@router.post("/incidents/{incident_id}/actions")
def action(incident_id: int, body: ActionIn, db: Session = Depends(get_db),
           user: m.User = Depends(require("security.write"))):
    with WRITE_LOCK:
        inc = get_or_404(db, m.LossIncident, incident_id)
        now = Ctx(db, None).now
        try:
            loss.act(db, inc, body.action, now, user.full_name or user.username, step=body.step, outcome=body.outcome,
                     note=(body.note or "").strip() or None, recovered_qty=body.recovered_qty, assignee=body.assignee)
        except loss.LossError as e:
            raise HTTPException(e.status, str(e))
        commit_change(db, "loss", recompute=False)
        from ..services import alerts
        alerts.evaluate(db, now)
        db.commit()
        return loss.incident_dict(db, inc, detail=True)
