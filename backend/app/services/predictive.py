"""Predictive maintenance analytics and S!AP advisories.

ILLUSTRATIVE MODEL ON SIMULATED TELEMETRY: not a validated failure-prediction
model. Every response carries DISCLAIMER.

Failure model (per machine)
---------------------------
* Reliability: a Weibull distribution per machine type (WEIBULL: shape beta,
  scale eta in operating hours). Age t = operating hours since the last
  service; cumulative hazard H(t) = (t / eta) ** beta.
* Condition: a proportional-hazards multiplier M = exp(sum(gamma_i * x_i))
  from the current condition (COVARIATES): vibration / temperature above
  baseline, health-score deficit and accumulated wear.
* Probability of failure in the next d operating hours, given the machine
  is running now:  P = 1 - exp(-M * (H(t + d) - H(t))).
  Calendar horizons (24 h, 72 h, 7 d, 30 d) are converted to operating hours
  with the shift calendar (2 x 460 productive min a day = 15.3 h; nothing
  runs 23:00-06:00).
* Remaining useful life (RUL): P10 / P50 / P90 of the same conditional
  distribution (closed form). When the vibration trend (regression over
  recent running telemetry, per operating hour) reaches the alarm threshold
  (machine_health.VIBRATION_ALARM x baseline) sooner than the Weibull median,
  the more conservative trend drives the RUL: M is raised so that the median
  equals the trend time, which keeps probabilities and RUL consistent.
* Failure mode from the anomaly signature + machine type (_failure_mode,
  REPAIRS). Unplanned MTTR = planned MTTR x 1.8 + 0.5 h response.
* Impact: pillows at risk = line planned rate x unplanned MTTR (production
  hours); illustrative cost (COST); open production orders on the line.
* Maintenance window: expected loss of each option (run to failure, stop now,
  each remaining planned stop today, tonight's non-production window
  23:00-06:00) = P(failure before the window) x run-to-failure loss
  + (1 - P) x planned loss. The cheapest planned option is recommended.

S!AP advisories
---------------
advisories(db, factory_ids, now, limit=None) concatenates the output of every
generator in GENERATORS and ranks by severity, then impact. A generator is a
function `gen(ctx: AdvisoryContext) -> list[dict]` (ctx.db, ctx.fids, ctx.now,
ctx.factories and the lazily computed ctx.fleet). Append new generators to
GENERATORS. Each item is built with advisory(), which defines the contract.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from functools import cached_property
from itertools import groupby
from operator import itemgetter

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal
from . import machine_health as mh
from .common import iso, next_number, state

DISCLAIMER = ("Illustrative model: Weibull reliability x condition-based proportional hazards on simulated telemetry. "
              "Probabilities, remaining life and costs are estimates for demonstration, not a validated failure prediction.")

OP_HOURS_PER_DAY = round(2 * cal.productive_minutes_per_shift() / 60, 2)  # 2 x 460 min = 15.33 h

# Weibull reliability per machine type: shape beta (> 1 = wear-out) and scale eta
# (characteristic life, operating hours). Illustrative values for textile
# carding / filling equipment, not fitted to field data.
WEIBULL = {
    "Fibre Opener":       {"beta": 1.6, "eta": 7000, "dominant": "beater bearings and spike lattice"},
    "Flapper Unit":       {"beta": 1.5, "eta": 6000, "dominant": "flapper linkage and pivot bushings"},
    "Garnett Main Motor": {"beta": 2.0, "eta": 6500, "dominant": "drive-end bearing, belts, card clothing"},
    "Blow Fan Motor":     {"beta": 1.8, "eta": 7500, "dominant": "impeller balance and motor bearings"},
    "Filling Head":       {"beta": 1.4, "eta": 5000, "dominant": "pneumatic seals and fill valves"},
}
DEFAULT_WEIBULL = {"beta": 1.6, "eta": 6000, "dominant": "general wear"}

# Proportional-hazards covariates: M = exp(sum(gamma * x)); x = 0 at baseline condition.
COVARIATES = {
    "vibration":   {"gamma": 4.0, "label": "Vibration", "x": "vibration / baseline - 1.05 (excess ratio)"},
    "temperature": {"gamma": 8.0, "label": "Temperature", "x": "temperature / baseline - 1.02 (excess ratio)"},
    "health":      {"gamma": 1.1, "label": "Health score", "x": "(85 - health score) / 15, when below 85"},
    "wear":        {"gamma": 2.2, "label": "Wear index", "x": "degradation rate x hours since service"},
}
MAX_LOG_MULTIPLIER = 9.0

# Repair catalogue: failure mode, typical repair, part, planned MTTR (h) and crew size.
REPAIRS = {
    "bearing":  {"mode": "Drive-end bearing wear", "repair": "Replace drive-end bearing; check coupling alignment and lubrication",
                 "part": "Drive-end bearing kit", "part_cost": 420, "planned_mttr_h": 2.5, "crew": 2},
    "belt":     {"mode": "Drive belt slip", "repair": "Re-tension or replace the V-belt set; check pulley wear",
                 "part": "V-belt set", "part_cost": 160, "planned_mttr_h": 1.0, "crew": 1},
    "card":     {"mode": "Card clothing loading / motor overload", "repair": "Clean and re-point card clothing; verify fibre feed rate",
                 "part": "Card clothing service kit", "part_cost": 650, "planned_mttr_h": 3.0, "crew": 2},
    "overload": {"mode": "Motor overload (feed jam / drive drag)", "repair": "Clear feed path; check drive train drag and motor current",
                 "part": "Overload relay & contactor", "part_cost": 190, "planned_mttr_h": 1.0, "crew": 1},
    "impeller": {"mode": "Impeller imbalance", "repair": "Clean fibre build-up and dynamically balance the impeller",
                 "part": "Impeller balancing kit", "part_cost": 140, "planned_mttr_h": 1.5, "crew": 2},
    "seal":     {"mode": "Pneumatic seal leak (under-fill risk)", "repair": "Replace fill-valve pneumatic seals; recalibrate fill weight",
                 "part": "Fill-valve seal kit", "part_cost": 95, "planned_mttr_h": 1.0, "crew": 1},
    "air":      {"mode": "Pneumatic supply leak", "repair": "Locate and fix air leak; check regulator",
                 "part": "Pneumatic fittings & regulator kit", "part_cost": 85, "planned_mttr_h": 0.75, "crew": 1},
    "overheat": {"mode": "Motor overheating (cooling fault)", "repair": "Clean cooling fan and filters; megger-test the winding",
                 "part": "Cooling fan & filter set", "part_cost": 210, "planned_mttr_h": 1.5, "crew": 1},
    "beater":   {"mode": "Beater bearing / spike lattice wear", "repair": "Replace beater bearings; inspect the spike lattice",
                 "part": "Beater bearing pair", "part_cost": 360, "planned_mttr_h": 2.0, "crew": 2},
    "linkage":  {"mode": "Flapper linkage / pivot wear", "repair": "Replace pivot bushings and linkage arms; re-time the flapper",
                 "part": "Flapper pivot bushing kit", "part_cost": 180, "planned_mttr_h": 1.5, "crew": 1},
    "nozzle":   {"mode": "Fill-nozzle drive wear", "repair": "Replace the nozzle drive bearing; verify fill weight",
                 "part": "Nozzle drive bearing", "part_cost": 150, "planned_mttr_h": 1.0, "crew": 1},
}
VIBRATION_MODE = {"Fibre Opener": "beater", "Flapper Unit": "linkage", "Garnett Main Motor": "bearing",
                  "Blow Fan Motor": "impeller", "Filling Head": "nozzle"}
DEFAULT_MODE = {"Fibre Opener": "beater", "Flapper Unit": "linkage", "Garnett Main Motor": "bearing",
                "Blow Fan Motor": "impeller", "Filling Head": "seal"}
TOLERANCE = {"rpm": 0.008, "temperature": 0.06, "vibration": 0.25, "pressure": 0.04, "power": 0.06}  # as machine_health

# Illustrative cost and planning assumptions.
COST = {
    "contribution_margin_pct": 0.35,     # lost pillow = unit price x margin
    "default_unit_price_usd": 8.0,
    "technician_rate_usd_h": 42.0,       # loaded labour rate
    "after_hours_multiplier": 1.5,       # overtime call-in for the 23:00-06:00 window
    "unplanned_parts_multiplier": 1.3,   # collateral damage + expedited freight
    "unplanned_mttr_factor": 1.8,
    "response_h": 0.5,                   # time to get a technician to an unplanned stop
    "mobilise_min": 20,                  # lead time to start a planned job
    "technician_available_share": 0.6,   # share of paid time free for corrective / predictive work
    "paid_hours_per_shift": cal.paid_minutes_per_shift() / 60,
    "energy_usd_kwh": 0.11,
    "fill_weight_sensitivity": 0.6,      # fill weight change per unit of relative pressure change
    "underfill_limit_pct": -4.0,         # pressure deviation that puts fill weight out of tolerance
    "underfill_rework_share": 0.02,      # share of exposed pillows expected to need re-work if under-filled
}
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


# ─── maths ────────────────────────────────────────────────────────────
def _wb(mtype: str) -> dict:
    return WEIBULL.get(mtype, DEFAULT_WEIBULL)


def cum_hazard(t: float, beta: float, eta: float) -> float:
    return (max(t, 0.0) / eta) ** beta


def p_fail(age: float, d: float, beta: float, eta: float, mult: float) -> float:
    """P(failure within d operating hours | running at age)."""
    if d <= 0:
        return 0.0
    return 1.0 - math.exp(-mult * (cum_hazard(age + d, beta, eta) - cum_hazard(age, beta, eta)))


def rul_quantile(age: float, q: float, beta: float, eta: float, mult: float) -> float:
    """Operating hours until the conditional failure probability reaches q."""
    target = -math.log(1.0 - q) / mult + cum_hazard(age, beta, eta)
    return max(0.0, eta * target ** (1.0 / beta) - age)


def hazard_rate(age: float, beta: float, eta: float, mult: float) -> float:
    """Instantaneous failure rate per operating hour."""
    return mult * beta / eta * (max(age, 1e-6) / eta) ** (beta - 1)


def weibull_mean(beta: float, eta: float) -> float:
    return eta * math.gamma(1 + 1 / beta)


def poisson_quantile(lam: float, q: float = 0.9) -> int:
    if lam <= 0:
        return 0
    k, p, cdf = 0, math.exp(-lam), math.exp(-lam)
    while cdf < q and k < 100:
        k += 1
        p *= lam / k
        cdf += p
    return k


def op_hours(a: datetime, b: datetime) -> float:
    return cal.productive_minutes(a, b) / 60 if b > a else 0.0


def _r(x, d=3):
    return None if x is None else round(float(x), d)


# ─── calendar windows ─────────────────────────────────────────────────
def night_window(now: datetime) -> tuple[datetime, datetime]:
    """Next non-production window (23:00-06:00); zero pillows lost."""
    d = now.date()
    if now.time() < cal.DAY_START:
        return now, datetime.combine(d, cal.DAY_START)
    start = datetime.combine(d, cal.DAY_END)
    return max(now, start), datetime.combine(d + timedelta(days=1), cal.DAY_START)


def planned_stops(now: datetime) -> list[tuple[datetime, datetime, str]]:
    """Breaks and lunches remaining today that start after the mobilisation lead time."""
    out = []
    lead = now + timedelta(minutes=COST["mobilise_min"])
    for sh, s in cal.SHIFTS.items():
        for a, b in [*s["breaks"], s["lunch"]]:
            st, en = datetime.combine(now.date(), a), datetime.combine(now.date(), b)
            if st >= lead:
                kind = "lunch" if (a, b) == s["lunch"] else "break"
                out.append((st, en, f"Shift {sh} {kind} {st:%H:%M}"))
    return sorted(out)


# ─── telemetry ────────────────────────────────────────────────────────
METRIC_IDX = {k: i for i, k in enumerate(mh.METRICS)}  # rpm, temperature, vibration, pressure, power


class Series:
    """Telemetry of one machine: timestamps + an (n x 5) array in mh.METRICS order."""
    __slots__ = ("ts", "arr")

    def __init__(self, ts: list, arr):
        self.ts, self.arr = ts, arr

    def __len__(self):
        return len(self.ts)

    def col(self, metric: str):
        return self.arr[:, METRIC_IDX[metric]]

    def running(self) -> "Series":
        if not len(self.ts):
            return self
        mask = np.nan_to_num(self.arr[:, 0]) > 0
        return Series([t for t, keep in zip(self.ts, mask) if keep], self.arr[mask])


EMPTY_SERIES = Series([], np.zeros((0, len(mh.METRICS))))


def anomalies(cond: dict, b: dict, run) -> list[dict]:
    """Vectorised equivalent of machine_health.anomalies (same tolerances and messages):
    rolling z-score of the latest running readings vs the earlier window + deviation from baseline."""
    z_all = None
    n = len(run)
    if n >= 20:
        ref, recent = run[: int(n * 0.6)], run[-6:]
        sd = np.nanstd(ref, axis=0)
        sd[sd == 0] = 1e-6
        z_all = (np.nanmean(recent, axis=0) - np.nanmean(ref, axis=0)) / sd
    out = []
    for i, k in enumerate(mh.METRICS):
        cur = cond.get(k)
        dev = cur / b[k] - 1 if b.get(k) and cur else 0.0
        z = float(z_all[i]) if z_all is not None and not math.isnan(z_all[i]) else None
        tol = TOLERANCE[k]
        if (z is not None and abs(z) > 3 and abs(dev) > tol / 2) or abs(dev) > tol:
            direction = "above" if dev > 0 else "below"
            out.append({"metric": k, "severity": round(abs(dev) / tol, 2), "deviation_pct": round(dev * 100, 1),
                        "z_score": round(z, 1) if z is not None else None,
                        "message": f"{k.capitalize()} {abs(dev) * 100:.1f}% {direction} baseline"
                                   + (f" (z={z:+.1f})" if z is not None else "")})
    return sorted(out, key=lambda a: -a["severity"])


# ─── batch snapshot ───────────────────────────────────────────────────
class Fleet:
    """Batch-loaded snapshot of a factory scope plus every per-machine prediction."""

    def __init__(self, db: Session, factory_ids, now: datetime, machine_ids=None):
        self.db, self.fids, self.now = db, list(factory_ids), now
        self.factories = {f.id: f for f in db.scalars(select(m.Factory))}
        self.lines = {ln.id: ln for ln in db.scalars(select(m.ProductionLine).where(m.ProductionLine.factory_id.in_(self.fids)))}
        q = select(m.Machine).where(m.Machine.factory_id.in_(self.fids))
        if machine_ids:
            q = q.where(m.Machine.id.in_(list(machine_ids)))
        self.machines = list(db.scalars(q.order_by(m.Machine.id)))
        ids = [mc.id for mc in self.machines]
        self.skus = {s.id: s for s in db.scalars(select(m.Sku))}

        T = m.TelemetryPoint
        self.series: dict[int, Series] = {}
        if ids:
            rows = db.execute(select(T.machine_id, T.ts, T.rpm, T.temperature, T.vibration, T.pressure, T.power)
                              .where(T.machine_id.in_(ids), T.ts >= now - timedelta(hours=48), T.ts <= now)
                              .order_by(T.machine_id, T.ts)).all()
            for mid, grp in groupby(rows, key=itemgetter(0)):
                g = list(grp)[-144:]
                self.series[mid] = Series([r[1] for r in g], np.array([tuple(r[2:]) for r in g], dtype=float))

        self.open_wo = {}
        if ids:
            for wo in db.scalars(select(m.WorkOrder).where(m.WorkOrder.machine_id.in_(ids), m.WorkOrder.status != "Completed")
                                 .order_by(m.WorkOrder.created_at)):
                cur = self.open_wo.get(wo.machine_id)
                if cur is None or (wo.status == "In Progress" and cur.status != "In Progress"):
                    self.open_wo[wo.machine_id] = wo

        customers = {c.id: c for c in db.scalars(select(m.Customer))}
        cos = {o.id: o for o in db.scalars(select(m.CustomerOrder).where(m.CustomerOrder.factory_id.in_(self.fids)))}
        self.orders_by_line = defaultdict(list)
        for po in db.scalars(select(m.ProductionOrder).where(m.ProductionOrder.factory_id.in_(self.fids),
                                                            m.ProductionOrder.status != "Completed")):
            if po.line_id is None:
                continue
            co = cos.get(po.customer_order_id)
            sku = self.skus.get(po.sku_id)
            cust = customers.get(co.customer_id) if co else (customers.get(sku.customer_id) if sku else None)
            due = po.required_completion or (co.scheduled_dispatch if co else None)
            self.orders_by_line[po.line_id].append({
                "number": co.number if co else po.number, "po": po.number, "customer": cust.name if cust else None,
                "due": due, "sku": sku.code if sku else None, "remaining": max(0, po.required_qty - po.completed_qty),
                "estimated_completion": po.estimated_completion, "sequence": po.sequence or 0, "status": po.status,
            })
        for lst in self.orders_by_line.values():
            lst.sort(key=lambda o: (o["due"] or datetime.max, o["sequence"]))

        self.techs = list(db.scalars(select(m.Employee).where(m.Employee.factory_id.in_(self.fids),
                                                              m.Employee.role == "Maintenance technician")))
        self.breakdowns = list(db.scalars(select(m.DowntimeEvent).where(m.DowntimeEvent.factory_id.in_(self.fids),
                                                                        m.DowntimeEvent.category == "Mechanical breakdown")))

        # Shared calendar conversions (identical for every machine).
        self._op_cache: dict = {}
        self._oh_cache: dict = {}
        self.mc_by_id = {mc.id: mc for mc in self.machines}
        self.ref = datetime.combine(now.date() - timedelta(days=3), cal.DAY_START)
        self.h24 = op_hours(now, now + timedelta(hours=24))
        self.h72 = op_hours(now, now + timedelta(hours=72))
        self.h7d = op_hours(now, now + timedelta(days=7))
        self.h30d = op_hours(now, now + timedelta(days=30))
        self.night = night_window(now)
        self.stops = planned_stops(now)
        self.predictions = [self._predict(mc) for mc in self.machines]
        self.by_id = {p["machine_id"]: p for p in self.predictions}

    # op-hours since a fixed reference, cached per timestamp (shared by all machines)
    def op_index(self, ts: datetime) -> float:
        v = self._op_cache.get(ts)
        if v is None:
            v = op_hours(self.ref, ts) if ts > self.ref else 0.0
            self._op_cache[ts] = v
        return v

    def oh(self, a: datetime, b: datetime) -> float:
        """Cached operating hours between two instants."""
        key = (a, b)
        v = self._oh_cache.get(key)
        if v is None:
            v = self._oh_cache[key] = op_hours(a, b)
        return v

    def to_calendar(self, op_h: float) -> datetime:
        if op_h <= 30 * OP_HOURS_PER_DAY:
            return cal.add_productive(self.now, op_h * 60)
        return self.now + timedelta(days=op_h / OP_HOURS_PER_DAY)  # far horizon: day-level precision is enough

    def factory_code(self, fid):
        f = self.factories.get(fid)
        return f.code if f else None

    def unit_price(self, line) -> float:
        sku = self.skus.get(line.current_sku_id) if line and line.current_sku_id else None
        return float(sku.unit_price) if sku and sku.unit_price else COST["default_unit_price_usd"]

    # ── per-metric regression over running telemetry ──
    def trend(self, running: Series, metric: str, threshold: float | None, rising: bool = True) -> dict | None:
        ts = running.ts[-72:]
        if len(ts) < 12:
            return None
        x = np.array([self.op_index(t) for t in ts], dtype=float)
        x -= x[0]
        if x[-1] < 1.0:
            return None
        y = running.col(metric)[-72:]
        if np.isnan(y).any():
            return None
        slope, icpt = np.polyfit(x, y, 1)
        fit = slope * x + icpt
        ss_res = float(((y - fit) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum()) or 1e-9
        sxx = float(((x - x.mean()) ** 2).sum()) or 1e-9
        se = math.sqrt(ss_res / max(1, len(x) - 2) / sxx)
        now_fit = float(fit[-1])
        hours = None
        if threshold is not None:
            sig = abs(slope) > 3 * se
            if rising and ((now_fit >= threshold) or (slope > 0 and sig)):
                hours = 0.0 if now_fit >= threshold else (threshold - now_fit) / slope
            elif not rising and ((now_fit <= threshold) or (slope < 0 and sig)):
                hours = 0.0 if now_fit <= threshold else (threshold - now_fit) / slope
        return {"slope": float(slope), "se": se, "r2": max(0.0, 1 - ss_res / ss_tot), "now": now_fit,
                "threshold": threshold, "hours_to_threshold": hours, "n": len(ts),
                "last_ts": ts[-1], "x_last": float(x[-1])}

    # ── the per-machine model ──
    def _predict(self, mc: m.Machine) -> dict:
        line = self.lines.get(mc.line_id)
        b = mc.baseline or mh.BASELINES.get(mc.machine_type, {})
        running = self.series.get(mc.id, EMPTY_SERIES).running()
        wb = _wb(mc.machine_type)
        beta, eta = wb["beta"], wb["eta"]
        base = {
            "machine_id": mc.id, "code": mc.code, "name": mc.name, "type": mc.machine_type, "stage": mc.stage,
            "factory": self.factory_code(mc.factory_id), "factory_id": mc.factory_id,
            "line_id": mc.line_id, "line": line.name if line else None, "line_status": line.status if line else None,
            "status": mc.status, "health_score": _r(mc.health_score, 1), "health": mh.classify(mc.health_score or 0),
            "age_hours": _r(mc.hours_since_maintenance, 1), "weibull": {"beta": beta, "eta": eta},
        }
        wo = self.open_wo.get(mc.id)
        base["work_order"] = ({"id": wo.id, "number": wo.number, "status": wo.status, "type": wo.wo_type,
                               "scheduled_start": iso(wo.scheduled_start), "scheduled_end": iso(wo.scheduled_end)}
                              if wo else None)
        if mc.status in ("Breakdown", "Maintenance"):
            return base | {"available": False, "severity": None, "unavailable_until": iso(mc.unavailable_until),
                           "failure_prob_24h": None, "failure_prob_72h": None, "failure_prob_7d": None,
                           "failure_prob_30d": None, "rul_hours": None, "rul_low_hours": None, "rul_high_hours": None,
                           "method": None, "failure_mode": None, "impact": None, "recommended": None,
                           "loss_avoided_usd": 0.0, "confidence": None}

        # Condition: the last few running readings (machines on an idle line read ~0).
        recent = running.arr[-6:]
        if len(recent):
            cond = dict(zip(mh.METRICS, (float(v) for v in np.nanmean(recent, axis=0))))
        elif mc.rpm:
            cond = {k: float(getattr(mc, k) or b.get(k, 0)) for k in mh.METRICS}
        else:
            cond = {k: float(b.get(k, 0)) for k in mh.METRICS}
        ratio = {k: (cond[k] / b[k] if b.get(k) else 1.0) for k in mh.METRICS}
        dev = {k: ratio[k] - 1 for k in mh.METRICS}
        anoms = anomalies(cond, b, running.arr)
        health = float(mc.health_score if mc.health_score is not None else 95)
        wear = mh.wear(mc)

        x = {"vibration": max(0.0, ratio["vibration"] - 1.05), "temperature": max(0.0, ratio["temperature"] - 1.02),
             "health": max(0.0, 85 - health) / 15, "wear": wear}
        log_m = min(MAX_LOG_MULTIPLIER, sum(COVARIATES[k]["gamma"] * x[k] for k in x))
        mult_w = math.exp(log_m)
        age = float(mc.hours_since_maintenance or 0.0)

        thr = b.get("vibration", 1) * mh.VIBRATION_ALARM
        vib_trend = self.trend(running, "vibration", thr, rising=True)
        trend_h = vib_trend["hours_to_threshold"] if vib_trend else None
        p50_w = rul_quantile(age, 0.5, beta, eta, mult_w)
        mult, method = mult_w, "Weibull proportional hazards"
        if trend_h is not None and trend_h < p50_w:
            method = "Vibration trend to alarm threshold"
            d = max(trend_h, 0.25)
            dh = cum_hazard(age + d, beta, eta) - cum_hazard(age, beta, eta)
            mult = max(mult_w, math.log(2) / dh) if dh > 0 else mult_w
        P = lambda d: p_fail(age, d, beta, eta, mult)  # noqa: E731
        rul50 = rul_quantile(age, 0.5, beta, eta, mult)
        rul10 = rul_quantile(age, 0.1, beta, eta, mult)
        rul90 = rul_quantile(age, 0.9, beta, eta, mult)
        p24, p72, p7, p30 = P(self.h24), P(self.h72), P(self.h7d), P(self.h30d)

        # Failure mode and repair.
        mode_key, mode_reason, explicit = _failure_mode(mc.machine_type, dev, anoms)
        rep = REPAIRS[mode_key]
        unplanned_mttr = rep["planned_mttr_h"] * COST["unplanned_mttr_factor"] + COST["response_h"]

        # Impact if it runs to failure.
        rate = float(line.planned_rate or 0) if line else 0.0
        price = self.unit_price(line)
        margin = price * COST["contribution_margin_pct"]
        rtf_pillows = rate * unplanned_mttr
        rtf_cost = (rtf_pillows * margin + rep["part_cost"] * COST["unplanned_parts_multiplier"]
                    + rep["crew"] * unplanned_mttr * COST["technician_rate_usd_h"])
        orders = []
        for o in self.orders_by_line.get(mc.line_id, []):
            late = None
            if o["due"] and o["estimated_completion"]:
                slack = (self.oh(o["estimated_completion"], o["due"]) if o["due"] > o["estimated_completion"]
                         else -self.oh(o["due"], o["estimated_completion"]))
                late = slack < unplanned_mttr
            orders.append({"number": o["number"], "po": o["po"], "customer": o["customer"], "due": iso(o["due"]),
                           "sku": o["sku"], "remaining": o["remaining"], "late_if_failure": late})
        impact = {"pillows_at_risk": int(round(rtf_pillows)), "downtime_hours": round(unplanned_mttr, 2),
                  "cost_usd": round(rtf_cost), "orders": orders[:5], "orders_count": len(orders),
                  "orders_late_if_failure": sum(1 for o in orders if o["late_if_failure"]),
                  "line_rate_per_h": round(rate), "unit_price_usd": price}

        # Maintenance window options (expected loss of each).
        def planned_cost(start, end, after_hours):
            lost = rate * self.oh(start, end)
            labour = rep["crew"] * rep["planned_mttr_h"] * COST["technician_rate_usd_h"]
            labour *= COST["after_hours_multiplier"] if after_hours else 1.0
            return lost, lost * margin + rep["part_cost"] + labour

        def option(key, label, start, after_hours=False):
            end = start + timedelta(hours=rep["planned_mttr_h"])
            pb = P(self.oh(self.now, start))
            lost, cost = planned_cost(start, end, after_hours)
            return {"key": key, "label": label, "start": start, "end": end, "p_fail_before": pb,
                    "pillows_lost": lost, "expected_loss_usd": pb * rtf_cost + (1 - pb) * cost, "planned_cost_usd": cost}

        opts = [option("now", "Stop now", self.now)]
        stop_opts = [option("stop", f"Planned stop · {lbl}", s) for s, _e, lbl in self.stops]
        if stop_opts:
            opts.append(stop_opts[0])
            best_stop = min(stop_opts, key=lambda o: o["expected_loss_usd"])
            if best_stop is not stop_opts[0]:
                best_stop = dict(best_stop, key="best_stop")
                opts.append(best_stop)
        ns, ne = self.night
        opts.append(option("night", "Non-production window (23:00–06:00)" if ns.time() == cal.DAY_END
                           else "Non-production window", ns, after_hours=True))
        best = min(opts, key=lambda o: o["expected_loss_usd"])
        rtf_expected = p7 * rtf_cost
        intervene = best["expected_loss_usd"] < rtf_expected
        for o in opts:
            o["recommended"] = intervene and o is best
        options = [{"key": "rtf", "label": "Run to failure (7-day horizon)", "start": None, "end": None,
                    "p_fail_before": _r(p7), "pillows_lost": round(p7 * rtf_pillows),
                    "expected_loss_usd": round(rtf_expected), "planned_cost_usd": None, "recommended": not intervene}]
        options += [{**o, "start": iso(o["start"]), "end": iso(o["end"]), "p_fail_before": _r(o["p_fail_before"]),
                     "pillows_lost": round(o["pillows_lost"]), "expected_loss_usd": round(o["expected_loss_usd"]),
                     "planned_cost_usd": round(o["planned_cost_usd"])} for o in opts]
        rec = next((o for o in options if o["recommended"] and o["key"] != "rtf"), None)
        best_public = next(o for o in options if o["key"] == best["key"])
        window_src = rec or best_public
        window = {"label": window_src["label"], "start": window_src["start"], "end": window_src["end"]}

        # Severity.
        if p24 >= 0.25 or p7 >= 0.6 or rul50 <= OP_HOURS_PER_DAY:
            sev = "critical"
        elif p7 >= 0.10 or rul50 <= 5 * OP_HOURS_PER_DAY:
            sev = "warning"
        elif p7 >= 0.05 or (anoms and health < 85):
            sev = "info"
        else:
            sev = None
        p_before_wo = None
        if wo:
            start = wo.started_at if wo.status == "In Progress" else (wo.scheduled_start or self.now)
            p_before_wo = P(self.oh(self.now, max(start, self.now)))
            if sev:
                sev = "warning" if p_before_wo >= 0.3 else "info"

        # Confidence: data sufficiency, agreement of the two methods, clarity of the signature.
        data_q = min(1.0, len(running) / 60)
        agree = 0.5
        if trend_h is not None and trend_h > 0:
            agree = max(0.0, 1 - abs(math.log(max(trend_h, 0.5) / max(p50_w, 0.5))) / math.log(6))
        conf = round(min(0.95, 0.35 + 0.3 * data_q + 0.15 * agree + (0.15 if explicit else 0.05)), 2)

        covs = [{"key": k, "label": COVARIATES[k]["label"], "x": round(x[k], 3), "gamma": COVARIATES[k]["gamma"],
                 "factor": round(math.exp(COVARIATES[k]["gamma"] * x[k]), 2), "definition": COVARIATES[k]["x"]} for k in x]
        rul_at = self.to_calendar(rul50) if rul50 < 5000 else None
        evidence = _evidence(mc, b, cond, ratio, health, wear, anoms, vib_trend, thr, beta, eta, age, mult, mult_w,
                             method, line, rate, unplanned_mttr, orders, mode_reason)
        return base | {
            "available": True, "severity": sev, "confidence": conf,
            "failure_prob_24h": _r(p24), "failure_prob_72h": _r(p72), "failure_prob_7d": _r(p7), "failure_prob_30d": _r(p30),
            "rul_hours": _r(rul50, 1), "rul_low_hours": _r(rul10, 1), "rul_high_hours": _r(rul90, 1),
            "expected_failure_at": iso(rul_at), "method": method,
            "weibull_rul_hours": _r(p50_w, 1), "trend_hours_to_alarm": _r(trend_h, 1),
            "hazard_multiplier": round(mult, 2), "condition_multiplier": round(mult_w, 2), "covariates": covs,
            "hazard_per_op_hour": _r(hazard_rate(age, beta, eta, mult), 5),
            "condition": {k: round(cond[k], 2) for k in cond}, "deviation_pct": {k: round(dev[k] * 100, 1) for k in dev},
            "wear": round(wear, 3), "anomalies": anoms, "running_points": len(running),
            "vibration_trend": ({"slope_per_op_h": round(vib_trend["slope"], 4), "r2": round(vib_trend["r2"], 2),
                                 "threshold": round(thr, 2), "hours_to_alarm": _r(trend_h, 1)} if vib_trend else None),
            "failure_mode": rep["mode"], "failure_mode_key": mode_key, "mode_reason": mode_reason,
            "repair": {**rep, "unplanned_mttr_h": round(unplanned_mttr, 2)},
            "impact": impact, "options": options, "recommended": rec, "window": window,
            "rtf_expected_loss_usd": round(rtf_expected), "rtf_cost_usd": round(rtf_cost),
            "loss_avoided_usd": round(max(0.0, rtf_expected - best["expected_loss_usd"])) if intervene else 0,
            "p_fail_before_work_order": _r(p_before_wo), "evidence": evidence,
            "_model": {"age": age, "beta": beta, "eta": eta, "mult": mult},
        }

    def survival(self, p: dict, op_h: float, plan_at: float | None = None) -> float:
        """Survival over op_h operating hours from now; with plan_at, the machine is restored
        (age 0, baseline hazard) after plan_at operating hours."""
        md = p["_model"]
        age, beta, eta, mult = md["age"], md["beta"], md["eta"], md["mult"]
        if plan_at is None or op_h <= plan_at:
            return math.exp(-mult * (cum_hazard(age + op_h, beta, eta) - cum_hazard(age, beta, eta)))
        s_w = math.exp(-mult * (cum_hazard(age + plan_at, beta, eta) - cum_hazard(age, beta, eta)))
        return s_w * math.exp(-cum_hazard(op_h - plan_at, beta, eta))

    def plan_offset(self, p: dict) -> float | None:
        """Operating hours from now until the machine's planned (WO) or recommended repair."""
        wo = p.get("work_order")
        start = None
        if wo and wo.get("scheduled_start"):
            start = datetime.fromisoformat(wo["scheduled_start"])
        elif p.get("recommended") and p["recommended"].get("start"):
            start = datetime.fromisoformat(p["recommended"]["start"])
        if start is None:
            return None
        return self.oh(self.now, max(start, self.now))

    @cached_property
    def available(self) -> list[dict]:
        return [p for p in self.predictions if p["available"]]


