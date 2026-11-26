"""Production & Manufacturing (incl. machine health, workforce, changeovers, optimization)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, require
from ..db import WRITE_LOCK, get_db
from ..services import calendar as cal
from ..services import machine_health as mh
from ..services import metrics, planning, readiness, workforce
from ..services.common import business_rules, iso, log_event, next_number
from .core import commit_change
from .serializers import Ctx, get_or_404, lookup, prod_order_dict

router = APIRouter(prefix="/api/production")


def _last_hour(db, fids, day, now, rates: dict, detail: dict) -> tuple[int, dict]:
    """Output of the last completed clock hour per line and that hour's plan (the hourly record's plan for the
    productive minutes in the hour, less any "No orders" idle), so a break hour is not compared to a full hour."""
    last_h = now.hour - 1
    agg = defaultdict(lambda: {"actual": 0.0, "planned": 0.0})
    for r in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.factory_id.in_(fids),
                                                         m.ProductionHourly.date == day, m.ProductionHourly.hour == last_h)):
        x = agg[r.line_id]
        x["actual"] += r.actual
        x["planned"] += r.planned
    for (lid, d, h, _sh), mins in detail["no_orders_by_hour"].items():
        if d == day and h == last_h and lid in agg:
            agg[lid]["planned"] = max(0.0, agg[lid]["planned"] - rates.get(lid, 0) * mins / 60)
    return last_h, agg


@router.get("/summary")
def summary(factory: str = "ALL", date: str | None = None, shift: int | None = None, db: Session = Depends(get_db),
            user=Depends(current_user)):
    c = Ctx(db, factory, date, shift)
    a, b = metrics.scope_window(c.day, c.shift, c.now)
    dd = metrics.downtime_detail(db, c.fids, a, b, c.now)
    p = metrics.production(db, c.fids, c.day, c.shift, c.now, detail=dd)
    from ..services import quality
    q = quality.quality_counts(db, c.fids, a, metrics.quality_until(b, c.now))
    pipe = metrics.pipeline(db, c.fids, c.day, c.now)
    lines = p["lines"]
    open_pos = db.scalar(select(func.count()).select_from(m.ProductionOrder).where(
        m.ProductionOrder.factory_id.in_(c.fids), m.ProductionOrder.status.in_(planning.OPEN_PO))) or 0
    delayed = db.scalar(select(func.count()).select_from(m.ProductionOrder).where(
        m.ProductionOrder.factory_id.in_(c.fids), m.ProductionOrder.status == "Delayed")) or 0
    return {"sim_time": iso(c.now), "production": {k: p[k] for k in ("actual", "target", "planned_to_date", "achievement_pct",
                                                                    "efficiency_pct", "on_track", "started")},
            "oee": metrics.oee(p, q), "downtime": dd["by_category"], "downtime_total": round(dd["total_min"]),
            "idle_no_orders_min": round(dd["no_orders_min"]),
            "lines_running": sum(1 for ln in lines if ln.status == "Running"), "lines_total": len(lines),
            "open_orders": open_pos, "delayed_orders": delayed, "pipeline": pipe}


@router.get("/orders")
def orders(factory: str = "ALL", status: str | None = None, view: str = "open", db: Session = Depends(get_db),
           user=Depends(current_user)):
    c = Ctx(db, factory)
    q = select(m.ProductionOrder).where(m.ProductionOrder.factory_id.in_(c.fids))
    if status:
        q = q.where(m.ProductionOrder.status == status)
    elif view == "open":
        q = q.where((m.ProductionOrder.status != "Completed") | (m.ProductionOrder.completed_at >= c.now - timedelta(days=1)))
    L = lookup(db)
    pos = list(db.scalars(q))
    orders_ = {o.id: o for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.id.in_([p.customer_order_id for p in pos] or [0])))}
    rank = {"In Progress": 0, "Delayed": 0, "Ready": 1, "Planned": 2, "Completed": 3}
    rows = [prod_order_dict(p, L, orders_) for p in pos]
    return sorted(rows, key=lambda r: (rank.get(r["status"], 4), r["required_completion"] or ""))


@router.get("/orders/{po_id}")
def order_detail(po_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    po = get_or_404(db, m.ProductionOrder, po_id)
    L = lookup(db)
    now = Ctx(db, None).now
    o = db.get(m.CustomerOrder, po.customer_order_id) if po.customer_order_id else None
    sku = L["skus"][po.sku_id]
    rem = po.required_qty - po.completed_qty
    bom = []
    for mid, per, label in ((sku.fill_material_id, sku.fill_kg, "Fill"), (sku.shell_material_id, 1, "Shell"),
                            (sku.bag_material_id, 1, "Polybag"), (sku.carton_material_id, 1 / sku.pillows_per_carton, "Carton"),
                            (sku.label_material_id, 1 / sku.pillows_per_carton, "Label")):
        st = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == po.factory_id, m.MaterialStock.material_id == mid))
        need = rem * per
        mat = L["materials"][mid]
        bom.append({"material_id": mid, "code": mat.code, "name": mat.name, "role": label, "uom": mat.uom,
                    "per_pillow": round(per, 4), "required": round(need, 1), "available": round(st.on_hand, 1) if st else 0,
                    "sufficient": bool(st and st.on_hand >= need)})
    cartons = db.execute(select(m.Carton.status, func.count()).where(m.Carton.po_id == po.id).group_by(m.Carton.status)).all()
    info = readiness.assess(db, [o], now)[o.id] if o else None
    events = [{"ts": iso(e.ts), "message": e.message, "stage": e.stage}
              for e in db.scalars(select(m.EventLog).where(m.EventLog.entity_type == "production_order", m.EventLog.entity_id == po.id)
                                  .order_by(m.EventLog.ts.desc()).limit(20))]
    queue = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.line_id == po.line_id,
                                                            m.ProductionOrder.status.in_(planning.OPEN_PO))
                            .order_by(m.ProductionOrder.sequence, m.ProductionOrder.id)))
    return {
        "po": prod_order_dict(po, L, {o.id: o} if o else {}),
        "sku": {"code": sku.code, "description": sku.description, "pillows_per_carton": sku.pillows_per_carton,
                "fill_kg": sku.fill_kg, "packaging_type": sku.packaging_type, "carton_type": sku.carton_type},
        "customer_order": {"id": o.id, "number": o.number, "customer": o.customer.name, "qty": o.qty,
                           "scheduled_dispatch": iso(o.scheduled_dispatch), "status": o.status,
                           "readiness": info["readiness"] if info else None,
                           "risk": info["risk"] if info else None} if o else None,
        "progress": [
            {"stage": "Produced (filled & sewn)", "qty": po.completed_qty},
            {"stage": "At packing station", "qty": po.pack_queue},
            {"stage": "Blocked at inspection", "qty": po.held_qty},
            {"stage": "Approved cartons (pillows)", "qty": po.packed_qty},
            {"stage": "Stored in finished goods", "qty": po.stored_qty},
        ],
        "cartons": {s: n for s, n in cartons}, "materials": bom, "events": events,
        "queue_position": next((i + 1 for i, q in enumerate(queue) if q.id == po.id), None), "queue_length": len(queue),
    }


@router.post("/orders/{po_id}/prioritize")
def prioritize(po_id: int, db: Session = Depends(get_db), user: m.User = Depends(require("production.write"))):
    with WRITE_LOCK:
        po = get_or_404(db, m.ProductionOrder, po_id)
        prioritize_po(db, po, Ctx(db, None).now, user.username)
        commit_change(db, "production")
        return prod_order_dict(po, lookup(db))


def prioritize_po(db: Session, po: m.ProductionOrder, now: datetime, user: str):
    if po.status not in planning.OPEN_PO:
        raise HTTPException(400, f"{po.number} is {po.status}")
    line = db.get(m.ProductionLine, po.line_id)
    queue = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.line_id == po.line_id,
                                                            m.ProductionOrder.status.in_(planning.OPEN_PO))
                            .order_by(m.ProductionOrder.sequence, m.ProductionOrder.id)))
    cur = db.get(m.ProductionOrder, line.current_po_id) if line and line.current_po_id else None
    ordered = [po] + [q for q in queue if q.id != po.id]
    for i, q in enumerate(ordered):
        q.sequence = i
    po.priority = "High"
    if cur and cur.id != po.id and line.status in ("Running", "Idle"):
        line.current_po_id = None  # the engine switches at the next tick (with changeover if SKU differs)
        if line.status == "Running":
            line.status, line.status_reason = "Idle", f"Switching to prioritized {po.number}"
    log_event(db, now, po.factory_id, "production", "prioritized", f"{po.number} moved to the front of {line.name if line else '—'}",
              "production_order", po.id, user)


class ReassignIn(BaseModel):
    line_id: int | None = None


@router.post("/orders/{po_id}/reallocate")
def reallocate(po_id: int, body: ReassignIn, db: Session = Depends(get_db), user: m.User = Depends(require("production.write"))):
    with WRITE_LOCK:
        po = get_or_404(db, m.ProductionOrder, po_id)
        reallocate_po(db, po, Ctx(db, None).now, user.username, body.line_id)
        commit_change(db, "production")
        return prod_order_dict(po, lookup(db))


def reallocate_po(db: Session, po: m.ProductionOrder, now: datetime, user: str, line_id: int | None = None):
    """Move the order's remaining quantity to the line that can finish it earliest."""
    if po.status not in planning.OPEN_PO:
        raise HTTPException(400, f"{po.number} is {po.status}")
    ctx = planning.PlanCtx(db, [po.factory_id], now)
    old = db.get(m.ProductionLine, po.line_id) if po.line_id else None
    best, best_end = None, None
    for ln in ctx.lines:
        if line_id and ln.id != line_id:
            continue
        q = [x for x in ctx.current_queues()[ln.id] if x.id != po.id]
        if ln.id != po.line_id:
            head = [x for x in q if x.id == ln.current_po_id]
            q = head + [po] + [x for x in q if x.id != ln.current_po_id]
        else:
            q = [po] + q
        res, _ = planning.evaluate_line(ctx, ln, q)
        end = res[po.id]["end"]
        if best_end is None or end < best_end:
            best, best_end = ln, end
    if best is None:
        raise HTTPException(400, "No line available")
    if old and old.current_po_id == po.id and best.id != old.id:
        if po.completed_qty > 0:  # split: finished part stays, remainder moves
            rest = po.required_qty - po.completed_qty
            po.required_qty = po.completed_qty
            po.status = "Completed"
            po.completed_at = now
            new = m.ProductionOrder(number=next_number(db, m.ProductionOrder, "PRD-", 24000), customer_order_id=po.customer_order_id,
                                    factory_id=po.factory_id, sku_id=po.sku_id, line_id=best.id, sequence=-1, required_qty=rest,
                                    start_date=now, required_completion=po.required_completion, priority="High", status="Ready")
            db.add(new)
            db.flush()
            log_event(db, now, po.factory_id, "production", "reallocated",
                      f"{po.number} split: {rest:,} pillows moved from {old.name} to {best.name} as {new.number}",
                      "production_order", new.id, user)
            old.current_po_id = None
            prioritize_po(db, new, now, user)
            return
        old.current_po_id = None
    po.line_id = best.id
    po.sequence = -1
    prioritize_po(db, po, now, user)
    log_event(db, now, po.factory_id, "production", "reallocated",
              f"{po.number} reallocated to {best.name} (est. finish {best_end:%a %H:%M})", "production_order", po.id, user)


