"""Dispatch & Delivery."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, require
from ..db import WRITE_LOCK, get_db
from ..services import readiness
from ..services.common import business_rules, iso, log_event, next_number
from ..simulation import engine
from .core import commit_change
from .production import prioritize_po, reallocate_po
from .serializers import Ctx, get_or_404, lookup, prod_order_dict, truck_dict
from .warehouse import expedite_order

router = APIRouter(prefix="/api/dispatch")


def _order_row(o: m.CustomerOrder, info: dict, L, now) -> dict:
    rd, risk, truck = info["readiness"], info["risk"], info["truck"]
    prod = sum(p.completed_qty for p in info["pos"])
    return {"id": o.id, "number": o.number, "customer": o.customer.name, "customer_id": o.customer_id, "sku": o.sku.code,
            "factory": L["factories"][o.factory_id].code, "factory_id": o.factory_id, "qty": o.qty,
            "production_completed": min(o.qty, prod + max(0, o.qty - sum(p.required_qty for p in info["pos"]))),
            "available_inventory": o.allocated_qty, "picked": o.picked_qty, "pending_qty": max(0, o.qty - o.picked_qty),
            "ship_window_start": iso(o.ship_window_start), "ship_window_end": iso(o.ship_window_end),
            "scheduled_dispatch": iso(o.scheduled_dispatch), "priority": o.priority,
            "status": rd["status"], "readiness_pct": rd["pct"], "readiness": rd["ratios"], "blocking": rd["blocking"],
            "ready_eta": iso(info["ready_eta"]), "risk_level": risk["level"], "at_risk": risk["at_risk"],
            "slack_min": risk["slack_min"], "est_detention": risk["est_detention"], "risk_reasons": risk["reasons"],
            "truck": {"id": truck.id, "ref": truck.ref, "dock": truck.dock, "arrival": iso(truck.scheduled_arrival),
                      "status": truck.status} if truck else None,
            "dispatched_at": iso(o.dispatched_at), "delivered_at": iso(o.delivered_at), "expedite": o.expedite_picking}


@router.get("/orders")
def orders(factory: str = "ALL", customer: int | None = None, view: str = "active", db: Session = Depends(get_db),
           user=Depends(current_user)):
    c = Ctx(db, factory, customer=customer)
    q = select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(c.fids))
    if c.customer:
        q = q.where(m.CustomerOrder.customer_id == c.customer)
    if view == "active":
        q = q.where((m.CustomerOrder.status == "Open") | (m.CustomerOrder.dispatched_at >= c.now - timedelta(days=3)))
    orders_ = list(db.scalars(q.order_by(m.CustomerOrder.scheduled_dispatch)))
    info = readiness.assess(db, orders_, c.now)
    L = lookup(db)
    rows = [_order_row(o, info[o.id], L, c.now) for o in orders_]
    if view == "at_risk":
        rows = [r for r in rows if r["at_risk"]]
    return rows


@router.get("/summary")
def summary(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    rows = orders(factory, None, "active", db, user)
    open_rows = [r for r in rows if r["status"] not in ("Dispatched", "Delivered")]
    due48 = [r for r in open_rows if r["scheduled_dispatch"] and r["scheduled_dispatch"] <= iso(c.now + timedelta(hours=48))]
    day0 = datetime.combine(c.now.date(), datetime.min.time())
    trucks = list(db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(c.fids), m.TruckAppointment.scheduled_arrival >= day0,
        m.TruckAppointment.scheduled_arrival < day0 + timedelta(days=1))))
    det = readiness.detention_summary(db, c.fids, c.now, 7)
    return {"open_orders": len(open_rows), "due_48h": len(due48),
            "ready": sum(1 for r in due48 if r["status"] == "Ready for Dispatch"),
            "at_risk": sum(1 for r in open_rows if r["at_risk"]),
            "trucks_today": len(trucks), "trucks_departed_today": sum(1 for t in trucks if t.status == "Departed"),
            "trucks_waiting": sum(1 for t in trucks if t.status == "Arrived"),
            "dispatched_today": sum(1 for r in rows if r["dispatched_at"] and r["dispatched_at"] >= iso(day0)),
            "detention": {k: det[k] for k in ("incurred_estimate", "projected_estimate", "delayed_trucks", "avg_wait_min", "charge_per_truck")}}


@router.get("/orders/{order_id}")
def order_detail(order_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    o = get_or_404(db, m.CustomerOrder, order_id)
    c = Ctx(db, None)
    L = lookup(db)
    info = readiness.assess(db, [o], c.now)[o.id]
    allocs = [{"id": a.id, "pallet": a.pallet.code, "location": a.pallet.location.code if a.pallet.location else "staging",
               "cartons": a.cartons, "pillows": a.pillows, "status": a.status, "picked_at": iso(a.picked_at)}
              for a in db.scalars(select(m.Allocation).where(m.Allocation.order_id == o.id))]
    trucks = [truck_dict(t, L, c.now) for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.order_id == o.id))]
    events = [{"ts": iso(e.ts), "stage": e.stage, "message": e.message}
              for e in db.scalars(select(m.EventLog).where(m.EventLog.entity_type == "order", m.EventLog.entity_id == o.id)
                                  .order_by(m.EventLog.ts.desc()).limit(20))]
    return {"order": _order_row(o, info, L, c.now), "stages": [{"key": k, "label": lbl, "pct": info["readiness"]["ratios"][k]}
                                                               for k, lbl in readiness.STAGES],
            "recommendations": readiness.recommendations(o, info, c.now),
            "production_orders": [prod_order_dict(p, L) for p in info["pos"]], "allocations": allocs, "trucks": trucks,
            "events": events}


@router.get("/appointments")
def appointments(factory: str = "ALL", date: str | None = None, days: int = 2, db: Session = Depends(get_db),
                 user=Depends(current_user)):
    c = Ctx(db, factory, date)
    L = lookup(db)
    start = datetime.combine(c.day, datetime.min.time())
    rows = list(db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(c.fids), m.TruckAppointment.scheduled_arrival >= start,
        m.TruckAppointment.scheduled_arrival < start + timedelta(days=days)).order_by(m.TruckAppointment.scheduled_arrival)))
    orders_ = [t.order for t in rows if t.order]
    info = readiness.assess(db, orders_, c.now)
    out = []
    for t in rows:
        d = truck_dict(t, L, c.now)
        if t.order_id in info:
            d["readiness_pct"] = info[t.order_id]["readiness"]["pct"]
            d["readiness_status"] = info[t.order_id]["readiness"]["status"]
            d["at_risk"] = info[t.order_id]["risk"]["at_risk"]
        out.append(d)
    return {"docks": engine.DOCKS, "appointments": out, "start": iso(start), "days": days}


class ApptIn(BaseModel):
    order_id: int
    scheduled_arrival: datetime
    dock: str
    expected_loading_min: int = 75
    carrier: str | None = None


def _conflict(db: Session, factory_id: int, dock: str, start: datetime, minutes: int, exclude: int | None = None):
    end = start + timedelta(minutes=minutes)
    for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.factory_id == factory_id, m.TruckAppointment.dock == dock,
                                                         m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading", "Loaded")))):
        if t.id == exclude:
            continue
        ts = t.loading_start or t.scheduled_arrival
        te = ts + timedelta(minutes=t.expected_loading_min or 60)
        if ts < end and start < te:
            return t
    return None


@router.post("/appointments")
def create_appt(body: ApptIn, db: Session = Depends(get_db), user: m.User = Depends(require("dispatch.write"))):
    with WRITE_LOCK:
        o = get_or_404(db, m.CustomerOrder, body.order_id)
        if body.dock not in engine.DOCKS:
            raise HTTPException(400, f"Dock must be one of {', '.join(engine.DOCKS)}")
        clash = _conflict(db, o.factory_id, body.dock, body.scheduled_arrival, body.expected_loading_min)
        if clash:
            raise HTTPException(409, f"Dock {body.dock} is already assigned to {clash.ref} at {clash.scheduled_arrival:%a %H:%M}")
        t = m.TruckAppointment(ref=next_number(db, m.TruckAppointment, "TRK-", 7000), carrier=body.carrier or "Customer pickup",
                               factory_id=o.factory_id, customer_id=o.customer_id, order_id=o.id, dock=body.dock,
                               scheduled_arrival=body.scheduled_arrival, expected_loading_min=body.expected_loading_min, status="Scheduled")
        db.add(t)
        db.flush()
        log_event(db, Ctx(db, None).now, o.factory_id, "dispatch", "appointment", f"{t.ref} booked for {o.number} at {body.dock} {body.scheduled_arrival:%a %H:%M}",
                  "order", o.id, user.username)
        commit_change(db, "dispatch")
        return truck_dict(t, lookup(db), Ctx(db, None).now)


class ApptUpdate(BaseModel):
    scheduled_arrival: datetime | None = None
    dock: str | None = None
    expected_loading_min: int | None = None


@router.patch("/appointments/{tid}")
def update_appt(tid: int, body: ApptUpdate, db: Session = Depends(get_db), user: m.User = Depends(require("dispatch.write"))):
    with WRITE_LOCK:
        t = get_or_404(db, m.TruckAppointment, tid)
        res = reschedule(db, t, body.scheduled_arrival, body.dock, body.expected_loading_min, user.username)
        commit_change(db, "dispatch")
        return res


def reschedule(db: Session, t: m.TruckAppointment, when: datetime | None, dock: str | None, minutes: int | None, user: str):
    if t.status not in ("Scheduled",):
        raise HTTPException(400, f"{t.ref} is {t.status}; only scheduled appointments can be changed")
    when = when or t.scheduled_arrival
    minutes = minutes or t.expected_loading_min
    dock = dock or t.dock
    if dock not in engine.DOCKS:
        raise HTTPException(400, "Unknown dock")
    clash = _conflict(db, t.factory_id, dock, when, minutes, exclude=t.id)
    if clash:
        free = engine.free_dock(db, t.factory_id, when, minutes, exclude_id=t.id)
        if not free:
            raise HTTPException(409, f"Dock {dock} conflicts with {clash.ref} and no other dock is free")
        dock = free
    old = t.scheduled_arrival
    t.scheduled_arrival, t.dock, t.expected_loading_min = when, dock, minutes
    t.order.scheduled_dispatch = when
    log_event(db, when, t.factory_id, "dispatch", "rescheduled",
              f"{t.ref} moved from {old:%a %H:%M} to {when:%a %H:%M} at {dock}", "order", t.order_id, user)
    return truck_dict(t, lookup(db), Ctx(db, None).now)


class LoadIn(BaseModel):
    loaded_qty: int | None = None


@router.post("/appointments/{tid}/{action}")
def appt_action(tid: int, action: str, body: LoadIn | None = None, db: Session = Depends(get_db),
                user: m.User = Depends(require("dispatch.write"))):
    with WRITE_LOCK:
        t = get_or_404(db, m.TruckAppointment, tid)
        now = Ctx(db, None).now
        o = t.order
        if action == "arrive":
            if t.status != "Scheduled":
                raise HTTPException(400, f"{t.ref} is {t.status}")
            t.status, t.actual_arrival = "Arrived", now
            log_event(db, now, t.factory_id, "dispatch", "truck_arrived", f"{t.ref} checked in at {t.dock}", "truck", t.id, user.username)
        elif action == "start-loading":
            if t.status != "Arrived":
                raise HTTPException(400, "Truck must be checked in before loading")
            if o.picked_qty <= 0:
                raise HTTPException(400, f"{o.number} has nothing picked yet")
            engine.start_loading(t, now)
            log_event(db, now, t.factory_id, "dispatch", "loading", f"Loading {o.number} onto {t.ref}", "order", o.id, user.username)
        elif action == "confirm-loaded":
            if t.status != "Loading":
                raise HTTPException(400, "Truck is not loading")
            qty = (body.loaded_qty if body and body.loaded_qty is not None else o.picked_qty)
            if qty > o.picked_qty:
                raise HTTPException(400, f"Loaded quantity cannot exceed picked quantity ({o.picked_qty:,})")
            t.loaded_qty, t.status, t.loading_end = qty, "Loaded", now
        elif action == "depart":
            if t.status not in ("Loading", "Loaded"):
                raise HTTPException(400, "Truck has not been loaded")
            engine.depart(db, t, now, user.username, business_rules(db), t.loaded_qty or o.picked_qty)
        elif action == "cancel":
            t.status = "Cancelled"
        else:
            raise HTTPException(404, "Unknown action")
        commit_change(db, "dispatch")
        return truck_dict(t, lookup(db), now)


@router.post("/orders/{order_id}/deliver")
def deliver(order_id: int, db: Session = Depends(get_db), user: m.User = Depends(require("dispatch.write"))):
    with WRITE_LOCK:
        o = get_or_404(db, m.CustomerOrder, order_id)
        if o.status != "Dispatched":
            raise HTTPException(400, f"{o.number} is {o.status}")
        now = Ctx(db, None).now
        o.status, o.delivered_at = "Delivered", now
        log_event(db, now, o.factory_id, "dispatch", "delivered", f"Delivery of {o.number} confirmed", "order", o.id, user.username)
        commit_change(db, "dispatch")
        return {"ok": True}


@router.get("/risk")
def risk(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    orders_ = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(c.fids), m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders_, c.now)
    out = []
    for o in orders_:
        if info[o.id]["risk"]["at_risk"] or info[o.id]["risk"]["level"] == "Low":
            row = _order_row(o, info[o.id], L, c.now)
            row["recommendations"] = readiness.recommendations(o, info[o.id], c.now)
            out.append(row)
    return sorted(out, key=lambda r: (r["slack_min"] if r["slack_min"] is not None else 1e9))


class RiskAction(BaseModel):
    action: str
    new_time: datetime | None = None


@router.post("/risk/{order_id}/action")
def risk_action(order_id: int, body: RiskAction, db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    from ..auth import has_perm
    with WRITE_LOCK:
        o = get_or_404(db, m.CustomerOrder, order_id)
        now = Ctx(db, None).now
        info = readiness.assess(db, [o], now)[o.id]
        open_pos = [p for p in info["pos"] if p.completed_qty < p.required_qty]
        need = {"prioritize": "production.write", "reallocate": "production.write", "expedite_picking": "warehouse.write",
                "reschedule": "dispatch.write"}.get(body.action)
        if not need:
            raise HTTPException(400, "Unknown action")
        if not has_perm(user, need):
            raise HTTPException(403, "Your role cannot perform this action")
        if body.action in ("prioritize", "reallocate"):
            if not open_pos:
                raise HTTPException(400, "Production is already complete")
            (prioritize_po if body.action == "prioritize" else reallocate_po)(db, open_pos[0], now, user.username)
        elif body.action == "expedite_picking":
            expedite_order(db, o, now, user.username)
        elif body.action == "reschedule":
            t = info["truck"]
            if not t:
                raise HTTPException(400, "No truck appointment")
            when = body.new_time or ((info["ready_eta"] or now) + timedelta(minutes=30))
            reschedule(db, t, when, None, None, user.username)
        commit_change(db, "dispatch")
        new = readiness.assess(db, [o], now)[o.id]
        return {"ok": True, "message": f"{body.action.replace('_', ' ').title()} applied to {o.number}",
                "ready_eta": iso(new["ready_eta"]), "risk": new["risk"]}


@router.get("/detention")
def detention(factory: str = "ALL", days: int = 7, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    det = readiness.detention_summary(db, c.fids, c.now, days)
    L = lookup(db)
    trucks = list(db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(c.fids), m.TruckAppointment.actual_arrival >= c.now - timedelta(days=days))
        .order_by(m.TruckAppointment.actual_arrival.desc())))
    det["trucks"] = [truck_dict(t, L, c.now) for t in trucks]
    return det