def _failure_mode(mtype: str, dev: dict, anoms: list[dict]) -> tuple[str, str, bool]:
    """Repair key, human reason and whether a clear signature drove it."""
    sev = {k: abs(dev[k]) / TOLERANCE[k] for k in TOLERANCE}
    flagged = {a["metric"] for a in anoms} | {k for k, s in sev.items() if s >= 1}
    if mtype == "Filling Head" and dev["pressure"] <= -0.02:
        return "seal", f"fill pressure {dev['pressure'] * 100:+.1f}% vs baseline", True
    heat = 0.03 if mtype == "Garnett Main Motor" else 0.08
    if mtype in ("Garnett Main Motor", "Blow Fan Motor") and "vibration" in flagged and dev["temperature"] >= heat:
        return "bearing", (f"vibration {dev['vibration'] * 100:+.0f}% and temperature {dev['temperature'] * 100:+.1f}% "
                           "rising together (bearing signature)"), True
    signals = [k for k in sorted(sev, key=lambda k: -sev[k]) if k in flagged]
    if signals:
        top = signals[0]
        if top == "vibration":
            return VIBRATION_MODE.get(mtype, "bearing"), f"vibration {dev['vibration'] * 100:+.0f}% vs baseline", True
        if top == "rpm":
            return "belt", f"speed drift {dev['rpm'] * 100:+.1f}% (slip signature)", True
        if top == "power" and dev["power"] > 0:
            return ("card" if mtype == "Garnett Main Motor" else "overload"), f"power draw {dev['power'] * 100:+.1f}%", True
        if top == "temperature" and dev["temperature"] > 0:
            return "overheat", f"temperature {dev['temperature'] * 100:+.1f}% without vibration rise", True
        if top == "pressure":
            return ("seal" if mtype == "Filling Head" else "air"), f"pressure {dev['pressure'] * 100:+.1f}%", True
    key = DEFAULT_MODE.get(mtype, "bearing")
    return key, f"no dominant anomaly; most common failure mode for a {mtype}", False