@router.get("/lines")
def lines(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    day = c.now.date()
    p = metrics.production(db, c.fids, day, None, c.now)  # same per-line plan as the KPIs (no-orders idle excluded)
    last_h, last = _last_hour(db, c.fids, day, c.now, {ln.id: ln.planned_rate for ln in p["lines"]}, p["downtime"])
    pipe = metrics.pipeline(db, c.fids, day, c.now)["per_line"]
    out = []
    for ln in sorted(p["lines"], key=lambda x: x.id):
        x = p["by_line"][ln.id]
        lh = last.get(ln.id)
        po = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
        sku = L["skus"].get(ln.current_sku_id)
        out.append({"id": ln.id, "code": ln.code, "name": ln.name, "factory_id": ln.factory_id, "factory": L["factories"][ln.factory_id].code,
                    "line_type": ln.line_type, "status": ln.status, "status_reason": ln.status_reason,
                    "customer": L["customers"][sku.customer_id].name if sku else None, "sku": sku.code if sku else None,
                    "po": po.number if po else None, "po_id": po.id if po else None,
                    "target_today": round(x["target"]), "planned_to_date": round(x["planned"]),
                    "actual": int(x["actual"]), "rated_per_hour": ln.planned_rate,
                    "last_hour": f"{last_h:02d}:00" if lh else None,
                    "output_per_hour": int(lh["actual"]) if lh else None,
                    "planned_last_hour": round(lh["planned"]) if lh else None,
                    "efficiency_pct": x["efficiency_pct"], "downtime_min": round(x["down"]),
                    "idle_no_orders_min": round(x["no_orders"]),
                    "active_shift": cal.shift_of(c.now), "constraint": pipe.get(ln.id, {}).get("binding"),
                    "constraint_loss_pct": pipe.get(ln.id, {}).get("loss_pct"),
                    "changeover_until": iso(ln.changeover_until)})
    return out


@router.get("/lines/{line_id}")
def line_detail(line_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    ln = get_or_404(db, m.ProductionLine, line_id)
    c = Ctx(db, None)
    L = lookup(db)
    day = c.now.date()
    a, b = metrics.scope_window(day, None, c.now)
    dd = metrics.downtime_detail(db, [ln.factory_id], a, b, c.now)
    shift = metrics.current_shift(day, c.now) or 1
    hours = defaultdict(lambda: {"planned": 0.0, "actual": 0.0, "downtime": 0.0})
    shift_output = 0.0
    for r in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.line_id == ln.id, m.ProductionHourly.date == day)):
        h = hours[r.hour]
        h["planned"] += r.planned
        h["actual"] += r.actual
        h["downtime"] += r.downtime_min
        if r.shift == shift:
            shift_output += r.actual
    for (lid, d, hr, _sh), mins in dd["no_orders_by_hour"].items():  # no-orders idle is neither plan nor downtime
        if lid == ln.id and d == day and hr in hours:
            hours[hr]["planned"] = max(0.0, hours[hr]["planned"] - ln.planned_rate * mins / 60)
            hours[hr]["downtime"] = max(0.0, hours[hr]["downtime"] - mins)
    yday = defaultdict(float)
    for r in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.line_id == ln.id,
                                                         m.ProductionHourly.date == c.now.date() - timedelta(days=1))):
        yday[r.hour] += r.actual
    machines = [_machine_row(mc, L) for mc in db.scalars(select(m.Machine).where(m.Machine.line_id == ln.id))]
    # current-shift crew cost against current-shift output only
    wf = workforce.summary(db, [ln.factory_id], day, c.now, shift_output, line_id=ln.id, shift=shift)
    queue_pos = list(db.scalars(select(m.ProductionOrder).where(
        m.ProductionOrder.line_id == ln.id, m.ProductionOrder.status.in_(planning.OPEN_PO)).order_by(m.ProductionOrder.sequence)))
    qorders = {o.id: o for o in db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.id.in_([x.customer_order_id for x in queue_pos] or [0])))}
    queue = [prod_order_dict(x, L, qorders) for x in queue_pos]
    downtime = []  # events overlapping today (incl. ones that started earlier or are still open), minutes inside today
    if b > a:
        for d in db.scalars(select(m.DowntimeEvent).where(
                m.DowntimeEvent.line_id == ln.id, m.DowntimeEvent.start < b,
                (m.DowntimeEvent.end.is_(None)) | (m.DowntimeEvent.end > a)).order_by(m.DowntimeEvent.start.desc()).limit(25)):
            mins = cal.productive_minutes(max(d.start, a), min(d.end or c.now, b))
            if mins <= 0:
                continue
            starved = metrics.is_no_orders(d.category, d.reason)
            downtime.append({"category": metrics.NO_ORDERS_LABEL if starved else d.category, "reason": d.reason,
                             "start": iso(d.start), "end": iso(d.end), "minutes": round(mins, 1),
                             "machine_loss": not starved, "open": d.end is None})
    cos = [_co_row(x, L) for x in db.scalars(select(m.ChangeoverEvent).where(m.ChangeoverEvent.line_id == ln.id)
                                             .order_by(m.ChangeoverEvent.start.desc()).limit(10))]
    return {"date": day.isoformat(), "line": {"id": ln.id, "code": ln.code, "name": ln.name, "status": ln.status, "status_reason": ln.status_reason,
                     "planned_rate": ln.planned_rate, "factory": L["factories"][ln.factory_id].name, "line_type": ln.line_type,
                     "perf_modifier": ln.perf_modifier, "effective_rate": round(planning.PlanCtx(db, [ln.factory_id], c.now).line_rate(ln))},
            "hourly": [{"hour": f"{h:02d}:00", "planned": round(v["planned"]), "actual": round(v["actual"]),
                        "yesterday": round(yday.get(h, 0)), "downtime": round(v["downtime"])} for h, v in sorted(hours.items())],
            "machines": machines, "workforce": {**{k: v for k, v in wf.items() if k != "below_benchmark"},
                                                "shift": shift, "pillows": int(shift_output)},
            "queue": queue, "downtime": downtime, "changeovers": cos}


