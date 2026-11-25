"""ORM -> JSON helpers shared by the routers."""
from __future__ import annotations

from datetime import date, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..services.common import factory_ids, iso, sim_now


class Ctx:
    """Common query context: factory scope, date, shift, customer."""

    def __init__(self, db: Session, factory: str | None, day: str | None = None, shift: int | None = None,
                 customer: int | None = None):
        self.db = db
        self.now = sim_now(db)
        self.fids = factory_ids(db, factory)
        if not self.fids:
            raise HTTPException(404, f"Unknown factory {factory}")
        self.factory = factory or "ALL"
        self.day = date.fromisoformat(day) if day else self.now.date()
        self.shift = shift if shift in (1, 2) else None
        self.customer = customer or None


def lookup(db: Session):
    return {
        "factories": {f.id: f for f in db.scalars(select(m.Factory))},
        "customers": {c.id: c for c in db.scalars(select(m.Customer))},
        "skus": {s.id: s for s in db.scalars(select(m.Sku))},
        "materials": {x.id: x for x in db.scalars(select(m.Material))},
        "suppliers": {x.id: x for x in db.scalars(select(m.Supplier))},
        "lines": {x.id: x for x in db.scalars(select(m.ProductionLine))},
    }


def get_or_404(db: Session, model, id_):
    obj = db.get(model, id_)
    if not obj:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


def po_dict(po: m.PurchaseOrder, L) -> dict:
    mat = L["materials"][po.material_id]
    return {"id": po.id, "number": po.number, "factory_id": po.factory_id, "factory": L["factories"][po.factory_id].code,
            "supplier_id": po.supplier_id, "supplier": L["suppliers"][po.supplier_id].name,
            "material_id": mat.id, "material": mat.name, "sku": mat.code, "uom": mat.uom,
            "ordered_qty": po.ordered_qty, "received_qty": po.received_qty, "damaged_qty": po.damaged_qty,
            "pending_qty": max(0.0, po.ordered_qty - po.received_qty) if po.status != "Cancelled" else 0,
            "order_date": iso(po.order_date), "expected_date": iso(po.expected_date), "status": po.status,
            "unit_price": po.unit_price, "value": round(po.ordered_qty * (po.unit_price or 0), 2)}


def shipment_dict(s: m.InboundShipment, L) -> dict:
    mat = L["materials"][s.material_id]
    return {"id": s.id, "ref": s.ref, "bol_number": s.bol_number, "carrier": s.carrier, "po_id": s.po_id,
            "po": s.po.number if s.po else None, "factory_id": s.factory_id, "factory": L["factories"][s.factory_id].code,
            "supplier": L["suppliers"][s.supplier_id].name if s.supplier_id else None,
            "material_id": mat.id, "material": mat.name, "sku": mat.code, "uom": mat.uom, "qty": s.qty,
            "expected_arrival": iso(s.expected_arrival), "original_expected_arrival": iso(s.original_expected_arrival),
            "actual_arrival": iso(s.actual_arrival), "dock": s.dock, "status": s.status, "delay_reason": s.delay_reason,
            "delayed": bool(s.original_expected_arrival and s.expected_arrival and s.expected_arrival > s.original_expected_arrival),
            "scanned_qty": s.scanned_qty, "scanned_damaged": s.scanned_damaged}


def prod_order_dict(po: m.ProductionOrder, L, orders=None) -> dict:
    sku = L["skus"][po.sku_id]
    o = orders.get(po.customer_order_id) if orders is not None else None
    ln = L["lines"].get(po.line_id)
    late_h = None
    if po.estimated_completion and po.required_completion:
        late_h = round((po.estimated_completion - po.required_completion).total_seconds() / 3600, 1)
    return {"id": po.id, "number": po.number, "factory_id": po.factory_id, "factory": L["factories"][po.factory_id].code,
            "customer_order_id": po.customer_order_id, "order": o.number if o else None,
            "customer": L["customers"][sku.customer_id].name, "sku_id": sku.id, "sku": sku.code, "description": sku.description,
            "required_qty": po.required_qty, "completed_qty": po.completed_qty,
            "remaining_qty": max(0, po.required_qty - po.completed_qty), "packed_qty": po.packed_qty,
            "pack_queue": po.pack_queue, "held_qty": po.held_qty, "stored_qty": po.stored_qty,
            "progress_pct": round(100 * po.completed_qty / po.required_qty, 1) if po.required_qty else 0,
            "start_date": iso(po.start_date), "required_completion": iso(po.required_completion),
            "estimated_completion": iso(po.estimated_completion), "late_hours": late_h,
            "priority": po.priority, "line_id": po.line_id, "line": ln.name if ln else None, "sequence": po.sequence,
            "status": po.status, "material_risk": po.material_risk, "risk_reason": po.risk_reason,
            "started_at": iso(po.started_at), "completed_at": iso(po.completed_at)}


