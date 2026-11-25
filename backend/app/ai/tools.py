"""Data tools for the AI Command Center.

Every tool queries the same database the dashboards use and returns:
  summary  - one-paragraph factual answer
  data     - compact rows (sent to the LLM as the tool result)
  chart    - optional chart spec for the UI {type, title, labels, datasets}
  table    - optional table {columns, rows}
  actions  - optional recommended actions
  links    - records the answer is based on (page/entity) so the UI can open them
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..services import inventory, machine_health as mh, metrics, planning, quality, readiness, workforce
from ..services.common import factory_ids, iso
from ..services.insights import supplier_performance


def _fname(db, fids):
    if len(fids) > 1:
        return "all factories"
    f = db.get(m.Factory, fids[0])
    return f"{f.name}, {f.state}"


def _res(summary, data=None, chart=None, table=None, actions=None, links=None):
    return {"summary": summary, "data": data, "chart": chart, "table": table, "actions": actions or [], "links": links or []}


def kpi_snapshot(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    k = metrics.kpis(db, fids, now.date(), None, None, now)
    p = k["production"]
    s = (f"{_fname(db, fids)} today: {p['actual']:,} pillows vs {p['target']:,} target ({p['achievement_pct']}%), "
         f"{k['efficiency']['pct']}% efficiency vs plan to date ({p['status']}). OEE {k['oee']['oee_pct']}% "
         f"(A {k['oee']['availability_pct']}% × P {k['oee']['performance_pct']}% × Q {k['oee']['quality_pct']}%). "
         f"Downtime {k['downtime']['total_min']} min; labour cost/pillow ${k['labour_cost']['per_pillow'] or 0:.3f}; "
         f"packaging accuracy {k['packaging']['accuracy_pct']}%; {k['shipment']['ready']}/{k['shipment']['scheduled']} "
         f"orders due in 48 h ready ({k['shipment']['at_risk']} at risk).")
    chart = {"type": "bar", "title": "OEE components (%)", "labels": ["Availability", "Performance", "Quality", "OEE"],
             "datasets": [{"label": "%", "data": [k["oee"]["availability_pct"], k["oee"]["performance_pct"],
                                                  k["oee"]["quality_pct"], k["oee"]["oee_pct"]]}]}
    return _res(s, k, chart, links=[{"page": "overview"}])


def material_runout(db: Session, factory: str, now: datetime, days: float = 5, **_):
    fids = factory_ids(db, factory)
    rows = [r for r in inventory.material_rows(db, fids, now) if r["days_cover"] is not None and r["days_cover"] <= days]
    if not rows:
        return _res(f"No raw material at {_fname(db, fids)} is projected to run out within {days:g} days.", [])
    lines = [f"{r['code']} ({r['name']}) at {r['factory']}: {r['available']:,.0f} {r['uom']}, {r['days_cover']} days cover"
             f" — next delivery {r['next_delivery'] or 'none scheduled'}" for r in rows]
    s = f"{len(rows)} material(s) may run out within {days:g} days: " + "; ".join(lines) + "."
    chart = {"type": "bar", "title": "Days of stock remaining", "labels": [f"{r['code']} {r['factory']}" for r in rows],
             "datasets": [{"label": "Days cover", "data": [r["days_cover"] for r in rows],
                           "color": ["danger" if r["status"] == "Critical" else "warning" for r in rows]}]}
    table = {"columns": ["Material", "Factory", "Available", "Use/day", "Days cover", "Incoming", "Next delivery"],
             "rows": [[r["code"], r["factory"], f"{r['available']:,.0f} {r['uom']}", f"{r['consumption_per_day']:,.0f}",
                       r["days_cover"], f"{r['incoming']:,.0f}", (r["next_delivery"] or "—").replace("T", " ")] for r in rows]}
    acts = [f"Expedite or raise a PO for {r['code']} at {r['factory']}" for r in rows if r["status"] == "Critical"]
    return _res(s, rows, chart, table, acts, [{"page": "procurement", "entity_type": "material", "entity_id": r["material_id"]} for r in rows[:3]])


def supplier_delays(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    rows = supplier_performance(db, fids)
    bad = [r for r in rows if (r["on_time_pct"] or 100) < 85 or r["open_delayed"]]
    s = ("Suppliers causing delays: " + "; ".join(
        f"{r['supplier']} — {r['late']} of {r['deliveries']} deliveries late (on-time {r['on_time_pct']}%, avg {r['avg_late_days']} days late)"
        + (f", {r['open_delayed']} open shipment(s) delayed now" if r["open_delayed"] else "") for r in bad)) if bad else \
        "No supplier currently shows recurring delays."
    chart = {"type": "bar", "title": "Supplier on-time delivery (%)", "labels": [r["supplier"] for r in rows],
             "datasets": [{"label": "On-time %", "data": [r["on_time_pct"] for r in rows],
                           "color": ["danger" if (r["on_time_pct"] or 100) < 70 else "warning" if (r["on_time_pct"] or 100) < 85 else "success" for r in rows]}]}
    return _res(s, rows, chart, actions=[f"Add buffer stock / alternate source for {r['supplier']}" for r in bad[:2]],
                links=[{"page": "procurement", "entity_type": "supplier", "entity_id": r["supplier_id"]} for r in bad[:2]])


def line_downtime(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    day0 = datetime.combine(now.date(), datetime.min.time())
    by_line = defaultdict(lambda: defaultdict(float))
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine))}
    for d in db.scalars(select(m.DowntimeEvent).where(m.DowntimeEvent.factory_id.in_(fids), m.DowntimeEvent.start >= day0)):
        by_line[d.line_id][d.category] += d.minutes or 0
    if not by_line:
        return _res("No downtime has been recorded today.", [])
    rows = sorted(({"line": lines[k].name, "factory": db.get(m.Factory, lines[k].factory_id).code, "line_id": k,
                    "total_min": round(sum(v.values()), 1), **{c: round(x, 1) for c, x in v.items()}} for k, v in by_line.items()),
                  key=lambda r: -r["total_min"])
    top = rows[0]
    cats = sorted(((c, top.get(c, 0)) for c in metrics.DOWNTIME_CATEGORIES if top.get(c)), key=lambda x: -x[1])
    s = (f"{top['line']} ({top['factory']}) has the highest downtime today: {top['total_min']:.0f} min "
         f"({', '.join(f'{c} {v:.0f} min' for c, v in cats)}). Next: " +
         ", ".join(f"{r['line']} ({r['factory']}) {r['total_min']:.0f} min" for r in rows[1:4]) + ".")
    top8 = rows[:8]
    chart = {"type": "bar", "stacked": True, "title": "Downtime by line today (min)",
             "labels": [f"{r['line']} {r['factory']}" for r in top8],
             "datasets": [{"label": c, "data": [r.get(c, 0) for r in top8]} for c in metrics.DOWNTIME_CATEGORIES]}
    return _res(s, rows[:10], chart, links=[{"page": "production", "entity_type": "line", "entity_id": top["line_id"]}])


def production_gap(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    p = metrics.production(db, fids, now.date(), None, now)
    a, b = metrics.scope_window(now.date(), None, now)
    dt = metrics.downtime(db, fids, a, b)
    pipe = metrics.pipeline(db, fids, now.date(), now)
    gap = p["planned_to_date"] - p["actual"]
    avg_rate = sum(ln.planned_rate for ln in p["lines"]) / max(1, len(p["lines"]))
    lost = {c: round(v * avg_rate / 60) for c, v in dt.items() if v}
    perf_loss = max(0, gap - sum(lost.values()))
    lines = sorted(((ln, p["by_line"][ln.id]["actual"]) for ln in p["lines"]), key=lambda x: x[1] / max(1, x[0].planned_rate))
    elapsed = metrics.cal.productive_minutes(a, b)
    under = [(ln, act, ln.planned_rate * elapsed / 60) for ln, act in lines if act < 0.88 * ln.planned_rate * elapsed / 60][:4]
    if gap <= 0:
        s = f"Production at {_fname(db, fids)} is on or ahead of plan: {p['actual']:,} vs {p['planned_to_date']:,} planned to date."
    else:
        drivers = sorted(lost.items(), key=lambda x: -x[1])
        s = (f"{_fname(db, fids)} is {gap:,} pillows behind plan to date ({p['actual']:,} vs {p['planned_to_date']:,}, "
             f"{p['efficiency_pct']}%). Estimated losses: " + ", ".join(f"{c} ≈{v:,} pillows" for c, v in drivers) +
             f", speed/performance losses ≈{perf_loss:,} pillows. ")
        if pipe["bottleneck"]:
            st = next(x for x in pipe["stages"] if x["stage"] == pipe["bottleneck"])
            s += f"Current bottleneck: {pipe['bottleneck']} (constraining {', '.join(st['constrained_lines'][:3]) or 'several lines'}). "
        if under:
            s += "Lines furthest below plan: " + ", ".join(f"{ln.name} ({db.get(m.Factory, ln.factory_id).code}) {act / max(1, plan) * 100:.0f}%"
                                                          for ln, act, plan in under) + "."
    chart = {"type": "bar", "title": "Lost output by cause (pillows, est.)", "labels": list(lost) + ["Speed / performance"],
             "datasets": [{"label": "Pillows", "data": list(lost.values()) + [perf_loss], "color": "danger"}]}
    acts = ["Run Optimize Production Plan", "Address the bottleneck stage first"] if gap > 0 else []
    return _res(s, {"gap": gap, "losses": lost, "performance_loss": perf_loss, "bottleneck": pipe["bottleneck"]}, chart,
                actions=acts, links=[{"page": "production"}])


def line_allocation(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    fid = fids[0]
    if len(fids) > 1:  # pick the factory with the most late orders
        late = {f: db.scalar(select(func.count()).select_from(m.ProductionOrder).where(
            m.ProductionOrder.factory_id == f, m.ProductionOrder.status == "Delayed")) or 0 for f in fids}
        fid = max(late, key=late.get)
    run = planning.optimize(db, fid, now, "ai-assistant")
    b, a, mt = run.before["summary"], run.after["summary"], run.metrics
    seq = []
    for ln in run.after["lines"]:
        if ln["orders"]:
            seq.append(f"{ln['line']}: " + " → ".join(f"{o['po']} ({o['sku']})" for o in ln["orders"][:3]))
    f = db.get(m.Factory, fid)
    s = (f"Recommended allocation for {f.name} (proposal #{run.id}, not yet applied): late orders {b['late_orders']} → {a['late_orders']}, "
         f"total delay {b['total_delay_h']} h → {a['total_delay_h']} h (−{mt['delay_reduction_h']} h), changeovers {b['changeovers']} → "
         f"{a['changeovers']}, 24-h output {b['output_24h']:,} → {a['output_24h']:,}. Sequence: " + "; ".join(seq[:5]) + ".")
    chart = {"type": "bar", "title": "Current vs optimized plan", "labels": ["Late orders", "Delay (h)", "Changeovers"],
             "datasets": [{"label": "Current", "data": [b["late_orders"], b["total_delay_h"], b["changeovers"]], "color": "chart-8"},
                          {"label": "Optimized", "data": [a["late_orders"], a["total_delay_h"], a["changeovers"]], "color": "primary"}]}
    return _res(s, {"run_id": run.id, "before": b, "after": a, "metrics": mt}, chart,
                actions=["Review and accept the proposal in Production → Optimization"],
                links=[{"page": "production", "entity_type": "optimization", "entity_id": run.id}])


def labour_cost(db: Session, factory: str, now: datetime, **_):
    out = []
    for f in db.scalars(select(m.Factory).order_by(m.Factory.id)):
        if factory and factory.upper() != "ALL" and f.code != factory.upper():
            continue
        p = metrics.production(db, [f.id], now.date(), None, now)
        wf = workforce.summary(db, [f.id], now.date(), now, p["actual"])
        out.append({"factory": f.code, "name": f.name, "pillows": p["actual"], "labour_cost": wf["labour_cost"],
                    "per_pillow": wf["labour_cost_per_pillow"], "per_hour": wf["labour_cost_per_hour"],
                    "deployed": wf["workers_deployed"]})
    tot_cost = sum(r["labour_cost"] for r in out)
    tot_p = sum(r["pillows"] for r in out)
    diff = workforce.business_rules(db)["shift2_differential"]
    s = (f"Labour cost per pillow today: ${tot_cost / tot_p:.3f} overall (${tot_cost:,.0f} paid labour / {tot_p:,} pillows). " if tot_p else "No output yet today. ") + \
        "; ".join(f"{r['name']} ${r['per_pillow']:.3f}" for r in out if r["per_pillow"]) + \
        f". Paid time excludes the 30-min unpaid lunch; second shift includes the ${diff:.2f}/h differential."
    chart = {"type": "bar", "title": "Labour cost per pillow ($)", "labels": [r["name"] for r in out],
             "datasets": [{"label": "$ / pillow", "data": [r["per_pillow"] or 0 for r in out]}]}
    return _res(s, out, chart, links=[{"page": "production", "entity_type": "workforce"}])


def idle_time(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    rows = workforce.employee_rows(db, fids, now.date(), now)
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine))}
    agg = defaultdict(float)
    for r in rows:
        if r["line_id"] and r["role"] in ("Filler", "Sewer", "Bagger"):
            agg[r["line_id"]] += r["idle_min"]
    if not agg:
        return _res("No idle time recorded yet today.", [])
    top = sorted(agg.items(), key=lambda x: -x[1])[:8]
    lid, mins = top[0]
    ln = lines[lid]
    s = (f"{ln.name} ({db.get(m.Factory, ln.factory_id).code}) has the highest idle time today: {mins / 60:.1f} worker-hours "
         f"(current status: {ln.status}{' — ' + ln.status_reason if ln.status_reason else ''}). Others: " +
         ", ".join(f"{lines[k].name} ({db.get(m.Factory, lines[k].factory_id).code}) {v / 60:.1f} h" for k, v in top[1:4]) + ".")
    chart = {"type": "bar", "title": "Idle worker-hours by line", "labels": [f"{lines[k].name} {db.get(m.Factory, lines[k].factory_id).code}" for k, _ in top],
             "datasets": [{"label": "Idle h", "data": [round(v / 60, 1) for _, v in top], "color": "warning"}]}
    return _res(s, [{"line": lines[k].name, "idle_hours": round(v / 60, 1)} for k, v in top], chart,
                links=[{"page": "production", "entity_type": "line", "entity_id": lid}])


def carton_failures(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    day0 = datetime.combine(now.date(), datetime.min.time())
    c = quality.quality_counts(db, fids, day0)
    defects = list(db.scalars(select(m.DefectRecord).where(m.DefectRecord.factory_id.in_(fids), m.DefectRecord.created_at >= day0)))
    cats = defaultdict(int)
    for d in defects:
        cats[d.category] += 1
    lines = {ln.id: ln.name for ln in db.scalars(select(m.ProductionLine))}
    s = (f"{c['failed']} of {c['inspected']:,} cartons failed first-pass inspection today at {_fname(db, fids)} "
         f"(packaging accuracy {c['accuracy_pct']}%). Currently blocked: {c['blocked']}; reworked {c['reworked']}, "
         f"overridden {c['overridden']}, rejected {c['rejected']}. By category: " +
         (", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])) or "none") + ".")
    chart = {"type": "doughnut", "title": "Failed inspections by category", "labels": list(cats), "datasets": [{"data": list(cats.values())}]} if cats else None
    table = {"columns": ["Time", "Line", "SKU", "Category", "Status"],
             "rows": [[d.created_at.strftime("%H:%M"), lines.get(d.line_id, "—"), db.get(m.Sku, d.sku_id).code, d.category, d.status]
                      for d in sorted(defects, key=lambda x: x.created_at, reverse=True)[:12]]}
    return _res(s, c | {"categories": dict(cats)}, chart, table, links=[{"page": "quality"}])


def packaging_errors_by_customer(db: Session, factory: str, now: datetime, days: int = 7, **_):
    fids = factory_ids(db, factory)
    since = now - timedelta(days=days)
    rows = defaultdict(lambda: [0, 0])
    for cid, fp, n in db.execute(select(m.Carton.customer_id, m.Carton.first_pass, func.count()).where(
            m.Carton.factory_id.in_(fids), m.Carton.created_at >= since).group_by(m.Carton.customer_id, m.Carton.first_pass)):
        rows[cid][0] += n
        if not fp:
            rows[cid][1] += n
    cust = {c.id: c.name for c in db.scalars(select(m.Customer))}
    data = sorted(({"customer": cust[k], "inspected": v[0], "errors": v[1], "rate_pct": round(100 * v[1] / v[0], 2) if v[0] else 0}
                   for k, v in rows.items()), key=lambda r: -r["rate_pct"])
    if not data:
        return _res("No carton inspections recorded in that period.", [])
    top = data[0]
    s = (f"{top['customer']} has the highest packaging error rate: {top['rate_pct']}% ({top['errors']} of {top['inspected']:,} cartons) "
         f"over the cartons recorded in the system. Others: " + ", ".join(f"{r['customer']} {r['rate_pct']}%" for r in data[1:5]) + ".")
    chart = {"type": "bar", "title": "Packaging error rate by customer (%)", "labels": [r["customer"] for r in data],
             "datasets": [{"label": "Error %", "data": [r["rate_pct"] for r in data], "color": "danger"}]}
    return _res(s, data, chart, links=[{"page": "quality"}])


def orders_waiting_for_picking(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    agg = defaultdict(lambda: {"cartons": 0, "pillows": 0, "locations": set()})
    for a in db.scalars(select(m.Allocation).join(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids), m.Allocation.status == "Reserved")):
        x = agg[a.order_id]
        x["cartons"] += a.cartons
        x["pillows"] += a.pillows
        if a.pallet.location:
            x["locations"].add(a.pallet.location.code)
    if not agg:
        return _res("No orders are currently waiting for picking.", [])
    trucks = readiness.truck_for_orders(db, list(agg))
    rows = []
    for oid, x in agg.items():
        o = db.get(m.CustomerOrder, oid)
        t = trucks.get(oid)
        rows.append({"order": o.number, "order_id": oid, "customer": o.customer.name, "sku": o.sku.code, "cartons": x["cartons"],
                     "pillows": x["pillows"], "truck": f"{t.ref} {t.scheduled_arrival:%a %H:%M}" if t else "—",
                     "locations": sorted(x["locations"])[:4]})
    rows.sort(key=lambda r: r["truck"])
    s = f"{len(rows)} orders are waiting for picking: " + "; ".join(
        f"{r['order']} ({r['customer']}, {r['cartons']} cartons, truck {r['truck']})" for r in rows[:6]) + "."
    table = {"columns": ["Order", "Customer", "SKU", "Cartons", "Truck", "Locations"],
             "rows": [[r["order"], r["customer"], r["sku"], r["cartons"], r["truck"], ", ".join(r["locations"])] for r in rows]}
    return _res(s, rows, table=table, links=[{"page": "warehouse", "entity_type": "order", "entity_id": rows[0]["order_id"]}])


def locate_sku(db: Session, factory: str, now: datetime, sku: str = "", **_):
    fids = factory_ids(db, factory)
    q = (sku or "").upper().strip()
    skus = [s for s in db.scalars(select(m.Sku)) if q and (q in s.code or q in s.description.upper())]
    if not skus:
        return _res(f"I couldn't find a SKU matching “{sku}”. Known SKUs: " + ", ".join(s.code for s in db.scalars(select(m.Sku))) + ".", [])
    out = []
    for s in skus:
        for p in db.scalars(select(m.Pallet).where(m.Pallet.factory_id.in_(fids), m.Pallet.sku_id == s.id, m.Pallet.status == "Stored")):
            out.append({"sku": s.code, "location": p.location.code if p.location else "—", "pallet": p.code, "cartons": p.cartons,
                        "pillows": p.cartons * s.pillows_per_carton, "reserved_cartons": p.allocated_cartons,
                        "factory": db.get(m.Factory, p.factory_id).code, "location_id": p.location_id})
    if not out:
        return _res(f"{', '.join(s.code for s in skus)} has no stored finished goods at {_fname(db, fids)} right now.", [])
    tot = sum(r["pillows"] for r in out)
    zones = sorted({r["location"].split("-")[1][0] for r in out if r["location"] != "—"})
    s_ = (f"{', '.join(s.code for s in skus)} is stored in {len(out)} pallet location(s) — {tot:,} pillows "
          f"({sum(r['reserved_cartons'] for r in out)} cartons reserved). Zones {', '.join(zones)}; e.g. " +
          ", ".join(f"{r['location']} ({r['cartons']} ctn)" for r in out[:5]) + ".")
    table = {"columns": ["Location", "Pallet", "SKU", "Cartons", "Reserved"],
             "rows": [[r["location"], r["pallet"], r["sku"], r["cartons"], r["reserved_cartons"]] for r in out[:20]]}
    return _res(s_, out[:20], table=table, links=[{"page": "warehouse", "entity_type": "location", "entity_id": out[0]["location_id"]}])


def trucks_at_risk(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    orders = list(db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open")))
    info = readiness.assess(db, orders, now)
    risky = [o for o in orders if info[o.id]["risk"]["at_risk"]]
    if not risky:
        return _res(f"No truck appointments at {_fname(db, fids)} are currently at risk.", [])
    rows = []
    for o in sorted(risky, key=lambda x: info[x.id]["risk"]["slack_min"]):
        t, r = info[o.id]["truck"], info[o.id]["risk"]
        rows.append({"order": o.number, "order_id": o.id, "customer": o.customer.name, "truck": t.ref, "dock": t.dock,
                     "arrival": t.scheduled_arrival.strftime("%a %H:%M"), "ready_eta": info[o.id]["ready_eta"].strftime("%a %H:%M"),
                     "late_min": -r["slack_min"], "level": r["level"], "reasons": r["reasons"],
                     "detention": r["est_detention"]})
    s = f"{len(rows)} truck appointment(s) at risk: " + "; ".join(
        f"{r['truck']} for {r['order']} ({r['customer']}) arrives {r['arrival']} but the order is projected ready {r['ready_eta']} "
        f"({r['late_min']} min late{', est. detention $' + format(r['detention'], ',.0f') if r['detention'] else ''})" for r in rows[:5]) + "."
    recs = []
    for o in risky[:2]:
        for rec in readiness.recommendations(o, info[o.id], now)[:2]:
            recs.append(f"{o.number}: {rec['label']}")
    chart = {"type": "bar", "title": "Projected lateness vs truck arrival (min)", "labels": [r["order"] for r in rows],
             "datasets": [{"label": "Minutes late", "data": [r["late_min"] for r in rows],
                           "color": ["danger" if r["level"] == "High" else "warning" for r in rows]}]}
    return _res(s, rows, chart, actions=recs, links=[{"page": "dispatch", "entity_type": "order", "entity_id": rows[0]["order_id"]}])


def detention_cost(db: Session, factory: str, now: datetime, days: int = 7, **_):
    fids = factory_ids(db, factory)
    d = readiness.detention_summary(db, fids, now, days)
    s = (f"Estimated detention over the last {days} days at {_fname(db, fids)}: ${d['incurred_estimate']:,.0f} from {d['delayed_trucks']} "
         f"delayed truck(s) (avg wait {d['avg_wait_min']} min, ${d['charge_per_truck']:,.0f} per qualifying truck after {d['free_minutes']:.0f} free minutes). "
         f"Projected for upcoming at-risk appointments: ${d['projected_estimate']:,.0f}. "
         + (f"Main reason: {d['reasons'][0]['name']}. " if d["reasons"] else "") + d["note"])
    chart = {"type": "bar", "title": "Estimated detention by customer ($)", "labels": [x["name"] for x in d["by_customer"]] or ["—"],
             "datasets": [{"label": "$", "data": [x["value"] for x in d["by_customer"]] or [0], "color": "warning"}]}
    return _res(s, {k: d[k] for k in ("incurred_estimate", "projected_estimate", "delayed_trucks", "avg_wait_min", "by_customer", "by_factory", "reasons")},
                chart, links=[{"page": "dispatch", "entity_type": "detention"}])


def machine_health_summary(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    lines = {ln.id: ln.name for ln in db.scalars(select(m.ProductionLine))}
    rows = []
    for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(fids)).order_by(m.Machine.health_score)):
        if mc.health_score < 85 or mc.status in ("Breakdown", "Maintenance"):
            p = mh.prediction(mc, mh.history(db, mc.id, 144), now) if mc.status == "Running" else None
            rows.append({"machine": mc.code, "id": mc.id, "name": mc.name, "line": lines.get(mc.line_id), "status": mc.status,
                         "health": round(mc.health_score), "class": mh.classify(mc.health_score),
                         "anomaly": p["anomaly"] if p else mc.status, "eta_h": p["est_hours_to_maintenance"] if p else None,
                         "window": p["suggested_window"] if p else None})
    if not rows:
        return _res("All machines are healthy (score ≥ 85).", [])
    s = "Machines needing attention: " + "; ".join(
        f"{r['machine']} {r['name']} on {r['line']} — {r['status']}, health {r['health']} ({r['class']}): {r['anomaly']}"
        + (f", est. {r['eta_h']:.0f} operating h to maintenance" if r["eta_h"] is not None else "") for r in rows[:5]) + \
        f". {mh.DISCLAIMER}"
    chart = {"type": "bar", "title": "Machine health score", "labels": [r["machine"] for r in rows[:10]],
             "datasets": [{"label": "Health", "data": [r["health"] for r in rows[:10]],
                           "color": ["danger" if r["health"] < 65 else "warning" for r in rows[:10]]}]}
    return _res(s, rows, chart, links=[{"page": "production", "entity_type": "machine", "entity_id": rows[0]["id"]}])


def order_status(db: Session, factory: str, now: datetime, order: str = "", **_):
    num = (order or "").upper().replace(" ", "")
    if num and not num.startswith("ORD-"):
        num = "ORD-" + num.lstrip("ORD").lstrip("-")
    o = db.scalar(select(m.CustomerOrder).where(m.CustomerOrder.number == num))
    if not o:
        return _res(f"Order {order} was not found in the order book.", None)
    info = readiness.assess(db, [o], now)[o.id]
    rd, r, t = info["readiness"], info["risk"], info["truck"]
    s = (f"{o.number} ({o.customer.name}, {o.qty:,} × {o.sku.code}) is {rd['status']} — readiness {rd['pct']}% "
         f"(production {rd['ratios']['production']}%, quality {rd['ratios']['quality']}%, inventory {rd['ratios']['inventory']}%, "
         f"allocated {rd['ratios']['allocation']}%, picked {rd['ratios']['picking']}%). ")
    if t:
        s += f"Truck {t.ref} at {t.dock}, {t.scheduled_arrival:%a %H:%M} ({t.status}). "
    if info["ready_eta"]:
        s += f"Projected ready {info['ready_eta']:%a %H:%M}. "
    if r["at_risk"]:
        s += f"AT RISK ({r['level']}): " + "; ".join(r["reasons"]) + "."
    chart = {"type": "bar", "title": f"{o.number} readiness (%)", "labels": [lbl for _, lbl in readiness.STAGES],
             "datasets": [{"label": "%", "data": [rd["ratios"][k] for k, _ in readiness.STAGES]}]}
    return _res(s, {"readiness": rd, "risk": r}, chart,
                actions=[x["label"] for x in readiness.recommendations(o, info, now)],
                links=[{"page": "dispatch", "entity_type": "order", "entity_id": o.id}])


def workforce_productivity(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    p = metrics.production(db, fids, now.date(), None, now)
    wf = workforce.summary(db, fids, now.date(), now, p["actual"])
    low = wf["below_benchmark"]
    s = (f"Workforce at {_fname(db, fids)}: {wf['workers_deployed']} deployed vs {wf['workers_required']} required, utilization "
         f"{wf['labour_utilization_pct']}%, idle {wf['idle_hours']} worker-hours, output {wf['output_per_worker']} pillows per worker. "
         + (f"{len(low)} employee(s) below the 80% productivity benchmark: " +
            ", ".join(f"{r['code']} {r['role']} on {r['line']} ({r['productivity_pct']}%)" for r in low[:5]) + "." if low else
            "No employee is significantly below benchmark."))
    return _res(s, {k: wf[k] for k in ("workers_deployed", "workers_required", "labour_utilization_pct", "idle_hours", "output_per_worker")}
                | {"below_benchmark": [{k: r[k] for k in ("code", "role", "line", "productivity_pct")} for r in low]},
                links=[{"page": "production", "entity_type": "workforce"}])


def inventory_status(db: Session, factory: str, now: datetime, **_):
    fids = factory_ids(db, factory)
    fg = inventory.fg_summary(db, fids)
    mats = inventory.material_rows(db, fids, now)
    low = [r for r in mats if r["status"] in ("Critical", "Low")]
    s = (f"Finished goods at {_fname(db, fids)}: {fg['total_pillows']:,} pillows stored ({fg['available_pillows']:,} available, "
         f"{fg['reserved_pillows']:,} reserved), {fg['pending_putaway_pallets']} pallets pending put-away, warehouse {fg['utilization_pct']}% full. "
         f"Raw materials below threshold: " + (", ".join(f"{r['code']} {r['factory']} ({r['days_cover']} d)" for r in low) or "none") + ".")
    chart = {"type": "doughnut", "title": "Finished goods (pillows)", "labels": ["Available", "Reserved", "Pending put-away"],
             "datasets": [{"data": [fg["available_pillows"], fg["reserved_pillows"], fg["pending_putaway_pillows"]]}]}
    return _res(s, fg, chart, links=[{"page": "warehouse"}])


TOOLS = {
    "kpi_snapshot": (kpi_snapshot, "Current KPIs: output vs target, efficiency, OEE components, downtime, labour cost per pillow, packaging accuracy, shipment readiness.", {}),
    "material_runout": (material_runout, "Raw materials projected to run out within N days (days of stock cover, next deliveries).", {"days": {"type": "number", "description": "Horizon in days (default 5)"}}),
    "supplier_delays": (supplier_delays, "Supplier on-time delivery performance and currently delayed inbound shipments.", {}),
    "line_downtime": (line_downtime, "Downtime minutes by production line and category for today.", {}),
    "production_gap": (production_gap, "Explains why production is behind target: loss decomposition by cause, bottleneck stage, underperforming lines.", {}),
    "line_allocation": (line_allocation, "Runs the production scheduling optimizer and returns the recommended line allocation and sequence (proposal only).", {}),
    "labour_cost": (labour_cost, "Labour cost per pillow and per hour by factory, including the second-shift differential.", {}),
    "idle_time": (idle_time, "Idle worker-hours by production line today.", {}),
    "carton_failures": (carton_failures, "Cartons that failed inspection today with defect categories and status.", {}),
    "packaging_errors_by_customer": (packaging_errors_by_customer, "Packaging error rate by customer over recent days.", {"days": {"type": "integer", "description": "Look-back days (default 7)"}}),
    "orders_waiting_for_picking": (orders_waiting_for_picking, "Customer orders with allocated stock that is not yet picked.", {}),
    "locate_sku": (locate_sku, "Where a SKU is stored in the warehouse (locations, pallets, quantities).", {"sku": {"type": "string", "description": "SKU code or part of it, e.g. WMT-MS-Q"}}),
    "trucks_at_risk": (trucks_at_risk, "Truck appointments at risk because the order will not be ready when the truck arrives.", {}),
    "detention_cost": (detention_cost, "Estimated detention charges (illustrative) for delayed trucks over a period.", {"days": {"type": "integer", "description": "Look-back days (default 7)"}}),
    "machine_health": (machine_health_summary, "Machines with warning/critical health, anomalies and maintenance predictions.", {}),
    "order_status": (order_status, "Readiness, risk and truck for one customer order.", {"order": {"type": "string", "description": "Order number, e.g. ORD-501"}}),
    "workforce_productivity": (workforce_productivity, "Workforce deployment, utilization, idle time and employees below benchmark.", {}),
    "inventory_status": (inventory_status, "Finished goods stock (available/reserved) and raw materials below threshold.", {}),
}


def run_tool(db: Session, name: str, factory: str, now: datetime, args: dict) -> dict:
    fn = TOOLS[name][0]
    fac = (args.pop("factory", None) or factory or "ALL")
    return fn(db, fac, now, **args)


def anthropic_tool_specs() -> list[dict]:
    specs = []
    for name, (_, desc, props) in TOOLS.items():
        specs.append({"name": name, "description": desc,
                      "input_schema": {"type": "object",
                                       "properties": {"factory": {"type": "string", "enum": ["ALL", "KNV", "PHX", "CMH"],
                                                                  "description": "KNV Kernersville NC, PHX Phoenix AZ, CMH Columbus OH, or ALL"},
                                                      **props},
                                       "required": []}})
    return specs
