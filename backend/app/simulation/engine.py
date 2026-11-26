"""Simulation engine.

Each tick advances the simulated clock and moves material through the
whole lifecycle using the same tables the UI reads:

  inbound shipments -> raw material stock -> production lines (filling/sewing)
  -> packing station (carton count validation) -> pallets -> put-away
  -> allocation -> picking -> truck loading -> dispatch -> delivery

Machine telemetry, workforce activity, changeovers, downtime, schedule
projections and alerts are updated in the same pass."""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import get_settings
from ..services import alerts, calendar as cal, inventory, loss, machine_health as mh, planning, quality, readiness
from ..services import procurement as procurement_svc
from ..services.common import business_rules, log_event, next_number, state
from ..services.planning import OPEN_PO, SEWERS_REQUIRED, machine_condition, sewing_capacity
from . import rfid

RNG = random.Random(get_settings().sim_seed)
RNG_WIP = random.Random(get_settings().sim_seed + 31)  # buffer noise; separate stream keeps the engine's sequence
DOCKS = ["D1", "D2", "D3", "D4"]
RECEIVING_DOCKS = ["R1", "R2"]
TELEMETRY_EVERY_MIN = 10
PICK_BUDGET: dict[int, float] = {}   # unused pick capacity carried between ticks
ALLOC_HORIZON_H = 72   # free finished-goods stock is reserved for orders shipping within this window
PICK_HORIZON_H = 24    # orders are picked to the staging lane up to a day before their truck
ORDER_LOAD = 0.72      # new demand as a share of rated line capacity (lines achieve ~80-85 % of rated)
PUTAWAY_BUDGET: dict[int, float] = {}
PACK_BUDGET: dict[int, float] = {}     # packing-station capacity carried between ticks (per line)
FAST_MODE = False  # seed: skip per-tick alert evaluation


def reseed(seed: int) -> None:
    RNG.seed(seed)
    RNG_WIP.seed(seed + 31)
    loss.reseed(seed)
    rfid.reseed(seed)


def downtime_category(reason: str | None) -> str:
    r = (reason or "").lower()
    if r.startswith("material"):
        return "Material shortage"
    if r.startswith("operator"):
        return "Operator-related delay"
    if r.startswith("no production order"):
        return "No orders"  # line starved of work: not a machine loss, excluded from planned time
    return "Other"


class TickCtx:
    def __init__(self, db: Session, a: datetime, b: datetime):
        self.db, self.a, self.b = db, a, b
        self.rules = business_rules(db)
        self.segs = cal.segments(a, b)
        self.total_pm = sum(s[3] for s in self.segs)
        self.factories = list(db.scalars(select(m.Factory).order_by(m.Factory.id)))
        self.lines = list(db.scalars(select(m.ProductionLine).order_by(m.ProductionLine.id)))
        self.machines = defaultdict(list)
        for mc in db.scalars(select(m.Machine)):
            self.machines[mc.line_id].append(mc)
        self.skus = {s.id: s for s in db.scalars(select(m.Sku))}
        self.materials = {x.id: x for x in db.scalars(select(m.Material))}
        self.stock = {(s.factory_id, s.material_id): s for s in db.scalars(select(m.MaterialStock))}
        self.emps = defaultdict(list)
        self.wh_crew = defaultdict(int)
        for e in db.scalars(select(m.Employee).where(m.Employee.attendance.in_(("Present", "Late")))):
            if e.line_id:
                self.emps[(e.line_id, e.shift)].append(e)
            if e.role in ("Warehouse associate", "Forklift operator"):
                self.wh_crew[(e.factory_id, e.shift)] += 1
            if e.role == "Packaging operator" and e.line_id:
                pass
        dates = {s[0] for s in self.segs}
        self.hourly = {}
        if dates:
            for h in db.scalars(select(m.ProductionHourly).where(m.ProductionHourly.date.in_(dates))):
                self.hourly[(h.line_id, h.date, h.hour, h.shift)] = h
        self.stats = {}
        if dates:
            for s in db.scalars(select(m.EmployeeShiftStat).where(m.EmployeeShiftStat.date.in_(dates))):
                self.stats[(s.employee_id, s.date, s.shift)] = s
        self.open_dt = {}
        for ev in db.scalars(select(m.DowntimeEvent).where(m.DowntimeEvent.end.is_(None))):
            self.open_dt[ev.line_id] = ev
        self.wip_rows = {w.line_id: w for w in db.scalars(select(m.LineWip))}

    def wip(self, line, d):
        w = self.wip_rows.get(line.id)
        if w is None:
            w = m.LineWip(line_id=line.id, date=d, sewing=0.0, bagging=0.0)
            self.db.add(w)
            self.wip_rows[line.id] = w
        if w.date != d:
            w.date, w.sewing, w.bagging = d, 0.0, 0.0
        return w

    def hourly_row(self, line, d, hour, shift):
        k = (line.id, d, hour, shift)
        h = self.hourly.get(k)
        if h is None:
            planned = line.planned_rate * cal.planned_minutes_in_hour(d, hour, shift) / 60
            h = m.ProductionHourly(line_id=line.id, factory_id=line.factory_id, date=d, hour=hour, shift=shift,
                                   planned=round(planned, 1), actual=0, run_min=0, downtime_min=0)
            self.db.add(h)
            self.hourly[k] = h
        return h

    def stat(self, emp, d, shift):
        k = (emp.id, d, shift)
        s = self.stats.get(k)
        if s is None:
            s = m.EmployeeShiftStat(employee_id=emp.id, date=d, shift=shift, units=0, productive_min=0, idle_min=0, paid_min=0)
            self.db.add(s)
            self.stats[k] = s
        return s


# ─── line state helpers ──────────────────────────────────────────────────
def _close_downtime(ctx: TickCtx, line, t):
    ev = ctx.open_dt.pop(line.id, None)
    if ev:
        ev.end = t


