"""Purchase orders, inbound shipments and the goods-receipt workflow.
Inventory is updated only when a goods receipt is confirmed."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .. import models as m
from .common import log_event, next_number

OPEN_PO = ("Open", "Partially Received", "Delayed")
OPEN_SHIP = ("Scheduled", "In Transit", "Arrived", "Receiving")
CLOSED_SHORT = "Closed short"
# An ETA that moved out by more than this counts as a delayed delivery (KPI, list, supplier reliability).
DELAY_TOLERANCE = timedelta(hours=6)
EXPECTED_WINDOW = timedelta(days=7)


def is_delayed(s: m.InboundShipment) -> bool:
    return bool(s.status in ("Scheduled", "In Transit") and s.original_expected_arrival and s.expected_arrival
                and s.expected_arrival > s.original_expected_arrival + DELAY_TOLERANCE)


def expected_clause(now: datetime):
    S = m.InboundShipment
    return and_(S.status.in_(("Scheduled", "In Transit")), S.expected_arrival <= now + EXPECTED_WINDOW)


def pending_qty(po: m.PurchaseOrder) -> float:
    """Quantity still to be delivered on an open purchase order."""
    return max(0.0, po.ordered_qty - po.received_qty) if po.status in OPEN_PO else 0.0


def _history(po: m.PurchaseOrder, ts: datetime, event: str, user: str) -> None:
    po.history = [*(po.history or []), {"ts": ts.isoformat(timespec="minutes"), "event": event, "by": user}]


def settle_status(db: Session, po: m.PurchaseOrder, now: datetime, user: str) -> None:
    """Received in full -> Received; short with another shipment still open -> Partially Received;
    short with nothing left in the pipeline (no backorder) -> Closed short, so it stops counting as open."""
    if po.status in ("Cancelled", CLOSED_SHORT):
        return
    if po.received_qty >= po.ordered_qty - 0.5:
        po.status = "Received"
        return
    still_open = db.scalar(select(m.InboundShipment.id).where(m.InboundShipment.po_id == po.id,
                                                              m.InboundShipment.status.in_(OPEN_SHIP)).limit(1))
    if still_open:
        if po.received_qty > 0 and po.status != "Delayed":
            po.status = "Partially Received"
        return
    mat = db.get(m.Material, po.material_id)
    short = po.ordered_qty - po.received_qty
    po.status = CLOSED_SHORT
    msg = (f"Closed short: {short:,.0f} {mat.uom} not delivered (shortage / damage) and no further shipment is "
           f"open; raise a new PO if the quantity is still needed")
    _history(po, now, msg, user)
    log_event(db, now, po.factory_id, "procurement", "po_closed_short", f"{po.number} {msg[0].lower()}{msg[1:]}",
              "purchase_order", po.id, user)


def close_short_pos(db: Session, now: datetime, factory_ids: list[int] | None = None, user: str = "system") -> int:
    """Short-close open POs that have been received against and have no shipment left in the pipeline
    (e.g. history from before short-closing existed). Returns how many were closed."""
    q = select(m.PurchaseOrder).where(m.PurchaseOrder.status.in_(OPEN_PO), m.PurchaseOrder.received_qty > 0)
    if factory_ids:
        q = q.where(m.PurchaseOrder.factory_id.in_(factory_ids))
    n = 0
    for po in db.scalars(q).all():
        settle_status(db, po, now, user)
        n += po.status == CLOSED_SHORT
    return n


def create_po(db: Session, factory_id: int, supplier_id: int, material_id: int, qty: float,
              expected: datetime, now: datetime, user: str, unit_price: float | None = None,
              notes: str | None = None) -> m.PurchaseOrder:
    mat = db.get(m.Material, material_id)
    sup = db.get(m.Supplier, supplier_id)
    if not mat or not sup:
        raise ValueError("Unknown material or supplier")
    if qty <= 0:
        raise ValueError("Quantity must be positive")
    po = m.PurchaseOrder(number=next_number(db, m.PurchaseOrder, "PO-", 45000), factory_id=factory_id,
                         supplier_id=supplier_id, material_id=material_id, ordered_qty=qty,
                         unit_price=unit_price if unit_price is not None else mat.unit_cost,
                         order_date=now, expected_date=expected, status="Open", created_by=user, notes=notes, history=[])
    db.add(po)
    db.flush()
    _history(po, now, f"PO created for {qty:,.0f} {mat.uom} {mat.code}", user)
    ship = m.InboundShipment(ref=next_number(db, m.InboundShipment, "INB-", 8000), bol_number=f"BOL{now:%y%m}{po.id:05d}",
                             carrier="Supplier freight", po_id=po.id, factory_id=factory_id, supplier_id=supplier_id,
                             material_id=material_id, qty=qty, expected_arrival=expected,
                             original_expected_arrival=expected, status="Scheduled")
    db.add(ship)
    log_event(db, now, factory_id, "procurement", "po_created",
              f"{po.number} raised with {sup.name}: {qty:,.0f} {mat.uom} {mat.code}, due {expected:%b %d}", "purchase_order", po.id, user)
    return po


def update_po(db: Session, po: m.PurchaseOrder, now: datetime, user: str, **changes) -> m.PurchaseOrder:
    notes = []
    if changes.get("expected_date"):
        new = changes["expected_date"]
        for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.po_id == po.id,
                                                            m.InboundShipment.status.in_(("Scheduled", "In Transit")))):
            s.expected_arrival = new
        notes.append(f"expected date → {new:%b %d %H:%M}")
        po.expected_date = new
    if changes.get("ordered_qty"):
        q = float(changes["ordered_qty"])
        if q < po.received_qty:
            raise ValueError("Ordered quantity cannot be below the received quantity")
        notes.append(f"quantity {po.ordered_qty:,.0f} → {q:,.0f}")
        delta = q - po.ordered_qty
        po.ordered_qty = q
        s = db.scalar(select(m.InboundShipment).where(m.InboundShipment.po_id == po.id,
                                                      m.InboundShipment.status.in_(("Scheduled", "In Transit"))))
        if s:
            s.qty = max(0.0, s.qty + delta)
    if changes.get("status") == "Cancelled":
        po.status = "Cancelled"
        notes.append("cancelled")
    if changes.get("notes") is not None:
        po.notes = changes["notes"]
    if notes:
        _history(po, now, "Updated: " + ", ".join(notes), user)
        log_event(db, now, po.factory_id, "procurement", "po_updated", f"{po.number} " + ", ".join(notes), "purchase_order", po.id, user)
    return po


def mark_arrived(db: Session, s: m.InboundShipment, dock: str, now: datetime, user: str) -> None:
    if s.status not in ("Scheduled", "In Transit"):
        raise ValueError(f"Shipment is already {s.status}")
    s.status = "Arrived"
    s.actual_arrival = now
    s.dock = dock or s.dock or "R1"
    _history(s.po, now, f"{s.ref} arrived at dock {s.dock}", user)
    log_event(db, now, s.factory_id, "procurement", "shipment_arrived", f"{s.ref} marked arrived at {s.dock}", "shipment", s.id, user)


def start_receipt(db: Session, s: m.InboundShipment, now: datetime, user: str) -> None:
    if s.status != "Arrived":
        raise ValueError("Goods receipt can only start for an arrived shipment")
    s.status = "Receiving"
    s.scanned_qty = 0
    s.scanned_damaged = 0
    log_event(db, now, s.factory_id, "procurement", "receipt_started", f"Receiving started for {s.ref}", "shipment", s.id, user)


def scan(s: m.InboundShipment, qty: float, damaged: float = 0.0) -> None:
    if s.status != "Receiving":
        raise ValueError("Start the goods receipt before scanning")
    s.scanned_qty = max(0.0, (s.scanned_qty or 0) + qty)
    s.scanned_damaged = max(0.0, (s.scanned_damaged or 0) + damaged)


def confirm_receipt(db: Session, s: m.InboundShipment, received: float, damaged: float, now: datetime,
                    user: str, notes: str | None = None) -> m.GoodsReceipt:
    if s.status in ("Received", "Discrepancy"):
        raise ValueError("Shipment already received")
    if received < 0 or damaged < 0 or damaged > received:
        raise ValueError("Damaged quantity must be between 0 and the received quantity")
    po = s.po
    mat = db.get(m.Material, s.material_id)
    expected = s.qty
    shortage = max(0.0, expected - received)
    accepted = received - damaged
    gr = m.GoodsReceipt(number=next_number(db, m.GoodsReceipt, "GR-", 60000), shipment_id=s.id, po_id=po.id,
                        factory_id=s.factory_id, material_id=s.material_id, expected_qty=expected, received_qty=received,
                        damaged_qty=damaged, shortage_qty=shortage, accepted_qty=accepted, confirmed_at=now,
                        confirmed_by=user, notes=notes)
    db.add(gr)
    db.flush()
    stock = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == s.factory_id,
                                                    m.MaterialStock.material_id == s.material_id))
    if stock is None:  # first receipt of a material this plant has no stock record for
        stock = m.MaterialStock(factory_id=s.factory_id, material_id=s.material_id, on_hand=0.0, damaged=0.0,
                                avg_daily_consumption=0.0, consumed_today=0.0)
        db.add(stock)
    stock.on_hand = (stock.on_hand or 0) + accepted
    stock.damaged = (stock.damaged or 0) + damaged
    po.received_qty += accepted
    po.damaged_qty = (po.damaged_qty or 0) + damaged
    s.actual_arrival = s.actual_arrival or now
    s.status = "Discrepancy" if (shortage > 0.5 or damaged > 0) else "Received"
    if shortage > 0.5:
        db.add(m.MaterialDiscrepancy(receipt_id=gr.id, po_id=po.id, factory_id=s.factory_id, material_id=s.material_id,
                                     kind="Shortage", qty=shortage, status="Open", created_at=now))
    if damaged > 0:
        db.add(m.MaterialDiscrepancy(receipt_id=gr.id, po_id=po.id, factory_id=s.factory_id, material_id=s.material_id,
                                     kind="Damage", qty=damaged, status="Open", created_at=now))
    msg = f"{gr.number}: received {received:,.0f} of {expected:,.0f} {mat.uom} {mat.code}"
    if shortage > 0.5:
        msg += f", shortage {shortage:,.0f}"
    if damaged:
        msg += f", damaged {damaged:,.0f}"
    msg += f"; {accepted:,.0f} {mat.uom} added to stock"
    _history(po, now, msg, user)
    log_event(db, now, s.factory_id, "procurement", "goods_receipt", msg, "shipment", s.id, user)
    db.flush()
    settle_status(db, po, now, user)
    close_short_pos(db, now, [s.factory_id], user)  # also settles older POs left open with nothing in the pipeline
    return gr