@router.get("/hourly")
def hourly(factory: str = "ALL", date: str | None = None, shift: int | None = None, db: Session = Depends(get_db),
           user=Depends(current_user)):
    c = Ctx(db, factory, date, shift)
    # Same per-line plan to date as the KPIs: rate x planned productive minutes elapsed (the hour in progress
    # counts only its elapsed productive minutes; "No orders" idle is not planned time).
    p = metrics.production(db, c.fids, c.day, c.shift, c.now)
    L = lookup(db)
    table = []
    for ln in (p["lines"] if p["started"] else []):
        x = p["by_line"][ln.id]
        table.append({"line_id": ln.id, "line": ln.name, "factory": L["factories"][ln.factory_id].code,
                      "actual": round(x["actual"]), "target": round(x["planned"]), "efficiency_pct": x["efficiency_pct"],
                      "downtime_min": round(x["down"]), "idle_no_orders_min": round(x["no_orders"]),
                      "underperforming": x["planned"] > 50 and x["actual"] < 0.88 * x["planned"]})
    table.sort(key=lambda r: (r["efficiency_pct"] is None, r["efficiency_pct"] or 0))
    dd = p["downtime"]
    return {"chart": metrics.hourly(db, c.fids, c.day, c.shift, c.now), "lines": table,
            "totals": {"actual": p["actual"], "planned_to_date": p["planned_to_date"], "efficiency_pct": p["efficiency_pct"]},
            "downtime": dd["by_category"], "downtime_total": round(dd["total_min"]),
            "idle_no_orders_min": round(dd["no_orders_min"])}


