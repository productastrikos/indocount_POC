"""KPI calculations. Every number is derived from the operational tables.

Time model (standard OEE treatment)
-----------------------------------
* Elapsed time = productive minutes of the scope window so far (shift calendar: lunch and breaks excluded).
* Idle time with no production order queued ("No orders": a line starved of work) is NOT planned
  production time and NOT a machine loss. It is excluded from planned time, plan to date and target, and
  reported separately (`no_orders_min`, shown as "Idle · no orders").
* Planned time of a line = elapsed - no-orders idle = run time + downtime (every other cause).
* Availability = sum(rate x run) / sum(rate x planned); Performance = actual / sum(rate x run / 60).
  Hence Availability x Performance = actual / plan to date = Efficiency, and OEE = A x P x Q.
* Downtime comes from the downtime events that overlap the window: an event that started earlier or is
  still open counts only its productive minutes inside the window, so it equals the hourly records.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from . import inventory, quality, readiness, workforce
from .common import pct
from .planning import machine_condition, sewing_capacity

PIPELINE = ["Raw Material Preparation", "Mixing", "Flapper Processing", "Bun Formation",
            "Pillow Filling", "Sewing", "Transfer to Packaging"]
# Losses inside planned production time (machine and process causes).
DOWNTIME_CATEGORIES = ["Mechanical breakdown", "Changeover", "Material shortage", "Operator-related delay", "Other"]
NO_ORDERS = "No orders"  # engine.downtime_category() for a line starved of work: not a machine loss
NO_ORDERS_LABEL = "Idle · no orders"
MACHINE_STAGE = {"Fibre Opener": "Mixing", "Flapper Unit": "Flapper Processing", "Garnett Main Motor": "Bun Formation",
                 "Blow Fan Motor": "Bun Formation", "Filling Head": "Pillow Filling"}


def scope_window(day: date, shift: int | None, now: datetime) -> tuple[datetime, datetime]:
    if shift:
        a, b = cal.shift_bounds(day, shift)
    else:
        a, b = datetime.combine(day, cal.DAY_START), datetime.combine(day, cal.DAY_END)
    return a, min(b, now) if day == now.date() else b


def quality_until(b: datetime, now: datetime) -> datetime:
    """Exclusive end for carton queries; a window that ends now also includes the latest tick's cartons."""
    return b + timedelta(seconds=1) if b >= now else b


def is_no_orders(category: str | None, reason: str | None) -> bool:
    """Starvation: category "No orders" (current engine) or legacy "Other" rows with that reason."""
    return category == NO_ORDERS or (reason or "").lower().startswith("no production order")


def _hourly_downtime(db: Session, fids: list[int], a: datetime, b: datetime) -> dict:
    """Downtime minutes of the hourly records whose productive slot lies in [a, b), per line."""
    out = defaultdict(float)
    rows = db.execute(select(m.ProductionHourly.line_id, m.ProductionHourly.date, m.ProductionHourly.hour,
                             m.ProductionHourly.shift, m.ProductionHourly.downtime_min).where(
        m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date >= a.date(), m.ProductionHourly.date <= b.date(),
        m.ProductionHourly.downtime_min > 0))
    for lid, d, h, sh, mins in rows:
        hs = datetime.combine(d, time(h, 0))
        lo, hi = cal.shift_bounds(d, sh)
        if max(hs, lo) < b and min(hs + timedelta(hours=1), hi) > a:
            out[lid] += float(mins or 0)
    return out