def _open_downtime(ctx: TickCtx, line, t, category, reason, machine_id=None):
    ev = ctx.open_dt.get(line.id)
    if ev and ev.category == category:
        return ev
    _close_downtime(ctx, line, t)
    ev = m.DowntimeEvent(line_id=line.id, machine_id=machine_id, factory_id=line.factory_id, category=category,
                         reason=reason, start=t, end=None, minutes=0)
    ctx.db.add(ev)
    ctx.open_dt[line.id] = ev
    return ev


def _account_downtime(ctx: TickCtx, line, hr, minutes, d, shift, category, reason, t, machine_id=None):
    if minutes <= 0:
        return
    hr.downtime_min += minutes
    ev = _open_downtime(ctx, line, t, category, reason, machine_id)
    ev.minutes += minutes
    for e in ctx.emps[(line.id, shift)]:
        if e.role in ("Filler", "Sewer", "Bagger"):
            ctx.stat(e, d, shift).idle_min += minutes


def set_running(ctx: TickCtx, line, t):
    if line.status != "Running":
        line.status, line.status_reason, line.status_since = "Running", None, t
    _close_downtime(ctx, line, t)


def set_idle(ctx: TickCtx, line, t, reason):
    if line.status != "Idle" or line.status_reason != reason:
        line.status, line.status_reason, line.status_since = "Idle", reason, t


def select_next(ctx: TickCtx, line, t) -> m.ProductionOrder | None:
    db = ctx.db
    cands = list(db.scalars(select(m.ProductionOrder).where(
        m.ProductionOrder.line_id == line.id, m.ProductionOrder.status.in_(OPEN_PO))
        .order_by(m.ProductionOrder.sequence, m.ProductionOrder.id)))
    cands = [p for p in cands if p.completed_qty < p.required_qty]
    if not cands:
        line.current_po_id = None
        set_idle(ctx, line, t, "No production order queued")
        return None
    po = cands[0]
    prev = line.current_sku_id
    line.current_po_id = po.id
    if prev and prev != po.sku_id:
        std = ctx.rules["changeover_standard_min"]
        dur = std * (line.changeover_factor or 1.0) * RNG.uniform(0.85, 1.4)
        until = cal.add_productive(t, dur)
        db.add(m.ChangeoverEvent(line_id=line.id, factory_id=line.factory_id, prev_sku_id=prev, new_sku_id=po.sku_id,
                                 customer_id=ctx.skus[po.sku_id].customer_id, po_id=po.id, start=t, end=None,
                                 standard_min=std))
        line.status, line.status_since = "Changeover", t
        line.status_reason = f"{ctx.skus[prev].code} → {ctx.skus[po.sku_id].code}"
        line.changeover_until = until
    else:
        set_running(ctx, line, t)
    line.current_sku_id = po.sku_id
    if not po.started_at:
        po.started_at = t
    po.status = "In Progress"
    log_event(db, t, line.factory_id, "production", "po_started", f"{line.name} started {po.number} ({ctx.skus[po.sku_id].code})",
              "production_order", po.id)
    return po


def finish_changeover(ctx: TickCtx, line, t):
    ev = ctx.db.scalar(select(m.ChangeoverEvent).where(m.ChangeoverEvent.line_id == line.id,
                                                       m.ChangeoverEvent.end.is_(None)))
    if ev:
        ev.end = t
        ev.duration_min = round(max(0.5, cal.productive_minutes(ev.start, t)), 1)
        ev.lost_min = round(max(0.0, ev.duration_min - ev.standard_min), 1)
        ev.flagged = ev.duration_min > ev.standard_min
    line.changeover_until = None
    set_running(ctx, line, t)


def restore_time(ctx: TickCtx, line):
    down = [mc for mc in ctx.machines[line.id] if mc.status in ("Breakdown", "Maintenance")]
    if not down:
        return ctx.a
    if any(mc.unavailable_until is None for mc in down):
        return None
    return max(mc.unavailable_until for mc in down)


def restore_machines(ctx: TickCtx, line, t):
    db = ctx.db
    for mc in ctx.machines[line.id]:
        if mc.status in ("Breakdown", "Maintenance") and mc.unavailable_until and mc.unavailable_until <= t:
            was = mc.status
            mc.status = "Running"
            mc.unavailable_until = None
            mc.hours_since_maintenance = 0
            mc.degradation = 0.0002
            mc.last_maintenance_at = t
            mc.health_score = 96.0
            for wo in db.scalars(select(m.WorkOrder).where(m.WorkOrder.machine_id == mc.id,
                                                           m.WorkOrder.status == "In Progress")):
                wo.status, wo.completed_at = "Completed", t
            log_event(db, t, line.factory_id, "production", "machine_restored",
                      f"{mc.code} {mc.name} back in service after {was.lower()}", "machine", mc.id)
    if line.status in ("Breakdown", "Maintenance"):
        line.status = "Idle"
        line.status_reason = "Restarting after maintenance"


def complete_po(ctx: TickCtx, line, po, t):
    po.status = "Completed"
    po.completed_at = t
    po.estimated_completion = t
    line.current_po_id = None
    log_event(ctx.db, t, line.factory_id, "production", "po_completed",
              f"{po.number} completed on {line.name} ({po.required_qty:,} pillows)", "production_order", po.id)