def _evidence(mc, b, cond, ratio, health, wear, anoms, vt, thr, beta, eta, age, mult, mult_w, method, line,
              rate, unplanned_mttr, orders, mode_reason) -> list[str]:
    ev = []
    ev.append(f"Vibration {cond['vibration']:.2f} mm/s = {ratio['vibration']:.2f}× baseline {b.get('vibration')} "
              f"(alarm {thr:.2f} mm/s at {mh.VIBRATION_ALARM}×)")
    if abs(ratio["temperature"] - 1) >= 0.02:
        ev.append(f"Temperature {cond['temperature']:.1f} °C, {(ratio['temperature'] - 1) * 100:+.1f}% vs baseline {b.get('temperature')} °C")
    for a in anoms[:3]:
        if a["metric"] not in ("vibration", "temperature"):
            ev.append(a["message"])
    ev.append(f"Health score {health:.0f}/100; wear index {wear:.2f}; {age:,.0f} operating h since last service")
    if vt:
        if vt["hours_to_threshold"] is not None:
            ev.append(f"Vibration trend {vt['slope']:+.4f} mm/s per operating h (R² {vt['r2']:.2f}, n={vt['n']}) → "
                      f"alarm threshold in ≈{vt['hours_to_threshold']:.0f} op h")
        else:
            ev.append(f"Vibration trend {vt['slope']:+.4f} mm/s per operating h (R² {vt['r2']:.2f}) — no significant rise")
    ev.append(f"Weibull β={beta}, η={eta:,} op h for {mc.machine_type}; condition hazard multiplier ×{mult_w:,.1f}"
              + (f" (raised to ×{mult:,.1f} to match the vibration trend)" if mult > mult_w * 1.001 else ""))
    ev.append(f"Failure mode: {mode_reason}; RUL driven by {method.lower()}")
    if line:
        ev.append(f"{line.name} plans {rate:,.0f} pillows/h; an unplanned stop is estimated at {unplanned_mttr:.1f} h")
    if orders:
        o = orders[0]
        due = f" due {datetime.fromisoformat(o['due']):%a %b %d %H:%M}" if o["due"] else ""
        ev.append(f"{len(orders)} open order(s) on the line; next: {o['number']} ({o['customer']}){due}")
    return ev


