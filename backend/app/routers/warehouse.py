"""Warehouse & Inventory."""
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
from ..services import insights, inventory, readiness
from ..services.common import iso, log_event
from .core import commit_change
from .serializers import Ctx, carton_dict, get_or_404, lookup, pallet_dict

router = APIRouter(prefix="/api/warehouse")


def _single_factory(c: Ctx) -> int:
    return c.fids[0]


@router.get("/summary")
def summary(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    fg = inventory.fg_summary(db, c.fids)
    overridden = db.scalar(select(func.count()).select_from(m.Carton).join(m.Pallet, m.Carton.pallet_id == m.Pallet.id).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.status == "Overridden", m.Pallet.status == "Stored")) or 0
    stale = db.scalar(select(func.count()).select_from(m.Pallet).where(
        m.Pallet.factory_id.in_(c.fids), m.Pallet.status == "Pending Put-away", m.Pallet.closed_at < c.now - timedelta(hours=2))) or 0
    pend_pick_orders = db.scalar(select(func.count(func.distinct(m.Allocation.order_id))).join(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(c.fids), m.Allocation.status == "Reserved")) or 0
    return {**fg, "discrepancies": overridden + stale, "discrepancy_detail": {"override_count_cartons": overridden,
                                                                              "unlocated_pallets_over_2h": stale},
            "pending_pick_orders": pend_pick_orders}


@router.get("/layout")
def layout(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    fid = _single_factory(c)
    L = lookup(db)
    pallets = defaultdict(list)
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id == fid, m.Pallet.status == "Stored")):
        pallets[p.location_id].append(p)
    zones = defaultdict(list)
    for loc in db.scalars(select(m.StorageLocation).where(m.StorageLocation.factory_id == fid)
                          .order_by(m.StorageLocation.zone, m.StorageLocation.bay, m.StorageLocation.rack, m.StorageLocation.level)):
        ps = pallets.get(loc.id, [])
        skus = {L["skus"][p.sku_id].code for p in ps}
        zones[loc.zone].append({"id": loc.id, "code": loc.code, "bay": loc.bay, "rack": loc.rack, "level": loc.level,
                                "kind": loc.kind, "capacity": loc.capacity_pallets, "occupied": len(ps),
                                "occupancy_pct": round(100 * len(ps) / loc.capacity_pallets),
                                "sku": ", ".join(sorted(skus)) if skus else None,
                                "reserved": any(p.allocated_cartons for p in ps)})
    names = {"A": "Zone A — Racking", "B": "Zone B — Racking", "C": "Zone C — Racking", "D": "Zone D — Bulk floor lanes (Gaylords)"}
    return {"factory": L["factories"][fid].code, "factory_name": L["factories"][fid].name,
            "zones": [{"zone": z, "name": names.get(z, z), "locations": locs,
                       "utilization_pct": round(100 * sum(x["occupied"] for x in locs) / max(1, sum(x["capacity"] for x in locs)), 1)}
                      for z, locs in sorted(zones.items())]}