# ─── production ──────────────────────────────────────────────────────────
def produce(ctx: TickCtx, line, po, hr, pm, t, shift, d) -> tuple[int, float]:
    db = ctx.db
    sku = ctx.skus[po.sku_id]
    present = ctx.emps[(line.id, shift)]
    sewers = [e for e in present if e.role == "Sewer"]
    sfac = sewing_capacity(sewers)
    if sfac <= 0.05:
        set_idle(ctx, line, t, "Operator shortage — no sewers at station")
        return 0, 0.0
    mfac = machine_condition(ctx.machines[line.id])
    # RFID: sewing capacity counts only the time each sewer's badge was at the sewing table
    pres = {e.id: rfid.presence(e.id) for e in present}
    sfac_live = min(1.05, sum(e.skill * e.productivity_modifier * pres[e.id] for e in sewers) / SEWERS_REQUIRED)
    # micro-stoppages: fibre jams / operator away
    stop = 0.0
    if RNG.random() < 0.004 * pm:
        stop = min(pm, RNG.uniform(2, 6))
        db.add(m.DowntimeEvent(line_id=line.id, factory_id=line.factory_id, category="Other",
                               reason="Fibre feed jam cleared", start=t, end=t + timedelta(minutes=stop), minutes=stop))
    elif sfac < 0.92 and RNG.random() < 0.006 * pm:
        stop = min(pm, RNG.uniform(2, 5))
        db.add(m.DowntimeEvent(line_id=line.id, factory_id=line.factory_id, category="Operator-related delay",
                               reason="Sewing station waiting on operator", start=t, end=t + timedelta(minutes=stop), minutes=stop))
    if stop:
        hr.downtime_min += stop
    run = pm - stop
    eff = line.base_efficiency * line.perf_modifier * min(mfac, sfac_live) * RNG.uniform(0.965, 1.035)
    potential = line.planned_rate / 60 * run * eff + (line.output_carry or 0)
    n = int(potential)
    carry = potential - n
    n = min(n, po.required_qty - po.completed_qty)
    fill = ctx.stock.get((line.factory_id, sku.fill_material_id))
    shell = ctx.stock.get((line.factory_id, sku.shell_material_id))
    cap = min(fill.on_hand / sku.fill_kg if fill else 1e9, shell.on_hand if shell else 1e9)
    used = pm
    if cap < 1:
        mat = ctx.materials[sku.fill_material_id if (fill and fill.on_hand / sku.fill_kg < 1) else sku.shell_material_id]
        set_idle(ctx, line, t, f"Material shortage — {mat.code} {mat.name}")
        log_event(db, t, line.factory_id, "production", "material_stockout",
                  f"{line.name} stopped: {mat.code} stock exhausted", "material", mat.id)
        return 0, 0.0
    if n > cap:
        used = pm * (cap / max(n, 1))
        n = int(cap)
        carry = 0
    if fill:
        fill.on_hand -= n * sku.fill_kg
        fill.consumed_today = (fill.consumed_today or 0) + n * sku.fill_kg
    if shell:
        shell.on_hand -= n
        shell.consumed_today = (shell.consumed_today or 0) + n
    line.output_carry = carry
    po.completed_qty += n
    po.pack_queue += n
    hr.actual += n
    hr.run_min += run * (used / pm if pm else 1)
    update_wip(ctx, line, po, d, n, present, pres)
    # workforce activity, credited by employee ID and RFID presence at the station
    total_skill = sum(e.skill * e.productivity_modifier * pres[e.id] for e in sewers) or 1
    for e in present:
        st = ctx.stat(e, d, shift)
        f = pres[e.id]
        if e.role in ("Filler", "Sewer", "Bagger", "Packaging operator"):
            st.productive_min += run * (used / pm if pm else 1) * f
            st.idle_min += stop
        if e.role == "Sewer":
            st.units += n * (e.skill * e.productivity_modifier * f) / total_skill
        elif e.role in ("Filler", "Bagger", "Packaging operator"):
            st.units += n * f
    return n, used


def update_wip(ctx: TickCtx, line, po, d, n: int, present, pres) -> None:
    """Pillows between stations. The bagging output is the line's completed count;
    the buffers in front of sewing and bagging grow while those operators are
    away (RFID) and shrink as the order nears its end, so
    filled >= sewn >= bagged holds and every count only moves forward."""
    w = ctx.wip(line, d)
    rate_min = line.planned_rate / 60
    away = lambda role: (sum(1 - pres[e.id] for e in present if e.role == role)  # noqa: E731
                         / max(1, sum(1 for e in present if e.role == role)))
    remaining = max(0.0, po.required_qty - po.completed_qty)
    filler_away = away("Filler")
    t_sew = rate_min * (1.6 + 9.0 * away("Sewer")) * (1 - 0.7 * filler_away) * RNG_WIP.uniform(0.85, 1.15)
    t_bag = rate_min * (1.0 + 7.0 * away("Bagger")) * RNG_WIP.uniform(0.85, 1.15)
    t_bag = min(t_bag, remaining)
    t_sew = min(t_sew, max(0.0, remaining - t_bag))
    old_bag, old_sew = w.bagging or 0.0, w.sewing or 0.0
    new_bag = max(t_bag if n else old_bag, old_bag - n)          # bagging buffer drains at most n per tick
    new_sew = max(t_sew if n else old_sew, old_sew + old_bag - new_bag - n) if n else old_sew
    w.bagging, w.sewing = round(new_bag, 2), round(max(0.0, new_sew), 2)
    w.updated_at = ctx.b


def material_ok(ctx: TickCtx, line, po) -> bool:
    sku = ctx.skus[po.sku_id]
    fill = ctx.stock.get((line.factory_id, sku.fill_material_id))
    shell = ctx.stock.get((line.factory_id, sku.shell_material_id))
    return (not fill or fill.on_hand >= sku.fill_kg * 5) and (not shell or shell.on_hand >= 5)