def downtime_detail(db: Session, fids: list[int], a: datetime, b: datetime, now: datetime | None = None) -> dict:
    """Downtime of the window [a, b) in productive (line-)minutes.
    The hourly records are authoritative for how much time was lost (run + downtime = elapsed); the downtime
    events that overlap the window give the cause (an event that started earlier or is still open counts only
    its minutes inside the window). Recorded downtime without a matching event is reported as "Other".
    by_category: losses inside planned time (DOWNTIME_CATEGORIES); no_orders_min: starvation, kept apart;
    by_line: {line_id: {"down", "no_orders"}}; no_orders_by_hour: {(line_id, date, hour, shift): minutes}."""
    out = {c: 0.0 for c in DOWNTIME_CATEGORIES}
    by_line = defaultdict(lambda: {"down": 0.0, "no_orders": 0.0})
    by_hour = defaultdict(float)
    if b > a:
        open_end = now or b
        causes = defaultdict(lambda: defaultdict(float))
        q = select(m.DowntimeEvent).where(m.DowntimeEvent.factory_id.in_(fids), m.DowntimeEvent.start < b,
                                          or_(m.DowntimeEvent.end.is_(None), m.DowntimeEvent.end > a))
        for e in db.scalars(q):
            lo, hi = max(e.start, a), min(e.end or open_end, b)
            if hi <= lo:
                continue
            if is_no_orders(e.category, e.reason):
                for d, h, sh, pm, _s, _e in cal.segments(lo, hi):
                    by_hour[(e.line_id, d, h, sh)] += pm
                    by_line[e.line_id]["no_orders"] += pm
            else:
                causes[e.line_id][e.category if e.category in out else "Other"] += cal.productive_minutes(lo, hi)
        recorded = _hourly_downtime(db, fids, a, b)
        for lid in set(recorded) | set(causes):
            total = max(0.0, recorded.get(lid, 0.0) - by_line[lid]["no_orders"])  # machine/process downtime
            cs = causes.get(lid, {})
            explained = sum(cs.values())
            scale = total / explained if explained > total else 1.0  # events never exceed the recorded time
            for cat, mins in cs.items():
                out[cat] += mins * scale
            out["Other"] += max(0.0, total - explained)
            by_line[lid]["down"] = total
    no_orders = sum(x["no_orders"] for x in by_line.values())
    return {"by_category": {k: round(v, 1) for k, v in out.items()}, "total_min": round(sum(out.values()), 1),
            "no_orders_min": round(no_orders, 1), "by_line": by_line, "no_orders_by_hour": by_hour}


def downtime(db: Session, fids: list[int], a: datetime, b: datetime, now: datetime | None = None) -> dict:
    """Downtime by cause inside planned production time (excludes "No orders" idle)."""
    return downtime_detail(db, fids, a, b, now)["by_category"]


def production(db: Session, fids: list[int], day: date, shift: int | None, now: datetime,
               detail: dict | None = None) -> dict:
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))))
    a, b = scope_window(day, shift, now)
    elapsed = cal.productive_minutes(a, b) if b > a else 0.0
    full = cal.productive_minutes(*scope_window(day, shift, datetime.combine(day, time(23, 59))))
    dd = detail if detail is not None else downtime_detail(db, fids, a, b, now)
    q = select(m.ProductionHourly).where(m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date == day)
    if shift:
        q = q.where(m.ProductionHourly.shift == shift)
    rows = list(db.scalars(q))
    by_line = defaultdict(lambda: {"actual": 0.0, "run": 0.0, "down": 0.0, "no_orders": 0.0, "planned_min": 0.0,
                                   "planned": 0.0, "target": 0.0, "efficiency_pct": None})
    for r in rows:
        x = by_line[r.line_id]
        x["actual"] += r.actual
        x["run"] += r.run_min
    remaining = max(0.0, full - elapsed)
    for ln in lines:
        x = by_line[ln.id]
        d = dd["by_line"].get(ln.id) or {}
        x["no_orders"] = min(elapsed, d.get("no_orders", 0.0))
        x["down"] = d.get("down", 0.0)
        x["planned_min"] = max(0.0, elapsed - x["no_orders"])
        x["planned"] = ln.planned_rate * x["planned_min"] / 60
        x["target"] = x["planned"] + ln.planned_rate * remaining / 60
        x["efficiency_pct"] = pct(x["actual"], x["planned"]) if x["planned"] > 0 else None
    actual = sum(by_line[ln.id]["actual"] for ln in lines)
    planned_to_date = sum(by_line[ln.id]["planned"] for ln in lines)
    target = sum(by_line[ln.id]["target"] for ln in lines)
    run = sum(by_line[ln.id]["run"] for ln in lines)
    planned_min = sum(by_line[ln.id]["planned_min"] for ln in lines)
    ideal = sum(ln.planned_rate * by_line[ln.id]["run"] / 60 for ln in lines)  # rated output for the run time
    started = elapsed > 0 and bool(rows)
    return {
        "actual": int(actual), "target": int(round(target)), "planned_to_date": int(round(planned_to_date)),
        "achievement_pct": pct(actual, target) if target else None,
        "efficiency_pct": pct(actual, planned_to_date) if (started and planned_to_date) else None,
        "on_track": actual >= 0.95 * planned_to_date if planned_to_date else True,
        "started": started, "elapsed_min": elapsed,
        "run_min": run, "planned_min": planned_min,
        "no_orders_min": round(sum(by_line[ln.id]["no_orders"] for ln in lines), 1),
        # rate-weighted so that availability x performance = efficiency (see module docstring)
        "availability": ideal / planned_to_date if (started and planned_to_date) else None,
        "performance": actual / ideal if (started and ideal) else None,
        "by_line": by_line, "lines": lines, "downtime": dd,
    }


