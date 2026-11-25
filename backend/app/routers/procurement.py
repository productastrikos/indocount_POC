"""Procurement & Inbound."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, require
from ..db import WRITE_LOCK, get_db
from ..services import insights, inventory, procurement as proc
from ..services.common import iso
from .core import commit_change
from .serializers import Ctx, get_or_404, lookup, po_dict, shipment_dict

router = APIRouter(prefix="/api/procurement")
OPEN_PO = ("Open", "Partially Received", "Delayed")


@router.get("/summary")
def summary(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    day0 = datetime.combine(c.now.date(), datetime.min.time())
    pos = list(db.scalars(select(m.PurchaseOrder).where(m.PurchaseOrder.factory_id.in_(c.fids), m.PurchaseOrder.status.in_(OPEN_PO))))
    ships = list(db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(c.fids))))
    expected = [s for s in ships if s.status in ("Scheduled", "In Transit") and s.expected_arrival <= c.now + timedelta(days=7)]
    pending = [s for s in ships if s.status in ("Arrived", "Receiving")]
    delayed = [s for s in ships if s.status in ("Scheduled", "In Transit") and s.original_expected_arrival
               and s.expected_arrival > s.original_expected_arrival + timedelta(hours=6)]
    receipts = list(db.scalars(select(m.GoodsReceipt).where(m.GoodsReceipt.factory_id.in_(c.fids), m.GoodsReceipt.confirmed_at >= day0)))
    mats = inventory.material_rows(db, c.fids, c.now)
    return {
        "open_pos": len(pos), "open_po_value": round(sum(p.ordered_qty * (p.unit_price or 0) for p in pos)),
        "expected_inbound": len(expected), "received_today": len(receipts),
        "received_today_lines": [{"number": r.number, "qty": r.accepted_qty, "material_id": r.material_id} for r in receipts],
        "pending_receipts": len(pending), "shortages": sum(1 for r in mats if r["status"] in ("Critical", "Low")),
        "critical": sum(1 for r in mats if r["status"] == "Critical"), "delayed": len(delayed),
    }


@router.get("/purchase-orders")
def list_pos(factory: str = "ALL", status: str | None = None, view: str | None = None, material_id: int | None = None,
             supplier_id: int | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    q = select(m.PurchaseOrder).where(m.PurchaseOrder.factory_id.in_(c.fids))
    if status:
        q = q.where(m.PurchaseOrder.status == status)
    if view == "open":
        q = q.where(m.PurchaseOrder.status.in_(OPEN_PO))
    if view == "delayed":
        q = q.where(m.PurchaseOrder.status == "Delayed")
    if material_id:
        q = q.where(m.PurchaseOrder.material_id == material_id)
    if supplier_id:
        q = q.where(m.PurchaseOrder.supplier_id == supplier_id)
    L = lookup(db)
    rows = [po_dict(p, L) for p in db.scalars(q.order_by(m.PurchaseOrder.expected_date.desc()))]
    order = {"Delayed": 0, "Open": 1, "Partially Received": 2, "Received": 3, "Cancelled": 4}
    return sorted(rows, key=lambda r: (order.get(r["status"], 5), r["expected_date"] or ""))


@router.get("/purchase-orders/{po_id}")
def po_detail(po_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    po = get_or_404(db, m.PurchaseOrder, po_id)
    L = lookup(db)
    sup = L["suppliers"][po.supplier_id]
    perf = next((s for s in insights.supplier_performance(db, [f for f in L["factories"]]) if s["supplier_id"] == sup.id), None)
    ships = [shipment_dict(s, L) for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.po_id == po.id))]
    receipts = [{"number": r.number, "received_qty": r.received_qty, "damaged_qty": r.damaged_qty, "shortage_qty": r.shortage_qty,
                 "accepted_qty": r.accepted_qty, "confirmed_at": iso(r.confirmed_at), "confirmed_by": r.confirmed_by, "notes": r.notes}
                for r in db.scalars(select(m.GoodsReceipt).where(m.GoodsReceipt.po_id == po.id))]
    disc = [{"id": d.id, "kind": d.kind, "qty": d.qty, "status": d.status, "created_at": iso(d.created_at)}
            for d in db.scalars(select(m.MaterialDiscrepancy).where(m.MaterialDiscrepancy.po_id == po.id))]
    affected = []
    skus = [s for s in L["skus"].values() if po.material_id in (s.fill_material_id, s.shell_material_id)]
    for p in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id == po.factory_id,
                                                        m.ProductionOrder.status != "Completed",
                                                        m.ProductionOrder.sku_id.in_([s.id for s in skus] or [0]))):
        affected.append({"id": p.id, "number": p.number, "sku": L["skus"][p.sku_id].code, "status": p.status,
                         "material_risk": p.material_risk, "estimated_completion": iso(p.estimated_completion)})
    return {"po": po_dict(po, L), "history": po.history or [], "notes": po.notes,
            "supplier": {"id": sup.id, "name": sup.name, "location": sup.location, "lead_time_days": sup.lead_time_days,
                         "contact": sup.contact, "performance": perf},
            "shipments": ships, "receipts": receipts, "discrepancies": disc, "affected_production_orders": affected}


class POIn(BaseModel):
    factory_id: int
    supplier_id: int
    material_id: int
    qty: float
    expected_date: datetime | None = None
    expected_days: float | None = None
    unit_price: float | None = None
    notes: str | None = None


@router.post("/purchase-orders")
def create_po(body: POIn, db: Session = Depends(get_db), user: m.User = Depends(require("procurement.write"))):
    with WRITE_LOCK:
        c = Ctx(db, None)
        expected = body.expected_date or (c.now + timedelta(days=body.expected_days or db.get(m.Supplier, body.supplier_id).lead_time_days))
        try:
            po = proc.create_po(db, body.factory_id, body.supplier_id, body.material_id, body.qty, expected, c.now,
                                user.username, body.unit_price, body.notes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "procurement")
        return po_dict(po, lookup(db))


class POUpdate(BaseModel):
    expected_date: datetime | None = None
    ordered_qty: float | None = None
    status: str | None = None
    notes: str | None = None


@router.patch("/purchase-orders/{po_id}")
def update_po(po_id: int, body: POUpdate, db: Session = Depends(get_db), user: m.User = Depends(require("procurement.write"))):
    with WRITE_LOCK:
        po = get_or_404(db, m.PurchaseOrder, po_id)
        try:
            proc.update_po(db, po, Ctx(db, None).now, user.username, **body.model_dump(exclude_none=True))
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "procurement")
        return po_dict(po, lookup(db))


@router.get("/shipments")
def shipments(factory: str = "ALL", view: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    q = select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(c.fids))
    if view == "expected":
        q = q.where(m.InboundShipment.status.in_(("Scheduled", "In Transit")))
    elif view == "pending":
        q = q.where(m.InboundShipment.status.in_(("Arrived", "Receiving")))
    elif view == "delayed":
        q = q.where(m.InboundShipment.status.in_(("Scheduled", "In Transit")),
                    m.InboundShipment.expected_arrival > m.InboundShipment.original_expected_arrival)
    elif view == "received_today":
        q = q.where(m.InboundShipment.status.in_(("Received", "Discrepancy")),
                    m.InboundShipment.actual_arrival >= c.now - timedelta(days=1))
    else:
        q = q.where((m.InboundShipment.status.notin_(("Received", "Discrepancy"))) |
                    (m.InboundShipment.actual_arrival >= c.now - timedelta(days=7)))
    L = lookup(db)
    order = {"Arrived": 0, "Receiving": 1, "In Transit": 2, "Scheduled": 3, "Discrepancy": 4, "Received": 5}
    rows = [shipment_dict(s, L) for s in db.scalars(q)]
    return sorted(rows, key=lambda r: (order.get(r["status"], 9), r["expected_arrival"] or ""))


class ArriveIn(BaseModel):
    dock: str = "R1"


@router.post("/shipments/{sid}/arrive")
def arrive(sid: int, body: ArriveIn, db: Session = Depends(get_db), user: m.User = Depends(require("receiving.write"))):
    with WRITE_LOCK:
        s = get_or_404(db, m.InboundShipment, sid)
        try:
            proc.mark_arrived(db, s, body.dock, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "procurement")
        return shipment_dict(s, lookup(db))


@router.post("/shipments/{sid}/receipt/start")
def start_receipt(sid: int, db: Session = Depends(get_db), user: m.User = Depends(require("receiving.write"))):
    with WRITE_LOCK:
        s = get_or_404(db, m.InboundShipment, sid)
        try:
            proc.start_receipt(db, s, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "procurement", recompute=False)
        return shipment_dict(s, lookup(db))


class ScanIn(BaseModel):
    qty: float = 0
    damaged: float = 0


@router.post("/shipments/{sid}/receipt/scan")
def scan(sid: int, body: ScanIn, db: Session = Depends(get_db), user: m.User = Depends(require("receiving.write"))):
    with WRITE_LOCK:
        s = get_or_404(db, m.InboundShipment, sid)
        try:
            proc.scan(s, body.qty, body.damaged)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db.commit()
        return shipment_dict(s, lookup(db))


class ConfirmIn(BaseModel):
    received_qty: float
    damaged_qty: float = 0
    notes: str | None = None


@router.post("/shipments/{sid}/receipt/confirm")
def confirm(sid: int, body: ConfirmIn, db: Session = Depends(get_db), user: m.User = Depends(require("receiving.write"))):
    with WRITE_LOCK:
        s = get_or_404(db, m.InboundShipment, sid)
        if s.status not in ("Arrived", "Receiving"):
            raise HTTPException(400, f"Shipment is {s.status}; it must be arrived before goods receipt")
        try:
            gr = proc.confirm_receipt(db, s, body.received_qty, body.damaged_qty, Ctx(db, None).now, user.username, body.notes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "procurement")
        stock = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == s.factory_id,
                                                        m.MaterialStock.material_id == s.material_id))
        return {"receipt": {"number": gr.number, "expected_qty": gr.expected_qty, "received_qty": gr.received_qty,
                            "damaged_qty": gr.damaged_qty, "shortage_qty": gr.shortage_qty, "accepted_qty": gr.accepted_qty},
                "shipment": shipment_dict(s, lookup(db)), "stock_on_hand": round(stock.on_hand, 1)}


@router.get("/materials")
def materials(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    return inventory.material_rows(db, c.fids, c.now)


@router.get("/materials/{material_id}")
def material_detail(material_id: int, factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    mat = get_or_404(db, m.Material, material_id)
    L = lookup(db)
    rows = [r for r in inventory.material_rows(db, c.fids, c.now) if r["material_id"] == material_id]
    pos = [po_dict(p, L) for p in db.scalars(select(m.PurchaseOrder).where(
        m.PurchaseOrder.material_id == material_id, m.PurchaseOrder.factory_id.in_(c.fids)).order_by(m.PurchaseOrder.expected_date.desc()).limit(12))]
    skus = [s for s in L["skus"].values() if material_id in (s.fill_material_id, s.shell_material_id, s.bag_material_id,
                                                             s.carton_material_id, s.label_material_id)]
    consumers = [{"id": p.id, "number": p.number, "sku": L["skus"][p.sku_id].code, "line": L["lines"][p.line_id].name if p.line_id else None,
                  "remaining": p.required_qty - p.completed_qty, "status": p.status, "material_risk": p.material_risk,
                  "estimated_completion": iso(p.estimated_completion)}
                 for p in db.scalars(select(m.ProductionOrder).where(
                     m.ProductionOrder.factory_id.in_(c.fids), m.ProductionOrder.status != "Completed",
                     m.ProductionOrder.sku_id.in_([s.id for s in skus] or [0])))]
    suppliers = [{"id": s.id, "name": s.name, "lead_time_days": s.lead_time_days} for s in L["suppliers"].values()
                 if mat.code in (s.material_codes or [])]
    return {"material": {"id": mat.id, "code": mat.code, "name": mat.name, "uom": mat.uom, "category": mat.category,
                         "unit_cost": mat.unit_cost},
            "stock": rows, "purchase_orders": pos, "consumers": consumers, "suppliers": suppliers,
            "skus": [s.code for s in skus]}


@router.get("/suppliers")
def suppliers(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    return insights.supplier_performance(db, c.fids)


@router.get("/discrepancies")
def discrepancies(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    out = []
    for d in db.scalars(select(m.MaterialDiscrepancy).where(m.MaterialDiscrepancy.factory_id.in_(c.fids))
                        .order_by(m.MaterialDiscrepancy.created_at.desc()).limit(50)):
        po = db.get(m.PurchaseOrder, d.po_id)
        out.append({"id": d.id, "kind": d.kind, "qty": d.qty, "status": d.status, "created_at": iso(d.created_at),
                    "po": po.number, "po_id": po.id, "material": L["materials"][d.material_id].code,
                    "uom": L["materials"][d.material_id].uom, "supplier": L["suppliers"][po.supplier_id].name})
    return out