def simulate_line(ctx: TickCtx, line) -> None:
    db = ctx.db
    po = db.get(m.ProductionOrder, line.current_po_id) if line.current_po_id else None
    if po and (po.status == "Completed" or po.line_id != line.id):
        po, line.current_po_id = None, None
    if po is None and line.status in ("Running", "Idle") and ctx.segs:
        po = select_next(ctx, line, ctx.segs[0][4])
    for (d, hour, sh, pm, s0, s1) in ctx.segs:
        hr = ctx.hourly_row(line, d, hour, sh)
        rem, t = pm, s0
        guard = 0
        while rem > 1e-6 and guard < 12:
            guard += 1
            if line.status in ("Breakdown", "Maintenance"):
                until = restore_time(ctx, line)
                if until is not None and until <= t:
                    restore_machines(ctx, line, t)
                    po = select_next(ctx, line, t)
                    continue
                end = min(s1, until) if until else s1
                lost = max(0.0, (end - t).total_seconds() / 60)
                down = next((mc for mc in ctx.machines[line.id] if mc.status in ("Breakdown", "Maintenance")), None)
                cat = "Mechanical breakdown" if line.status == "Breakdown" else "Other"
                _account_downtime(ctx, line, hr, min(lost, rem), d, sh, cat,
                                  line.status_reason or line.status, t, down.id if down else None)
                rem -= min(lost, rem)
                t = end
                continue
            if line.status == "Changeover":
                until = line.changeover_until or t
                if until <= t:
                    finish_changeover(ctx, line, t)
                    continue
                end = min(s1, until)
                lost = max(0.0, (end - t).total_seconds() / 60)
                _account_downtime(ctx, line, hr, min(lost, rem), d, sh, "Changeover", line.status_reason, t)
                rem -= min(lost, rem)
                t = end
                continue
            if line.status == "Idle":
                reason = line.status_reason or ""
                if po is None:
                    po = select_next(ctx, line, t)
                    if line.status != "Idle":
                        continue
                elif reason.startswith("Operator"):
                    if sewing_capacity([e for e in ctx.emps[(line.id, sh)] if e.role == "Sewer"]) > 0.05:
                        set_running(ctx, line, t)
                        continue
                elif reason.startswith("Material"):
                    if material_ok(ctx, line, po):
                        set_running(ctx, line, t)
                        log_event(db, t, line.factory_id, "production", "line_resumed",
                                  f"{line.name} resumed {po.number}: material available again", "line", line.id)
                        continue
                else:
                    set_running(ctx, line, t)
                    continue
                _account_downtime(ctx, line, hr, rem, d, sh, downtime_category(line.status_reason),
                                  line.status_reason or "Idle", t)
                rem = 0
                continue
            # Running
            if po is None:
                po = select_next(ctx, line, t)
                if po is None or line.status != "Running":
                    continue
            n, used = produce(ctx, line, po, hr, rem, t, sh, d)
            if used <= 0:
                continue
            rem -= used
            t += timedelta(minutes=used)
            if po.completed_qty >= po.required_qty:
                complete_po(ctx, line, po, t)
                po = select_next(ctx, line, t)


# ─── packaging ───────────────────────────────────────────────────────────
def simulate_packaging(ctx: TickCtx, factory) -> None:
    if ctx.total_pm <= 0:
        return
    db = ctx.db
    from ..cv.inference import count_pillows  # local import keeps engine importable without cv deps
    lines = [ln for ln in ctx.lines if ln.factory_id == factory.id]
    pos = list(db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id == factory.id,
                                                          m.ProductionOrder.pack_queue > 0).order_by(m.ProductionOrder.id)))
    by_line = defaultdict(list)
    for po in pos:
        by_line[po.line_id].append(po)
    cams = {c.line_id for c in db.scalars(select(m.Camera).where(m.Camera.factory_id == factory.id,
                                                                  m.Camera.station == "Carton packing"))}
    span = (ctx.b - ctx.a)
    for ln in lines:
        # Packing runs at 1.3x line rate; unused capacity carries over so a carton
        # larger than one tick's capacity (e.g. a 60-pillow Gaylord) still gets packed.
        cap = PACK_BUDGET.get(ln.id, 0.0) + ln.planned_rate * 1.3 / 60 * ctx.total_pm
        queue = by_line.get(ln.id, [])
        cartons_to_make = []
        for po in queue:
            sku = ctx.skus[po.sku_id]
            ppc = sku.pillows_per_carton
            q = po.pack_queue
            while q >= ppc and cap >= ppc:
                cartons_to_make.append(po)
                q -= ppc
                cap -= ppc
        PACK_BUDGET[ln.id] = min(cap, 130.0)
        total = len(cartons_to_make)
        for i, po in enumerate(cartons_to_make):
            sku = ctx.skus[po.sku_id]
            ppc = sku.pillows_per_carton
            if po.pack_queue < ppc:
                continue
            if not quality.packaging_available(db, factory.id, sku):
                log_event(db, ctx.b, factory.id, "quality", "packaging_material_out",
                          f"Packing stopped on {ln.name}: carton/polybag stock exhausted for {sku.code}", "line", ln.id)
                break
            t = ctx.a + span * ((i + 1) / (total + 1))
            detected, defect = ppc, None
            r = RNG.random()
            if r < 0.0035:
                detected = ppc - 1
            elif r < 0.005 and po.pack_queue >= ppc + 1:
                detected = ppc + 1
            elif r < 0.0068:
                defect = RNG.choice(["Sewing defect", "Label mismatch", "Polybag seal failure", "Compression failure"])
            source, conf = "edge-vision", round(RNG.uniform(0.93, 0.995), 3)
            if ln.id in cams:
                detected, conf, source = count_pillows(detected, RNG)
            quality.pack_carton(db, po, ln, t, detected, defect, source, conf)
    # Close the last (partial) pallet of a finished production order once what is left in its packing
    # queue cannot fill another carton; those few loose pillows go back to the line as rework stock.
    open_pallet_pos = select(m.Pallet.po_id).where(m.Pallet.factory_id == factory.id, m.Pallet.status == "Open")
    for po in db.scalars(select(m.ProductionOrder).where(
            m.ProductionOrder.factory_id == factory.id, m.ProductionOrder.status == "Completed",
            (m.ProductionOrder.pack_queue > 0) | m.ProductionOrder.id.in_(open_pallet_pos))):
        ppc = ctx.skus[po.sku_id].pillows_per_carton
        if po.pack_queue >= ppc:
            continue
        if po.pack_queue > 0:
            log_event(db, ctx.b, factory.id, "quality", "loose_pillows",
                      f"{po.number}: {po.pack_queue} loose pillow(s) short of a full carton returned to rework", "production_order", po.id)
            po.pack_queue = 0
        quality.close_open_pallet(db, po, ctx.b)
    # Blocked cartons older than 90 minutes are reworked by the packing team.
    for c in db.scalars(select(m.Carton).where(m.Carton.factory_id == factory.id, m.Carton.status == "Blocked",
                                               m.Carton.created_at < ctx.b - timedelta(minutes=90)).limit(6)):
        quality.rework(db, c, ctx.b, "Packing team")