# ─── public helpers ───────────────────────────────────────────────────
PUBLIC_DROP = {"_model"}


COMPACT_KEYS = ("machine_id", "code", "name", "type", "stage", "factory", "factory_id", "line_id", "line", "status",
                "available", "health_score", "health", "age_hours", "severity", "confidence", "failure_prob_24h",
                "failure_prob_72h", "failure_prob_7d", "failure_prob_30d", "rul_hours", "rul_low_hours", "rul_high_hours",
                "expected_failure_at", "method", "failure_mode", "work_order", "window", "loss_avoided_usd",
                "rtf_expected_loss_usd", "unavailable_until")


def public(p: dict, compact: bool = False) -> dict:
    if not compact:
        return {k: v for k, v in p.items() if k not in PUBLIC_DROP}
    row = {k: p.get(k) for k in COMPACT_KEYS}
    imp = p.get("impact")
    row["impact"] = ({k: imp[k] for k in ("pillows_at_risk", "downtime_hours", "cost_usd", "orders_count",
                                          "orders_late_if_failure")} if imp else None)
    row["recommended"] = bool(p.get("recommended"))
    return row


def prediction_summary(p: dict) -> dict | None:
    if not p.get("available"):
        return None
    return {"failure_prob_24h": p["failure_prob_24h"], "failure_prob_72h": p["failure_prob_72h"],
            "failure_prob_7d": p["failure_prob_7d"], "rul_hours": p["rul_hours"], "rul_low_hours": p["rul_low_hours"],
            "rul_high_hours": p["rul_high_hours"], "failure_mode": p["failure_mode"], "method": p["method"],
            "expected_failure_at": p["expected_failure_at"], "health_score": p["health_score"]}


def machine_ref(p: dict) -> dict:
    return {"id": p["machine_id"], "code": p["code"], "name": p["name"], "line_id": p["line_id"], "line": p["line"]}


