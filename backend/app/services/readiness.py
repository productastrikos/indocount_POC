"""Shipment readiness, shipment risk and detention costs.

An order is ready only when production is complete, cartons have passed
quality, finished goods are put away, the quantity is allocated and picking
is complete. Risk compares the projected ready time with the truck
appointment; detention uses the configurable illustrative charge."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from .common import business_rules, iso, pct
from .planning import POST_PRODUCTION_LAG_MIN

STAGES = [
    ("production", "Production complete"),
    ("quality", "Passed quality inspection"),
    ("inventory", "Received into finished goods"),
    ("allocation", "Allocated to order"),
    ("picking", "Picking complete"),
]
PENDING_STATUS = {"production": "Production Pending", "quality": "Quality Pending",
                  "inventory": "Inventory Pending", "allocation": "Inventory Pending",
                  "picking": "Picking Pending"}
ACTIVE_TRUCK = ("Scheduled", "Arrived", "Loading", "Loaded")


def order_pos(db: Session, order_ids):
    out = defaultdict(list)
    if not order_ids:
        return out
    for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.customer_order_id.in_(list(order_ids)))):
        out[po.customer_order_id].append(po)
    return out


def readiness(order: m.CustomerOrder, pos: list[m.ProductionOrder]) -> dict:
    q = order.qty or 1
    if order.status in ("Dispatched", "Delivered"):
        ratios = {k: 100.0 for k, _ in STAGES}
        return {"ratios": ratios, "pct": 100.0, "status": order.status, "blocking": None}
    required = sum(p.required_qty for p in pos)
    from_stock = max(0, q - required)  # portion served from existing stock
    alloc = order.allocated_qty / q
    prod = min(1.0, (sum(p.completed_qty for p in pos) + from_stock) / q)
    qual = min(1.0, (sum(p.packed_qty for p in pos) + from_stock) / q)
    inv = min(1.0, (sum(p.stored_qty for p in pos) + from_stock) / q)
    ratios = {
        "production": max(prod, alloc), "quality": max(qual, alloc), "inventory": max(inv, alloc),
        "allocation": min(1.0, alloc), "picking": min(1.0, order.picked_qty / q),
    }
    p = round(sum(ratios.values()) / len(ratios) * 100, 1)
    blocking = next((k for k, _ in STAGES if ratios[k] < 0.999), None)
    if blocking is None:
        status = "Ready for Dispatch"
    elif blocking == "production" and ratios["production"] == 0 and not any(x.started_at for x in pos):
        status = "Not Ready"
    else:
        status = PENDING_STATUS[blocking]
    return {"ratios": {k: round(v * 100, 1) for k, v in ratios.items()}, "pct": p, "status": status, "blocking": blocking}


def ready_eta(order: m.CustomerOrder, pos: list[m.ProductionOrder], factory: m.Factory, now: datetime,
              rd: dict) -> datetime:
    """Projected time the order will be fully picked and ready to load."""
    if rd["blocking"] is None:
        return now
    q = order.qty
    sku_ppc = order.sku.pillows_per_carton
    t = now
    open_pos = [p for p in pos if p.completed_qty < p.required_qty]
    if open_pos:
        ends = [p.estimated_completion or (now + timedelta(days=3)) for p in open_pos]
        t = max(ends)
        t = cal.add_productive(t, POST_PRODUCTION_LAG_MIN)
    elif rd["blocking"] in ("quality", "inventory"):
        t = cal.add_productive(now, POST_PRODUCTION_LAG_MIN * 0.6)
    cartons_to_pick = max(0, q - order.picked_qty) / sku_ppc
    rate = max(40.0, (factory.pick_rate_cartons_hr or 400) * (factory.wh_productivity or 1.0) * (0.5 if not order.expedite_picking else 1.0))
    t = t + timedelta(hours=cartons_to_pick / rate)
    return t


def truck_for_orders(db: Session, order_ids) -> dict[int, m.TruckAppointment]:
    out = {}
    if not order_ids:
        return out
    for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.order_id.in_(list(order_ids)))
                        .order_by(m.TruckAppointment.scheduled_arrival)):
        if t.status in ACTIVE_TRUCK + ("Departed",):
            if t.order_id not in out or out[t.order_id].status == "Departed":
                out[t.order_id] = t
    return out


def assess(db: Session, orders: list[m.CustomerOrder], now: datetime) -> dict[int, dict]:
    """Readiness + risk for many orders at once."""
    rules = business_rules(db)
    ids = [o.id for o in orders]
    pos = order_pos(db, ids)
    trucks = truck_for_orders(db, ids)
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    out = {}
    for o in orders:
        rd = readiness(o, pos[o.id])
        truck = trucks.get(o.id)
        eta = ready_eta(o, pos[o.id], factories[o.factory_id], now, rd) if o.status == "Open" else None
        risk = {"level": "None", "at_risk": False, "slack_min": None, "est_detention": 0.0, "reasons": []}
        if o.status == "Open" and truck and truck.status in ACTIVE_TRUCK:
            arrival = truck.actual_arrival or truck.scheduled_arrival
            slack = (arrival - eta).total_seconds() / 60  # positive = ready before truck
            risk["slack_min"] = round(slack)
            if slack < 0:
                wait = -slack
                risk["at_risk"] = True
                risk["level"] = "High" if wait > rules["detention_free_min"] or truck.actual_arrival else "Medium"
                if wait > rules["detention_free_min"]:
                    risk["est_detention"] = rules["detention_charge"]
            elif slack < 45:
                risk["level"] = "Low"
            if rd["blocking"] == "production":
                late_pos = [p for p in pos[o.id] if p.completed_qty < p.required_qty]
                risk["reasons"].append(
                    f"Production completion est. {min([p.estimated_completion for p in late_pos if p.estimated_completion] or [eta]):%a %H:%M}")
                if any(p.material_risk for p in late_pos):
                    risk["reasons"].append(next(p.risk_reason for p in late_pos if p.material_risk))
            if rd["blocking"] in ("quality", "inventory"):
                risk["reasons"].append("Cartons awaiting inspection / put-away")
            if rd["blocking"] == "allocation":
                risk["reasons"].append("Finished goods not yet allocated")
            if rd["blocking"] == "picking" or (rd["blocking"] and o.picked_qty < o.qty):
                risk["reasons"].append(f"{max(0, o.qty - o.picked_qty):,} pillows still to pick")
        out[o.id] = {"readiness": rd, "ready_eta": eta, "truck": truck, "risk": risk, "pos": pos[o.id]}
    return out


def recommendations(order: m.CustomerOrder, info: dict, now: datetime) -> list[dict]:
    recs = []
    rd, risk, truck = info["readiness"], info["risk"], info["truck"]
    if not risk["at_risk"] and risk["level"] != "Low":
        return recs
    open_pos = [p for p in info["pos"] if p.completed_qty < p.required_qty]
    if open_pos:
        po = open_pos[0]
        recs.append({"action": "prioritize", "label": f"Prioritize {po.number} to the front of its line",
                     "detail": "Moves the production order ahead of other queued orders on the same line."})
        recs.append({"action": "reallocate", "label": f"Reallocate {po.number} to the line with most free capacity",
                     "detail": "Moves remaining quantity to the line that can finish it earliest."})
    if order.picked_qty < order.qty:
        recs.append({"action": "expedite_picking", "label": "Expedite warehouse picking",
                     "detail": "Raises pick priority and assigns the next free picker to this order."})
    if truck and info["ready_eta"]:
        new_time = info["ready_eta"] + timedelta(minutes=30)
        recs.append({"action": "reschedule", "label": f"Reschedule truck {truck.ref} to {new_time:%a %H:%M}",
                     "detail": "Avoids detention by aligning the appointment with projected readiness.",
                     "new_time": iso(new_time)})
    return recs


def detention_summary(db: Session, factory_ids: list[int], now: datetime, days: int = 7) -> dict:
    rules = business_rules(db)
    since = now - timedelta(days=days)
    trucks = list(db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(factory_ids),
        m.TruckAppointment.actual_arrival.is_not(None),
        m.TruckAppointment.actual_arrival >= since)))
    delayed, waits = [], []
    by_customer, by_factory, reasons = defaultdict(float), defaultdict(float), defaultdict(int)
    customers = {c.id: c.name for c in db.scalars(select(m.Customer))}
    factories = {f.id: f.name for f in db.scalars(select(m.Factory))}
    incurred = 0.0
    for t in trucks:
        wait = t.wait_min
        if t.status in ("Arrived",):
            wait = (now - t.actual_arrival).total_seconds() / 60
        waits.append(wait)
        if wait > rules["detention_free_min"]:
            charge = rules["detention_charge"]
            incurred += charge
            delayed.append(t)
            by_customer[customers.get(t.customer_id, "—")] += charge
            by_factory[factories.get(t.factory_id, "—")] += charge
            reasons[t.delay_reason or "Order not ready"] += 1
    # Projected charges for upcoming appointments that are at risk.
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(factory_ids),
                                                           m.CustomerOrder.status == "Open")))
    info = assess(db, orders, now)
    projected, projected_rows = 0.0, []
    for o in orders:
        r = info[o.id]["risk"]
        t = info[o.id]["truck"]
        if r["est_detention"] and t and t.status == "Scheduled":
            projected += r["est_detention"]
            projected_rows.append({"order": o.number, "truck": t.ref, "customer": customers.get(o.customer_id),
                                   "arrival": iso(t.scheduled_arrival), "projected_wait_min": -r["slack_min"],
                                   "charge": r["est_detention"]})
    return {
        "charge_per_truck": rules["detention_charge"], "free_minutes": rules["detention_free_min"],
        "period_days": days, "trucks_arrived": len(trucks),
        "delayed_trucks": len(delayed),
        "avg_wait_min": round(sum(waits) / len(waits), 1) if waits else 0.0,
        "incurred_estimate": incurred, "projected_estimate": projected,
        "by_customer": [{"name": k, "value": v} for k, v in sorted(by_customer.items(), key=lambda x: -x[1])],
        "by_factory": [{"name": k, "value": v} for k, v in sorted(by_factory.items(), key=lambda x: -x[1])],
        "reasons": [{"name": k, "value": v} for k, v in sorted(reasons.items(), key=lambda x: -x[1])],
        "projected": projected_rows,
        "note": "Illustrative estimates from simulated arrival and loading events, not invoiced charges.",
    }


def readiness_counts(db: Session, factory_ids: list[int], now: datetime, horizon_h: int = 48) -> dict:
    """Orders scheduled to ship within the horizon vs those ready."""
    orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(factory_ids), m.CustomerOrder.status == "Open",
        m.CustomerOrder.scheduled_dispatch <= now + timedelta(hours=horizon_h))))
    info = assess(db, orders, now)
    ready = sum(1 for o in orders if info[o.id]["readiness"]["status"] == "Ready for Dispatch")
    at_risk = sum(1 for o in orders if info[o.id]["risk"]["at_risk"])
    return {"scheduled": len(orders), "ready": ready, "at_risk": at_risk, "pct": pct(ready, len(orders))}
