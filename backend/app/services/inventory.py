"""Raw-material coverage and finished-goods inventory operations."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from .common import log_event, next_number
from .planning import OPEN_PO as OPEN_PRODUCTION

OPEN_SHIP = ("Scheduled", "In Transit", "Arrived", "Receiving")
# When inbound material becomes usable: the same rule as planning.PlanCtx._material_projection, so the
# procurement run-out and the production / dispatch risk projection agree on replenishment timing.
AT_DOCK_LAG_MIN = 60      # arrived / being received: usable once the goods receipt is posted
IN_TRANSIT_LAG_MIN = 90   # scheduled / in transit: ETA + unloading and goods receipt
CRITICAL_DAYS = 1.5
STATUS_RANK = {"Critical": 0, "Untracked": 1, "Low": 2, "OK": 3, "No demand": 4}


# ─── Raw materials ───────────────────────────────────────────────────────
def usable_at(s: m.InboundShipment, now: datetime) -> datetime:
    if s.status in ("Arrived", "Receiving"):
        return now + timedelta(minutes=AT_DOCK_LAG_MIN)
    return max(now, s.expected_arrival) + timedelta(minutes=IN_TRANSIT_LAG_MIN)


def committed_need(db: Session, factory_ids: list[int]) -> tuple[dict, dict]:
    """Material that open production orders still have to consume, per (factory, material).

    Fill and shell are drawn when a pillow is made; one polybag per pillow and one carton + one label
    per carton are drawn when it is packed (quality._consume_packaging). Returns (quantity, orders)."""
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    need, orders = defaultdict(float), defaultdict(set)

    def add(po, mid, qty):
        if mid and qty > 0:
            need[(po.factory_id, mid)] += qty
            orders[(po.factory_id, mid)].add(po.id)

    for po in db.scalars(select(m.ProductionOrder).where(
            m.ProductionOrder.factory_id.in_(factory_ids),
            m.ProductionOrder.status.in_(OPEN_PRODUCTION) | (m.ProductionOrder.pack_queue > 0))):
        sku = skus[po.sku_id]
        if po.status in OPEN_PRODUCTION:
            to_make = max(0, po.required_qty - po.completed_qty)
            add(po, sku.fill_material_id, to_make * (sku.fill_kg or 0))
            add(po, sku.shell_material_id, to_make)
            to_pack = max(0, po.required_qty - (po.packed_qty or 0) - (po.held_qty or 0))
        else:  # finished order with pillows still waiting at the packing station
            to_pack = po.pack_queue or 0
        cartons = -(-to_pack // max(1, sku.pillows_per_carton))
        add(po, sku.bag_material_id, to_pack)
        add(po, sku.carton_material_id, cartons)
        add(po, sku.label_material_id, cartons)
    return need, {k: len(v) for k, v in orders.items()}


def material_rows(db: Session, factory_ids: list[int], now: datetime) -> list[dict]:
    """Raw-material position per factory.

    on_hand = reserved + available - shortfall, where reserved is everything open production orders
    still need (uncapped) and available is the free remainder. Cover and run-out use the average daily
    consumption on the shift calendar; a delivery counts toward cover only if it is usable before the
    stock runs out (same receiving lag as the production plan). Status = the lower of
    days of cover and free days ((on hand + on order - committed) / use per day)."""
    stocks = {(st.factory_id, st.material_id): st for st in db.scalars(
        select(m.MaterialStock).where(m.MaterialStock.factory_id.in_(factory_ids)))}
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    materials = {x.id: x for x in db.scalars(select(m.Material))}
    ships = defaultdict(list)
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(factory_ids),
                                                        m.InboundShipment.status.in_(OPEN_SHIP))):
        ships[(s.factory_id, s.material_id)].append(s)
    need, n_orders = committed_need(db, factory_ids)
    day_pm = cal.productive_minutes_per_shift(1) + cal.productive_minutes_per_shift(2)
    rows = []
    for key in set(stocks) | {k for k, v in need.items() if v > 0}:
        fid, mid = key
        st, mat = stocks.get(key), materials[mid]
        on_hand = max(0.0, st.on_hand) if st else 0.0
        cons = (st.avg_daily_consumption or 0.0) if st else 0.0
        reserved = need.get(key, 0.0)
        sh = sorted(ships[key], key=lambda s: usable_at(s, now))
        incoming = sum(s.qty for s in sh)
        in_time, late, runout = incoming, [], None
        if cons > 0:
            in_time, per_min = 0.0, cons / day_pm
            runout = cal.add_productive(now, on_hand / per_min)
            for s in sh:
                if usable_at(s, now) <= runout:
                    in_time += s.qty
                    runout = cal.add_productive(now, (on_hand + in_time) / per_min)
                else:
                    late.append(s)
        days = round((on_hand + in_time) / cons, 1) if cons > 0 else None
        free_days = round((on_hand + incoming - reserved) / cons, 1) if cons > 0 else None
        uncovered = max(0.0, reserved - on_hand - incoming)
        if st is None:
            status = "Untracked"
            reason = (f"No stock record at {factories[fid].code}: {n_orders.get(key, 0)} open production order(s) need "
                      f"{reserved:,.0f} {mat.uom}, but on-hand stock is not tracked")
        elif cons <= 0:
            status = "No demand" if reserved <= 0 else ("OK" if uncovered <= 0 else "Low")
            reason = (f"{uncovered:,.0f} {mat.uom} of committed need not covered by stock or open POs" if uncovered
                      else "No consumption recorded")
        else:
            basis = min(days, free_days)
            status = "Critical" if basis < CRITICAL_DAYS else "Low" if basis < st.reorder_days else "OK"
            if free_days < days and free_days < st.reorder_days:
                reason = (f"{n_orders.get(key, 0)} open production order(s) need {reserved:,.0f} {mat.uom}; on hand + on order "
                          f"{on_hand + incoming:,.0f}" + (f", {uncovered:,.0f} {mat.uom} not covered" if uncovered else ""))
            else:
                reason = f"Stock lasts {days} days at {cons:,.0f} {mat.uom}/day"
                if late:
                    reason += f"; next delivery {late[0].ref} is usable only after the stock-out"
                elif in_time:
                    reason += f" incl. {in_time:,.0f} {mat.uom} arriving before the stock-out"
        nxt = min(sh, key=lambda s: s.expected_arrival) if sh else None
        lt = late[0] if late else None
        rows.append({
            "id": st.id if st else f"untracked-{fid}-{mid}", "tracked": st is not None,
            "factory_id": fid, "factory": factories[fid].code,
            "material_id": mid, "code": mat.code, "name": mat.name, "category": mat.category, "uom": mat.uom,
            "on_hand": round(on_hand, 1), "reserved": round(reserved, 1),
            "available": round(max(0.0, on_hand - reserved), 1), "shortfall": round(max(0.0, reserved - on_hand), 1),
            "uncovered": round(uncovered, 1), "open_orders": n_orders.get(key, 0),
            "damaged": round(st.damaged or 0, 1) if st else 0.0,
            "incoming": round(incoming, 1), "incoming_in_time": round(in_time, 1),
            "consumption_per_day": round(cons, 1), "consumed_today": round(st.consumed_today or 0, 1) if st else 0.0,
            "days_cover": days, "on_hand_days": round(on_hand / cons, 1) if cons > 0 else None,
            "projected_days_cover": round((on_hand + incoming) / cons, 1) if cons > 0 else None,
            "free_days": free_days,
            "reorder_days": st.reorder_days if st else None, "reorder_point": round(cons * st.reorder_days, 1) if st else None,
            "runout_at": runout.isoformat(timespec="minutes") if runout else None,
            "next_delivery": nxt.expected_arrival.isoformat(timespec="minutes") if nxt else None,
            "late_delivery": {"shipment_id": lt.id, "ref": lt.ref, "po_id": lt.po_id, "po": lt.po.number if lt.po else None,
                              "usable_at": usable_at(lt, now).isoformat(timespec="minutes")} if lt else None,
            "status": status, "status_reason": reason,
        })
    # lowest cover first (callers take rows[0] as "lowest cover"); no-cover rows last, worst status first
    rows.sort(key=lambda r: (r["days_cover"] if r["days_cover"] is not None else 1e9, STATUS_RANK.get(r["status"], 5)))
    return rows


def recommended_reorder(row: dict, lead_days: int, safety_days: float = 3) -> float:
    """Order enough for lead time + safety + a 7-day cycle at current use, and at least the committed
    need of open production orders plus safety stock, net of stock on hand and on order."""
    cons = row["consumption_per_day"] or 0
    demand = max(cons * (lead_days + safety_days + 7), row["reserved"] + cons * safety_days)
    need = demand - row["on_hand"] - row["incoming"]
    return max(0.0, -(-need // 100) * 100)


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
    po = db.get(m.ProductionOrder, pallet.po_id) if pallet.po_id else None
    if po:
        po.stored_qty += pallet.cartons * sku.pillows_per_carton
    log_event(db, now, pallet.factory_id, "warehouse", "putaway",
              f"Pallet {pallet.code} ({pallet.cartons} cartons {sku.code}) stored at {location.code}", "pallet", pallet.id, user)
    # make-to-order: the pallet is reserved for its own customer order as soon as it is stored
    order = db.get(m.CustomerOrder, po.customer_order_id) if po and po.customer_order_id else None
    if order and order.status == "Open" and order.allocated_qty < order.qty:
        db.flush()
        allocate(db, order, now, user, own_only=True)


def available_pallets(db: Session, factory_id: int, sku_id: int) -> list[m.Pallet]:
    return list(db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == factory_id, m.Pallet.sku_id == sku_id, m.Pallet.status == "Stored",
        m.Pallet.cartons > m.Pallet.allocated_cartons).order_by(m.Pallet.stored_at, m.Pallet.id)))


def pegged_po_ids(db: Session, factory_id: int, exclude_order_id: int | None = None) -> set[int]:
    """Production orders whose output is still owed to their own (open, not fully allocated) customer order.

    Make-to-order stock belongs to that order until the order is covered; only surplus and make-to-stock
    pallets are free for other orders."""
    rows = db.execute(select(m.ProductionOrder.id).join(
        m.CustomerOrder, m.CustomerOrder.id == m.ProductionOrder.customer_order_id).where(
        m.ProductionOrder.factory_id == factory_id, m.CustomerOrder.status == "Open",
        m.CustomerOrder.allocated_qty < m.CustomerOrder.qty))
    return {r[0] for r in rows} - set(own_po_ids(db, exclude_order_id) if exclude_order_id else ())


def own_po_ids(db: Session, order_id: int) -> list[int]:
    return list(db.scalars(select(m.ProductionOrder.id).where(m.ProductionOrder.customer_order_id == order_id)))


def allocate(db: Session, order: m.CustomerOrder, now: datetime, user: str = "system", own_only: bool = False) -> dict:
    """Reserve stored finished goods: the order's own production first, then free stock (FIFO).

    Pallets made for another open order are not taken. Whatever is covered from free stock is netted off
    the order's open production orders, so the lines do not make it twice."""
    sku = order.sku
    ppc = sku.pillows_per_carton
    need_cartons = -(-(order.qty - order.allocated_qty) // ppc)  # whole cartons, rounded up
    if need_cartons <= 0 or order.status != "Open":
        return {"allocated_cartons": 0, "shortage_cartons": 0}
    own_po = set(own_po_ids(db, order.id))
    pegged = pegged_po_ids(db, order.factory_id, order.id)
    pallets = [p for p in available_pallets(db, order.factory_id, order.sku_id)
               if p.po_id in own_po or (not own_only and p.po_id not in pegged)]
    pallets.sort(key=lambda p: (0 if p.po_id in own_po else 1, p.stored_at or now, p.id))
    got = from_stock = 0
    for p in pallets:
        if need_cartons <= 0:
            break
        free = p.cartons - p.allocated_cartons
        take = min(free, need_cartons)
        if take <= 0:
            continue
        pillows = min(take * ppc, order.qty - order.allocated_qty)
        p.allocated_cartons += take
        db.add(m.Allocation(order_id=order.id, pallet_id=p.id, cartons=take, pillows=pillows,
                            status="Reserved", created_at=now))
        need_cartons -= take
        got += take
        if p.po_id not in own_po:
            from_stock += pillows
        order.allocated_qty += pillows
    if got:
        log_event(db, now, order.factory_id, "warehouse", "allocation",
                  f"{got} cartons ({got * ppc:,} pillows) of {sku.code} reserved for {order.number}"
                  + (f", {from_stock:,} from free stock" if from_stock else ""), "order", order.id, user)
    if from_stock:
        net_production(db, order, now, user)
    return {"allocated_cartons": got, "shortage_cartons": max(0, need_cartons)}


def net_production(db: Session, order: m.CustomerOrder, now: datetime, user: str) -> int:
    """Cut the order's open production by what free stock already covers. Returns pillows cut."""
    pos = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.customer_order_id == order.id)))
    own = {p.id for p in pos}
    from_stock = db.scalar(select(func.coalesce(func.sum(m.Allocation.pillows), 0)).join(m.Pallet).where(
        m.Allocation.order_id == order.id, (m.Pallet.po_id.is_(None)) | (m.Pallet.po_id.notin_(own or {0})))) or 0
    excess = sum(p.required_qty for p in pos) - max(0, order.qty - from_stock)
    cut = 0
    for po in sorted(pos, key=lambda p: -p.id):
        if excess <= 0:
            break
        if po.status == "Completed":
            continue
        c = min(excess, po.required_qty - po.completed_qty)
        if c <= 0:
            continue
        po.required_qty -= c
        excess -= c
        cut += c
        if po.required_qty <= po.completed_qty:
            po.status, po.completed_at, po.estimated_completion = "Completed", now, now
        log_event(db, now, order.factory_id, "production", "po_netted",
                  f"{po.number} reduced by {c:,} pillows: {order.number} is covered from finished-goods stock",
                  "production_order", po.id, user)
    return cut


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
