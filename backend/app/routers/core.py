"""Auth, metadata, simulation control, alerts, insights and settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import DEMO_USERS, ROLES, create_token, current_user, require, user_dict, verify_password
from ..config import get_settings
from ..db import WRITE_LOCK, get_db
from ..realtime import notify_change, runner
from ..services import alerts, insights, planning
from ..services.common import business_rules, iso, set_setting, sim_now, state
from ..simulation import engine, scenarios
from .serializers import Ctx, alert_dict, lookup

router = APIRouter(prefix="/api")


def commit_change(db: Session, topic: str, recompute: bool = True):
    now = sim_now(db)
    if recompute:
        db.flush()
        planning.recompute(db, now)
        alerts.evaluate(db, now)
    db.commit()
    notify_change(topic)


# ─── auth ────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.scalar(select(m.User).where(m.User.username == body.username.strip().lower()))
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Invalid username or password")
    return {"token": create_token(u), "user": user_dict(u)}


@router.get("/auth/me")
def me(user: m.User = Depends(current_user)):
    return user_dict(user)


@router.get("/auth/demo-users")
def demo_users():
    return [{"username": u, "name": n, "role": r, "role_label": ROLES[r]["label"], "password": p} for u, n, r, p in DEMO_USERS]


# ─── metadata ───────────────────────────────────────────────────────────
@router.get("/meta")
def meta(db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    s = get_settings()
    st = state(db)
    return {
        "app": s.app_name,
        "factories": [{"id": f.id, "code": f.code, "name": f.name, "state": f.state, "timezone": f.timezone,
                       "daily_target": f.daily_target} for f in db.scalars(select(m.Factory).order_by(m.Factory.id))],
        "customers": [{"id": c.id, "code": c.code, "name": c.name, "channel": c.channel} for c in db.scalars(select(m.Customer))],
        "skus": [{"id": x.id, "code": x.code, "customer_id": x.customer_id, "description": x.description,
                  "pillows_per_carton": x.pillows_per_carton} for x in db.scalars(select(m.Sku))],
        "materials": [{"id": x.id, "code": x.code, "name": x.name, "uom": x.uom, "category": x.category}
                      for x in db.scalars(select(m.Material))],
        "suppliers": [{"id": x.id, "code": x.code, "name": x.name, "lead_time_days": x.lead_time_days,
                       "materials": x.material_codes} for x in db.scalars(select(m.Supplier))],
        "lines": [{"id": x.id, "code": x.code, "name": x.name, "factory_id": x.factory_id}
                  for x in db.scalars(select(m.ProductionLine).order_by(m.ProductionLine.id))],
        "rules": business_rules(db),
        "sim": sim_state_dict(st),
        "llm_provider": s.llm_provider, "cv_provider": s.cv_provider,
        "integrations": integrations_status(),
    }


def integrations_status():
    from ..integrations import registry
    return registry.status()


def sim_state_dict(st: m.SimState) -> dict:
    return {"sim_time": iso(st.sim_time), "running": st.running, "speed": st.speed, "tick": st.tick,
            "scenario_log": list(reversed(st.scenario_log or []))[:10], "tick_ms": runner.last_tick_ms,
            "last_error": runner.last_error}


# ─── simulation control ─────────────────────────────────────────────────
@router.get("/sim/state")
def sim_state(db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    return sim_state_dict(state(db))


class SpeedIn(BaseModel):
    speed: int


@router.post("/sim/{action}")
def sim_control(action: str, body: SpeedIn | None = None, db: Session = Depends(get_db), user=Depends(require("simulation"))):
    with WRITE_LOCK:
        st = state(db)
        if action == "start":
            st.running = True
        elif action == "pause":
            st.running = False
        elif action == "speed":
            if not body or body.speed not in (1, 5, 15, 30):
                raise HTTPException(400, "Speed must be 1, 5, 15 or 30 simulated minutes per tick")
            st.speed = body.speed
        elif action == "step":
            engine.step(db, (body.speed if body else 0) or st.speed or 1)
        elif action == "reset":
            from ..seed import reset_all, seed
            was_running = st.running
            db.close()
            from ..db import SessionLocal
            fresh = SessionLocal()
            try:
                reset_all(fresh)
                seed(fresh)
                fresh.get(m.SimState, 1).running = was_running
                fresh.commit()
                notify_change("reset")
                return sim_state_dict(state(fresh))
            finally:
                fresh.close()
        else:
            raise HTTPException(404, "Unknown action")
        db.commit()
        notify_change("sim")
        return sim_state_dict(st)


class ScenarioIn(BaseModel):
    kind: str
    factory_id: int | None = None
    line_id: int | None = None
    machine_id: int | None = None
    material_code: str | None = None


@router.post("/sim/scenario/run")
def run_scenario(body: ScenarioIn, db: Session = Depends(get_db), user=Depends(require("simulation"))):
    with WRITE_LOCK:
        params = {"factory_id": body.factory_id}
        if body.kind in ("machine_failure",):
            params.update(line_id=body.line_id, machine_id=body.machine_id)
        elif body.kind in ("production_delay", "packaging_mismatch", "workforce_decline"):
            params.update(line_id=body.line_id)
        elif body.kind == "material_shortage":
            params.update(material_code=body.material_code)
        try:
            msg = scenarios.trigger(db, body.kind, engine.RNG, **params)
        except scenarios.ScenarioError as e:
            db.rollback()
            raise HTTPException(400, str(e))
        db.commit()
        notify_change("scenario", msg)
        return {"message": msg, "sim": sim_state_dict(state(db))}


@router.get("/sim/scenarios")
def list_scenarios(user=Depends(current_user)):
    return [{"kind": k, "label": v[0]} for k, v in scenarios.SCENARIOS.items()]


# ─── alerts / insights / activity ───────────────────────────────────────
@router.get("/alerts")
def get_alerts(factory: str = "ALL", stage: str | None = None, limit: int = 60, db: Session = Depends(get_db),
               user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    return [alert_dict(a, L) for a in alerts.open_alerts(db, c.fids, limit, stage)]


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    with WRITE_LOCK:
        a = db.get(m.Alert, alert_id)
        if not a:
            raise HTTPException(404, "Alert not found")
        a.status = "Acknowledged"
        a.acknowledged_by = user.username
        db.commit()
        notify_change("alerts")
        return {"ok": True}


@router.get("/insights")
def get_insights(page: str = "overview", factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    fn = insights.PAGES.get(page)
    if not fn:
        raise HTTPException(404, "Unknown page")
    return {"page": page, "generated_at": iso(c.now), "insights": fn(db, c.fids, c.now),
            "method": "Rule-based analytics over live operational records (no generic text)."}


@router.get("/events")
def events(factory: str = "ALL", entity_type: str | None = None, entity_id: int | None = None, stage: str | None = None,
           limit: int = 40, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    q = select(m.EventLog).where((m.EventLog.factory_id.in_(c.fids)) | (m.EventLog.factory_id.is_(None)))
    if entity_type:
        q = q.where(m.EventLog.entity_type == entity_type)
    if entity_id:
        q = q.where(m.EventLog.entity_id == entity_id)
    if stage:
        q = q.where(m.EventLog.stage == stage)
    rows = db.scalars(q.order_by(m.EventLog.ts.desc(), m.EventLog.id.desc()).limit(min(limit, 200)))
    return [{"id": e.id, "ts": iso(e.ts), "stage": e.stage, "type": e.event_type, "message": e.message,
             "entity_type": e.entity_type, "entity_id": e.entity_id, "user": e.user} for e in rows]


class RulesIn(BaseModel):
    detention_charge: float | None = None
    detention_free_min: float | None = None
    changeover_standard_min: float | None = None
    shift2_differential: float | None = None


@router.put("/settings/rules")
def update_rules(body: RulesIn, db: Session = Depends(get_db), user=Depends(require("settings"))):
    with WRITE_LOCK:
        for k, v in body.model_dump(exclude_none=True).items():
            if v < 0:
                raise HTTPException(400, f"{k} must be positive")
            set_setting(db, k, v)
        commit_change(db, "settings")
        return business_rules(db)