# ─── fleet analytics ──────────────────────────────────────────────────
def forecast(fleet: Fleet, days: int = 14) -> list[dict]:
    """Expected failures / downtime per calendar day, as-is and with the planned + recommended jobs."""
    now = fleet.now
    bounds = [now] + [datetime.combine(now.date() + timedelta(days=k + 1), cal.DAY_START) for k in range(days)]
    offs = [op_hours(now, t) for t in bounds]
    out = []
    plans = {p["machine_id"]: fleet.plan_offset(p) for p in fleet.available}
    for k in range(days):
        a, z = offs[k], offs[k + 1]
        f_as = f_plan = dt_as = dt_plan = pillows = 0.0
        for p in fleet.available:
            mttr = p["repair"]["unplanned_mttr_h"]
            d_as = fleet.survival(p, a) - fleet.survival(p, z)
            pl = plans[p["machine_id"]]
            d_pl = fleet.survival(p, a, pl) - fleet.survival(p, z, pl) if pl is not None else d_as
            f_as += d_as
            f_plan += d_pl
            dt_as += d_as * mttr
            dt_plan += d_pl * mttr
            pillows += d_as * p["impact"]["pillows_at_risk"]
        day = bounds[k].date() if k else now.date()
        out.append({"date": day.isoformat(), "label": f"{day:%a %d}", "op_hours": round(z - a, 1),
                    "expected_failures": round(f_as, 3), "expected_failures_planned": round(f_plan, 3),
                    "expected_downtime_h": round(dt_as, 2), "expected_downtime_planned_h": round(dt_plan, 2),
                    "expected_pillows_lost": round(pillows)})
    return out


def reliability(fleet: Fleet) -> list[dict]:
    st = state(fleet.db)
    start = datetime.combine(st.seed_date, cal.DAY_START) if st and st.seed_date else fleet.now - timedelta(days=1)
    obs_per_machine = op_hours(start, fleet.now)
    types = defaultdict(list)
    for mc in fleet.machines:
        types[mc.machine_type].append(mc)
    mtype_of = {mc.id: mc.machine_type for mc in fleet.machines}
    events = defaultdict(list)
    for e in fleet.breakdowns:
        if e.machine_id in mtype_of:
            events[mtype_of[e.machine_id]].append(e)
    out = []
    for t, mcs in sorted(types.items()):
        wb = _wb(t)
        rep = REPAIRS[DEFAULT_MODE.get(t, "bearing")]
        ev = events.get(t, [])
        durs = [((e.end or fleet.now) - e.start).total_seconds() / 3600 for e in ev if e.start]
        obs = obs_per_machine * len(mcs)
        out.append({
            "type": t, "machines": len(mcs), "beta": wb["beta"], "eta": wb["eta"], "dominant_failure": wb["dominant"],
            "model_mtbf_h": round(weibull_mean(wb["beta"], wb["eta"])),
            "model_mttr_planned_h": rep["planned_mttr_h"],
            "model_mttr_unplanned_h": round(rep["planned_mttr_h"] * COST["unplanned_mttr_factor"] + COST["response_h"], 2),
            "observed_failures": len(ev), "observed_op_hours": round(obs),
            "observed_mtbf_h": round(obs / len(ev)) if ev else None,
            "observed_mttr_h": round(sum(durs) / len(durs), 2) if durs else None,
            "observed_since": iso(start),
        })
    return out


def maintenance_plan(fleet: Fleet) -> dict:
    jobs = []
    for p in fleet.available:
        wo = p.get("work_order")
        if wo:
            if not p["severity"]:
                continue
            jobs.append({"machine": machine_ref(p), "factory": p["factory"], "advisory_id": f"pm:{p['code']}",
                         "status": f"{wo['number']} {wo['status'].lower()}", "work_order": wo,
                         "window": {"label": "Work order", "start": wo["scheduled_start"], "end": wo["scheduled_end"]},
                         "crew": p["repair"]["crew"], "hours": p["repair"]["planned_mttr_h"],
                         "technician_hours": p["repair"]["crew"] * p["repair"]["planned_mttr_h"],
                         "part": p["repair"]["part"], "failure_mode": p["failure_mode"], "severity": p["severity"],
                         "failure_prob_7d": p["failure_prob_7d"], "loss_avoided_usd": p["loss_avoided_usd"]})
        elif p["severity"] in ("critical", "warning") and p["recommended"]:
            r = p["recommended"]
            jobs.append({"machine": machine_ref(p), "factory": p["factory"], "advisory_id": f"pm:{p['code']}",
                         "status": "Recommended", "work_order": None,
                         "window": {"label": r["label"], "start": r["start"], "end": r["end"]},
                         "crew": p["repair"]["crew"], "hours": p["repair"]["planned_mttr_h"],
                         "technician_hours": p["repair"]["crew"] * p["repair"]["planned_mttr_h"],
                         "part": p["repair"]["part"], "failure_mode": p["failure_mode"], "severity": p["severity"],
                         "failure_prob_7d": p["failure_prob_7d"], "loss_avoided_usd": p["loss_avoided_usd"],
                         "expected_loss_usd": r["expected_loss_usd"], "rtf_expected_loss_usd": p["rtf_expected_loss_usd"]})
    jobs.sort(key=lambda j: (j["window"]["start"] or "9999", SEVERITY_RANK.get(j["severity"], 3)))
    return {"jobs": jobs, "loss_avoided_usd": sum(j["loss_avoided_usd"] for j in jobs if j["status"] == "Recommended"),
            "technician_hours": round(sum(j["technician_hours"] for j in jobs), 1)}


def technician_load(fleet: Fleet, plan: dict, days: int = 7) -> dict:
    """Maintenance technician demand vs capacity: today per shift, and per day for a week."""
    now = fleet.now
    share, paid = COST["technician_available_share"], COST["paid_hours_per_shift"]
    by_f = defaultdict(lambda: {1: [], 2: []})
    for e in fleet.techs:
        if e.shift in (1, 2):
            by_f[e.factory_id][e.shift].append(e)
    shifts = []
    for fid in fleet.fids:
        code = fleet.factory_code(fid)
        for sh in (1, 2):
            lo, hi = cal.shift_bounds(now.date(), sh)
            emps = by_f[fid][sh]
            present = [e for e in emps if e.attendance in ("Present", "Late")]
            cap = len(present) * paid * share
            planned = sum(j["technician_hours"] for j in plan["jobs"] if j["factory"] == code and j["window"]["start"]
                          and lo <= datetime.fromisoformat(j["window"]["start"]) < hi)
            a, z = max(now, lo), hi
            reactive = 0.0
            if z > a:
                oa, oz = op_hours(now, a), op_hours(now, z)
                for p in fleet.available:
                    if p["factory_id"] == fid:
                        reactive += ((fleet.survival(p, oa) - fleet.survival(p, oz))
                                     * p["repair"]["unplanned_mttr_h"] * p["repair"]["crew"])
            demand = planned + reactive
            shifts.append({"factory": code, "factory_id": fid, "shift": sh, "rostered": len(emps), "present": len(present),
                           "capacity_h": round(cap, 1), "planned_h": round(planned, 1), "reactive_expected_h": round(reactive, 2),
                           "demand_h": round(demand, 1), "load_pct": round(100 * demand / cap, 1) if cap else None,
                           "past": hi <= now})
    ns, ne = fleet.night
    after_hours = [j for j in plan["jobs"] if j["window"]["start"] and ns <= datetime.fromisoformat(j["window"]["start"]) < ne]
    # Week view (all factories in scope).
    rostered = len([e for e in fleet.techs if e.shift in (1, 2)])
    day_cap = rostered * paid * share
    week = []
    bounds = [now] + [datetime.combine(now.date() + timedelta(days=k + 1), cal.DAY_START) for k in range(days)]
    offs = [op_hours(now, t) for t in bounds]
    for k in range(days):
        day = now.date() + timedelta(days=k)
        lo = datetime.combine(day, cal.DAY_START)
        hi = datetime.combine(day, cal.DAY_END)
        planned = night = 0.0
        for j in plan["jobs"]:
            st = j["window"]["start"] and datetime.fromisoformat(j["window"]["start"])
            if not st:
                continue
            if lo <= st < hi:
                planned += j["technician_hours"]
            elif hi <= st < datetime.combine(day + timedelta(days=1), cal.DAY_START):
                night += j["technician_hours"]
        reactive = sum((fleet.survival(p, offs[k]) - fleet.survival(p, offs[k + 1], None))
                       * p["repair"]["unplanned_mttr_h"] * p["repair"]["crew"] for p in fleet.available)
        week.append({"date": day.isoformat(), "label": f"{day:%a %d}", "capacity_h": round(day_cap, 1),
                     "planned_h": round(planned, 1), "after_hours_h": round(night, 1), "reactive_expected_h": round(reactive, 2),
                     "load_pct": round(100 * (planned + reactive) / day_cap, 1) if day_cap else None})
    current = [s for s in shifts if not s["past"]]
    peak = max((s["load_pct"] or 0 for s in current), default=0)
    return {"shifts": shifts, "week": week, "after_hours_tonight": {
        "start": iso(ns), "end": iso(ne), "jobs": len(after_hours),
        "technician_hours": round(sum(j["technician_hours"] for j in after_hours), 1),
        "crew_needed": max((j["crew"] for j in after_hours), default=0)},
        "peak_load_pct": peak, "assumption": (f"Capacity = present technicians x {paid:.0f} paid h x {int(share * 100)}% "
                                              "available for corrective/predictive work (illustrative).")}


def spares(fleet: Fleet) -> list[dict]:
    """Expected spare-part demand over the next 30 days from Weibull probabilities (+ planned jobs)."""
    parts = {}
    for p in fleet.available:
        rep = p["repair"]
        key = rep["part"]
        row = parts.setdefault(key, {"part": key, "unit_cost_usd": rep["part_cost"], "planned_qty": 0, "unplanned_expected": 0.0,
                                     "machines": [], "factories": set()})
        planned = bool(p.get("work_order")) or (p["severity"] in ("critical", "warning") and p["recommended"])
        if planned:
            row["planned_qty"] += 1
            row["machines"].append({"code": p["code"], "p": 1.0, "planned": True})
            row["factories"].add(p["factory"])
        else:
            pr = p["failure_prob_30d"] or 0.0
            row["unplanned_expected"] += pr
            if pr >= 0.02:
                row["machines"].append({"code": p["code"], "p": round(pr, 3), "planned": False})
                row["factories"].add(p["factory"])
    out = []
    for row in parts.values():
        lam = row["unplanned_expected"]
        exp_qty = row["planned_qty"] + lam
        stock = row["planned_qty"] + poisson_quantile(lam, 0.9)
        out.append({"part": row["part"], "unit_cost_usd": row["unit_cost_usd"], "planned_qty": row["planned_qty"],
                    "unplanned_expected": round(lam, 2), "expected_qty": round(exp_qty, 2),
                    "recommended_stock_p90": stock, "value_usd": round(stock * row["unit_cost_usd"]),
                    "machines": sorted(row["machines"], key=lambda x: -x["p"])[:6], "factories": sorted(row["factories"])})
    return sorted(out, key=lambda r: -r["expected_qty"])


