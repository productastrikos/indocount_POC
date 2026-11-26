"""Carton validation workflow.

1. Identify the active SKU on the packing station (the line's production order)
2. Retrieve its packaging specification (pillows per carton, carton type…)
3. Count pillows entering the carton (computer vision / simulated inference)
4. Compare with the spec; correct -> approve and assign a carton ID,
   mismatch -> block (red alert) until rework or a supervisor override.
Only approved cartons reach pallets and finished-goods inventory."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from .common import log_event, next_number
from .inventory import new_pallet

DEFECT_CATEGORIES = ["Underpacked", "Overpacked", "Sewing defect", "Label mismatch",
                     "Polybag seal failure", "Compression failure", "Wrong carton type"]


def _consume_packaging(db: Session, factory_id: int, sku: m.Sku, pillows: int) -> None:
    for mid, qty in ((sku.bag_material_id, pillows), (sku.carton_material_id, 1), (sku.label_material_id, 1)):
        st = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == factory_id,
                                                     m.MaterialStock.material_id == mid))
        if st:
            st.on_hand = max(0.0, st.on_hand - qty)
            st.consumed_today = (st.consumed_today or 0) + qty


def packaging_available(db: Session, factory_id: int, sku: m.Sku) -> bool:
    for mid, qty in ((sku.carton_material_id, 1), (sku.bag_material_id, sku.pillows_per_carton)):
        st = db.scalar(select(m.MaterialStock).where(m.MaterialStock.factory_id == factory_id,
                                                     m.MaterialStock.material_id == mid))
        if st and st.on_hand < qty:
            return False
    return True


def _camera(db: Session, factory_id: int, station: str, line_id: int | None = None):
    q = select(m.Camera).where(m.Camera.factory_id == factory_id, m.Camera.station == station)
    cams = list(db.scalars(q))
    if line_id:
        for c in cams:
            if c.line_id == line_id:
                return c
    return cams[0] if cams else None


def _detect(db, cam, now, etype, label, conf, carton=None, count=None, severity="info"):
    if not cam:
        return
    db.add(m.DetectionEvent(camera_id=cam.id, ts=now, event_type=etype, label=label, confidence=conf,
                            carton_id=carton.id if carton else None, count=count, severity=severity))
    cam.detections_today = (cam.detections_today or 0) + 1
    cam.last_event_at = now


def add_to_pallet(db: Session, carton: m.Carton, po: m.ProductionOrder, now: datetime) -> None:
    pallet = db.scalar(select(m.Pallet).where(m.Pallet.po_id == po.id, m.Pallet.status == "Open"))
    if not pallet:
        pallet = new_pallet(db, po, now)
    pallet.cartons += 1
    carton.pallet_id = pallet.id
    if pallet.cartons >= pallet.capacity:
        pallet.status = "Pending Put-away"
        pallet.closed_at = now


def close_open_pallet(db: Session, po: m.ProductionOrder, now: datetime) -> None:
    pallet = db.scalar(select(m.Pallet).where(m.Pallet.po_id == po.id, m.Pallet.status == "Open"))
    if pallet and pallet.cartons > 0:
        pallet.status = "Pending Put-away"
        pallet.closed_at = now


def approve(db: Session, carton: m.Carton, po: m.ProductionOrder, now: datetime, user: str, status: str) -> None:
    carton.status = status
    carton.carton_code = f"CTN-{db.get(m.Factory, carton.factory_id).code}-{carton.id:07d}"
    carton.approved_at = now
    carton.approved_by = user
    pillows = carton.detected_count if status == "Overridden" else carton.required_count
    po.packed_qty += pillows
    add_to_pallet(db, carton, po, now)


def pack_carton(db: Session, po: m.ProductionOrder, line: m.ProductionLine, now: datetime,
                detected: int | None = None, defect: str | None = None, source: str = "edge-vision",
                confidence: float = 0.97) -> m.Carton:
    """Create one carton from the packing queue and validate it.

    A carton can only hold pillows that are waiting at the station: a count above the packing queue
    (camera over-read, or a request for more than exists) is booked at the queue, so packed + held +
    queued never exceeds what the line completed. The carton is still validated on the count that was
    read, so a mismatch is held for a recount."""
    sku = db.get(m.Sku, po.sku_id)
    req = sku.pillows_per_carton
    seen = req if detected is None else detected
    detected = max(0, min(seen, po.pack_queue or 0))
    carton = m.Carton(factory_id=po.factory_id, line_id=line.id, po_id=po.id, sku_id=sku.id,
                      customer_id=sku.customer_id, required_count=req, detected_count=detected,
                      created_at=now, inference_source=source, confidence=confidence)
    db.add(carton)
    db.flush()
    po.pack_queue -= detected
    _consume_packaging(db, po.factory_id, sku, detected)
    cam = _camera(db, po.factory_id, "Carton packing", line.id)
    final_cam = _camera(db, po.factory_id, "Final inspection")
    if cam:
        cam.inspections_today = (cam.inspections_today or 0) + 1
    if seen != req or detected != req or defect:
        count = seen if seen != req else detected  # what the reading says, else what was physically there
        carton.result = "Underpacked" if count < req else "Overpacked" if count > req else "Defect"
        carton.defect_category = defect or carton.result
        carton.status = "Blocked"
        carton.first_pass = False
        carton.hold_code = f"HOLD-{carton.id:07d}"
        po.held_qty += detected
        db.add(m.DefectRecord(carton_id=carton.id, factory_id=po.factory_id, line_id=line.id, po_id=po.id,
                              sku_id=sku.id, customer_id=sku.customer_id, category=carton.defect_category,
                              qty=abs(req - count) or 1, disposition="Rework", status="Open", created_at=now))
        msg = (f"{carton.hold_code}: {carton.defect_category} — detected {count}/{req} pillows ({sku.code})"
               if not defect else f"{carton.hold_code}: {defect} on {sku.code}")
        if seen > detected:
            msg += f"; only {detected} pillow(s) were waiting at the station, recount required"
        _detect(db, cam, now, "mismatch", msg, confidence, carton, detected, "critical")
        log_event(db, now, po.factory_id, "quality", "carton_blocked", msg, "carton", carton.id)
    else:
        carton.result = "Correct"
        approve(db, carton, po, now, "auto-inspection", "Passed")
        _detect(db, cam, now, "count_complete", f"{carton.carton_code}: {detected}/{req} pillows — PASS", confidence, carton, detected)
    if final_cam:
        final_cam.inspections_today = (final_cam.inspections_today or 0) + 1
    return carton


def rework(db: Session, carton: m.Carton, now: datetime, user: str) -> None:
    if carton.status != "Blocked":
        raise ValueError("Only blocked cartons can be reworked")
    po = db.get(m.ProductionOrder, carton.po_id)
    diff = carton.required_count - carton.detected_count
    if diff < 0:  # overpacked: the extra pillows go back to the packing queue
        po.pack_queue += -diff
    elif diff > 0:  # underpacked: top up from the queue, then from loose pillows set aside at rework,
        # and only then have the line make the missing pillows, so every pillow in the carton was produced
        from_queue = min(diff, max(0, po.pack_queue))
        loose = max(0, po.completed_qty - po.pack_queue - po.held_qty - po.packed_qty)
        made = max(0, diff - from_queue - loose)
        po.pack_queue -= from_queue
        if made:
            po.completed_qty += made
            log_event(db, now, carton.factory_id, "quality", "rework_topup",
                      f"{made} pillow(s) made on {po.number} to complete reworked carton {carton.hold_code}",
                      "carton", carton.id, user)
    po.held_qty = max(0, po.held_qty - carton.detected_count)
    carton.detected_count = carton.required_count
    approve(db, carton, po, now, user, "Reworked")
    for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.carton_id == carton.id, m.DefectRecord.status == "Open")):
        d.status, d.resolved_at, d.resolved_by, d.disposition = "Resolved", now, user, "Rework"
    log_event(db, now, carton.factory_id, "quality", "carton_reworked",
              f"{carton.hold_code} reworked and recounted ({carton.required_count}) → {carton.carton_code}", "carton", carton.id, user)


def override(db: Session, carton: m.Carton, now: datetime, user: str, reason: str) -> None:
    if carton.status != "Blocked":
        raise ValueError("Only blocked cartons can be overridden")
    if not reason or len(reason.strip()) < 5:
        raise ValueError("An override reason is required")
    po = db.get(m.ProductionOrder, carton.po_id)
    po.held_qty = max(0, po.held_qty - carton.detected_count)
    carton.override_reason = reason.strip()
    approve(db, carton, po, now, user, "Overridden")
    for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.carton_id == carton.id, m.DefectRecord.status == "Open")):
        d.status, d.resolved_at, d.resolved_by, d.disposition, d.notes = "Resolved", now, user, "Override", reason[:200]
    log_event(db, now, carton.factory_id, "quality", "carton_override",
              f"Supervisor override on {carton.hold_code}: {reason}", "carton", carton.id, user)


def reject(db: Session, carton: m.Carton, now: datetime, user: str) -> None:
    if carton.status != "Blocked":
        raise ValueError("Only blocked cartons can be rejected")
    po = db.get(m.ProductionOrder, carton.po_id)
    po.held_qty = max(0, po.held_qty - carton.detected_count)
    po.completed_qty = max(0, po.completed_qty - carton.detected_count)  # must be produced again
    if po.status == "Completed":
        po.status = "In Progress"
        po.completed_at = None
    carton.status = "Rejected"
    for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.carton_id == carton.id, m.DefectRecord.status == "Open")):
        d.status, d.resolved_at, d.resolved_by, d.disposition = "Resolved", now, user, "Reject"
    log_event(db, now, carton.factory_id, "quality", "carton_rejected",
              f"{carton.hold_code} rejected; {carton.detected_count} pillows scrapped and re-queued for production",
              "carton", carton.id, user)


def quality_counts(db: Session, factory_ids: list[int], since: datetime, until: datetime | None = None) -> dict:
    q = select(m.Carton.status, m.Carton.first_pass, func.count()).where(
        m.Carton.factory_id.in_(factory_ids), m.Carton.created_at >= since)
    if until:
        q = q.where(m.Carton.created_at < until)
    q = q.group_by(m.Carton.status, m.Carton.first_pass)
    c = {"inspected": 0, "passed_first": 0, "blocked": 0, "reworked": 0, "overridden": 0, "rejected": 0, "passed": 0}
    for status, first, n in db.execute(q):
        c["inspected"] += n
        if status == "Passed":
            c["passed"] += n
            if first:
                c["passed_first"] += n
        elif status == "Blocked":
            c["blocked"] += n
        elif status == "Reworked":
            c["reworked"] += n
        elif status == "Overridden":
            c["overridden"] += n
        elif status == "Rejected":
            c["rejected"] += n
    c["failed"] = c["inspected"] - c["passed_first"]
    c["accuracy_pct"] = round(100 * c["passed_first"] / c["inspected"], 2) if c["inspected"] else 100.0
    return c


ERROR_RATE_DAYS = 7
ERROR_RATE_MIN_CARTONS = 200   # below this a customer's error rate is too noisy to rank


def customer_error_rates(db: Session, factory_ids: list[int], now: datetime, days: int = ERROR_RATE_DAYS,
                         min_cartons: int = ERROR_RATE_MIN_CARTONS) -> list[dict]:
    """First-pass failure rate per customer over the last `days` (rolling). One definition for the
    Quality analytics chart, the S!AP insight and the AI assistant. Rows with fewer than `min_cartons`
    inspected cartons are returned with ranked=False and sorted after the ranked ones."""
    since = now - timedelta(days=days)
    agg: dict[int, list[int]] = {}
    for cid, fp, n in db.execute(select(m.Carton.customer_id, m.Carton.first_pass, func.count()).where(
            m.Carton.factory_id.in_(factory_ids), m.Carton.created_at >= since, m.Carton.created_at <= now)
            .group_by(m.Carton.customer_id, m.Carton.first_pass)):
        a = agg.setdefault(cid, [0, 0])
        a[0] += n
        if not fp:
            a[1] += n
    names = {c.id: c.name for c in db.scalars(select(m.Customer))}
    rows = [{"customer_id": cid, "customer": names.get(cid, "—"), "inspected": n, "errors": bad,
             "error_rate_pct": round(100 * bad / n, 2) if n else 0.0, "ranked": n >= min_cartons}
            for cid, (n, bad) in agg.items()]
    return sorted(rows, key=lambda r: (not r["ranked"], -r["error_rate_pct"], -r["inspected"]))


def next_hold(db: Session) -> str:
    return next_number(db, m.Carton, "HOLD-", 0, 7)
