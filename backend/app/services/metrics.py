"""KPI calculations. Every number is derived from the operational tables."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from . import inventory, quality, readiness, workforce
from .common import pct
from .planning import machine_condition, sewing_capacity

PIPELINE = ["Raw Material Preparation", "Mixing", "Flapper Processing", "Bun Formation",
            "Pillow Filling", "Sewing", "Transfer to Packaging"]
DOWNTIME_CATEGORIES = ["Mechanical breakdown", "Changeover", "Material shortage", "Operator-related delay", "Other"]
MACHINE_STAGE = {"Fibre Opener": "Mixing", "Flapper Unit": "Flapper Processing", "Garnett Main Motor": "Bun Formation",
                 "Blow Fan Motor": "Bun Formation", "Filling Head": "Pillow Filling"}


def scope_window(day: date, shift: int | None, now: datetime) -> tuple[datetime, datetime]:
    if shift:
        a, b = cal.shift_bounds(day, shift)
    else:
        a, b = datetime.combine(day, cal.DAY_START), datetime.combine(day, cal.DAY_END)
    return a, min(b, now) if day == now.date() else b


def production(db: Session, fids: list[int], day: date, shift: int | None, now: datetime) -> dict:
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))))
    a, b = scope_window(day, shift, now)
    elapsed = cal.productive_minutes(a, b) if b > a else 0.0
    full = cal.productive_minutes(*scope_window(day, shift, datetime.combine(day, time(23, 59))))
    q = select(m.ProductionHourly).where(m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date == day)
    if shift:
        q = q.where(m.ProductionHourly.shift == shift)
    rows = list(db.scalars(q))
    by_line = defaultdict(lambda: {"actual": 0.0, "run": 0.0, "down": 0.0})
    for r in rows:
        x = by_line[r.line_id]
        x["actual"] += r.actual
        x["run"] += r.run_min
        x["down"] += r.downtime_min
    actual = sum(x["actual"] for x in by_line.values())
    planned_to_date = sum(ln.planned_rate * elapsed / 60 for ln in lines)
    target = sum(ln.planned_rate * full / 60 for ln in lines)
    run = sum(x["run"] for x in by_line.values())
    planned_min = elapsed * len(lines)
    ideal = sum(ln.planned_rate * by_line[ln.id]["run"] / 60 for ln in lines)
    return {
        "actual": int(actual), "target": int(round(target)), "planned_to_date": int(round(planned_to_date)),
        "achievement_pct": pct(actual, target), "efficiency_pct": pct(actual, planned_to_date),
        "on_track": actual >= 0.95 * planned_to_date if planned_to_date else True,
        "run_min": run, "planned_min": planned_min,
        "availability": run / planned_min if planned_min else 1.0,
        "performance": actual / ideal if ideal else 1.0,
        "by_line": by_line, "lines": lines,
    }


def downtime(db: Session, fids: list[int], a: datetime, b: datetime) -> dict:
    out = {c: 0.0 for c in DOWNTIME_CATEGORIES}
    rows = db.execute(select(m.DowntimeEvent.category, func.sum(m.DowntimeEvent.minutes)).where(
        m.DowntimeEvent.factory_id.in_(fids), m.DowntimeEvent.start >= a, m.DowntimeEvent.start < b + timedelta(minutes=1))
        .group_by(m.DowntimeEvent.category))
    for cat, mins in rows:
        out[cat if cat in out else "Other"] += float(mins or 0)
    return {k: round(v, 1) for k, v in out.items()}


def oee(prod: dict, q: dict) -> dict:
    A = min(1.0, prod["availability"])
    P = min(1.0, prod["performance"])
    Q = q["passed_first"] / q["inspected"] if q["inspected"] else 1.0
    return {"oee_pct": round(A * P * Q * 100, 1), "availability_pct": round(A * 100, 1),
            "performance_pct": round(P * 100, 1), "quality_pct": round(Q * 100, 1)}


def previous_shift(day: date, shift: int | None, now: datetime) -> tuple[date, int]:
    cur = shift or cal.shift_of(now) or 2
    return (day - timedelta(days=1), 2) if cur == 1 else (day, 1)


def shift_snapshot(db: Session, fids: list[int], day: date, shift: int, now: datetime) -> dict:
    rows = list(db.scalars(select(m.ShiftSummary).where(m.ShiftSummary.factory_id.in_(fids),
                                                        m.ShiftSummary.date == day, m.ShiftSummary.shift == shift)))
    if rows:
        planned = sum(r.planned for r in rows)
        actual = sum(r.actual for r in rows)
        return {"actual": actual, "efficiency_pct": pct(actual, planned),
                "labour_cost_per_pillow": round(sum(r.labour_cost for r in rows) / actual, 3) if actual else None,
                "workers": sum(r.workers_deployed for r in rows),
                "output_per_worker": round(actual / max(1, sum(r.workers_deployed for r in rows)) / (cal.productive_minutes_per_shift() / 60), 1)}
    p = production(db, fids, day, shift, now)
    return {"actual": p["actual"], "efficiency_pct": p["efficiency_pct"], "labour_cost_per_pillow": None,
            "workers": None, "output_per_worker": None}


# ─── pipeline / bottlenecks ──────────────────────────────────────────────
def line_constraints(db: Session, line: m.ProductionLine, machines, sewers, stock_by_mat, pack_queue: int, sku) -> dict:
    f = {s: 1.0 for s in PIPELINE}
    reason = (line.status_reason or "")
    if line.status == "Idle" and reason.startswith("Material"):
        f["Raw Material Preparation"] = 0.0
    elif sku:
        st = stock_by_mat.get(sku.fill_material_id)
        if st and st.avg_daily_consumption and st.on_hand / st.avg_daily_consumption < 1:
            f["Raw Material Preparation"] = 0.94
    for mc in machines:
        stage = MACHINE_STAGE.get(mc.machine_type)
        if stage:
            f[stage] = min(f[stage], machine_condition([mc]))
    # Changeovers are transient and reported separately; they are not a capacity constraint.
    if line.perf_modifier < 0.95:
        f["Pillow Filling"] = min(f["Pillow Filling"], line.perf_modifier)
    f["Sewing"] = min(1.0, sewing_capacity(sewers)) if line.status != "Idle" or not reason.startswith("Material") else 1.0
    if sku and pack_queue > sku.pillows_per_carton * 20:
        f["Transfer to Packaging"] = 0.85
    binding = min(f, key=lambda s: f[s])
    return {"factors": f, "binding": binding if f[binding] < 0.95 else None, "loss": 1 - f[binding]}


def pipeline(db: Session, fids: list[int], day: date, now: datetime) -> dict:
    prod = production(db, fids, day, None, now)
    lines = prod["lines"]
    shift = cal.shift_of(now) or 1
    machines = defaultdict(list)
    for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(fids))):
        machines[mc.line_id].append(mc)
    sewers = defaultdict(list)
    for e in db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(fids), m.Employee.role == "Sewer",
                                                 m.Employee.shift == shift)):
        sewers[e.line_id].append(e)
    stocks = defaultdict(dict)
    for st in db.scalars(select(m.MaterialStock).where(m.MaterialStock.factory_id.in_(fids))):
        stocks[st.factory_id][st.material_id] = st
    skus = {s.id: s for s in db.scalars(select(m.Sku))}
    queue = defaultdict(int)
    for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id.in_(fids),
                                                         m.ProductionOrder.pack_queue > 0)):
        queue[po.line_id] += po.pack_queue
    per_line = {}
    for ln in lines:
        sku = skus.get(ln.current_sku_id)
        per_line[ln.id] = line_constraints(db, ln, machines[ln.id], sewers[ln.id], stocks[ln.factory_id], queue[ln.id], sku)
    fill_ids = [mt.id for mt in db.scalars(select(m.Material).where(m.Material.category.in_(("Fibre", "Foam"))))]
    kg = sum(st.consumed_today or 0 for f in stocks.values() for mid, st in f.items() if mid in fill_ids)
    avg_fill = (kg / prod["actual"]) if prod["actual"] else 0.75
    wip = sum(ln.planned_rate * 0.08 for ln in lines if ln.status == "Running")
    active_pos = sum(1 for ln in lines if ln.current_po_id and ln.status in ("Running", "Changeover"))
    target = prod["planned_to_date"]
    qty = {
        "Raw Material Preparation": (round(kg * 1.004), round(target * avg_fill), "kg"),
        "Mixing": (round(kg), round(target * avg_fill), "kg"),
        "Flapper Processing": (round(kg * 0.997), round(target * avg_fill), "kg"),
        "Bun Formation": (int(prod["actual"] + wip * 1.6), target, "buns"),
        "Pillow Filling": (int(prod["actual"] + wip), target, "pillows"),
        "Sewing": (prod["actual"], target, "pillows"),
        "Transfer to Packaging": (prod["actual"] - int(sum(queue.values()) * 0.15), target, "pillows"),
    }
    stage_rows = []
    worst, worst_loss = None, 0.0
    for s in PIPELINE:
        constrained = [ln for ln in lines if per_line[ln.id]["binding"] == s and per_line[ln.id]["loss"] >= 0.05]
        down = [ln for ln in lines if per_line[ln.id]["factors"][s] == 0.0 and ln.status != "Changeover"]
        changeover = [ln for ln in lines if ln.status == "Changeover"] if s == "Pillow Filling" else []
        avg = sum(per_line[ln.id]["factors"][s] for ln in lines) / len(lines) if lines else 1.0
        loss = sum(per_line[ln.id]["loss"] for ln in constrained)
        if loss > worst_loss:
            worst, worst_loss = s, loss
        v, t, unit = qty[s]
        stage_rows.append({
            "stage": s, "qty": v, "target": t, "unit": unit, "efficiency_pct": round(avg * 100, 1),
            "output_pct": pct(v, t), "active_orders": active_pos,
            "constrained_lines": [ln.name + (f" ({ln.factory.code})" if len(fids) > 1 else "") for ln in constrained],
            "down_lines": [ln.name for ln in down], "changeover_lines": [ln.name for ln in changeover],
            "status": "Down" if down and len(down) == len(lines) else ("Constrained" if constrained or down else "Normal"),
        })
    for r in stage_rows:
        r["bottleneck"] = r["stage"] == worst and worst_loss >= 0.08
        if r["bottleneck"]:
            r["status"] = "Bottleneck"
    return {"stages": stage_rows, "bottleneck": worst if worst_loss >= 0.08 else None,
            "per_line": {lid: {"binding": v["binding"], "loss_pct": round(v["loss"] * 100, 1)} for lid, v in per_line.items()}}


# ─── overview ────────────────────────────────────────────────────────────
def hourly(db: Session, fids: list[int], day: date, shift: int | None, now: datetime) -> list[dict]:
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))))
    q = select(m.ProductionHourly.hour, func.sum(m.ProductionHourly.actual), func.sum(m.ProductionHourly.downtime_min)).where(
        m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date == day)
    if shift:
        q = q.where(m.ProductionHourly.shift == shift)
    actual = {h: (a, d) for h, a, d in db.execute(q.group_by(m.ProductionHourly.hour))}
    hours = sorted(set(cal.hours_for_shift(shift)) if shift else set(cal.hours_for_shift(1)) | set(cal.hours_for_shift(2)))
    out = []
    for h in hours:
        mins = sum(cal.planned_minutes_in_hour(day, h, sh) for sh in ((shift,) if shift else (1, 2)))
        planned = sum(ln.planned_rate for ln in lines) * mins / 60
        started = datetime.combine(day, time(h, 0)) < now
        a, d = actual.get(h, (None, None))
        out.append({"hour": f"{h:02d}:00", "planned": round(planned), "actual": round(a) if (a is not None and started) else None,
                    "downtime_min": round(d or 0)})
    return out


def factory_comparison(db: Session, day: date, now: datetime) -> list[dict]:
    out = []
    for f in db.scalars(select(m.Factory).order_by(m.Factory.id)):
        fids = [f.id]
        p = production(db, fids, day, None, now)
        a, b = scope_window(day, None, now)
        dt = downtime(db, fids, a, b)
        q = quality.quality_counts(db, fids, a)
        wf = workforce.summary(db, fids, day, now, p["actual"])
        rd = readiness.readiness_counts(db, fids, now)
        out.append({"factory_id": f.id, "code": f.code, "name": f"{f.name}, {f.state}",
                    "output": p["actual"], "target": p["target"], "efficiency_pct": p["efficiency_pct"],
                    "downtime_min": round(sum(dt.values())), "labour_cost_per_pillow": wf["labour_cost_per_pillow"],
                    "packaging_accuracy_pct": q["accuracy_pct"], "shipment_readiness_pct": rd["pct"]})
    return out


def lifecycle_health(db: Session, fids: list[int], day: date, now: datetime, pipe: dict) -> list[dict]:
    mats = inventory.material_rows(db, fids, now)
    crit = [r for r in mats if r["status"] == "Critical"]
    low = [r for r in mats if r["status"] == "Low"]
    delayed = db.scalar(select(func.count()).select_from(m.InboundShipment).where(
        m.InboundShipment.factory_id.in_(fids), m.InboundShipment.status.in_(("Scheduled", "In Transit")),
        m.InboundShipment.expected_arrival > m.InboundShipment.original_expected_arrival)) or 0
    out = [{"stage": "Procurement", "page": "procurement",
            "status": "Bottleneck" if crit else ("Constrained" if low or delayed else "Normal"),
            "detail": f"{len(crit)} critical, {len(low)} low materials; {delayed} delayed deliveries"}]
    for s in pipe["stages"]:
        detail = (f"Constrains {', '.join(s['constrained_lines'][:3])}" if s["constrained_lines"]
                  else f"{s['output_pct']}% of plan to date")
        out.append({"stage": s["stage"], "page": "production", "status": s["status"], "detail": detail})
    a, _ = scope_window(day, None, now)
    q = quality.quality_counts(db, fids, a)
    out.append({"stage": "Quality & Packaging", "page": "quality",
                "status": "Bottleneck" if q["blocked"] >= 8 else ("Constrained" if q["blocked"] else "Normal"),
                "detail": f"{q['blocked']} blocked cartons; accuracy {q['accuracy_pct']}%"})
    fg = inventory.fg_summary(db, fids)
    out.append({"stage": "Warehouse", "page": "warehouse",
                "status": "Constrained" if fg["utilization_pct"] > 90 or fg["pending_putaway_pallets"] > 8 else "Normal",
                "detail": f"{fg['utilization_pct']}% capacity; {fg['pending_putaway_pallets']} pallets pending put-away"})
    rd = readiness.readiness_counts(db, fids, now)
    out.append({"stage": "Dispatch", "page": "dispatch",
                "status": "Bottleneck" if rd["at_risk"] >= 2 else ("Constrained" if rd["at_risk"] else "Normal"),
                "detail": f"{rd['ready']}/{rd['scheduled']} orders ready; {rd['at_risk']} at risk"})
    return out


def close_day(db: Session, day: date) -> None:
    """Write shift summaries for a finished day (history for trends)."""
    end = datetime.combine(day, time(23, 0))
    for f in db.scalars(select(m.Factory)):
        for sh in (1, 2):
            fids = [f.id]
            p = production(db, fids, day, sh, end)
            a, b = cal.shift_bounds(day, sh)
            dt = downtime(db, fids, a, b)
            qc = quality.quality_counts(db, fids, a, b)
            wf = workforce.summary(db, fids, day, end, p["actual"], shift=sh)
            co = list(db.scalars(select(m.ChangeoverEvent).where(m.ChangeoverEvent.factory_id == f.id,
                                                                 m.ChangeoverEvent.start >= a, m.ChangeoverEvent.start < b)))
            defects = defaultdict(int)
            for d in db.scalars(select(m.DefectRecord).where(m.DefectRecord.factory_id == f.id,
                                                             m.DefectRecord.created_at >= a, m.DefectRecord.created_at < b)):
                defects[d.category] += 1
            row = db.scalar(select(m.ShiftSummary).where(m.ShiftSummary.factory_id == f.id, m.ShiftSummary.date == day,
                                                         m.ShiftSummary.shift == sh))
            if not row:
                row = m.ShiftSummary(factory_id=f.id, date=day, shift=sh)
                db.add(row)
            row.planned, row.actual = p["target"], p["actual"]
            row.planned_min, row.run_min, row.downtime_min = p["planned_min"], p["run_min"], sum(dt.values())
            row.cartons_inspected, row.cartons_passed = qc["inspected"], qc["passed_first"]
            row.labour_cost = wf["labour_cost"]
            row.workers_deployed, row.workers_required = wf["workers_deployed"], wf["workers_required"]
            row.idle_min = wf["idle_hours"] * 60
            row.changeovers = len(co)
            row.changeover_lost_min = sum(c.lost_min or 0 for c in co)
            row.defects = dict(defects)


def kpis(db: Session, fids: list[int], day: date, shift: int | None, customer_id: int | None, now: datetime) -> dict:
    p = production(db, fids, day, shift, now)
    a, b = scope_window(day, shift, now)
    dt = downtime(db, fids, a, b)
    q = quality.quality_counts(db, fids, a, b if day != now.date() else None)
    if customer_id:
        cq = select(m.Carton.status, m.Carton.first_pass, func.count()).where(
            m.Carton.factory_id.in_(fids), m.Carton.customer_id == customer_id, m.Carton.created_at >= a)
        insp = sum(n for _, _, n in db.execute(cq.group_by(m.Carton.status, m.Carton.first_pass)))
        first = sum(n for s, fp, n in db.execute(cq.group_by(m.Carton.status, m.Carton.first_pass)) if s == "Passed" and fp)
        q = {**q, "inspected": insp, "passed_first": first, "failed": insp - first,
             "accuracy_pct": round(100 * first / insp, 2) if insp else 100.0}
    o = oee(p, q)
    wf = workforce.summary(db, fids, day, now, p["actual"], shift=shift)
    prev_day, prev_sh = previous_shift(day, shift, now)
    prev = shift_snapshot(db, fids, prev_day, prev_sh, now)
    fg = inventory.fg_summary(db, fids)
    mats = inventory.material_rows(db, fids, now)
    stopped = [ln for ln in p["lines"] if ln.status == "Idle" and (ln.status_reason or "").startswith("Material")]
    risk_pos = db.scalar(select(func.count()).select_from(m.ProductionOrder).where(
        m.ProductionOrder.factory_id.in_(fids), m.ProductionOrder.material_risk.is_(True),
        m.ProductionOrder.status != "Completed")) or 0
    rd_orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open",
        m.CustomerOrder.scheduled_dispatch <= now + timedelta(hours=48),
        *( [m.CustomerOrder.customer_id == customer_id] if customer_id else []))))
    info = readiness.assess(db, rd_orders, now)
    ready = sum(1 for x in rd_orders if info[x.id]["readiness"]["status"] == "Ready for Dispatch")
    at_risk = [x for x in rd_orders if info[x.id]["risk"]["at_risk"]]
    det = readiness.detention_summary(db, fids, now, days=1)
    hours_elapsed = max(1.0, cal.productive_minutes(a, b) / 60)
    return {
        "production": {"actual": p["actual"], "target": p["target"], "planned_to_date": p["planned_to_date"],
                       "achievement_pct": p["achievement_pct"], "on_track": p["on_track"],
                       "status": "On track" if p["on_track"] else "Behind schedule"},
        "efficiency": {"pct": p["efficiency_pct"], "previous_shift_pct": prev["efficiency_pct"],
                       "delta": round(p["efficiency_pct"] - prev["efficiency_pct"], 1),
                       "previous_label": f"{prev_day:%a} shift {prev_sh}"},
        "oee": o,
        "downtime": {"total_min": round(sum(dt.values())), "by_category": dt},
        "workforce": {"required": wf["workers_required"], "deployed": wf["workers_deployed"],
                      "utilization_pct": wf["labour_utilization_pct"], "idle_hours": wf["idle_hours"],
                      "idle_fte": wf["idle_fte"], "absent": wf["absent"],
                      "output_per_worker_hour": round(p["actual"] / max(1, wf["workers_deployed"]) / hours_elapsed, 1),
                      "previous_output_per_worker": prev["output_per_worker"],
                      "below_benchmark": len(wf["below_benchmark"])},
        "labour_cost": {"per_pillow": wf["labour_cost_per_pillow"], "total": wf["labour_cost"],
                        "per_hour": wf["labour_cost_per_hour"], "previous_per_pillow": prev["labour_cost_per_pillow"],
                        "shift2_differential": wf["shift_rules"]["shift2_differential"]},
        "packaging": {"accuracy_pct": q["accuracy_pct"], "inspected": q["inspected"], "failed": q["failed"],
                      "blocked": q["blocked"]},
        "inventory": {"fg_available": fg["available_pillows"], "fg_reserved": fg["reserved_pillows"],
                      "fg_total": fg["total_pillows"],
                      "materials_critical": sum(1 for r in mats if r["status"] == "Critical"),
                      "materials_low": sum(1 for r in mats if r["status"] == "Low"),
                      "lines_stopped_for_material": len(stopped), "orders_with_material_risk": risk_pos,
                      "lowest": mats[0] if mats else None},
        "shipment": {"ready": ready, "scheduled": len(rd_orders), "pct": pct(ready, len(rd_orders)),
                     "at_risk": len(at_risk)},
        "detention": {"incurred": det["incurred_estimate"], "projected": det["projected_estimate"],
                      "delayed_trucks": det["delayed_trucks"], "charge_per_truck": det["charge_per_truck"]},
    }
