"""Contextual AI insights for each stage.

Insights are computed from the operational records (no canned text): each
one states the evidence, the recommended action and links to the record it
concerns so the UI can open it."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal, inventory, machine_health as mh, quality as qsvc, readiness, workforce
from .metrics import pipeline
from .procurement import is_delayed

WORKLOAD_WINDOW_H = 10


def _ins(id_, severity, category, title, detail, evidence, recommendation, link=None, action=None):
    return {"id": id_, "severity": severity, "category": category, "title": title, "detail": detail,
            "evidence": evidence, "recommendation": recommendation, "link": link, "action": action}


def when(ts, now: datetime) -> str:
    """Timestamp for insight / assistant text: 'Mon 12:48' today or tomorrow, 'Mon Dec 21 12:48' otherwise,
    so a weekday never silently means a week (or three) away."""
    if ts is None:
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.strftime("%a %H:%M") if 0 <= (ts.date() - now.date()).days <= 1 else ts.strftime("%a %b %d %H:%M")


def stock_cover(db: Session, orders: list, now: datetime) -> dict[int, int]:
    """Pillows of each order's open requirement that stored stock can still cover, walking the orders in
    ship-date order against a running pool of unallocated cartons: the order's own production first, then
    free stock FIFO, never pallets made for another open order (as inventory.allocate takes them)."""
    pool, by_sku = {}, defaultdict(list)
    fids = sorted({o.factory_id for o in orders})
    for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(fids), m.Pallet.status == "Stored",
                                               m.Pallet.cartons > m.Pallet.allocated_cartons)
                        .order_by(m.Pallet.stored_at, m.Pallet.id)):
        pool[p.id] = p.cartons - p.allocated_cartons
        by_sku[(p.factory_id, p.sku_id)].append(p)
    out = {}
    for o in sorted(orders, key=lambda x: (x.scheduled_dispatch or now, x.id)):
        ppc = o.sku.pillows_per_carton
        pending = max(0, o.qty - o.allocated_qty) if o.status == "Open" else 0
        need = -(-pending // ppc)
        own = set(inventory.own_po_ids(db, o.id)) if need else set()
        pegged = inventory.pegged_po_ids(db, o.factory_id, o.id) if need else set()
        got = 0
        for p in sorted((p for p in by_sku[(o.factory_id, o.sku_id)] if p.po_id in own or p.po_id not in pegged),
                        key=lambda p: (0 if p.po_id in own else 1, p.stored_at or now, p.id)):
            if got >= need:
                break
            take = min(pool[p.id], need - got)
            pool[p.id] -= take
            got += take
        out[o.id] = min(pending, got * ppc)
    return out


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
        if is_delayed(sh):  # same tolerance as the Procurement "Delayed deliveries" tile
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
    """One run-out insight per material (the same cover / run-out as the Procurement page and the AI
    assistant), a reorder quantity when open supply does not cover it, and at most one expedite per material."""
    out = []
    rows = inventory.material_rows(db, fids, now)
    factories = {f.id: f for f in db.scalars(select(m.Factory))}
    suppliers = list(db.scalars(select(m.Supplier)))
    for r in sorted(rows, key=lambda x: (inventory.STATUS_RANK.get(x["status"], 5),  # worst status first
                                         x["days_cover"] if x["days_cover"] is not None else 1e9)):
        u, fac = r["uom"], factories[r["factory_id"]]
        link = {"page": "procurement", "entity_type": "material", "entity_id": r["material_id"], "factory_id": r["factory_id"]}
        late = r["late_delivery"]
        if r["status"] == "Untracked":
            out.append(_ins(f"untracked-{r['factory_id']}-{r['material_id']}", "medium", "Stock record missing",
                            f"{r['code']} at {fac.name} has no stock record",
                            f"{r['open_orders']} open production order(s) still need {r['reserved']:,.0f} {u} of {r['name']}, "
                            f"but the plant holds no stock record, so cover and run-out cannot be projected.",
                            [f"Committed need {r['reserved']:,.0f} {u}", f"On order {r['incoming']:,.0f} {u}"],
                            "Cycle-count the material and create its stock record so consumption and reorder alerts work.", link))
            continue
        if r["status"] in ("Critical", "Low"):
            uncovered = r["uncovered"] > 0
            title = (f"{r['code']} at {fac.name}: open orders need {r['uncovered']:,.0f} {u} more than stock + open POs"
                     if uncovered else f"{r['code']} at {fac.name} covers {r['days_cover']} days")
            detail = (f"{r['on_hand']:,.0f} {u} on hand at {r['consumption_per_day']:,.0f} {u}/day: stock-out "
                      f"{when(r['runout_at'], now)}. {r['open_orders']} open production order(s) still need {r['reserved']:,.0f} {u}"
                      + (f"; stock + open POs ({r['on_hand'] + r['incoming']:,.0f} {u}) leave {r['uncovered']:,.0f} {u} uncovered."
                         if uncovered else "."))
            if late:
                rec = f"Expedite {late['po']} ({late['ref']}): usable {when(late['usable_at'], now)}, after the stock-out"
            elif uncovered or not r["incoming"]:
                rec = "Raise a purchase order now"
            else:
                rec = "Open deliveries arrive before the stock-out; raise a PO to restore cover above the reorder threshold"
            out.append(_ins(f"runout-{r['id']}", "high" if r["status"] == "Critical" else "medium", "Material run-out",
                            title, detail,
                            [f"Reorder threshold {r['reorder_days']} days",
                             f"On order {r['incoming']:,.0f} {u}" + (f" ({r['incoming_in_time']:,.0f} usable before the stock-out)"
                                                                     if r["incoming"] else ""),
                             f"Next delivery {when(r['next_delivery'], now) if r['next_delivery'] else 'none scheduled'}"],
                            rec, link))
            sups = [s for s in suppliers if r["code"] in (s.material_codes or [])]
            if sups:
                best = min(sups, key=lambda s: s.lead_time_days)
                qty = inventory.recommended_reorder(r, best.lead_time_days)
                if qty > 0:
                    cycle = r["consumption_per_day"] * (best.lead_time_days + 3 + 7)
                    basis = (f"Covers {best.lead_time_days} days lead time + 3 days safety + 7 days cycle at current consumption"
                             if cycle >= r["reserved"] + r["consumption_per_day"] * 3 else
                             f"Covers the {r['reserved']:,.0f} {u} committed to open production orders + 3 days safety stock")
                    out.append(_ins(f"reorder-{r['id']}", "medium", "Reorder quantity",
                                    f"Order {qty:,.0f} {u} of {r['code']} for {fac.code}",
                                    f"{basis}, net of {r['on_hand']:,.0f} {u} on hand and {r['incoming']:,.0f} {u} on order.",
                                    [f"Consumption {r['consumption_per_day']:,.0f}/day", f"On hand {r['on_hand']:,.0f}",
                                     f"Committed {r['reserved']:,.0f}", f"On order {r['incoming']:,.0f}"],
                                    f"Create PO with {best.name} (lead time {best.lead_time_days} d)", link,
                                    {"type": "create_po", "label": "Create PO", "payload": {
                                        "factory_id": r["factory_id"], "material_id": r["material_id"], "supplier_id": best.id,
                                        "qty": qty, "expected_days": best.lead_time_days}}))
        elif late:  # cover is above the threshold, but the next delivery still lands after the stock-out
            out.append(_ins(f"expedite-{r['factory_id']}-{r['material_id']}", "low", "Expedite delivery",
                            f"Expedite {late['po']}: {r['code']} at {fac.code} runs out before it arrives",
                            f"Stock lasts {r['days_cover']} days (stock-out {when(r['runout_at'], now)}) but {late['ref']} "
                            f"is usable only {when(late['usable_at'], now)}.",
                            [f"Next delivery {when(r['next_delivery'], now)}", f"On order {r['incoming']:,.0f} {u}"],
                            "Request expedited freight or a partial early shipment.",
                            {"page": "procurement", "entity_type": "purchase_order", "entity_id": late["po_id"]}))
    for s in supplier_performance(db, fids):
        if s["on_time_pct"] is not None and (s["on_time_pct"] < 80 or s["open_delayed"]):
            out.append(_ins(f"supplier-{s['supplier_id']}", "medium", "Supplier reliability",
                            f"{s['supplier']} has recurring delivery delays",
                            f"{s['late']} of {s['deliveries']} recent deliveries late (avg {s['avg_late_days']} days); "
                            f"{s['open_delayed']} open shipment(s) currently delayed.",
                            [f"On-time {s['on_time_pct']}%", f"Quoted lead time {s['lead_time_days']} days"],
                            "Add buffer stock for materials from this supplier and qualify an alternate source.",
                            {"page": "procurement", "entity_type": "supplier", "entity_id": s["supplier_id"]}))
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
                        f"Worst: {worst.number} est. {when(worst.estimated_completion, now)} vs required {when(worst.required_completion, now)}."
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
    # same window and minimum sample as the Quality analytics chart and the AI assistant
    ranked = [r for r in qsvc.customer_error_rates(db, fids, now) if r["ranked"]]
    if ranked and ranked[0]["errors"] > 0:
        top = ranked[0]
        out.append(_ins("customer-errors", "medium", "Customer impact", f"{top['customer']} has the highest packaging error rate",
                        f"{top['error_rate_pct']:.2f}% of cartons ({top['errors']} of {top['inspected']:,}) failed first-pass "
                        f"inspection in the last {qsvc.ERROR_RATE_DAYS} days.",
                        [f"{r['customer']} {r['error_rate_pct']:.2f}% ({r['errors']}/{r['inspected']:,})" for r in ranked[1:4]]
                        + [f"Customers with fewer than {qsvc.ERROR_RATE_MIN_CARTONS} cartons are not ranked"],
                        "Add a second count check for this customer's SKUs until the rate falls below 0.3%.",
                        {"page": "quality", "entity_type": "customer", "entity_id": top["customer_id"]}))
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
    for due, o, t in urgent[:3]:
        out.append(_ins(f"urgent-pick-{o.id}", "high", "Picking priority", f"Pick {o.number} before {when(due, now)}",
                        f"{(o.allocated_qty - o.picked_qty):,} allocated pillows not yet picked; truck {t.ref} at {t.dock}.",
                        [f"Readiness {info[o.id]['readiness']['pct']}%"], "Expedite picking for this order.",
                        {"page": "warehouse", "entity_type": "order", "entity_id": o.id},
                        {"type": "expedite_picking", "label": "Expedite picking", "payload": {"order_id": o.id}}))
    soon = [o for o in orders if o.scheduled_dispatch and o.scheduled_dispatch <= now + timedelta(hours=36)]
    cover = stock_cover(db, soon, now)  # same walk as the Allocation tab's "Shortage" column
    for o in sorted(soon, key=lambda x: x.scheduled_dispatch):
        pending = max(0, o.qty - o.allocated_qty)
        short = pending - cover.get(o.id, 0)
        if short <= 0:
            continue
        pos = info[o.id]["pos"]
        po = next((p for p in pos if p.status != "Completed" and p.completed_qty < p.required_qty), None)
        stock_txt = (f"Stored stock covers {cover[o.id]:,} of the {pending:,} pillows still to allocate (after earlier orders). "
                     if cover.get(o.id) else "No unallocated stock is free for this order. ")
        if po:
            prod_txt = (f"Linked production order {po.number} est. {when(po.estimated_completion, now)}." if po.estimated_completion
                        else f"Linked production order {po.number} has no completion estimate yet.")
        elif pos:
            prod_txt = f"Its production order {pos[0].number} is complete; the remaining {short:,} pillows need top-up production."
        else:
            prod_txt = "No production order linked."
        out.append(_ins(f"short-{o.id}", "medium", "Inventory shortage",
                        f"{o.number} is short {short:,} pillows of {o.sku.code}", stock_txt + prod_txt,
                        [f"Ships {when(o.scheduled_dispatch, now)}"],
                        "Allocate the free stock and prioritize the linked production order." if cover.get(o.id)
                        else "Prioritize the linked production order.",
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
    gap = round(wl["required_fte"] - wl["deployed"], 1)
    if abs(gap) >= 1:
        out.append(_ins("wh-workforce", "medium" if gap > 0 else "low", "Workforce",
                        f"Warehouse needs {wl['required_fte']} FTE vs {wl['deployed']} deployed (next {wl['window_h']} h)",
                        f"Workload in the next {wl['window_h']} h: {wl['inbound_shipments']} inbound shipment(s), "
                        f"{wl['pallets_to_putaway']} pallets to put away, {wl['cartons_to_pick']:,} cartons to pick for trucks "
                        f"due by {when(wl['window_end'], now)}, {wl['outbound_loads']} truck load(s) = {wl['workload_hours']} h "
                        f"(utilization {wl['utilization_pct']}%).",
                        [f"{wl['cartons_reserved_total']:,} reserved cartons in total; picks for later trucks can wait",
                         "Required FTE = workload hours ÷ productive hours per associate-shift"],
                        f"Short {gap} FTE: move associates from put-away to picking for trucks due first." if gap > 0
                        else f"{abs(gap)} FTE spare: release associates to packing support.",
                        {"page": "warehouse", "entity_type": "workforce", "entity_id": None}))
    return out


def workload(db: Session, fids: list[int], now: datetime, window_h: float = WORKLOAD_WINDOW_H) -> dict:
    """Warehouse work due in the next `window_h` hours (Warehouse workload tab and S!AP FTE insight).

    inbound: shipments at the dock now + shipments due in the window (not yet arrived, so nothing is counted twice)
    put-away: pallets waiting + pallets the lines can fill in the window's productive minutes
    picking: reserved cartons whose truck (or, without a truck, the order's dispatch time) falls in the window;
             picking may start up to 24 h ahead, so picks for later trucks are reported but not counted
    loads: truck appointments in the window (including overdue ones still at the yard)"""
    end = now + timedelta(hours=window_h)
    S = m.InboundShipment
    at_dock = db.scalar(select(func.count()).select_from(S).where(
        S.factory_id.in_(fids), S.status.in_(("Arrived", "Receiving")))) or 0
    due_in = db.scalar(select(func.count()).select_from(S).where(
        S.factory_id.in_(fids), S.status.in_(("Scheduled", "In Transit")),
        S.expected_arrival >= now - timedelta(hours=2), S.expected_arrival <= end)) or 0
    pallets = db.scalar(select(func.count()).select_from(m.Pallet).where(m.Pallet.factory_id.in_(fids),
                                                                          m.Pallet.status == "Pending Put-away")) or 0
    run_min = cal.productive_minutes(now, end)
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    upcoming_putaway = 0.0
    for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))):
        sku = skus.get(ln.current_sku_id)
        per_pallet = (sku.cartons_per_pallet * sku.pillows_per_carton) if sku and sku.cartons_per_pallet else 300
        upcoming_putaway += ln.planned_rate * run_min / 60 / max(1, per_pallet)
    upcoming_putaway = round(upcoming_putaway)
    trucks = {}
    for t in db.scalars(select(m.TruckAppointment).where(m.TruckAppointment.factory_id.in_(fids),
                                                         m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")))):
        if t.order_id and (t.order_id not in trucks or t.scheduled_arrival < trucks[t.order_id]):
            trucks[t.order_id] = t.scheduled_arrival
    cartons = total_reserved = 0
    for a_ctn, oid, dispatch in db.execute(select(m.Allocation.cartons, m.CustomerOrder.id, m.CustomerOrder.scheduled_dispatch)
                                           .join(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids),
                                                                        m.Allocation.status == "Reserved")):
        total_reserved += a_ctn
        due = trucks.get(oid) or dispatch
        if due and due <= end:
            cartons += a_ctn
    loads = db.scalar(select(func.count()).select_from(m.TruckAppointment).where(
        m.TruckAppointment.factory_id.in_(fids), m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")),
        m.TruckAppointment.scheduled_arrival <= end)) or 0
    hours = (at_dock + due_in) * 1.5 + (pallets + upcoming_putaway) * 0.08 + cartons / 90 + loads * 1.25
    shift = cal.shift_of(now) or 1
    deployed = db.scalar(select(func.count()).select_from(m.Employee).where(
        m.Employee.factory_id.in_(fids), m.Employee.role.in_(("Warehouse associate", "Forklift operator")),
        m.Employee.shift == shift, m.Employee.attendance.in_(("Present", "Late")))) or 0
    req = round(hours / (cal.productive_minutes_per_shift() / 60), 1)
    return {"window_h": window_h, "window_end": end.isoformat(timespec="minutes"),
            "inbound_shipments": at_dock + due_in, "inbound_at_dock": at_dock, "inbound_due": due_in,
            "pallets_to_putaway": pallets + upcoming_putaway, "pallets_pending": pallets, "pallets_from_lines": upcoming_putaway,
            "cartons_to_pick": int(cartons), "cartons_reserved_total": int(total_reserved),
            "outbound_loads": loads, "workload_hours": round(hours, 1),
            "required_fte": req, "deployed": deployed,
            "utilization_pct": round(100 * req / deployed, 1) if deployed else None}


def dispatch(db: Session, fids: list[int], now: datetime) -> list[dict]:
    out = []
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)

    def urgency(x):  # no ready ETA (nothing covers the gap) sorts first among the at-risk orders
        r = info[x.id]["risk"]
        return r["slack_min"] if r["slack_min"] is not None else (-1e9 if r["at_risk"] else 1e9)

    for o in sorted(orders, key=urgency):
        r = info[o.id]["risk"]
        if not r["at_risk"]:
            continue
        recs = readiness.recommendations(o, info[o.id], now)
        t = info[o.id]["truck"]
        eta = info[o.id]["ready_eta"]
        late = -r["slack_min"] if r["slack_min"] is not None else None
        ready_txt = ("Projected ready: unknown — no stock or production covering the gap." if eta is None
                     else f"Projected ready {when(eta, now)}" + (
                         "." if late is None else f", {late:.0f} min after arrival." if late < 120
                         else f", {late / 60:.1f} h after arrival."))
        out.append(_ins(f"risk-{o.id}", "high" if r["level"] == "High" else "medium", "Shipment risk",
                        f"{o.number} ({o.customer.name}) at risk for {when(t.scheduled_arrival, now) if t else 'its'} truck",
                        ready_txt
                        + (f" Estimated detention ${r['est_detention']:,.0f}." if r["est_detention"] else ""),
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


def loss_prevention(db: Session, fids: list[int], now: datetime) -> list[dict]:
    """Loss-prevention page: the cases to work first and where the losses concentrate."""
    from . import loss
    out = []
    rows = loss.incidents(db, fids, status="active")
    sev = {"critical": "high", "warning": "medium", "info": "low"}
    for r in rows[:3]:
        nxt = loss.next_step(r)
        age = (now - r.detected_at).total_seconds() / 3600 if r.detected_at else 0
        out.append(_ins(f"loss-{r.id}", sev.get(r.severity, "medium"), "Loss prevention", f"{r.ref} · {r.title}",
                        f"{r.zone} · ${r.value_usd or 0:,.0f} at standard cost · open {age:.1f} h · {r.status.lower()}.",
                        {"case": r.ref, "value_usd": round(r.value_usd or 0, 2), "hours_open": round(age, 1)},
                        (nxt[1] if nxt else "Close the case with an outcome") + ".",
                        {"page": "loss-prevention", "entity_type": "loss_incident", "entity_id": r.id}))
    since = loss.window_start(now)  # same 7-day window (incl. today) as the Loss Prevention KPIs
    week = [r for r in loss.incidents(db, fids) if r.detected_at and r.detected_at >= since]
    conf = [r for r in week if r.outcome == "Confirmed loss"]
    if conf:
        by_zone = defaultdict(lambda: [0, 0.0])
        for r in conf:
            by_zone[r.zone][0] += 1
            by_zone[r.zone][1] += r.value_usd or 0
        zone, (n, val) = max(by_zone.items(), key=lambda kv: (kv[1][1], kv[1][0]))  # by value written off
        total = sum(v for _, v in by_zone.values())
        out.append(_ins("loss-zone", "medium", "Loss prevention", f"Confirmed losses concentrate at {zone}",
                        f"${val:,.0f} of ${total:,.0f} written off in 7 days ({n} of {len(conf)} confirmed-loss cases).",
                        {"zone": zone, "cases": n, "value_usd": round(val, 2), "total_value_usd": round(total, 2)},
                        "Add the zone to daily cycle counts and point a PTZ preset at it during shift change.",
                        {"page": "loss-prevention", "entity_type": "loss_zone", "entity_id": zone}))
    closed = [r for r in week if r.status == "Resolved"]
    fa = [r for r in closed if r.outcome == "False alarm"]
    if closed and len(fa) / len(closed) >= 0.25:
        k = Counter(r.kind for r in fa).most_common(1)[0][0]
        out.append(_ins("loss-false-alarms", "low", "Loss prevention",
                        f"{round(100 * len(fa) / len(closed))}% of closed cases were false alarms",
                        f"Most come from {loss.KINDS[k]['label'].lower()} ({loss.KINDS[k]['channel']}).",
                        {"closed": len(closed), "false_alarms": len(fa)},
                        "Tune that detector (antenna power, zone geometry, badge policy) so security time goes to real losses.",
                        {"page": "loss-prevention", "entity_type": "loss_kind", "entity_id": k}))
    return out


PAGES = {"overview": overview, "procurement": procurement, "production": production, "quality": quality,
         "warehouse": warehouse, "dispatch": dispatch, "loss-prevention": loss_prevention}
