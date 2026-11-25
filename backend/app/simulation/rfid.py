"""Simulated RFID badge tracking (passive UHF badges + zone readers).

Every employee wears a badge. Readers cover each workstation (filling,
sewing, bagging, carton packing), the production floor, the warehouse
aisles and docks, plus the break room, canteen, restrooms, locker room,
material store, supervisor desk and maintenance crib. The simulation writes
the same records an RFID/RTLS middleware would: one interval per continuous
stay of a badge in a zone (rfid_intervals).

States
  Station  badge read at the employee's assigned workstation / work area
  Break    scheduled break or lunch (never counted as time away)
  Away     unscheduled time away from the workstation during working time

Unscheduled time away reduces the employee's presence at the station for that
tick; the engine uses it for sewing capacity (output) and per-person activity,
so time away shows up in line output, work-in-process and productivity.
Badge behaviour uses its own random stream so it never perturbs the rest of
the simulation."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import get_settings
from ..services import calendar as cal

RNG = random.Random(get_settings().sim_seed + 7919)

# zone, weight, (min, max) minutes of a trip away
AWAY_ZONES = [
    ("Restroom", 0.34, (3, 8)),
    ("Material store", 0.20, (3, 9)),
    ("Supervisor desk", 0.10, (2, 6)),
    ("Maintenance crib", 0.07, (3, 8)),
    ("Locker room", 0.11, (4, 11)),
    ("No read · out of coverage", 0.18, (5, 15)),
]
AWAY_READER = {"Restroom": "WC", "Material store": "STORE", "Supervisor desk": "SUP", "Maintenance crib": "MAINT",
               "Locker room": "LOCK", "No read · out of coverage": None, "Break room": "BRK", "Canteen": "CANT"}
BASE_AWAY_RATE = 1 / 190      # unscheduled trips per working minute (≈2.4 per 460-minute shift at propensity 1)
OVERRUN_P = 0.12              # chance a return from break/lunch overruns (× propensity)
AWAY_ALERT_MIN = 10           # a single trip longer than this is flagged on the twin
AWAY_MIN: dict[int, float] = {}  # employee id -> unscheduled minutes away inside this tick's working time
TICK_PM: list[float] = [0.0]      # working minutes in the current tick (denominator for presence)

STATION = {
    "Filler": ("Filling station", "F"),
    "Sewer": ("Sewing table", "S"),
    "Bagger": ("Bagging & compression", "B"),
    "Packaging operator": ("Carton packing", "P"),
}
AREA = {
    "Supervisor": ("Production floor", "FLOOR"),
    "Maintenance technician": ("Maintenance crib", "MAINT"),
    "Warehouse associate": ("FG warehouse aisles", "WH"),
    "Forklift operator": ("Dock & staging lanes", "DOCK"),
}


def reseed(seed: int) -> None:
    RNG.seed(seed + 7919)
    AWAY_MIN.clear()


def badge_id(e: m.Employee) -> str:
    """Stable 96-bit EPC-style tag id shown on the twin."""
    h = random.Random(e.id * 104729).getrandbits(48)
    return f"E280-{h >> 32 & 0xFFFF:04X}-{h >> 16 & 0xFFFF:04X}-{h & 0xFFFF:04X}"


def propensity(e: m.Employee) -> float:
    """How often this person leaves the station (1 = plant norm). Stable per person;
    a few people are chronic. Productivity declines (scenario) raise it."""
    r = random.Random(e.id * 7907).random()
    p = 0.55 + 0.9 * r
    if r > 0.93:
        p = 2.0 + (r - 0.93) * 12  # chronic: 2.0 - 2.8x
    if (e.skill or 1) < 0.85:
        p *= 1.35
    pm = e.productivity_modifier or 1.0
    if pm < 0.999:
        p *= 1 + (1 - pm) * 3.5
    return p


def station_of(e: m.Employee, line: m.ProductionLine | None, factory_code: str, seat: int = 1) -> tuple[str, str]:
    if e.role in STATION and line is not None:
        name, suffix = STATION[e.role]
        if e.role == "Sewer":
            suffix = f"S{seat}"
        return f"{name} · {line.name}", f"RD-{line.code}-{suffix}"
    zone, rd = AREA.get(e.role, ("Production floor", "FLOOR"))
    return zone, f"RD-{factory_code}-{rd}"


def _breaks(d: date, shift: int) -> list[tuple[datetime, datetime, str]]:
    s = cal.SHIFTS[shift]
    out = [(datetime.combine(d, a), datetime.combine(d, b), "Break room") for a, b in s["breaks"]]
    la, lb = s["lunch"]
    out.append((datetime.combine(d, la), datetime.combine(d, lb), "Canteen"))
    return sorted(out)


def _late_minutes(e: m.Employee, d: date) -> float:
    return 12 + random.Random(e.id * 31 + d.toordinal()).random() * 26


class _Walker:
    def __init__(self, db: Session, e: m.Employee, home: tuple[str, str], fcode: str, open_iv,
                 home_reason: str = "At workstation"):
        self.db, self.e, self.home, self.fcode, self.iv = db, e, home, fcode, open_iv
        self.home_reason = home_reason
        self.p = propensity(e)

    def move(self, at: datetime, state: str, zone: str, reason: str | None = None, planned_end: datetime | None = None):
        if self.iv is not None:
            if self.iv.state == state and self.iv.zone == zone:
                return
            self.iv.end = max(at, self.iv.start)
        rd = self.home[1] if state == "Station" else AWAY_READER.get(zone.split(" (")[0], "AUX")
        self.iv = m.RfidInterval(employee_id=self.e.id, factory_id=self.e.factory_id, line_id=self.e.line_id,
                                 date=at.date(), shift=self.e.shift, state=state, zone=zone,
                                 reader=(rd if rd is None or rd.startswith("RD-") else f"RD-{self.fcode}-{rd}"),
                                 reason=reason, start=at, end=None, planned_end=planned_end)
        self.db.add(self.iv)

    def close(self, at: datetime):
        if self.iv is not None:
            self.iv.end = max(at, self.iv.start)
            self.iv = None

    def trip(self, at: datetime, reason: str = "Unscheduled") -> None:
        r, acc = RNG.random(), 0.0
        for zone, w, (lo, hi) in AWAY_ZONES:
            acc += w
            if r <= acc:
                break
        if zone == self.home[0]:  # e.g. a technician based in the crib
            zone, (lo, hi) = AWAY_ZONES[0][0], AWAY_ZONES[0][2]
        dur = RNG.uniform(lo, hi) * (0.8 + 0.4 * min(2.5, self.p))
        self.move(at, "Away", zone, reason, at + timedelta(minutes=dur))

    def walk(self, t0: datetime, t1: datetime, shift_end: datetime) -> float:
        """Advance the badge through [t0, t1); returns unscheduled away minutes in working time."""
        away = 0.0
        d = t0.date()
        brks = _breaks(d, self.e.shift)
        t = t0
        guard = 0
        while t < t1 and guard < 200:
            guard += 1
            brk = next(((bs, be, z) for bs, be, z in brks if bs <= t < be), None)
            if brk:
                self.move(t, "Break", brk[2], "Scheduled " + ("lunch" if brk[2] == "Canteen" else "break"))
                t = min(t1, brk[1])
                continue
            nxt = min([bs for bs, _, _ in brks if bs > t] + [shift_end])
            seg_end = min(t1, nxt)
            iv = self.iv
            if iv is None or iv.state == "Break":
                came_from_break = iv is not None and iv.state == "Break"
                if came_from_break and RNG.random() < OVERRUN_P * self.p:
                    dur = RNG.uniform(2, 7) * (0.8 + 0.3 * min(2.5, self.p))
                    self.move(t, "Away", f"{iv.zone} (overrun)", "Break overrun", t + timedelta(minutes=dur))
                else:
                    self.move(t, "Station", self.home[0], self.home_reason)
                continue
            if iv.state == "Away":
                pe = iv.planned_end or t
                if pe > t:
                    stop = min(pe, seg_end)
                    away += (stop - t).total_seconds() / 60
                    t = stop
                    continue
                self.move(t, "Station", self.home[0], "Returned to workstation")
                continue
            if iv.zone != self.home[0]:  # assignment changed (technician dispatched / back to crib)
                self.move(t, "Station", self.home[0], self.home_reason)
                continue
            # at the station: time to the next unscheduled trip (memoryless)
            rate = BASE_AWAY_RATE * self.p
            gap = RNG.expovariate(rate) if rate > 0 else 1e9
            nt = t + timedelta(minutes=gap)
            if nt < seg_end:
                self.trip(nt)
                t = nt
            else:
                t = seg_end
        return away


def simulate(db: Session, a: datetime, b: datetime, lines: dict[int, m.ProductionLine],
             factories: dict[int, m.Factory], working_minutes: float) -> None:
    """Move every badge through [a, b). Called once per tick before production."""
    AWAY_MIN.clear()
    TICK_PM[0] = working_minutes
    open_iv = {iv.employee_id: iv for iv in db.scalars(select(m.RfidInterval).where(m.RfidInterval.end.is_(None)))}
    emps = list(db.scalars(select(m.Employee).order_by(m.Employee.id)))
    # machines waiting for / under repair pull maintenance technicians to the line
    down: dict[int, list] = {}
    for mc in db.scalars(select(m.Machine).where(m.Machine.status.in_(("Breakdown", "Maintenance"))).order_by(m.Machine.id)):
        ln = lines.get(mc.line_id)
        if ln is not None:
            down.setdefault(mc.factory_id, []).append((mc, ln))
    seat: dict[tuple, int] = {}
    d = a.date()
    for e in emps:
        iv = open_iv.get(e.id)
        s_start, s_end = cal.shift_bounds(d, e.shift)
        # a stale open interval from a previous day is closed at its shift end
        if iv is not None and iv.date != d:
            iv.end = max(iv.start, cal.shift_bounds(iv.date, iv.shift)[1])
            iv = None
        if e.attendance not in ("Present", "Late"):
            if iv is not None:
                iv.end = max(iv.start, a)
            continue
        line = lines.get(e.line_id)
        k = (e.line_id, e.shift, e.role)
        seat[k] = seat.get(k, 0) + 1
        f = factories.get(e.factory_id)
        home = station_of(e, line, f.code if f else "PLANT", seat[k])
        reason = "At workstation"
        if e.role == "Maintenance technician" and down.get(e.factory_id):
            jobs = down[e.factory_id]
            mc, ln = jobs[(seat[k] - 1) % len(jobs)]
            home = (f"{mc.code} {mc.name} · {ln.name}", f"RD-{ln.code}-M")
            reason = f"Repair job {mc.code}"
        arrive = s_start + timedelta(minutes=_late_minutes(e, d)) if e.attendance == "Late" else s_start
        lo, hi = max(a, arrive), min(b, s_end)
        w = _Walker(db, e, home, f.code if f else "PLANT", iv, reason)
        if hi > lo:
            AWAY_MIN[e.id] = w.walk(lo, hi, s_end)
        if w.iv is not None and b >= s_end:
            w.close(s_end)


def presence(emp_id: int) -> float:
    """Share of this tick's working time the badge was at the station (1 = whole tick)."""
    pm = TICK_PM[0]
    if pm <= 0:
        return 1.0
    return max(0.0, 1.0 - AWAY_MIN.get(emp_id, 0.0) / pm)


def close_all(db: Session, at: datetime) -> None:
    for iv in db.scalars(select(m.RfidInterval).where(m.RfidInterval.end.is_(None))):
        iv.end = max(iv.start, min(at, cal.shift_bounds(iv.date, iv.shift)[1]))