def _machine_row(mc: m.Machine, L) -> dict:
    return {"id": mc.id, "code": mc.code, "name": mc.name, "type": mc.machine_type, "stage": mc.stage, "line_id": mc.line_id,
            "line": L["lines"][mc.line_id].name if mc.line_id else None, "factory": L["factories"][mc.factory_id].code,
            "status": mc.status, "health_score": round(mc.health_score or 0, 1), "health": mh.classify(mc.health_score or 0),
            "rpm": mc.rpm, "temperature": mc.temperature, "vibration": mc.vibration, "pressure": mc.pressure, "power": mc.power,
            "baseline": mc.baseline, "operating_hours": round(mc.operating_hours or 0), "hours_since_maintenance": round(mc.hours_since_maintenance or 0),
            "unavailable_until": iso(mc.unavailable_until)}


@router.get("/machines")
def machines(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    rows = [_machine_row(mc, L) for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(c.fids)))]
    return sorted(rows, key=lambda r: (0 if r["status"] in ("Breakdown", "Maintenance") else 1, r["health_score"]))


@router.get("/machines/{machine_id}")
def machine_detail(machine_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    mc = get_or_404(db, m.Machine, machine_id)
    c = Ctx(db, None)
    L = lookup(db)
    hist = mh.history(db, mc.id, 144)
    comp = mh.score_components(mc, hist)
    wos = [_wo_row(w, L) for w in db.scalars(select(m.WorkOrder).where(m.WorkOrder.machine_id == mc.id).order_by(m.WorkOrder.created_at.desc()))]
    return {"machine": _machine_row(mc, L), "units": mh.UNITS,
            "telemetry": [{"ts": iso(h.ts), "rpm": h.rpm, "temperature": h.temperature, "vibration": h.vibration,
                           "pressure": h.pressure, "power": h.power} for h in hist],
            "health_components": comp, "anomalies": mh.anomalies(mc, hist), "prediction": mh.prediction(mc, hist, c.now),
            "thresholds": {"healthy": 85, "warning": 65, "vibration_alarm": round(mc.baseline["vibration"] * mh.VIBRATION_ALARM, 2)},
            "work_orders": wos, "disclaimer": mh.DISCLAIMER,
            "maintenance_history": [{"date": iso(mc.last_maintenance_at), "type": "Preventive service", "notes": "Last recorded service"}] + [
                {"date": w["completed_at"], "type": w["type"], "notes": w["description"]} for w in wos if w["status"] == "Completed"]}


def _wo_row(w: m.WorkOrder, L) -> dict:
    mc = w.machine
    return {"id": w.id, "number": w.number, "machine_id": w.machine_id, "machine": mc.code if mc else None,
            "machine_name": mc.name if mc else None, "line": L["lines"][mc.line_id].name if mc and mc.line_id else None,
            "factory": L["factories"][w.factory_id].code, "type": w.wo_type, "status": w.status, "priority": w.priority,
            "description": w.description, "created_at": iso(w.created_at), "acknowledged_by": w.acknowledged_by,
            "scheduled_start": iso(w.scheduled_start), "scheduled_end": iso(w.scheduled_end), "started_at": iso(w.started_at),
            "expected_completion": iso(w.expected_completion), "completed_at": iso(w.completed_at)}


@router.get("/maintenance")
def maintenance(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    recs = []
    open_wo_machines = {w.machine_id for w in db.scalars(select(m.WorkOrder).where(
        m.WorkOrder.factory_id.in_(c.fids), m.WorkOrder.status != "Completed"))}
    for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(c.fids), m.Machine.health_score < 85,
                                                 m.Machine.status == "Running")):
        p = mh.prediction(mc, mh.history(db, mc.id, 144), c.now)
        if p:
            p["line"] = L["lines"][mc.line_id].name
            p["factory"] = L["factories"][mc.factory_id].code
            p["has_work_order"] = mc.id in open_wo_machines
            recs.append(p)
    recs.sort(key=lambda r: r["health_score"])
    wos = [_wo_row(w, L) for w in db.scalars(select(m.WorkOrder).where(m.WorkOrder.factory_id.in_(c.fids))
                                            .order_by(m.WorkOrder.created_at.desc()).limit(30))]
    return {"recommendations": recs, "work_orders": wos, "disclaimer": mh.DISCLAIMER}


