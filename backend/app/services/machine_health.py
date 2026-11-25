"""Machine health: simulated telemetry, a transparent health score, anomaly
detection and a simple trend-based maintenance prediction.

This is an illustrative POC model on simulated data. It is NOT a validated
failure-prediction model; every output carries that caveat."""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import calendar as cal

METRICS = ("rpm", "temperature", "vibration", "pressure", "power")
UNITS = {"rpm": "rpm", "temperature": "°C", "vibration": "mm/s", "pressure": "bar", "power": "kW"}
VIBRATION_ALARM = 1.9  # x baseline -> intervention threshold
DISCLAIMER = "Simulated telemetry and an illustrative model. Not a validated failure prediction."

BASELINES = {
    "Fibre Opener":       {"rpm": 950,  "temperature": 46, "vibration": 2.2, "pressure": 5.8, "power": 11.0},
    "Flapper Unit":       {"rpm": 620,  "temperature": 44, "vibration": 3.0, "pressure": 6.0, "power": 7.5},
    "Garnett Main Motor": {"rpm": 2000, "temperature": 62, "vibration": 2.8, "pressure": 6.2, "power": 18.0},
    "Blow Fan Motor":     {"rpm": 3200, "temperature": 58, "vibration": 2.4, "pressure": 6.4, "power": 15.0},
    "Filling Head":       {"rpm": 1450, "temperature": 50, "vibration": 2.0, "pressure": 6.5, "power": 9.0},
}
STAGE_OF = {"Fibre Opener": "Mixing", "Flapper Unit": "Flapper Processing",
            "Garnett Main Motor": "Bun Formation", "Blow Fan Motor": "Bun Formation",
            "Filling Head": "Pillow Filling"}


def wear(mc: m.Machine) -> float:
    return (mc.degradation or 0.0) * (mc.hours_since_maintenance or 0.0)


def reading(mc: m.Machine, running: bool, rng: random.Random) -> dict:
    b = mc.baseline
    if mc.status == "Breakdown":
        return {"rpm": round(b["rpm"] * rng.uniform(0.0, 0.05)), "temperature": round(b["temperature"] * 1.32, 1),
                "vibration": round(b["vibration"] * rng.uniform(3.2, 3.8), 2), "pressure": round(b["pressure"] * 0.6, 2),
                "power": round(b["power"] * 0.08, 2)}
    if mc.status == "Maintenance" or not running:
        return {"rpm": 0, "temperature": round(b["temperature"] * 0.72 + rng.uniform(-0.5, 0.5), 1),
                "vibration": round(rng.uniform(0.05, 0.2), 2), "pressure": round(b["pressure"] * 0.95, 2),
                "power": round(b["power"] * 0.06, 2)}
    w = wear(mc)
    n = lambda s: 1 + rng.gauss(0, s)  # noqa: E731
    return {
        "rpm": round(b["rpm"] * (1 + 0.017 * w) * n(0.002)),
        "temperature": round(b["temperature"] * (1 + 0.24 * w) * n(0.008), 1),
        "vibration": round(b["vibration"] * (1 + 1.1 * w) * n(0.015), 2),
        "pressure": round(b["pressure"] * (1 - 0.05 * w) * n(0.01), 2),
        "power": round(b["power"] * (1 + 0.12 * w) * n(0.015), 2),
    }


def score_components(mc: m.Machine, history: list[m.TelemetryPoint] | None = None) -> dict:
    """Penalty breakdown that makes up the 0-100 score."""
    b = mc.baseline
    if mc.status == "Breakdown":
        return {"total": 25.0, "penalties": {"breakdown": 75.0}}
    cur = {k: getattr(mc, k) for k in METRICS}
    if not cur["rpm"]:  # idle/maintenance: score from wear only
        w = wear(mc)
        pen = {"wear": round(min(40.0, w * 60), 1)}
        return {"total": round(max(0.0, 100 - sum(pen.values())), 1), "penalties": pen}
    r = {k: (cur[k] / b[k]) if b[k] else 1.0 for k in METRICS}
    pen = {
        "vibration": min(40.0, max(0.0, (r["vibration"] - 1.05) * 40)),
        "temperature": min(20.0, max(0.0, (r["temperature"] - 1.02) * 80)),
        "rpm": min(10.0, abs(r["rpm"] - 1) * 300),
        "pressure": min(10.0, max(0.0, abs(r["pressure"] - 1) - 0.01) * 100),
        "power": min(10.0, max(0.0, (r["power"] - 1.03) * 60)),
    }
    if history and len(history) >= 12:
        temps = np.array([h.temperature for h in history[-24:] if h.rpm])
        if len(temps) >= 8:
            slope = float(np.polyfit(np.arange(len(temps)), temps, 1)[0])
            pen["temperature_trend"] = min(5.0, max(0.0, slope * 25))
    pen["hours_since_service"] = min(5.0, max(0.0, ((mc.hours_since_maintenance or 0) - 1500) / 300))
    pen = {k: round(v, 1) for k, v in pen.items() if v > 0.05}
    return {"total": round(max(0.0, 100 - sum(pen.values())), 1), "penalties": pen}


def classify(score: float) -> str:
    return "Healthy" if score >= 85 else "Warning" if score >= 65 else "Critical"


def history(db: Session, machine_id: int, limit: int = 144) -> list[m.TelemetryPoint]:
    rows = list(db.scalars(select(m.TelemetryPoint).where(m.TelemetryPoint.machine_id == machine_id)
                           .order_by(m.TelemetryPoint.ts.desc()).limit(limit)))
    return rows[::-1]


