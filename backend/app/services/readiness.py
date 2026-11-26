"""Shipment readiness, shipment risk and detention costs.

An order is ready only when production is complete, cartons have passed
quality, finished goods are put away, the quantity is allocated and picking
is complete. Risk compares the projected ready time with the truck
appointment; detention uses the configurable illustrative charge."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
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


def packing_hold(db: Session, factory_id: int, sku: m.Sku, now: datetime) -> dict | None:
    """Packing of this SKU is stopped for lack of cartons or polybags: which material and when it resumes
    (the next delivery, received about an hour after it reaches the dock). None when packing can run."""
    from .quality import packaging_available
    if packaging_available(db, factory_id, sku):
        return None
    for mid, need in ((sku.carton_material_id, 1), (sku.bag_material_id, sku.pillows_per_carton)):
        st = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == factory_id,
                                                     m.MaterialStock.material_id == mid))
        if not st or st.on_hand >= need:
            continue
        mat = db.get(m.Material, mid)
        nxt = db.scalar(select(m.InboundShipment).where(
            m.InboundShipment.factory_id == factory_id, m.InboundShipment.material_id == mid,
            m.InboundShipment.status.in_(("Scheduled", "In Transit", "Arrived", "Receiving")))
            .order_by(m.InboundShipment.expected_arrival))
        if nxt is None:
            return {"material": mat.code, "resume": None,
                    "text": f"Packing stopped: {mat.code} out of stock and no delivery on order"}
        at_dock = nxt.status in ("Arrived", "Receiving")
        resume = cal.add_productive(now if at_dock else max(now, nxt.expected_arrival), 60)
        when = "at the dock" if at_dock else nxt.expected_arrival.strftime("%a %b %d %H:%M")
        return {"material": mat.code, "resume": resume,
                "text": f"Packing stopped: {mat.code} out of stock, next delivery {nxt.ref} {when}"}
    return None


def ready_eta(order: m.CustomerOrder, pos: list[m.ProductionOrder], factory: m.Factory, now: datetime,
              rd: dict, coverable: int | None = None, hold: dict | None = None) -> datetime | None:
    """Projected time the order will be fully picked and ready to load.

    `coverable` is how many of the still-unallocated pillows existing goods can cover (the order's own
    unallocated production plus its share of free stock). With no open production and not enough stock,
    the time is unknown (None) until a top-up production order exists."""
    if rd["blocking"] is None:
        return now
    q = order.qty
    sku_ppc = order.sku.pillows_per_carton
    t = now
    open_pos = [p for p in pos if p.status != "Completed" and p.completed_qty < p.required_qty]
    short = max(0, q - order.allocated_qty)
    if open_pos:
        ends = [p.estimated_completion or (now + timedelta(days=3)) for p in open_pos]
        t = max(ends)
        t = cal.add_productive(t, POST_PRODUCTION_LAG_MIN)
        if hold and hold["resume"]:
            t = max(t, cal.add_productive(hold["resume"], POST_PRODUCTION_LAG_MIN * 0.6))
    elif short and coverable is not None and coverable < short:
        return None
    elif hold and any((p.pack_queue or 0) > 0 for p in pos):
        if hold["resume"] is None:
            return None
        t = cal.add_productive(hold["resume"], POST_PRODUCTION_LAG_MIN * 0.6)
    elif short or rd["blocking"] in ("quality", "inventory"):
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


def coverable_stock(db: Session, orders: list[m.CustomerOrder]) -> dict[int, int]:
    """Unallocated pillows that existing goods can still give each open order: its own production
    (stored or on its way to storage) plus free stock of the SKU, shared out in ship-date order."""
    from .inventory import pegged_po_ids
    open_orders = sorted((o for o in orders if o.status == "Open" and o.allocated_qty < o.qty),
                         key=lambda o: (o.scheduled_dispatch or datetime.max, o.id))
    if not open_orders:
        return {}
    fids = {o.factory_id for o in open_orders}
    sku_ids = {o.sku_id for o in open_orders}
    pegged = set().union(*(pegged_po_ids(db, fid) for fid in fids))
    po_order = dict(db.execute(select(m.ProductionOrder.id, m.ProductionOrder.customer_order_id).where(
        m.ProductionOrder.id.in_(pegged))).all()) if pegged else {}
    ppc = {s.id: s.pillows_per_carton for s in db.scalars(select(m.Sku).where(m.Sku.id.in_(sku_ids)))}
    own, free = defaultdict(int), defaultdict(int)
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(fids), m.Pallet.sku_id.in_(sku_ids),
                                               m.Pallet.status.in_(("Open", "Pending Put-away", "Stored")))):
        n = (p.cartons - p.allocated_cartons) * ppc[p.sku_id]
        if n <= 0:
            continue
        if p.po_id in pegged:
            own[po_order[p.po_id]] += n
        elif p.status == "Stored":
            free[(p.factory_id, p.sku_id)] += n
    # pillows of the order's own production still in the packing queue or in blocked cartons awaiting rework
    order_ids = [o.id for o in open_orders]
    po_order_all = {}
    for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.customer_order_id.in_(order_ids))):
        own[po.customer_order_id] += po.pack_queue or 0
        po_order_all[po.id] = po.customer_order_id
    if po_order_all:
        for po_id, n in db.execute(select(m.Carton.po_id, func.coalesce(func.sum(m.Carton.required_count), 0)).where(
                m.Carton.po_id.in_(list(po_order_all)), m.Carton.status == "Blocked").group_by(m.Carton.po_id)):
            own[po_order_all[po_id]] += int(n)
    out = {}
    for o in open_orders:
        short = o.qty - o.allocated_qty
        mine = min(short, own.get(o.id, 0))
        share = min(short - mine, free[(o.factory_id, o.sku_id)])
        free[(o.factory_id, o.sku_id)] -= share
        out[o.id] = mine + share
    return out


def assess(db: Session, orders: list[m.CustomerOrder], now: datetime) -> dict[int, dict]:
    """Readiness + risk for many orders at once."""
    rules = business_rules(db)
    ids = [o.id for o in orders]
    pos = order_pos(db, ids)
    trucks = truck_for_orders(db, ids)
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    cover = coverable_stock(db, orders)
    holds = {}
    for o in orders:
        if o.status == "Open" and any((p.pack_queue or 0) > 0 for p in pos[o.id]):
            key = (o.factory_id, o.sku_id)
            if key not in holds:
                holds[key] = packing_hold(db, o.factory_id, o.sku, now)
    out = {}
    for o in orders:
        rd = readiness(o, pos[o.id])
        truck = trucks.get(o.id)
        hold = holds.get((o.factory_id, o.sku_id)) if any((p.pack_queue or 0) > 0 for p in pos[o.id]) else None
        eta = (ready_eta(o, pos[o.id], factories[o.factory_id], now, rd, cover.get(o.id, 0), hold)
               if o.status == "Open" else None)
        risk = {"level": "None", "at_risk": False, "slack_min": None, "est_detention": 0.0, "reasons": []}
        if o.status == "Open" and rd["blocking"] is not None and eta is None and truck and truck.status in ("Scheduled", "Arrived"):
            short = o.qty - o.allocated_qty
            risk.update(level="High", at_risk=True, est_detention=rules["detention_charge"],
                        reasons=[hold["text"] if hold else
                                 f"{short:,} pillows not covered by stock or open production — top-up production needed"])
        elif o.status == "Open" and rd["blocking"] is not None and truck and truck.status in ("Scheduled", "Arrived"):
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
                    f"Production completion est. {min([p.estimated_completion for p in late_pos if p.estimated_completion] or [eta]):%a %b %d %H:%M}")
                if any(p.material_risk for p in late_pos):
                    risk["reasons"].append(next(p.risk_reason for p in late_pos if p.material_risk))
            if hold:
                risk["reasons"].append(hold["text"])
            elif rd["blocking"] in ("quality", "inventory"):
                risk["reasons"].append("Cartons awaiting inspection / put-away")
            if rd["blocking"] == "allocation":
                risk["reasons"].append("Finished goods not yet allocated")
            if rd["blocking"] == "picking" or (rd["blocking"] and o.picked_qty < o.qty):
                risk["reasons"].append(f"{max(0, o.qty - o.picked_qty):,} pillows still to pick")
        out[o.id] = {"readiness": rd, "ready_eta": eta, "truck": truck, "risk": risk, "pos": pos[o.id]}
    return out


def reason_bucket(reason: str | None) -> str:
    """Detention cause without the wait length ("Released after 7 h wait (Picking Pending)" → "Picking Pending · truck released")."""
    if not reason:
        return "Order not ready"
    mt = re.match(r"Released(?: after [\d.]+ h wait)? ?[—(-]? ?\(?([^)]+)\)?$", reason)
    return f"{mt.group(1).strip()} · truck released" if mt else reason


def recommendations(order: m.CustomerOrder, info: dict, now: datetime) -> list[dict]:
    recs = []
    rd, risk, truck = info["readiness"], info["risk"], info["truck"]
    if not risk["at_risk"] and risk["level"] != "Low":
        return recs
    open_pos = [p for p in info["pos"] if p.status != "Completed" and p.completed_qty < p.required_qty]
    if info["ready_eta"] is None and not open_pos:
        short = order.qty - order.allocated_qty
        recs.append({"action": "top_up", "label": f"Queue top-up production for {short:,} pillows",
                     "detail": "Nothing in stock or in production covers the rest of this order; a top-up order "
                               "goes first on the line already running this SKU."})
        return recs
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
        recs.append({"action": "reschedule", "label": f"Reschedule truck {truck.ref} to {new_time:%a %b %d %H:%M}",
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
            reasons[reason_bucket(t.delay_reason)] += 1
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
                                   "arrival": iso(t.scheduled_arrival),
                                   "projected_wait_min": -r["slack_min"] if r["slack_min"] is not None else None,
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
        "note": "Estimated from truck arrival and loading times at the configured charge.",
    }


READY_HORIZON_H = 24  # orders are picked to staging up to a day before their truck (engine.PICK_HORIZON_H)


def readiness_counts(db: Session, factory_ids: list[int], now: datetime, horizon_h: int = READY_HORIZON_H) -> dict:
    """Shipment readiness over the pick window: of the orders shipping within `horizon_h`, how many are
    picked and staged ("ready"); the rest are either on track (projected ready before the truck) or at risk.
    Orders further out are not expected to be picked yet, so they are reported separately (next 48 h)."""
    orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(factory_ids), m.CustomerOrder.status == "Open",
        m.CustomerOrder.scheduled_dispatch <= now + timedelta(hours=max(48, horizon_h)))))
    info = assess(db, orders, now)
    due = [o for o in orders if o.scheduled_dispatch <= now + timedelta(hours=horizon_h)]
    ready = sum(1 for o in due if info[o.id]["readiness"]["status"] == "Ready for Dispatch")
    at_risk = sum(1 for o in due if info[o.id]["risk"]["at_risk"])
    return {"horizon_h": horizon_h, "scheduled": len(due), "ready": ready, "at_risk": at_risk,
            "on_track": len(due) - ready - at_risk, "pct": pct(ready, len(due)),
            "due_48h": len(orders), "at_risk_48h": sum(1 for o in orders if info[o.id]["risk"]["at_risk"])}
