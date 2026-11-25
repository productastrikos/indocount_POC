"""Production planning.

* Effective line rate  = planned rate x base efficiency x scenario modifier
                         x min(machine condition, sewing crew capacity)
* Schedule projection  walks each line's order queue on the shift calendar,
                         adding changeovers, machine downtime windows and
                         raw-material stock-outs (until the next inbound
                         shipment is received). The result is the estimated
                         completion that Dispatch uses for shipment risk.
* Optimizer            constructs a schedule by earliest-due-date with
                         changeover-aware line assignment, then improves it by
                         relocation / swap local search on weighted tardiness,
                         changeovers and completion time.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from .common import PRIORITY_WEIGHT, business_rules, iso

SEWERS_REQUIRED = 2
OPEN_PO = ("Planned", "Ready", "In Progress", "Delayed")
POST_PRODUCTION_LAG_MIN = 75  # packing + inspection + put-away buffer before picking


def machine_condition(machines) -> float:
    f = 1.0
    for mc in machines:
        if mc.status in ("Breakdown", "Maintenance"):
            return 0.0
        if mc.health_score < 65:
            f = min(f, 0.9)
        elif mc.health_score < 85:
            f = min(f, 0.97)
    return f


def sewing_capacity(sewers) -> float:
    present = [e for e in sewers if e.attendance in ("Present", "Late")]
    if not present:
        return 0.0
    return min(1.05, sum(e.skill * e.productivity_modifier for e in present) / SEWERS_REQUIRED)


class PlanCtx:
    """Snapshot of everything the planner needs for a set of factories."""

    def __init__(self, db: Session, factory_ids: list[int], now: datetime):
        self.db, self.now, self.fids = db, now, factory_ids
        self.rules = business_rules(db)
        self.lines = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(factory_ids))
                                     .order_by(m.ProductionLine.id)))
        self.line_by_id = {ln.id: ln for ln in self.lines}
        self.machines = defaultdict(list)
        for mc in db.scalars(select(m.Machine).where(m.Machine.factory_id.in_(factory_ids))):
            self.machines[mc.line_id].append(mc)
        self.sewers = defaultdict(list)
        for e in db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(factory_ids), m.Employee.role == "Sewer")):
            self.sewers[(e.line_id, e.shift)].append(e)
        self.skus = {s.id: s for s in db.scalars(select(m.Sku))}
        self.materials = {x.id: x for x in db.scalars(select(m.Material))}
        self.stock = {(s.factory_id, s.material_id): s for s in db.scalars(
            select(m.MaterialStock).where(m.MaterialStock.factory_id.in_(factory_ids)))}
        self.pos = list(db.scalars(select(m.ProductionOrder).where(
            m.ProductionOrder.factory_id.in_(factory_ids), m.ProductionOrder.status.in_(OPEN_PO))))
        self.orders = {o.id: o for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(factory_ids)))}
        self.customers = {c.id: c for c in db.scalars(select(m.Customer))}
        self.trucks = {}
        for t in db.scalars(select(m.TruckAppointment).where(
                m.TruckAppointment.factory_id.in_(factory_ids),
                m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading")))):
            if t.order_id and (t.order_id not in self.trucks or t.scheduled_arrival < self.trucks[t.order_id].scheduled_arrival):
                self.trucks[t.order_id] = t
        self._rate_cache = {}
        self._material_projection()

    # ── rates & availability ──
    def line_rate(self, line) -> float:
        """Pillows per productive hour under current conditions (0 if stopped)."""
        if line.id in self._rate_cache:
            return self._rate_cache[line.id]
        mach = [mc for mc in self.machines[line.id] if mc.status not in ("Breakdown", "Maintenance")]
        mcond = machine_condition(mach) if mach else 0.0
        sew = (sewing_capacity(self.sewers[(line.id, 1)]) + sewing_capacity(self.sewers[(line.id, 2)])) / 2
        r = line.planned_rate * line.base_efficiency * line.perf_modifier * min(mcond, sew)
        self._rate_cache[line.id] = r
        return r

    def available_from(self, line) -> datetime:
        t = self.now
        for mc in self.machines[line.id]:
            if mc.status in ("Breakdown", "Maintenance") and mc.unavailable_until:
                t = max(t, mc.unavailable_until)
        if line.status == "Changeover" and line.changeover_until:
            t = max(t, line.changeover_until)
        return t

    def changeover_min(self, line) -> float:
        return self.rules["changeover_standard_min"] * (line.changeover_factor or 1.0)

    # ── materials ──
    def usage(self, sku):
        return {sku.fill_material_id: sku.fill_kg, sku.shell_material_id: 1.0}

    def _material_projection(self):
        cons = defaultdict(float)  # per productive minute
        for ln in self.lines:
            if ln.status in ("Running", "Changeover") and ln.current_sku_id:
                sku = self.skus[ln.current_sku_id]
                rate = self.line_rate(ln) / 60
                for mid, u in self.usage(sku).items():
                    cons[(ln.factory_id, mid)] += rate * u
        self.consumption_per_min = cons
        self.runout, self.replenish = {}, {}
        ships = self.db.scalars(select(m.InboundShipment).where(
            m.InboundShipment.factory_id.in_(self.fids),
            m.InboundShipment.status.in_(("Scheduled", "In Transit", "Arrived", "Receiving"))))
        eta = {}
        for s in ships:
            when = self.now + timedelta(minutes=60) if s.status in ("Arrived", "Receiving") else max(self.now, s.expected_arrival) + timedelta(minutes=90)
            key = (s.factory_id, s.material_id)
            eta[key] = min(eta.get(key, when), when)
        for key, st in self.stock.items():
            c = cons.get(key, 0.0)
            if c > 0:
                self.runout[key] = cal.add_productive(self.now, max(0.0, st.on_hand) / c)
            self.replenish[key] = eta.get(key) or (self.now + timedelta(days=5))

    # ── order context ──
    def due(self, po) -> datetime:
        due = po.required_completion or (self.now + timedelta(days=7))
        t = self.trucks.get(po.customer_order_id)
        if t:
            due = min(due, t.scheduled_arrival - timedelta(minutes=POST_PRODUCTION_LAG_MIN))
        return due

    def weight(self, po) -> float:
        w = PRIORITY_WEIGHT.get(po.priority, 2)
        o = self.orders.get(po.customer_order_id)
        if o:
            tier = self.customers[o.customer_id].priority_tier
            w *= {1: 1.5, 2: 1.0, 3: 0.8}.get(tier, 1.0)
        return w

    def current_queues(self) -> dict[int, list]:
        q = defaultdict(list)
        for po in self.pos:
            if po.line_id in self.line_by_id:
                q[po.line_id].append(po)
        for lid, lst in q.items():
            cur = self.line_by_id[lid].current_po_id
            lst.sort(key=lambda p: (0 if p.id == cur else 1, p.sequence or 0, p.id))
        for ln in self.lines:
            q.setdefault(ln.id, [])
        return dict(q)


def evaluate_line(ctx: PlanCtx, line, queue) -> tuple[dict, int]:
    res, changeovers = {}, 0
    t = ctx.available_from(line)
    prev_sku = line.current_sku_id
    rate = max(ctx.line_rate(line), 1.0)
    co = ctx.changeover_min(line)
    for po in queue:
        rem = max(0, po.required_qty - po.completed_qty)
        in_progress_here = po.id == line.current_po_id
        if prev_sku is not None and po.sku_id != prev_sku and not in_progress_here:
            t = cal.add_productive(t, co)
            changeovers += 1
        start = t
        end = cal.add_productive(start, rem / rate * 60)
        delay_reason = None
        sku = ctx.skus[po.sku_id]
        for mid in ctx.usage(sku):
            key = (po.factory_id, mid)
            r_out, rep = ctx.runout.get(key), ctx.replenish.get(key)
            if r_out and r_out < end and rep and rep > r_out:
                made = rate / 60 * cal.productive_minutes(start, r_out) if r_out > start else 0.0
                restart = max(rep, start)
                end = cal.add_productive(restart, max(0.0, rem - made) / rate * 60)
                mat = ctx.materials[mid]
                delay_reason = f"{mat.code} projected stock-out {r_out:%a %H:%M}; replenishment {rep:%a %H:%M}"
        res[po.id] = {"start": start, "end": end, "line_id": line.id, "delay_reason": delay_reason, "rate": rate}
        t = end
        prev_sku = po.sku_id
    return res, changeovers


def evaluate(ctx: PlanCtx, queues: dict[int, list], horizon_h: float = 24) -> dict:
    results, changeovers = {}, 0
    for lid, q in queues.items():
        r, c = evaluate_line(ctx, ctx.line_by_id[lid], q)
        results.update(r)
        changeovers += c
    horizon = ctx.now + timedelta(hours=horizon_h)
    late, tard, wtard, output = 0, 0.0, 0.0, 0.0
    by_po = {p.id: p for qs in queues.values() for p in qs}
    for pid, r in results.items():
        po = by_po[pid]
        hrs = max(0.0, (r["end"] - ctx.due(po)).total_seconds() / 3600)
        if hrs > 0.05:
            late += 1
        tard += hrs
        wtard += hrs * ctx.weight(po)
        rem = max(0, po.required_qty - po.completed_qty)
        output += min(rem, r["rate"] / 60 * cal.productive_minutes(r["start"], min(r["end"], horizon)))
    ends = [r["end"] for r in results.values()]
    return {
        "results": results, "changeovers": changeovers, "late_orders": late,
        "total_delay_h": round(tard, 1), "weighted_delay": wtard,
        "output_24h": int(output), "makespan": max(ends) if ends else ctx.now,
        "avg_completion_h": round(sum((e - ctx.now).total_seconds() for e in ends) / 3600 / len(ends), 1) if ends else 0,
    }


def objective(ev: dict, ctx: PlanCtx) -> float:
    # Lateness dominates; then average completion time (hours); a changeover costs
    # minutes of line time, so it only breaks ties between otherwise similar plans.
    return ev["weighted_delay"] * 10 + ev["avg_completion_h"] * 1.0 + ev["changeovers"] * 0.25


# ── projection of the live schedule ─────────────────────────────────────
def recompute(db: Session, now: datetime, factory_ids: list[int] | None = None) -> None:
    if factory_ids is None:
        factory_ids = [f.id for f in db.scalars(select(m.Factory))]
    ctx = PlanCtx(db, factory_ids, now)
    ev = evaluate(ctx, ctx.current_queues())
    for po in ctx.pos:
        r = ev["results"].get(po.id)
        if not r:
            po.estimated_completion = None
            continue
        po.estimated_completion = r["end"]
        po.material_risk = bool(r["delay_reason"])
        po.risk_reason = r["delay_reason"]
        late = r["end"] > (po.required_completion or r["end"])
        line = ctx.line_by_id.get(po.line_id)
        if line and line.current_po_id == po.id and po.completed_qty > 0:
            po.status = "Delayed" if late else "In Progress"
        elif late:
            po.status = "Delayed"
        else:
            st = ctx.stock.get((po.factory_id, ctx.skus[po.sku_id].fill_material_id))
            need = (po.required_qty - po.completed_qty) * ctx.skus[po.sku_id].fill_kg
            po.status = "Ready" if st and st.on_hand >= need and not po.material_risk else "Planned"


def schedule_snapshot(ctx: PlanCtx, queues: dict[int, list], ev: dict) -> list[dict]:
    out = []
    for ln in ctx.lines:
        items = []
        for po in queues.get(ln.id, []):
            r = ev["results"][po.id]
            o = ctx.orders.get(po.customer_order_id)
            due = ctx.due(po)
            items.append({
                "po_id": po.id, "po": po.number, "sku": ctx.skus[po.sku_id].code,
                "customer": ctx.customers[o.customer_id].name if o else None,
                "order": o.number if o else None, "priority": po.priority,
                "remaining": max(0, po.required_qty - po.completed_qty),
                "start": iso(r["start"]), "end": iso(r["end"]), "due": iso(due),
                "late_h": round(max(0.0, (r["end"] - due).total_seconds() / 3600), 1),
            })
        out.append({"line_id": ln.id, "line": ln.name, "status": ln.status,
                    "rate": round(ctx.line_rate(ln)), "orders": items})
    return out


def _summary(ev):
    return {k: ev[k] for k in ("changeovers", "late_orders", "total_delay_h", "output_24h", "avg_completion_h")} | {
        "makespan": iso(ev["makespan"])}


# ── optimizer ───────────────────────────────────────────────────────────
def optimize(db: Session, factory_id: int, now: datetime, user: str) -> m.OptimizationRun:
    ctx = PlanCtx(db, [factory_id], now)
    before_q = ctx.current_queues()
    before = evaluate(ctx, before_q)

    fixed = {}
    for ln in ctx.lines:
        cur = next((p for p in before_q[ln.id] if p.id == ln.current_po_id and p.completed_qty > 0), None)
        if cur:
            fixed[ln.id] = cur
    fixed_ids = {p.id for p in fixed.values()}
    movable = [p for p in ctx.pos if p.id not in fixed_ids]

    # 1. Construction: earliest due date first, best line by incremental cost.
    queues = {ln.id: ([fixed[ln.id]] if ln.id in fixed else []) for ln in ctx.lines}
    movable.sort(key=lambda p: (ctx.due(p), -ctx.weight(p)))
    for po in movable:
        best, best_cost = None, None
        for ln in ctx.lines:
            res, co = evaluate_line(ctx, ln, queues[ln.id] + [po])
            r = res[po.id]
            late = max(0.0, (r["end"] - ctx.due(po)).total_seconds() / 3600)
            cost = late * ctx.weight(po) * 10 + co * 0.25 + (r["end"] - now).total_seconds() / 3600 * 1.0
            if best_cost is None or cost < best_cost:
                best, best_cost = ln.id, cost
        queues[best].append(po)

    # 2. Local search: relocate each movable order to its best position.
    def total(qs):
        return objective(evaluate(ctx, qs), ctx)

    cur_cost = total(queues)
    for _ in range(3):
        improved = False
        for po in movable:
            src = next(l for l, q in queues.items() if po in q)
            base = {l: list(q) for l, q in queues.items()}
            base[src].remove(po)
            best_q, best_c = None, cur_cost
            for lid, q in base.items():
                start_idx = 1 if lid in fixed else 0
                for i in range(start_idx, len(q) + 1):
                    trial = {l: list(x) for l, x in base.items()}
                    trial[lid].insert(i, po)
                    c = total(trial)
                    if c < best_c - 1e-6:
                        best_q, best_c = trial, c
            if best_q:
                queues, cur_cost, improved = best_q, best_c, True
        if not improved:
            break

    after = evaluate(ctx, queues)
    # Never propose something worse than the current plan.
    if objective(after, ctx) > objective(before, ctx):
        queues, after = before_q, before

    run = m.OptimizationRun(
        factory_id=factory_id, created_at=now, created_by=user, status="Proposed",
        before={"summary": _summary(before), "lines": schedule_snapshot(ctx, before_q, before)},
        after={"summary": _summary(after), "lines": schedule_snapshot(ctx, queues, after),
               "assignment": {str(l): [p.id for p in q] for l, q in queues.items()}},
        metrics={
            "delay_reduction_h": round(before["total_delay_h"] - after["total_delay_h"], 1),
            "changeovers_saved": before["changeovers"] - after["changeovers"],
            "late_orders_before": before["late_orders"], "late_orders_after": after["late_orders"],
            "output_gain_24h": after["output_24h"] - before["output_24h"],
            "orders_considered": len(ctx.pos),
            "algorithm": "EDD construction with changeover-aware line assignment + relocation local search",
        },
    )
    db.add(run)
    db.flush()
    return run


def apply_run(db: Session, run: m.OptimizationRun, now: datetime) -> None:
    assignment = run.after.get("assignment", {})
    for lid, po_ids in assignment.items():
        for i, pid in enumerate(po_ids):
            po = db.get(m.ProductionOrder, pid)
            if po and po.status in OPEN_PO:
                po.line_id = int(lid)
                po.sequence = i
    # Lines whose queued order changed pick up the new head at the next tick.
    for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id == run.factory_id)):
        cur = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
        if cur and (cur.line_id != ln.id) and cur.completed_qty == 0:
            ln.current_po_id = None
            if ln.status == "Running":
                ln.status = "Idle"
                ln.status_reason = "Re-sequenced by optimizer"
    run.status = "Accepted"
    db.flush()
    recompute(db, now, [run.factory_id])