@router.get("/locations/{loc_id}")
def location(loc_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    loc = get_or_404(db, m.StorageLocation, loc_id)
    L = lookup(db)
    rows = []
    for p in db.scalars(select(m.Pallet).where(m.Pallet.location_id == loc.id, m.Pallet.status == "Stored")):
        d = pallet_dict(p, L)
        po = db.get(m.ProductionOrder, p.po_id) if p.po_id else None
        d["production_order"] = po.number if po else "Stock replenishment"
        d["rfid_tag"] = f"E280-{p.id:08X}"
        rows.append(d)
    return {"location": {"id": loc.id, "code": loc.code, "zone": loc.zone, "bay": loc.bay, "rack": loc.rack, "level": loc.level,
                         "kind": loc.kind, "capacity": loc.capacity_pallets, "occupied": len(rows),
                         "barcode": loc.code.replace("-", "")}, "pallets": rows}


@router.get("/receiving")
def receiving(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    pending = []
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(c.fids), m.Pallet.status == "Pending Put-away")
                        .order_by(m.Pallet.closed_at)):
        d = pallet_dict(p, L)
        loc = inventory.suggest_location(db, p)
        d["suggested_location"] = {"id": loc.id, "code": loc.code} if loc else None
        d["cartons_list"] = [x.carton_code for x in db.scalars(select(m.Carton).where(m.Carton.pallet_id == p.id).limit(4))]
        d["waiting_min"] = round((c.now - p.closed_at).total_seconds() / 60) if p.closed_at else None
        pending.append(d)
    building = [pallet_dict(p, L) for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(c.fids), m.Pallet.status == "Open"))]
    recent = [carton_dict(x, L) for x in db.scalars(select(m.Carton).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.carton_code.is_not(None)).order_by(m.Carton.approved_at.desc()).limit(12))]
    return {"pending": pending, "building": building, "recent_cartons": recent}


class ScanIn(BaseModel):
    code: str
    factory: str | None = None


@router.post("/scan")
def scan(body: ScanIn, db: Session = Depends(get_db), user=Depends(current_user)):
    code = body.code.strip().upper()
    L = lookup(db)
    if code.startswith("HOLD-"):
        c = db.scalar(select(m.Carton).where(m.Carton.hold_code == code))
        if c:
            return {"type": "blocked_carton", "ok": False, "carton": carton_dict(c, L),
                    "message": f"{code} failed quality inspection ({c.defect_category}). It cannot enter finished goods."}
    if code.startswith("CTN-"):
        c = db.scalar(select(m.Carton).where(m.Carton.carton_code == code))
        if c:
            p = db.get(m.Pallet, c.pallet_id) if c.pallet_id else None
            return {"type": "carton", "ok": c.status in ("Passed", "Reworked", "Overridden"), "carton": carton_dict(c, L),
                    "pallet": pallet_dict(p, L) if p else None,
                    "message": f"{code}: {c.detected_count} × {L['skus'][c.sku_id].code} — {c.status}; on pallet {p.code if p else '—'}"}
    if code.startswith("PLT-"):
        p = db.scalar(select(m.Pallet).where(m.Pallet.code == code))
        if p:
            d = pallet_dict(p, L)
            loc = inventory.suggest_location(db, p) if p.status in ("Pending Put-away", "Open") else None
            d["suggested_location"] = {"id": loc.id, "code": loc.code} if loc else None
            return {"type": "pallet", "ok": True, "pallet": d, "message": f"{code}: {p.cartons} cartons {L['skus'][p.sku_id].code} — {p.status}"}
    loc = db.scalar(select(m.StorageLocation).where(m.StorageLocation.code == code))
    if loc:
        return {"type": "location", "ok": True, "location": {"id": loc.id, "code": loc.code}, "message": f"Location {loc.code}"}
    raise HTTPException(404, f"No carton, pallet or location matches {code}")


class PutawayIn(BaseModel):
    pallet_id: int
    location_id: int
    confirmed_sku: str | None = None
    confirmed_cartons: int | None = None


@router.post("/putaway")
def putaway(body: PutawayIn, db: Session = Depends(get_db), user: m.User = Depends(require("warehouse.write"))):
    with WRITE_LOCK:
        p = get_or_404(db, m.Pallet, body.pallet_id)
        loc = get_or_404(db, m.StorageLocation, body.location_id)
        sku = db.get(m.Sku, p.sku_id)
        if p.status == "Open":
            p.status, p.closed_at = "Pending Put-away", Ctx(db, None).now
        if body.confirmed_sku and body.confirmed_sku.upper() != sku.code:
            raise HTTPException(400, f"SKU mismatch: pallet holds {sku.code}, scanned {body.confirmed_sku}")
        if body.confirmed_cartons is not None and body.confirmed_cartons != p.cartons:
            raise HTTPException(400, f"Quantity mismatch: pallet record shows {p.cartons} cartons, counted {body.confirmed_cartons}")
        if loc.factory_id != p.factory_id:
            raise HTTPException(400, "Location belongs to another factory")
        blocked = db.scalar(select(func.count()).select_from(m.Carton).where(m.Carton.pallet_id == p.id, m.Carton.status == "Blocked"))
        if blocked:
            raise HTTPException(400, "Pallet contains cartons that failed quality inspection")
        try:
            inventory.put_away(db, p, loc, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "warehouse")
        return pallet_dict(p, lookup(db))