def carton_dict(c: m.Carton, L) -> dict:
    sku = L["skus"][c.sku_id]
    return {"id": c.id, "carton_code": c.carton_code, "hold_code": c.hold_code, "code": c.carton_code or c.hold_code,
            "factory": L["factories"][c.factory_id].code, "line_id": c.line_id, "line": L["lines"][c.line_id].name if c.line_id else None,
            "po_id": c.po_id, "sku": sku.code, "customer": L["customers"][c.customer_id].name,
            "required": c.required_count, "detected": c.detected_count, "status": c.status, "result": c.result,
            "defect": c.defect_category, "first_pass": c.first_pass, "created_at": iso(c.created_at),
            "approved_at": iso(c.approved_at), "approved_by": c.approved_by, "override_reason": c.override_reason,
            "pallet_id": c.pallet_id, "source": c.inference_source, "confidence": c.confidence,
            "carton_type": sku.carton_type, "packaging_type": sku.packaging_type}


def pallet_dict(p: m.Pallet, L) -> dict:
    sku = L["skus"][p.sku_id]
    return {"id": p.id, "code": p.code, "factory": L["factories"][p.factory_id].code, "sku_id": sku.id, "sku": sku.code,
            "customer": L["customers"][sku.customer_id].name, "po_id": p.po_id, "batch_no": p.batch_no,
            "cartons": p.cartons, "pillows": p.cartons * sku.pillows_per_carton, "capacity": p.capacity,
            "allocated_cartons": p.allocated_cartons, "status": p.status,
            "location": p.location.code if p.location else None, "location_id": p.location_id,
            "created_at": iso(p.created_at), "closed_at": iso(p.closed_at), "stored_at": iso(p.stored_at),
            "carton_type": sku.carton_type}


def truck_dict(t: m.TruckAppointment, L, now: datetime) -> dict:
    o = t.order
    wait = t.wait_min
    if t.status == "Arrived" and t.actual_arrival:
        wait = round((now - t.actual_arrival).total_seconds() / 60, 1)
    return {"id": t.id, "ref": t.ref, "carrier": t.carrier, "factory_id": t.factory_id, "factory": L["factories"][t.factory_id].code,
            "customer": L["customers"][t.customer_id].name if t.customer_id else None, "order_id": t.order_id,
            "order": o.number if o else None, "dock": t.dock, "scheduled_arrival": iso(t.scheduled_arrival),
            "expected_loading_min": t.expected_loading_min, "actual_arrival": iso(t.actual_arrival),
            "loading_start": iso(t.loading_start), "loading_end": iso(t.loading_end), "departed_at": iso(t.departed_at),
            "loaded_qty": t.loaded_qty, "status": t.status, "wait_min": wait, "detention_charge": t.detention_charge,
            "delay_reason": t.delay_reason}


def alert_dict(a: m.Alert, L) -> dict:
    page = {"procurement": "procurement", "production": "production", "quality": "quality",
            "warehouse": "warehouse", "dispatch": "dispatch"}.get(a.stage, "overview")
    return {"id": a.id, "key": a.key, "severity": a.severity, "factory_id": a.factory_id,
            "factory": L["factories"][a.factory_id].name if a.factory_id else None, "stage": a.stage, "page": page,
            "title": a.title, "description": a.description, "recommended_action": a.recommended_action,
            "entity_type": a.entity_type, "entity_id": a.entity_id, "created_at": iso(a.created_at),
            "updated_at": iso(a.updated_at), "status": a.status, "acknowledged_by": a.acknowledged_by}