def oee(prod: dict, q: dict) -> dict:
    Q = q["passed_first"] / q["inspected"] if q["inspected"] else 1.0
    if prod.get("availability") is None:  # scope not started / no planned production time
        return {"oee_pct": None, "availability_pct": None, "performance_pct": None,
                "quality_pct": round(Q * 100, 1) if q["inspected"] else None}
    A = min(1.0, prod["availability"])
    P = min(1.0, prod["performance"] or 0.0)
    return {"oee_pct": round(A * P * Q * 100, 1), "availability_pct": round(A * 100, 1),
            "performance_pct": round(P * 100, 1), "quality_pct": round(Q * 100, 1)}


def current_shift(day: date, now: datetime) -> int | None:
    """The shift a day-level scope is in: the running shift today, shift 2 for a past day, None before 06:00."""
    if day < now.date():
        return 2
    if day > now.date() or now < cal.shift_bounds(day, 1)[0]:
        return None
    return cal.shift_of(now) or 2


def previous_shift(day: date, shift: int) -> tuple[date, int]:
    return (day - timedelta(days=1), 2) if shift == 1 else (day, 1)


def shift_snapshot(db: Session, fids: list[int], day: date, shift: int, now: datetime,
                   prod: dict | None = None, wf: dict | None = None) -> dict:
    """Efficiency, labour cost per pillow and output per worker-hour of one shift, computed exactly like
    the KPIs (live for today; from the hourly records and shift summaries for past days)."""
    p = prod or production(db, fids, day, shift, now)
    if not p["started"]:
        return {"actual": p["actual"], "efficiency_pct": None, "labour_cost_per_pillow": None, "workers": None,
                "output_per_worker_hour": None}
    w = wf or workforce.summary(db, fids, day, now, p["actual"], shift=shift)
    return {"actual": p["actual"], "efficiency_pct": p["efficiency_pct"],
            "labour_cost_per_pillow": w["labour_cost_per_pillow"], "workers": w["workers_deployed"],
            "output_per_worker_hour": round(p["actual"] / w["worker_hours"], 1) if w["worker_hours"] else None}


def _delta(cur, prev, digits=1):
    return round(cur - prev, digits) if cur is not None and prev is not None else None


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


def _packed_fill_kg(db: Session, fids: list[int], a: datetime, b: datetime) -> float | None:
    """Average fill weight per pillow of the cartons packed in [a, b) (the day's SKU mix)."""
    row = db.execute(select(func.sum(m.Carton.required_count * m.Sku.fill_kg), func.sum(m.Carton.required_count))
                     .join(m.Sku, m.Sku.id == m.Carton.sku_id)
                     .where(m.Carton.factory_id.in_(fids), m.Carton.created_at >= a, m.Carton.created_at < b)).one()
    return (float(row[0]) / float(row[1])) if row[1] else None