def anomalies(mc: m.Machine, hist: list[m.TelemetryPoint]) -> list[dict]:
    """Rolling z-score of the latest readings against the earlier window,
    plus deviation from the nameplate baseline."""
    out = []
    running = [h for h in hist if h.rpm]
    b = mc.baseline
    for k in METRICS:
        dev = (getattr(mc, k) or 0) / b[k] - 1 if b[k] and getattr(mc, k) else 0.0
        z = None
        if len(running) >= 20:
            vals = np.array([getattr(h, k) for h in running], dtype=float)
            ref, recent = vals[: int(len(vals) * 0.6)], vals[-6:]
            sd = float(ref.std()) or 1e-6
            z = float((recent.mean() - ref.mean()) / sd)
        tol = {"rpm": 0.008, "temperature": 0.06, "vibration": 0.25, "pressure": 0.04, "power": 0.06}[k]
        if (z is not None and abs(z) > 3 and abs(dev) > tol / 2) or abs(dev) > tol:
            direction = "above" if dev > 0 else "below"
            out.append({"metric": k, "severity": round(abs(dev) / tol, 2), "deviation_pct": round(dev * 100, 1),
                        "z_score": round(z, 1) if z is not None else None,
                        "message": f"{k.capitalize()} {abs(dev) * 100:.1f}% {direction} baseline"
                                   + (f" (z={z:+.1f})" if z is not None else "")})
    return sorted(out, key=lambda a: -a["severity"])


def prediction(mc: m.Machine, hist: list[m.TelemetryPoint], now: datetime) -> dict | None:
    score = mc.health_score
    running = [h for h in hist if h.rpm]
    hours_to = None
    if len(running) >= 12:
        window = running[-72:]
        vib = np.array([h.vibration for h in window], dtype=float)
        ts = np.array([(h.ts - window[0].ts).total_seconds() / 3600 for h in window])
        if ts[-1] > 0:
            slope = float(np.polyfit(ts, vib, 1)[0])  # mm/s per operating hour
            thr = mc.baseline["vibration"] * VIBRATION_ALARM
            if slope > 0.002:
                hours_to = max(0.0, (thr - mc.vibration) / slope)
    if score >= 85 and (hours_to is None or hours_to > 120):
        return None
    risk = "High" if score < 65 or (hours_to is not None and hours_to < 24) else "Medium"
    eta_h = hours_to if hours_to is not None else (48 if score >= 65 else 12)
    _, shift_end = cal.shift_bounds(now.date(), 2)
    hours_to_night = max(0.0, (shift_end - now).total_seconds() / 3600)
    if eta_h <= hours_to_night * 1.2 or score < 55:
        window = f"Next planned stop today (before {min(now + timedelta(hours=max(1, eta_h * 0.6)), shift_end):%H:%M})"
    else:
        window = f"Non-production window {shift_end:%a %H:%M}–{(shift_end + timedelta(hours=2)):%H:%M}"
    anoms = anomalies(mc, hist)
    out = {
        "machine_id": mc.id, "machine": mc.code, "name": mc.name, "line_id": mc.line_id,
        "health_score": round(score, 1), "status": classify(score),
        "anomaly": "; ".join(a["message"] for a in anoms[:3]) or "Wear trend above normal",
        "risk": risk, "est_hours_to_maintenance": round(eta_h, 1),
        "recommended_action": ("Inspect drive-end bearing and coupling alignment; check lubrication"
                               if any(a["metric"] == "vibration" for a in anoms)
                               else "Inspect cooling and motor load; verify fibre feed"),
        "suggested_window": window,
        "disclaimer": DISCLAIMER,
    }
    _align_with_siap(mc, now, out)
    return out


def _align_with_siap(mc: m.Machine, now: datetime, out: dict) -> None:
    """Use the S!AP reliability model (services/predictive.py) for the time to
    maintenance, risk and window, so every screen quotes the same prediction."""
    from sqlalchemy.orm import object_session
    db = object_session(mc)
    if db is None:
        return
    try:
        from . import predictive
        p = predictive.Fleet(db, [mc.factory_id], now, machine_ids=[mc.id]).predictions[0]
    except Exception:  # keep the trend-only estimate if the model cannot run
        return
    if p.get("rul_hours") is not None:
        out["est_hours_to_maintenance"] = round(float(p["rul_hours"]), 1)
    out["est_unit"] = "operating hours"
    out["risk"] = "High" if p.get("severity") == "critical" else "Medium"
    out["failure_prob_7d"] = p.get("failure_prob_7d")
    out["failure_mode"] = p.get("failure_mode")
    w = p.get("window") or {}
    if w.get("start"):
        a = datetime.fromisoformat(w["start"])
        b = datetime.fromisoformat(w["end"]) if w.get("end") else None
        out["suggested_window"] = f"{w['label']} · {a:%a %H:%M}" + (f"–{b:%H:%M}" if b else "")
    out["model"] = "S!AP reliability model"


def update_machine(db: Session, mc: m.Machine, running: bool, op_hours: float, now: datetime,
                   rng: random.Random, record: bool) -> None:
    if running and mc.status == "Running":
        mc.operating_hours = (mc.operating_hours or 0) + op_hours
        mc.hours_since_maintenance = (mc.hours_since_maintenance or 0) + op_hours
    r = reading(mc, running, rng)
    for k, v in r.items():
        setattr(mc, k, v)
    if record:
        db.add(m.TelemetryPoint(machine_id=mc.id, ts=now, **r))
        hist = history(db, mc.id, 30) if mc.status == "Running" else None
        mc.health_score = score_components(mc, hist)["total"]
