"""Raw-material coverage and finished-goods inventory operations."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from .common import log_event, next_number

OPEN_SHIP = ("Scheduled", "In Transit", "Arrived", "Receiving")


# ─── Raw materials ───────────────────────────────────────────────────────
def material_rows(db: Session, factory_ids: list[int], now: datetime) -> list[dict]:
    stocks = list(db.scalars(select(m.MaterialStock).where(m.MaterialStock.factory_id.in_(factory_ids))))
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    incoming = defaultdict(float)
    next_eta = {}
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(factory_ids),
                                                        m.InboundShipment.status.in_(OPEN_SHIP))):
        incoming[(s.factory_id, s.material_id)] += s.qty
        k = (s.factory_id, s.material_id)
        if k not in next_eta or s.expected_arrival < next_eta[k]:
            next_eta[k] = s.expected_arrival
    reserved = defaultdict(float)
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id.in_(factory_ids),
                                                         m.ProductionOrder.status.in_(("Ready", "In Progress", "Delayed")))):
        sku = skus[po.sku_id]
        rem = max(0, po.required_qty - po.completed_qty)
        reserved[(po.factory_id, sku.fill_material_id)] += rem * sku.fill_kg
        reserved[(po.factory_id, sku.shell_material_id)] += rem
    rows = []
    for st in stocks:
        k = (st.factory_id, st.material_id)
        cons = st.avg_daily_consumption or 0
        days = round(st.on_hand / cons, 1) if cons > 0 else None
        proj_days = round((st.on_hand + incoming[k]) / cons, 1) if cons > 0 else None
        if days is None:
            status = "No demand"
        elif days < 1.5:
            status = "Critical"
        elif days < st.reorder_days:
            status = "Low"
        else:
            status = "OK"
        mat = st.material
        rows.append({
            "id": st.id, "factory_id": st.factory_id, "factory": factories[st.factory_id].code,
            "material_id": mat.id, "code": mat.code, "name": mat.name, "category": mat.category, "uom": mat.uom,
            "available": round(st.on_hand, 1), "reserved": round(min(st.on_hand, reserved[k]), 1),
            "damaged": round(st.damaged, 1), "incoming": round(incoming[k], 1),
            "consumption_per_day": round(cons, 1), "consumed_today": round(st.consumed_today or 0, 1),
            "days_cover": days, "projected_days_cover": proj_days, "reorder_days": st.reorder_days,
            "reorder_point": round(cons * st.reorder_days, 1),
            "next_delivery": next_eta.get(k).isoformat(timespec="minutes") if next_eta.get(k) else None,
            "status": status,
        })
    rows.sort(key=lambda r: (r["days_cover"] if r["days_cover"] is not None else 999))
    return rows


def recommended_reorder(row: dict, lead_days: int, safety_days: float = 3) -> float:
    need = row["consumption_per_day"] * (lead_days + safety_days + 7) - row["available"] - row["incoming"]
    return max(0.0, round(need, -2))


# ─── Finished goods ──────────────────────────────────────────────────────
def location_occupancy(db: Session, factory_ids: list[int]) -> dict[int, int]:
    rows = db.execute(select(m.Pallet.location_id, func.count()).where(
        m.Pallet.factory_id.in_(factory_ids), m.Pallet.status == "Stored").group_by(m.Pallet.location_id))
    return {lid: n for lid, n in rows if lid}


def suggest_location(db: Session, pallet: m.Pallet) -> m.StorageLocation | None:
    """Prefer bulk floor lanes for Gaylords, otherwise the nearest rack slot
    next to the same SKU (consolidation), else the first free slot."""
    sku = db.get(m.Sku, pallet.sku_id)
    occ = location_occupancy(db, [pallet.factory_id])
    locs = list(db.scalars(select(m.StorageLocation).where(m.StorageLocation.factory_id == pallet.factory_id)
                           .order_by(m.StorageLocation.zone, m.StorageLocation.bay, m.StorageLocation.rack, m.StorageLocation.level)))
    bulk = "Gaylord" in (sku.carton_type or "")
    pool = [loc for loc in locs if (loc.kind == "Floor") == bulk and occ.get(loc.id, 0) < loc.capacity_pallets]
    if not pool:
        pool = [loc for loc in locs if occ.get(loc.id, 0) < loc.capacity_pallets]
    if not pool:
        return None
    same = {p.location_id for p in db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == pallet.factory_id, m.Pallet.sku_id == pallet.sku_id, m.Pallet.status == "Stored"))}
    near = [loc for loc in pool if loc.id in same] or [
        loc for loc in pool if any(abs(loc.bay - db.get(m.StorageLocation, s).bay) <= 1 and loc.zone == db.get(m.StorageLocation, s).zone for s in same if s)]
    return (near or pool)[0]


def put_away(db: Session, pallet: m.Pallet, location: m.StorageLocation, now: datetime, user: str) -> None:
    if pallet.status != "Pending Put-away":
        raise ValueError(f"Pallet {pallet.code} is {pallet.status}; only pallets pending put-away can be stored")
    occ = location_occupancy(db, [pallet.factory_id]).get(location.id, 0)
    if occ >= location.capacity_pallets:
        raise ValueError(f"Location {location.code} is full")
    sku = db.get(m.Sku, pallet.sku_id)
    pallet.location_id = location.id
    pallet.status = "Stored"
    pallet.stored_at = now
    pallet.stored_by = user
    if pallet.po_id:
        po = db.get(m.ProductionOrder, pallet.po_id)
        po.stored_qty += pallet.cartons * sku.pillows_per_carton
    log_event(db, now, pallet.factory_id, "warehouse", "putaway",
              f"Pallet {pallet.code} ({pallet.cartons} cartons {sku.code}) stored at {location.code}", "pallet", pallet.id, user)


def available_pallets(db: Session, factory_id: int, sku_id: int) -> list[m.Pallet]:
    return list(db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == factory_id, m.Pallet.sku_id == sku_id, m.Pallet.status == "Stored",
        m.Pallet.cartons > m.Pallet.allocated_cartons).order_by(m.Pallet.stored_at, m.Pallet.id)))


def allocate(db: Session, order: m.CustomerOrder, now: datetime, user: str = "system") -> dict:
    """Reserve stored finished goods (FIFO, own production order first)."""
    sku = order.sku
    ppc = sku.pillows_per_carton
    need_cartons = (order.qty - order.allocated_qty) // ppc
    if need_cartons <= 0:
        return {"allocated_cartons": 0, "shortage_cartons": 0}
    own_po = {p.id for p in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.customer_order_id == order.id))}
    pallets = available_pallets(db, order.factory_id, order.sku_id)
    pallets.sort(key=lambda p: (0 if p.po_id in own_po else 1, p.stored_at or now))
    got = 0
    for p in pallets:
        if need_cartons <= 0:
            break
        free = p.cartons - p.allocated_cartons
        take = min(free, need_cartons)
        if take <= 0:
            continue
        p.allocated_cartons += take
        db.add(m.Allocation(order_id=order.id, pallet_id=p.id, cartons=take, pillows=take * ppc,
                            status="Reserved", created_at=now))
        need_cartons -= take
        got += take
        order.allocated_qty += take * ppc
    if got:
        log_event(db, now, order.factory_id, "warehouse", "allocation",
                  f"{got} cartons ({got * ppc:,} pillows) of {sku.code} reserved for {order.number}", "order", order.id, user)
    return {"allocated_cartons": got, "shortage_cartons": max(0, need_cartons)}


def pick(db: Session, alloc: m.Allocation, now: datetime, user: str) -> None:
    if alloc.status != "Reserved":
        raise ValueError("Allocation already picked")
    p = alloc.pallet
    p.cartons -= alloc.cartons
    p.allocated_cartons -= alloc.cartons
    if p.cartons <= 0:
        p.cartons = 0
        p.status = "Depleted"
        p.location_id = None
    alloc.status = "Picked"
    alloc.picked_at = now
    alloc.picked_by = user
    order = db.get(m.CustomerOrder, alloc.order_id)
    order.picked_qty += alloc.pillows
    log_event(db, now, order.factory_id, "warehouse", "pick",
              f"Picked {alloc.cartons} cartons from {p.code} for {order.number}", "order", order.id, user)


def fg_summary(db: Session, factory_ids: list[int]) -> dict:
    pallets = list(db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(factory_ids),
                                                     m.Pallet.status.in_(("Open", "Pending Put-away", "Stored")))))
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    stored = [p for p in pallets if p.status == "Stored"]
    total = sum(p.cartons * skus[p.sku_id].pillows_per_carton for p in stored)
    reserved = sum(p.allocated_cartons * skus[p.sku_id].pillows_per_carton for p in stored)
    pending = [p for p in pallets if p.status == "Pending Put-away"]
    locs = list(db.scalars(select(m.StorageLocation).where(m.StorageLocation.factory_id.in_(factory_ids))))
    capacity = sum(loc.capacity_pallets for loc in locs)
    pend_pick = db.scalar(select(func.coalesce(func.sum(m.Allocation.pillows), 0)).join(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(factory_ids), m.Allocation.status == "Reserved")) or 0
    return {
        "total_pillows": total, "total_cartons": sum(p.cartons for p in stored),
        "available_pillows": total - reserved, "reserved_pillows": reserved,
        "pending_putaway_pallets": len(pending),
        "pending_putaway_pillows": sum(p.cartons * skus[p.sku_id].pillows_per_carton for p in pending),
        "pending_pick_pillows": int(pend_pick),
        "stored_pallets": len(stored), "capacity_pallets": capacity,
        "utilization_pct": round(100 * len(stored) / capacity, 1) if capacity else 0,
    }


def new_pallet(db: Session, po: m.ProductionOrder, now: datetime) -> m.Pallet:
    sku = db.get(m.Sku, po.sku_id)
    p = m.Pallet(code=next_number(db, m.Pallet, "PLT-", 50000), factory_id=po.factory_id, po_id=po.id,
                 sku_id=po.sku_id, customer_id=sku.customer_id, batch_no=f"B{now:%y%m%d}-{po.number[-4:]}",
                 cartons=0, capacity=sku.cartons_per_pallet, status="Open", created_at=now)
    db.add(p)
    db.flush()
    return p


def stock_by_sku(db: Session, factory_ids: list[int]) -> list[dict]:
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    customers = {c.id: c.name for c in db.scalars(select(m.Customer))}
    agg = defaultdict(lambda: {"stored": 0, "reserved": 0, "pallets": 0, "locations": set()})
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(factory_ids), m.Pallet.status == "Stored")):
        a = agg[(p.factory_id, p.sku_id)]
        ppc = skus[p.sku_id].pillows_per_carton
        a["stored"] += p.cartons * ppc
        a["reserved"] += p.allocated_cartons * ppc
        a["pallets"] += 1
        if p.location:
            a["locations"].add(p.location.code)
    out = []
    for (fid, sid), a in agg.items():
        out.append({"factory_id": fid, "sku_id": sid, "sku": skus[sid].code, "customer": customers[skus[sid].customer_id],
                    "stored": a["stored"], "reserved": a["reserved"], "available": a["stored"] - a["reserved"],
                    "pallets": a["pallets"], "locations": sorted(a["locations"])[:6]})
    return sorted(out, key=lambda r: -r["stored"])


def days_ago(now: datetime, d: int) -> datetime:
    return now - timedelta(days=d)