# ─── warehouse ───────────────────────────────────────────────────────────
def simulate_warehouse(ctx: TickCtx, factory) -> None:
    db = ctx.db
    hours = ctx.total_pm / 60
    if hours <= 0:
        return
    shift = cal.shift_of(ctx.a) or 1
    crew = ctx.wh_crew[(factory.id, shift)]
    prod = factory.wh_productivity or 1.0
    # put-away (pallets wait at least 15 minutes in the receiving lane)
    PUTAWAY_BUDGET[factory.id] = min(12.0, PUTAWAY_BUDGET.get(factory.id, 0.0) + crew * 10 * hours * prod)
    cap = int(PUTAWAY_BUDGET[factory.id])
    PUTAWAY_BUDGET[factory.id] -= cap
    pending = list(db.scalars(select(m.Pallet).where(
        m.Pallet.factory_id == factory.id, m.Pallet.status == "Pending Put-away",
        m.Pallet.closed_at <= ctx.b - timedelta(minutes=15)).order_by(m.Pallet.closed_at).limit(cap)))
    for p in pending:
        loc = inventory.suggest_location(db, p)
        if loc:
            inventory.put_away(db, p, loc, ctx.b, "Warehouse team")
    db.flush()
    # allocation: an order's own production is reserved for it whatever the ship date; free stock goes to
    # orders shipping within ALLOC_HORIZON_H, earliest ship date first
    for o in list(db.scalars(select(m.CustomerOrder).where(
            m.CustomerOrder.factory_id == factory.id, m.CustomerOrder.status == "Open",
            m.CustomerOrder.allocated_qty < m.CustomerOrder.qty).order_by(m.CustomerOrder.scheduled_dispatch))):
        soon = o.scheduled_dispatch <= ctx.b + timedelta(hours=ALLOC_HORIZON_H)
        inventory.allocate(db, o, ctx.b, "Warehouse team", own_only=not soon)
    db.flush()
    top_up_shortfalls(ctx, factory)
    # picking: orders whose truck (or ship date) is within PICK_HORIZON_H, expedited first, then by truck time
    rate = (factory.pick_rate_cartons_hr or 400) * prod * (crew / 5 if crew else 0.2)
    cap_cartons = min(rate * 2, PICK_BUDGET.get(factory.id, 0.0) + rate * hours)
    trucks = {t.order_id: t for t in db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id == factory.id,
        m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading"))))}
    allocs = list(db.scalars(select(m.Allocation).join(m.CustomerOrder).where(
        m.CustomerOrder.factory_id == factory.id, m.Allocation.status == "Reserved")))
    def due(a):
        t = trucks.get(a.order_id)
        return t.scheduled_arrival if t else db.get(m.CustomerOrder, a.order_id).scheduled_dispatch
    allocs.sort(key=lambda a: (0 if db.get(m.CustomerOrder, a.order_id).expedite_picking else 1, due(a), a.id))
    for a in allocs:
        o = db.get(m.CustomerOrder, a.order_id)
        if not (due(a) <= ctx.b + timedelta(hours=PICK_HORIZON_H) or o.expedite_picking):
            continue
        cost = a.cartons * (0.5 if o.expedite_picking else 1)
        if cost > cap_cartons:
            break
        cap_cartons -= cost
        inventory.pick(db, a, ctx.b, "Picker")
    PICK_BUDGET[factory.id] = max(0.0, cap_cartons)


def top_up_shortfalls(ctx: TickCtx, factory) -> None:
    """An order whose own production is finished and packed but which still lacks goods (rejected
    cartons, a confirmed loss) and cannot be covered from stock gets a top-up production order."""
    db = ctx.db
    orders = list(db.scalars(select(m.CustomerOrder).where(
        m.CustomerOrder.factory_id == factory.id, m.CustomerOrder.status == "Open",
        m.CustomerOrder.allocated_qty < m.CustomerOrder.qty)))
    if not orders:
        return
    pos_by = readiness.order_pos(db, [o.id for o in orders])
    waiting = [o for o in orders if pos_by[o.id] and all(
        p.status == "Completed" and (p.pack_queue or 0) == 0 for p in pos_by[o.id])]
    if not waiting:
        return
    blocked = {r[0] for r in db.execute(select(m.Carton.po_id).where(
        m.Carton.factory_id == factory.id, m.Carton.status == "Blocked"))}
    waiting = [o for o in waiting if not any(p.id in blocked for p in pos_by[o.id])]
    cover = readiness.coverable_stock(db, waiting)
    for o in waiting:
        short = o.qty - o.allocated_qty - cover.get(o.id, 0)
        if short > 0:
            planning.queue_top_up(db, o, short, ctx.b, "Planner")


# ─── inbound ─────────────────────────────────────────────────────────────
def simulate_inbound(ctx: TickCtx, factory) -> None:
    db = ctx.db
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id == factory.id,
                                                        m.InboundShipment.status.in_(("Scheduled", "In Transit", "Arrived")))):
        po = s.po
        if s.status == "Scheduled" and s.expected_arrival - timedelta(hours=24) <= ctx.b:
            s.status = "In Transit"
            log_event(db, ctx.b, factory.id, "procurement", "shipment_in_transit", f"{s.ref} ({s.bol_number}) in transit", "shipment", s.id)
        elif s.status == "In Transit" and s.expected_arrival <= ctx.b:
            s.status = "Arrived"
            s.actual_arrival = s.expected_arrival
            s.dock = s.dock or RNG.choice(RECEIVING_DOCKS)
            log_event(db, ctx.b, factory.id, "procurement", "shipment_arrived",
                      f"{s.ref} arrived at dock {s.dock}: {s.qty:,.0f} {ctx.materials[s.material_id].uom} {ctx.materials[s.material_id].code}",
                      "shipment", s.id)
        elif s.status == "Arrived" and s.actual_arrival:
            # receiving clears a delivery within 6 h; one the plant has run short of (< 1 day of cover) goes first
            st = ctx.stock.get((factory.id, s.material_id))
            urgent = st is not None and (st.avg_daily_consumption or 0) > 0 and st.on_hand < st.avg_daily_consumption
            if s.actual_arrival <= ctx.b - timedelta(hours=1 if urgent else 6):
                from ..services.procurement import confirm_receipt
                confirm_receipt(db, s, s.qty, 0.0, ctx.b, "Receiving",
                                "Received first: material below one day of cover" if urgent
                                else "Received after dock check")
        if po and po.status in ("Open", "Partially Received") and procurement_svc.is_delayed(s):
            po.status = "Delayed"
    auto_replenish(ctx, factory)


