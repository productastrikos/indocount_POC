"""Workforce productivity and labour cost.

Activity is simulated per employee ID from production events (no facial
recognition). Shift rules come from services/calendar.py; second shift is
paid the configured differential."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from .common import business_rules

PRODUCTION_ROLES = ("Filler", "Sewer", "Bagger", "Packaging operator")
LINE_ROLE_REQUIREMENT = {"Filler": 1, "Sewer": 2, "Bagger": 1}
SUPPORT_REQUIREMENT = {"Packaging operator": 4, "Supervisor": 2, "Maintenance technician": 2}  # per factory per shift
WAREHOUSE_ROLES = ("Warehouse associate", "Forklift operator")
BENCHMARK_SHARE = {"Sewer": 0.5, "Filler": 1.0, "Bagger": 1.0, "Packaging operator": 1.0}
INDIVIDUAL_ROLES = ("Sewer",)  # contribution measured per person (others follow line output)
# Roles whose shift presence is not recorded per person after the day (no production activity rows):
# past-day totals for them come from the shift summaries written at day close.
UNTRACKED_ROLES = ("Supervisor", "Maintenance technician") + WAREHOUSE_ROLES
LOW_PRODUCTIVITY_PCT = 80.0


def wage(e: m.Employee, rules: dict) -> float:
    return e.hourly_wage + (rules["shift2_differential"] if e.shift == 2 else 0.0)


def employee_rows(db: Session, factory_ids: list[int], day: date, now: datetime,
                  line_id: int | None = None, shift: int | None = None) -> list[dict]:
    rules = business_rules(db)
    q = select(m.Employee).where(m.Employee.factory_id.in_(factory_ids))
    if line_id:
        q = q.where(m.Employee.line_id == line_id)
    if shift:
        q = q.where(m.Employee.shift == shift)
    emps = list(db.scalars(q.order_by(m.Employee.factory_id, m.Employee.shift, m.Employee.line_id, m.Employee.role)))
    stats = {(s.employee_id, s.shift): s for s in db.scalars(select(m.EmployeeShiftStat).where(
        m.EmployeeShiftStat.date == day, m.EmployeeShiftStat.employee_id.in_([e.id for e in emps] or [0])))}
    lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine))}
    is_today = day == now.date()
    rows = []
    for e in emps:
        st = stats.get((e.id, e.shift))
        started = cal.shift_bounds(day, e.shift)[0] <= now
        untracked = not is_today and e.role in UNTRACKED_ROLES
        present = (e.attendance in ("Present", "Late") and started) if is_today else bool(st)
        if is_today:
            paid = cal.paid_minutes_elapsed(day, e.shift, now) if present else 0.0
        else:
            paid = cal.paid_minutes_per_shift() if present else 0.0
        units = st.units if st else 0.0
        prod_min = st.productive_min if st else 0.0
        idle_min = st.idle_min if st else 0.0
        ln = lines.get(e.line_id)
        share = BENCHMARK_SHARE.get(e.role)
        expected = (ln.planned_rate * share * prod_min / 60) if (ln and share) else None
        productivity = round(100 * units / expected, 1) if expected else None
        w = wage(e, rules)
        rows.append({
            "id": e.id, "code": e.code, "name": e.name, "role": e.role, "factory_id": e.factory_id,
            "line_id": e.line_id, "line": ln.name if ln else ("Warehouse" if e.role in WAREHOUSE_ROLES else "Plant"),
            "shift": e.shift, "employment_type": e.employment_type, "hourly_wage": round(w, 2),
            "base_wage": e.hourly_wage, "differential": round(w - e.hourly_wage, 2),
            "attendance": ((e.attendance if started else "Scheduled") if is_today
                           else "Not recorded" if untracked else ("Present" if present else "Absent")),
            "units": int(units), "productive_min": round(prod_min), "idle_min": round(idle_min),
            "paid_hours": round(paid / 60, 2), "labour_cost": round(paid / 60 * w, 2),
            "productivity_pct": productivity,
            "below_benchmark": e.role in INDIVIDUAL_ROLES and productivity is not None and prod_min >= 60
                               and productivity < LOW_PRODUCTIVITY_PCT,
        })
    return rows


def required_workers(n_lines: int, shifts: int, include_support: bool = True) -> int:
    per_line = sum(LINE_ROLE_REQUIREMENT.values())
    support = sum(SUPPORT_REQUIREMENT.values()) if include_support else 0
    return (per_line * n_lines + support) * shifts


def productive_hours_elapsed(day: date, shift: int, now: datetime) -> float:
    lo, hi = cal.shift_bounds(day, shift)
    end = hi if day < now.date() else min(hi, now)
    return cal.productive_minutes(lo, end) / 60 if end > lo else 0.0


def paid_hours_elapsed(day: date, shift: int, now: datetime) -> float:
    if day < now.date():
        return cal.paid_minutes_per_shift() / 60
    return cal.paid_minutes_elapsed(day, shift, now) / 60 if day == now.date() else 0.0


def summary(db: Session, factory_ids: list[int], day: date, now: datetime, pillows: float,
            line_id: int | None = None, shift: int | None = None) -> dict:
    """Workforce KPIs for a scope. Deployed and required count the same roles: the line crew (filler, sewers,
    bagger) for a line; every production role (line crews, packaging, supervisors, maintenance) for a plant.
    Past days take deployed and labour cost from the shift summaries written at day close, because support
    staff have no per-person activity rows."""
    rows = employee_rows(db, factory_ids, day, now, line_id, shift)
    prod = [r for r in rows if r["role"] not in WAREHOUSE_ROLES]
    if line_id:
        prod = [r for r in prod if r["role"] in LINE_ROLE_REQUIREMENT]
    present = [r for r in prod if r["attendance"] in ("Present", "Late")]
    shifts = (shift,) if shift else (1, 2)
    if day == now.date():
        started = [sh for sh in shifts if cal.shift_bounds(day, sh)[0] <= now]
    else:
        started = list(shifts) if day < now.date() else []
    shifts_in_scope = max(1, len(started))
    if line_id:
        required = sum(LINE_ROLE_REQUIREMENT.values()) * shifts_in_scope
    else:
        n_lines = db.query(m.ProductionLine).filter(m.ProductionLine.factory_id.in_(factory_ids)).count()
        required = (required_workers(n_lines, shifts_in_scope, include_support=False)
                    + sum(SUPPORT_REQUIREMENT.values()) * shifts_in_scope * len(factory_ids))
    history = []
    if day < now.date() and not line_id:
        history = list(db.scalars(select(m.ShiftSummary).where(
            m.ShiftSummary.factory_id.in_(factory_ids), m.ShiftSummary.date == day, m.ShiftSummary.shift.in_(shifts))))
        if len(history) != len(factory_ids) * len(shifts):
            history = []
    if history:
        deployed_by_shift = Counter()
        for h in history:
            deployed_by_shift[h.shift] += h.workers_deployed or 0
        cost = sum(h.labour_cost or 0 for h in history)
        deployed = sum(deployed_by_shift.values())
        absent = max(0, len(prod) - deployed)
    else:
        deployed_by_shift = Counter(r["shift"] for r in present)
        cost = sum(r["labour_cost"] for r in present)
        deployed = len(present)
        absent = sum(1 for r in prod if r["attendance"] in ("Absent", "On Leave"))
    # worker-hours = deployed in each shift x productive hours elapsed in that shift
    worker_hours = sum(n * productive_hours_elapsed(day, sh, now) for sh, n in deployed_by_shift.items())
    paid_hours = sum(paid_hours_elapsed(day, sh, now) for sh, n in deployed_by_shift.items() if n)
    prod_min = sum(r["productive_min"] for r in present)
    idle_min = sum(r["idle_min"] for r in present)
    return {
        "workers_required": required, "workers_deployed": deployed, "absent": absent,
        "deployed_by_shift": {int(sh): n for sh, n in sorted(deployed_by_shift.items())},
        "worker_hours": round(worker_hours, 2), "paid_hours": round(paid_hours, 2),
        "source": "shift summaries" if history else "live",
        "output_per_worker": round(pillows / deployed, 1) if deployed else 0,
        "idle_hours": round(idle_min / 60, 1),
        "idle_fte": round(idle_min / max(1.0, cal.productive_minutes_per_shift()), 1),
        "labour_utilization_pct": round(100 * prod_min / (prod_min + idle_min), 1) if (prod_min + idle_min) else 0,
        # crew cost per clock hour of paid time (one shift = that shift's hourly bill; both = the average)
        "labour_cost": round(cost, 2), "labour_cost_per_hour": round(cost / paid_hours, 2) if paid_hours else None,
        "labour_cost_per_pillow": round(cost / pillows, 3) if pillows else None,
        "below_benchmark": [r for r in rows if r["below_benchmark"]],
        "shift_rules": {
            "scheduled_presence_h": cal.PRESENCE_MIN / 60, "unpaid_lunch_min": cal.UNPAID_LUNCH_MIN,
            "paid_breaks": "2 × 10 min", "productive_min": cal.productive_minutes_per_shift(),
            "paid_min": cal.paid_minutes_per_shift(),
            "shift2_differential": business_rules(db)["shift2_differential"],
        },
        "employees": rows,
    }

