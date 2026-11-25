"""Demo scenarios. Each trigger changes operational records; the effects
propagate through the engine, the schedule projection and the alert rules
exactly like organic events."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..services import alerts, planning, quality
from ..services.common import log_event, next_number, state


class ScenarioError(ValueError):
    pass


def _pick_line(db: Session, factory_id: int | None, line_id: int | None, rng: random.Random, statuses=("Running",)):
    if line_id:
        ln = db.get(m.ProductionLine, line_id)
        if not ln:
            raise ScenarioError("Unknown line")
        return ln
    q = select(m.ProductionLine).where(m.ProductionLine.status.in_(statuses))
    if factory_id:
        q = q.where(m.ProductionLine.factory_id == factory_id)
    lines = list(db.scalars(q))
    if not lines:
        raise ScenarioError("No running line available for this scenario")
    # Prefer a line working on an order that ships soon (most visible impact)
    def urgency(ln):
        po = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
        return po.required_completion if po and po.required_completion else datetime.max
    lines.sort(key=urgency)
    return lines[0] if rng.random() < 0.7 else rng.choice(lines)


def break_machine(db: Session, mc: m.Machine, now: datetime, reason: str, rng: random.Random, repair_min: int = 95) -> m.WorkOrder:
    ln = db.get(m.ProductionLine, mc.line_id)
    mc.status = "Breakdown"
    mc.health_score = 25.0
    mc.unavailable_until = now + timedelta(minutes=repair_min)
    mc.vibration = round(mc.baseline["vibration"] * 3.5, 2)
    ln.status = "Breakdown"
    ln.status_reason = f"{mc.code} {mc.name}: {reason}"
    ln.status_since = now
    wo = m.WorkOrder(number=next_number(db, m.WorkOrder, "WO-", 3000), machine_id=mc.id, factory_id=mc.factory_id,
                     wo_type="Corrective", status="In Progress", priority="Critical",
                     description=f"Breakdown: {reason}", created_at=now, started_at=now,
                     expected_completion=mc.unavailable_until)
    db.add(wo)
    db.add(m.TelemetryPoint(machine_id=mc.id, ts=now, rpm=0, temperature=round(mc.baseline["temperature"] * 1.3, 1),
                            vibration=mc.vibration, pressure=round(mc.baseline["pressure"] * 0.6, 2), power=0.5))
    log_event(db, now, mc.factory_id, "production", "breakdown",
              f"{mc.code} on {ln.name} broke down ({reason}); repair ETA {mc.unavailable_until:%H:%M}", "machine", mc.id)
    return wo


def machine_failure(db, now, rng, factory_id=None, line_id=None, machine_id=None):
    if machine_id:
        mc = db.get(m.Machine, machine_id)
    else:
        ln = _pick_line(db, factory_id, line_id, rng)
        mcs = list(db.scalars(select(m.Machine).where(m.Machine.line_id == ln.id, m.Machine.status == "Running")))
        if not mcs:
            raise ScenarioError("No running machine on that line")
        mc = next((x for x in mcs if "Motor" in x.machine_type), mcs[0])
    wo = break_machine(db, mc, now, "Drive motor bearing seizure", rng)
    return f"{mc.code} ({mc.name}) on {db.get(m.ProductionLine, mc.line_id).name} broke down; {wo.number} opened, repair ETA {mc.unavailable_until:%H:%M}"


def production_delay(db, now, rng, factory_id=None, line_id=None):
    ln = _pick_line(db, factory_id, line_id, rng)
    ln.perf_modifier = 0.72
    log_event(db, now, ln.factory_id, "production", "scenario_delay",
              f"{ln.name} running at reduced speed (fibre feed instability)", "line", ln.id)
    return f"{ln.name} slowed to ~72% of standard speed (fibre feed instability)"


def material_shortage(db, now, rng, factory_id=None, material_code=None):
    q = select(m.MaterialStock).join(m.Material)
    if factory_id:
        q = q.where(m.MaterialStock.factory_id == factory_id)
    if material_code:
        q = q.where(m.Material.code == material_code)
    else:
        # the fibre consumed by the most running lines
        running = list(db.scalars(select(m.ProductionLine).where(m.ProductionLine.status == "Running",
                                                                 *([m.ProductionLine.factory_id == factory_id] if factory_id else []))))
        counts = {}
        for ln in running:
            if ln.current_sku_id:
                sku = db.get(m.Sku, ln.current_sku_id)
                counts[(ln.factory_id, sku.fill_material_id)] = counts.get((ln.factory_id, sku.fill_material_id), 0) + 1
        if not counts:
            raise ScenarioError("No running lines consuming fibre")
        fid, mid = max(counts, key=counts.get)
        q = select(m.MaterialStock).where(m.MaterialStock.factory_id == fid, m.MaterialStock.material_id == mid)
    st = db.scalars(q).first()
    if not st:
        raise ScenarioError("Material not found")
    mat = db.get(m.Material, st.material_id)
    cons = st.avg_daily_consumption or 1000
    st.on_hand = round(cons * 0.12, 1)  # ~2 production hours left
    delayed = []
    for s in db.scalars(select(m.InboundShipment).where(m.InboundShipment.factory_id == st.factory_id,
                                                        m.InboundShipment.material_id == st.material_id,
                                                        m.InboundShipment.status.in_(("Scheduled", "In Transit", "Arrived")))):
        if s.status == "Arrived":
            s.status = "In Transit"
            s.actual_arrival = None
        s.original_expected_arrival = s.original_expected_arrival or s.expected_arrival
        s.expected_arrival = max(s.expected_arrival, now) + timedelta(days=3)
        s.delay_reason = "Supplier production delay — trailer not dispatched"
        s.po.status = "Delayed"
        delayed.append(s.ref)
    log_event(db, now, st.factory_id, "procurement", "scenario_shortage",
              f"{mat.code} stock reduced to {st.on_hand:,.0f} {mat.uom}; shipments {', '.join(delayed) or 'none'} delayed 3 days",
              "material", mat.id)
    return f"{mat.code} at {db.get(m.Factory, st.factory_id).code} cut to {st.on_hand:,.0f} {mat.uom}; delayed: {', '.join(delayed) or 'no open shipments'}"


def packaging_mismatch(db, now, rng, factory_id=None, line_id=None, count: int = 3):
    ln = _pick_line(db, factory_id, line_id, rng, ("Running", "Changeover", "Idle"))
    po = db.get(m.ProductionOrder, ln.current_po_id) if ln.current_po_id else None
    if not po:
        po = db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.line_id == ln.id)
                        .order_by(m.ProductionOrder.id.desc())).first()
    if not po:
        raise ScenarioError("No production order on that line")
    sku = db.get(m.Sku, po.sku_id)
    made = []
    for i in range(count):
        need = sku.pillows_per_carton
        if po.pack_queue < need:  # pillows come from the line's output buffer
            po.pack_queue += need
            po.completed_qty = min(po.required_qty, po.completed_qty + need) if po.completed_qty + need <= po.required_qty else po.completed_qty
        det = need - 1 if i % 3 != 2 else need + 1
        c = quality.pack_carton(db, po, ln, now + timedelta(seconds=20 * i), det, None, "simulated", 0.96)
        made.append(c.hold_code)
    return f"{len(made)} mismatched cartons created on {ln.name} ({sku.code}, spec {sku.pillows_per_carton}/carton): {', '.join(made)}"


def workforce_decline(db, now, rng, factory_id=None, line_id=None):
    ln = _pick_line(db, factory_id, line_id, rng)
    from ..services.calendar import shift_of
    sh = shift_of(now) or 1
    sewers = list(db.scalars(select(m.Employee).where(m.Employee.line_id == ln.id, m.Employee.role == "Sewer",
                                                      m.Employee.shift == sh)))
    for e in sewers:
        e.productivity_modifier = 0.62
    log_event(db, now, ln.factory_id, "production", "scenario_workforce",
              f"Sewing productivity declined on {ln.name}: {', '.join(e.code for e in sewers)}", "line", ln.id)
    return f"Sewers on {ln.name} (shift {sh}) dropped to ~62% productivity: {', '.join(e.code for e in sewers)}"


def truck_delay(db, now, rng, factory_id=None):
    q = select(m.TruckAppointment).where(m.TruckAppointment.status == "Scheduled")
    if factory_id:
        q = q.where(m.TruckAppointment.factory_id == factory_id)
    trucks = sorted(db.scalars(q), key=lambda t: t.scheduled_arrival)
    trucks = [t for t in trucks if t.order and t.order.picked_qty < t.order.qty] or trucks
    if not trucks:
        raise ScenarioError("No scheduled truck appointment to delay")
    t = trucks[0]
    o = t.order
    arrived = now - timedelta(minutes=140)
    t.scheduled_arrival = arrived
    t.actual_arrival = arrived
    t.status = "Arrived"
    t.late_by_min = 0
    t.delay_reason = "Order not picked — warehouse delay"
    t.wait_min = 140
    f = db.get(m.Factory, t.factory_id)
    f.wh_productivity = 0.3
    log_event(db, now, t.factory_id, "dispatch", "scenario_truck_delay",
              f"{t.ref} has been waiting 140 min at {t.dock}; {o.number} not picked (warehouse crew short)", "truck", t.id)
    return f"{t.ref} for {o.number} waiting 140 min at {t.dock}; warehouse picking slowed to 30% at {f.code}"


SCENARIOS = {
    "machine_failure": ("Generate machine failure", machine_failure),
    "production_delay": ("Generate production delay", production_delay),
    "material_shortage": ("Generate material shortage", material_shortage),
    "packaging_mismatch": ("Generate packaging mismatch", packaging_mismatch),
    "workforce_decline": ("Generate workforce productivity decline", workforce_decline),
    "truck_delay": ("Generate truck delay", truck_delay),
}


def trigger(db: Session, kind: str, rng: random.Random, **params) -> str:
    if kind not in SCENARIOS:
        raise ScenarioError(f"Unknown scenario {kind}")
    st = state(db)
    now = st.sim_time
    params = {k: v for k, v in params.items() if v is not None}
    msg = SCENARIOS[kind][1](db, now, rng, **params)
    st.scenario_log = [*(st.scenario_log or []), {"ts": now.isoformat(timespec="minutes"), "kind": kind, "message": msg}][-30:]
    db.flush()
    planning.recompute(db, now)
    alerts.evaluate(db, now)
    return msg