MRP_LAST: dict[int, datetime] = {}


def auto_replenish(ctx: TickCtx, factory) -> None:
    """MRP run by the simulated procurement team (hourly): for every material whose free stock does not
    cover what open production orders need, or whose cover is below the reorder threshold, raise a PO
    for the recommended quantity (lead time + safety + a week, at least the committed need), net of stock
    and supply on order. One MRP PO per material per day."""
    from ..services.procurement import create_po
    db = ctx.db
    last = MRP_LAST.get(factory.id)
    if last and ctx.b - last < timedelta(hours=1):
        return
    MRP_LAST[factory.id] = ctx.b
    recent = {p.material_id for p in db.scalars(select(m.PurchaseOrder).where(
        m.PurchaseOrder.factory_id == factory.id, m.PurchaseOrder.created_by == "MRP",
        m.PurchaseOrder.order_date >= ctx.b - timedelta(days=1)))}
    suppliers = list(db.scalars(select(m.Supplier)))
    for r in inventory.material_rows(db, [factory.id], ctx.b):
        if not r["tracked"] or r["material_id"] in recent or r["status"] not in ("Critical", "Low"):
            continue
        mat = ctx.materials[r["material_id"]]
        sup = next((s for s in suppliers if mat.code in (s.material_codes or [])), None)
        if not sup:
            continue
        qty = inventory.recommended_reorder(r, sup.lead_time_days)
        if qty <= 0:
            continue
        create_po(db, factory.id, sup.id, mat.id, qty, ctx.b + timedelta(days=sup.lead_time_days), ctx.b, "MRP",
                  notes=f"MRP: {r['status_reason']}")
    return


def _auto_replenish_cover_only(ctx: TickCtx, factory) -> None:  # previous rule, kept for reference
    from ..services.procurement import create_po
    db = ctx.db
    incoming = {}
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id == factory.id,
                                                        m.InboundShipment.status.in_(("Scheduled", "In Transit", "Arrived", "Receiving")))):
        incoming[s.material_id] = incoming.get(s.material_id, 0) + s.qty
    recent = {p.material_id for p in db.scalars(select(m.PurchaseOrder).where(
        m.PurchaseOrder.factory_id == factory.id, m.PurchaseOrder.created_by == "MRP",
        m.PurchaseOrder.order_date >= ctx.b - timedelta(days=1)))}
    suppliers = list(db.scalars(select(m.Supplier)))
    for (fid, mid), st in ctx.stock.items():
        cons = st.avg_daily_consumption or 0
        if fid != factory.id or cons <= 0 or mid in recent or st.on_hand / cons >= st.reorder_days:
            continue
        mat = ctx.materials[mid]
        sup = next((s for s in suppliers if mat.code in (s.material_codes or [])), None)
        if not sup or incoming.get(mid, 0) >= cons * (sup.lead_time_days + 2):
            continue
        qty = round(cons * (30 if mat.category == "Shell" else 8), -2)
        create_po(db, factory.id, sup.id, mid, qty, ctx.b + timedelta(days=sup.lead_time_days), ctx.b, "MRP",
                  notes="Auto-replenishment: cover below reorder threshold")


# ─── dispatch ────────────────────────────────────────────────────────────
def depart(db: Session, t: m.TruckAppointment, when: datetime, user: str, rules: dict, loaded: int | None = None) -> None:
    o = t.order
    t.loading_end = when
    t.departed_at = when
    t.loaded_qty = loaded if loaded is not None else o.picked_qty
    t.status = "Departed"
    if t.actual_arrival and t.loading_start:
        t.wait_min = round((t.loading_start - t.actual_arrival).total_seconds() / 60, 1)
    t.detention_charge = rules["detention_charge"] if t.wait_min > rules["detention_free_min"] else 0.0
    o.loaded_qty = t.loaded_qty
    o.status = "Dispatched"
    o.dispatched_at = when
    for a in db.scalars(select(m.Allocation).where(m.Allocation.order_id == o.id, m.Allocation.status == "Picked")):
        a.status = "Loaded"
    log_event(db, when, o.factory_id, "dispatch", "dispatched",
              f"{t.ref} departed with {o.number}: {t.loaded_qty:,} pillows" +
              (f" — detention ${t.detention_charge:,.0f}" if t.detention_charge else ""), "order", o.id, user)


def start_loading(t: m.TruckAppointment, when: datetime) -> None:
    t.status = "Loading"
    t.loading_start = max(when, t.actual_arrival or when)
    t.wait_min = round(((t.loading_start - t.actual_arrival).total_seconds() / 60) if t.actual_arrival else 0, 1)