@router.get("/allocation")
def allocation(factory: str = "ALL", days: int = 7, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    stock = {(r["factory_id"], r["sku_id"]): r for r in inventory.stock_by_sku(db, c.fids)}
    orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(c.fids), m.CustomerOrder.status == "Open",
        m.CustomerOrder.scheduled_dispatch <= c.now + timedelta(days=days)).order_by(m.CustomerOrder.scheduled_dispatch)))
    info = readiness.assess(db, orders, c.now)
    rows = []
    for o in orders:
        s = stock.get((o.factory_id, o.sku_id), {"stored": 0, "reserved": 0, "available": 0, "locations": []})
        shortage = max(0, o.qty - o.allocated_qty - s["available"])
        po = next(iter(info[o.id]["pos"]), None)
        rows.append({"order_id": o.id, "order": o.number, "customer": o.customer.name, "sku": o.sku.code, "factory": L["factories"][o.factory_id].code,
                     "required": o.qty, "allocated": o.allocated_qty, "picked": o.picked_qty,
                     "sku_total_stock": s["stored"], "sku_reserved": s["reserved"], "sku_unallocated": s["available"],
                     "pending_requirement": max(0, o.qty - o.allocated_qty), "shortage": shortage,
                     "linked_po": {"id": po.id, "number": po.number, "remaining": po.required_qty - po.completed_qty,
                                   "estimated_completion": iso(po.estimated_completion), "status": po.status} if po and shortage else None,
                     "scheduled_dispatch": iso(o.scheduled_dispatch), "readiness": info[o.id]["readiness"]})
    totals = inventory.fg_summary(db, c.fids)
    return {"orders": rows, "totals": {"total": totals["total_pillows"], "reserved": totals["reserved_pillows"],
                                       "unallocated": totals["available_pillows"],
                                       "pending_requirements": sum(r["pending_requirement"] for r in rows),
                                       "shortage": sum(r["shortage"] for r in rows)},
            "stock_by_sku": [r | {"factory": L["factories"][r["factory_id"]].code} for r in inventory.stock_by_sku(db, c.fids)]}


class OrderIn(BaseModel):
    order_id: int


@router.post("/allocate")
def allocate(body: OrderIn, db: Session = Depends(get_db), user: m.User = Depends(require("warehouse.write"))):
    with WRITE_LOCK:
        o = get_or_404(db, m.CustomerOrder, body.order_id)
        if o.status != "Open":
            raise HTTPException(400, f"{o.number} is {o.status}")
        res = inventory.allocate(db, o, Ctx(db, None).now, user.username)
        commit_change(db, "warehouse")
        po = db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.customer_order_id == o.id)).first()
        res["message"] = (f"Allocated {res['allocated_cartons']} cartons to {o.number}" +
                          (f"; short {res['shortage_cartons']} cartons — covered by {po.number} (est. {po.estimated_completion:%a %H:%M})"
                           if res["shortage_cartons"] and po and po.estimated_completion else ""))
        return res


