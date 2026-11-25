"""CCTV feeds with computer-vision analytics.

The plant-floor footage is analysed offline by tools/cctv/analyze.py (YOLO pose
tracking with ByteTrack, open-vocabulary pillow/carton detection, OCR of the
carton print and labels, optical flow on conveyors). The per-frame results are
published next to each video (public/cctv/<id>.json) and replayed in sync with it
in the browser, exactly as a live edge pipeline would stream them.

This service adds the live plant context the overlay fuses with: the camera's
station and line, the RFID badges read at that station (people are identified by
badge, never by face), the order the recognised customer label belongs to and
today's counts at the station."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import twin
from .common import iso

CATALOG = Path(__file__).resolve().parent.parent / "cv" / "cctv_catalog.json"

# who works in front of each camera station (first match wins for identity fusion)
STATION_ROLES = {
    "Carton packing": ("Packaging operator", "Bagger", "Warehouse associate"),
    "Sewing": ("Sewer",),
    "Pillow filling": ("Filler", "Sewer"),
    "Compression": ("Bagger", "Packaging operator"),
    "Final inspection": ("Warehouse associate", "Forklift operator", "Supervisor"),
    "Fibre opening": ("Filler", "Maintenance technician"),
    "Carding": ("Filler", "Maintenance technician"),
}
STATE_ORDER = {"Station": 0, "Away": 1, "Break": 2}


@lru_cache(maxsize=1)
def _catalog_cached(mtime: float) -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))["feeds"]


def catalog() -> list[dict]:
    if not CATALOG.exists():
        return []
    return _catalog_cached(CATALOG.stat().st_mtime)


def _camera(db: Session, code: str):
    return db.scalar(select(m.Camera).where(m.Camera.code == code))


def _base(feed: dict, cam, factory: m.Factory, line) -> dict:
    out = {k: feed[k] for k in ("id", "factory", "camera", "title", "kind", "ptz", "duration", "fps", "width", "height",
                                "video", "poster", "analytics", "models", "summary", "recognition") if k in feed}
    out.update({
        "factory_name": factory.name if factory else None,
        "station": cam.station if cam else feed.get("station"),
        "line_id": line.id if line else None,
        "line": line.name if line else feed.get("area", "Plant floor"),
        "line_status": line.status if line else None,
        "status": cam.status if cam else "Online",
        "mode": "recorded footage · offline inference replay",
        "cv_camera": cam is not None,
    })
    return out


def feeds(db: Session, factory_codes: list[str] | None = None, kind: str | None = None) -> list[dict]:
    factories = {f.code: f for f in db.scalars(select(m.Factory))}
    out = []
    for fd in catalog():
        if factory_codes and fd["factory"] not in factory_codes:
            continue
        if kind and fd["kind"] != kind:
            continue
        cam = _camera(db, fd["camera"])
        line = db.get(m.ProductionLine, cam.line_id) if cam and cam.line_id else None
        row = _base(fd, cam, factories.get(fd["factory"]), line)
        if cam:
            row.update({"camera_id": cam.id, "inspections_today": cam.inspections_today,
                        "detections_today": cam.detections_today})
        out.append(row)
    return out


def _workers_for(fac: dict, station: str, line_id: int | None, limit: int = 8) -> list[dict]:
    roles = STATION_ROLES.get(station, ("Supervisor",))
    rows = []
    for w in fac["workers"]:
        if w["role"] not in roles or w["state"] in ("Absent", "Scheduled", "Off", "Not arrived"):
            continue
        same_line = line_id is not None and w["line_id"] == line_id
        rows.append(((roles.index(w["role"]), 0 if same_line else 1, STATE_ORDER.get(w["state"], 3), w["id"]), w))
    rows.sort(key=lambda r: r[0])
    keep = ("id", "code", "name", "role", "line", "state", "zone", "reader", "badge", "minutes_in_state",
            "availability_pct", "performance_pct", "effectiveness_pct", "pillows_per_hour", "away_min", "flag")
    return [{k: w.get(k) for k in keep} for _, w in rows[:limit]]


def _order_for(db: Session, factory: m.Factory, customer_code: str | None, now: datetime) -> dict | None:
    if not customer_code:
        return None
    cust = db.scalar(select(m.Customer).where(m.Customer.code == customer_code))
    if cust is None:
        return None
    o = db.scalar(select(m.CustomerOrder).where(m.CustomerOrder.factory_id == factory.id,
                                                m.CustomerOrder.customer_id == cust.id,
                                                m.CustomerOrder.status.notin_(("Dispatched", "Delivered", "Cancelled")))
                  .order_by(m.CustomerOrder.scheduled_dispatch))
    out = {"customer": cust.name, "customer_code": cust.code, "channel": cust.channel}
    if o is not None:
        sku = db.get(m.Sku, o.sku_id)
        out.update({"order_id": o.id, "order": o.number, "qty": o.qty, "status": o.status,
                    "ship": iso(o.scheduled_dispatch), "priority": o.priority,
                    "sku": sku.code if sku else None, "sku_name": sku.description if sku else None,
                    "pillows_per_carton": sku.pillows_per_carton if sku else None,
                    "picked": o.picked_qty, "allocated": o.allocated_qty})
    return out


def feed_detail(db: Session, feed_id: str, now: datetime) -> dict | None:
    fd = next((f for f in catalog() if f["id"] == feed_id), None)
    if fd is None:
        return None
    factory = db.scalar(select(m.Factory).where(m.Factory.code == fd["factory"]))
    cam = _camera(db, fd["camera"])
    line = db.get(m.ProductionLine, cam.line_id) if cam and cam.line_id else None
    out = _base(fd, cam, factory, line)
    fac = twin.facility(db, factory, now)
    station = out["station"]
    out["workers"] = _workers_for(fac, station, line.id if line else None)
    rec = fd.get("recognition") or {}
    out["order"] = _order_for(db, factory, rec.get("customer_code"), now)
    if line is not None:
        po = db.get(m.ProductionOrder, line.current_po_id) if line.current_po_id else None
        sku = db.get(m.Sku, line.current_sku_id) if line.current_sku_id else None
        out["line_context"] = {"sku": sku.code if sku else None, "sku_name": sku.description if sku else None,
                               "pillows_per_carton": sku.pillows_per_carton if sku else None,
                               "order": po.number if po else None,
                               "rate_per_hour": line.planned_rate}
        day0 = datetime.combine(now.date(), datetime.min.time())
        q = select(func.count(m.Carton.id), func.coalesce(func.sum(m.Carton.detected_count), 0)).where(
            m.Carton.line_id == line.id, m.Carton.created_at >= day0)
        n, pillows = db.execute(q).one()
        out["today"] = {"cartons": int(n), "pillows": int(pillows)}
    if cam is not None:
        ev = list(db.scalars(select(m.DetectionEvent).where(m.DetectionEvent.camera_id == cam.id)
                             .order_by(m.DetectionEvent.ts.desc(), m.DetectionEvent.id.desc()).limit(8)))
        out["recent_events"] = [{"ts": iso(e.ts), "type": e.event_type, "label": e.label, "confidence": e.confidence,
                                 "count": e.count, "severity": e.severity} for e in ev]
        out.update({"camera_id": cam.id, "inspections_today": cam.inspections_today,
                    "detections_today": cam.detections_today})
    out["sim_time"] = now.isoformat()
    return out