def free_dock(db: Session, factory_id: int, start: datetime, minutes: int, exclude_id: int | None = None) -> str | None:
    end = start + timedelta(minutes=minutes)
    busy = set()
    for t in db.scalars(select(m.TruckAppointment).where(
            m.TruckAppointment.factory_id == factory_id,
            m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading", "Loaded")))):
        if t.id == exclude_id:
            continue
        ts = t.scheduled_arrival
        te = ts + timedelta(minutes=t.expected_loading_min or 60)
        if ts < end and start < te:
            busy.add(t.dock)
    for d in DOCKS:
        if d not in busy:
            return d
    return None


def simulate_dispatch(ctx: TickCtx, factory) -> None:
    db = ctx.db
    trucks = list(db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id == factory.id,
        m.TruckAppointment.status.in_(("Scheduled", "Arrived", "Loading"))).order_by(m.TruckAppointment.scheduled_arrival)))
    loading_docks = {t.dock for t in trucks if t.status == "Loading"}
    for t in trucks:
        o = t.order
        if t.status == "Scheduled":
            arrive = t.scheduled_arrival + timedelta(minutes=t.late_by_min or 0)
            if arrive <= ctx.b:
                t.status = "Arrived"
                t.actual_arrival = arrive
                log_event(db, arrive, factory.id, "dispatch", "truck_arrived",
                          f"{t.ref} ({t.carrier}) arrived at {t.dock} for {o.number if o else '—'}", "truck", t.id)
        if t.status == "Arrived":
            if o and o.picked_qty >= o.qty and t.dock not in loading_docks:
                start_loading(t, max(ctx.a, t.actual_arrival))
                loading_docks.add(t.dock)
            else:
                t.wait_min = round((ctx.b - t.actual_arrival).total_seconds() / 60, 1)
                if o:
                    from ..services.readiness import assess
                    info = assess(db, [o], ctx.b)[o.id]
                    t.delay_reason = info["readiness"]["status"]
                    if t.wait_min > 360:  # carrier releases the truck; the order is rebooked at projected readiness
                        t.status = "Cancelled"
                        t.detention_charge = ctx.rules["detention_charge"]
                        t.delay_reason = f"Released ({info['readiness']['status']})"  # wait length is in wait_min
                        o.scheduled_dispatch = (max(ctx.b + timedelta(hours=2), info["ready_eta"] + timedelta(hours=1))
                                                if info["ready_eta"] else ctx.b + timedelta(hours=24))
                        log_event(db, ctx.b, factory.id, "dispatch", "truck_released",
                                  f"{t.ref} released after {t.wait_min / 60:.1f} h; {o.number} rebooked for {o.scheduled_dispatch:%a %H:%M}",
                                  "order", o.id)
        if t.status == "Loading" and t.loading_start + timedelta(minutes=t.expected_loading_min or 60) <= ctx.b:
            depart(db, t, t.loading_start + timedelta(minutes=t.expected_loading_min or 60), "Dock team", ctx.rules)
    for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id == factory.id,
                                                      m.CustomerOrder.status == "Dispatched",
                                                      m.CustomerOrder.dispatched_at <= ctx.b - timedelta(hours=30))):
        o.status = "Delivered"
        o.delivered_at = o.dispatched_at + timedelta(hours=30)
        log_event(db, ctx.b, factory.id, "dispatch", "delivered", f"{o.number} delivered to {o.customer.name}", "order", o.id)
    # Book a truck for orders shipping within 30 hours that have none.
    active = {t.order_id for t in db.scalars(select(m.TruckAppointment).where(
        m.TruckAppointment.factory_id == factory.id, m.TruckAppointment.status != "Cancelled"))}
    for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id == factory.id,
                                                      m.CustomerOrder.status == "Open",
                                                      m.CustomerOrder.scheduled_dispatch <= ctx.b + timedelta(hours=30))):
        if o.id in active:
            continue
        when = max(o.scheduled_dispatch, ctx.b + timedelta(hours=2))
        dock = free_dock(db, factory.id, when, 75) or "D4"
        db.add(m.TruckAppointment(ref=next_number(db, m.TruckAppointment, "TRK-", 7000), carrier=RNG.choice(CARRIERS),
                                  factory_id=factory.id, customer_id=o.customer_id, order_id=o.id, dock=dock,
                                  scheduled_arrival=when, expected_loading_min=75, status="Scheduled"))
        db.flush()


CARRIERS = ["J.B. Hunt", "Schneider", "Werner", "Knight-Swift", "XPO", "Old Dominion", "Landstar"]


# ─── new demand ─────────────────────────────────────────────────────────
SHIP_SLOTS = (8, 10, 13, 15)
SHIP_BUFFER_H = 12  # promised ship date leaves this much slack after the projected ready time


def _ship_slot(t: datetime) -> datetime:
    """First customer pick-up slot at or after t."""
    d = t.date()
    while True:
        for h in SHIP_SLOTS:
            c = datetime.combine(d, time(h, 0))
            if c >= t:
                return c
        d += timedelta(days=1)