def pipeline(db: Session, fids: list[int], day: date, now: datetime) -> dict:
    prod = production(db, fids, day, None, now)
    lines = prod["lines"]
    live = day == now.date()
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
    note = None
    if live:
        fill_ids = [mt.id for mt in db.scalars(select(m.Material).where(m.Material.category.in_(("Fibre", "Foam"))))]
        kg = sum(st.consumed_today or 0 for f in stocks.values() for mid, st in f.items() if mid in fill_ids)
        avg_fill = (kg / prod["actual"]) if prod["actual"] else 0.75
        wip = sum(ln.planned_rate * 0.08 for ln in lines if ln.status == "Running")
        in_transfer = int(sum(queue.values()) * 0.15)
    else:
        # Fibre consumption and work in process are tracked for the current day only: for a past date the kg
        # are estimated from that day's output and packed SKU mix, and no WIP is added.
        a, b = scope_window(day, None, now)
        avg_fill = _packed_fill_kg(db, fids, a, b) or 0.75
        kg = prod["actual"] * avg_fill
        wip, in_transfer = 0.0, 0
        note = "Past date: fibre kg estimated from the day's output and packed SKU mix; work in process is tracked live only."
    active_pos = sum(1 for ln in lines if ln.current_po_id and ln.status in ("Running", "Changeover"))
    target = prod["planned_to_date"]
    qty = {
        "Raw Material Preparation": (round(kg * 1.004), round(target * avg_fill), "kg"),
        "Mixing": (round(kg), round(target * avg_fill), "kg"),
        "Flapper Processing": (round(kg * 0.997), round(target * avg_fill), "kg"),
        "Bun Formation": (int(prod["actual"] + wip * 1.6), target, "buns"),
        "Pillow Filling": (int(prod["actual"] + wip), target, "pillows"),
        "Sewing": (prod["actual"], target, "pillows"),
        "Transfer to Packaging": (prod["actual"] - in_transfer, target, "pillows"),
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
    return {"stages": stage_rows, "bottleneck": worst if worst_loss >= 0.08 else None, "note": note,
            "per_line": {lid: {"binding": v["binding"], "loss_pct": round(v["loss"] * 100, 1)} for lid, v in per_line.items()}}


# ─── overview ────────────────────────────────────────────────────────────
def _elapsed_in_hour(day: date, hour: int, shift: int, now: datetime) -> float:
    hs = datetime.combine(day, time(hour, 0))
    lo, hi = cal.shift_bounds(day, shift)
    a, b = max(hs, lo), min(hs + timedelta(hours=1), hi, now)
    return cal.productive_minutes(a, b) if b > a else 0.0


def hourly(db: Session, fids: list[int], day: date, shift: int | None, now: datetime) -> list[dict]:
    """Per clock hour: plan for the hour, plan elapsed so far (sums to the KPI plan to date), actual output and
    downtime. "No orders" idle is excluded from the plan and the downtime, as in production()."""
    lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(fids))))
    rate = {ln.id: ln.planned_rate for ln in lines}
    capacity = sum(rate.values())
    a, b = scope_window(day, shift, now)
    dd = downtime_detail(db, fids, a, b, now)
    starved_rate, starved_min = defaultdict(float), defaultdict(float)
    for (lid, d, h, sh), mins in dd["no_orders_by_hour"].items():
        if d == day and lid in rate and (not shift or sh == shift):
            starved_rate[h] += rate[lid] * mins
            starved_min[h] += mins
    q = select(m.ProductionHourly.hour, func.sum(m.ProductionHourly.actual), func.sum(m.ProductionHourly.downtime_min)).where(
        m.ProductionHourly.factory_id.in_(fids), m.ProductionHourly.date == day)
    if shift:
        q = q.where(m.ProductionHourly.shift == shift)
    actual = {h: (a_, d_) for h, a_, d_ in db.execute(q.group_by(m.ProductionHourly.hour))}
    shifts = (shift,) if shift else (1, 2)
    hours = sorted(set().union(*(cal.hours_for_shift(sh) for sh in shifts)))
    out = []
    for h in hours:
        mins = sum(cal.planned_minutes_in_hour(day, h, sh) for sh in shifts)
        el = sum(_elapsed_in_hour(day, h, sh, now) for sh in shifts)
        hs = datetime.combine(day, time(h, 0))
        started = hs < now
        a_, d_ = actual.get(h, (None, None))
        out.append({"hour": f"{h:02d}:00", "planned": round(max(0.0, capacity * mins - starved_rate[h]) / 60),
                    "planned_to_date": round(max(0.0, capacity * el - starved_rate[h]) / 60, 1),
                    "actual": round(a_) if (a_ is not None and started) else None,
                    "completed": hs + timedelta(hours=1) <= now,
                    "downtime_min": round(max(0.0, (d_ or 0) - starved_min[h])), "idle_no_orders_min": round(starved_min[h])})
    return out


def factory_comparison(db: Session, day: date, now: datetime, shift: int | None = None) -> list[dict]:
    """Per-factory KPIs for the same date and shift scope as the overview (always plant-wide: no customer filter)."""
    out = []
    a, b = scope_window(day, shift, now)
    for f in db.scalars(select(m.Factory).order_by(m.Factory.id)):
        fids = [f.id]
        dd = downtime_detail(db, fids, a, b, now)
        p = production(db, fids, day, shift, now, detail=dd)
        q = quality.quality_counts(db, fids, a, quality_until(b, now))
        wf = workforce.summary(db, fids, day, now, p["actual"], shift=shift)
        rd = readiness.readiness_counts(db, fids, now)
        out.append({"factory_id": f.id, "code": f.code, "name": f"{f.name}, {f.state}",
                    "output": p["actual"], "target": p["target"], "planned_to_date": p["planned_to_date"],
                    "efficiency_pct": p["efficiency_pct"],
                    "downtime_min": round(dd["total_min"]), "idle_no_orders_min": round(dd["no_orders_min"]),
                    "labour_cost_per_pillow": wf["labour_cost_per_pillow"],
                    "packaging_accuracy_pct": q["accuracy_pct"], "shipment_readiness_pct": rd["pct"]})
    return out


