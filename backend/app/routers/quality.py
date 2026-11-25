"""Quality & Packaging."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user, has_perm, require
from ..cv import inference as cv
from ..db import WRITE_LOCK, get_db
from ..services import quality as qs
from ..services.common import iso, pct
from .core import commit_change
from .serializers import Ctx, carton_dict, get_or_404, lookup

router = APIRouter(prefix="/api/quality")
WORKFLOW = ["Pillow Received", "Sewing Inspection", "Pillow Counting", "Compression", "Polybag Packaging",
            "Carton Packing", "Final Approval"]


@router.get("/summary")
def summary(factory: str = "ALL", date: str | None = None, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory, date)
    day0 = datetime.combine(c.day, datetime.min.time())
    day1 = day0 + timedelta(days=1)
    counts = qs.quality_counts(db, c.fids, day0, day1)
    received = float(db.scalar(select(func.sum(m.ProductionHourly.actual)).where(
        m.ProductionHourly.factory_id.in_(c.fids), m.ProductionHourly.date == c.day)) or 0)
    in_cartons = float(db.scalar(select(func.sum(m.Carton.detected_count)).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.created_at >= day0, m.Carton.created_at < day1)) or 0)
    approved = db.scalar(select(func.count()).select_from(m.Carton).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.approved_at >= day0, m.Carton.approved_at < day1)) or 0
    queue = int(db.scalar(select(func.sum(m.ProductionOrder.pack_queue)).where(m.ProductionOrder.factory_id.in_(c.fids))) or 0)
    defects = Counter(d.category for d in db.scalars(select(m.DefectRecord).where(
        m.DefectRecord.factory_id.in_(c.fids), m.DefectRecord.created_at >= day0, m.DefectRecord.created_at < day1)))
    rework_qty = int(db.scalar(select(func.coalesce(func.sum(m.Carton.required_count), 0)).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.status == "Reworked", m.Carton.created_at >= day0)) or 0)
    rejected_qty = int(db.scalar(select(func.coalesce(func.sum(m.Carton.detected_count), 0)).where(
        m.Carton.factory_id.in_(c.fids), m.Carton.status == "Rejected", m.Carton.created_at >= day0)) or 0)
    sewing_issues = defects.get("Sewing defect", 0)
    count_issues = defects.get("Underpacked", 0) + defects.get("Overpacked", 0)
    stages = [
        ("Pillow Received", int(received), "pillows", "Normal"),
        ("Sewing Inspection", max(0, int(received) - sewing_issues), "pillows", "Attention" if sewing_issues else "Normal"),
        ("Pillow Counting", int(in_cartons), "pillows", "Attention" if count_issues else "Normal"),
        ("Compression", int(in_cartons), "pillows", "Attention" if defects.get("Compression failure") else "Normal"),
        ("Polybag Packaging", int(in_cartons), "pillows", "Attention" if defects.get("Polybag seal failure") else "Normal"),
        ("Carton Packing", counts["inspected"], "cartons", "Attention" if defects.get("Label mismatch") else "Normal"),
        ("Final Approval", approved, "cartons", "Blocked" if counts["blocked"] else "Normal"),
    ]
    return {"workflow": [{"stage": s, "qty": q, "unit": u, "status": st} for s, q, u, st in stages],
            "counts": counts, "pack_queue": queue, "rework_qty": rework_qty, "rejected_qty": rejected_qty,
            "defects_today": dict(defects)}


@router.get("/stations")
def stations(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    day0 = datetime.combine(c.now.date(), datetime.min.time())
    out = []
    for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(c.fids)).order_by(m.ProductionLine.id)):
        po = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
        sku = L["skus"].get(po.sku_id if po else ln.current_sku_id)
        recent = list(db.scalars(select(m.Carton).where(m.Carton.line_id == ln.id).order_by(m.Carton.created_at.desc()).limit(6)))
        today = db.execute(select(m.Carton.status, func.count()).where(m.Carton.line_id == ln.id, m.Carton.created_at >= day0)
                           .group_by(m.Carton.status)).all()
        cnt = {s: n for s, n in today}
        queue = int(db.scalar(select(func.sum(m.ProductionOrder.pack_queue)).where(m.ProductionOrder.line_id == ln.id)) or 0)
        out.append({"line_id": ln.id, "line": ln.name, "factory": L["factories"][ln.factory_id].code, "line_status": ln.status,
                    "po": po.number if po else None, "po_id": po.id if po else None,
                    "sku": sku.code if sku else None, "customer": L["customers"][sku.customer_id].name if sku else None,
                    "spec": {"pillows_per_carton": sku.pillows_per_carton, "packaging_type": sku.packaging_type,
                             "carton_type": sku.carton_type, "label_requirements": sku.label_requirements} if sku else None,
                    "current_count": (queue % sku.pillows_per_carton) if sku else 0, "queue": queue,
                    "cartons_today": sum(cnt.values()), "blocked": cnt.get("Blocked", 0),
                    "accuracy_pct": pct(cnt.get("Passed", 0), sum(cnt.values())) if cnt else 100.0,
                    "recent": [carton_dict(x, L) for x in recent]})
    return out


@router.post("/stations/{line_id}/pack")
def pack_manual(line_id: int, body: dict, db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    """Simulate one carton at the packing station with a chosen detected count."""
    if not (has_perm(user, "quality.write") or has_perm(user, "simulation")):
        raise HTTPException(403, "Requires quality permissions")
    with WRITE_LOCK:
        ln = get_or_404(db, m.ProductionLine, line_id)
        po = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
        if not po:
            raise HTTPException(400, f"{ln.name} has no active production order")
        sku = db.get(m.Sku, po.sku_id)
        detected = int(body.get("detected", sku.pillows_per_carton))
        if detected < 0 or detected > sku.pillows_per_carton * 2:
            raise HTTPException(400, "Detected count out of range")
        if po.pack_queue < detected:
            # Simulated station: pull the next pillows straight from the line's active order.
            short = detected - po.pack_queue
            if po.completed_qty + short > po.required_qty:
                raise HTTPException(400, f"{po.number} has only {po.pack_queue} pillows left to pack")
            po.completed_qty += short
            po.pack_queue += short
        carton = qs.pack_carton(db, po, ln, Ctx(db, None).now, detected, body.get("defect"), "simulated", 0.97)
        commit_change(db, "quality")
        return carton_dict(carton, lookup(db))


@router.get("/cameras")
def cameras(factory: str = "ALL", db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    out = []
    for cam in db.scalars(select(m.Camera).where(m.Camera.factory_id.in_(c.fids)).order_by(m.Camera.id)):
        ln = L["lines"].get(cam.line_id)
        sku = L["skus"].get(ln.current_sku_id) if ln else None
        out.append({"id": cam.id, "code": cam.code, "name": cam.name, "station": cam.station, "factory": L["factories"][cam.factory_id].code,
                    "line_id": cam.line_id, "line": ln.name if ln else "All lines", "sku": sku.code if sku else None,
                    "status": cam.status, "mode": cam.mode, "inspections_today": cam.inspections_today,
                    "detections_today": cam.detections_today, "last_event_at": iso(cam.last_event_at),
                    "provider": cv.provider().name})
    return out


def _latest_carton(db: Session, cam: m.Camera):
    q = select(m.Carton).where(m.Carton.factory_id == cam.factory_id)
    if cam.line_id:
        q = q.where(m.Carton.line_id == cam.line_id)
    return db.scalars(q.order_by(m.Carton.created_at.desc(), m.Carton.id.desc()).limit(1)).first()


@router.get("/cameras/{cam_id}")
def camera_detail(cam_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    cam = get_or_404(db, m.Camera, cam_id)
    L = lookup(db)
    ev = list(db.scalars(select(m.DetectionEvent).where(m.DetectionEvent.camera_id == cam.id)
                         .order_by(m.DetectionEvent.ts.desc(), m.DetectionEvent.id.desc()).limit(25)))
    if not ev and cam.station in ("Final inspection",):
        ev = list(db.scalars(select(m.DetectionEvent).join(m.Camera).where(m.Camera.factory_id == cam.factory_id)
                             .order_by(m.DetectionEvent.ts.desc()).limit(25)))
    latest = _latest_carton(db, cam)
    ln = L["lines"].get(cam.line_id)
    return {"camera": {"id": cam.id, "code": cam.code, "name": cam.name, "station": cam.station, "status": cam.status,
                       "line": ln.name if ln else "All lines", "sku": L["skus"][ln.current_sku_id].code if ln and ln.current_sku_id else None,
                       "inspections_today": cam.inspections_today, "detections_today": cam.detections_today,
                       "provider": cv.provider().name, "opencv_available": cv.HAVE_CV2},
            "events": [{"ts": iso(e.ts), "type": e.event_type, "label": e.label, "confidence": e.confidence, "count": e.count,
                        "severity": e.severity} for e in ev],
            "latest_carton": carton_dict(latest, L) if latest else None,
            "note": "Simulated camera: frames are generated and analysed by the configured inference provider."}


@router.get("/cameras/{cam_id}/frame")
def camera_frame(cam_id: int, db: Session = Depends(get_db)):
    cam = get_or_404(db, m.Camera, cam_id)
    latest = _latest_carton(db, cam)
    carton = None
    if latest:
        sku = db.get(m.Sku, latest.sku_id)
        carton = {"required": latest.required_count, "detected": latest.detected_count, "sku": sku.code,
                  "code": latest.carton_code or latest.hold_code}
    img, meta = cv.camera_frame(cam.station, carton)
    if not img:
        return Response(status_code=204, headers={"X-CV-Provider": "simulated"})
    return Response(content=img, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-CV-Provider": meta.get("provider", ""),
                             "X-Detected-Count": str(meta.get("count", "")), "Access-Control-Expose-Headers": "X-CV-Provider, X-Detected-Count"})


@router.get("/specs")
def specs(db: Session = Depends(get_db), user=Depends(current_user)):
    L = lookup(db)
    return [{"id": s.id, "code": s.code, "customer": L["customers"][s.customer_id].name, "description": s.description,
             "size": s.size, "pillows_per_carton": s.pillows_per_carton, "packaging_type": s.packaging_type,
             "carton_type": s.carton_type, "label_requirements": s.label_requirements, "cartons_per_pallet": s.cartons_per_pallet,
             "fill": L["materials"][s.fill_material_id].code, "fill_kg": s.fill_kg} for s in L["skus"].values()]


class SpecIn(BaseModel):
    pillows_per_carton: int | None = None
    packaging_type: str | None = None
    carton_type: str | None = None
    label_requirements: str | None = None


@router.put("/specs/{sku_id}")
def update_spec(sku_id: int, body: SpecIn, db: Session = Depends(get_db), user=Depends(require("quality.write"))):
    with WRITE_LOCK:
        s = get_or_404(db, m.Sku, sku_id)
        for k, v in body.model_dump(exclude_none=True).items():
            if k == "pillows_per_carton" and not (1 <= v <= 120):
                raise HTTPException(400, "Pillows per carton must be 1–120")
            setattr(s, k, v)
        commit_change(db, "quality", recompute=False)
        return {"ok": True}


@router.get("/cartons")
def cartons(factory: str = "ALL", status: str | None = None, limit: int = 60, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    q = select(m.Carton).where(m.Carton.factory_id.in_(c.fids))
    if status:
        q = q.where(m.Carton.status == status)
    L = lookup(db)
    return [carton_dict(x, L) for x in db.scalars(q.order_by(m.Carton.created_at.desc(), m.Carton.id.desc()).limit(min(limit, 300)))]


@router.get("/cartons/{carton_id}")
def carton_detail(carton_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    c = get_or_404(db, m.Carton, carton_id)
    L = lookup(db)
    defects = [{"category": d.category, "qty": d.qty, "disposition": d.disposition, "status": d.status,
                "created_at": iso(d.created_at), "resolved_by": d.resolved_by, "notes": d.notes}
               for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.carton_id == c.id))]
    pallet = db.get(m.Pallet, c.pallet_id) if c.pallet_id else None
    return {"carton": carton_dict(c, L), "defects": defects,
            "pallet": {"code": pallet.code, "status": pallet.status, "location": pallet.location.code if pallet.location else None} if pallet else None}


class OverrideIn(BaseModel):
    reason: str


@router.post("/cartons/{carton_id}/rework")
def rework(carton_id: int, db: Session = Depends(get_db), user: m.User = Depends(require("quality.write"))):
    with WRITE_LOCK:
        c = get_or_404(db, m.Carton, carton_id)
        try:
            qs.rework(db, c, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "quality")
        return carton_dict(c, lookup(db))


@router.post("/cartons/{carton_id}/override")
def override(carton_id: int, body: OverrideIn, db: Session = Depends(get_db), user: m.User = Depends(require("quality.override"))):
    with WRITE_LOCK:
        c = get_or_404(db, m.Carton, carton_id)
        try:
            qs.override(db, c, Ctx(db, None).now, user.username, body.reason)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "quality")
        return carton_dict(c, lookup(db))


@router.post("/cartons/{carton_id}/reject")
def reject(carton_id: int, db: Session = Depends(get_db), user: m.User = Depends(require("quality.write"))):
    with WRITE_LOCK:
        c = get_or_404(db, m.Carton, carton_id)
        try:
            qs.reject(db, c, Ctx(db, None).now, user.username)
        except ValueError as e:
            raise HTTPException(400, str(e))
        commit_change(db, "quality")
        return carton_dict(c, lookup(db))


@router.get("/defects")
def defects(factory: str = "ALL", days: int = 2, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    since = c.now - timedelta(days=days)
    out = []
    for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.factory_id.in_(c.fids), m.DefectRecord.created_at >= since)
                        .order_by(m.DefectRecord.created_at.desc()).limit(200)):
        carton = db.get(m.Carton, d.carton_id) if d.carton_id else None
        out.append({"id": d.id, "time": iso(d.created_at), "carton_id": d.carton_id,
                    "carton": (carton.carton_code or carton.hold_code) if carton else None,
                    "sku": L["skus"][d.sku_id].code, "customer": L["customers"][d.customer_id].name,
                    "line": L["lines"][d.line_id].name if d.line_id else None, "factory": L["factories"][d.factory_id].code,
                    "category": d.category, "qty": d.qty, "disposition": d.disposition, "status": d.status,
                    "resolved_by": d.resolved_by})
    return out


@router.get("/analytics")
def analytics(factory: str = "ALL", days: int = 14, db: Session = Depends(get_db), user=Depends(current_user)):
    c = Ctx(db, factory)
    L = lookup(db)
    trend = []
    hist = defaultdict(lambda: [0, 0])
    for s in db.scalars(select(m.ShiftSummary).where(m.ShiftSummary.factory_id.in_(c.fids),
                                                     m.ShiftSummary.date >= c.now.date() - timedelta(days=days))):
        hist[s.date][0] += s.cartons_inspected
        hist[s.date][1] += s.cartons_passed
    for d in sorted(hist):
        if d < c.now.date():
            i, p = hist[d]
            trend.append({"date": d.isoformat(), "accuracy_pct": round(100 * p / i, 2) if i else None, "inspected": i})
    day0 = datetime.combine(c.now.date(), datetime.min.time())
    today = qs.quality_counts(db, c.fids, day0)
    trend.append({"date": c.now.date().isoformat(), "accuracy_pct": today["accuracy_pct"], "inspected": today["inspected"], "live": True})
    since = c.now - timedelta(days=7)
    dist = Counter()
    for s in db.scalars(select(m.ShiftSummary).where(m.ShiftSummary.factory_id.in_(c.fids), m.ShiftSummary.date >= since.date(),
                                                     m.ShiftSummary.date < c.now.date())):
        for k, v in (s.defects or {}).items():
            dist[k] += v
    for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.factory_id.in_(c.fids), m.DefectRecord.created_at >= day0)):
        dist[d.category] += 1
    by_cust = defaultdict(lambda: [0, 0])
    by_line = defaultdict(lambda: [0, 0])
    for cid, lid, fp, n in db.execute(select(m.Carton.customer_id, m.Carton.line_id, m.Carton.first_pass, func.count()).where(
            m.Carton.factory_id.in_(c.fids), m.Carton.created_at >= since).group_by(m.Carton.customer_id, m.Carton.line_id, m.Carton.first_pass)):
        by_cust[cid][0] += n
        by_line[lid][0] += n
        if not fp:
            by_cust[cid][1] += n
            by_line[lid][1] += n
    return {
        "accuracy_trend": trend,
        "defect_distribution": [{"category": k, "count": v} for k, v in dist.most_common()],
        "by_customer": sorted([{"customer": L["customers"][k].name, "inspected": v[0], "errors": v[1],
                                "error_rate_pct": round(100 * v[1] / v[0], 2) if v[0] else 0} for k, v in by_cust.items()],
                              key=lambda r: -r["error_rate_pct"]),
        "by_line": sorted([{"line": f"{L['lines'][k].name} ({L['factories'][L['lines'][k].factory_id].code})", "line_id": k,
                            "inspected": v[0], "errors": v[1], "defect_rate_pct": round(100 * v[1] / v[0], 2) if v[0] else 0}
                           for k, v in by_line.items() if k], key=lambda r: -r["defect_rate_pct"]),
    }