def maybe_new_order(ctx: TickCtx, factory) -> None:
    """New customer demand at about ORDER_LOAD of the plant's effective line capacity.

    The ship date is quoted from the real line queues: the order goes to the line that can finish it
    first, and the date allows for packing, inspection, put-away, picking and SHIP_BUFFER_H of slack."""
    lines = [ln for ln in ctx.lines if ln.factory_id == factory.id]
    cap_h = sum(ln.planned_rate * (ln.base_efficiency or 1.0) for ln in lines)  # pillows per productive hour
    scale = (factory.daily_target or 20000) / 20000
    mean_qty = 4200 * max(0.6, scale)
    if ctx.total_pm <= 0 or not cap_h or RNG.random() > ctx.total_pm / 60 * cap_h * ORDER_LOAD / mean_qty:
        return
    db = ctx.db
    # the plant's product range: SKUs whose fill and shell materials it stocks
    skus = [k for k in ctx.skus.values()
            if (factory.id, k.fill_material_id) in ctx.stock and (factory.id, k.shell_material_id) in ctx.stock]
    if not skus:
        return
    sku = RNG.choice(sorted(skus, key=lambda k: k.id))
    ppc = sku.pillows_per_carton
    qty = max(ppc, int(RNG.uniform(2400, 6000) * max(0.6, scale) // ppc * ppc))
    pctx = planning.PlanCtx(db, [factory.id], ctx.b)
    queues = pctx.current_queues()
    best, best_done = None, None
    for ln in pctx.lines:
        rate = pctx.line_rate(ln)
        if rate < 1:
            continue
        res, _ = planning.evaluate_line(pctx, ln, queues[ln.id])
        free_at = max([r["end"] for r in res.values()], default=pctx.available_from(ln))
        last_sku = queues[ln.id][-1].sku_id if queues[ln.id] else ln.current_sku_id
        co = pctx.changeover_min(ln) if last_sku not in (None, sku.id) else 0.0
        done = cal.add_productive(free_at, co + qty / rate * 60)
        if best_done is None or done < best_done:
            best, best_done = ln, done
    if best is None:
        return
    ready = cal.add_productive(best_done, planning.POST_PRODUCTION_LAG_MIN)
    quoted = ctx.b + timedelta(days=RNG.randint(1, 3))  # customer's requested lead time (FG racks hold ~3 days)
    ship = _ship_slot(max(quoted, ready + timedelta(hours=SHIP_BUFFER_H)))
    # Provisional number = the final ORD-{500 + id}; always above the seeded ORD-496…543 range.
    o = m.CustomerOrder(number=next_number(db, m.CustomerOrder, "ORD-", 500), customer_id=sku.customer_id,
                        factory_id=factory.id, sku_id=sku.id, qty=qty, order_date=ctx.b,
                        ship_window_start=ship - timedelta(days=21), ship_window_end=ship + timedelta(days=7),
                        scheduled_dispatch=ship, priority=RNG.choice(["High", "Medium", "Medium", "Low"]))
    db.add(o)
    db.flush()
    o.number = f"ORD-{500 + o.id}"
    seq = max([p.sequence or 0 for p in pctx.pos if p.line_id == best.id], default=0) + 1
    po = m.ProductionOrder(number=next_number(db, m.ProductionOrder, "PRD-", 24000), customer_order_id=o.id,
                           factory_id=factory.id, sku_id=sku.id, line_id=best.id, sequence=seq, required_qty=qty,
                           start_date=ctx.b, required_completion=ship - timedelta(hours=SHIP_BUFFER_H), priority=o.priority,
                           status="Planned")
    db.add(po)
    log_event(db, ctx.b, factory.id, "dispatch", "order_received",
              f"New order {o.number}: {qty:,} × {sku.code} for {ctx.skus[sku.id].customer.name}, ships {ship:%b %d %H:%M}",
              "order", o.id)


# ─── machines ───────────────────────────────────────────────────────────
def simulate_machines(ctx: TickCtx) -> None:
    db = ctx.db
    record = (ctx.a.minute // TELEMETRY_EVERY_MIN != ctx.b.minute // TELEMETRY_EVERY_MIN) or (ctx.b - ctx.a) >= timedelta(minutes=TELEMETRY_EVERY_MIN)
    lines = {ln.id: ln for ln in ctx.lines}
    for lid, mcs in ctx.machines.items():
        ln = lines.get(lid)
        running = ln is not None and ln.status == "Running" and ctx.total_pm > 0
        for mc in mcs:
            mh.update_machine(db, mc, running, ctx.total_pm / 60 if running else 0, ctx.b, RNG, record)
            # Severely degraded machines can fail if nobody intervenes.
            if running and mc.status == "Running" and mc.health_score < 45 and RNG.random() < 0.0015 * ctx.total_pm:
                from .scenarios import break_machine
                break_machine(db, mc, ctx.b, "Bearing failure after sustained high vibration", RNG)


# ─── day rollover ────────────────────────────────────────────────────────
def rollover(db: Session, day) -> None:
    from ..services.metrics import close_day
    close_day(db, day)
    for st in db.scalars(select(m.MaterialStock)):
        if st.consumed_today:
            st.avg_daily_consumption = round(0.6 * (st.avg_daily_consumption or st.consumed_today) + 0.4 * st.consumed_today, 1)
        st.consumed_today = 0
    for c in db.scalars(select(m.Camera)):
        c.inspections_today = 0
        c.detections_today = 0
    for e in db.scalars(select(m.Employee)):
        r = RNG.random()
        e.attendance = "Present" if r < 0.94 else "Late" if r < 0.96 else "Absent"
        if e.role == "Sewer" and e.productivity_modifier < 1:  # temporary declines recover overnight
            e.productivity_modifier = min(1.0, e.productivity_modifier + 0.15)
    for ln in db.scalars(select(m.ProductionLine)):
        ln.output_carry = 0
    rfid.close_all(db, datetime.combine(day, cal.DAY_END))
    for w in db.scalars(select(m.LineWip)):
        w.sewing, w.bagging = 0.0, 0.0
    db.execute(delete(m.RfidInterval).where(m.RfidInterval.date < day - timedelta(days=6)))
    db.execute(delete(m.TelemetryPoint).where(m.TelemetryPoint.ts < datetime.combine(day, time(0, 0)) - timedelta(days=1)))
    db.execute(delete(m.DetectionEvent).where(m.DetectionEvent.ts < datetime.combine(day, time(0, 0))))


# ─── tick ────────────────────────────────────────────────────────────────
def process_block(db: Session, a: datetime, b: datetime) -> None:
    ctx = TickCtx(db, a, b)
    rfid.simulate(db, a, b, {ln.id: ln for ln in ctx.lines}, {f.id: f for f in ctx.factories}, ctx.total_pm)
    for ln in ctx.lines:
        simulate_line(ctx, ln)
    db.flush()
    for f in ctx.factories:
        simulate_packaging(ctx, f)
        db.flush()
        simulate_warehouse(ctx, f)
        simulate_inbound(ctx, f)
        simulate_dispatch(ctx, f)
        maybe_new_order(ctx, f)
    loss.simulate(db, a, b, ctx.factories)  # own random stream: the production sequence is unchanged
    simulate_machines(ctx)
    db.flush()
    planning.recompute(db, b)
    if not FAST_MODE:
        alerts.evaluate(db, b)


def step(db: Session, minutes: float) -> datetime:
    st = state(db)
    a = st.sim_time
    b = a + timedelta(minutes=minutes)
    day_end = datetime.combine(a.date(), cal.DAY_END)
    if a >= day_end or b > day_end:
        if a < day_end:
            process_block(db, a, day_end)
        rollover(db, a.date())
        b = datetime.combine(a.date() + timedelta(days=1), cal.DAY_START)
        log_event(db, b, None, "system", "day_start", f"Shift 1 started {b:%a %b %d} (lines closed 23:00–06:00)")
    elif a.time() < cal.DAY_START:
        b = datetime.combine(a.date(), cal.DAY_START)
    else:
        process_block(db, a, b)
    st.sim_time = b
    st.tick = (st.tick or 0) + 1
    db.flush()
    return b


def advance_to(db: Session, until: datetime, step_min: float = 5) -> None:
    while state(db).sim_time < until:
        remaining = (until - state(db).sim_time).total_seconds() / 60
        step(db, min(step_min, remaining))