def lifecycle_health(db: Session, fids: list[int], day: date, now: datetime, pipe: dict) -> list[dict]:
    mats = inventory.material_rows(db, fids, now)
    crit = [r for r in mats if r["status"] == "Critical"]
    low = [r for r in mats if r["status"] == "Low"]
    from .procurement import is_delayed  # same 6 h tolerance as the Procurement page
    delayed = sum(1 for s in db.scalars(select(m.InboundShipment).where(
        m.InboundShipment.factory_id.in_(fids), m.InboundShipment.status.in_(("Scheduled", "In Transit"))))
        if is_delayed(s))
    out = [{"stage": "Procurement", "page": "procurement",
            "status": "Bottleneck" if crit else ("Constrained" if low or delayed else "Normal"),
            "detail": f"{len(crit)} critical, {len(low)} low materials; {delayed} delayed deliveries"}]
    for s in pipe["stages"]:
        detail = (f"Constrains {', '.join(s['constrained_lines'][:3])}" if s["constrained_lines"]
                  else f"{s['output_pct']}% of plan to date")
        out.append({"stage": s["stage"], "page": "production", "status": s["status"], "detail": detail})
    a, b = scope_window(day, None, now)
    q = quality.quality_counts(db, fids, a, quality_until(b, now))
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
            dt = p["downtime"]["by_category"]
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


def _customer_quality(db: Session, fids: list[int], customer_id: int, a: datetime, until: datetime) -> dict:
    c = {"inspected": 0, "passed_first": 0, "blocked": 0}
    for status, first, n in db.execute(select(m.Carton.status, m.Carton.first_pass, func.count()).where(
            m.Carton.factory_id.in_(fids), m.Carton.customer_id == customer_id, m.Carton.created_at >= a,
            m.Carton.created_at < until).group_by(m.Carton.status, m.Carton.first_pass)):
        c["inspected"] += n
        c["passed_first"] += n if (status == "Passed" and first) else 0
        c["blocked"] += n if status == "Blocked" else 0
    c["failed"] = c["inspected"] - c["passed_first"]
    c["accuracy_pct"] = round(100 * c["passed_first"] / c["inspected"], 2) if c["inspected"] else 100.0
    return c


def _shipment(db: Session, fids: list[int], now: datetime, customer_id: int | None) -> dict:
    """Shipment readiness over the pick window (readiness.readiness_counts), optionally for one customer."""
    if not customer_id:
        return readiness.readiness_counts(db, fids, now)
    horizon = getattr(readiness, "READY_HORIZON_H", 24)
    orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id.in_(fids), m.CustomerOrder.status == "Open", m.CustomerOrder.customer_id == customer_id,
        m.CustomerOrder.scheduled_dispatch <= now + timedelta(hours=max(48, horizon)))))
    info = readiness.assess(db, orders, now)
    due = [o for o in orders if o.scheduled_dispatch <= now + timedelta(hours=horizon)]
    ready = sum(1 for o in due if info[o.id]["readiness"]["status"] == "Ready for Dispatch")
    at_risk = sum(1 for o in due if info[o.id]["risk"]["at_risk"])
    return {"horizon_h": horizon, "scheduled": len(due), "ready": ready, "at_risk": at_risk,
            "on_track": len(due) - ready - at_risk, "pct": pct(ready, len(due)),
            "due_48h": len(orders), "at_risk_48h": sum(1 for o in orders if info[o.id]["risk"]["at_risk"])}