class WOIn(BaseModel):
    machine_id: int
    wo_type: str = "Predictive"
    description: str | None = None
    scheduled_start: datetime | None = None
    duration_min: int = 60


@router.post("/maintenance/work-orders")
def create_wo(body: WOIn, db: Session = Depends(get_db), user: m.User = Depends(require("maintenance.write"))):
    with WRITE_LOCK:
        mc = get_or_404(db, m.Machine, body.machine_id)
        now = Ctx(db, None).now
        start = body.scheduled_start or now
        wo = m.WorkOrder(number=next_number(db, m.WorkOrder, "WO-", 3000), machine_id=mc.id, factory_id=mc.factory_id,
                         wo_type=body.wo_type, status="Scheduled", priority="High" if mc.health_score < 65 else "Medium",
                         description=body.description or f"Inspect {mc.name}: health {mc.health_score:.0f}",
                         created_at=now, acknowledged_at=now, acknowledged_by=user.username, scheduled_start=start,
                         scheduled_end=start + timedelta(minutes=body.duration_min))
        db.add(wo)
        db.flush()
        log_event(db, now, mc.factory_id, "production", "work_order",
                  f"{wo.number} created for {mc.code} by {user.username}, window {start:%a %H:%M}", "machine", mc.id, user.username)
        commit_change(db, "maintenance")
        return _wo_row(wo, lookup(db))