def fill_weight(fleet: Fleet) -> list[dict]:
    """Under-fill / fill-weight drift risk from Filling Head pressure level and trend."""
    out = []
    limit = COST["underfill_limit_pct"] / 100
    for p in fleet.available:
        if p["type"] != "Filling Head":
            continue
        mc = fleet.mc_by_id[p["machine_id"]]
        b = mc.baseline or {}
        running = fleet.series.get(mc.id, EMPTY_SERIES).running()
        thr = b.get("pressure", 6.5) * (1 + limit)
        tr = fleet.trend(running, "pressure", thr, rising=False)
        dev = p["deviation_pct"]["pressure"] / 100
        slope_rel = (tr["slope"] / b["pressure"]) if tr and b.get("pressure") else 0.0
        proj24 = dev + slope_rel * fleet.h24
        hours = tr["hours_to_threshold"] if tr else None
        line = fleet.lines.get(mc.line_id)
        sku = fleet.skus.get(line.current_sku_id) if line and line.current_sku_id else None
        fill_g = (sku.fill_kg if sku else 0.7) * 1000
        drift_g = fill_g * dev * COST["fill_weight_sensitivity"]
        if dev <= limit * 0.75 or (hours is not None and hours <= OP_HOURS_PER_DAY):
            risk = "High"
        elif dev <= limit * 0.5 or proj24 <= limit * 0.75 or (hours is not None and hours <= 5 * OP_HOURS_PER_DAY):
            risk = "Medium"
        else:
            risk = "Low"
        out.append({"machine": machine_ref(p), "factory": p["factory"], "factory_id": p["factory_id"],
                    "sku": sku.code if sku else None, "fill_g": round(fill_g),
                    "pressure": p["condition"]["pressure"], "baseline": b.get("pressure"),
                    "deviation_pct": round(dev * 100, 2), "projected_24h_pct": round(proj24 * 100, 2),
                    "slope_pct_per_op_h": round(slope_rel * 100, 4), "hours_to_limit": _r(hours, 1),
                    "limit_pct": COST["underfill_limit_pct"], "est_fill_drift_g": round(drift_g, 1),
                    "risk": risk, "line_rate_per_h": p["impact"]["line_rate_per_h"]})
    return sorted(out, key=lambda r: ({"High": 0, "Medium": 1, "Low": 2}[r["risk"]], r["deviation_pct"]))


def energy(fleet: Fleet) -> dict:
    rows = []
    for p in fleet.available:
        mc = fleet.mc_by_id[p["machine_id"]]
        base_kw = (mc.baseline or {}).get("power")
        kw = p["condition"]["power"]
        if not base_kw or not kw:
            continue
        ratio = kw / base_kw
        excess = max(0.0, kw - base_kw * 1.03)  # +3% is normal variation
        kwh_day = excess * OP_HOURS_PER_DAY
        rows.append({"machine": machine_ref(p), "factory": p["factory"], "power_kw": round(kw, 2), "baseline_kw": base_kw,
                     "deviation_pct": round((ratio - 1) * 100, 1), "excess_kw": round(excess, 2),
                     "excess_kwh_day": round(kwh_day, 1), "cost_30d_usd": round(kwh_day * 30 * COST["energy_usd_kwh"], 2),
                     "anomaly": ratio - 1 > TOLERANCE["power"]})
    flagged = sorted([r for r in rows if r["excess_kw"] > 0], key=lambda r: -r["excess_kw"])
    return {"machines": flagged[:10], "flagged": len(flagged), "anomalies": sum(1 for r in flagged if r["anomaly"]),
            "excess_kwh_day": round(sum(r["excess_kwh_day"] for r in flagged), 1),
            "cost_30d_usd": round(sum(r["cost_30d_usd"] for r in flagged), 2), "tariff_usd_kwh": COST["energy_usd_kwh"]}


def fleet_view(db: Session, fids, now: datetime) -> dict:
    fleet = Fleet(db, fids, now)
    av = fleet.available
    plan = maintenance_plan(fleet)
    techs = technician_load(fleet, plan)
    fc = forecast(fleet)
    at_risk = [p for p in av if (p["failure_prob_7d"] or 0) >= 0.10]
    sev_counts = {s: sum(1 for p in av if p["severity"] == s) for s in SEVERITY_RANK}
    exp7 = sum(p["failure_prob_7d"] or 0 for p in av)
    exp7_plan = sum(1 - fleet.survival(p, fleet.h7d, fleet.plan_offset(p)) for p in av)
    risk_matrix = [{"machine_id": p["machine_id"], "code": p["code"], "name": p["name"], "factory": p["factory"],
                    "line": p["line"], "p7d": p["failure_prob_7d"], "pillows_at_risk": p["impact"]["pillows_at_risk"],
                    "downtime_hours": p["impact"]["downtime_hours"], "cost_usd": p["impact"]["cost_usd"],
                    "severity": p["severity"], "failure_mode": p["failure_mode"],
                    "scheduled": bool(p.get("work_order"))} for p in av]
    lowest = sorted(av, key=lambda p: p["rul_hours"])[:10]
    kpis = {
        "machines": len(fleet.machines), "available": len(av), "unavailable": len(fleet.machines) - len(av),
        "at_risk_7d": len(at_risk), "critical": sev_counts["critical"], "warning": sev_counts["warning"],
        "info": sev_counts["info"],
        "expected_failures_7d": round(exp7, 2), "expected_failures_7d_planned": round(exp7_plan, 2),
        "pillows_at_risk": sum(p["impact"]["pillows_at_risk"] for p in at_risk),
        "expected_pillows_lost_7d": round(sum((p["failure_prob_7d"] or 0) * p["impact"]["pillows_at_risk"] for p in av)),
        "loss_avoidable_usd": plan["loss_avoided_usd"],
        "recommended_jobs": sum(1 for j in plan["jobs"] if j["status"] == "Recommended"),
        "scheduled_jobs": sum(1 for j in plan["jobs"] if j["work_order"]),
        "technician_peak_load_pct": techs["peak_load_pct"],
        "technician_hours_planned": plan["technician_hours"],
    }
    return {
        "sim_time": iso(now), "disclaimer": DISCLAIMER, "kpis": kpis,
        "machines": sorted([public(p, compact=True) for p in fleet.predictions],
                           key=lambda p: (0 if p["available"] else 1, -(p["failure_prob_7d"] or 0))),
        "fleet": {
            "risk_matrix": risk_matrix, "forecast": fc,
            "lowest_rul": [{**machine_ref(p), "factory": p["factory"], "rul_hours": p["rul_hours"],
                            "rul_low_hours": p["rul_low_hours"], "rul_high_hours": p["rul_high_hours"],
                            "expected_failure_at": p["expected_failure_at"], "method": p["method"],
                            "severity": p["severity"], "failure_mode": p["failure_mode"],
                            "scheduled": bool(p.get("work_order"))} for p in lowest],
            "reliability": reliability(fleet), "plan": plan,
            "unavailable": [{**machine_ref(p), "factory": p["factory"], "status": p["status"],
                             "unavailable_until": p.get("unavailable_until"), "work_order": p.get("work_order")}
                            for p in fleet.predictions if not p["available"]],
        },
        "related": {"fill_weight": fill_weight(fleet), "energy": energy(fleet), "spares": spares(fleet),
                    "technicians": techs},
        "model": model_card(),
    }


def model_card() -> dict:
    return {
        "name": "Weibull reliability x condition-based proportional hazards (illustrative)",
        "weibull": {t: {"beta": v["beta"], "eta": v["eta"], "mean_life_h": round(weibull_mean(v["beta"], v["eta"])),
                        "dominant_failure": v["dominant"]} for t, v in WEIBULL.items()},
        "covariates": {k: {"gamma": v["gamma"], "x": v["x"]} for k, v in COVARIATES.items()},
        "repairs": REPAIRS, "assumptions": COST, "op_hours_per_day": OP_HOURS_PER_DAY,
        "vibration_alarm_x_baseline": mh.VIBRATION_ALARM,
        "formula": "P(fail within d op-h) = 1 - exp(-M * ((t+d)/eta)^beta + M * (t/eta)^beta), M = exp(sum(gamma*x))",
    }