def kpis(db: Session, fids: list[int], day: date, shift: int | None, customer_id: int | None, now: datetime) -> dict:
    a, b = scope_window(day, shift, now)
    dd = downtime_detail(db, fids, a, b, now)
    p = production(db, fids, day, shift, now, detail=dd)
    until = quality_until(b, now)
    q = quality.quality_counts(db, fids, a, until)
    o = oee(p, q)  # plant-wide, like availability and performance (production is not tracked per customer)
    pack = _customer_quality(db, fids, customer_id, a, until) if customer_id else q
    wf = workforce.summary(db, fids, day, now, p["actual"], shift=shift)
    # "vs previous shift" compares like with like: the selected (or current) shift against the shift before it.
    cur_sh = shift or current_shift(day, now)
    if cur_sh:
        cur = shift_snapshot(db, fids, day, cur_sh, now, prod=p if shift else None, wf=wf if shift else None)
        prev_day, prev_sh = previous_shift(day, cur_sh)
        prev = shift_snapshot(db, fids, prev_day, prev_sh, now)
    else:
        cur = prev = {"efficiency_pct": None, "labour_cost_per_pillow": None, "output_per_worker_hour": None}
        prev_day, prev_sh = previous_shift(day, 1)
    fg = inventory.fg_summary(db, fids)
    mats = inventory.material_rows(db, fids, now)
    stopped = [ln for ln in p["lines"] if ln.status == "Idle" and (ln.status_reason or "").startswith("Material")]
    risk_pos = db.scalar(select(func.count()).select_from(m.ProductionOrder).where(
        m.ProductionOrder.factory_id.in_(fids), m.ProductionOrder.material_risk.is_(True),
        m.ProductionOrder.status != "Completed")) or 0
    ship = _shipment(db, fids, now, customer_id)
    det = readiness.detention_summary(db, fids, now, days=1)
    status = "Not started" if not p["started"] else ("On track" if p["on_track"] else "Behind schedule")
    return {
        "production": {"actual": p["actual"], "target": p["target"], "planned_to_date": p["planned_to_date"],
                       "achievement_pct": p["achievement_pct"], "on_track": p["on_track"], "started": p["started"],
                       "status": status, "idle_no_orders_min": round(dd["no_orders_min"])},
        "efficiency": {"pct": p["efficiency_pct"], "current_shift_pct": cur["efficiency_pct"],
                       "current_label": f"{day:%a} shift {cur_sh}" if cur_sh else None,
                       "previous_shift_pct": prev["efficiency_pct"], "previous_label": f"{prev_day:%a} shift {prev_sh}",
                       "delta": _delta(cur["efficiency_pct"], prev["efficiency_pct"]), "shift_scope": bool(shift)},
        "oee": o,
        "downtime": {"total_min": round(dd["total_min"]), "by_category": dd["by_category"],
                     "idle_no_orders_min": round(dd["no_orders_min"])},
        "workforce": {"required": wf["workers_required"], "deployed": wf["workers_deployed"],
                      "utilization_pct": wf["labour_utilization_pct"], "idle_hours": wf["idle_hours"],
                      "idle_fte": wf["idle_fte"], "absent": wf["absent"], "worker_hours": wf["worker_hours"],
                      "output_per_worker_hour": round(p["actual"] / wf["worker_hours"], 1) if wf["worker_hours"] else None,
                      "current_output_per_worker_hour": cur["output_per_worker_hour"],
                      "previous_output_per_worker": prev["output_per_worker_hour"],
                      "below_benchmark": len(wf["below_benchmark"])},
        "labour_cost": {"per_pillow": wf["labour_cost_per_pillow"], "total": wf["labour_cost"],
                        "per_hour": wf["labour_cost_per_hour"], "current_per_pillow": cur["labour_cost_per_pillow"],
                        "previous_per_pillow": prev["labour_cost_per_pillow"],
                        "shift2_differential": wf["shift_rules"]["shift2_differential"]},
        "packaging": {"accuracy_pct": pack["accuracy_pct"], "inspected": pack["inspected"], "failed": pack["failed"],
                      "blocked": pack["blocked"], "customer_scoped": bool(customer_id)},
        "inventory": {"fg_available": fg["available_pillows"], "fg_reserved": fg["reserved_pillows"],
                      "fg_total": fg["total_pillows"],
                      "materials_critical": sum(1 for r in mats if r["status"] == "Critical"),
                      "materials_low": sum(1 for r in mats if r["status"] == "Low"),
                      "lines_stopped_for_material": len(stopped), "orders_with_material_risk": risk_pos,
                      "lowest": mats[0] if mats else None},
        "shipment": {**ship, "customer_scoped": bool(customer_id)},
        "detention": {"incurred": det["incurred_estimate"], "projected": det["projected_estimate"],
                      "delayed_trucks": det["delayed_trucks"], "charge_per_truck": det["charge_per_truck"]},
        "customer_scoped": ["packaging", "shipment"] if customer_id else [],
    }