@router.post("/maintenance/work-orders/{wo_id}/{action}")
def wo_action(wo_id: int, action: str, db: Session = Depends(get_db), user: m.User = Depends(require("maintenance.write"))):
    with WRITE_LOCK:
        wo = get_or_404(db, m.WorkOrder, wo_id)
        mc = wo.machine
        ln = db.get(m.ProductionLine, mc.line_id)
        now = Ctx(db, None).now
        if action == "start":
            if wo.status in ("Completed", "In Progress"):
                raise HTTPException(400, f"Work order is {wo.status}")
            dur = (wo.scheduled_end - wo.scheduled_start) if wo.scheduled_start and wo.scheduled_end else timedelta(minutes=60)
            wo.status, wo.started_at, wo.expected_completion = "In Progress", now, now + dur
            mc.status, mc.unavailable_until = "Maintenance", wo.expected_completion
            ln.status, ln.status_reason, ln.status_since = "Maintenance", f"{wo.number}: {mc.code} {mc.name}", now
            log_event(db, now, mc.factory_id, "production", "maintenance_started",
                      f"{mc.code} in maintenance until {wo.expected_completion:%H:%M}; {ln.name} availability reduced", "machine", mc.id, user.username)
        elif action == "complete":
            if wo.status == "Completed":
                raise HTTPException(400, "Already completed")
            wo.status, wo.completed_at = "Completed", now
            mc.status, mc.unavailable_until = "Running", None
            mc.hours_since_maintenance, mc.degradation, mc.health_score, mc.last_maintenance_at = 0, 0.0002, 96.0, now
            if not any(x.status in ("Breakdown", "Maintenance") for x in db.scalars(select(m.Machine).where(m.Machine.line_id == ln.id))):
                if ln.status in ("Breakdown", "Maintenance"):
                    ln.status, ln.status_reason, ln.status_since = ("Running" if ln.current_po_id else "Idle"), None, now
                    for ev in db.scalars(select(m.DowntimeEvent).where(m.DowntimeEvent.line_id == ln.id, m.DowntimeEvent.end.is_(None))):
                        ev.end = now
            log_event(db, now, mc.factory_id, "production", "maintenance_completed", f"{wo.number} completed; {mc.code} restored",
                      "machine", mc.id, user.username)
        elif action == "acknowledge":
            wo.status, wo.acknowledged_at, wo.acknowledged_by = "Acknowledged", now, user.username
        else:
            raise HTTPException(404, "Unknown action")
        commit_change(db, "maintenance")
        return _wo_row(wo, lookup(db))