def machine_detail(db: Session, mc: m.Machine, now: datetime) -> dict:
    fleet = Fleet(db, [mc.factory_id], now, machine_ids=[mc.id])
    p = fleet.predictions[0]
    out = {"sim_time": iso(now), "disclaimer": DISCLAIMER, "advisory_id": f"pm:{mc.code}", "machine": public(p)}
    hist = fleet.series.get(mc.id, EMPTY_SERIES)
    b = mc.baseline or {}
    out["telemetry"] = {
        "points": [{"ts": iso(t), "running": bool(row[0] > 0), **{k: (None if math.isnan(v) else round(float(v), 2))
                                                                  for k, v in zip(mh.METRICS, row)}}
                   for t, row in zip(hist.ts, hist.arr)],
        "baseline": b, "units": mh.UNITS,
        "thresholds": {"vibration_alarm": round(b.get("vibration", 0) * mh.VIBRATION_ALARM, 2),
                       "pressure_underfill": round(b.get("pressure", 0) * (1 + COST["underfill_limit_pct"] / 100), 2),
                       "temperature_warning": round(b.get("temperature", 0) * (1 + TOLERANCE["temperature"]), 1)},
    }
    if not p["available"]:
        out["curve"], out["telemetry"]["projection"] = [], []
        return out
    # Probability / hazard curve for the next 14 days (every 6 calendar hours).
    md = p["_model"]
    plan_at = fleet.plan_offset(p)
    curve = []
    for k in range(0, 14 * 4 + 1):
        t = now + timedelta(hours=6 * k)
        oh = op_hours(now, t)
        curve.append({"ts": iso(t), "day": round(k / 4, 2), "op_hours": round(oh, 1),
                      "p_fail": round(1 - fleet.survival(p, oh), 4),
                      "p_fail_planned": round(1 - fleet.survival(p, oh, plan_at), 4) if plan_at is not None else None,
                      "hazard": round(hazard_rate(md["age"] + oh, md["beta"], md["eta"], md["mult"]), 6)})
    out["curve"] = curve
    # Vibration projection to the alarm threshold (operating time mapped to calendar).
    running = hist.running()
    thr = out["telemetry"]["thresholds"]["vibration_alarm"]
    tr = fleet.trend(running, "vibration", thr)
    proj = []
    if tr and tr["slope"] > 0:
        horizon = min(tr["hours_to_threshold"] if tr["hours_to_threshold"] is not None else 48.0, 7 * OP_HOURS_PER_DAY)
        for i in range(0, 9):
            oh = horizon * i / 8
            proj.append({"ts": iso(cal.add_productive(tr["last_ts"], oh * 60)), "op_hours": round(oh, 1),
                         "vibration": round(tr["now"] + tr["slope"] * oh, 3)})
    out["telemetry"]["projection"] = proj
    out["telemetry"]["trend"] = ({"slope_per_op_h": round(tr["slope"], 4), "r2": round(tr["r2"], 2),
                                  "hours_to_alarm": _r(tr["hours_to_threshold"], 1)} if tr else None)
    if mc.machine_type == "Filling Head":
        fw = fill_weight(fleet)
        out["fill_weight"] = fw[0] if fw else None
    out["work_orders"] = [{"id": w.id, "number": w.number, "type": w.wo_type, "status": w.status, "priority": w.priority,
                           "scheduled_start": iso(w.scheduled_start), "scheduled_end": iso(w.scheduled_end),
                           "created_at": iso(w.created_at), "completed_at": iso(w.completed_at), "description": w.description}
                          for w in db.scalars(select(m.WorkOrder).where(m.WorkOrder.machine_id == mc.id)
                                              .order_by(m.WorkOrder.created_at.desc()).limit(8))]
    return out


# ─── advisories ───────────────────────────────────────────────────────
def advisory(id_: str, category: str, severity: str, title: str, summary: str, factory, *, machine=None, prediction=None,
             impact=None, recommendation: str = "", window=None, confidence: float = 0.5, evidence=(), actions=(),
             link=None, **extra) -> dict:
    """The S!AP advisory contract (consumed by the twin, the Executive Overview and the S!AP panel)."""
    return {
        "id": id_, "category": category, "severity": severity, "title": title, "summary": summary,
        "factory": factory.code if factory else None, "factory_id": factory.id if factory else None,
        "machine": machine, "prediction": prediction, "impact": impact, "recommendation": recommendation,
        "window": window, "confidence": round(float(confidence), 2), "evidence": list(evidence),
        "actions": list(actions), "link": link, **extra,
    }


class AdvisoryContext:
    """What a generator receives. `fleet` (all predictions + analytics) is computed on first use."""

    def __init__(self, db: Session, factory_ids, now: datetime):
        self.db, self.fids, self.now = db, list(factory_ids), now
        self.factories = {f.id: f for f in db.scalars(select(m.Factory))}

    @cached_property
    def fleet(self) -> Fleet:
        return Fleet(self.db, self.fids, self.now)

    @cached_property
    def plan(self) -> dict:
        return maintenance_plan(self.fleet)


def _pct(p):
    return f"{p * 100:.0f}%" if p is not None and p >= 0.01 else ("<1%" if p is not None else "—")


def _hrs(h):
    if h is None:
        return "—"
    return f"{h:.0f} op h" if h < 48 else f"{h / OP_HOURS_PER_DAY:.0f} production days"


def _when(ts: str | None) -> str:
    return datetime.fromisoformat(ts).strftime("%a %H:%M") if ts else "—"


def gen_predictive_maintenance(ctx: AdvisoryContext) -> list[dict]:
    out = []
    for p in ctx.fleet.available:
        if not p["severity"]:
            continue
        f = ctx.factories.get(p["factory_id"])
        rep, imp, wo = p["repair"], p["impact"], p.get("work_order")
        title = f"{p['code']} {p['name']}: {rep['mode'].lower()} — {_pct(p['failure_prob_7d'])} failure risk within 7 days"
        summary = (f"{p['line']} ({p['factory']}). The model gives {_pct(p['failure_prob_24h'])} probability of failure in "
                   f"24 h and {_pct(p['failure_prob_7d'])} in 7 days; median remaining life ≈ {_hrs(p['rul_hours'])} "
                   f"(P10–P90 {p['rul_low_hours']:.0f}–{p['rul_high_hours']:.0f} op h). Running to failure would stop the line "
                   f"≈{imp['downtime_hours']:.1f} h and lose ≈{imp['pillows_at_risk']:,} pillows.")
        actions, window = [], p["window"]
        if wo:
            start = wo["scheduled_start"]
            recommendation = (f"Work order {wo['number']} {wo['status'].lower()}" + (f" for {_when(start)}" if start else "")
                              + f"; {_pct(p['p_fail_before_work_order'])} chance of failure before then. "
                              f"Pre-stage {rep['part']} ({rep['crew']} technician(s), {rep['planned_mttr_h']} h).")
            window = {"label": f"{wo['number']} · {wo['status']}", "start": start, "end": wo["scheduled_end"]}
        elif p["recommended"]:
            r = p["recommended"]
            lost = f"{r['pillows_lost']:,} pillows lost" if r["pillows_lost"] else "no pillows lost"
            recommendation = (f"{rep['repair']}. Best window {_when(r['start'])}–{r['end'][11:16]}: {r['label'][0].lower() + r['label'][1:]}, "
                              f"{lost}. Expected loss ${r['expected_loss_usd']:,.0f} vs ${p['rtf_expected_loss_usd']:,.0f} if run to failure.")
            actions = [{"type": "schedule_work_order", "label": f"Schedule {_when(r['start'])}", "machine_id": p["machine_id"],
                        "start": r["start"], "end": r["end"]}]
        else:
            best = next((o for o in p["options"] if o["key"] != "rtf" and o["label"] == window["label"]), None)
            recommendation = (f"Monitor and include in the next preventive service: run-to-failure expected loss "
                              f"(${p['rtf_expected_loss_usd']:,.0f} over 7 days) is below the cost of intervening now"
                              + (f" (${best['expected_loss_usd']:,.0f})" if best else "") + ".")
            if best:
                actions = [{"type": "schedule_work_order", "label": f"Schedule {_when(best['start'])}",
                            "machine_id": p["machine_id"], "start": best["start"], "end": best["end"]}]
        out.append(advisory(
            f"pm:{p['code']}", "Predictive maintenance", p["severity"], title, summary, f,
            machine=machine_ref(p), prediction=prediction_summary(p),
            impact={k: imp[k] for k in ("pillows_at_risk", "downtime_hours", "cost_usd", "orders")}
            | {"orders_count": imp["orders_count"], "orders_late_if_failure": imp["orders_late_if_failure"]},
            recommendation=recommendation, window=window, confidence=p["confidence"], evidence=p["evidence"],
            actions=actions, link={"page": "production", "entity_type": "machine", "entity_id": p["machine_id"]},
            work_order=wo, loss_avoided_usd=p["loss_avoided_usd"], repair={k: rep[k] for k in (
                "repair", "part", "crew", "planned_mttr_h", "unplanned_mttr_h")},
        ))
    return out


def gen_fill_weight(ctx: AdvisoryContext) -> list[dict]:
    out = []
    for r in fill_weight(ctx.fleet):
        if r["risk"] == "Low":
            continue
        p = ctx.fleet.by_id[r["machine"]["id"]]
        f = ctx.factories.get(r["factory_id"])
        rate = r["line_rate_per_h"]
        hrs = r["hours_to_limit"]
        exposure = int(round(rate * min(hrs if hrs is not None else OP_HOURS_PER_DAY, OP_HOURS_PER_DAY)))
        wo = p.get("work_order")
        win = None if wo else night_window(ctx.now)
        out.append(advisory(
            f"fill:{p['code']}", "Quality risk", "warning" if r["risk"] == "High" else "info",
            f"Under-fill risk on {p['line']} ({p['factory']}): fill pressure {r['deviation_pct']:+.1f}%",
            (f"{p['code']} Filling Head pressure is {r['deviation_pct']:+.1f}% vs baseline (projected {r['projected_24h_pct']:+.1f}% "
             f"in 24 h; limit {r['limit_pct']:.0f}%). Estimated fill-weight drift {r['est_fill_drift_g']:+.0f} g on "
             f"{r['sku'] or 'the current SKU'} ({r['fill_g']} g fill)."),
            f, machine=machine_ref(p), prediction=prediction_summary(p),
            impact={"pillows_at_risk": exposure, "downtime_hours": 0.0,
                    "cost_usd": round(exposure * COST["underfill_rework_share"] * ctx.fleet.unit_price(ctx.fleet.lines.get(p["line_id"]))),
                    "orders": p["impact"]["orders"][:3]},
            recommendation=(f"Raise check-weigher sampling on {p['line']} and replace the fill-valve seals"
                            + (f" (work order {wo['number']} open)." if wo else
                               f" in tonight's non-production window ({win[0]:%a %H:%M}).")),
            window=None if wo else {"label": "Non-production window", "start": iso(win[0]),
                                    "end": iso(win[0] + timedelta(hours=REPAIRS['seal']['planned_mttr_h']))},
            confidence=0.55 if r["hours_to_limit"] is None else 0.7,
            evidence=[f"Pressure {r['pressure']} bar vs baseline {r['baseline']} bar ({r['deviation_pct']:+.2f}%)",
                      f"Trend {r['slope_pct_per_op_h']:+.3f}% per operating h"
                      + (f"; under-fill limit in ≈{hrs:.0f} op h" if hrs is not None else ""),
                      f"Fill weight assumed to move {COST['fill_weight_sensitivity']}× the relative pressure change; "
                      f"{COST['underfill_rework_share'] * 100:.0f}% of exposed pillows re-worked (illustrative)"],
            actions=[] if wo else [{"type": "schedule_work_order", "label": f"Schedule {win[0]:%a %H:%M}",
                                    "machine_id": p["machine_id"], "start": iso(win[0]),
                                    "end": iso(win[0] + timedelta(hours=REPAIRS['seal']['planned_mttr_h']))}],
            link={"page": "production", "entity_type": "machine", "entity_id": p["machine_id"]},
        ))
    return out


