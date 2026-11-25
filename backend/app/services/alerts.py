"""Alert rules engine. Re-evaluated every simulation tick (and after user
actions). Alerts are keyed, so a persisting condition updates one alert,
and alerts whose condition has cleared are resolved automatically."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import inventory, machine_health as mh, readiness, workforce
from .common import business_rules

SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


def _line_perf(db: Session, now: datetime) -> dict[int, tuple[float, float]]:
    since = now - timedelta(hours=2)
    out = defaultdict(lambda: [0.0, 0.0])
    rows = db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.date == now.date(),
                                                       m.ProductionHourly.hour >= since.hour))
    for r in rows:
        if r.hour <= now.hour:
            out[r.line_id][0] += r.actual
            out[r.line_id][1] += r.planned if r.hour < now.hour else r.planned * min(1.0, now.minute / 60)
    return {k: (a, p) for k, (a, p) in out.items()}


def generate(db: Session, now: datetime) -> list[dict]:
    rules = business_rules(db)
    out: list[dict] = []
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine))}

    def add(key, severity, fid, stage, title, desc, action, etype=None, eid=None, impact=0.0):
        out.append(dict(key=key, severity=severity, factory_id=fid, stage=stage, title=title, description=desc,
                        recommended_action=action, entity_type=etype, entity_id=eid, impact=impact))

    # 1. Line underperformance
    perf = _line_perf(db, now)
    from .metrics import pipeline  # local import: metrics imports this module's siblings
    pipes = {fid: pipeline(db, [fid], now.date(), now)["per_line"] for fid in factories}
    for lid, (act, plan) in perf.items():
        ln = lines[lid]
        if plan < 60 or ln.status in ("Breakdown", "Maintenance", "Changeover") or                 (ln.status == "Idle" and (ln.status_reason or "").startswith("No production order")):
            continue
        ratio = act / plan
        if ratio < 0.85:
            gap = round((1 - ratio) * 100)
            binding = pipes[ln.factory_id].get(lid, {}).get("binding")
            action = {
                "Sewing": "Rebalance the sewing crew — move a trained sewer from a line running ahead of plan, and coach low performers.",
                "Raw Material Preparation": "Expedite the constrained raw material and re-sequence orders that use available fibre.",
                "Transfer to Packaging": "Add a packing operator to clear the packing-station queue.",
            }.get(binding, "Inspect the constrained machine and verify line speed settings; review recent changeovers.")
            add(f"line_perf:{lid}", "critical" if ratio < 0.7 else "warning", ln.factory_id, "production",
                f"{ln.name} is operating {gap}% below target",
                f"Last 2 h: {act:,.0f} pillows vs {plan:,.0f} planned at {factories[ln.factory_id].name}."
                + (f" Binding constraint: {binding}." if binding else ""), action, "line", lid, gap)

    idle = defaultdict(list)
    for ln in lines.values():
        if ln.status == "Idle" and (ln.status_reason or "").startswith("No production order"):
            idle[ln.factory_id].append(ln.name)
    for fid, names in idle.items():
        add(f"idle_capacity:{fid}", "warning", fid, "production", f"{len(names)} line(s) idle — no production orders queued",
            f"{', '.join(names)} at {factories[fid].name} have no work while other lines carry queued orders.",
            "Run Optimize Production Plan to rebalance queued orders across lines.", "optimization", None, 30)

    # 2. Machines
    for mc in db.scalars(select(m.Machine)):
        ln = lines.get(mc.line_id)
        if mc.status == "Breakdown":
            add(f"machine_down:{mc.id}", "critical", mc.factory_id, "production",
                f"Machine {mc.code} breakdown — {ln.name if ln else ''} stopped",
                f"{mc.name} failed. Repair ETA {mc.unavailable_until:%H:%M}." if mc.unavailable_until else f"{mc.name} failed.",
                "Dispatch maintenance; affected orders have been re-estimated — review at-risk shipments.",
                "machine", mc.id, 90)
        elif mc.status == "Running" and mc.health_score < 85:
            hist = mh.history(db, mc.id, 60)
            an = mh.anomalies(mc, hist)
            if mc.health_score < 65 or an:
                main = an[0]["metric"] if an else "condition"
                add(f"machine_health:{mc.id}", "critical" if mc.health_score < 65 else "warning", mc.factory_id, "production",
                    f"Machine {mc.code} has abnormal {main}",
                    f"{mc.name} on {ln.name if ln else '—'}: health score {mc.health_score:.0f}/100. "
                    + "; ".join(a["message"] for a in an[:2]),
                    "Acknowledge the predictive maintenance recommendation and schedule an inspection.",
                    "machine", mc.id, 100 - mc.health_score)

    # 3. Blocked cartons
    rows = db.execute(select(m.Carton.factory_id, m.Carton.result, func.count()).where(m.Carton.status == "Blocked")
                      .group_by(m.Carton.factory_id, m.Carton.result))
    blocked = defaultdict(dict)
    for fid, res, n in rows:
        blocked[fid][res] = n
    for fid, d in blocked.items():
        qty_err = d.get("Underpacked", 0) + d.get("Overpacked", 0)
        total = sum(d.values())
        first = db.scalar(select(m.Carton.id).where(m.Carton.factory_id == fid, m.Carton.status == "Blocked")
                          .order_by(m.Carton.created_at))
        title = (f"{qty_err} cartons have incorrect pillow quantities" if qty_err
                 else f"{total} cartons blocked at final inspection")
        add(f"cartons_blocked:{fid}", "critical" if total >= 3 or qty_err else "warning", fid, "quality", title,
            f"{total} cartons on hold at {factories[fid].name} " + ", ".join(f"{k}: {v}" for k, v in d.items())
            + ". Blocked cartons cannot enter finished goods.",
            "Recount and rework at the packing station, or record a supervisor override with reason.",
            "carton", first, 40 + total)

    # 4. Raw material coverage & delayed inbound
    mats = inventory.material_rows(db, list(factories), now)
    for r in mats:
        if r["status"] in ("Critical", "Low"):
            days = r["days_cover"]
            when = "today" if days < 0.6 else "tomorrow" if days < 1.6 else f"in {days:.1f} days"
            add(f"material:{r['factory_id']}:{r['material_id']}", "critical" if r["status"] == "Critical" else "warning",
                r["factory_id"], "procurement", f"Raw material SKU {r['code']} may run out {when}",
                f"{r['name']}: {r['available']:,.0f} {r['uom']} available, consuming {r['consumption_per_day']:,.0f}/day "
                f"({days} days cover vs {r['reorder_days']} threshold). Incoming {r['incoming']:,.0f} {r['uom']}.",
                "Expedite the open purchase order or raise an emergency PO; re-sequence orders using this material.",
                "material", r["material_id"], 60 if r["status"] == "Critical" else 25)
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.status.in_(("Scheduled", "In Transit")))):
        if s.original_expected_arrival and s.expected_arrival > s.original_expected_arrival + timedelta(hours=6):
            days = (s.expected_arrival - s.original_expected_arrival).total_seconds() / 86400
            sup = db.get(m.Supplier, s.supplier_id)
            mat = db.get(m.Material, s.material_id)
            add(f"inbound_delay:{s.id}", "warning", s.factory_id, "procurement",
                f"Supplier delivery {s.ref} ({mat.code}) delayed {days:.1f} days",
                f"{sup.name}: {s.delay_reason or 'revised ETA'}; now due {s.expected_arrival:%a %b %d %H:%M}.",
                "Confirm revised ETA with the supplier and evaluate an alternate source.", "shipment", s.id, 20)

    # 5. Workforce below benchmark
    for fid in factories:
        rows_ = workforce.employee_rows(db, [fid], now.date(), now)
        low = [r for r in rows_ if r["below_benchmark"]]
        if len(low) >= 2:
            add(f"workforce_low:{fid}", "warning", fid, "production",
                f"{len(low)} employees are significantly below the productivity benchmark",
                "; ".join(f"{r['code']} {r['role']} {r['line']} ({r['productivity_pct']}%)" for r in low[:4]),
                "Review station assignments and provide coaching; consider pairing with experienced operators.",
                "employee", low[0]["id"], 15)

    # 6. Shipments & trucks
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)
    material_chain = defaultdict(list)
    for o in orders:
        r = info[o.id]["risk"]
        t = info[o.id]["truck"]
        if r["at_risk"] and t:
            add(f"ship_risk:{o.id}", "critical" if r["level"] == "High" else "warning", o.factory_id, "dispatch",
                f"Shipment {o.number} is unlikely to meet its truck appointment",
                f"{o.customer.name} · {t.ref} due {t.scheduled_arrival:%a %H:%M}; projected ready "
                f"{info[o.id]['ready_eta']:%a %H:%M}. " + "; ".join(r["reasons"]),
                "Prioritize the order, expedite picking or reschedule the truck (see Dispatch).",
                "order", o.id, 70 + min(30, -r["slack_min"] / 10 if r["slack_min"] else 0))
            for po in info[o.id]["pos"]:
                if po.material_risk and po.risk_reason:
                    material_chain[(o.factory_id, po.risk_reason.split(" ")[0])].append(o.number)
        if t and t.status == "Arrived" and t.actual_arrival:
            wait = (now - t.actual_arrival).total_seconds() / 60
            if wait > rules["detention_free_min"]:
                add(f"detention:{t.id}", "critical", o.factory_id, "dispatch",
                    f"Truck {t.ref} waiting {wait:.0f} min — detention charge likely",
                    f"{o.number} for {o.customer.name}: {t.delay_reason or 'order not ready'}. "
                    f"Illustrative charge ${rules['detention_charge']:,.0f}.",
                    "Expedite picking/loading for this order or release the truck and rebook.", "truck", t.id, 80)
    for (fid, mat_code), olist in material_chain.items():
        add(f"chain_material:{fid}:{mat_code}", "critical", fid, "procurement",
            f"Material shortage on {mat_code} puts {len(olist)} customer order(s) at risk",
            f"Procurement → Production → Dispatch: {mat_code} stock-out delays production; affected orders: {', '.join(olist[:5])}.",
            "Expedite the inbound shipment, re-sequence production to unaffected SKUs and notify customers.",
            "material", db.scalar(select(m.Material.id).where(m.Material.code == mat_code)), 95)

    # 7. Changeover losses and warehouse backlog
    day0 = datetime.combine(now.date(), datetime.min.time())
    for lid, lost in db.execute(select(m.ChangeoverEvent.line_id, func.sum(m.ChangeoverEvent.lost_min)).where(
            m.ChangeoverEvent.start >= day0).group_by(m.ChangeoverEvent.line_id)):
        if lost and lost >= 8:
            ln = lines[lid]
            add(f"changeover:{lid}", "info", ln.factory_id, "production",
                f"{ln.name} lost {lost:.0f} min to changeovers beyond the 3-min standard today",
                "Changeovers exceeded the standard duration.", "Review the changeover checklist and pre-stage materials.",
                "line", lid, 5)
    for fid in factories:
        fg = inventory.fg_summary(db, [fid])
        if fg["pending_putaway_pallets"] >= 6:
            add(f"putaway:{fid}", "warning", fid, "warehouse",
                f"{fg['pending_putaway_pallets']} pallets waiting for put-away",
                "Approved cartons are accumulating in the receiving lane.", "Assign an additional forklift operator to put-away.",
                "warehouse", fid, 10)
        if fg["utilization_pct"] >= 90:
            add(f"capacity:{fid}", "warning", fid, "warehouse", f"Warehouse at {fg['utilization_pct']}% capacity",
                "Storage locations are nearly full.", "Prioritize outbound picking and consolidate partial pallets.",
                "warehouse", fid, 10)
    return out


def evaluate(db: Session, now: datetime) -> None:
    found = {a["key"]: a for a in generate(db, now)}
    existing = {a.key: a for a in db.scalars(select(m.Alert))}
    for key, a in found.items():
        row = existing.get(key)
        if row is None:
            db.add(m.Alert(**a, created_at=now, updated_at=now, status="Open"))
        else:
            for k in ("severity", "title", "description", "recommended_action", "entity_type", "entity_id", "impact", "factory_id", "stage"):
                setattr(row, k, a[k])
            row.updated_at = now
            if row.status == "Resolved":
                row.status, row.resolved_at, row.created_at = "Open", None, now
    for key, row in existing.items():
        if key not in found and row.status != "Resolved":
            row.status = "Resolved"
            row.resolved_at = now


def open_alerts(db: Session, factory_ids: list[int], limit: int = 50, stage: str | None = None) -> list[m.Alert]:
    q = select(m.Alert).where(m.Alert.status != "Resolved", m.Alert.factory_id.in_(factory_ids))
    if stage:
        q = q.where(m.Alert.stage == stage)
    rows = list(db.scalars(q))
    rows.sort(key=lambda a: (SEV_RANK.get(a.severity, 3), -(a.impact or 0), a.created_at or datetime.min))
    return rows[:limit]