@router.get("/workforce")
def workforce_view(factory: str = "ALL", line_id: int | None = None, shift: int | None = None, date: str | None = None,
                   db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory, date, shift)
    if line_id:
        q = select(func.sum(m.ProductionHourly.actual)).where(m.ProductionHourly.line_id == line_id, m.ProductionHourly.date == c.day)
    else:
        q = select(func.sum(m.ProductionHourly.actual)).where(m.ProductionHourly.factory_id.in_(c.fids), m.ProductionHourly.date == c.day)
    if c.shift:
        q = q.where(m.ProductionHourly.shift == c.shift)
    pillows = float(db.scalar(q) or 0)
    wf = workforce.summary(db, c.fids, c.day, c.now, pillows, line_id=line_id, shift=c.shift)
    crew = sum(workforce.LINE_ROLE_REQUIREMENT.values())
    by_line = defaultdict(lambda: {"deployed": 0, "units": 0, "idle_min": 0, "cost": 0.0})
    L = lookup(db)
    for r in wf["employees"]:
        if r["line_id"] and r["attendance"] in ("Present", "Late") and r["role"] in workforce.LINE_ROLE_REQUIREMENT:
            b = by_line[r["line_id"]]
            b["deployed"] += 1
            b["idle_min"] += r["idle_min"]
            b["cost"] += r["labour_cost"]
    line_out = defaultdict(float)
    q2 = select(m.ProductionHourly.line_id, func.sum(m.ProductionHourly.actual)).where(
        m.ProductionHourly.factory_id.in_(c.fids), m.ProductionHourly.date == c.day)
    if c.shift:
        q2 = q2.where(m.ProductionHourly.shift == c.shift)
    for lid, a in db.execute(q2.group_by(m.ProductionHourly.line_id)):
        line_out[lid] = float(a or 0)
    shifts = 1 if c.shift else max(1, sum(1 for sh in (1, 2) if cal.shift_bounds(c.day, sh)[0] <= c.now))
    lines_tbl = [{"line_id": lid, "line": L["lines"][lid].name, "factory": L["factories"][L["lines"][lid].factory_id].code,
                  "deployed": b["deployed"], "required": crew * shifts, "pillows": int(line_out[lid]),
                  "output_per_worker": round(line_out[lid] / b["deployed"], 1) if b["deployed"] else 0,
                  "idle_hours": round(b["idle_min"] / 60, 1), "labour_cost": round(b["cost"], 2),
                  "cost_per_pillow": round(b["cost"] / line_out[lid], 3) if line_out[lid] else None}
                 for lid, b in by_line.items()]
    lines_tbl.sort(key=lambda r: -r["idle_hours"])
    wf["lines"] = lines_tbl
    wf["pillows"] = int(pillows)
    return wf