def gen_spares(ctx: AdvisoryContext) -> list[dict]:
    out = []
    jobs = ctx.plan["jobs"]
    by_f = defaultdict(list)
    for j in jobs:
        by_f[j["factory"]].append(j)
    soon = ctx.now + timedelta(hours=24)
    for fid in ctx.fids:
        f = ctx.factories.get(fid)
        fj = by_f.get(f.code, [])
        if not fj:
            continue
        parts = defaultdict(list)
        for j in fj:
            parts[j["part"]].append(j)
        urgent = [j for j in fj if j["window"]["start"] and datetime.fromisoformat(j["window"]["start"]) <= soon]
        lines = [f"{len(v)}× {k} ({', '.join(j['machine']['code'] for j in v)})" for k, v in parts.items()]
        value = sum(REPAIRS_BY_PART.get(k, 0) * len(v) for k, v in parts.items())
        out.append(advisory(
            f"spares:{f.code}", "Spare parts", "warning" if any(j["severity"] == "critical" for j in urgent) else "info",
            f"{f.code}: pre-stage parts for {len(fj)} predicted repair(s)" + (f", {len(urgent)} within 24 h" if urgent else ""),
            "Parts needed for planned and recommended predictive jobs: " + "; ".join(lines) + ".",
            f, impact={"pillows_at_risk": 0, "downtime_hours": 0.0, "cost_usd": round(value), "orders": []},
            recommendation=("Confirm stock and stage the parts at the line before the window"
                            + (f" — first job {_when(urgent[0]['window']['start'])} ({urgent[0]['machine']['code']})." if urgent else ".")),
            window=({"label": urgent[0]["window"]["label"], "start": urgent[0]["window"]["start"], "end": urgent[0]["window"]["end"]}
                    if urgent else None),
            confidence=0.8,
            evidence=[f"{j['machine']['code']}: {j['failure_mode']} → {j['part']} ({j['status']})" for j in fj[:6]]
            + ["30-day expected demand per part is on the Predictive maintenance tab (Weibull probabilities)."],
        ))
    return out


REPAIRS_BY_PART = {r["part"]: r["part_cost"] for r in REPAIRS.values()}


def gen_technician_capacity(ctx: AdvisoryContext) -> list[dict]:
    out = []
    load = technician_load(ctx.fleet, ctx.plan)
    for s in load["shifts"]:
        if s["past"] or s["load_pct"] is None or s["load_pct"] <= 100:
            continue
        f = ctx.factories.get(s["factory_id"])
        out.append(advisory(
            f"techcap:{s['factory']}:S{s['shift']}", "Maintenance capacity", "warning",
            f"{s['factory']} shift {s['shift']}: maintenance demand {s['demand_h']:.1f} h vs {s['capacity_h']:.1f} h available",
            (f"{s['present']} of {s['rostered']} maintenance technicians present. Planned predictive work {s['planned_h']:.1f} h "
             f"+ expected reactive work {s['reactive_expected_h']:.1f} h."),
            f, impact={"pillows_at_risk": 0, "downtime_hours": 0.0, "cost_usd": 0, "orders": []},
            recommendation="Move non-critical jobs to the non-production window or call in a contractor technician.",
            confidence=0.7, evidence=[load["assumption"]]))
    night = load["after_hours_tonight"]
    if night["jobs"]:
        for fid in ctx.fids:
            f = ctx.factories.get(fid)
            jobs = [j for j in ctx.plan["jobs"] if j["factory"] == f.code and j["window"]["start"]
                    and night["start"] <= j["window"]["start"] < night["end"]]
            if not jobs:
                continue
            hours = sum(j["technician_hours"] for j in jobs)
            crew = max(j["crew"] for j in jobs)
            out.append(advisory(
                f"techcap:{f.code}:night", "Maintenance capacity", "info",
                f"{f.code}: after-hours crew of {crew} needed tonight for {len(jobs)} job(s)",
                (f"{hours:.1f} technician-hours in the non-production window {_when(night['start'])}–{night['end'][11:16]}: "
                 + ", ".join(f"{j['machine']['code']} ({j['failure_mode'].lower()})" for j in jobs) + "."),
                f, impact={"pillows_at_risk": 0, "downtime_hours": 0.0,
                           "cost_usd": round(hours * COST["technician_rate_usd_h"] * COST["after_hours_multiplier"]), "orders": []},
                recommendation="Confirm overtime call-in for the night crew so the zero-loss window is used.",
                window={"label": "Non-production window", "start": night["start"], "end": night["end"]},
                confidence=0.75, evidence=[f"Overtime at {COST['after_hours_multiplier']}× the loaded technician rate (illustrative)",
                                           load["assumption"]]))
    return out


def gen_energy(ctx: AdvisoryContext) -> list[dict]:
    out = []
    e = energy(ctx.fleet)
    by_f = defaultdict(list)
    for r in e["machines"]:
        if r["anomaly"]:
            by_f[r["factory"]].append(r)
    for code, rows in by_f.items():
        f = next((x for x in ctx.factories.values() if x.code == code), None)
        cost = sum(r["cost_30d_usd"] for r in rows)
        out.append(advisory(
            f"energy:{code}", "Energy anomaly", "info",
            f"{code}: {len(rows)} machine(s) drawing {max(r['deviation_pct'] for r in rows):.0f}% above baseline power",
            ("Excess power is an early overload / friction signal. "
             + "; ".join(f"{r['machine']['code']} {r['power_kw']} kW vs {r['baseline_kw']} kW" for r in rows[:3]) + "."),
            f, machine=rows[0]["machine"] if len(rows) == 1 else None,
            impact={"pillows_at_risk": 0, "downtime_hours": 0.0, "cost_usd": round(cost), "orders": []},
            recommendation=f"Check drive-train friction and feed load; ≈${cost:,.0f} excess energy over 30 days at "
                           f"${COST['energy_usd_kwh']}/kWh if unchanged.",
            confidence=0.6, evidence=[f"{r['machine']['code']}: +{r['excess_kwh_day']} kWh per production day" for r in rows[:4]],
            link={"page": "production", "entity_type": "machine", "entity_id": rows[0]["machine"]["id"]}))
    return out


# Extend with additional generators (fn(ctx) -> list[advisory dict]).
def gen_workforce_rfid(ctx: AdvisoryContext) -> list[dict]:
    """RFID badge patterns (people away from stations, chronic time away, store trips, break overruns)."""
    from .twin import workforce_advisories  # local import: twin depends on the simulation package
    return workforce_advisories(ctx.db, ctx.fids, ctx.now)


GENERATORS = [gen_predictive_maintenance, gen_fill_weight, gen_spares, gen_technician_capacity, gen_energy,
              gen_workforce_rfid]


def rank_key(a: dict):
    imp = a.get("impact") or {}
    return (SEVERITY_RANK.get(a.get("severity"), 3), -(imp.get("cost_usd") or 0), -(imp.get("pillows_at_risk") or 0),
            -(a.get("confidence") or 0))


def advisories(db: Session, factory_ids, now: datetime, limit: int | None = None) -> list[dict]:
    ctx = AdvisoryContext(db, factory_ids, now)
    items = []
    for gen in GENERATORS:
        items.extend(gen(ctx))
    items.sort(key=rank_key)
    return items[:limit] if limit else items


# ─── scheduling ───────────────────────────────────────────────────────
class ScheduleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def schedule_work_order(db: Session, advisory_id: str, now: datetime, username: str,
                        start: datetime | None = None, end: datetime | None = None) -> m.WorkOrder:
    kind, _, code = advisory_id.partition(":")
    if kind not in ("pm", "fill") or not code:
        raise ScheduleError(400, f"Advisory {advisory_id} has no schedulable machine action")
    mc = db.scalar(select(m.Machine).where(m.Machine.code == code))
    if not mc:
        raise ScheduleError(404, f"Machine {code} not found")
    fleet = Fleet(db, [mc.factory_id], now, machine_ids=[mc.id])
    p = fleet.predictions[0]
    if p.get("work_order"):
        raise ScheduleError(409, f"{p['work_order']['number']} is already open for {mc.code}")
    if not p["available"]:
        raise ScheduleError(409, f"{mc.code} is in {mc.status.lower()}; it already has an active work order")
    rep = REPAIRS["seal"] if kind == "fill" else REPAIRS[p["failure_mode_key"]]
    if start is None:
        if kind == "fill":
            start = night_window(now)[0]
        else:
            src = p["recommended"] or next((o for o in p["options"] if o["key"] != "rtf" and o["label"] == p["window"]["label"]), None)
            start = datetime.fromisoformat(src["start"]) if src and src.get("start") else now
    start = start.replace(tzinfo=None)
    if end is None or end <= start:
        end = start + timedelta(hours=rep["planned_mttr_h"])
    end = end.replace(tzinfo=None)
    sev = p["severity"]
    wo = m.WorkOrder(
        number=next_number(db, m.WorkOrder, "WO-", 3000), machine_id=mc.id, factory_id=mc.factory_id, wo_type="Predictive",
        status="Scheduled", priority={"critical": "Critical", "warning": "High"}.get(sev, "Medium"),
        description=(f"S!AP {advisory_id}: {rep['mode']} — {rep['repair']}. Part: {rep['part']}. "
                     f"P(7d) {_pct(p['failure_prob_7d'])}, RUL ≈ {p['rul_hours']:.0f} op h (illustrative model).")[:2000],
        created_at=now, acknowledged_at=now, acknowledged_by=username, scheduled_start=start, scheduled_end=end)
    db.add(wo)
    db.flush()
    return wo