@router.get("/picking")
def picking(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    trucks = {}
    for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.factory_id.in_(c.fids),
                                                         m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")))):
        trucks[t.order_id] = t
    rows = []
    for a in db.scalars(select(m.Allocation).join(m.CustomerOrder).where(
            m.CustomerOrder.factory_id.in_(c.fids), m.Allocation.status == "Reserved")):
        o = db.get(m.CustomerOrder, a.order_id)
        t = trucks.get(o.id)
        p = a.pallet
        rows.append({"id": a.id, "order_id": o.id, "order": o.number, "customer": o.customer.name, "sku": o.sku.code,
                     "cartons": a.cartons, "pillows": a.pillows, "pallet": p.code, "location": p.location.code if p.location else "—",
                     "required_dispatch": iso(t.scheduled_arrival if t else o.scheduled_dispatch), "truck": t.ref if t else None,
                     "dock": t.dock if t else None, "expedite": o.expedite_picking, "factory": L["factories"][o.factory_id].code,
                     "truck_arrived": bool(t and t.status == "Arrived")})
    rows.sort(key=lambda r: (not r["expedite"], not r["truck_arrived"], r["required_dispatch"] or ""))
    return rows


@router.post("/picking/{alloc_id}/confirm")
def confirm_pick(alloc_id: int, db: Session = Depends(get_db), user: m.User = Depends(require("warehouse.write"))):
    with WRITE_LOCK:
        a = get_or_404(db, m.Allocation, alloc_id)
        try:
            inventory.pick(db, a, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "warehouse")
        o = db.get(m.CustomerOrder, a.order_id)
        info = readiness.assess(db, [o], Ctx(db, None).now)[o.id]
        return {"ok": True, "order": o.number, "picked_qty": o.picked_qty, "readiness": info["readiness"]}


@router.post("/picking/expedite")
def expedite(body: OrderIn, db: Session = Depends(get_db), user: m.User = Depends(require("warehouse.write"))):
    with WRITE_LOCK:
        o = get_or_404(db, m.CustomerOrder, body.order_id)
        expedite_order(db, o, Ctx(db, None).now, user.username)
        commit_change(db, "warehouse")
        return {"ok": True}


def expedite_order(db: Session, o: m.CustomerOrder, now: datetime, user: str):
    o.expedite_picking = True
    if o.allocated_qty < o.qty:
        inventory.allocate(db, o, now, user)
    log_event(db, now, o.factory_id, "warehouse", "expedite", f"Picking expedited for {o.number}", "order", o.id, user)


@router.get("/workload")
def workload(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    wl = insights.workload(db, c.fids, c.now)
    day0 = datetime.combine(c.now.date(), datetime.min.time())
    hours = defaultdict(lambda: {"inbound": 0, "putaway": 0, "picking": 0, "outbound": 0})
    kinds = {"goods_receipt": "inbound", "putaway": "putaway", "pick": "picking", "dispatched": "outbound"}
    for e in db.scalars(select(m.EventLog).where(m.EventLog.factory_id.in_(c.fids), m.EventLog.ts >= day0,
                                                 m.EventLog.event_type.in_(list(kinds)))):
        hours[e.ts.hour][kinds[e.event_type]] += 1
    activity = [{"hour": f"{h:02d}:00", **hours[h]} for h in range(6, min(23, c.now.hour + 1))]
    deployed = []
    from ..services import calendar as cal
    sh = cal.shift_of(c.now) or 1
    for e in db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(c.fids), m.Employee.shift == sh,
                                                 m.Employee.role.in_(("Warehouse associate", "Forklift operator")))):
        deployed.append({"code": e.code, "name": e.name, "role": e.role, "attendance": e.attendance, "hourly_wage": e.hourly_wage})
    return {**wl, "activity": activity, "crew": deployed,
            "today": {k: sum(h[k] for h in hours.values()) for k in ("inbound", "putaway", "picking", "outbound")}}


@router.get("/stock")
def stock(factory: str = "ALL", sku: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    rows = inventory.stock_by_sku(db, c.fids)
    if sku:
        rows = [r for r in rows if sku.upper() in r["sku"]]
    return [r | {"factory": L["factories"][r["factory_id"]].code} for r in rows]