def _co_row(x: m.ChangeoverEvent, L) -> dict:
    return {"id": x.id, "line_id": x.line_id, "line": L["lines"][x.line_id].name, "factory": L["factories"][x.factory_id].code,
            "prev_sku": L["skus"][x.prev_sku_id].code if x.prev_sku_id else None, "new_sku": L["skus"][x.new_sku_id].code,
            "customer": L["customers"][x.customer_id].name if x.customer_id else None, "start": iso(x.start), "end": iso(x.end),
            "duration_min": x.duration_min, "standard_min": x.standard_min, "lost_min": x.lost_min, "flagged": x.flagged,
            "in_progress": x.end is None}


@router.get("/changeovers")
def changeovers(factory: str = "ALL", days: int = 2, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    since = datetime.combine(c.now.date() - timedelta(days=days - 1), datetime.min.time())
    rows = [_co_row(x, L) for x in db.scalars(select(m.ChangeoverEvent).where(
        m.ChangeoverEvent.factory_id.in_(c.fids), m.ChangeoverEvent.start >= since).order_by(m.ChangeoverEvent.start.desc()))]
    top = defaultdict(lambda: {"lost": 0.0, "count": 0, "flagged": 0})
    for r in rows:
        t = top[(r["line_id"], r["line"], r["factory"])]
        t["lost"] += r["lost_min"] or 0
        t["count"] += 1
        t["flagged"] += 1 if r["flagged"] else 0
    tops = sorted([{"line_id": k[0], "line": k[1], "factory": k[2], "lost_min": round(v["lost"], 1), "changeovers": v["count"],
                    "flagged": v["flagged"]} for k, v in top.items()], key=lambda r: -r["lost_min"])
    return {"standard_min": business_rules(db)["changeover_standard_min"], "events": rows, "top_lines": tops[:8],
            "flagged": sum(1 for r in rows if r["flagged"]), "total_lost_min": round(sum(r["lost_min"] or 0 for r in rows), 1)}


class OptimizeIn(BaseModel):
    factory_id: int


@router.post("/optimize")
def optimize(body: OptimizeIn, db: Session = Depends(get_db), user: m.User = Depends(require("optimize"))):
    with WRITE_LOCK:
        now = Ctx(db, None).now
        run = planning.optimize(db, body.factory_id, now, user.username)
        db.commit()
        return _run_dict(run)


def _run_dict(run: m.OptimizationRun) -> dict:
    return {"id": run.id, "factory_id": run.factory_id, "status": run.status, "created_at": iso(run.created_at),
            "before": run.before, "after": {k: v for k, v in run.after.items() if k != "assignment"}, "metrics": run.metrics}


@router.get("/optimize/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return _run_dict(get_or_404(db, m.OptimizationRun, run_id))


@router.post("/optimize/{run_id}/{decision}")
def decide(run_id: int, decision: str, db: Session = Depends(get_db), user: m.User = Depends(require("optimize"))):
    with WRITE_LOCK:
        run = get_or_404(db, m.OptimizationRun, run_id)
        if run.status != "Proposed":
            raise HTTPException(400, f"Run already {run.status}")
        now = Ctx(db, None).now
        if decision == "accept":
            planning.apply_run(db, run, now)
            log_event(db, now, run.factory_id, "production", "plan_accepted",
                      f"Optimized plan accepted: delay reduction {run.metrics['delay_reduction_h']} h, "
                      f"{run.metrics['changeovers_saved']} changeovers saved", "optimization", run.id, user.username)
        elif decision == "reject":
            run.status = "Rejected"
        else:
            raise HTTPException(404, "Unknown decision")
        commit_change(db, "production")
        return _run_dict(run)
