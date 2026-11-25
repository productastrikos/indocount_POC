"""Contextual AI insights for each stage.

Insights are computed from the operational records (no canned text): each
one states the evidence, the recommended action and links to the record it
concerns so the UI can open it."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import inventory, machine_health as mh, readiness, workforce
from .metrics import pipeline


def _ins(id_, severity, category, title, detail, evidence, recommendation, link=None, action=None):
    return {"id": id_, "severity": severity, "category": category, "title": title, "detail": detail,
            "evidence": evidence, "recommendation": recommendation, "link": link, "action": action}


def supplier_performance(db: Session, factory_ids: list[int]) -> list[dict]:
    stats = defaultdict(lambda: {"deliveries": 0, "late": 0, "late_days": 0.0, "open_delayed": 0})
    for gr in db.scalars(select(m.GoodsReceipt).where(m.GoodsReceipt.factory_id.in_(factory_ids))):
        po = db.get(m.PurchaseOrder, gr.po_id)
        ship = db.get(m.InboundShipment, gr.shipment_id) if gr.shipment_id else None
        due = (ship.original_expected_arrival if ship and ship.original_expected_arrival else po.expected_date)
        arrived = ship.actual_arrival if ship and ship.actual_arrival else gr.confirmed_at
        s = stats[po.supplier_id]
        s["deliveries"] += 1
        late = (arrived - due).total_seconds() / 86400 if arrived and due else 0
        if late > 0.25:
            s["late"] += 1
            s["late_days"] += late
    for sh in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(factory_ids),
                                                         m.InboundShipment.status.in_(("Scheduled", "In Transit")))):
        if sh.original_expected_arrival and sh.expected_arrival > sh.original_expected_arrival + timedelta(hours=6):
            stats[sh.supplier_id]["open_delayed"] += 1
    out = []
    for sid, s in stats.items():
        sup = db.get(m.Supplier, sid)
        out.append({"supplier_id": sid, "supplier": sup.name, "deliveries": s["deliveries"], "late": s["late"],
                    "on_time_pct": round(100 * (s["deliveries"] - s["late"]) / s["deliveries"], 1) if s["deliveries"] else None,
                    "avg_late_days": round(s["late_days"] / s["late"], 1) if s["late"] else 0,
                    "open_delayed": s["open_delayed"], "lead_time_days": sup.lead_time_days})
    return sorted(out, key=lambda r: (r["on_time_pct"] if r["on_time_pct"] is not None else 101))


def procurement(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    rows = inventory.material_rows(db, fids, now)
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    skus = list(db.scalars(select(m.Sku)))
    for r in rows:
        if r["status"] not in ("Critical", "Low"):
            continue
        dep = list(db.scalars(select(m.ProductionOrder).where(
            m.ProductionOrder.factory_id == r["factory_id"], m.ProductionOrder.status != "Completed",
            m.ProductionOrder.sku_id.in_([s.id for s in skus if r["material_id"] in (s.fill_material_id, s.shell_material_id)]))))
        need = sum((p.required_qty - p.completed_qty) * (next(s for s in skus if s.id == p.sku_id).fill_kg
                   if r["category"] in ("Fibre", "Foam") else 1) for p in dep)
        out.append(_ins(f"runout-{r['id']}", "high" if r["status"] == "Critical" else "medium", "Material run-out",
                        f"{r['code']} at {factories[r['factory_id']].name} covers {r['days_cover']} days",
                        f"{r['available']:,.0f} {r['uom']} on hand vs {r['consumption_per_day']:,.0f} {r['uom']}/day. "
                        f"{len(dep)} open production orders need {need:,.0f} {r['uom']}.",
                        [f"Reorder threshold {r['reorder_days']} days", f"Incoming {r['incoming']:,.0f} {r['uom']}",
                         f"Next delivery {r['next_delivery'] or 'none scheduled'}"],
                        "Expedite the open PO" if r["incoming"] else "Raise a purchase order now",
                        {"page": "procurement", "entity_type": "material", "entity_id": r["material_id"], "factory_id": r["factory_id"]}))
        sups = [s for s in db.scalars(select(m.Supplier)) if r["code"] in (s.material_codes or [])]
        if sups:
            best = min(sups, key=lambda s: s.lead_time_days)
            qty = inventory.recommended_reorder(r, best.lead_time_days)
            if qty > 0:
                out.append(_ins(f"reorder-{r['id']}", "medium", "Reorder quantity",
                                f"Order {qty:,.0f} {r['uom']} of {r['code']} for {factories[r['factory_id']].code}",
                                f"Covers {best.lead_time_days} days lead time + 3 days safety + 7 days cycle at current consumption.",
                                [f"Consumption {r['consumption_per_day']:,.0f}/day", f"Available {r['available']:,.0f}",
                                 f"Incoming {r['incoming']:,.0f}"],
                                f"Create PO with {best.name} (lead time {best.lead_time_days} d)",
                                {"page": "procurement", "entity_type": "material", "entity_id": r["material_id"], "factory_id": r["factory_id"]},
                                {"type": "create_po", "label": "Create PO", "payload": {
                                    "factory_id": r["factory_id"], "material_id": r["material_id"], "supplier_id": best.id,
                                    "qty": qty, "expected_days": best.lead_time_days}}))
    for s in supplier_performance(db, fids):
        if s["on_time_pct"] is not None and (s["on_time_pct"] < 80 or s["open_delayed"]):
            out.append(_ins(f"supplier-{s['supplier_id']}", "medium", "Supplier reliability",
                            f"{s['supplier']} has recurring delivery delays",
                            f"{s['late']} of {s['deliveries']} recent deliveries late (avg {s['avg_late_days']} days); "
                            f"{s['open_delayed']} open shipment(s) currently delayed.",
                            [f"On-time {s['on_time_pct']}%", f"Quoted lead time {s['lead_time_days']} days"],
                            "Add buffer stock for materials from this supplier and qualify an alternate source.",
                            {"page": "procurement", "entity_type": "supplier", "entity_id": s["supplier_id"]}))
    for sh in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id.in_(fids),
                                                         m.InboundShipment.status.in_(("Scheduled", "In Transit")))):
        row = next((r for r in rows if r["factory_id"] == sh.factory_id and r["material_id"] == sh.material_id), None)
        if row and row["days_cover"] is not None:
            eta_days = (sh.expected_arrival - now).total_seconds() / 86400
            if eta_days > row["days_cover"]:
                out.append(_ins(f"expedite-{sh.id}", "high", "Expedite delivery",
                                f"Expedite {sh.po.number}: {row['code']} runs out before it arrives",
                                f"Stock lasts {row['days_cover']} days but {sh.ref} arrives in {eta_days:.1f} days.",
                                [f"ETA {sh.expected_arrival:%a %b %d %H:%M}", sh.delay_reason or "On original schedule"],
                                "Request expedited freight or a partial early shipment.",
                                {"page": "procurement", "entity_type": "purchase_order", "entity_id": sh.po_id}))
    return out


def production(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    pipe = pipeline(db, fids, now.date(), now)
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids)))}
    if pipe["bottleneck"]:
        s = next(x for x in pipe["stages"] if x["stage"] == pipe["bottleneck"])
        out.append(_ins("bottleneck", "high", "Bottleneck", f"{s['stage']} is the current bottleneck",
                        f"Constraining {', '.join(s['constrained_lines'][:4]) or 'output'}; stage capacity {s['efficiency_pct']}%.",
                        [f"{s['qty']:,} {s['unit']} vs {s['target']:,} planned to date"],
                        {"Sewing": "Move a sewer from a line ahead of plan; coach operators below benchmark.",
                         "Raw Material Preparation": "Expedite raw material and run SKUs with available fibre.",
                         "Pillow Filling": "Inspect filling heads and restore line speed."}.get(s["stage"], "Inspect the constrained equipment."),
                        {"page": "production", "entity_type": "stage", "entity_id": s["stage"]}))
    for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(fids), m.Machine.health_score < 85,
                                                 m.Machine.status == "Running")):
        p = mh.prediction(mc, mh.history(db, mc.id, 144), now)
        if p:
            out.append(_ins(f"pm-{mc.id}", "high" if p["risk"] == "High" else "medium", "Predictive maintenance",
                            f"{mc.code} ({lines[mc.line_id].name}) needs maintenance within ~{p['est_hours_to_maintenance']:.0f} operating h",
                            p["anomaly"], [f"Health {p['health_score']}", f"Risk {p['risk']}", p["disclaimer"]],
                            f"{p['recommended_action']}. Window: {p['suggested_window']}.",
                            {"page": "production", "entity_type": "machine", "entity_id": mc.id}))
    day0 = datetime.combine(now.date(), datetime.min.time())
    co = db.execute(select(m.ChangeoverEvent.line_id, func.count(), func.sum(m.ChangeoverEvent.lost_min)).where(
        m.ChangeoverEvent.factory_id.in_(fids), m.ChangeoverEvent.start >= day0 - timedelta(days=1))
        .group_by(m.ChangeoverEvent.line_id)).all()
    if co:
        lid, n, lost = max(co, key=lambda r: r[2] or 0)
        if lost and lost > 3:
            out.append(_ins("changeover", "medium", "Changeover", f"{lines[lid].name} loses the most time to changeovers",
                            f"{n} changeovers since yesterday, {lost:.0f} min beyond the 3-min standard.",
                            ["Standard changeover: 3 min"],
                            "Run the optimizer to group same-SKU orders and pre-stage materials before changeovers.",
                            {"page": "production", "entity_type": "line", "entity_id": lid},
                            {"type": "optimize", "label": "Optimize production plan", "payload": {"factory_id": lines[lid].factory_id}}))
    late = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id.in_(fids),
                                                           m.ProductionOrder.status == "Delayed")))
    if late:
        worst = max(late, key=lambda p: (p.estimated_completion - p.required_completion).total_seconds()
                    if p.estimated_completion and p.required_completion else 0)
        out.append(_ins("late-pos", "high", "Schedule", f"{len(late)} production orders projected late",
                        f"Worst: {worst.number} est. {worst.estimated_completion:%a %H:%M} vs required {worst.required_completion:%a %H:%M}."
                        if worst.estimated_completion and worst.required_completion else "",
                        [p.number for p in late[:5]], "Run Optimize Production Plan to re-sequence orders across lines.",
                        {"page": "production", "entity_type": "production_order", "entity_id": worst.id},
                        {"type": "optimize", "label": "Optimize production plan", "payload": {"factory_id": worst.factory_id}}))
    rows = workforce.employee_rows(db, fids, now.date(), now)
    idle = defaultdict(float)
    for r in rows:
        if r["line_id"]:
            idle[r["line_id"]] += r["idle_min"]
    if idle:
        lid = max(idle, key=idle.get)
        if idle[lid] > 30:
            out.append(_ins("idle", "medium", "Workforce", f"{lines[lid].name} has the highest idle time today",
                            f"{idle[lid] / 60:.1f} worker-hours idle (downtime, changeovers, starvation).",
                            [f"Line status: {lines[lid].status} {lines[lid].status_reason or ''}"],
                            "Temporarily redeploy idle operators to packing or to lines behind plan.",
                            {"page": "production", "entity_type": "line", "entity_id": lid}))
    low = [r for r in rows if r["below_benchmark"]]
    if low:
        out.append(_ins("low-productivity", "medium", "Workforce", f"{len(low)} employees below productivity benchmark",
                        ", ".join(f"{r['code']} ({r['productivity_pct']}%)" for r in low[:4]),
                        [f"Benchmark threshold {workforce.LOW_PRODUCTIVITY_PCT:.0f}% of standard rate"],
                        "Pair with experienced operators and review workstation set-up.",
                        {"page": "production", "entity_type": "employee", "entity_id": low[0]["id"]}))
    return out


def quality(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    since = now - timedelta(days=1)
    defects = list(db.scalars(select(m.DefectRecord).where(m.DefectRecord.factory_id.in_(fids), m.DefectRecord.created_at >= since)))
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine))}
    customers = {c.id: c.name for c in db.scalars(select(m.Customer))}
    if defects:
        by_line = Counter(d.line_id for d in defects)
        lid, n = by_line.most_common(1)[0]
        share = round(100 * n / len(defects))
        cats = Counter(d.category for d in defects if d.line_id == lid)
        if n >= 2:
            out.append(_ins("line-defects", "high" if share >= 40 else "medium", "Recurring issue",
                            f"{lines[lid].name} accounts for {share}% of packaging defects (24 h)",
                            ", ".join(f"{k}: {v}" for k, v in cats.most_common(3)),
                            [f"{len(defects)} defects in 24 h"],
                            "Recalibrate the pillow counter at this packing station and brief the packing operator.",
                            {"page": "quality", "entity_type": "line", "entity_id": lid}))
        by_cat = Counter(d.category for d in defects)
        cat, n = by_cat.most_common(1)[0]
        out.append(_ins("top-defect", "medium", "Defect pattern", f"{cat} is the most frequent defect",
                        f"{n} of {len(defects)} defects in 24 h.", [f"{k}: {v}" for k, v in by_cat.most_common(4)],
                        {"Underpacked": "Check the count sensor and carton-closing interlock; enforce count-before-seal.",
                         "Overpacked": "Verify the SKU spec loaded at the station matches the production order.",
                         "Sewing defect": "Inspect sewing machine tension and needle condition.",
                         "Label mismatch": "Verify label templates against the customer specification."}.get(cat, "Review the station SOP."),
                        {"page": "quality", "entity_type": "defect_category", "entity_id": cat}))
    rows = db.execute(select(m.Carton.customer_id, func.count(), func.sum(case((m.Carton.first_pass.is_(False), 1), else_=0)))
                      .where(m.Carton.factory_id.in_(fids), m.Carton.created_at >= since).group_by(m.Carton.customer_id)).all()
    rates = [(cid, (bad or 0) / n) for cid, n, bad in rows if n >= 20]
    if rates:
        cid, rate = max(rates, key=lambda x: x[1])
        if rate > 0:
            out.append(_ins("customer-errors", "medium", "Customer impact", f"{customers[cid]} has the highest packaging error rate",
                            f"{rate * 100:.2f}% of cartons failed first-pass inspection in 24 h.", [],
                            "Add a second count check for this customer's SKUs until the rate falls below 0.3%.",
                            {"page": "quality", "entity_type": "customer", "entity_id": cid}))
    blocked = db.scalar(select(func.count()).select_from(m.Carton).where(m.Carton.factory_id.in_(fids), m.Carton.status == "Blocked"))
    if blocked:
        first = db.scalar(select(m.Carton.id).where(m.Carton.factory_id.in_(fids), m.Carton.status == "Blocked").order_by(m.Carton.created_at))
        out.append(_ins("blocked", "high", "Action required", f"{blocked} blocked cartons awaiting resolution",
                        "Blocked cartons are excluded from finished goods and delay shipment readiness.", [],
                        "Rework (recount) or apply a supervisor override with a recorded reason.",
                        {"page": "quality", "entity_type": "carton", "entity_id": first}))
    return out


def warehouse(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)
    urgent = []
    for o in orders:
        t = info[o.id]["truck"]
        if t and t.status in ("Scheduled", "Arrived") and o.picked_qty < o.allocated_qty and t.scheduled_arrival <= now + timedelta(hours=6):
            urgent.append((t.scheduled_arrival, o, t))
    urgent.sort(key=lambda x: x[0])
    for when, o, t in urgent[:3]:
        out.append(_ins(f"urgent-pick-{o.id}", "high", "Picking priority", f"Pick {o.number} before {when:%H:%M}",
                        f"{(o.allocated_qty - o.picked_qty):,} allocated pillows not yet picked; truck {t.ref} at {t.dock}.",
                        [f"Readiness {info[o.id]['readiness']['pct']}%"], "Expedite picking for this order.",
                        {"page": "warehouse", "entity_type": "order", "entity_id": o.id},
                        {"type": "expedite_picking", "label": "Expedite picking", "payload": {"order_id": o.id}}))
    for o in orders:
        short = o.qty - o.allocated_qty
        t = info[o.id]["truck"]
        if short > 0 and o.scheduled_dispatch <= now + timedelta(hours=36):
            po = next(iter(info[o.id]["pos"]), None)
            out.append(_ins(f"short-{o.id}", "medium", "Inventory shortage",
                            f"{o.number} is short {short:,} pillows of {o.sku.code}",
                            f"Finished goods cannot cover the order; linked production order {po.number if po else '—'} "
                            f"est. {po.estimated_completion:%a %H:%M}." if po and po.estimated_completion else "No production order linked.",
                            [f"Ships {o.scheduled_dispatch:%a %H:%M}"], "Prioritize the linked production order.",
                            {"page": "warehouse", "entity_type": "order", "entity_id": o.id}))
    # storage consolidation: SKUs scattered across many zones
    spread = defaultdict(set)
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(fids), m.Pallet.status == "Stored")):
        if p.location:
            spread[(p.factory_id, p.sku_id)].add(p.location.zone)
    for (fid, sid), zones in spread.items():
        if len(zones) >= 3:
            sku = db.get(m.Sku, sid)
            out.append(_ins(f"consolidate-{fid}-{sid}", "low", "Storage allocation", f"Consolidate {sku.code} — stored in {len(zones)} zones",
                            f"Zones {', '.join(sorted(zones))}. Consolidating reduces pick travel.", [],
                            "Direct new put-away of this SKU to its primary zone and relocate stragglers during low activity.",
                            {"page": "warehouse", "entity_type": "sku", "entity_id": sid}))
            break
    wl = workload(db, fids, now)
    gap = wl["required_fte"] - wl["deployed"]
    if abs(gap) >= 1:
        out.append(_ins("wh-workforce", "medium" if gap > 0 else "low", "Workforce",
                        f"Warehouse needs {wl['required_fte']} FTE vs {wl['deployed']} deployed",
                        f"Workload: {wl['inbound_shipments']} inbound, {wl['pallets_to_putaway']} pallets to put away, "
                        f"{wl['cartons_to_pick']:,} cartons to pick, {wl['outbound_loads']} loads.",
                        [], "Move one associate from put-away to picking for the next 2 hours." if gap > 0
                        else "Release surplus associates to packing support.",
                        {"page": "warehouse", "entity_type": "workforce", "entity_id": None}))
    return out


def workload(db: Session, fids: list[int], now: datetime) -> dict:
    from . import calendar as cal
    inbound = db.scalar(select(func.count()).select_from(m.InboundShipment).where(
        m.InboundShipment.factory_id.in_(fids), m.InboundShipment.status.in_(("Arrived", "Receiving")))) or 0
    inbound_today = db.scalar(select(func.count()).select_from(m.InboundShipment).where(
        m.InboundShipment.factory_id.in_(fids), m.InboundShipment.expected_arrival >= now - timedelta(hours=2),
        m.InboundShipment.expected_arrival <= now + timedelta(hours=10))) or 0
    pallets = db.scalar(select(func.count()).select_from(m.Pallet).where(m.Pallet.factory_id.in_(fids),
                                                                          m.Pallet.status == "Pending Put-away")) or 0
    upcoming_putaway = int(sum(ln.planned_rate for ln in db.scalars(select(m.ProductionLine).where(
        m.ProductionLine.factory_id.in_(fids)))) * 8 / 300)
    cartons = db.scalar(select(func.coalesce(func.sum(m.Allocation.cartons), 0)).join(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(fids), m.Allocation.status == "Reserved")) or 0
    loads = db.scalar(select(func.count()).select_from(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(fids), m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")),
        m.TruckAppointment.scheduled_arrival <= now + timedelta(hours=10))) or 0
    hours = (inbound + inbound_today) * 1.5 + (pallets + upcoming_putaway) * 0.08 + cartons / 90 + loads * 1.25
    shift = cal.shift_of(now) or 1
    deployed = db.scalar(select(func.count()).select_from(m.Employee).where(
        m.Employee.factory_id.in_(fids), m.Employee.role.in_(("Warehouse associate", "Forklift operator")),
        m.Employee.shift == shift, m.Employee.attendance.in_(("Present", "Late")))) or 0
    req = round(hours / (cal.productive_minutes_per_shift() / 60), 1)
    return {"inbound_shipments": inbound + inbound_today, "pallets_to_putaway": pallets + upcoming_putaway,
            "cartons_to_pick": int(cartons), "outbound_loads": loads, "workload_hours": round(hours, 1),
            "required_fte": req, "deployed": deployed,
            "utilization_pct": round(100 * req / deployed, 1) if deployed else None}


def dispatch(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)
    for o in sorted(orders, key=lambda x: info[x.id]["risk"]["slack_min"] if info[x.id]["risk"]["slack_min"] is not None else 1e9):
        r = info[o.id]["risk"]
        if not r["at_risk"]:
            continue
        recs = readiness.recommendations(o, info[o.id], now)
        t = info[o.id]["truck"]
        out.append(_ins(f"risk-{o.id}", "high" if r["level"] == "High" else "medium", "Shipment risk",
                        f"{o.number} ({o.customer.name}) at risk for {t.scheduled_arrival:%a %H:%M} truck",
                        f"Projected ready {info[o.id]['ready_eta']:%a %H:%M}, {(-r['slack_min']):.0f} min after arrival."
                        + (f" Estimated detention ${r['est_detention']:,.0f} (illustrative)." if r["est_detention"] else ""),
                        r["reasons"], recs[0]["label"] if recs else "Review the order",
                        {"page": "dispatch", "entity_type": "order", "entity_id": o.id},
                        {"type": "risk_action", "label": recs[0]["label"], "payload": {"order_id": o.id, "action": recs[0]["action"]}} if recs else None))
    det = readiness.detention_summary(db, fids, now, 7)
    if det["delayed_trucks"]:
        top = det["reasons"][0]["name"] if det["reasons"] else "order not ready"
        out.append(_ins("detention", "medium", "Detention", f"${det['incurred_estimate']:,.0f} estimated detention this week",
                        f"{det['delayed_trucks']} trucks waited beyond {det['free_minutes']:.0f} free minutes; main reason: {top}.",
                        [det["note"]], "Align truck appointments with projected readiness and pre-pick orders 4 h before arrival.",
                        {"page": "dispatch", "entity_type": "detention", "entity_id": None}))
    return out


def overview(db: Session, fids: list[int], now: datetime) -> list[dict]:
    pools = [dispatch(db, fids, now)[:2], production(db, fids, now)[:2], procurement(db, fids, now)[:1],
             quality(db, fids, now)[:1], warehouse(db, fids, now)[:1]]
    out = [x for p in pools for x in p]
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda x: rank.get(x["severity"], 3))[:6]


PAGES = {"overview": overview, "procurement": procurement, "production": production, "quality": quality,
         "warehouse": warehouse, "dispatch": dispatch}
